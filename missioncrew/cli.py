"""mc 命令行:统一任务入口的终端形态。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import typer
import yaml

from .runtime import adapters
from .core import seed as seed_mod
from .collab.chat import ChatEngine
from .core.config import db_path, mc_home
from .taskflow.engine import Engine
from .collab.documents import library_for
from .core.models import Backend, Channel, Project, Role, Task
from .core.store import Store

app = typer.Typer(help="MissionCrew:策略驱动的多 Agent 研发任务执行平台(纯本地)")
project_app = typer.Typer(help="项目中心")
backend_app = typer.Typer(help="后端与模型注册表")
task_app = typer.Typer(help="任务")
chat_app = typer.Typer(help="聊天协作")
role_app = typer.Typer(help="角色")
app.add_typer(project_app, name="project")
app.add_typer(backend_app, name="backend")
app.add_typer(task_app, name="task")
app.add_typer(chat_app, name="chat")
app.add_typer(role_app, name="role")


def _store() -> Store:
    store = Store(db_path())
    seed_mod.ensure_role_bindings(store)
    return store


def _engine() -> Engine:
    return Engine(_store())


def _fmt_ts(ts: float) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


def _print_task(t: Task) -> None:
    typer.echo(f"[{t.id}] {t.title}")
    typer.echo(f"  项目={t.project_id} 类型={t.task_type} 风险={t.risk} "
               f"密级={t.security_level} 状态={t.status}")
    for i, s in enumerate(t.stages):
        mark = "▶" if i == t.stage_index and t.status not in ("done",) else " "
        extra = []
        if s.backend_id:
            extra.append(f"by {s.backend_id}")
        if s.attempts:
            extra.append(f"重试 {s.attempts}")
        if s.human_gate:
            extra.append("需人工审批")
        if s.independent:
            extra.append("独立")
        typer.echo(f"  {mark} {s.name:<16} [{s.status:^17}] {' '.join(extra)}")


# ---------------- 平台 ----------------

@app.command()
def demo(run: bool = typer.Option(True, help="是否顺带演示典型流程")):
    """写入演示数据(mock 后端 + WebShop 项目 + 默认角色),并演示典型流程。"""
    store = _store()
    seed_mod.seed(store)
    typer.echo(f"演示数据已写入 {mc_home()}")
    typer.echo("后端: " + ", ".join(f"{b.id}({b.tier})" for b in store.list_backends()))
    typer.echo("角色: " + ", ".join(f"@{r.id}" for r in store.list_roles()))
    if not run:
        return

    engine = Engine(store)
    typer.echo("\n=== 演示 1:困难 Bug(经济档失败 -> 自动升级标准档) ===")
    t1 = engine.create_task(
        "webshop", "购物车合计金额在优惠券叠加时错误", task_type="bug",
        labels=["hard"], description="多张优惠券叠加时合计金额偏大")
    for r in engine.run(t1.id):
        typer.echo(f"  · {r.message}")

    typer.echo("\n=== 演示 2:高风险认证改动(安全审查 + 人工审批门禁) ===")
    t2 = engine.create_task(
        "webshop", "登录接口增加多因素认证", task_type="feature",
        labels=["auth"], risk="high", description="新增 TOTP 二次验证")
    for r in engine.run(t2.id):
        typer.echo(f"  · {r.message}")
    typer.echo(f"  批准命令: mc approve {t2.id} --approver 你的名字")

    typer.echo("\n=== 演示 3:聊天协作(@dev 干活,完成后自动 @reviewer 评审) ===")
    chat = ChatEngine(store)
    chat.post("general", "human",
              "@dev 优惠券叠加的边界条件再排查一遍,完成后请 @reviewer 复核结论。")
    chat.wait_idle()
    for m in store.list_messages("general"):
        who = f"@{m['author']}" if m["author_type"] == "agent" else m["author"]
        typer.echo(f"  [{who}] {m['content'].splitlines()[0]}")
    typer.echo("\n打开看板与聊天: mc serve  ->  http://127.0.0.1:8321")
    typer.echo("接入真实本地 Agent: mc backend detect")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8321):
    """启动 Web 服务(REST API + 看板)。"""
    import uvicorn
    from .api import create_app
    typer.echo(f"MissionCrew 看板: http://{host}:{port}")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


@app.command()
def approve(task_id: str,
            approver: str = typer.Option("human", help="审批人"),
            stage: Optional[str] = typer.Option(None, help="阶段名,默认当前阶段"),
            reject: bool = typer.Option(False, help="拒绝而不是批准"),
            note: str = typer.Option("", help="审批意见")):
    """人工审批当前等待的门禁,批准后自动继续推进。"""
    engine = _engine()
    decision = "rejected" if reject else "approved"
    task = engine.approve(task_id, approver, decision, note, stage)
    typer.echo(f"审批已记录: {decision}")
    if decision == "approved":
        for r in engine.run(task.id):
            typer.echo(f"  · {r.message}")
    _print_task(engine.store.get_task(task_id))  # type: ignore[arg-type]


@app.command()
def audit(task_id: Optional[str] = typer.Argument(None), limit: int = 50):
    """查看审计日志(平台每个决策都可追溯)。"""
    for row in reversed(_store().list_audit(task_id, limit)):
        typer.echo(f"{_fmt_ts(row['ts'])} [{row['actor']}] {row['action']} "
                   f"{row['task_id']} {row['detail']}")


# ---------------- 项目 ----------------

@project_app.command("add")
def project_add(file: Path = typer.Option(..., help="项目定义 YAML 文件")):
    """从 YAML 导入/更新项目(含验证准则)。"""
    data = yaml.safe_load(file.read_text())
    p = Project.from_dict(data)
    store = _store()
    is_new = store.get_project(p.id) is None
    if is_new and not seed_mod.has_enabled_runtime(store):
        raise typer.BadParameter("请先检测并启用至少一个 runtime,再创建项目角色")
    if not is_new and store.get_role(p.id, p.orchestrator_role_id) is None:
        raise typer.BadParameter(f"主控角色不属于当前项目: @{p.orchestrator_role_id}")
    if is_new and p.orchestrator_role_id != "lead":
        raise typer.BadParameter("新项目请先创建角色，再修改主控角色")
    store.put_project(p)
    if is_new:
        seed_mod.init_project(store, p.id)
        library_for(p.id)
    typer.echo(f"项目已保存: {p.id}")


@project_app.command("list")
def project_list():
    for p in _store().list_projects():
        typer.echo(f"{p.id:<16} {p.name}  准则 {len(p.rules)} 条")


@project_app.command("show")
def project_show(project_id: str):
    p = _store().get_project(project_id)
    if p is None:
        raise typer.Exit(1)
    typer.echo(json.dumps(p.to_dict(), ensure_ascii=False, indent=2))


# ---------------- 后端 ----------------

@backend_app.command("add")
def backend_add(file: Path = typer.Option(..., help="后端定义 YAML(单个或列表)")):
    data = yaml.safe_load(file.read_text())
    items = data if isinstance(data, list) else [data]
    store = _store()
    for d in items:
        b = Backend(**d)
        store.put_backend(b)
        typer.echo(f"后端已保存: {b.id}")


@backend_app.command("list")
def backend_list():
    for b in _store().list_backends():
        quota = "∞" if b.quota is None else f"{b.quota:g}"
        typer.echo(f"{b.id:<10} {b.name:<12} adapter={b.adapter:<12} tier={b.tier:<8} "
                   f"密级={b.security_level} 成本={b.cost_per_run:g} 配额={quota} "
                   f"能力=[{','.join(b.capabilities)}]")


@backend_app.command("detect")
def backend_detect(register: bool = typer.Option(True, help="检测到后立即注册")):
    """扫描本机已安装的 Agent CLI 工具(仿 Multica Runtime:工具+版本+状态)。"""
    report = adapters.detect_report()
    for item in report:
        if item["installed"]:
            typer.echo(f"  ✓ {item['binary']:<14} {item['version'] or '?':<12} {item['path']}")
        else:
            typer.echo(f"  - {item['binary']:<14} 未安装")
    found = adapters.detect_backends(report)
    if not found:
        raise typer.Exit(1)
    if register:
        store = _store()
        for b in found:
            existing = store.get_backend(b.id)
            if existing is None:
                store.put_backend(b)
            else:  # 只刷新检测信息,保留用户的启停/配额/模型调整
                existing.binary_path, existing.version = b.binary_path, b.version
                if not existing.models:
                    existing.models = b.models
                store.put_backend(existing)
        seed_mod.ensure_default_project(store)  # 平台至少要有一个项目
        seed_mod.ensure_role_bindings(store)
        typer.echo("工具已注册/刷新;默认项目(含角色与 general 频道)就绪。mc serve 打开页面。")


# ---------------- 聊天 ----------------

def _resolve_channel(store: Store, channel: str, project: Optional[str]) -> str:
    """频道解析:精确 id 优先;给了项目则用 '<project>:<name>';否则按名唯一匹配。"""
    if store.get_channel(channel):
        return channel
    if project and store.get_channel(f"{project}:{channel}"):
        return f"{project}:{channel}"
    hits = [c.id for c in store.list_channels(project)
            if c.name == channel or c.id.endswith(f":{channel}")]
    if len(hits) == 1:
        return hits[0]
    raise typer.BadParameter(
        f"无法唯一定位频道 '{channel}'"
        + (f"(项目 {project})" if project else ",可加 --project 限定")
        + f";候选: {hits or [c.id for c in store.list_channels(project)]}")


@chat_app.command("send")
def chat_send(content: str = typer.Argument(..., help="消息内容,@角色 触发执行"),
              channel: str = typer.Option("general", "-c", "--channel"),
              project: Optional[str] = typer.Option(None, "-p", "--project"),
              author: str = typer.Option("human", "--author"),
              wait: bool = typer.Option(True, help="等待所有触发的执行(含级联)结束")):
    """向频道发消息;@到的角色会执行工作并把回复发回频道。"""
    store = _store()
    cid = _resolve_channel(store, channel, project)
    chat = ChatEngine(store)
    msgs = store.list_messages(cid)
    before = msgs[-1]["id"] if msgs else 0
    chat.post(cid, author, content)
    if wait:
        chat.wait_idle()
    for m in store.list_messages(cid, after_id=before):
        who = f"@{m['author']}" if m["author_type"] == "agent" else m["author"]
        typer.echo(f"[{who}] {m['content']}")


@chat_app.command("log")
def chat_log(channel: str = typer.Argument("general"),
             project: Optional[str] = typer.Option(None, "-p", "--project"),
             limit: int = 50):
    """查看频道聊天记录(含 Agent 之间的协作消息)。"""
    store = _store()
    cid = _resolve_channel(store, channel, project)
    for m in store.recent_messages(cid, limit):
        who = f"@{m['author']}" if m["author_type"] == "agent" else m["author"]
        typer.echo(f"{_fmt_ts(m['created_at'])} [{who}] {m['content']}")


@chat_app.command("channels")
def chat_channels(create: Optional[str] = typer.Option(None, help="创建频道名"),
                  project: Optional[str] = typer.Option(None, "-p", "--project",
                                                        help="所属项目(创建时必填)"),
                  workdir: Optional[str] = typer.Option(None, help="执行工作目录")):
    store = _store()
    if create:
        if not project or store.get_project(project) is None:
            raise typer.BadParameter("创建频道必须用 --project 指定已存在的项目")
        cid = f"{project}:{create}"
        store.put_channel(Channel(id=cid, name=create, project_id=project,
                                  workdir=workdir))
        typer.echo(f"频道已创建: {cid}")
        return
    for c in store.list_channels(project):
        extra = [f"项目={c.project_id}"]
        if c.workdir:
            extra.append(f"工作目录={c.workdir}")
        typer.echo(f"# {c.id:<20} {c.name}  {' '.join(extra)}")


# ---------------- 角色 ----------------

@role_app.command("list")
def role_list(project: Optional[str] = typer.Option(None, "-p", "--project")):
    for r in _store().list_roles(project):
        model = r.model or "(CLI 默认)"
        fixed = f" runtime={r.runtime_id or '(未配置)'}/{model}"
        if r.effort:
            fixed += f"/effort={r.effort}"
        traits = f" 偏好={r.preference}" if r.preference else ""
        caps = f" 能力=[{','.join(r.capabilities)}]" if r.capabilities else ""
        typer.echo(f"[{r.project_id}] @{r.id:<10} {r.name:<6}{traits}{caps}{fixed}  {r.description}")


@role_app.command("add")
def role_add(file: Path = typer.Option(..., help="角色定义 YAML(单个或列表)"),
             project: Optional[str] = typer.Option(None, "-p", "--project",
                                                   help="覆盖/补充 YAML 中的 project_id")):
    data = yaml.safe_load(file.read_text())
    items = data if isinstance(data, list) else [data]
    store = _store()
    for d in items:
        if project:
            d["project_id"] = project
        r = Role.from_dict(d)   # 经迁移入口,示例 YAML 的旧版 traits 等字段可直接用
        if not r.project_id or store.get_project(r.project_id) is None:
            raise typer.BadParameter(f"@{r.id} 缺少有效的 project_id(角色按项目隔离)")
        backend = store.get_backend(r.runtime_id)
        if backend is None:
            raise typer.BadParameter(f"@{r.id} 缺少有效的 runtime_id")
        known_models = {str(m.get("name", "")) for m in backend.models}
        if known_models and r.model not in known_models:
            raise typer.BadParameter(f"@{r.id} 的模型不属于 runtime {r.runtime_id}")
        if r.effort and r.effort not in adapters.EFFORT_SUPPORT.get(backend.adapter, []):
            raise typer.BadParameter(
                f"@{r.id} 的 effort={r.effort} 不受 runtime {r.runtime_id} 支持")
        store.put_role(r)
        typer.echo(f"角色已保存: [{r.project_id}] @{r.id}")


# ---------------- 任务 ----------------

@task_app.command("create")
def task_create(project: str = typer.Option(..., "-p", "--project"),
                title: str = typer.Option(..., "--title"),
                description: str = typer.Option("", "-d", "--desc"),
                task_type: str = typer.Option("feature", "-t", "--type"),
                label: list[str] = typer.Option([], "-l", "--label"),
                risk: str = typer.Option("normal", help="low|normal|high"),
                security_level: int = typer.Option(0, help="任务密级"),
                max_tier: Optional[str] = typer.Option(None, help="成本上限档位"),
                run: bool = typer.Option(False, help="创建后立即推进")):
    engine = _engine()
    t = engine.create_task(project, title, description, task_type, list(label),
                           risk, security_level, max_tier)
    typer.echo(f"任务已创建: {t.id},计划: {' -> '.join(s.name for s in t.stages)}")
    if run:
        for r in engine.run(t.id):
            typer.echo(f"  · {r.message}")
        _print_task(engine.store.get_task(t.id))  # type: ignore[arg-type]


@task_app.command("run")
def task_run(task_id: str, step: bool = typer.Option(False, help="只推进一步")):
    engine = _engine()
    reports = [engine.step(task_id)] if step else engine.run(task_id)
    for r in reports:
        typer.echo(f"  · {r.message}")
    _print_task(engine.store.get_task(task_id))  # type: ignore[arg-type]


@task_app.command("list")
def task_list():
    for t in _store().list_tasks():
        stage = t.current_stage.name if t.current_stage else "-"
        typer.echo(f"{t.id:<12} [{t.status:^17}] {t.task_type:<8} 阶段={stage:<16} {t.title}")


@task_app.command("show")
def task_show(task_id: str, verbose: bool = typer.Option(False, "-v")):
    store = _store()
    t = store.get_task(task_id)
    if t is None:
        raise typer.Exit(1)
    _print_task(t)
    typer.echo("\n证据:")
    for e in store.list_evidence(task_id):
        typer.echo(f"  [{e['type']:<22}] {e['path']}  ({e['stage']}) {e['summary']}")
    typer.echo("\n执行记录:")
    for r in store.list_runs(task_id):
        ok = "✓" if r["success"] else "✗"
        typer.echo(f"  {ok} {r['stage']:<16} {r['backend_id']:<10} tier={r['tier']:<8} "
                   f"成本={r['cost']:g}  {r['summary']}")
        if verbose and r["trace"]:
            for line in json.loads(r["trace"]):
                typer.echo(f"      路由: {line}")
    approvals = store.list_approvals(task_id)
    if approvals:
        typer.echo("\n审批:")
        for a in approvals:
            typer.echo(f"  {a['stage']:<16} {a['decision']:<10} by {a['approver']} {a['note']}")


if __name__ == "__main__":
    app()
