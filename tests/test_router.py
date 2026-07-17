"""路由器:安全 -> 能力 -> 独立性 -> 档位窗口 -> 成功率/成本。"""
from missioncrew import router
from missioncrew.models import Backend, Task, TaskStage


def _task(**kw):
    defaults = dict(id="t_x", project_id="webshop", title="t")
    defaults.update(kw)
    return Task(**defaults)


def test_prefers_cheapest_tier(seeded):
    task = _task()
    stage = TaskStage(name="develop")
    d = router.route(seeded, task, stage, seeded.get_project("webshop"))
    assert d.backend is not None
    assert d.backend.tier == "economy"


def test_security_filter_excludes_low_clearance(seeded):
    task = _task(security_level=2)
    stage = TaskStage(name="develop")
    d = router.route(seeded, task, stage, seeded.get_project("webshop"))
    assert d.backend is not None
    assert d.backend.id == "trust-1"  # 只有可信环境执行者密级达标


def test_capability_filter(seeded):
    task = _task()
    stage = TaskStage(name="verify", required_capabilities=["multimodal"])
    d = router.route(seeded, task, stage, seeded.get_project("webshop"))
    assert d.backend is not None
    assert "multimodal" in d.backend.capabilities


def test_independence_excludes_work_backends(seeded):
    task = _task()
    task.stages = [TaskStage(name="develop", kind="work", backend_id="rev-1", status="passed")]
    stage = TaskStage(name="review", kind="review", independent=True,
                      required_capabilities=["review"])
    d = router.route(seeded, task, stage, seeded.get_project("webshop"))
    # rev-1 参与过 work,独立审查必须换人;种子里另一个有 review 能力的是 trust-1
    assert d.backend is not None
    assert d.backend.id == "trust-1"


def test_escalation_raises_tier_floor(seeded):
    task = _task()
    stage = TaskStage(name="develop", attempts=1)  # 失败过一次
    d = router.route(seeded, task, stage, seeded.get_project("webshop"))
    assert d.backend is not None
    assert d.backend.tier != "economy"


def test_max_tier_budget_cap(seeded):
    task = _task(max_tier="economy")
    stage = TaskStage(name="develop", attempts=1)  # 需要升级但预算不允许
    d = router.route(seeded, task, stage, seeded.get_project("webshop"))
    assert d.backend is None


def test_low_success_rate_switches_backend(seeded):
    # 经济档在该项目/类型上大量失败后,路由不再为省钱冒险
    for _ in range(5):
        seeded.stats_record("eco-1", "webshop", "feature", False)
    task = _task()
    stage = TaskStage(name="develop")
    d = router.route(seeded, task, stage, seeded.get_project("webshop"))
    assert d.backend is not None
    assert d.backend.id != "eco-1"


def test_quota_exhausted_excluded(seeded):
    b = seeded.get_backend("eco-1")
    b.quota = 0.0
    seeded.put_backend(b)
    task = _task()
    stage = TaskStage(name="develop")
    d = router.route(seeded, task, stage, seeded.get_project("webshop"))
    assert d.backend is not None
    assert d.backend.id != "eco-1"


# ---- 工具×模型:一个工具挂模型阶梯,路由展开成执行单元 ----

def _single_tool_store(seeded):
    """只留一个带模型阶梯的工具,模拟真实单 CLI 环境。"""
    for b in seeded.list_backends():
        b.enabled = False
        seeded.put_backend(b)
    seeded.put_backend(Backend(
        id="claude", name="claude", adapter="mock", tier="standard", cost_per_run=5.0,
        capabilities=["coding", "reasoning", "review"],
        models=[{"name": "haiku", "tier": "economy", "cost": 1.0},
                {"name": "", "tier": "standard", "cost": 5.0},
                {"name": "opus", "tier": "expert", "cost": 20.0}]))


def test_model_ladder_prefers_cheapest_model(seeded):
    _single_tool_store(seeded)
    d = router.route(seeded, _task(), TaskStage(name="develop"),
                     seeded.get_project("webshop"))
    assert d.backend is not None
    assert (d.backend.id, d.backend.model, d.backend.tier) == ("claude", "haiku", "economy")
    assert d.backend.cost_per_run == 1.0


def test_model_ladder_escalates_within_tool(seeded):
    _single_tool_store(seeded)
    d = router.route(seeded, _task(), TaskStage(name="develop", attempts=2),
                     seeded.get_project("webshop"))
    assert d.backend is not None
    assert (d.backend.model, d.backend.tier) == ("opus", "expert")


def test_model_units_do_not_mutate_registry(seeded):
    _single_tool_store(seeded)
    router.route(seeded, _task(), TaskStage(name="develop"), seeded.get_project("webshop"))
    stored = seeded.get_backend("claude")
    assert stored.model == "" and stored.tier == "standard"  # 注册表记录未被模型副本污染
