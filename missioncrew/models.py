"""领域模型。

关键设计:Backend 只描述"能力、成本、安全等级"(选谁干活);
Project 承载"准则、上下文、Skill"(领域怎么理解);
两者在任务的每个阶段由路由器 + 装配器组合成一次性的执行实例。
"""
from __future__ import annotations

import secrets as _secrets
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

# 成本档位从低到高,路由优先低档,失败后逐级升级
TIER_ORDER = ["economy", "standard", "expert"]

# 能力约定(自由字符串,以下为内置约定):
#   coding / reasoning / review / multimodal / web_search / sub_agents / security
CAP_REVIEW = "review"
CAP_MULTIMODAL = "multimodal"


def new_id(prefix: str) -> str:
    return f"{prefix}_{_secrets.token_hex(3)}"


def now() -> float:
    return time.time()


@dataclass
class Backend:
    """一个可调度的执行后端 = 本机的 Agent CLI 工具(一个工具一条记录)。

    模型阶梯挂在工具下(models 列表,自动检测时填充,可编辑):
    路由时按 工具×模型 展开成执行单元;models 为空则工具本身就是
    单一执行单元(用 tier/cost_per_run 的默认值)。
    """

    id: str
    name: str
    adapter: str                      # mock | claude_code | codex ...
    model: str = ""                   # 执行单元的模型(路由展开时填入;""=CLI 默认)
    tier: str = "standard"            # 默认档位(models 为空时生效)
    capabilities: list[str] = field(default_factory=lambda: ["coding"])
    security_level: int = 0           # 后端的安全许可:>= 任务密级才可承接
    cost_per_run: float = 1.0         # 默认单次成本(models 为空时生效)
    quota: Optional[float] = None     # 剩余配额(工具级),None 表示不限
    environments: list[str] = field(default_factory=list)
    command: Optional[list[str]] = None  # 覆盖适配器默认命令模板,支持 {prompt}/{model}
    models: list[dict] = field(default_factory=list)  # [{name, tier, cost}],name=""=CLI 默认
    binary_path: str = ""             # 检测到的可执行文件路径
    version: str = ""                 # 检测到的 CLI 版本
    enabled: bool = True

    def units(self) -> list["Backend"]:
        """展开为可路由的执行单元(工具×模型);模型属性覆盖默认档位与成本。"""
        if not self.models:
            return [self]
        from dataclasses import replace
        return [replace(self, model=m.get("name", ""), tier=m.get("tier", self.tier),
                        cost_per_run=float(m.get("cost", self.cost_per_run)))
                for m in self.models]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Backend":
        return cls(**d)


@dataclass
class Rule:
    """项目验证准则:匹配任务特征 -> 追加证据要求 / 门禁 / 能力要求。"""

    match: dict = field(default_factory=dict)   # {task_type, labels(任一命中), risk}
    require_evidence: list[str] = field(default_factory=list)
    require_gates: list[str] = field(default_factory=list)       # security_review | human_approval
    require_capabilities: list[str] = field(default_factory=list)  # 作用于验证类阶段
    note: str = ""

    def matches(self, task_type: str, labels: list[str], risk: str) -> bool:
        m = self.match
        if "task_type" in m and m["task_type"] != task_type:
            return False
        if "labels" in m and not set(m["labels"]) & set(labels):
            return False
        if "risk" in m:
            wanted = m["risk"] if isinstance(m["risk"], list) else [m["risk"]]
            if risk not in wanted:
                return False
        return True


@dataclass
class Project:
    """项目中心条目:领域知识的主要载体,不为项目永久绑定 Agent。"""

    id: str
    name: str
    description: str = ""
    repos: list[str] = field(default_factory=list)
    charter: str = ""            # 项目准则:目标、范围、业务边界
    dev_guidelines: str = ""     # 开发准则:架构原则、代码要求、变更约束
    skills: list[str] = field(default_factory=list)   # 装配进上下文的 skill 名称/路径
    resources: list[str] = field(default_factory=list)  # 可申请的受控资源 id
    required_env: Optional[str] = None                  # 执行环境要求,如 linux/gpu
    rules: list[Rule] = field(default_factory=list)     # 验证准则

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        d = dict(d)
        d["rules"] = [Rule(**r) for r in d.get("rules", [])]
        return cls(**d)


@dataclass
class Resource:
    """受控资源:Agent 拿到的是任务级、限时、可审计的能力,不是裸凭据。"""

    id: str
    kind: str                    # db | api | deploy | logs ...
    description: str = ""
    env: str = ""                # 注入到执行环境的变量名
    secret_ref: str = ""         # 在 secrets.yaml 中的键,值不落任何 Prompt
    security_level: int = 0
    stages: list[str] = field(default_factory=list)  # 允许使用的阶段,空=全部
    ttl_seconds: int = 3600

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Resource":
        return cls(**d)


@dataclass
class TaskStage:
    """任务计划中的一个阶段:静态要求 + 运行状态。

    kind: work(实现类) | verify(验证类) | review(独立审查) | final(平台收尾,无 Agent)
    """

    name: str
    kind: str = "work"
    goal: str = ""
    produces: list[str] = field(default_factory=list)          # 本阶段必须产出的证据类型
    requires_evidence: list[str] = field(default_factory=list)  # 通过前必须已存在的证据类型
    required_capabilities: list[str] = field(default_factory=list)
    independent: bool = False     # 必须由未参与 work 阶段的后端执行(独立审查)
    human_gate: bool = False      # 通过前需要人工审批
    status: str = "pending"       # pending | awaiting_approval | passed | failed
    attempts: int = 0             # 失败次数,同时是升级档位的下限索引
    backend_id: Optional[str] = None


@dataclass
class Task:
    id: str
    project_id: str
    title: str
    description: str = ""
    task_type: str = "feature"    # feature | bug | chore | research
    labels: list[str] = field(default_factory=list)
    risk: str = "normal"          # low | normal | high
    security_level: int = 0       # 任务密级,决定可承接的后端范围
    max_tier: Optional[str] = None  # 成本上限档位(预算控制)
    status: str = "open"          # open | awaiting_approval | blocked | failed | done
    stage_index: int = 0
    stages: list[TaskStage] = field(default_factory=list)
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)

    @property
    def current_stage(self) -> Optional[TaskStage]:
        if 0 <= self.stage_index < len(self.stages):
            return self.stages[self.stage_index]
        return None

    def work_backends(self) -> set[str]:
        """已参与非审查阶段的后端,用于独立审查的排除项。"""
        return {
            s.backend_id
            for s in self.stages
            if s.backend_id and s.kind in ("work", "verify")
        }

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        d = dict(d)
        d["stages"] = [TaskStage(**s) for s in d.get("stages", [])]
        return cls(**d)


# 角色偏好标签:只影响路由(档位窗口/能力要求)与名册展示,
# 不改写角色的人格提示词(description 始终是自由文本)
TRAITS: dict[str, dict] = {
    "fast":       {"label": "快速",     "max_tier": "standard"},
    "low-cost":   {"label": "低成本",   "max_tier": "standard"},
    "quality":    {"label": "高质量",   "min_tier": "standard"},
    "deep":       {"label": "深度攻坚", "min_tier": "expert"},
    "multimodal": {"label": "多模态",   "require": ["multimodal"]},
    "web":        {"label": "联网检索", "require": ["web_search"]},
    "review":     {"label": "代码评审", "require": ["review"]},
    "security":   {"label": "安全审查", "require": ["security", "review"]},
    "testing":    {"label": "适合测试"},
    "docs":       {"label": "适合文档"},
}


@dataclass
class Role:
    """聊天中可 @ 的角色 = 人格 + 领域上下文 + 结构化配置。

    人格(description)是自由文本,平台原样装配进 Prompt,不拼接任何约束;
    结构化配置分两种执行方式:
    - 自动路由:required_capabilities / traits / min_tier / max_tier 约束路由;
    - 固定组合:pinned_backend(+ pinned_model)直接指定 Agent 与模型。
    """

    id: str                       # @ 提及名,如 dev、reviewer(项目内唯一)
    project_id: str = ""          # 所属项目:角色按项目隔离,不跨项目共享
    name: str = ""                # 显示名
    description: str = ""         # 人格与领域上下文(自由文本,不锁定)
    required_capabilities: list[str] = field(default_factory=list)
    traits: list[str] = field(default_factory=list)   # 偏好标签,见 TRAITS
    pinned_backend: Optional[str] = None   # 固定后端(跳过路由)
    pinned_model: Optional[str] = None     # 固定模型(配合 pinned_backend)
    min_tier: Optional[str] = None         # 最低档位(如攻坚角色直接用 expert)
    max_tier: Optional[str] = None         # 最高档位(成本上限)
    color: str = ""                        # 看板/聊天中的标识色

    def effective_constraints(self) -> tuple[list[str], Optional[str], Optional[str]]:
        """显式约束 + 偏好标签推导 => (能力要求, 最低档位, 最高档位)。"""
        caps = set(self.required_capabilities)
        mins = [self.min_tier] if self.min_tier in TIER_ORDER else []
        maxs = [self.max_tier] if self.max_tier in TIER_ORDER else []
        for t in self.traits:
            spec = TRAITS.get(t, {})
            caps |= set(spec.get("require", []))
            if spec.get("min_tier"):
                mins.append(spec["min_tier"])
            if spec.get("max_tier"):
                maxs.append(spec["max_tier"])
        min_tier = max(mins, key=TIER_ORDER.index) if mins else None
        max_tier = min(maxs, key=TIER_ORDER.index) if maxs else None
        return sorted(caps), min_tier, max_tier

    def trait_labels(self) -> list[str]:
        return [TRAITS[t]["label"] for t in self.traits if t in TRAITS]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Role":
        return cls(**d)


@dataclass
class Channel:
    """聊天频道:一次协作的场所,归属于某个项目(项目是第一层级)。"""

    id: str                            # 全局唯一,约定为 "<project>:<name>"
    name: str = ""
    project_id: Optional[str] = None   # 所属项目,装配其准则、使用其角色
    workdir: Optional[str] = None      # 执行工作目录,默认 MC_HOME/channels/<id>
    created_at: float = field(default_factory=now)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Channel":
        return cls(**d)


@dataclass
class RunResult:
    """一次执行实例的结果,由适配器返回;证据统一从工作区 manifest 读取。"""

    success: bool
    summary: str = ""
    output: str = ""


@dataclass
class ExecutionConfig:
    """装配后的执行配置:平台决定'在什么约束下执行'的最终产物。"""

    task_id: str
    stage_name: str
    backend: Backend
    prompt: str
    workdir: str
    env: dict = field(default_factory=dict)
    timeout: int = 3600
    routing_trace: list[str] = field(default_factory=list)
