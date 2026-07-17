"""计划构建与端到端流程(mock 后端)。"""
from missioncrew.workflow import build_plan


def _plan_names(seeded, task_type, labels=None, risk="normal"):
    project = seeded.get_project("webshop")
    return [s.name for s in build_plan(task_type, labels or [], risk, project)]


def test_bug_plan_has_reproduce_and_regression(seeded):
    names = _plan_names(seeded, "bug")
    assert names == ["reproduce", "fix", "regression", "review", "merge"]


def test_auth_rule_inserts_security_review(seeded):
    names = _plan_names(seeded, "feature", labels=["auth"])
    assert "security_review" in names
    assert names.index("security_review") < names.index("review")


def test_high_risk_requires_human_approval(seeded):
    project = seeded.get_project("webshop")
    plan = build_plan("feature", [], "high", project)
    assert plan[-1].human_gate is True


def test_ui_rule_adds_multimodal_and_evidence(seeded):
    project = seeded.get_project("webshop")
    plan = build_plan("feature", ["ui"], "normal", project)
    verify = next(s for s in plan if s.kind == "verify")
    assert "multimodal" in verify.required_capabilities
    assert {"browser_test", "screenshot"} <= set(verify.produces)
    # 门禁层面同样强制
    assert {"browser_test", "screenshot"} <= set(plan[-1].requires_evidence)


# ---- 端到端 ----

def test_bug_task_end_to_end(engine):
    t = engine.create_task("webshop", "结算金额错误", task_type="bug")
    reports = engine.run(t.id)
    final = engine.store.get_task(t.id)
    assert final.status == "done", [r.message for r in reports]
    assert all(s.status == "passed" for s in final.stages)
    # 证据齐备:复现、变更、回归、审查
    types = engine.store.evidence_types(t.id)
    assert {"reproduction", "change_summary", "regression_test", "review_report"} <= types
    # 独立审查:review 阶段执行者未参与 work 阶段
    review = next(s for s in final.stages if s.name == "review")
    work_ids = {s.backend_id for s in final.stages if s.kind in ("work", "verify")}
    assert review.backend_id not in work_ids


def test_hard_task_escalates_from_economy(engine):
    t = engine.create_task("webshop", "疑难 bug", task_type="bug", labels=["hard"])
    engine.run(t.id)
    final = engine.store.get_task(t.id)
    assert final.status == "done"
    runs = engine.store.list_runs(t.id)
    # 第一次经济档失败,随后升级成功
    first = runs[0]
    assert first["tier"] == "economy" and not first["success"]
    succeeded_tiers = {r["tier"] for r in runs if r["success"]}
    assert "economy" not in succeeded_tiers


def test_high_risk_blocks_until_approved(engine):
    t = engine.create_task("webshop", "改登录", task_type="feature",
                           labels=["auth"], risk="high")
    engine.run(t.id)
    mid = engine.store.get_task(t.id)
    assert mid.status == "awaiting_approval"
    assert mid.current_stage.name == "merge"
    # 安全审查阶段已由具备 security 能力的独立后端完成
    assert "security_review_report" in engine.store.evidence_types(t.id)

    engine.approve(t.id, "alice")
    engine.run(t.id)
    final = engine.store.get_task(t.id)
    assert final.status == "done"


def test_rejection_blocks_task(engine):
    t = engine.create_task("webshop", "高危变更", risk="high")
    engine.run(t.id)
    engine.approve(t.id, "alice", decision="rejected", note="先补充回滚方案")
    final = engine.store.get_task(t.id)
    assert final.status == "blocked"


def test_very_hard_task_fails_after_max_attempts_without_expert(engine):
    # 限制预算到 standard,very-hard 任务永远无法成功 -> 最终 failed/blocked
    t = engine.create_task("webshop", "超难任务", task_type="chore",
                           labels=["very-hard"], max_tier="standard")
    engine.run(t.id)
    final = engine.store.get_task(t.id)
    assert final.status in ("failed", "blocked")


def test_resource_granted_and_audited(engine):
    t = engine.create_task("webshop", "修 bug", task_type="bug")
    engine.run(t.id)
    actions = {a["action"] for a in engine.store.list_audit(t.id)}
    assert "resource_granted" in actions
