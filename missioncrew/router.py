"""智能路由器:为任务的某个阶段挑选执行后端。

严格按 design.md 的顺序做决策,并输出可审计的决策轨迹:
    安全限制 -> 必需能力 -> 项目和环境适配 -> 成功概率 -> 成本与剩余配额

成本策略:候选按 (档位, 单次成本) 升序排列,选择第一个历史成功率不低于阈值的;
全部低于阈值时选成功率最高的(宁可贵也不反复失败)。
升级策略:阶段每失败一次,允许的最低档位上调一档(economy -> standard -> expert)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import TIER_ORDER, Backend, Project, Task, TaskStage
from .store import Store

# 低于该成功率阈值时不再为省钱冒险,改选成功率更高的候选
PROB_THRESHOLD = 0.4


@dataclass
class RoutingDecision:
    backend: Optional[Backend]
    trace: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return " | ".join(self.trace)


def route(store: Store, task: Task, stage: TaskStage, project: Project) -> RoutingDecision:
    trace: list[str] = []
    candidates = [b for b in store.list_backends() if b.enabled]
    trace.append(f"候选后端 {len(candidates)} 个")

    # 1. 安全限制:后端安全许可必须覆盖任务密级
    candidates = [b for b in candidates if b.security_level >= task.security_level]
    trace.append(f"安全过滤(密级>={task.security_level}): 剩 {len(candidates)}")

    # 2. 必需能力:阶段声明的能力必须全部具备
    need = set(stage.required_capabilities)
    if need:
        candidates = [b for b in candidates if need <= set(b.capabilities)]
    trace.append(f"能力过滤({','.join(sorted(need)) or '无'}): 剩 {len(candidates)}")

    # 独立审查:排除参与过 work/verify 阶段的后端
    if stage.independent:
        used = task.work_backends()
        candidates = [b for b in candidates if b.id not in used]
        trace.append(f"独立性过滤(排除 {','.join(sorted(used)) or '无'}): 剩 {len(candidates)}")

    # 3. 项目与环境适配
    if project.required_env:
        candidates = [b for b in candidates
                      if not b.environments or project.required_env in b.environments]
        trace.append(f"环境过滤({project.required_env}): 剩 {len(candidates)}")

    # 展开为执行单元:工具×模型(工具级过滤已完成,档位/成本按模型判断)
    units = [u for b in candidates for u in b.units()]
    trace.append(f"展开执行单元(工具×模型): {len(units)} 个")

    # 升级下限与预算上限共同约束档位窗口
    floor = min(stage.attempts, len(TIER_ORDER) - 1)
    ceil = TIER_ORDER.index(task.max_tier) if task.max_tier in TIER_ORDER else len(TIER_ORDER) - 1
    if floor > ceil:
        trace.append(f"升级下限 {TIER_ORDER[floor]} 超出预算上限 {TIER_ORDER[ceil]},无可用档位")
        return RoutingDecision(None, trace)
    units = [u for u in units if floor <= TIER_ORDER.index(u.tier) <= ceil]
    trace.append(f"档位窗口 [{TIER_ORDER[floor]}..{TIER_ORDER[ceil]}]: 剩 {len(units)}")

    # 5(前置). 配额:工具级余额不足的直接排除
    units = [u for u in units if u.quota is None or u.quota >= u.cost_per_run]
    trace.append(f"配额过滤: 剩 {len(units)}")

    if not units:
        trace.append("无可用后端")
        return RoutingDecision(None, trace)

    # 4+5. 成功概率与成本:按成本升序,取第一个成功率达标的
    def _label(u: Backend) -> str:
        return u.id + (f"/{u.model}" if u.model else "")
    scored = sorted(
        ((u, store.stats_prob(u.id, project.id, task.task_type)) for u in units),
        key=lambda up: (TIER_ORDER.index(up[0].tier), up[0].cost_per_run, -up[1]),
    )
    for u, p in scored:
        if p >= PROB_THRESHOLD:
            trace.append(f"选中 {_label(u)}(tier={u.tier}, 成功率={p:.2f}, 成本={u.cost_per_run})")
            return RoutingDecision(u, trace)

    best = max(scored, key=lambda up: up[1])
    trace.append(f"全部低于阈值 {PROB_THRESHOLD},选成功率最高的 {_label(best[0])}({best[1]:.2f})")
    return RoutingDecision(best[0], trace)
