"""API 请求体模型(pydantic)。"""
from __future__ import annotations

from typing import Optional, Union

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    project_id: str
    title: str
    summary: str = ""
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    channel_ids: list[str] = Field(default_factory=list)
    status: Optional[str] = None   # 状态文本;None = labels 已带则保留,否则默认
    source: Optional[str] = None   # 数据源 id;None = 内置源
    url: Optional[str] = None      # 外部条目链接
    meta: Optional[list[str]] = None   # 看板卡片附注
    external_id: Optional[str] = None  # 外部条目 id(同源唯一,自定义源的整体同步键)
    # 外部系统更新时间:Unix 秒或带时区的 ISO 8601;系统 updated_at 不可设置
    external_updated_at: Optional[Union[float, str]] = None


class TaskUpdate(BaseModel):
    snapshot_updated_at: float
    title: Optional[str] = None
    summary: Optional[str] = None
    body: Optional[str] = None
    labels: Optional[list[str]] = None
    # 逐个设置属性:{属性名: 值 | [值...] | null},null 清空;没提到的属性不动
    properties: Optional[dict[str, Optional[Union[str, list[str]]]]] = None
    channel_ids: Optional[list[str]] = None
    status: Optional[str] = None
    url: Optional[str] = None
    meta: Optional[list[str]] = None
    external_id: Optional[str] = None   # "" 去掉同步键
    external_updated_at: Optional[Union[float, str]] = None   # 0 或 "" 清空


class TaskBriefInput(BaseModel):
    content: str
    status: Optional[str] = None


class MessageMentionInput(BaseModel):
    role_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w-]+$")
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class TaskProcessInput(BaseModel):
    message: str = ""
    # message 内由角色选择器生成的结构化提及;为空时按默认流程交给项目主控
    mentions: list[MessageMentionInput] = Field(default_factory=list, max_length=50)
    # 接收派发消息的唯一频道;为空时用 Task 的首个绑定频道
    channel_id: str = Field(default="", max_length=200)


class AutomationInput(BaseModel):
    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    script: Optional[str] = None
    cron: Optional[str] = None           # 五段 crontab;空字符串 = 不定时
    enabled: Optional[bool] = None       # 只管定时触发
    # 订阅的全局 webhook 来源 id;空字符串 = 不接受 webhook,None = 保留现值
    webhook_source: Optional[str] = None
    # 订阅 GitHub 轮询来源时:要哪些仓库(owner/repo)的事件、只要哪些事件名;None = 保留现值
    github_repos: Optional[list[str]] = None
    github_events: Optional[list[str]] = None
    actions: Optional[list[str]] = None  # None = 保留现值
    timeout_seconds: Optional[int] = Field(None, ge=1)


class WebhookSourceInput(BaseModel):
    id: str = Field(max_length=100)
    name: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    path: Optional[str] = Field(None, max_length=210)   # /hooks/ 之后的部分;空 = 同 id
    enabled: Optional[bool] = None
    kind: Optional[str] = Field(None, max_length=20)    # webhook(默认)| github;只在新建时生效
    # kind=github 的轮询配置(notifications/participating/project_id/interval_seconds),
    # 整份替换;None = 保留现值。仓库由订阅脚本的 github_repos 声明
    github: Optional[dict] = None


class CredentialVariableInput(BaseModel):
    name: str = Field(max_length=64)
    secret: bool = True
    value: Optional[str] = Field(None, max_length=70000)   # None = 保留原值;secret 只写不读


class CredentialInput(BaseModel):
    """一个环境:variables 是保存后的完整变量列表,不在其中的旧变量被删除。"""
    name: str = Field(max_length=64)
    description: Optional[str] = Field(None, max_length=2000)
    variables: list[CredentialVariableInput] = Field(max_length=50)
    create: bool = False     # True = 只新建:同名环境已存在时拒绝,不整份覆盖它


class MessageInput(BaseModel):
    author: str = "human"
    content: str
    mentions: list[MessageMentionInput] = Field(default_factory=list, max_length=50)
    context: dict = Field(default_factory=dict)


class PageContextInput(BaseModel):
    page_kind: str = Field(max_length=32)
    page_key: str = Field(max_length=1000)
    content: str = Field(max_length=2_000_000)


class RuntimeInteractionInput(BaseModel):
    decision: str
    answers: dict = Field(default_factory=dict)
    reason: str = ""


class AgentToolCallInput(BaseModel):
    action: str = Field(min_length=1, max_length=100, pattern=r"^[\w.-]+$")
    arguments: dict = Field(default_factory=dict)
    # 旧客户端可继续发送；服务端以 Bearer token 的逐 Run 绑定为事实源。
    run_id: Optional[int] = Field(None, gt=0)
    request_id: Optional[str] = Field(
        None, min_length=1, max_length=100, pattern=r"^[\w-]+$")


class ChannelCreate(BaseModel):
    id: str = ""      # 留空 = 由 name 推导(`group/topic` -> `group-topic`)
    name: str = ""
    project_id: Optional[str] = None
    workdir: Optional[str] = None
    purpose: str = ""
    actor_role_id: Optional[str] = None


class ChannelUpdate(BaseModel):
    # None = 保留;workdir 传空字符串 = 改回平台内置工作区
    name: Optional[str] = None
    purpose: Optional[str] = None
    workdir: Optional[str] = None


class ContentChannelInput(BaseModel):
    content_kind: str = Field(pattern=r"^(docs|guidelines|skills|custom)$")
    content_key: str = Field(min_length=1, max_length=1000)
    label: str = Field(default="", max_length=1000)


class RoleTemplateInput(BaseModel):
    id: str
    runtime_id: str            # 角色定义时固定的 runtime,必填
    model: str = ""            # 空 = CLI 默认模型
    effort: str = ""           # 推理力度,仅支持的 runtime 可设;空 = CLI 默认
    usage_linkage_enabled: bool = False  # 该角色是否跟随账户限额自动启停
    manual_only: bool = False            # 仅人工点名:Agent 看不到、不能派发给它
    name: str = ""
    description: str = ""
    capabilities: list[str] = []   # 固定能力选项(ROLE_ABILITIES)
    preference: str = ""           # 偏好:自由文本(风格/领域,如前端/后端)
    color: str = ""
    sort_order: Optional[int] = None   # None = 保留现值;新角色排到项目末尾
    max_concurrency: int = Field(default=0, ge=0)  # 跨频道同时执行的轮数上限;0 = 不限


class RoleInput(RoleTemplateInput):
    project_id: str


class RoleReorder(BaseModel):
    project_id: str
    ids: list[str]                 # 项目全部角色 id,按目标显示顺序排列


class RoleEnabledInput(BaseModel):
    project_id: str
    enabled: bool


class RoleTemplateReorder(BaseModel):
    ids: list[str]                 # 全部全局角色模板 id,按目标显示顺序排列


class RoleImport(BaseModel):
    project_id: str
    roles: list[RoleTemplateInput] = Field(min_length=1, max_length=500)
    # 前端预览时明确提示过的覆盖项；服务端复核，避免预览后出现新同名角色。
    overwrite_ids: list[str] = Field(default_factory=list, max_length=500)


class RoleTemplateImport(BaseModel):
    roles: list[RoleTemplateInput] = Field(min_length=1, max_length=500)
    overwrite_ids: list[str] = Field(default_factory=list, max_length=500)


class GuidelineInput(BaseModel):
    markdown: str
    enabled: bool = True
    original_name: Optional[str] = None
    actor_role_id: Optional[str] = None


class GuidelineRestore(BaseModel):
    revision: str = Field(
        min_length=7, max_length=40, pattern=r"^[0-9a-fA-F]{7,40}$")
    actor_role_id: Optional[str] = None


class GuidelineCompare(BaseModel):
    from_revision: str = Field(
        min_length=7, max_length=40, pattern=r"^[0-9a-fA-F]{7,40}$")
    to_revision: str = Field(
        min_length=7, max_length=40, pattern=r"^[0-9a-fA-F]{7,40}$")


class SkillInput(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    instructions: str = ""
    markdown: Optional[str] = None     # 完整 SKILL.md 原文(含 frontmatter),优先于上面三个字段
    enabled: bool = True
    actor_role_id: Optional[str] = None


class SkillRestore(BaseModel):
    revision: str = Field(
        min_length=7, max_length=40, pattern=r"^[0-9a-fA-F]{7,40}$")
    actor_role_id: Optional[str] = None


class SkillCompare(BaseModel):
    from_revision: str = Field(
        min_length=7, max_length=40, pattern=r"^[0-9a-fA-F]{7,40}$")
    to_revision: str = Field(
        min_length=7, max_length=40, pattern=r"^[0-9a-fA-F]{7,40}$")


class SkillFolderImport(BaseModel):
    path: str
    overwrite: bool = False
    actor_role_id: Optional[str] = None


class ProjectInput(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    repos: Optional[list[dict | str]] = None   # None = 保留;代码仓经专用端点管理
    charter: str = ""
    dev_guidelines: Optional[str] = None       # 已由准则文档替代;None = 保留
    orchestrator_role_id: Optional[str] = None
    max_chain_runs: Optional[int] = Field(None, ge=1)
    guidelines: Optional[list[dict]] = None
    skills: Optional[list[dict | str]] = None
    required_env: Optional[str] = None
    task_auto_rules: Optional[list[dict]] = None      # None = 保留现值
    # 内置任务看板锁定状态列之后的自定义筛选列;None = 保留现值
    task_board_filters: Optional[list[dict]] = None
    # 内置任务看板的分列属性;None = 保留现值,空串 = 恢复筛选列
    task_board_group_by: Optional[str] = None


class ResourceAdd(BaseModel):
    target: str          # 本地路径或 git 远程地址
    name: str = ""


class DocumentWrite(BaseModel):
    content: str
    actor: str = "human"
    message: str = ""


class DocumentRestore(BaseModel):
    path: str
    revision: str
    actor: str = "human"


class DocumentCompare(BaseModel):
    path: str
    from_revision: str = Field(
        min_length=7, max_length=40, pattern=r"^[0-9a-fA-F]{7,40}$")
    to_revision: str = Field(
        min_length=7, max_length=40, pattern=r"^[0-9a-fA-F]{7,40}$")


class BoardInput(BaseModel):
    id: str
    name: str = ""
    description: Optional[str] = None    # None = 更新时保留现值
    layout: Optional[list[dict]] = None  # None = 更新时保留现有布局
    actor_role_id: Optional[str] = None
    kind: Optional[str] = None           # widgets | taskboard;None = 保留现值
    source: Optional[str] = None         # taskboard 的数据源 id;None = 保留现值
    # taskboard 的筛选列 [{title,query,color}];None = 保留现值
    filters: Optional[list[dict]] = None
    # taskboard 的分列属性(按取值横向分列);None = 保留现值,空串 = 恢复筛选列
    group_by: Optional[str] = None


class WidgetDataInput(BaseModel):
    widgets: list[dict] = []


class BackendInput(BaseModel):
    id: str
    name: Optional[str] = None
    model: Optional[str] = None
    tier: Optional[str] = None
    cost_per_run: Optional[float] = None
    security_level: Optional[int] = None
    capabilities: Optional[list[str]] = None
    enabled: Optional[bool] = None
