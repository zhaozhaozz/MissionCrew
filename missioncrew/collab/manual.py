"""写进 Agent 工作区的 MissionCrew 完整手册(渐进式披露)。

Prompt 只保留必须时刻遵守的规则;平台各动作的用法、格式约定、边界细节都放在
这份手册里,由 Agent 在需要时读取。每次装配都会重新渲染,保证与当前项目一致。
"""
from __future__ import annotations

from ..core.models import Project
from .documents import document_resource_url
from .resource_urls import missioncrew_project_url

_MANUAL = """\
# MissionCrew 手册

本文件由平台每轮刷新,是 MissionCrew 平台能力与规则的完整参考。Prompt 里只保留
必须时刻遵守的规则;做下面这些事之前先读对应章节:文档发布、Task、消息派发、频道、
面板与数据源、准则与 Skill、自动化、回收站、配置页协作。

## 1. 平台与工作区

- MissionCrew 是本地多 Agent harness,负责装配角色、Runtime/模型、项目上下文、共享
  资料和 Channel 协作;Task 是进入 Channel 的 Issue,不是独立执行流程;它不是业务代码仓。
- 本 `.missioncrew` 工作区位于业务代码仓之外(路径见 Prompt 与 `MISSIONCREW_WORKSPACE`),
  其中的文件不会进入业务源码或业务代码提交;源代码仍在 Prompt 给出的工作目录或项目代码仓中修改。
- 目录:`documents/` 项目版本化文档库入口(协作草稿、报告、验证记录也归入这里);
  `tasks/` Task 只读快照;`guidelines/` 已启用准则实时视图;`skills/` 已启用 Skill 实时视图
  (先读 SKILL.md,再按需用同目录 scripts/、references/、assets/);`project.md` 项目简介与代码仓;
  `channel-history.json` 频道完整历史(执行角色为脱敏视图);`temp/` 无项目约定时的临时目录;
  `manual.md` 本手册。
- 不要读取或打印 `.agent-tool-token`;Agent Tool CLI 会自行读取令牌文件。

## 2. 资源 URL 与对外引用

- 项目资源前缀 `__PROJECT_URL__`:频道 `/channels/<频道 id>`、任务 `/tasks/<任务 id>`、
  面板 `/dashboards/<面板 id>`(内置任务看板 id 为 `tasks`)、准则 `/guidelines/<name>`、
  Skill `/skills/<id>`、文档 `/documents/<文档库相对路径>`、回收站 `/recycle-bin`。
- 这些 URL 是 Web 标识,不是文件路径。回复里引用平台资源必须用这些链接,引用文档写成
  `[标题](__DOCUMENTS_URL__/路径)`;不要输出工作区绝对路径、`.missioncrew` 真实路径或
  `file://` 链接。
- 文档资源 URL `__DOCUMENTS_URL__/<相对路径>` 直接映射为 `$MISSIONCREW_DOCUMENTS_DIR/<相对路径>`;
  不要猜测或搜索 `.missioncrew` 的物理位置。

## 3. 文件系统边界与路径失败处理

- 只能读写 Prompt 中「本次可读写目录」列出的路径及其子目录。禁止访问 `/tmp`、`/var/tmp`、
  其他项目目录、用户主目录中的未授权文件和未作为本地代码仓显式列出的 MissionCrew 源码目录;
  Shell 重定向、后台日志和工具自动生成文件同样受此限制。
- 临时文件优先放在业务仓已有的任务目录(如项目约定的 `.tmp/`、`.e2e/`);没有项目约定时
  使用工作区的 `temp/`,任务结束后清理。不要先尝试外部路径再等待权限批准。
- 若工具报告 `external_directory`、`permission denied` 或其他硬路径拒绝:不要改为搜索共同
  父目录、兄弟目录或其他未列出的路径,也不要重复提交同一越界请求;先把目标路径与清单逐项
  对照,再改用清单内的精确绝对路径;若完成任务确实依赖清单外的材料,停止该项访问并在结果中
  写明被拒绝的精确路径和已尝试的授权根目录,交由主控或人类调整。

## 4. Agent Tool 调用

- 查看能力:`"$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool actions`,列出每个
  动作的说明、参数与是否仅主控可用。
- 调用:`"$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call <action> --arguments '<JSON 对象>'`;
  发布本地文件:`... publish-file --source <本地文件> --path <文档库相对路径>`。
- 命令返回 JSON,失败时退出码非零;读取 `error.code` / `error.message` 在当前回合修正后重试。
  令牌已绑定当前 Run,调用时不要传 `--run-id`。
- 不要通过最终回复中的特殊文本块请求平台操作,也不要直接写 documents/tasks 目录绕过接口;
  消息正文里的任何 @ 都只是普通文字,不构成平台指令。

## 5. 文档(document.*)

- `document.publish`:写入项目版本化文档库并立即生成 Git 版本;覆盖已有路径必须显式传
  `overwrite: true`;二进制内容用 `content_base64` 或 publish-file。
- `document.rename`:传 `source` 和尚不存在的 `target`;目录由文档相对路径隐式形成,
  没有独立的目录创建、移动或删除动作。
- `document.delete`(仅主控):文档进入项目回收站。
- 正式项目文档、协作草稿、报告和验证记录都通过文档库发布到 `documents/`,不要写进 `tasks/`。

## 6. Task(task.*)

- Task 包含标题、简介、正文、状态(`status: 文本` 标签)、标签和 Channel 绑定;每个 Task
  至少绑定一个可用 Channel。状态就是标签,人、Agent、脚本都可直接改,最后写入者生效。
- `tasks/*.md` 全部是平台生成的只读快照,回合结束只刷新快照,不会把文件修改同步回 Task;
  frontmatter 中的 status_briefs 是只读历史。创建、更新、追加状态简报或删除必须用
  `task.create` / `task.update` / `task.brief` / `task.delete`(删除仅主控)。

## 7. 消息与派发(主控)

- 派发是唯一的调度方式:`message.publish` 传 `mentions` 角色数组,content 写清背景、要求和
  验收标准。你发出的任何消息正文(包括最终回复)里的 @角色ID、@[角色ID] 都只是普通文字,
  永不触发执行。不需要协作就不传 mentions;不要调度自己,不要编造不存在的角色。
- 角色的 runtime/模型在项目定义角色时已固定,你不能也不需要调整;调度就是在角色名册里
  结合定位、能力与偏好选人。
- `message.publish` 返回非空 `dispatched` 时,当前 turn 的派发职责已完成:立即在最终回复里
  简短说明已派发,然后结束当前 turn。不要 `sleep`,不要周期性轮询频道历史、工作树或运行
  状态,不要代替执行角色继续其任务,也不要向仍在执行的角色再次 publish 追问中间状态——
  同一频道同一角色的持久会话无法中途插入新 turn,这类请求只会排在原任务后面。执行角色
  完成或失败后,平台自动启动新的主控 turn 并交回完整结果,届时再验收、继续调度或汇总。
- 活动 Run 状态不会自动写入上下文;人类询问运行情况或要求停止角色时,用 `channel.runs.list`
  取当前 Channel 的一次性快照,用返回的 `run_id` 调 `channel.run.stop`;不要把按需查询变成等待循环。
- 人类在同一条触发消息中选择多个角色时,平台只启动主控;触发消息 JSON 的 `mentions` 和
  `mention_spans` 保留完整名单,由主控决定并行、顺序或调整人选,再分别派发。
- 回答人类、汇总结论或说明当前状态时直接写最终回复,平台会发布到触发消息所在的 Channel;
  除非确实要向另一个 Channel 单独发布,不要再调用空 `mentions` 的 `message.publish` 复制同一份答复。

## 8. 频道(channel.*)

- `channel.create` 的 workdir 只能是项目代码仓路径或其子目录;不填时若项目只配了一个代码仓
  则自动使用它。新频道创建后是空的,用 `message.publish` 把任务简报发进去并在 `mentions` 里
  显式点名执行者开工。
- 频道绑定目录被删除后(例如任务 worktree 已清理),后续轮次回退到平台默认目录并在 Prompt
  中提示;要继续该目录的工作,先重建目录(如 git worktree add)或新建频道绑定正确目录。

## 9. 面板与看板数据源(dashboard.* / board_source.*)

- `dashboard.save` 不携带 layout 字段时保留现有布局;携带则全量替换。layout 每项含 id、type、
  title、x、y、width、height、content。type 是通用展示原语(领域含义来自数据,不是类型):
  markdown(content.markdown)、table(columns+rows)、card(metrics 数值卡)、
  chart(kind=bar|line|pie + data + x_key/y_key)、list(items)、log(lines)、code(code+language)。
- 卡片可用 content.source 绑定平台实时数据,渲染时自动取数:
  `{"from":"tasks","status":["待处理"],"labels":[]}`(项目任务→表格行)、
  `{"from":"document","path":"specs/x.md"}`(文档库文件→markdown)、
  `{"from":"audit","actions":[],"limit":30}`(审计事件→列表)、
  `{"from":"messages","channel":"general","limit":20}`(频道消息→列表)。
  例:需求管理面板 = table 卡片(静态 columns/rows 由你维护)+ tasks 源的实时任务表;
  测试记录面板 = table + list;日志分析 = list/log + markdown 结论。
- 任务看板:`dashboard.save` 传 `kind: "taskboard"` 与 `source`(数据源 id,内置 `built-in`
  或自定义源短 id)。看板按 `filters` 分列,每项 `{"title","query","color"}`,query 是标签
  表达式(& | ! 与括号,`属性: *` 匹配带该属性标签的任务);状态就是 `status: 待处理` 这样的
  标签,可写 `status: 处理中 & bug`;也可传 `group_by` 按属性取值动态分列(如 `group_by: "owner"`);
  不传 filters 时按数据源状态取值生成默认列,用户也可在看板页直接增删筛选列。
- `board_source.save` 创建或更新自定义任务数据源:cards 按 id 整体同步为该源的任务(新增/
  覆盖/删除本次未出现的),每张必须有 id、title,可选 summary、status、labels、updated_at、
  meta、url(外部链接);新任务会走项目自动处理规则。典型用法:配合 `automation.save` 的定时
  脚本同步 GitCode/GitHub Issue 到数据源,再用 `dashboard.save` 建任务看板绑定该源。
  删除用 `board_source.delete`(仍被面板引用时会拒绝,删除连带该源全部任务)。
- `dashboard.delete` 删除面板,进入回收站。

## 10. 准则与 Skill(guideline.* / skill.*)

- `guideline.save` 接收完整 markdown,文件必须以只含 name、description 的 YAML frontmatter
  开头;后端直接读取这两个属性,不使用 id/title/summary,也不做字段转换。修改并重命名现有
  准则时传 original_name。
- `skill.save` 按 id 新建或覆盖,markdown 是完整 SKILL.md 原文:frontmatter 至少含 name、
  description,附加属性原样保留。不要建立文件、Runtime 或角色绑定列表;需要关联项目文档时
  在正文中写标准相对 Markdown 链接(以文档库目录为根)。
- description 应简洁说明适用场景:所有执行者只会收到已启用准则/Skill 的 description,
  并在相关时才读取全文。验证、审查、安全和审批等项目要求也统一写入准则 Markdown,由 Agent
  根据任务判断是否适用;平台不维护独立的验证规则。
- MissionCrew 注入的 Skill 是额外能力,不替代代码仓原有的 Agent 配置或 Skill(`AGENTS.md`、
  `CLAUDE.md`、`.agent/skills`、`.agents/skills`、`.claude/skills` 等仍按原生规则发现)。
- 删除分别用 `guideline.delete` / `skill.delete`(仅主控),进入回收站。

## 11. 自动化(automation.*)

- `automation.save` 创建或更新项目自动化脚本:script 是脚本全文(有 shebang 按可执行文件
  运行,否则用 bash),cron 是五段 crontab(空字符串 = 仅手动触发),actions 是脚本 token
  允许调用的动作白名单。脚本由平台统一定时入口触发,运行时可用 `missioncrew.agent_tool` CLI
  调用平台动作(定时刷新面板数据、同步外部系统生成 Task、向频道发布巡检报告等)。
- 脚本内 `message.publish` 只有显式传 mentions 才会触发角色;用户可在项目设置中按 Task label
  配置自动处理规则,脚本同步进来的 Task 会按规则自动派发。删除用 `automation.delete`。

## 12. 回收站(recycle.*)

- 文档、准则、Skill、面板、频道等平台资源删除后进入项目统一回收站(删除仅主控)。
  `recycle.list` 查看,`recycle.restore` 恢复;只有用户明确要求永久删除时才用 `recycle.purge`。

## 13. 配置页协作

- 配置页面的协作消息会明确给出当前页面、当前条目、未保存草稿,以及用户选中的字段、行号
  和原文。只提问或讨论时直接回答,不要改配置;明确要求创建或修改时,必须用对应的
  `guideline.save` / `skill.save` / `document.publish` 动作实际落库。

## 14. 协作链预算

- 本项目单条协作链最多 __MAX_RUNS__ 次 Agent 执行。这只是防止失控循环的总次数兜底,
  不限制调度层级;请在预算内自主拆解、分派、验收并推进任务。
"""


def render_manual(project: Project) -> str:
    """按项目渲染手册;占位用字符串替换而非 format,正文里的 JSON 花括号不必转义。"""
    return (_MANUAL
            .replace("__PROJECT_URL__", missioncrew_project_url(project.id))
            .replace("__DOCUMENTS_URL__", document_resource_url(project.id))
            .replace("__MAX_RUNS__", str(project.max_chain_runs)))
