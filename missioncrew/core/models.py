"""领域模型。

Backend 描述可用 Runtime，Project 承载项目上下文与协作角色；Task 是可由
人类和 Agent 共同维护、通过 Channel 交给项目主控处理的 Issue。
"""
from __future__ import annotations

import json as _json
import re as _re
import secrets as _secrets
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

import yaml as _yaml

# 成本档位从低到高,路由优先低档,失败后逐级升级
TIER_ORDER = ["economy", "standard", "expert"]
DEFAULT_MAX_CHAIN_RUNS = 100
TASK_STATUSES = ("open", "in_progress", "blocked", "done")

# 聊天公共上下文的注入模式:完整模式重发全部 common_prompt 并清零增量回合
# 计数;lean(增量回合)只发版本引用头;raw 是非聊天路径的原始 prompt。
INJECTION_FULL_MODES = frozenset({"first", "recovery", "update", "reinject"})

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

    模型清单挂在工具下(models 列表,检测时按 KNOWN_MODELS 填充,不可编辑):
    只记模型名,档位与成本一律取工具级的 tier/cost_per_run——平台不跟踪
    单个模型的档位与成本。models 为空则工具本身就是单一执行单元。
    """

    id: str
    name: str
    adapter: str                      # mock | claude_code | codex ...
    model: str = ""                   # 执行单元的模型(路由展开时填入;""=CLI 默认)
    tier: str = "standard"            # 工具级档位(展开的执行单元一律继承)
    capabilities: list[str] = field(default_factory=lambda: ["coding"])
    security_level: int = 0           # 后端的安全许可:>= 任务密级才可承接
    cost_per_run: float = 1.0         # 工具级单次成本(配额按它扣减)
    quota: Optional[float] = None     # 剩余配额(工具级),None 表示不限
    environments: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)   # 可选模型名,""=CLI 默认
    binary_path: str = ""             # 检测到的可执行文件路径
    version: str = ""                 # 检测到的 CLI 版本
    enabled: bool = True

    def units(self) -> list["Backend"]:
        """展开为可路由的执行单元(工具×模型);档位与成本一律沿用工具级取值。"""
        if not self.models:
            return [self]
        from dataclasses import replace
        return [replace(self, model=name) for name in self.models]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Backend":
        value = dict(d)
        value.pop("command", None)  # 兼容升级前持久化的自定义命令字段
        # 兼容旧记录:模型清单曾是 [{name, tier, cost}] 的阶梯,现在只留模型名
        if value.get("models"):
            value["models"] = [m.get("name", "") if isinstance(m, dict) else str(m)
                               for m in value["models"]]
        return cls(**value)


@dataclass
class GuidelineDocument:
    """一篇项目准则；name/description 与 Markdown frontmatter 同源。"""

    name: str
    description: str = ""
    content: str = ""
    enabled: bool = True

    _FRONTMATTER_RE = _re.compile(
        r"\A---[ \t]*\r?\n(?P<header>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
        _re.DOTALL,
    )

    @classmethod
    def _split_markdown(cls, markdown: str, *, legacy: bool = False) -> tuple[dict, str]:
        match = cls._FRONTMATTER_RE.match(markdown)
        if not match:
            raise ValueError("准则 Markdown 必须以 YAML frontmatter 开头")
        try:
            attributes = _yaml.safe_load(match.group("header")) or {}
        except _yaml.YAMLError as exc:
            raise ValueError(f"准则 frontmatter 不是有效 YAML：{exc}") from exc
        if not isinstance(attributes, dict):
            raise ValueError("准则 frontmatter 必须是属性对象")
        allowed = {"name", "description"}
        if legacy:
            allowed |= {"id", "title", "summary"}
        unexpected = sorted(set(attributes) - allowed)
        if unexpected:
            raise ValueError("准则 frontmatter 不支持属性：" + ", ".join(unexpected))
        content = markdown[match.end():]
        # frontmatter 与正文之间的一个空行属于文件结构，不作为正文内容保存。
        if content.startswith("\r\n"):
            content = content[2:]
        elif content.startswith("\n"):
            content = content[1:]
        return attributes, content

    @classmethod
    def from_markdown(cls, markdown: str, enabled: bool = True) -> "GuidelineDocument":
        """从用户编辑的完整 Markdown 读取 name/description，不维护字段映射。"""
        if not isinstance(markdown, str):
            raise ValueError("准则 Markdown 必须是字符串")
        attributes, content = cls._split_markdown(markdown)
        name = attributes.get("name")
        description = attributes.get("description")
        if name is None:
            name = ""
        if description is None:
            description = ""
        if not isinstance(name, str) or not isinstance(description, str):
            raise ValueError("准则 frontmatter 的 name 和 description 必须是字符串")
        return cls(name=name.strip(), description=description.strip(),
                   content=content.rstrip(), enabled=enabled)

    def render_markdown(self) -> str:
        frontmatter = _yaml.safe_dump(
            {"name": self.name, "description": self.description},
            allow_unicode=True, sort_keys=False, default_flow_style=False,
        ).strip()
        body = self.content.rstrip()
        return f"---\n{frontmatter}\n---\n" + (f"\n{body}\n" if body else "")

    def to_dict(self) -> dict:
        # markdown 是持久化事实源；name/description 便于 API 消费，并在读取时由
        # markdown 重新解析，避免两套属性发生漂移。
        return {
            "name": self.name,
            "description": self.description,
            "markdown": self.render_markdown(),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, value: dict | str) -> "GuidelineDocument":
        if isinstance(value, str):
            return cls(name=value, description=value)
        data = dict(value)
        # resource_url 是 API 的只读导航字段；项目配置整对象回传时忽略。
        data.pop("resource_url", None)
        if "markdown" in data:
            return cls.from_markdown(str(data["markdown"]), bool(data.get("enabled", True)))

        refs = data.pop("file_refs", [])  # 旧引用迁移成普通 Markdown 链接
        if refs:
            content = str(data.get("content", ""))
            links = [f"- [{ref}]({ref})" for ref in refs
                     if isinstance(ref, str) and f"]({ref})" not in content]
            if links:
                data["content"] = "\n\n".join(
                    part for part in (content, "## 相关文档\n" + "\n".join(links)) if part)
        data.pop("role_ids", None)  # 短期版本曾支持角色绑定，现统一由执行者判断
        data.pop("title", None)

        # 兼容曾被保存进正文的 id/title/summary frontmatter；迁移后只输出
        # name/description，正文不会再套一层 frontmatter。
        raw_content = str(data.get("content", ""))
        if cls._FRONTMATTER_RE.match(raw_content):
            try:
                attributes, raw_content = cls._split_markdown(raw_content, legacy=True)
            except ValueError:
                attributes = {}
            else:
                data["content"] = raw_content.rstrip()
                data["name"] = attributes.get("name", attributes.get("id", ""))
                data["description"] = attributes.get(
                    "description", attributes.get("summary", ""))

        legacy_id = data.pop("id", "")
        data["name"] = str(data.get("name") or legacy_id).strip()
        if "description" not in data and "summary" in data:
            data["description"] = data["summary"]
        data.pop("summary", None)
        if "description" not in data:
            # 旧条目没有摘要；取正文首个非空行作为一次性兼容摘要，避免升级后
            # 公共上下文只剩无法判断用途的 id。之后可在 Web 中独立编辑摘要。
            first_line = next((line.strip().lstrip("# ").strip()
                               for line in str(data.get("content", "")).splitlines()
                               if line.strip()), "")
            data["description"] = first_line[:240]
        data["description"] = str(data.get("description", "")).strip()
        return cls(**data)


@dataclass
class ProjectSkill:
    """项目 Skill：完整说明；由执行者结合当前任务判断是否适用。"""

    id: str
    name: str = ""
    description: str = ""
    instructions: str = ""
    enabled: bool = True

    @classmethod
    def from_dict(cls, value: dict | str) -> "ProjectSkill":
        if isinstance(value, str):
            return cls(id=value, name=value)
        data = dict(value)
        # resource_url 是 API 的只读导航字段；项目配置整对象回传时忽略。
        data.pop("resource_url", None)
        # 旧版按文件、Runtime 或角色预装配；升级后把文件引用转成 Markdown
        # 链接、把补充说明并入正文，仅丢弃绑定条件，由执行者自行判断。
        instructions = str(data.get("instructions", ""))
        refs = data.pop("file_refs", [])
        links = [f"- [{ref}]({ref})" for ref in refs
                 if isinstance(ref, str) and f"]({ref})" not in instructions]
        if links:
            instructions = "\n\n".join(
                part for part in (instructions, "## 相关文档\n" + "\n".join(links)) if part)
        overrides = data.pop("runtime_instructions", {})
        if isinstance(overrides, dict):
            notes = [f"### {key}\n{value}" for key, value in overrides.items()
                     if isinstance(key, str) and isinstance(value, str) and value]
            if notes:
                instructions = "\n\n".join(part for part in (
                    instructions,
                    "## 迁移的补充说明（按当前任务判断是否适用）\n" + "\n\n".join(notes),
                ) if part)
        data["instructions"] = instructions
        for key in ("runtime_ids", "adapters", "role_ids"):
            data.pop(key, None)
        return cls(**data)


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
        data = asdict(self)
        data["guidelines"] = [guideline.to_dict() for guideline in self.guidelines]
        return data

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        d = dict(d)
        guidelines = [GuidelineDocument.from_dict(v) for v in d.get("guidelines", [])]
        legacy_rules = d.pop("rules", [])
        if isinstance(legacy_rules, list) and any(
                isinstance(rule, dict) for rule in legacy_rules):
            # 旧验证规则不再驱动工作流；一次性转成普通准则，保留人类意图并由
            # Agent 结合任务自行判断。项目重写后 rules 字段自然消失。
            used_names = {guideline.name for guideline in guidelines}
            base_name = "migrated-validation-rules"
            name = base_name
            suffix = 2
            while name in used_names:
                name = f"{base_name}-{suffix}"
                suffix += 1
            sections = [
                "# 从旧验证规则迁移的准则",
                "",
                "这些要求原先由平台按任务属性机械匹配；现在作为普通准则，"
                "由 Agent 根据具体任务判断是否适用。",
            ]
            for index, rule in enumerate(
                    (item for item in legacy_rules if isinstance(item, dict)), 1):
                sections.extend([
                    "", f"## 旧规则 {index}", "",
                    "- 适用条件：`" + _json.dumps(
                        rule.get("match", {}), ensure_ascii=False, sort_keys=True) + "`",
                ])
                for key, label in (
                    ("require_evidence", "原证据要求"),
                    ("require_gates", "原门禁要求"),
                    ("require_capabilities", "原能力要求"),
                ):
                    values = rule.get(key, [])
                    if isinstance(values, list) and values:
                        sections.append(f"- {label}：" + "、".join(map(str, values)))
                if rule.get("note"):
                    sections.extend(["", str(rule["note"])])
            guidelines.append(GuidelineDocument(
                name=name,
                description="从旧验证规则迁移的任务执行与验证指导。",
                content="\n".join(sections),
            ))
        d["guidelines"] = guidelines
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
class Task:
    """项目 Issue：正文可编辑，状态简报单独以追加记录保存。"""

    id: str
    project_id: str
    title: str
    summary: str = ""
    body: str = ""
    labels: list[str] = field(default_factory=list)
    channel_ids: list[str] = field(default_factory=list)
    status: str = "open"          # open | in_progress | blocked | done
    archived: bool = False
    archived_at: float = 0.0
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        """读取新版 Task，并把旧阶段式任务无损降级为 Issue 内容。"""
        raw = dict(d)
        legacy_description = raw.get("description", "")
        body = raw.get("body", legacy_description)
        summary = raw.get("summary", "")
        if not summary and isinstance(legacy_description, str):
            summary = next(
                (line.strip() for line in legacy_description.splitlines()
                 if line.strip()), "")[:240]
        legacy_status = raw.get("status", "open")
        status = {
            "awaiting_approval": "in_progress",
            "failed": "blocked",
        }.get(legacy_status, legacy_status)
        if status not in TASK_STATUSES:
            status = "open"
        labels = raw.get("labels", [])
        channel_ids = raw.get("channel_ids", [])
        return cls(
            id=str(raw["id"]),
            project_id=str(raw["project_id"]),
            title=str(raw.get("title", "")),
            summary=str(summary or ""),
            body=str(body or ""),
            labels=list(dict.fromkeys(
                str(item) for item in labels if isinstance(item, str))),
            channel_ids=list(dict.fromkeys(
                str(item) for item in channel_ids if isinstance(item, str))),
            status=status,
            archived=bool(raw.get("archived", False)),
            archived_at=float(raw.get("archived_at", 0.0)),
            created_at=float(raw.get("created_at", now())),
            updated_at=float(raw.get("updated_at", now())),
        )


# 角色能力是固定选项,只收录"硬性模态/工具事实"(能不能做到,而非负责什么):
# 在名册中展示,供人类或主控按能力挑选合适的角色;执行角色看不到名册;
# 不参与执行时路由——角色的 runtime/model 在定义时已固定。
# 职责类描述(评审、安全审查等)写进定位/偏好自由文本;runtime 特性
# (如多 Agent 编排)由绑定的 runtime 决定,不在角色上声明。
# 注意与 Backend.capabilities 区分：后端能力位是 Runtime 元数据，词表互相独立。
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
    enabled: bool = True          # 临时停用只阻止新派发,不删除配置、历史或会话
    name: str = ""                # 显示名
    description: str = ""         # 人格与领域上下文(自由文本,不锁定)
    runtime_id: str = ""             # 固定 runtime(后端注册表 id)
    model: str = ""                  # 固定模型;"" 表示显式使用 CLI 默认模型
    effort: str = ""                 # 推理力度;"" = Runtime 默认，由统一抽象层校验
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
    content_kind: str = ""            # 绑定的内容页类型：docs / guidelines / skills
    content_key: str = ""             # 内容页在项目内的稳定键（路径、name 或 Skill id）
    context_start_message_id: int = 0  # 最近一次清除上下文的可见分隔消息
    archived: bool = False             # 归档后对人类只读，Agent 默认不感知
    archived_at: float = 0.0
    last_message_at: float = 0.0       # Store 按消息表计算，供列表排序与展示
    created_at: float = field(default_factory=now)

    @property
    def is_general(self) -> bool:
        """项目默认频道兼容历史 ``general`` 与新式 ``project:general``。"""
        return self.id == "general" or self.id.rsplit(":", 1)[-1] == "general"

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
class RuntimePermissions:
    """Runtime 无关的权限意图，由 Runtime 层翻译为各后端参数。"""

    approval: str = "auto"          # auto | prompt | deny
    filesystem: str = "workspace-write"  # read-only | workspace-write | full-access
    network: str = "inherit"        # inherit | allow | deny

    def __post_init__(self):
        choices = {
            "approval": {"auto", "prompt", "deny"},
            "filesystem": {"read-only", "workspace-write", "full-access"},
            "network": {"inherit", "allow", "deny"},
        }
        for field_name, allowed in choices.items():
            value = getattr(self, field_name)
            if value not in allowed:
                raise ValueError(
                    f"Runtime 权限 {field_name}={value!r} 必须是 {sorted(allowed)} 之一")


@dataclass
class RuntimePolicy:
    """一次执行的统一访问策略；主程序不需要知道后端的原生权限参数。"""

    readable_paths: list[str] = field(default_factory=list)
    writable_paths: list[str] = field(default_factory=list)
    skill_paths: list[str] = field(default_factory=list)
    permissions: RuntimePermissions = field(default_factory=RuntimePermissions)

    def allowed_paths(self) -> list[str]:
        found: list[str] = []
        for path in [*self.readable_paths, *self.writable_paths]:
            if path and path not in found:
                found.append(path)
        return found


@dataclass
class ExecutionConfig:
    """装配后的执行配置:平台决定'在什么约束下执行'的最终产物。"""

    task_id: str
    stage_name: str
    backend: Backend
    prompt: str
    workdir: str
    project_id: str = ""  # 状态与历史使用的归属元数据
    role_id: str = ""
    # 项目显式登记、允许 Runtime 读写的本地目录。workdir 是主工作根，
    # allowed_dirs 是旧构造入口；Runtime manager 会把统一策略翻译为后端参数。
    allowed_dirs: list[str] = field(default_factory=list)
    runtime_policy: RuntimePolicy = field(default_factory=RuntimePolicy)
    env: dict = field(default_factory=dict)
    # Agent 执行默认没有时间上限；由完成信号或用户主动停止结束。
    # Optional 值保留给测试和显式调用方设置局部截止时间。
    timeout: Optional[float] = None
    effort: str = ""      # 推理力度，由角色绑定的 Runtime 配置填入
    routing_trace: list[str] = field(default_factory=list)
    # 聊天 Runtime 会话按 channel×role 复用。common_prompt 只在新会话、版本
    # 变化、恢复和重注入触发时完整发送;复用会话且版本未变的增量回合只发
    # 版本引用头 + turn_prompt。recovery_prompt 只在新建/无法恢复原生会话时
    # 使用，包含最近消息用于恢复上下文。
    session_key: str = ""
    session_id: str = ""
    common_prompt: str = ""
    turn_prompt: str = ""
    recovery_prompt: str = ""
    context_version: str = ""
    context_changed: bool = False
    # 距上次完整注入的增量回合数/体积超过阈值,或此前检测到压缩:本轮强制
    # 重注入完整公共上下文(注入模式集合见 INJECTION_FULL_MODES)。
    reinject_due: bool = False
    # 本轮执行期间 Runtime 报告了上下文压缩;保存会话时据此保留重注入标记。
    compact_detected: bool = False
    # 执行可能在线程池中排队；真正拿到会话锁后重读一次，避免两个连续触发都
    # 使用装配时看到的空 id 而各自新建会话。
    load_session: Optional[Callable[[], tuple[str, str, bool]]] = None
    # save_session(session_id, context_version, turn_mode=, turn_bytes=,
    # compact_seen=):轮末携带注入模式与本轮体积,维护增量回合计数。
    save_session: Optional[Callable[..., None]] = None
    # Runtime 检测到上下文压缩时立即持久化重注入标记(独立于轮末保存,
    # 保证本轮异常中断也不丢信号)。
    mark_reinject: Optional[Callable[[], None]] = None
    # 运行过程回调 (kind, text):适配器在执行期间实时上报思考/工具/输出等
    # 事件；None 表示调用方不关心运行过程。
    emit: Optional[Callable[[str, str], None]] = None
    # 双向协议中的权限/用户输入请求。回调会阻塞当前原生请求，直到聊天 UI
    # 返回 decision/answers；未设置时 provider 按无头策略处理。
    interact: Optional[Callable[[str, dict], dict]] = None
    # 执行在 Runtime 自己的 session 锁后仍可能排队；真正启动 turn/进程前
    # 再检查一次，保证频道停止不会只中断当前轮、却放行同会话的下一轮。
    cancelled: Optional[Callable[[], bool]] = None
    # 进程内 Agent Tool 调用 (action, arguments) -> 结果 dict,与 HTTP 入口
    # 同一鉴权/审计路径。真实 CLI Runtime 走子进程 + HTTP,不用该回调;
    # MockAdapter 用它执行 message.publish 等显式命令(如模拟主控派发)。
    agent_action: Optional[Callable[[str, dict], dict]] = None

    def __post_init__(self):
        # 兼容旧的 allowed_dirs 构造入口；新代码只需提供统一策略。
        if self.allowed_dirs and not self.runtime_policy.allowed_paths():
            self.runtime_policy.readable_paths = list(self.allowed_dirs)
            self.runtime_policy.writable_paths = list(self.allowed_dirs)
        elif self.runtime_policy.allowed_paths():
            self.allowed_dirs = self.runtime_policy.allowed_paths()

    def cancellation_requested(self) -> bool:
        return bool(self.cancelled and self.cancelled())
