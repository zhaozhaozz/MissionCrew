"""上下文装配器:把项目准则、阶段目标、证据契约、资源说明组装成执行配置。

这是"同一个后端可以做多种工作"的落点:领域理解来自这里装配的上下文,
而不是来自固定的 Agent 岗位。
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import workspaces_dir
from .models import Backend, ExecutionConfig, Project, Task, TaskStage

# 证据契约:所有后端(真实或 Mock)统一通过工作区 manifest 提交证据
MANIFEST = "evidence/manifest.json"

PROMPT_TEMPLATE = """\
# 任务
标题: {title}
类型: {task_type} / 风险: {risk} / 标签: {labels}

{description}

# 项目背景({project_name})
{charter}

# 开发准则
{dev_guidelines}
{skills_section}
# 当前阶段: {stage_name}
{goal}

# 证据契约(必须遵守)
完成工作后,把证据文件写入工作区 evidence/ 目录,并在 {manifest} 中追加记录,
格式为 JSON 数组,每项: {{"type": "...", "path": "evidence/xxx", "summary": "..."}}。
本阶段必须产出的证据类型: {produces}
任务是否完成由平台核验证据决定,不要只在回复中声称完成。
{resources_section}"""


def workspace_for(task_id: str) -> Path:
    ws = workspaces_dir() / task_id
    (ws / "evidence").mkdir(parents=True, exist_ok=True)
    return ws


def read_manifest(workdir: str | Path) -> list[dict]:
    p = Path(workdir) / MANIFEST
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def assemble(task: Task, stage: TaskStage, project: Project, backend: Backend,
             env: dict, resource_notes: list[str], trace: list[str]) -> ExecutionConfig:
    ws = workspace_for(task.id)

    skills_section = ""
    if project.skills:
        skills_section = "\n# 可用 Skills\n" + "\n".join(f"- {s}" for s in project.skills) + "\n"
    resources_section = ""
    if resource_notes:
        resources_section = "\n# 受控资源(平台已授权,任务结束自动回收)\n" + "\n".join(resource_notes) + "\n"

    prompt = PROMPT_TEMPLATE.format(
        title=task.title,
        task_type=task.task_type,
        risk=task.risk,
        labels=", ".join(task.labels) or "无",
        description=task.description or "(无补充描述)",
        project_name=project.name,
        charter=project.charter or "(未配置)",
        dev_guidelines=project.dev_guidelines or "(未配置)",
        skills_section=skills_section,
        stage_name=stage.name,
        goal=stage.goal,
        manifest=MANIFEST,
        produces=", ".join(stage.produces) or "无强制要求",
        resources_section=resources_section,
    )
    return ExecutionConfig(
        task_id=task.id,
        stage_name=stage.name,
        backend=backend,
        prompt=prompt,
        workdir=str(ws),
        env=env,
        routing_trace=trace,
    )
