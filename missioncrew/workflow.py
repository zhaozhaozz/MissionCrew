"""工作流引擎的静态部分:按任务类型生成阶段计划,并用项目验证准则修正。

工作流只控制阶段和门禁,不干涉 Agent 内部如何搜索代码、拆分任务、写代码。
"""
from __future__ import annotations

from .models import Project, TaskStage

# 基础工作流:每类任务的阶段骨架。final 阶段无 Agent,只做门禁与收尾。
BASE_WORKFLOWS: dict[str, list[dict]] = {
    "feature": [
        dict(name="understand", kind="work", produces=["plan"],
             goal="理解需求,识别歧义,产出实施计划与验收标准(evidence 类型 plan)。"),
        dict(name="develop", kind="work", produces=["change_summary"],
             goal="按计划完成代码修改,产出变更摘要(evidence 类型 change_summary)。"),
        dict(name="verify", kind="verify", produces=["test_report"],
             goal="运行测试并验证验收标准,产出测试报告(evidence 类型 test_report)。"),
        dict(name="review", kind="review", produces=["review_report"], independent=True,
             goal="独立审查代码变更与全部证据,产出审查结论(evidence 类型 review_report)。"),
        dict(name="merge", kind="final", goal="平台核验全部证据与审批后合入并关闭任务。"),
    ],
    "bug": [
        dict(name="reproduce", kind="work", produces=["reproduction"],
             goal="分析并复现问题,给出修复前失败的复现证据(evidence 类型 reproduction);"
                  "无法复现时说明所需补充信息。"),
        dict(name="fix", kind="work", produces=["change_summary"],
             goal="根因分析并修复,产出变更摘要(evidence 类型 change_summary)。"),
        dict(name="regression", kind="verify", produces=["regression_test"],
             goal="运行回归测试,证明修复前失败、修复后通过(evidence 类型 regression_test)。"),
        dict(name="review", kind="review", produces=["review_report"], independent=True,
             goal="独立审查修复与回归证据,产出审查结论(evidence 类型 review_report)。"),
        dict(name="merge", kind="final", goal="平台核验全部证据与审批后合入并关闭任务。"),
    ],
    "chore": [
        dict(name="develop", kind="work", produces=["change_summary"],
             goal="完成杂项改动,产出变更摘要。"),
        dict(name="verify", kind="verify", produces=["test_report"],
             goal="验证改动无回归,产出测试报告。"),
        dict(name="merge", kind="final", goal="平台核验证据后合入并关闭任务。"),
    ],
    "research": [
        dict(name="investigate", kind="work", produces=["research_report"],
             goal="调研问题,产出调研报告(evidence 类型 research_report)。"),
        dict(name="review", kind="review", produces=["review_report"], independent=True,
             goal="独立审查调研结论的依据是否充分。"),
        dict(name="merge", kind="final", goal="平台核验证据后关闭任务。"),
    ],
}

# 安全审查阶段模板:由验证准则(如认证逻辑变更)动态插入到 review 之前
SECURITY_REVIEW_STAGE = dict(
    name="security_review", kind="review", produces=["security_review_report"],
    independent=True, required_capabilities=["review", "security"],
    goal="以安全视角独立审查变更:注入、越权、凭据泄露、危险默认值等,"
         "产出安全审查报告(evidence 类型 security_review_report)。",
)


def build_plan(task_type: str, labels: list[str], risk: str, project: Project) -> list[TaskStage]:
    """基础工作流 + 项目验证准则 => 任务的具体阶段计划。

    准则效果的落点:
    - require_capabilities -> 所有 verify 阶段(如 UI 变更要求 multimodal);
    - require_evidence     -> 追加到最后一个 verify 阶段的 produces(让执行者知道要做),
                              同时追加到 review/final 的 requires_evidence(让门禁强制检查);
    - require_gates        -> security_review 插入独立安全审查阶段;
                              human_approval 让 final 阶段要求人工审批。
    """
    templates = BASE_WORKFLOWS.get(task_type) or BASE_WORKFLOWS["feature"]
    stages = [TaskStage(**dict(t)) for t in templates]

    # review 阶段默认要求 review 能力
    for s in stages:
        if s.kind == "review" and not s.required_capabilities:
            s.required_capabilities = ["review"]

    matched = [r for r in project.rules if r.matches(task_type, labels, risk)]
    # 高风险任务默认需要人工审批,与准则叠加
    need_human = risk == "high"
    extra_evidence: list[str] = []
    extra_caps: list[str] = []
    need_security = False

    for r in matched:
        extra_evidence += r.require_evidence
        extra_caps += r.require_capabilities
        if "security_review" in r.require_gates:
            need_security = True
        if "human_approval" in r.require_gates:
            need_human = True

    verify_stages = [s for s in stages if s.kind == "verify"]
    for s in verify_stages:
        s.required_capabilities = sorted(set(s.required_capabilities) | set(extra_caps))
    if verify_stages and extra_evidence:
        # 其他阶段已承诺产出的证据不重复要求,避免验证阶段重做前序工作
        covered = {t for s in stages for t in s.produces}
        last = verify_stages[-1]
        last.produces = list(dict.fromkeys(
            last.produces + [t for t in extra_evidence if t not in covered]))

    if need_security:
        idx = next((i for i, s in enumerate(stages) if s.name == "review"), len(stages) - 1)
        stages.insert(idx, TaskStage(**dict(SECURITY_REVIEW_STAGE)))

    # 门禁强制:任务级证据要求在收尾前必须全部存在
    final = stages[-1]
    all_required = list(dict.fromkeys(extra_evidence))
    final.requires_evidence = list(dict.fromkeys(final.requires_evidence + all_required))
    if need_human:
        final.human_gate = True

    return stages


def gate_check(stage: TaskStage, present_types: set[str]) -> list[str]:
    """检查阶段门禁,返回缺失项列表(空 = 通过证据检查)。"""
    missing = [t for t in stage.produces if t not in present_types]
    missing += [t for t in stage.requires_evidence if t not in present_types and t not in missing]
    return missing
