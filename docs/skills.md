# 项目 Skill 完整目录

MissionCrew 的项目 Skill 是完整目录，不只是数据库中的一段提示词。每个项目都有独立的投放目录：

```text
${MISSIONCREW_HOME}/projects/<project-id>/skills/
├── code-review/
│   ├── SKILL.md
│   ├── scripts/
│   ├── references/
│   └── assets/
└── release/
    └── SKILL.md
```

默认 `MISSIONCREW_HOME` 是启动目录下的 `.missioncrew`。Skill 页面会显示当前项目的绝对投放路径。

## 有效 Skill

每个 Skill 必须是一个普通目录，目录中必须且只能有一个文件名不区分大小写的 `SKILL.md`。MissionCrew 载入时会统一为大写文件名。文件必须以 YAML frontmatter 开头，并至少包含字符串属性 `name` 和 `description`：

```markdown
---
name: code-review
description: Review code changes for correctness, risks, and maintainability
---

# Workflow

Read `references/checklist.md`, then run `scripts/inspect.py` when needed.
```

其他 frontmatter 属性会原样保留，以兼容不同 Agent 后端的扩展。`scripts/`、`references/`、`assets/` 和其他同目录文件都会作为 Skill 包的一部分保留。

Skill id 默认取目录名，只允许字母、数字、下划线和连字符。ZIP 根目录直接包含单个 `SKILL.md` 时，MissionCrew 使用 frontmatter 的 `name` 作为 id，前提是它也满足 id 规则。

## 三种导入方式

### 上传 ZIP

在项目 Skill 页面选择“上传 ZIP”。ZIP 可以包含单个 Skill，也可以包含任意层级的多个 Skill 目录；平台递归检测有效 `SKILL.md`，把每个完整目录安装到项目 Skill 根。

### 导入本地目录

选择“导入本地目录”，使用服务器本机目录选择器指定一个 Skill 或 Skill 集合目录。平台递归检测并复制完整目录。导入完成后源目录不受 MissionCrew 管理，后续修改源目录不会自动同步。

### 直接投放

把 Skill 文件夹直接复制到页面显示的项目投放目录下。这里按一级子目录管理，每个子目录对应一个 Skill。Web 总览轮询、手动“重新扫描”或下一次 Agent 执行装配都会自动发现变化，不需要再调用导入接口。

已有的旧式文本 Skill 会在项目首次初始化时自动迁移为 `skills/<id>/SKILL.md`。新项目创建时会立即获得空的 `skills/` 目录。

## 重复、删除与安全边界

- 导入发现同 id Skill 时先返回冲突清单，用户确认后才覆盖。
- 覆盖或从 Web 删除 Skill 时，旧目录移到项目下的 `.skill-trash/<timestamp>/`，不会直接永久删除。
- ZIP 拒绝绝对路径、`..` 路径穿越和符号链接；压缩包、单文件、文件数量及解压总大小都有上限。
- 本地目录导入拒绝符号链接，避免 Skill 通过链接引用导入根之外的文件。
- ZIP 或本地集合中的无效条目会显示在扫描问题中；有效的其他 Skill 仍可导入。

## Runtime 适配

每次执行前，MissionCrew 扫描项目 Skill 库，并执行以下通用适配：

1. 只把已启用 Skill 以目录链接映射到当前 Agent 的 `.missioncrew/skills/<id>/`。
2. 通过 `MISSIONCREW_SKILLS_DIR` 告知 Runtime 统一入口。
3. 把真实项目 Skill 根和 Agent harness 根加入 `MISSIONCREW_ALLOWED_DIRS`，支持原生多目录授权的 Runtime 同时获得对应命令行权限。
4. 在持久公共上下文中列出每个 Skill 的 id、name、description、完整目录内容版本和 `SKILL.md` 路径，不预加载正文。
5. Agent 判断 Skill 与任务相关时读取 `SKILL.md`，再以该 Skill 目录为根解析脚本、参考资料和资源文件。

目录中任意文件变化都会改变内容版本，因此同一频道、同一角色复用的 Runtime 会话会在下一轮收到更新后的公共上下文。这套适配不依赖某个 CLI 的原生 Skill 搜索目录，所以 MissionCrew 支持的打印模式和 ACP Runtime 都使用相同项目 Skill。

## API

- `GET /api/projects/<project>/skills/library`：读取投放路径、文件清单与扫描问题。
- `POST /api/projects/<project>/skills/import-zip`：请求体为原始 ZIP 字节；`overwrite=true` 表示确认覆盖。
- `POST /api/projects/<project>/skills/import-folder`：导入服务器本地目录。
- `POST /api/projects/<project>/skills/rescan`：立即重新扫描直接投放目录。
