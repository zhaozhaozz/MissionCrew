"""GitHub 身份:项目的 `GITHUB_TOKEN` 凭据即它在 GitHub 上的身份。

名为 `GITHUB_TOKEN` 的凭据(项目凭据覆盖全局)保存的是个人访问令牌(PAT,classic 或
fine-grained 均可)。有了它,平台做三件事:

1. 登录状态:在项目的 `runtime-context/github-cli/` 写一份 gh 配置(`hosts.yml`),
   派发 Agent 与运行自动化脚本时用 `GH_CONFIG_DIR` 指过去,并经 `GIT_CONFIG_*` 给
   github.com 的 HTTPS 远端配一个读凭据文件的 git credential helper。Agent 直接运行
   `gh`、`git push` 就是这个身份,效果等同本机 `gh auth login` + `gh auth setup-git`。
   两者都在每次调用时读文件,替换 token 后长驻会话的下一条命令即用新值。
2. 身份核对:用 token 调 `GET /user`,给设置页显示账号、token 类型、权限与到期时间。
3. 事件轮询:github_events.py 用同一个 token 查询通知与仓库事件。

没有 `GITHUB_TOKEN` 凭据时什么都不注入,`gh` 与 git 沿用启动服务的用户自己的登录状态。
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import urlencode

from ..core.config import projects_dir
from ..core.store import Store

GITHUB_TOKEN_NAME = "GITHUB_TOKEN"
API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
HOSTS_FILE = "hosts.yml"
# git 的 HTTPS 凭据按主机匹配;gist 是独立主机,与 `gh auth setup-git` 的范围一致
GIT_HOSTS = ("github.com", "gist.github.com")
# 宿主环境里的这两个变量优先于 gh 配置文件;项目有自己的身份时置空,让配置生效
HOST_TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN")
IDENTITY_TTL_SECONDS = 600
IDENTITY_ERROR_TTL_SECONDS = 60
# (前缀, 类型);各前缀互不包含,顺序无关
TOKEN_PREFIXES = (
    ("github_pat_", "fine_grained"),   # 细粒度 PAT:按仓库与权限项授权,没有 scope
    ("ghp_", "classic"),               # classic PAT:按 scope 授权
    ("gho_", "oauth"),                 # OAuth 应用令牌(如 gh auth login 签发),同样按 scope
    ("ghu_", "app_user"),              # GitHub App 用户令牌
    ("ghs_", "app_installation"),      # GitHub App 安装令牌
)
# 按 scope 授权的令牌才能用通知接口(细粒度 PAT 与 App 令牌都不行)
SCOPED_TOKEN_KINDS = frozenset({"classic", "oauth"})
NOTIFICATION_SCOPES = frozenset({"notifications", "repo"})


class GitHubError(Exception):
    """访问 GitHub 失败:网络不通、超时或响应不是预期的 JSON。"""


@dataclass
class GitHubResponse:
    status: int
    headers: dict[str, str]        # 键已转小写
    body: bytes = b""

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8")) if self.body.strip() else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubError("GitHub 返回的不是 JSON") from exc

    def message(self) -> str:
        """错误响应里 GitHub 给的原因;取不到时退回状态码。"""
        try:
            data = self.json()
        except GitHubError:
            data = None
        text = data.get("message") if isinstance(data, dict) else ""
        return str(text or f"HTTP {self.status}")


# (method, url, headers, timeout) -> 响应;测试注入假实现,不出网
Transport = Callable[[str, str, dict, float], GitHubResponse]


def http_transport(method: str, url: str, headers: dict, timeout: float) -> GitHubResponse:
    """标准库实现:4xx/5xx 与 304 同样作为响应返回,只有连不上才抛 GitHubError。"""
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return GitHubResponse(
                response.status,
                {key.lower(): value for key, value in response.headers.items()},
                response.read())
    except urllib.error.HTTPError as exc:
        return GitHubResponse(
            exc.code, {key.lower(): value for key, value in exc.headers.items()},
            exc.read() or b"")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise GitHubError(f"无法连接 GitHub: {reason}") from exc


def github_request(token: str, path: str, *, params: Optional[dict] = None,
                   headers: Optional[dict] = None, method: str = "GET",
                   transport: Optional[Transport] = None,
                   timeout: float = 20.0) -> GitHubResponse:
    """带身份调一次 GitHub REST API;path 可以是 `/user` 这样的路径或完整 URL。"""
    url = path if path.startswith("https://") else API_ROOT + path
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    merged = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "MissionCrew",
        **(headers or {}),
    }
    return (transport or http_transport)(method, url, merged, timeout)


def token_kind(token: str) -> str:
    for prefix, kind in TOKEN_PREFIXES:
        if token.startswith(prefix):
            return kind
    # 2021 年以前签发的 classic PAT 是 40 位十六进制,没有前缀
    if len(token) == 40 and all(char in "0123456789abcdef" for char in token):
        return "classic"
    return "unknown"


def inspect_token(token: str, transport: Optional[Transport] = None) -> dict:
    """调 `GET /user` 核对 token:它是谁、哪种令牌、有哪些 scope、何时到期。

    scope 只有按 scope 授权的令牌(classic PAT、OAuth)才有,来自响应头
    `X-OAuth-Scopes`;细粒度 PAT 的权限项查不到,scopes 为 None。notifications
    表示通知接口可用:True/False 是确定结论,None 表示无法从 token 本身判断。
    """
    kind = token_kind(token)
    result = {"ok": False, "kind": kind, "login": "", "name": "", "url": "",
              "scopes": None, "expires_at": "", "notifications": None,
              "error": "", "checked_at": time.time()}
    try:
        response = github_request(token, "/user", transport=transport)
        data = response.json() if response.status == 200 else None
    except GitHubError as exc:
        result["error"] = str(exc)
        return result
    if response.status == 401:
        result["error"] = "token 无效或已过期(GitHub 返回 401)"
        return result
    if response.status != 200 or not isinstance(data, dict):
        result["error"] = f"GitHub 拒绝了这个 token: {response.message()}"
        return result
    result.update(ok=True, login=str(data.get("login") or ""),
                  name=str(data.get("name") or ""), url=str(data.get("html_url") or ""),
                  expires_at=response.headers.get(
                      "github-authentication-token-expiration", ""))
    if "x-oauth-scopes" in response.headers:
        result["scopes"] = [item.strip() for item
                            in response.headers["x-oauth-scopes"].split(",") if item.strip()]
    if kind in SCOPED_TOKEN_KINDS and result["scopes"] is not None:
        result["notifications"] = bool(NOTIFICATION_SCOPES & set(result["scopes"]))
    elif kind in ("fine_grained", "app_user", "app_installation"):
        result["notifications"] = False
    return result


_IDENTITY_CACHE: dict[str, dict] = {}
_IDENTITY_GUARD = threading.Lock()


def cached_identity(token: str, *, refresh: bool = False,
                    transport: Optional[Transport] = None) -> dict:
    """inspect_token 的进程内缓存:设置页反复打开不必每次都出网。缓存键是 token 的
    哈希,token 本身不留在缓存里;失败结果只缓存很短时间。"""
    key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    with _IDENTITY_GUARD:
        cached = _IDENTITY_CACHE.get(key)
    if cached and not refresh:
        ttl = IDENTITY_TTL_SECONDS if cached["ok"] else IDENTITY_ERROR_TTL_SECONDS
        if now - cached["checked_at"] < ttl:
            return cached
    result = inspect_token(token, transport)
    with _IDENTITY_GUARD:
        _IDENTITY_CACHE[key] = result
    return result


def github_token(store: Store, project_id: str) -> tuple[str, str]:
    """项目生效的 GitHub token 及其来源:("", "") 表示没配;来源是 "project" 或
    "global"。project_id 为空时只看全局凭据。"""
    if project_id:
        value = store.credential_value(project_id, GITHUB_TOKEN_NAME)
        if value:
            return value, "project"
    value = store.credential_value("", GITHUB_TOKEN_NAME)
    return (value, "global") if value else ("", "")


def github_status(store: Store, project_id: str, *, refresh: bool = False,
                  transport: Optional[Transport] = None) -> dict:
    """设置页用的身份摘要:是否已配置、来自哪一层、核对结果。不含 token。"""
    token, scope = github_token(store, project_id)
    if not token:
        return {"configured": False, "scope": "", "identity": None}
    return {"configured": True, "scope": scope,
            "identity": cached_identity(token, refresh=refresh, transport=transport)}


def github_cli_dir(project_id: str) -> Path:
    return (projects_dir() / project_id / "runtime-context" / "github-cli").resolve()


def sync_github_cli_config(project_id: str, token: str,
                           write_private: Callable[[Path, str], None]) -> None:
    """按项目当前生效的 token 重写 gh 配置目录;没有 token 时删掉整个目录。

    由 write_credential_context 在同一把锁里调用,凭据目录与 gh 配置始终一致。
    hosts.yml 只写 token 与协议两项:gh 读到 `oauth_token` 即视为已登录,只读取
    不改写这个文件。
    """
    directory = github_cli_dir(project_id)
    if not token:
        shutil.rmtree(directory, ignore_errors=True)
        return
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    # JSON 字符串同时是合法的 YAML 双引号标量,不必担心 token 里的特殊字符
    write_private(directory / HOSTS_FILE,
                  f"github.com:\n    oauth_token: {json.dumps(token)}\n"
                  "    git_protocol: https\n")


def remove_github_cli_config(project_id: str) -> None:
    shutil.rmtree(github_cli_dir(project_id), ignore_errors=True)


def github_env(project_id: str, credentials_dir: Path,
               base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """让 gh 与 git 使用项目 GitHub 身份的环境变量;项目没有 token 时返回空字典。

    调用前须已执行 write_credential_context(装配工作区与运行脚本时都会),这里
    只看 gh 配置是否存在。git 的 credential helper 在每次需要凭据时读凭据文件:
    先写一个空值清掉 github.com 上已有的 helper(宿主的 store/cache/manager),再
    追加自己的,所以不会落回宿主账号。环境变量形式的 git 配置在所有配置文件之后
    生效,且不改动任何仓库或用户的配置文件。
    """
    directory = github_cli_dir(project_id)
    if not (directory / HOSTS_FILE).is_file():
        return {}
    base = os.environ if base is None else base
    token_file = shlex.quote(str(Path(credentials_dir) / GITHUB_TOKEN_NAME))
    # PAT 走 HTTPS 时用户名任意,x-access-token 是 GitHub 文档里的惯用写法
    helper = ("!f() { test \"$1\" = get && printf 'username=x-access-token\\npassword=%s\\n' "
              f"\"$(cat {token_file})\"; }}; f")
    try:
        count = max(0, int(base.get("GIT_CONFIG_COUNT", "0") or 0))
    except ValueError:
        count = 0
    env = {"GH_CONFIG_DIR": str(directory)}
    for host in GIT_HOSTS:
        for value in ("", helper):
            env[f"GIT_CONFIG_KEY_{count}"] = f"credential.https://{host}.helper"
            env[f"GIT_CONFIG_VALUE_{count}"] = value
            count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    for name in HOST_TOKEN_ENV:
        if base.get(name):
            env[name] = ""
    return env
