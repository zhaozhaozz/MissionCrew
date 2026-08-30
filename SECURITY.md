# 安全说明

## 威胁模型

MissionCrew 是一个运行在开发者本机上的多 Agent 协作平台，设计前提是**只有机器的主人在使用它**：

- Web 页面和 REST API **没有任何身份验证或授权**。
- API 可以浏览本机目录（`/api/fs/dirs`）、把本地代码仓绑定给频道、调度本机安装的 Agent CLI 在这些目录里执行命令、运行用户定义的自动化脚本。
- 因此，能访问服务端口的人，就等同于能以运行服务的用户身份在这台机器上执行命令。

基于这个前提：

- `mc serve` 默认只监听 `127.0.0.1`。只有在可信的局域网中、并且清楚上述后果时，才用 `--host 0.0.0.0`（或环境变量 `MISSIONCREW_HOST`）开放访问。
- 不要把服务暴露到公网，也不要放在反向代理后面而不加身份验证。
- 平台数据目录 `.missioncrew/` 里含 SQLite 数据库、Agent 令牌、自定义模型接入的 API Key（`pi/agent/models.json`，权限 0600）和 Runtime 会话文件，不要提交到代码仓或共享给他人。

## Agent 执行边界

- 每个 Channel×角色的执行只授权 Prompt 中列出的目录；派发层会从子进程环境剥离宿主终端注入的变量（`VSCODE_*`、`CLAUDE_*`、`SSH_AUTH_SOCK` 等）。这些是隔离措施，不是沙箱保证——最终由各 Agent CLI 自身的权限模式决定它能做什么。
- Agent Tool 令牌按 `project × channel × role × run` 签发，Run 结束即撤销；SQLite 只保存哈希。
- 自动化脚本以运行服务的用户身份执行任意 shell，脚本内容应视为可信代码。

## 报告漏洞

请通过 GitHub 的私密漏洞报告（Security Advisories）提交：<https://github.com/zhaozhaozz/MissionCrew/security/advisories/new>。请不要在公开 Issue 中披露未修复的漏洞。收到报告后会尽快确认并给出处理计划。
