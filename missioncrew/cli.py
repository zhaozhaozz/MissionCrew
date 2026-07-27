"""mc 命令行:统一任务入口的终端形态。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import typer
import yaml

from .runtime import runtime_manager
from .core import seed as seed_mod
from .collab.chat import ChatEngine
from .collab.recycle_bin import recycle_task
from .collab.tasks import add_task_brief, create_task, dispatch_task
from .core.config import db_path, mc_home
from .collab.documents import library_for
from .collab.skills import (materialize_project_skills,
                            sync_all_project_skill_libraries,
                            sync_project_skill_library)
from .collab.workspace import migrate_legacy_workspace_layout
from .core.models import Backend, Channel, Project, Role, Task
from .core.store import Store

app = typer.Typer(help="MissionCrew:Channel 驱动的多 Agent 协作平台(纯本地)")
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

DEFAULT_SERVE_HOST = "0.0.0.0"
DEFAULT_SERVE_PORT = 8321


def _store() -> Store:
    store = Store(db_path())
    runtime_manager.bind_usage_store(store)
    seed_mod.ensure_role_bindings(store)
    seed_mod.ensure_role_templates(store)
    migrated_paths = migrate_legacy_workspace_layout()
    if migrated_paths:
        store.audit("platform", "agent_workspace_layout_migrated",
                    detail=f"paths={migrated_paths}")
    sync_all_project_skill_libraries(store)
    return store


def _fmt_ts(ts: float) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


def _print_task(t: Task) -> None:
    typer.echo(f"[{t.id}] {t.title}")
    typer.echo(f"  项目={t.project_id} 状态={t.status}")
    typer.echo(f"  频道={', '.join(t.channel_ids)} 标签={', '.join(t.labels) or '-'}")
    typer.echo(f"  简介={t.summary or '-'}")
    if t.body:
        typer.echo(f"\n{t.body}")


# ---------------- 平台 ----------------

@app.command()
def demo(run: bool = typer.Option(True, help="是否顺带演示 Channel 派发")):
    """写入演示数据，并演示 Task 经 Channel 交给主控。"""
    store = _store()
    seed_mod.seed(store)
    typer.echo(f"演示数据已写入 {mc_home()}")
    typer.echo("后端: " + ", ".join(f"{b.id}({b.tier})" for b in store.list_backends()))
    typer.echo("角色: " + ", ".join(f"@{r.id}" for r in store.list_roles()))
    if not run:
        return

    chat = ChatEngine(store)
    task = create_task(
        store, "webshop", title="购物车优惠券叠加金额错误",
        summary="多张优惠券叠加时合计金额偏大",
        body="请复现、修复并补充覆盖叠加边界条件的测试。",
        labels=["bug"], channel_ids=[],
    )
    sent, _ = dispatch_task(store, chat, task)
    typer.echo(f"\nTask {task.id} 已发送到 {len(sent)} 个 Channel 的项目主控。")
    chat.wait_idle()
    for m in store.list_messages(task.channel_ids[0]):
        who = f"@{m['author']}" if m["author_type"] == "agent" else m["author"]
        typer.echo(f"  [{who}] {m['content'].splitlines()[0]}")
    typer.echo(
        f"\n打开看板与聊天: mc serve  ->  http://<本机IP>:{DEFAULT_SERVE_PORT}"
    )
    typer.echo("接入真实本地 Agent: mc backend detect")


@app.command()
def serve(host: str = DEFAULT_SERVE_HOST, port: int = DEFAULT_SERVE_PORT):
    """启动 Web 服务(REST API + 看板)。"""
    import uvicorn
    from .api import create_app
    # Runtime 总是经本机回环访问 Agent Tool API；监听地址可继续面向所有网卡。
    os.environ["MISSIONCREW_AGENT_TOOL_URL"] = (
        f"http://127.0.0.1:{port}/api/agent/v1")
    typer.echo(f"MissionCrew 看板: http://{host}:{port}")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


@app.command()
def audit(task_id: Optional[str] = typer.Argument(None), limit: int = 50):
    """查看审计日志(平台每个决策都可追溯)。"""
    for row in reversed(_store().list_audit(task_id, limit)):
        typer.echo(f"{_fmt_ts(row['ts'])} [{row['actor']}] {row['action']} "
                   f"{row['task_id']} {row['detail']}")


# ---------------- 项目 ----------------

@project_app.command("add")
def project_add(file: Path = typer.Option(..., help="项目定义 YAML 文件")):
    """从 YAML 导入/更新项目（含项目准则）。"""
    data = yaml.safe_load(file.read_text())
    p = Project.from_dict(data)
    store = _store()
    is_new = store.get_project(p.id) is None
    new_roles = None
    if is_new:
        try:
            new_roles = seed_mod.project_roles_from_templates(store, p.id)
        except RuntimeError as exc:
            raise typer.BadParameter(str(exc))
        p.orchestrator_role_id = data.get("orchestrator_role_id") or new_roles[0].id
    if not is_new and store.get_role(p.id, p.orchestrator_role_id) is None:
        raise typer.BadParameter(f"主控角色不属于当前项目: @{p.orchestrator_role_id}")
    if is_new and p.orchestrator_role_id not in {role.id for role in new_roles}:
        raise typer.BadParameter(f"主控角色不属于全局角色模板: @{p.orchestrator_role_id}")
    store.put_project(p)
    materialize_project_skills(p, overwrite=True)
    sync_project_skill_library(store, p)
    if is_new:
        seed_mod.init_project(store, p.id, new_roles)
        library_for(p.id)
    typer.echo(f"项目已保存: {p.id}")


@project_app.command("list")
def project_list():
    for p in _store().list_projects():
        typer.echo(f"{p.id:<16} {p.name}  准则 {len(p.guidelines)} 篇")


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
    seed_mod.ensure_role_templates(store)


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
    report = runtime_manager.detect_report()
    for item in report:
        if item["installed"]:
            typer.echo(f"  ✓ {item['binary']:<14} {item['version'] or '?':<12} {item['path']}")
        else:
            typer.echo(f"  - {item['binary']:<14} 未安装")
    found = runtime_manager.detect_backends(report)
    if not found:
        raise typer.Exit(1)
    if register:
        store = _store()
        for b in found:
            existing = store.get_backend(b.id)
            if existing is None:
                store.put_backend(b)
            else:  # 刷新检测信息与工具自带模型清单,保留用户的启停/配额调整
                existing.binary_path, existing.version = b.binary_path, b.version
                existing.models = b.models
                store.put_backend(existing)
        seed_mod.ensure_role_templates(store)
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
def chat_send(content: str = typer.Argument(..., help="消息内容；@[角色] 显式触发执行(人类 CLI 专用语法)"),
              channel: str = typer.Option("general", "-c", "--channel"),
              project: Optional[str] = typer.Option(None, "-p", "--project"),
              author: str = typer.Option("human", "--author"),
              wait: bool = typer.Option(True, help="等待所有触发的执行(含级联)结束")):
    """向频道发消息；@[角色] 会执行工作，普通 @角色 只是正文。

    方括号语法仅对人类 CLI 消息有效;Agent 派发一律走 message.publish 显式命令。
    """
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
    """查看频道聊天记录(含主控调度与执行角色回传)。"""
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
        known_models = set(backend.models)
        if known_models and r.model not in known_models:
            raise typer.BadParameter(f"@{r.id} 的模型不属于 runtime {r.runtime_id}")
        if r.effort and r.effort not in runtime_manager.effort_options(backend):
            raise typer.BadParameter(
                f"@{r.id} 的 effort={r.effort} 不受 runtime {r.runtime_id} 支持")
        store.put_role(r)
        typer.echo(f"角色已保存: [{r.project_id}] @{r.id}")


# ---------------- 任务 ----------------

@task_app.command("create")
def task_create(project: str = typer.Option(..., "-p", "--project"),
                title: str = typer.Option(..., "--title"),
                summary: str = typer.Option("", "-s", "--summary"),
                body: str = typer.Option("", "-b", "--body"),
                label: list[str] = typer.Option([], "-l", "--label"),
                channel: list[str] = typer.Option([], "-c", "--channel"),
                status: str = typer.Option("open", help="open|in_progress|blocked|done"),
                process: bool = typer.Option(False, help="创建后发送给绑定 Channel 的主控")):
    store = _store()
    task = create_task(
        store, project, title=title, summary=summary, body=body,
        labels=list(label), channel_ids=list(channel), status=status,
    )
    typer.echo(f"任务已创建: {task.id}")
    if process:
        sent, _ = dispatch_task(store, ChatEngine(store), task)
        typer.echo(f"已派发到 {len(sent)} 个 Channel")
    _print_task(task)


@task_app.command("process")
def task_process(task_id: str,
                 message: str = typer.Option("", "-m", "--message")):
    """在每个绑定 Channel 中通知项目主控处理 Task。"""
    store = _store()
    task = store.get_task(task_id)
    if task is None:
        raise typer.BadParameter("任务不存在")
    sent, _ = dispatch_task(store, ChatEngine(store), task, message=message)
    typer.echo(f"已派发到 {len(sent)} 个 Channel")


@task_app.command("brief")
def task_brief(task_id: str, content: str = typer.Option(..., "-m", "--message"),
               status: Optional[str] = typer.Option(None)):
    """追加状态简报，并可同时更新 Task 状态。"""
    store = _store()
    task = store.get_task(task_id)
    if task is None:
        raise typer.BadParameter("任务不存在")
    add_task_brief(store, task, content=content, status=status)
    typer.echo("状态简报已追加")


@task_app.command("delete")
def task_delete(
        task_id: str,
        yes: bool = typer.Option(False, "-y", "--yes", help="跳过删除确认")):
    """把 Task 及其状态简报移入项目回收站。"""
    store = _store()
    task = store.get_task(task_id)
    if task is None:
        raise typer.BadParameter("任务不存在")
    project = store.get_project(task.project_id)
    if project is None:
        raise typer.BadParameter("项目不存在")
    if not yes and not typer.confirm(f"将 Task「{task.title}」移入项目回收站？"):
        typer.echo("已取消")
        return
    item = recycle_task(store, project, task, actor="human")
    typer.echo(f"Task 已移入回收站: {item['id']}")


@task_app.command("list")
def task_list():
    for t in _store().list_tasks():
        typer.echo(f"{t.id:<12} [{t.status:^12}] {t.title}  "
                   f"channels={','.join(t.channel_ids)}")


@task_app.command("show")
def task_show(task_id: str):
    store = _store()
    t = store.get_task(task_id)
    if t is None:
        raise typer.Exit(1)
    _print_task(t)
    typer.echo("\n状态简报:")
    for brief in reversed(store.list_task_briefs(task_id)):
        typer.echo(f"  {_fmt_ts(brief['created_at'])} [{brief['status']}] "
                   f"{brief['author']}: {brief['content']}")


if __name__ == "__main__":
    app()
