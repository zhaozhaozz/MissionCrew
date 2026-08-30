# 参与贡献

感谢你对 MissionCrew 的兴趣。提 Issue、改文档、修 bug、接入新的 Agent CLI 都欢迎。

## 开发环境

```bash
git clone https://github.com/zhaozhaozz/MissionCrew.git
cd MissionCrew
uv sync                  # 创建 .venv 并安装含 dev 组的全部依赖
uv run pytest -q         # 完整测试(约 2 分钟;markdown 渲染测试需要本机有 node,没有会自动跳过)
uv run mc serve          # 本地起服务,访问 http://127.0.0.1:8321
```

要求 Python 3.10+ 和 [uv](https://docs.astral.sh/uv/)。CI 会在 3.10–3.14 上跑测试并构建 wheel，提 PR 前请至少在本机跑通完整测试。

## 代码约定

- 代码结构与分层规则见 [AGENTS.md](AGENTS.md)，其中的分层规则有测试钉住（`tests/test_runtime_abstraction.py`），违反会直接失败。
- 接入新的 Agent CLI：在 `missioncrew/runtime/clis/` 加一个声明模块并追加进 `clis.SPECS`，不要改执行器；步骤见 [docs/runtimes.md](docs/runtimes.md) 的「接入新工具」。
- 改动 `core/models.py` 等序列化字段时注意向后兼容：旧数据库里的记录经 `from_dict` 读入，未知字段会抛 `TypeError`。
- 行为变化要有测试；修 bug 请附回归测试。
- 注释和文档用中文，与现有代码保持一致；提交信息一行说明做了什么，正文按需补充原因。

## 提交 PR

1. 从 `main` 拉分支，保持一个 PR 只做一件事。
2. 跑通 `uv run pytest -q`；涉及前端改动请在浏览器里实际验证。
3. 涉及对外行为（CLI 参数、API、Agent Tool 动作、目录布局）的改动，同步更新 `README.md` 或 `docs/` 中对应的文档。
4. PR 描述写清动机、做法和验证方式。

## 安全问题

请不要在公开 Issue 中披露漏洞，报告方式见 [SECURITY.md](SECURITY.md)。
