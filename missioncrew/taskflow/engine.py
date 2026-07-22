"""引擎:任务生命周期编排。

平台在这里完成"控制平面"的职责:建计划 -> 路由 -> 装配 -> 执行 -> 收证据 ->
过门禁 -> 推进/升级/阻塞。Agent 在阶段内部如何工作,引擎不干涉。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import assembler, resources, router, workflow
from ..runtime import runtime_manager
from ..collab.documents import library_for, normalize_document_resource_urls
from ..collab.workspace import sync_task_files, write_task_files
from ..core.models import Task, TaskStage, new_id
from ..core.store import Store

MAX_ATTEMPTS = 3  # 每阶段最多尝试次数(0->economy 1->standard 2->expert)


@dataclass
class StepReport:
    task: Task
    message: str
    halted: bool  # True 表示本轮不应继续推进(等审批/阻塞/失败/完成)


class Engine:
    def __init__(self, store: Store):
        self.store = store

    # ---- 任务创建 ----
    def create_task(self, project_id: str, title: str, description: str = "",
                    task_type: str = "feature", labels: Optional[list[str]] = None,
                    risk: str = "normal", security_level: int = 0,
                    max_tier: Optional[str] = None) -> Task:
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"项目不存在: {project_id}")
        labels = labels or []
        task = Task(
            id=new_id("t"), project_id=project_id, title=title, description=description,
            task_type=task_type, labels=labels, risk=risk,
            security_level=security_level, max_tier=max_tier,
            stages=workflow.build_plan(task_type, risk),
        )
        self.store.put_task(task)
        plan = " -> ".join(s.name for s in task.stages)
        self.store.audit("platform", "task_created", task.id,
                         f"type={task_type} risk={risk} plan=[{plan}]")
        return task

    # ---- 审批 ----
    def approve(self, task_id: str, approver: str, decision: str = "approved",
                note: str = "", stage_name: Optional[str] = None) -> Task:
        task = self._must_get(task_id)
        stage = task.current_stage
        if stage_name:
            stage = next((s for s in task.stages if s.name == stage_name), None)
        if stage is None:
            raise ValueError("找不到待审批的阶段")
        self.store.add_approval(task_id, stage.name, approver, decision, note)
        self.store.audit(approver, f"approval_{decision}", task_id,
                         f"stage={stage.name} note={note}")
        if task.status == "awaiting_approval" and stage is task.current_stage:
            if decision == "approved":
                task.status = "open"   # 交回引擎,下一次 step 会核验并放行
            else:
                task.status = "blocked"
        self.store.put_task(task)
        return task

    # ---- 推进 ----
    def run(self, task_id: str, max_steps: int = 30) -> list[StepReport]:
        """连续推进任务,直到完成、失败、阻塞或等待人工输入。"""
        reports = []
        for _ in range(max_steps):
            report = self.step(task_id)
            reports.append(report)
            if report.halted:
                break
        return reports

    def step(self, task_id: str) -> StepReport:
        """推进一步:执行当前阶段一次,或核验门禁后进入下一阶段。"""
        task = self._must_get(task_id)
        if task.status in ("done", "failed", "blocked"):
            return StepReport(task, f"任务处于终态/阻塞态: {task.status}", True)
        if task.status == "awaiting_approval":
            return StepReport(task, "等待人工审批中", True)

        stage = task.current_stage
        if stage is None:  # 计划已走完
            return self._finish(task)

        if stage.status == "awaiting_approval":
            return self._check_approval(task, stage)
        if stage.kind == "final":
            return self._final_stage(task, stage)
        return self._agent_stage(task, stage)

    # ---- 内部:Agent 阶段 ----
    def _agent_stage(self, task: Task, stage: TaskStage) -> StepReport:
        project = self.store.get_project(task.project_id)
        assert project is not None

        decision = router.route(self.store, task, stage, project)
        if decision.backend is None:
            task.status = "blocked"
            self.store.put_task(task)
            self.store.audit("platform", "routing_failed", task.id, decision.reason)
            return StepReport(task, f"阶段 {stage.name} 无可用后端: {decision.reason}", True)

        backend = decision.backend
        env, notes = resources.grant_resources(self.store, task, stage, project)
        cfg = assembler.assemble(
            task, stage, project, backend, env, notes, decision.trace, self.store)
        self.store.audit("platform", "stage_dispatch", task.id,
                         f"stage={stage.name} backend={backend.id} {decision.reason}")

        library = library_for(project.id)
        library.commit_changes("platform", "Capture external document changes before task run")
        result = runtime_manager.start(cfg)
        document_roots = [
            library.root, cfg.env.get("MISSIONCREW_DOCUMENTS_DIR", "")]
        result.summary = normalize_document_resource_urls(
            result.summary, project.id, document_roots)
        result.output = normalize_document_resource_urls(
            result.output, project.id, document_roots)
        revision = library.commit_changes(
            f"task:{task.id}", f"Documents updated in stage {stage.name}")
        if revision:   # 执行中的文档改动进平台审计,与 API 写入口径一致
            self.store.audit(f"task:{task.id}", "documents_committed", task.id,
                             f"stage={stage.name} revision={revision[:10]}")
        task_sync_errors = sync_task_files(
            self.store, project.id, Path(cfg.env["MISSIONCREW_TASKS_DIR"]),
            f"task:{task.id}")
        write_task_files(
            self.store, project.id, Path(cfg.env["MISSIONCREW_TASKS_DIR"]))
        if task_sync_errors:
            self.store.audit(
                f"task:{task.id}", "task_workspace_sync_failed", task.id,
                "; ".join(task_sync_errors))
        # 任务可在 workspace 中编辑；当前执行仍沿用已派发阶段计划，但保留
        # Agent 对任务描述和可编辑元数据的更新，避免随后 put_task 覆盖它们。
        workspace_task = self.store.get_task(task.id)
        if workspace_task is not None:
            for field_name in ("title", "description", "task_type", "labels", "risk",
                               "security_level", "max_tier"):
                setattr(task, field_name, getattr(workspace_task, field_name))

        # 记账:配额扣减(工具级,重取注册表记录,避免模型副本覆盖工具条目)
        stored = self.store.get_backend(backend.id)
        if stored is not None and stored.quota is not None:
            stored.quota = max(0.0, stored.quota - backend.cost_per_run)
            self.store.put_backend(stored)
        self._ingest_manifest(task, stage, cfg.workdir)
        present = self.store.evidence_types(task.id)
        missing = workflow.gate_check(stage, present)
        success = result.success and not missing
        self.store.stats_record(backend.id, project.id, task.task_type, success)
        self.store.add_run(task.id, stage.name, backend.id, backend.tier, success,
                           backend.cost_per_run, result.summary,
                           json.dumps(decision.trace, ensure_ascii=False), cfg.workdir)

        if not success:
            reason = result.summary if not result.success else f"证据缺失: {', '.join(missing)}"
            return self._handle_failure(task, stage, backend.id, reason)

        stage.backend_id = backend.id
        if stage.human_gate and not self._approved(task, stage):
            stage.status = "awaiting_approval"
            task.status = "awaiting_approval"
            self.store.put_task(task)
            return StepReport(task, f"阶段 {stage.name} 完成,等待人工审批", True)
        return self._pass_stage(task, stage, f"由 {backend.id} 完成,证据齐备")

    # ---- 内部:final 阶段(平台收尾,无 Agent) ----
    def _final_stage(self, task: Task, stage: TaskStage) -> StepReport:
        present = self.store.evidence_types(task.id)
        missing = workflow.gate_check(stage, present)
        if missing:
            task.status = "blocked"
            self.store.put_task(task)
            self.store.audit("platform", "gate_blocked", task.id,
                             f"stage={stage.name} 缺失证据: {missing}")
            return StepReport(task, f"收尾门禁未过,缺失证据: {', '.join(missing)}", True)
        if stage.human_gate and not self._approved(task, stage):
            stage.status = "awaiting_approval"
            task.status = "awaiting_approval"
            self.store.put_task(task)
            return StepReport(task, f"证据齐备,阶段 {stage.name} 等待人工审批", True)
        return self._pass_stage(task, stage, "证据与审批核验通过")

    # ---- 内部:门禁与推进 ----
    def _check_approval(self, task: Task, stage: TaskStage) -> StepReport:
        approval = self.store.get_approval(task.id, stage.name)
        if approval is None:
            task.status = "awaiting_approval"
            self.store.put_task(task)
            return StepReport(task, f"阶段 {stage.name} 等待人工审批", True)
        if approval["decision"] != "approved":
            task.status = "blocked"
            stage.status = "failed"
            self.store.put_task(task)
            return StepReport(task, f"阶段 {stage.name} 审批被拒绝: {approval['note']}", True)
        return self._pass_stage(task, stage, f"人工审批通过(by {approval['approver']})")

    def _approved(self, task: Task, stage: TaskStage) -> bool:
        a = self.store.get_approval(task.id, stage.name)
        return bool(a and a["decision"] == "approved")

    def _pass_stage(self, task: Task, stage: TaskStage, why: str) -> StepReport:
        stage.status = "passed"
        task.stage_index += 1
        task.status = "open"
        self.store.audit("platform", "stage_passed", task.id, f"stage={stage.name} {why}")
        if task.stage_index >= len(task.stages):
            return self._finish(task)
        self.store.put_task(task)
        return StepReport(task, f"阶段 {stage.name} 通过: {why}", False)

    def _handle_failure(self, task: Task, stage: TaskStage, backend_id: str,
                        reason: str) -> StepReport:
        stage.attempts += 1
        self.store.audit("platform", "stage_failed", task.id,
                         f"stage={stage.name} backend={backend_id} attempt={stage.attempts} {reason}")
        if stage.attempts >= MAX_ATTEMPTS:
            stage.status = "failed"
            task.status = "failed"
            self.store.put_task(task)
            return StepReport(task, f"阶段 {stage.name} 连续失败 {stage.attempts} 次,任务失败", True)
        self.store.put_task(task)
        return StepReport(
            task,
            f"阶段 {stage.name} 失败({reason}),升级后重试(下限档位上调至 "
            f"attempt={stage.attempts})", False)

    def _finish(self, task: Task) -> StepReport:
        task.status = "done"
        self.store.put_task(task)
        self.store.audit("platform", "task_done", task.id)
        return StepReport(task, "任务完成:全部阶段通过,证据可审计", True)

    def _ingest_manifest(self, task: Task, stage: TaskStage, workdir: str) -> None:
        """从工作区 manifest 摄入新增证据(按 type+path 去重)。"""
        known = {(e["type"], e["path"]) for e in self.store.list_evidence(task.id)}
        for entry in assembler.read_manifest(workdir):
            ev_type, path = entry.get("type"), entry.get("path")
            if not ev_type or not path or (ev_type, path) in known:
                continue
            self.store.add_evidence(task.id, stage.name, ev_type, path,
                                    entry.get("summary", ""))
            known.add((ev_type, path))

    def _must_get(self, task_id: str) -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        return task
