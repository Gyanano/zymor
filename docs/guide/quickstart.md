# 快速开始

Zymor 是 Agent 的分层记忆系统：Markdown 双链知识库是事实源，SQLite FTS5 是索引缓存，MCP Server 提供工具面，做梦引擎在离线时巩固、仲裁与联想。

## 安装

```bash
git clone <repo> zymor && cd zymor
uv sync --extra dev
uv run zymor --version
```

开发期用 `uv run zymor ...`；发布后 `uv pip install zymor` 或 `uvx --from zymor zymor ...`。

## 初始化

```bash
cd my-project
uv run zymor init            # 项目库：./.zymor/vault
uv run zymor init --global   # 用户库：~/.zymor/vault（L4 人格 facet 只能写这里）
uv run zymor doctor          # 干净 vault 应无 error
```

项目库与用户库磁盘同构；`--vault PATH` 可覆盖定位。重复 `init` 幂等。

## 写入与检索

```bash
# 快速捕获一条记忆（semantic/L3，category 与 layer 可调）
uv run zymor remember "中文检索用 CJK bigram 预分词" --category semantic --layer L3

# 写入完整节点（Markdown + YAML frontmatter，服务端校验；格式见 spec §1.9）
uv run zymor write --file node.md

# 检索、读取（读取会递增 hit_count，同会话同节点只加一次）
uv run zymor search "bigram 分词"
uv run zymor read mem_20261027_aaaaaaaaaa --max-tokens 800

# 生成导航（ATLAS + 每类 catalog）
uv run zymor index catalog
```

## 焦点与做梦

```bash
uv run zymor focus --session s1 --create --task "实现检索层" --task-type feature
uv run zymor log --kind user_message --content "开始实现" --session s1

uv run zymor dream --dry-run    # 只输出计划，不写文件、不调 LLM
uv run zymor dream              # 真实 run：NREM + 仲裁 + REM（需配置 LLM）
```

做梦需要 LLM 通道。在 `<project>/.zymor/config.toml` 或 `~/.zymor/config.toml` 配置：

```toml
[llm]
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"

[llm.models]
extract = "gpt-4o-mini"
dedupe = "gpt-4o-mini"
dream = "gpt-4o"
arbitration = "gpt-4o"
```

密钥只放环境变量（`api_key_env` 指向变量名），不要写进配置文件。未配置 LLM 时 `dream` 会给出 warning 并保持水位线不推进，不会伪造巩固结果。

## 文件捕获（可选）

```bash
uv run zymor capture --path . --once --session capture-demo
uv run zymor capture --path . --interval 5 --session capture-daemon
```

- 默认忽略 `.git`、`.zymor`、`.venv`、`node_modules`、缓存目录；
- 文件创建/修改/删除写入 L0 `system_note`，`meta.source=capture`；
- 不带 `--once` 时作为可选后台轮询运行；默认不启动任何 daemon；
- 只监听显式指定的项目内目录，不监听用户库；
- 快照与待处理队列保存在 `<vault>/capture/state.json`，已被 vault `.gitignore` 忽略。

## 维护

```bash
uv run zymor review            # 列出 hypothesis/disputed 节点
uv run zymor answer Q1 --text "采纳新节点"   # 回答晨间追问
uv run zymor stats             # 节点/层/状态计数、写入动作、L0/dream/rules/检索指标
uv run zymor index sync        # 外部改动后增量同步索引
uv run zymor forget <id> --yes # 隐私删除（不可恢复）
```

## 下一步

- 做梦与追问语义：见 [dream.md](dream.md)
- 查错：见 [troubleshooting.md](troubleshooting.md)
