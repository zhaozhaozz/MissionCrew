# MissionCrew 文档

English version: [en/README.md](en/README.md)

本目录是 MissionCrew 的详细文档，读者既包括部署、使用和开发 MissionCrew 的人，也包括在 MissionCrew 里执行任务的 Agent。每篇文档都有中英两个版本：中文版在本目录，同名英文版在 [en/](en/README.md)。平台把英文版原样复制到每个 Agent 工作区的 `platform-docs/`，Agent 需要了解平台机制时按其中的索引检索。快速上手与整体介绍见仓库根目录的 [README](https://github.com/zhaozhaozz/MissionCrew#readme)。

## 使用平台

- [定时自动化与任务自动处理](automation.md)：自动化脚本的运行环境（注入的环境变量、跨次保留的工作目录、调用平台动作的方式）、webhook 触发（全局 webhook 来源与订阅、专用端口、验签方式、事件文件与排队）、GitHub 轮询来源（没有公网地址时用 `GITHUB_TOKEN` 定时查询仓库事件与账号通知：人建来源、脚本用 `github_repos` 自己订阅仓库，轮询间隔、事件形状、与 webhook 的差别、延迟）、`board_source.save` 把外部列表同步成任务时的字段格式与合并规则、任务自动处理规则的配置位置与触发时机，以及同步 GitHub/GitCode Issue 的完整示例。写同步脚本或 webhook 脚本、排查同步结果或自动派发时读。
- [Agent Tool API](agent-tool-api.md)：Agent 和脚本修改平台状态的统一动作接口，包括 CLI 用法、令牌与权限、各动作的参数和返回值、`version_conflict` 等错误码。调用动作前不确定参数或遇到错误时读。
- [harness 工作区边界](agent-harness-workspace.md)：Agent 工作区 `.missioncrew/` 里每个目录的读写语义、任务快照的字段与刷新时机、凭据文件的读取方式与脱敏规则、GitHub 身份（`GITHUB_TOKEN` 凭据怎样变成 `gh` 与 git 的登录状态，classic 与 fine-grained PAT 的差别）、注入的环境变量，以及工作区与业务代码仓的边界。不确定某个目录能不能写、快照何时更新、怎样使用凭据或以哪个账号操作 GitHub 时读。
- [资源与 URL 约定](resources.md)：频道、任务、文档、准则、Skill、面板等资源的公开 URL 格式和生命周期。在回复里引用平台资源时读。
- [项目 Skill 目录](skills.md)：完整目录形态的项目 Skill 怎样投放、导入、启停和做版本管理。创建或整理 Skill 包时读。

## 运维与开发

- [命令行工具](cli.md)：`mc` 子命令速查，与 Web、API 走同一套业务校验，适合脚本化和无头环境。
- [Runtime 接入](runtimes.md)：支持哪些本地 Agent CLI、如何检测和升级、模型清单从哪来，以及接入新工具的步骤。
