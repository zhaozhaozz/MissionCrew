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

# 自定义面板组件保持通用数据模型，前端可以按 type 选择不同呈现方式；
# 未知类型仍可按 JSON/Markdown 展示，避免面板能力被固定模板限制。
BOARD_WIDGET_TYPES = {
    "markdown", "requirements", "test_records", "log_analysis",
    "task_query", "metrics", "table",
}


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
class GuidelineDocument:
    """一篇项目准则文档，可引用项目文档库中的补充文件。"""

    id: str
    title: str = ""
    content: str = ""
    file_refs: list[str] = field(default_factory=list)
    enabled: bool = True

    @classmethod
    def from_dict(cls, value: dict | str) -> "GuidelineDocument":
        if isinstance(value, str):
            return cls(id=value, title=value)
        return cls(**value)


@dataclass
class ProjectSkill:
    """项目 Skill：完整说明、文件引用和 Runtime 适用范围。"""

    id: str
    name: str = ""
    description: str = ""
    instructions: str = ""
    file_refs: list[str] = field(default_factory=list)
    # 均为空表示注入所有 Runtime；否则 backend id 或 adapter 任一命中才注入。
    runtime_ids: list[str] = field(default_factory=list)
    adapters: list[str] = field(default_factory=list)
    runtime_instructions: dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    def applies_to(self, backend: "Backend") -> bool:
        return self.enabled and (
            (not self.runtime_ids and not self.adapters)
            or backend.id in self.runtime_ids
            or backend.adapter in self.adapters
        )

    def instructions_for(self, backend: "Backend") -> str:
        override = (self.runtime_instructions.get(backend.id)
                    or self.runtime_instructions.get(backend.adapter)
                    or self.runtime_instructions.get("default"))
        return "\n\n".join(x for x in (self.instructions, override) if x)

    @classmethod
    def from_dict(cls, value: dict | str) -> "ProjectSkill":
        if isinstance(value, str):
            return cls(id=value, name=value)
        return cls(**value)


@dataclass
class Project:
    """项目中心条目:领域知识的主要载体，并显式指定唯一主控角色。"""

    id: str
    name: str
    description: str = ""
    repos: list[str] = field(default_factory=list)
    charter: str = ""            # 项目准则:目标、范围、业务边界
    dev_guidelines: str = ""     # 开发准则:架构原则、代码要求、变更约束
    orchestrator_role_id: str = "lead"  # 负责整个项目和其他角色调度的唯一角色
    guidelines: list[GuidelineDocument] = field(default_factory=list)
    skills: list[ProjectSkill] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)  # 可申请的受控资源 id
    required_env: Optional[str] = None                  # 执行环境要求,如 linux/gpu
    rules: list[Rule] = field(default_factory=list)     # 验证准则

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        d = dict(d)
        d["rules"] = [Rule(**r) for r in d.get("rules", [])]
        d["guidelines"] = [GuidelineDocument.from_dict(v)
                           for v in d.get("guidelines", [])]
        d["skills"] = [ProjectSkill.from_dict(v) for v in d.get("skills", [])]
        d.setdefault("orchestrator_role_id", "lead")
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


# 角色偏好标签:描述角色的工作风格并展示在名册中;
# runtime/model 已固定,偏好不再参与执行时路由。
TRAITS: dict[str, dict] = {
    "fast":       {"label": "快速"},
    "low-cost":   {"label": "低成本"},
    "quality":    {"label": "高质量"},
    "deep":       {"label": "深度攻坚"},
    "multimodal": {"label": "多模态"},
    "web":        {"label": "联网检索"},
    "review":     {"label": "代码评审"},
    "security":   {"label": "安全审查"},
    "testing":    {"label": "适合测试"},
    "docs":       {"label": "适合文档"},
}

# 旧版中这些偏好会隐式追加路由能力。仅在读取旧角色 JSON 时
# 还原为显式能力,避免升级后名册丢失原有的专长信息。
_LEGACY_TRAIT_CAPABILITIES = {
    "multimodal": ["multimodal"],
    "web": ["web_search"],
    "review": ["review"],
    "security": ["security", "review"],
}


@dataclass
class Role:
    """聊天中可 @ 的角色 = 固定执行组合 + 定位 + 能力 + 偏好。

    runtime_id/model 始终指向固定执行组合;description/capabilities/traits
    用于协作方理解和选择角色,不参与执行时路由。
    """

    id: str                       # @ 提及名,如 dev、reviewer(项目内唯一)
    project_id: str = ""          # 所属项目:角色按项目隔离,不跨项目共享
    name: str = ""                # 显示名
    description: str = ""         # 人格与领域上下文(自由文本,不锁定)
    runtime_id: str = ""             # 固定 runtime(后端注册表 id)
    model: str = ""                  # 固定模型;"" 表示显式使用 CLI 默认模型
    capabilities: list[str] = field(default_factory=list)  # 角色能力标签
    traits: list[str] = field(default_factory=list)        # 工作偏好,见 TRAITS
    color: str = ""                        # 看板/聊天中的标识色

    def trait_labels(self) -> list[str]:
        return [TRAITS[t]["label"] for t in self.traits if t in TRAITS]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Role":
        d = dict(d)
        # v0.4 兼容迁移:旧角色的可选 pinned_* 与路由约束转成固定组合和描述能力。
        legacy = any(k in d for k in (
            "pinned_backend", "pinned_model", "required_capabilities", "min_tier", "max_tier"
        ))
        d.setdefault("runtime_id", d.pop("pinned_backend", None) or "")
        d.setdefault("model", d.pop("pinned_model", None) or "")
        legacy_caps = set(d.pop("required_capabilities", []))
        if legacy and "capabilities" not in d:
            for trait in d.get("traits", []):
                legacy_caps.update(_LEGACY_TRAIT_CAPABILITIES.get(trait, []))
            d["capabilities"] = sorted(legacy_caps)
        d.pop("min_tier", None)
        d.pop("max_tier", None)
        return cls(**d)


@dataclass
class Channel:
    """聊天频道:一次协作的场所,归属于某个项目(项目是第一层级)。"""

    id: str                            # 全局唯一,约定为 "<project>:<name>"
    name: str = ""
    project_id: Optional[str] = None   # 所属项目,装配其准则、使用其角色
    workdir: Optional[str] = None      # 执行工作目录,默认 MC_HOME/channels/<id>
    purpose: str = ""                 # 本频道负责的任务/讨论边界
    created_by_role_id: str = ""      # 为空表示人类/平台创建
    created_at: float = field(default_factory=now)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Channel":
        return cls(**d)


@dataclass
class BoardWidget:
    """自定义面板中的一个可自由布置组件。"""

    id: str
    type: str = "markdown"
    title: str = ""
    x: int = 0
    y: int = 0
    width: int = 6
    height: int = 4
    content: dict = field(default_factory=dict)


@dataclass
class Board:
    """项目自定义面板；layout 同时保存位置、尺寸和组件内容。"""

    id: str
    project_id: str
    name: str = ""
    description: str = ""
    layout: list[BoardWidget] = field(default_factory=list)
    created_by_role_id: str = ""
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Board":
        d = dict(d)
        d["layout"] = [BoardWidget(**item) for item in d.get("layout", [])]
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
