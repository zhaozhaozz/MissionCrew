"""领域模型。

关键设计:Backend 只描述"能力、成本、安全等级"(选谁干活);
Project 承载"准则、上下文、Skill"(领域怎么理解);
两者在任务的每个阶段由路由器 + 装配器组合成一次性的执行实例。
"""
from __future__ import annotations

import re as _re
import secrets as _secrets
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

# 成本档位从低到高,路由优先低档,失败后逐级升级
TIER_ORDER = ["economy", "standard", "expert"]
DEFAULT_MAX_CHAIN_RUNS = 20

# 能力约定(自由字符串,以下为内置约定):
#   coding / reasoning / review / multimodal / web_search / sub_agents / security
CAP_REVIEW = "review"
CAP_MULTIMODAL = "multimodal"

# 自定义面板组件保持通用数据模型，前端可以按 type 选择不同呈现方式；
# 未知类型仍可按 JSON/Markdown 展示，避免面板能力被固定模板限制。
# 面板卡片是一组通用展示原语(参考 AgentDesk 内置组件的思路):
# 领域含义来自数据与组合,而非类型本身——"需求面板"只是绑定了需求数据的 table。
# 卡片可通过 content.source 绑定平台数据源(tasks/audit/document/messages),
# 无 source 时渲染 content 里的静态数据。
BOARD_WIDGET_TYPES = {
    "markdown",   # 富文本:content.markdown
    "table",      # 表格:content.columns(:[{key,label}]或[str]) + rows([obj]或[数组])
    "card",       # 数值卡:content.metrics [{label,value,unit?,tone?}]
    "chart",      # 图表:content.kind(bar|line|pie) + data + x_key/y_key
    "list",       # 条目列表:content.items [str 或 {text,tone?}]
    "log",        # 日志尾部:content.lines [str] 或 content.text
    "code",       # 代码块:content.code + language?
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
class ProjectResource:
    """项目资源:本地路径或 git 仓。

    本地路径若是 git 仓,添加时自动读取其远程地址绑定为 git 资源;
    纯远程地址(http/git@)则是没有本地路径的 git 资源。
    """

    id: str
    kind: str = "path"     # path(普通本地路径) | git(git 仓,含 remote)
    path: str = ""         # 本地路径;纯远程 git 资源可为空
    remote: str = ""       # git 远程地址
    name: str = ""

    @classmethod
    def from_dict(cls, value: "dict | str") -> "ProjectResource":
        if isinstance(value, str):   # 旧版 repos 是路径字符串列表
            base = value.rstrip("/").rsplit("/", 1)[-1] or "repo"
            return cls(id=base, kind="path", path=value, name=base)
        return cls(**value)


@dataclass
class Project:
    """项目中心条目:领域知识的主要载体，并显式指定唯一主控角色。"""

    id: str
    name: str
    description: str = ""
    repos: list[ProjectResource] = field(default_factory=list)
    charter: str = ""            # 项目准则:目标、范围、业务边界
    dev_guidelines: str = ""     # 开发准则:架构原则、代码要求、变更约束
    orchestrator_role_id: str = "lead"  # 负责整个项目和其他角色调度的唯一角色
    max_chain_runs: int = DEFAULT_MAX_CHAIN_RUNS  # 单条人类消息最多触发的 Agent 执行数
    guidelines: list[GuidelineDocument] = field(default_factory=list)
    skills: list[ProjectSkill] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)  # 可申请的受控资源 id
    required_env: Optional[str] = None                  # 执行环境要求,如 linux/gpu
    rules: list[Rule] = field(default_factory=list)     # 验证准则

    def __post_init__(self):
        # 资源条目归一化:旧版字符串路径与 dict 均转成 ProjectResource
        self.repos = [r if isinstance(r, ProjectResource) else ProjectResource.from_dict(r)
                      for r in self.repos]
        if (isinstance(self.max_chain_runs, bool)
                or not isinstance(self.max_chain_runs, int)
                or self.max_chain_runs < 1):
            raise ValueError("max_chain_runs 必须是正整数")

    def repo_paths(self) -> list[str]:
        """有本地路径的资源(供频道工作目录校验等使用)。"""
        return [r.path for r in self.repos if r.path]

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
        d.setdefault("max_chain_runs", DEFAULT_MAX_CHAIN_RUNS)
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


# 角色能力是固定选项,只收录"硬性模态/工具事实"(能不能做到,而非负责什么):
# 在名册中展示,供人类或主控按能力挑选合适的角色;执行角色看不到名册;
# 不参与执行时路由——角色的 runtime/model 在定义时已固定。
# 职责类描述(评审、安全审查等)写进定位/偏好自由文本;runtime 特性
# (如多 Agent 编排)由绑定的 runtime 决定,不在角色上声明。
# 注意与 Backend.capabilities 区分:后端能力位(含 review/security)
# 仍是结构化任务路由的过滤条件,词表互相独立。
ROLE_ABILITIES: dict[str, str] = {
    "coding":      "代码执行",
    "reasoning":   "深度推理",
    "multimodal":  "图像/视觉输入",
    "audio":       "语音输入",
    "image_gen":   "图像生成",
    "web_search":  "联网检索",
}

# v0.5 起从角色能力词表退役的选项(职责/由 runtime 决定的特性):
# 旧角色带这些标签时,读取即迁移——从 capabilities 移除,含义并入偏好文本。
_RETIRED_ABILITY_TEXT = {
    "review": "代码评审", "security": "安全审查", "sub_agents": "多 Agent 编排",
}


def preference_segments(preference: str) -> set[str]:
    """偏好文本按顿号/逗号/分号切成片段集合。

    迁移去重与种子绑定都用"片段精确匹配"而非子串:避免"不做代码评审"
    这类否定表述被误认为已含"代码评审",导致含义被静默丢弃或反转。
    """
    return {s.strip() for s in _re.split(r"[、,,;;]", preference or "") if s.strip()}

# 旧偏好标签 -> 偏好文本 的迁移映射(偏好已改为自由文本)
_LEGACY_TRAIT_TEXT = {
    "fast": "快速", "low-cost": "低成本", "quality": "高质量", "deep": "深度攻坚",
    "multimodal": "多模态", "web": "联网检索", "review": "代码评审",
    "security": "安全审查", "testing": "适合测试", "docs": "适合文档",
}

# 旧版中这些偏好会隐式追加路由能力。仅在读取旧角色 JSON 时
# 还原为显式能力,避免升级后名册丢失原有的专长信息。
# (review/security 已随能力词表退役,其含义由偏好文本承载,不再还原)
_LEGACY_TRAIT_CAPABILITIES = {
    "multimodal": ["multimodal"],
    "web": ["web_search"],
}


@dataclass
class Role:
    """聊天中可 @ 的角色 = 固定执行组合 + 定位 + 能力 + 偏好。

    runtime_id/model 是定义角色时固定的执行组合;description/capabilities/preference
    用于人类和项目主控理解、选择角色,不参与执行时路由。
    """

    id: str                       # @ 提及名,如 dev、reviewer(项目内唯一)
    project_id: str = ""          # 所属项目:角色按项目隔离,不跨项目共享
    name: str = ""                # 显示名
    description: str = ""         # 人格与领域上下文(自由文本,不锁定)
    runtime_id: str = ""             # 固定 runtime(后端注册表 id)
    model: str = ""                  # 固定模型;"" 表示显式使用 CLI 默认模型
    effort: str = ""                 # 推理力度;"" = CLI 默认。仅支持 effort 的
                                     # runtime 可设(adapters.EFFORT_SUPPORT)
    capabilities: list[str] = field(default_factory=list)  # 固定能力选项,见 ROLE_ABILITIES
    preference: str = ""                   # 工作偏好:自由文本(如"前端"/"后端,偏好 React")
    color: str = ""                        # 看板/聊天中的标识色
    sort_order: int = 0                    # 项目内显示顺序(设置页/侧栏/名册),小的在前

    def ability_labels(self) -> list[str]:
        return [ROLE_ABILITIES.get(c, c) for c in self.capabilities]

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
        # 偏好从固定标签迁移为自由文本
        traits = d.pop("traits", [])
        if traits and not d.get("preference"):
            d["preference"] = "、".join(_LEGACY_TRAIT_TEXT.get(t, t) for t in traits)
        # v0.5:退役的能力标签迁移进偏好文本(读取即迁移,下次保存落库);
        # 去重按片段精确匹配,子串命中(如"不做代码评审")仍会追加,宁重勿丢
        caps = d.get("capabilities") or []
        retired = [c for c in caps if c in _RETIRED_ABILITY_TEXT]
        if retired:
            d["capabilities"] = [c for c in caps if c not in _RETIRED_ABILITY_TEXT]
            pref = d.get("preference") or ""
            segs = preference_segments(pref)
            extra = "、".join(_RETIRED_ABILITY_TEXT[c] for c in retired
                             if _RETIRED_ABILITY_TEXT[c] not in segs)
            if extra:
                d["preference"] = "、".join(x for x in (pref, extra) if x)
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
    # 项目显式登记、允许 Runtime 读写的本地目录。workdir 是主工作根，
    # allowed_dirs 是额外的多仓/文档库边界，由各适配器翻译成原生参数。
    allowed_dirs: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    timeout: int = 3600
    effort: str = ""      # 推理力度(聊天执行由角色填入;任务阶段暂不使用)
    routing_trace: list[str] = field(default_factory=list)
    # 聊天 Runtime 会话按 channel×role 复用。common_prompt 每轮重注入，确保
    # Runtime 压缩历史时仍拿到最新 MissionCrew 公共输入；recovery_prompt 只在
    # 新建/无法恢复原生会话时使用，包含最近消息用于恢复上下文。
    session_key: str = ""
    session_id: str = ""
    common_prompt: str = ""
    turn_prompt: str = ""
    recovery_prompt: str = ""
    context_version: str = ""
    context_changed: bool = False
    # 执行可能在线程池中排队；真正拿到会话锁后重读一次，避免两个连续触发都
    # 使用装配时看到的空 id 而各自新建会话。
    load_session: Optional[Callable[[], tuple[str, str]]] = None
    save_session: Optional[Callable[[str, str], None]] = None
    # 运行过程回调 (kind, text):适配器在执行期间实时上报思考/工具/输出等
    # 事件,None 表示调用方不关心过程(如结构化任务阶段)
    emit: Optional[Callable[[str, str], None]] = None
