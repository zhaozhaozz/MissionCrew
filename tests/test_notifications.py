"""浏览器"任务结束"通知:直接执行 notifications.js,覆盖偏好、文案与投递分流。"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NOTIFY_JS = ROOT / "missioncrew" / "web" / "js" / "notifications.js"
NODE = shutil.which("node")

# 最小浏览器桩:只提供投递路径用到的全局量,记录发出的页内提示与系统通知
BROWSER_STUBS = r"""
const store = {};
const localStorage = {
  getItem: key => (key in store ? store[key] : null),
  setItem: (key, value) => { store[key] = String(value); },
  removeItem: key => { delete store[key]; },
};
const sent = { toasts: [], system: [] };
const page = { visible: true, focused: true };
const document = {
  get visibilityState() { return page.visible ? "visible" : "hidden"; },
  hasFocus: () => page.focused,
};
const opened = [], routed = [];
const window = { isSecureContext: true, focus() {},
                 open: url => { opened.push(url); return {}; } };
const location = { pathname: "/" };
const history = { pushState: (state, title, url) => { location.pathname = url; } };
const applyRoute = async () => { routed.push(location.pathname); };
class Notification {
  constructor(title, options) { sent.system.push({ title, ...options }); }
  close() {}
}
Notification.permission = "granted";
const toast = (msg, type, ms, onClick, label) => sent.toasts.push({ msg, type, onClick, label });
const updateDocumentTitle = () => {};
let currentTab = "chat", currentChan = null, currentProject = "a";
const overview = { projects: [{ id: "a" }, { id: "b" }] };
"""


def evaluate(script: str):
    """桩 + 模块 + 用例拼成一个脚本(与浏览器经典脚本共享全局作用域一致)。"""
    if NODE is None:
        pytest.skip("node is required to execute the browser notification module")
    runner = r"""
const vm = require("vm");
const context = {};
vm.createContext(context);
vm.runInContext(require("fs").readFileSync(0, "utf8"), context);
process.stdout.write(JSON.stringify(context.result));
"""
    source = "\n".join([BROWSER_STUBS, NOTIFY_JS.read_text(encoding="utf-8"),
                        f"var result = (() => {{ {script} }})();"])
    completed = subprocess.run(
        [NODE, "-e", runner], input=source, text=True,
        capture_output=True, check=True, timeout=10,
    )
    return json.loads(completed.stdout)


def chain(project="a", channel="a:dev", root=1, status="done", **extra):
    return {"project_id": project, "project_name": project.upper(), "channel": channel,
            "channel_name": channel.split(":")[-1], "root_id": root, "status": status,
            "finished_at": 100.0 + root, "roles": ["dev"], "error": "",
            "reply": {"author": "dev", "content": "已修复"},
            "resource_url": f"/resources/{project}/channels/{channel.split(':')[-1]}",
            **extra}


def test_prefs_default_off_and_project_switch_turns_global_on():
    result = evaluate("""
const empty = normalizeNotifyPrefs('{bad');
const muted = normalizeNotifyPrefs({ enabled: true, projects: { a: false, b: true, c: 0 } });
const reopened = withProjectNotify(normalizeNotifyPrefs(null), 'a', true);
const off = withProjectNotify(muted, 'b', false);
return {
  empty, muted,
  mutedA: notifyEnabledFor(muted, 'a'), mutedB: notifyEnabledFor(muted, 'b'),
  noProject: notifyEnabledFor(muted, ''),
  reopened, off,
};
""")
    assert result["empty"] == {"enabled": False, "projects": {}}
    # 只记"关闭"的项目,其余跟随总开关
    assert result["muted"] == {"enabled": True, "projects": {"a": False}}
    assert (result["mutedA"], result["mutedB"], result["noProject"]) == (False, True, False)
    assert result["reopened"] == {"enabled": True, "projects": {}}
    assert result["off"] == {"enabled": True, "projects": {"a": False, "b": False}}


def test_notice_text_prefers_error_for_failed_chains_and_merges_bursts():
    item = chain()
    failed = chain(root=2, status="failed", error="exit 1")
    result = evaluate(f"""
return {{
  done: completionNotice({json.dumps(item)}),
  failed: completionNotice({json.dumps(failed)}),
  burst: noticesFor([1, 2, 3, 4].map(root => ({{ ...{json.dumps(failed)}, root_id: root }}))),
}};
""")
    assert result["done"]["title"] == "A · 任务完成 · #dev"
    assert (result["done"]["project"], result["done"]["headline"]) == ("A", "任务完成 · #dev")
    assert result["done"]["body"] == "@dev：已修复"
    assert result["done"]["url"] == "/resources/a/channels/dev"
    assert result["failed"]["title"] == "A · 任务失败 · #dev"
    assert result["failed"]["body"] == "执行出错：exit 1"
    [summary] = result["burst"]
    assert summary["title"] == "4 个任务已结束（4 个失败）"
    assert summary["body"] == "A #dev"


def test_delivery_skips_muted_stopped_and_watched_channel_then_uses_system_when_hidden():
    items = [chain(), chain(root=2, status="stopped"), chain(project="b", channel="b:ops", root=3),
             chain(channel="a:watched", root=4)]
    result = evaluate(f"""
notifyPrefs = normalizeNotifyPrefs({{ enabled: true, projects: {{ b: false }} }});
currentChan = 'a:watched';
deliverCompletions({json.dumps(items)});
const foreground = sent.toasts.map(t => [t.label, t.msg]);
page.visible = false; page.focused = false;
deliverCompletions([{json.dumps(chain(root=5))}]);
// 同一条链在另一个标签页已经提醒过:不重复
deliverCompletions([{json.dumps(chain(root=5))}]);
return {{ foreground, system: sent.system.map(n => n.title), unread: notifyUnread }};
""")
    # 页内提示:项目名走醒目标签,正文不再重复;系统通知:项目名在标题最前
    assert result["foreground"] == [["A", "任务完成 · #dev\n@dev：已修复"]]
    assert result["system"] == ["A · 任务完成 · #dev"]
    assert result["unread"] == 1


def test_hidden_page_without_system_notifications_buffers_until_focus():
    result = evaluate(f"""
notifyPrefs = normalizeNotifyPrefs({{ enabled: true }});
window.isSecureContext = false;     // 局域网 http:浏览器不给系统通知
page.visible = false; page.focused = false;
deliverCompletions([{json.dumps(chain())}]);
const whileHidden = {{ toasts: sent.toasts.length, unread: notifyUnread, title: notifyTitlePrefix() }};
page.visible = true; page.focused = true;
onNotifyPageFocus();
return {{ whileHidden, toasts: sent.toasts.map(t => t.msg), unread: notifyUnread }};
""")
    assert result["whileHidden"] == {"toasts": 0, "unread": 1, "title": "(1) "}
    assert result["toasts"] == ["任务完成 · #dev\n@dev：已修复"]
    assert result["unread"] == 0


def test_click_opens_other_project_in_new_tab_and_jumps_in_place_otherwise():
    items = [chain(), chain(project="b", channel="b:ops", root=2)]
    result = evaluate(f"""
notifyPrefs = normalizeNotifyPrefs({{ enabled: true }});
deliverCompletions({json.dumps(items)});
sent.toasts.forEach(t => t.onClick());
const normal = {{ opened: [...opened], routed: [...routed] }};
// 新标签页被拦截:退回原地跳转,不能点了没反应
window.open = () => null;
const blocked = openNoticeTarget('/resources/b/channels/ops', 'b');
// 还没选项目:无所谓"切走",原地跳转
currentProject = null;
const noProject = openNoticeTarget('/resources/b/channels/x', 'b');
return {{ normal, blocked, noProject, routed }};
""")
    assert result["normal"] == {"opened": ["/resources/b/channels/ops"],
                                "routed": ["/resources/a/channels/dev"]}
    assert (result["blocked"], result["noProject"]) == (False, False)
    assert result["routed"][1:] == ["/resources/b/channels/ops", "/resources/b/channels/x"]


def test_background_tab_leaves_notice_to_a_foreground_tab():
    """另一个标签页在前台时,后台标签页不发系统通知;那个标签页没认领就离开,再补发。"""
    watched, toasted, orphan = chain(root=1), chain(root=2), chain(root=3)
    result = evaluate(f"""
notifyPrefs = normalizeNotifyPrefs({{ enabled: true }});
const other = at => localStorage.setItem(NOTIFY_FOREGROUND_KEY,
  JSON.stringify({{ id: 'other-tab', at, since: 50 }}));
page.visible = false; page.focused = false;
other(Date.now());
deliverCompletions({json.dumps([watched, toasted, orphan])});
const whileOtherFocused = {{ system: sent.system.length, unread: notifyUnread,
                            deferred: notifyDeferred.length }};
// 前台标签页处理了前两条(页内提示,或正盯着那个频道):都会认领
for (const item of {json.dumps([watched, toasted])})
  claimNotice(`${{item.channel}}#${{item.root_id}}#${{item.finished_at}}`);
// 第三条还没认领,那个标签页就离开了前台:心跳被清掉,由本页补发系统通知
localStorage.removeItem(NOTIFY_FOREGROUND_KEY);
deliverCompletions([]);
const afterOtherLeft = {{ system: sent.system.map(n => n.tag), unread: notifyUnread,
                         deferred: notifyDeferred.length }};
// 心跳过期(标签页崩溃没来得及清)同样不再拦;它领游标之前结束的链它拉不到,也不拦
other(Date.now() - NOTIFY_FOREGROUND_TTL_MS - 1);
deliverCompletions([{json.dumps(chain(root=4))}]);
other(Date.now());
deliverCompletions([{json.dumps(chain(root=5, finished_at=40.0))}]);
return {{ whileOtherFocused, afterOtherLeft, system: sent.system.length }};
""")
    assert result["whileOtherFocused"] == {"system": 0, "unread": 0, "deferred": 3}
    assert result["afterOtherLeft"] == {
        "system": ["mc-chain-a:dev-3"], "unread": 1, "deferred": 0}
    assert result["system"] == 3


def test_foreground_tab_heartbeat_and_watched_channel_claim():
    result = evaluate(f"""
notifyPrefs = normalizeNotifyPrefs({{ enabled: true }});
markForeground();
const beforeCursor = readForeground();      // 还没领到游标:不登记
notifySince = 60;
markForeground();
const mark = readForeground();
// 正盯着这个频道:不提示,但认领掉,后台标签页之后也不会再发系统通知
currentChan = 'a:dev';
deliverCompletions([{json.dumps(chain())}]);
page.focused = false;                       // 整个浏览器退到后台,标签页仍可见
markForeground();
const afterBlur = readForeground();
deliverCompletions([{json.dumps(chain())}]);
return {{ beforeCursor, own: mark.id === notifyTabId, since: mark.since, afterBlur,
         toasts: sent.toasts.length, system: sent.system.length }};
""")
    assert result == {"beforeCursor": None, "own": True, "since": 60, "afterBlur": None,
                      "toasts": 0, "system": 0}
