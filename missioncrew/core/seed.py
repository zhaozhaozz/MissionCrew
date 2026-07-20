"""演示种子数据:一组分档后端 + 一个带验证准则的示例项目。

后端全部使用 mock 适配器,可零成本走通全流程;换成真实后端只需把
adapter 改为 claude_code / codex 并配置模型。
"""
from __future__ import annotations

from .models import (TIER_ORDER, Backend, Channel, Project, ProjectSkill,
                     Resource, Role, Rule, _RETIRED_ABILITY_TEXT,
                     preference_segments)
from .store import Store

DEMO_BACKENDS = [
    Backend(id="eco-1", name="经济型执行者", adapter="mock", model="mini",
            tier="economy", capabilities=["coding"], cost_per_run=1),
    Backend(id="std-1", name="标准执行者", adapter="mock", model="pro",
            tier="standard", capabilities=["coding", "reasoning", "web_search"],
            cost_per_run=4),
    Backend(id="exp-1", name="专家执行者", adapter="mock", model="ultra",
            tier="expert", capabilities=["coding", "reasoning", "sub_agents", "web_search"],
            cost_per_run=15),
    Backend(id="vis-1", name="多模态验证者", adapter="mock", model="pro-vision",
            tier="standard", capabilities=["coding", "multimodal"], cost_per_run=5),
    Backend(id="rev-1", name="独立 Reviewer", adapter="mock", model="pro",
            tier="standard", capabilities=["coding", "reasoning", "review", "security"],
            cost_per_run=4),
    Backend(id="trust-1", name="可信环境执行者", adapter="mock", model="onprem",
            tier="standard", capabilities=["coding", "reasoning", "review", "security"],
            security_level=2, cost_per_run=8),
]

DEMO_PROJECT = Project(
    id="webshop",
    name="WebShop 电商站",
    description="演示项目:一个典型的 Web 电商应用",
    repos=["~/code/demo/webshop"],
    charter="面向 C 端的电商站点,核心域是商品、购物车、订单、支付。"
            "任何改动不得破坏下单主链路。",
    dev_guidelines="后端 Python/FastAPI,前端 React。公共 API 保持向后兼容;"
                   "数据库变更必须走迁移脚本;错误必须显式处理,禁止裸 except。",
    skills=[ProjectSkill(id="webshop-local-ci", name="WebShop 本地 CI"),
            ProjectSkill(id="webshop-db-migration", name="WebShop 数据库迁移")],
    resources=["test-db"],
    rules=[
        Rule(match={"task_type": "bug"},
             require_evidence=["reproduction", "regression_test"],
             note="Bug 必须先复现,并提供修复前失败、修复后通过的回归证据"),
        Rule(match={"labels": ["ui"]},
             require_evidence=["browser_test", "screenshot"],
             require_capabilities=["multimodal"],
             note="前端界面变化必须完成浏览器流程测试和视觉验证"),
        Rule(match={"labels": ["auth"]},
             require_gates=["security_review"],
             note="认证逻辑变化必须增加安全审查"),
        Rule(match={"risk": "high"},
             require_gates=["human_approval"],
             note="高风险(如生产环境)变更必须人工审批"),
    ],
)

DEMO_RESOURCES = [
    Resource(id="test-db", kind="db", description="测试数据库(只读)",
             env="TEST_DB_URL", secret_ref="WEBSHOP_TEST_DB_URL",
             security_level=0, stages=["reproduce", "fix", "regression", "verify", "develop"],
             ttl_seconds=3600),
]


_DEFAULT_ROLE_TIERS = {
    "lead": "standard", "dev": "standard", "reviewer": "standard",
    "expert": "expert", "vision": "standard", "secure": "standard",
    "tester": "economy", "scribe": "economy",
}


def _choose_role_unit(store: Store, role: Role) -> Backend | None:
    """为默认/旧角色一次性选择执行单元;选择结果会持久化,运行时不再路由。"""
    units = [u for b in store.list_backends() if b.enabled for u in b.units()]
    if not units:
        return None
    need = set(role.capabilities)
    # 职责标签(代码评审/安全审查等)已从角色能力迁移进偏好文本;
    # 绑定时按片段还原为 Backend 能力位倾向,让评审/安全角色仍落在
    # 具备 review/security 位的后端上(软约束:无满足单元时回退全部)
    segs = preference_segments(role.preference)
    need |= {cap for cap, text in _RETIRED_ABILITY_TEXT.items() if text in segs}
    compatible = [u for u in units if need <= set(u.capabilities)]
    pool = compatible or units
    target = TIER_ORDER.index(_DEFAULT_ROLE_TIERS.get(role.id, "standard"))

    def score(unit: Backend) -> tuple:
        tier = TIER_ORDER.index(unit.tier)
        return (len(need - set(unit.capabilities)), abs(tier - target),
                tier < target, len(set(unit.capabilities) - need),
                unit.cost_per_run, unit.id, unit.model)

    return min(pool, key=score)


def _bind_role(store: Store, role: Role) -> bool:
    unit = _choose_role_unit(store, role)
    if unit is None:
        return False
    role.runtime_id = unit.id
    role.model = unit.model
    return True


def has_enabled_runtime(store: Store) -> bool:
    return any(b.enabled for b in store.list_backends())


# 内置角色只用于首次初始化全局模板。之后全局模板由用户维护，新项目复制当前
# 模板快照；模板或项目角色的后续修改互不联动。
def _builtin_role_templates() -> list[Role]:
    return [
        Role(id="lead", name="主管", color="#d97706",
             description="调度者,不亲自实现。接到需求先结合项目章程理解目标,必要时拆解;"
                         "对照名册按各角色定位挑选人选,@分派时为每个子任务写清背景、要求、"
                         "验收标准,并要求完成后向你汇报;收到汇报后核对验收标准再汇总结论。",
             capabilities=["reasoning"], preference="统筹与调度,重质量"),
        Role(id="dev", name="开发", color="#3564d7",
             description="全栈开发工程师,负责实现需求、修复缺陷。动手前先看清现有代码约定。",
             capabilities=["coding"], preference="全栈"),
        Role(id="reviewer", name="评审", color="#2e9e5b",
             description="独立代码评审员,只审查不改代码:正确性、可维护性、边界条件。",
             capabilities=[], preference="代码评审,严谨,只审不改"),
        Role(id="expert", name="专家", color="#8b5cf6",
             description="资深架构师,处理疑难问题、复杂分析和大型重构方案。",
             capabilities=["coding", "reasoning"], preference="深度攻坚,高质量"),
        Role(id="vision", name="视觉验证", color="#c98a1b",
             description="多模态验证员,负责页面截图、浏览器流程测试和视觉回归确认。",
             capabilities=["multimodal"], preference="页面与视觉验证"),
        Role(id="secure", name="安全", color="#c94b3c",
             description="安全工程师,从注入、越权、凭据泄露等角度审查变更与配置。",
             capabilities=[], preference="安全审查,代码评审视角"),
        Role(id="tester", name="测试", color="#0e9488",
             description="测试工程师,写用例、跑回归、构造边界输入,报告只讲事实与复现步骤。",
             capabilities=["coding"], preference="适合测试,快速反馈"),
        Role(id="scribe", name="文档", color="#64748b",
             description="技术写作者,维护 README、变更说明和使用文档,行文简洁面向读者。",
             capabilities=[], preference="适合文档,快速低成本"),
    ]


def ensure_role_templates(store: Store) -> int:
    """旧数据库首次升级时播种全局模板；已有模板永不覆盖。"""
    if store.list_role_templates():
        return 0
    roles = _builtin_role_templates()
    for i, role in enumerate(roles):
        role.sort_order = (i + 1) * 10
        if not _bind_role(store, role):
            return 0
    for role in roles:
        store.put_role_template(role)
    store.audit("platform", "role_templates_seeded",
                detail=f"roles={','.join(role.id for role in roles)}")
    return len(roles)


def project_roles_from_templates(store: Store, project_id: str) -> list[Role]:
    """校验并复制当前全局模板，供新项目在写入前完整准备角色。"""
    templates = store.list_role_templates()
    if not templates:
        raise RuntimeError("全局角色模板为空,请先检测并启用 runtime,再到全局设置中配置角色")
    roles = []
    for template in templates:
        backend = store.get_backend(template.runtime_id)
        if backend is None:
            raise RuntimeError(
                f"全局角色模板 @{template.id} 使用的 runtime 不存在: {template.runtime_id}")
        if not backend.enabled:
            raise RuntimeError(
                f"全局角色模板 @{template.id} 使用的 runtime 已停用: {template.runtime_id}")
        data = template.to_dict()
        data["project_id"] = project_id
        roles.append(Role.from_dict(data))
    return roles


def default_roles(store: Store, project_id: str) -> list[Role]:
    """兼容旧调用名：默认角色现从可配置的全局模板复制。"""
    return project_roles_from_templates(store, project_id)


def init_project(store: Store, project_id: str, roles: list[Role] | None = None) -> None:
    """项目初始化:复制全局角色模板并创建 general 频道(已存在的不覆盖)。"""
    for role in roles or project_roles_from_templates(store, project_id):
        if store.get_role(project_id, role.id) is None:
            store.put_role(role)
    if not store.list_channels(project_id):
        store.put_channel(Channel(id=f"{project_id}:general", name="general",
                                  project_id=project_id))


DEFAULT_CHANNEL = Channel(id="general", name="大厅", project_id="webshop")


def seed(store: Store) -> None:
    for b in DEMO_BACKENDS:
        store.put_backend(b)
    for r in DEMO_RESOURCES:
        store.put_resource(r)
    ensure_role_templates(store)
    demo_roles = project_roles_from_templates(store, DEMO_PROJECT.id)
    DEMO_PROJECT.orchestrator_role_id = demo_roles[0].id
    store.put_project(DEMO_PROJECT)
    for role in demo_roles:
        store.put_role(role)
    if store.get_channel(DEFAULT_CHANNEL.id) is None:
        store.put_channel(DEFAULT_CHANNEL)
    store.audit("platform", "seeded", detail="演示数据已写入")


def ensure_default_project(store: Store) -> None:
    """平台至少要有一个项目(项目是第一层级);没有时创建 default 项目。"""
    if store.list_projects() or not has_enabled_runtime(store):
        return
    ensure_role_templates(store)
    roles = project_roles_from_templates(store, "default")
    store.put_project(Project(id="default", name="默认项目",
                              description="首次使用自动创建,可在项目页改名或新建其他项目",
                              orchestrator_role_id=roles[0].id))
    init_project(store, "default", roles)


def ensure_role_bindings(store: Store) -> int:
    """把旧版可选 pinned_* / 自动路由角色一次性迁移为固定 runtime/model。"""
    bound = 0
    for role in store.list_roles():
        if not role.runtime_id or store.get_backend(role.runtime_id) is None:
            if not _bind_role(store, role):
                continue
            bound += 1
        # 即使已有固定组合也重写一次,清除旧 JSON 字段并落成新模型。
        store.put_role(role)
    return bound


def migrate_project_fields(store: Store) -> int:
    """一次性字段迁移:开发准则并入准则文档;旧版字符串 repos 归一化为资源。

    幂等:dev_guidelines 迁移后清空;repos 经 Project.__post_init__ 归一化,
    重写一遍即落库为结构化条目。
    """
    migrated = 0
    for role in store.list_role_templates():
        store.put_role_template(role)
    for role in store.list_roles():
        store.put_role(role)   # 旧 traits 标签经 from_dict 迁移为 preference,重写落库
    for project in store.list_projects():
        changed = False
        if project.dev_guidelines.strip():
            gid = "dev-guidelines"
            if not any(g.id == gid for g in project.guidelines):
                from .models import GuidelineDocument
                project.guidelines.append(GuidelineDocument(
                    id=gid, title="开发准则", content=project.dev_guidelines))
            project.dev_guidelines = ""
            changed = True
            migrated += 1
        store.put_project(project)   # 顺带把旧字符串 repos 写成结构化资源
        if changed:
            store.audit("platform", "project_migrated",
                        detail=f"project={project.id} dev_guidelines->guideline")
    return migrated
