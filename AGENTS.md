# MissionCrew 项目约定

## 代码结构

```
missioncrew/
├── cli.py            Typer CLI(角色/任务/运行管理,与 API 同一套校验口径)
├── agent_tool.py     Runtime 内可调用的 Agent Tool 客户端(权限校验在服务端)
├── core/             领域模型与持久化:models.py(dataclass 领域对象)、
│                     store.py(SQLite,领域对象存 JSON)、config.py、seed.py
├── collab/           业务引擎:chat.py(聊天协作/主控派发)、workspace.py
│                     (Agent 工作区装配)、manual.py(写进工作区的手册,Prompt
│                     只留必守规则)、platform_docs.py(把 docs/ 复制给 Agent 检索)、
│                     documents/tasks/skills/guidelines 等;github.py 是 GitHub 身份
│                     (`GITHUB` 环境的 `TOKEN` → gh 配置目录与 git credential helper、
│                     token 核对、REST 客户端),github_events.py 是 GitHub 轮询来源
│                     (用该 token 查询仓库事件与通知,整理成 webhook 同形的投递);
│                     callbacks.py 是角色
│                     登记的一次性回调(到点/频道或 Task 有更新/webhook 来源收到匹配
│                     事件时轮询触发,回到原频道启动原角色),event_filters.py 在沙箱里
│                     运行事件回调自带的过滤脚本
├── runtime/          Runtime 统一抽象层(业务层只使用 runtime_manager)
│   ├── base.py       RuntimeProvider 契约、host_isolated_environ()
│   ├── manager.py    RuntimeManager 控制面 + _BuiltinProvider;唯一对外入口
│   ├── native.py     原生 provider 共享的 JSONL 进程、安全 emit 与 usage/v1 统一结构
│   ├── claude.py     Claude 原生 provider(stream-json 双向协议)
│   ├── codex.py      Codex 原生 provider(app-server thread/turn)
│   ├── pi.py         pi 原生 provider(RPC;自定义 API 模型的执行后端)
│   ├── antigravity.py Antigravity 原生 provider(headless stream-json;逐轮续接 conversation)
│   ├── adapters.py   通用执行器 CliAdapter/AcpAdapter + 注册表汇总与分发
│   ├── acp.py        ACP stdio 协议客户端(JSON-RPC,长驻会话池)
│   ├── usage.py      账户限额探测实现(grok/kimi/claude/antigravity)
│   └── clis/         按工具的声明模块(一个工具一个文件):CliSpec 描述
│                     检测、命令模板、能力、模型、effort、升级渠道与行为钩子
├── api/              FastAPI 路由(按资源分文件);context.py 持缓存与共享状态,
│                     schemas.py 请求模型,spa.py 静态页兜底(必须最后注册);
│                     webhooks.py 是专用 webhook 端口(独立 Starlette 应用,验签与
│                     入队逻辑在 collab/webhooks.py)
└── web/              无构建步骤的前端:index.html + js/(按页面分文件;run-events.js
                      是聊天与配置聊天共用的运行卡片渲染,须先于两者加载;boards.js 的
                      任务看板渲染器同时服务内置任务看板与自定义任务看板,两者只差
                      绑定(数据端点/配置保存),个人视图状态(筛选/分列/折叠,存浏览器)
                      的纯数据转换在 taskboard-view.js,须先于 boards.js 加载;
                      notifications.js 是任务结束的浏览器通知(偏好存浏览器,轮询
                      /api/chat/runs/completions 的整条协作链结束增量);
                      task-rules.js 是项目级自动处理规则的列表页与编辑器(看板 ⚡ 共用),
                      条件与标签表达式互转的纯函数在 rule-condition.js;
                      tasks.js 只留任务详情与配置持久化;markdown.js
                      是共用的 Markdown 渲染,diagrams.js 按需加载 Mermaid 渲染其中的
                      图表)+ css/ + vendor/(第三方浏览器库原样落库,不走 CDN)
tests/                pytest;fake_acp_agent.py 是 ACP 协议假服务端
docs/                 详细文档,中文版在 docs/、英文版在 docs/en/(同名,Agent 读英文版),
                      入口各自的 README.md;runtimes.md 是 Runtime 层设计(会话恢复/
                      权限/effort/升级)
```

分层规则(有测试钉住):

- 业务层(api/collab/core)不得直接 import `runtime.adapters` 或原生执行器,只经 `runtime_manager`;见 `test_application_layers_do_not_import_raw_runtime_executors`。
- 工具相关知识进 `runtime/clis/<tool>.py`:静态事实写 `CliSpec` 字段,工具特有行为写可选钩子(`session_args`、`apply_permissions`、`prepare_env`、`parse_model_efforts`、`parse_prompt_usage`、`locate_binary`、`update_plan`、`latest_version`、`account_usage_probe` 等);`adapters.py` 只做汇总与分发,新增工具 = 加声明模块 + 追加进 `clis.SPECS`,不动执行器。
- claude/codex/pi/antigravity 走各自原生 provider 类,执行与 effort 档位声明都在 provider 里;`clis/` 里对应文件只负责检测、模型发现、回退模板与升级渠道。
- 钩子体内如需 adapters 工具函数,用函数内延迟导入,避免 clis↔adapters 环形依赖。
- 过程事件 `usage` 由各 provider 在源头用自己的字段表映射成 `native.build_usage` 的 `usage/v1`(turn/total 区段 + context_window/context_used/cost_usd/tool_uses/duration_ms,raw 保留上报);前端只按这一结构排版,不得出现工具私有字段名(有测试钉住),新增 Runtime 只改 provider;ACP 工具的标准 usage/usage_update 由 `acp.py` 归一,厂商私有用量走 `parse_prompt_usage` 钩子。
- 数据目录(`MISSIONCREW_HOME`,默认 `~/.missioncrew`)必须可整体搬迁:平台生成的目录链接一律经 `relative_link_target` 写相对路径;声明了 `locate_binary` 的工具视为平台托管安装(如 vendored pi),启动时按当前数据目录重新定位 `binary_path`,不要把数据目录内的绝对路径写进库或文件。
- 主控权限只经 `Project.controls_platform(role_id)` / `has_orchestrator` 判定,派发权限只经 `Project.can_dispatch(role_id)`(能否派发任意角色)/ `Project.can_mention(role_id, target_id, cross_channel)`(有主控时执行角色只能提及主控;有无主控,角色都可在其他频道提及自己),不要直接比较 `orchestrator_role_id`:该字段为空即无主控模式(所有角色都有主控级管理权限,但不能互相派发,除在其他频道提及自己外只由人类点名启动,不点名的人类消息拒收,多选各自启动,结果只留在频道);新增"仅主控"的动作、API 校验或 Prompt 分支都要走这些入口。
- 聊天运行按 `channel × role` 串行,经执行位调度:排队运行只登记在 `ChatEngine._waiting`,不占执行池线程;`_dispatch_locked` 按最早排队先后把执行位分给 channel×role(全局 `_max_workers` + 角色 `Role.max_concurrency` 跨频道计数),不要在执行池线程里阻塞等待别的运行。拿到执行位的 `_run_slot` 经 `ChatEngine._claim_queued_runs` 把该角色此刻排队的运行合并成一轮,选一条承接运行实际执行,其余标成终态 `merged`(`merged_into` 指向承接运行)。线程与运行不一一对应,执行路径只用承接运行的 id/触发消息,不要用提交线程时的参数;判断运行是否结束时 `merged` 与 `done`/`failed`/`stopped` 同为终态。
- 给角色的提示词(公共上下文、本轮输入、注入横幅、`manual.md`、工作区 README、Agent Tool 动作说明与 `resume`、页面对话 instructions)一律用英文;人类可见内容的语言只由身份段的 `Language` 一行约定为简体中文。平台发到频道的通知、错误信息和工具回执 summary 面向人类,保持中文。改提示词标题时同步 `MockAdapter` 的 `_role_from_prompt`/`_trigger_from_prompt`,它们按 `You are role @`、`# Trigger Message` 解析。
- 平台机制的完整说明只写在 `docs/`(人与 Agent 共用一份):`docs/*.md` 是中文版,`docs/en/` 下是同名英文版,两者内容保持一致、同一提交里一起改(有测试钉住文件一一对应)。`platform_docs.py` 把 `docs/en/*.md` 复制到数据目录,经工作区 `platform-docs/` 交给 Agent 按需检索。`manual.md` 与 Prompt 只写 Agent 的行为规则和关键约束,细节指向具体文档,不要另写一份;新增、删除文档或改变用途时同步更新两个 `README.md` 索引。
- 专用 webhook 端口(`api/webhooks.py`)上只有 `POST /hooks/<来源路径>` 一条路由:主服务无身份验证,对外只转发这个端口,不要往它上面挂其他接口或静态文件。webhook 来源(`WebhookSource`)是全局资源,只由人维护;自动化脚本经 `webhook_source` 订阅来源,一次投递扇出给全部订阅者。来源密钥只给人看:列表与 Agent Tool 结果一律用 `WebhookSource.public_dict()`,密钥只经 `/api/webhook-sources/{id}/secret` 端点返回。来源有两类(`kind`):`webhook` 接收外部投递;`github` 由 `GitHubEventPoller` 用 GitHub 身份(`GITHUB` 环境的 `TOKEN`)轮询 GitHub 生成投递,不接受外部 POST;这类来源只定义身份、轮询间隔与是否含通知,不绑定仓库,仓库与事件由订阅脚本的 `Automation.github_repos` / `github_events` 声明(主控经 `automation.save` 自行订阅新仓库),事件只交给订阅了对应仓库的脚本。平台对 GitHub 只读:不注册 webhook、不改通知状态,不要新增会修改外部系统的平台行为;更细的状态对比由 Agent 或脚本自己用 `gh api` 完成,平台不代做。两类都经 `webhooks.fan_out_event` 扇出,事件形状一致(`transport` 区分 `webhook` / `poll`),新增事件来源也走这个入口;轮询进度存 `webhook_source_state` 表,不写进来源对象。来源收到的事件(含没有脚本接收的)经 `webhooks.record_delivery` 记入 `webhook_deliveries` 表(保留 7 天、每来源 1000 条,正文落库前脱敏),人在来源的「记录」里看,聊天角色用只读动作 `webhook.history` 查;新增投递入口要调用它。角色的一次性事件回调(`callback.create` 的 `webhook_source`)也从这张表按游标取事件,不在投递路径上另埋钩子;它自带的过滤脚本只经 `event_filters.run_event_filter` 运行(环境从零搭起、不签发令牌,有 bubblewrap 时无网络且只读),不要给它平台能力或让它继承服务进程环境。
- 凭据以环境为单位:`Credential` 是一个环境,即一组配套使用的变量(`CredentialVariable`,地址 + 用户名 + 密码,或只有一个 token),每个变量各自标记是否 secret;没有"单个保密值"这种凭据。环境分全局与项目两层,同名时项目的整份覆盖全局(不逐变量合并)。secret 变量的值只存 `credentials` 表的 `value` 列("变量名 → 值"的 JSON),只经项目凭据目录(工作区 `credentials/<环境>/<变量>`、脚本的 `MISSIONCREW_CREDENTIALS_DIR`)交给 Agent,不进 Prompt、Agent Tool 结果或任何 API 返回,接口只写不读;普通变量的值不保密,放在元数据里随接口返回,不参与脱敏——不要把它当保密值处理,也不要让 secret 的值进入 `CredentialVariable.value`。新增会持久化人或 Agent 文本的写入口(消息、过程日志、运行输出、Agent Tool 参数等)要先过 `store.redact_credentials`。环境 `GITHUB` 的 secret 变量 `TOKEN` 同时是项目的 GitHub 身份(常量在 `core/models.py`,不要在别处写死这两个名字):`write_credential_context` 在同一把锁里重写 gh 配置目录,派发与脚本环境经 `github_env()` 注入 `GH_CONFIG_DIR` 和 git credential helper,`github_token()` 与凭据目录用同一口径(项目自己的 `GITHUB` 环境整份覆盖全局的);环境变量里只放文件路径,不放 token。旧版单值凭据由 `Store._migrate_credentials_to_environments` 在打开库时迁成环境(`NAME` → `NAME`/`VALUE`,`GITHUB_TOKEN` → `GITHUB`/`TOKEN`)。
- 总览分层(均带 ETag,前端 8s 轮询):`/api/overview` 只含全局数据(projects/backends/role_templates,准则/Skill 只留元信息+内容指纹);角色/面板/数据源(状态取值与颜色,不含同步统计)/自动化走 `/api/projects/{id}/overview`;频道与任务走 `/api/projects/{id}/channels|tasks?scope=active|archived|all`(响应含全量口径 counts,任务不含 body;项目任务列表只在看板页拉取,聊天页只带 `channel` 参数拉当前频道绑定的任务)。全文走准则单条端点、`skills/library` 与 `/api/tasks/{id}`。项目整对象回传时,带指纹的精简条目由 `save_project` 按主键回填现有全文,不得清空正文;给总览新增字段前先掂量轮询体积。
