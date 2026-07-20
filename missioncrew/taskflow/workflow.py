"""工作流引擎的静态部分:按任务类型生成固定阶段计划。

工作流只控制阶段和门禁,不干涉 Agent 内部如何搜索代码、拆分任务、写代码。
"""
from __future__ import annotations

from ..core.models import TaskStage

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

def build_plan(task_type: str, risk: str = "normal") -> list[TaskStage]:
    """由任务类型生成阶段计划；项目级指导统一由准则上下文提供。"""
    templates = BASE_WORKFLOWS.get(task_type) or BASE_WORKFLOWS["feature"]
    stages = [TaskStage(**dict(t)) for t in templates]

    # review 阶段默认要求 review 能力
    for s in stages:
        if s.kind == "review" and not s.required_capabilities:
            s.required_capabilities = ["review"]

    # 高风险是任务自身的固定安全属性，不依赖项目配置。
    if risk == "high":
        stages[-1].human_gate = True

    return stages


def gate_check(stage: TaskStage, present_types: set[str]) -> list[str]:
    """检查阶段门禁,返回缺失项列表(空 = 通过证据检查)。"""
    missing = [t for t in stage.produces if t not in present_types]
    missing += [t for t in stage.requires_evidence if t not in present_types and t not in missing]
    return missing
