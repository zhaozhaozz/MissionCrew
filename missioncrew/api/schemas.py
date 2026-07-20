"""API 请求体模型(pydantic)。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    project_id: str
    title: str
    description: str = ""
    task_type: str = "feature"
    labels: list[str] = []
    risk: str = "normal"
    security_level: int = 0
    max_tier: Optional[str] = None


class ApprovalInput(BaseModel):
    approver: str = "human"
    decision: str = "approved"
    note: str = ""
    stage: Optional[str] = None


class MessageInput(BaseModel):
    author: str = "human"
    content: str


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


class SkillInput(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    instructions: str = ""
    enabled: bool = True
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
