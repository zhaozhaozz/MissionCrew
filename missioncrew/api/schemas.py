"""API 请求体模型(pydantic)。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    project_id: str
    title: str
    summary: str = ""
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    channel_ids: list[str] = Field(default_factory=list)
    status: str = "open"


class TaskUpdate(BaseModel):
    snapshot_updated_at: float
    title: Optional[str] = None
    summary: Optional[str] = None
    body: Optional[str] = None
    labels: Optional[list[str]] = None
    channel_ids: Optional[list[str]] = None
    status: Optional[str] = None


class TaskBriefInput(BaseModel):
    content: str
    status: Optional[str] = None


class TaskProcessInput(BaseModel):
    message: str = ""


class MessageMentionInput(BaseModel):
    role_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w-]+$")
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class MessageInput(BaseModel):
    author: str = "human"
    content: str
    mentions: list[MessageMentionInput] = Field(default_factory=list, max_length=50)


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
    run_id: int = Field(gt=0)
    request_id: Optional[str] = Field(
        None, min_length=1, max_length=100, pattern=r"^[\w-]+$")


class ChannelCreate(BaseModel):
    id: str
    name: str = ""
    project_id: Optional[str] = None
    workdir: Optional[str] = None
    purpose: str = ""
    actor_role_id: Optional[str] = None


class RoleTemplateInput(BaseModel):
    id: str
    runtime_id: str            # 角色定义时固定的 runtime,必填
    model: str = ""            # 空 = CLI 默认模型
    effort: str = ""           # 推理力度,仅支持的 runtime 可设;空 = CLI 默认
    name: str = ""
    description: str = ""
    capabilities: list[str] = []   # 固定能力选项(ROLE_ABILITIES)
    preference: str = ""           # 偏好:自由文本(风格/领域,如前端/后端)
    color: str = ""
    sort_order: Optional[int] = None   # None = 保留现值;新角色排到项目末尾


class RoleInput(RoleTemplateInput):
    project_id: str


class RoleReorder(BaseModel):
    project_id: str
    ids: list[str]                 # 项目全部角色 id,按目标显示顺序排列


class RoleTemplateReorder(BaseModel):
    ids: list[str]                 # 全部全局角色模板 id,按目标显示顺序排列


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


class SkillFolderImport(BaseModel):
    path: str
    overwrite: bool = False
    actor_role_id: Optional[str] = None


class ProjectInput(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    repos: Optional[list[dict | str]] = None   # None = 保留;资源经专用端点管理
    charter: str = ""
    dev_guidelines: Optional[str] = None       # 已由准则文档替代;None = 保留
    orchestrator_role_id: Optional[str] = None
    max_chain_runs: Optional[int] = Field(None, ge=1)
    guidelines: Optional[list[dict]] = None
    skills: Optional[list[dict | str]] = None
    resources: Optional[list[str]] = None
    required_env: Optional[str] = None


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
    models: Optional[list[dict]] = None   # 模型阶梯 [{name, tier, cost}]
    enabled: Optional[bool] = None
