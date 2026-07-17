"""演示种子数据:一组分档后端 + 一个带验证准则的示例项目。

后端全部使用 mock 适配器,可零成本走通全流程;换成真实后端只需把
adapter 改为 claude_code / codex 并配置模型。
"""
from __future__ import annotations

from .models import TIER_ORDER, Backend, Channel, Project, Resource, Role, Rule
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
    skills=["webshop-local-ci", "webshop-db-migration"],
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


# 默认角色模板:人格(description)是"选人用的专长画像",供调度方(人类或
# @lead)挑选协作对象;任务简报由调度方结合项目章程撰写,人格不承担任务描述。
# 每个角色在创建时按能力与目标档位一次性绑定 runtime/model,之后不再自动路由。
def default_roles(store: Store, project_id: str) -> list[Role]:
    roles = [
        Role(id="lead", project_id=project_id, name="主管", color="#d97706",
             description="调度者,不亲自实现。接到需求先结合项目章程理解目标,必要时拆解;"
                         "对照名册按各角色定位挑选人选,@分派时为每个子任务写清背景、要求、"
                         "验收标准,并要求完成后向你汇报;收到汇报后核对验收标准再汇总结论。",
             capabilities=["reasoning"], traits=["quality"]),
        Role(id="dev", project_id=project_id, name="开发", color="#3564d7",
             description="全栈开发工程师,负责实现需求、修复缺陷。动手前先看清现有代码约定。",
             capabilities=["coding"]),
        Role(id="reviewer", project_id=project_id, name="评审", color="#2e9e5b",
             description="独立代码评审员,只审查不改代码:正确性、可维护性、边界条件。",
             capabilities=["review"], traits=["review"]),
        Role(id="expert", project_id=project_id, name="专家", color="#8b5cf6",
             description="资深架构师,处理疑难问题、复杂分析和大型重构方案。",
             capabilities=["coding", "reasoning"], traits=["deep", "quality"]),
        Role(id="vision", project_id=project_id, name="视觉验证", color="#c98a1b",
             description="多模态验证员,负责页面截图、浏览器流程测试和视觉回归确认。",
             capabilities=["multimodal"], traits=["multimodal"]),
        Role(id="secure", project_id=project_id, name="安全", color="#c94b3c",
             description="安全工程师,从注入、越权、凭据泄露等角度审查变更与配置。",
             capabilities=["security", "review"], traits=["security"]),
        Role(id="tester", project_id=project_id, name="测试", color="#0e9488",
             description="测试工程师,写用例、跑回归、构造边界输入,报告只讲事实与复现步骤。",
             capabilities=["coding"], traits=["testing", "fast"]),
        Role(id="scribe", project_id=project_id, name="文档", color="#64748b",
             description="技术写作者,维护 README、变更说明和使用文档,行文简洁面向读者。",
             traits=["docs", "fast", "low-cost"]),
    ]
    for role in roles:
        if not _bind_role(store, role):
            raise RuntimeError("没有已启用的 runtime,无法创建固定执行角色")
    return roles


def init_project(store: Store, project_id: str) -> None:
    """项目初始化:播种该项目的默认角色与 general 频道(已存在的不覆盖)。"""
    for role in default_roles(store, project_id):
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
    store.put_project(DEMO_PROJECT)
    for role in default_roles(store, DEMO_PROJECT.id):
        store.put_role(role)
    if store.get_channel(DEFAULT_CHANNEL.id) is None:
        store.put_channel(DEFAULT_CHANNEL)
    store.audit("platform", "seeded", detail="演示数据已写入")


def ensure_default_project(store: Store) -> None:
    """平台至少要有一个项目(项目是第一层级);没有时创建 default 项目。"""
    if store.list_projects() or not has_enabled_runtime(store):
        return
    store.put_project(Project(id="default", name="默认项目",
                              description="首次使用自动创建,可在项目页改名或新建其他项目"))
    init_project(store, "default")


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
