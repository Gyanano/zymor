<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/zymor-horizontal-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/zymor-horizontal-light.svg">
    <img alt="Zymor Logo" src="assets/zymor-horizontal-light.svg" width="800">
  </picture>
</p>

**为 AI Agent 打造的分层、长期存续的记忆引擎**

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-Server-6f42c1)](https://modelcontextprotocol.io/)
[![Version](https://img.shields.io/badge/Version-v0.2.1-brightgreen)](CHANGELOG.md)
[![PyPI](https://img.shields.io/pypi/v/zymor)](https://pypi.org/project/zymor/)
[![CI](https://github.com/Gyanano/zymor/actions/workflows/ci.yml/badge.svg)](https://github.com/Gyanano/zymor/actions/workflows/ci.yml)
![Storage](https://img.shields.io/badge/Storage-Markdown%20%2B%20SQLite-4b8bbe)

**中文** | [English](README.en.md)

**Zymor（织梦师）** 是一个与宿主无关的 Agent 记忆系统：经验沉淀为可检索、可纠错、可移植的分层记忆，夜间由「做梦」完成巩固、矛盾仲裁与知识升华。记忆库是纯 Markdown 事实源 + SQLite FTS5 索引，可读、可 diff、可迁移。

## 状态

**v0.1.0 — 记忆内核已完成。** 当前实现覆盖：

- **记忆内核**：L0 事件流、L1 焦点、L2 胶囊、L3 知识节点、L4 人格 facet；
- **检索与上下文**：FTS5 + 标识符子词、评分/过滤、渐进披露、ATLAS/catalog；
- **写入协议**：门控、去重、状态机、审计、隐私删除；
- **做梦引擎**：NREM 巩固、认知失调仲裁、REM 联想、人格提炼；
- **MCP Server**：stdio / Streamable HTTP，读写与只读模式；
- **长期记忆**：跨项目 L3 晋升、`projects` provenance、文件/依赖/API 版本锚点、stale 排除、可选 embedding 语义检索与混合检索、`memory_rules` 与 `memory_rule_check`。

## 为什么是 Zymor

- **分层，而不是一锅向量**：常驻微内核（人格 facet + 当前焦点 + 活跃胶囊）加指针索引；更深的内容按需展开。
- **可纠错，不是只追加**：矛盾会被验证、替代或墓碑化，不会静默覆盖。
- **会做梦，不只是检索**：离线巩固、仲裁、跨域联想与人格提炼。
- **可渐进披露**：目录导航 + 精确检索 + 链接跳转，而不是每次把整个库塞进上下文。

## 安装

```bash
pip install zymor
```

## 快速开始

```bash
git clone <repo> zymor && cd zymor
uv sync

cd my-project
uv run --project <repo> zymor init            # 项目库：./.zymor/vault
uv run --project <repo> zymor init --global   # 用户库：~/.zymor/vault
uv run --project <repo> zymor remember "中文检索用 CJK bigram 预分词"
uv run --project <repo> zymor search "bigram"
uv run --project <repo> zymor dream --phase all
uv run --project <repo> zymor mcp             # MCP server（stdio）
```

## 架构

```
宿主 Agent / MCP client
        │  memory_context / rules / search / semantic_search / read / ...
        ▼
┌──────────────────────────────────────────────┐
│  Zymor MCP Server                            │
│  write protocol · retrieval · dream engine   │
└──────────────────────────────────────────────┘
        │
        ▼
  .zymor/vault/                ~/.zymor/vault/
  L0 / L1 / L2 / L3            L4 + promoted L3
  Markdown facts + SQLite FTS5 index
```

- **项目库**：`<project>/.zymor/vault`；**用户库**：`~/.zymor/vault`。
- L4 人格 facet 只写用户库；L3 默认在项目库，跨项目晋升后才进用户库。
- 文件是事实源，索引是可重建缓存。

## CLI

| 命令 | 作用 |
| --- | --- |
| `zymor init [--global]` | 初始化项目库/用户库 |
| `zymor write --file PATH \| --stdin` | 写入完整 Markdown 节点 |
| `zymor remember TEXT [--category ...]` | 快速捕获一条记忆 |
| `zymor search QUERY [--semantic]` | 检索（可选混合语义） |
| `zymor semantic QUERY` | 向量语义检索 |
| `zymor read ID` | 读取节点 |
| `zymor log --kind KIND --content TEXT` | 追加 L0 事件 |
| `zymor focus --session ID` | 查看/更新 L1 焦点 |
| `zymor index sync\|rebuild\|catalog\|embed` | 索引、目录、向量维护 |
| `zymor rules / rule-check` | 读取/执行 verified 项目规则 |
| `zymor review / answer` | 人工确认与晨间追问 |
| `zymor promote / adopt / anchor / stale` | 跨项目晋升、采纳、失效锚定 |
| `zymor dream [--phase ...]` | 运行做梦引擎 |
| `zymor mcp` | 启动 MCP server |
| `zymor doctor / stats` | 自检与统计 |

## MCP 集成

```bash
uv run zymor mcp                       # stdio（本地宿主）
export ZYMOR_MCP_HTTP_TOKEN=<随机串>
uv run zymor mcp --transport http      # Streamable HTTP
uv run zymor mcp --read-only           # 只读模式
```

Claude Code：

```bash
claude mcp add zymor -- uv run --directory <ZYMOR_REPO> zymor mcp
```

DSH / Cursor 的完整配置与验收步骤见 [`docs/guide/mcp-integration.md`](docs/guide/mcp-integration.md)。

MCP 工具：`memory_context`、`memory_rules`、`memory_rule_check`、`memory_search`、`memory_semantic_search`、`memory_read`、`memory_browse`、`memory_links`、`memory_write`、`memory_focus`、`memory_close`、`memory_log`、`memory_answer`、`memory_dream`、`memory_promote`、`memory_adopt`、`memory_stale`。

## 测试

最小冒烟测试只验证基础功能可用（写入/检索/读取/上下文/失效锚点/规则检查/跨项目晋升），不验证效果：

```bash
uv sync --extra dev
uv run pytest tests/test_open_smoke.py -q
```

## 文档

| 路径 | 内容 |
| --- | --- |
| `docs/guide/` | 快速开始、做梦、MCP 接入、规则检查、排障 |
| `docs/development/repository-workflow.md` | 开发、评审、发布、分支保护与协作流程 |
| `docs/development/release.md` | 版本发布、PyPI Trusted Publishing 与 tag 流程 |
| `CONTRIBUTING.md` | 贡献指南 |
| `SECURITY.md` | 安全报告流程与边界 |
| `CODE_OF_CONDUCT.md` | 行为准则 |
| `GLOSSARY.md` | 领域术语 |
| `CHANGELOG.md` | 版本变更 |

## 已知边界

- **没有常驻 daemon**：做梦由 CLI、`memory_dream` 或 `dream.auto_after_close=true` 的关闭事件触发。
- **L0 捕获依赖 Agent**：`memory_close` 会补一条关闭事件，但事件级捕获仍需要 Agent 调用 `memory_log`。
- **规则检查需要主动调用**：`memory_rules` 负责加载，`memory_rule_check` 负责校验；不调用就不会检查。
- **跨项目晋升是标签/人工触发的**：带 `cross-project` 标签的 verified L3 才自动晋升；`promotion.min_projects` 可加多项目门槛。
- **外部 API 版本靠本地清单**：`zymor anchor --api` 检查 repo 内的 JSON/TOML 版本清单，不轮询远端服务。
- **语义检索可选**：需要 `[embedding]` 配置并先运行 `zymor index embed`。
- **MCP HTTP auth 是静态 Bearer token**：没有过期/作用域/吊销；stdio 依赖本机信任模型。

## 贡献

见 [`CONTRIBUTING.md`](CONTRIBUTING.md)、[`SECURITY.md`](SECURITY.md)、[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)，以及 [仓库工作流](docs/development/repository-workflow.md)。

## License

Apache-2.0，见 [`LICENSE`](LICENSE)。

