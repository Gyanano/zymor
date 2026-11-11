# 对话捕获（写入 L0）

做梦只能提炼 L0 里已有的事件。本页说明怎样让对话自动进入 L0，不依赖 Agent 主动调用 `memory_log`。

有三条路径，按宿主选一条：

| 宿主 | 路径 | 是否自动 |
| --- | --- | --- |
| Claude Code | Hook 调用 `zymor hook capture` | 是 |
| 能导出对话文件的工具 | `zymor ingest <文件或目录>` | 手动或定时任务 |
| 远端服务、脚本、代理 | `POST /ingest`（HTTP 传输） | 由调用方决定 |

三条路径写入同一种 L0 事件。写入前，Zymor 把疑似密钥替换为 `***`，并删除宿主注入的 `<system-reminder>` 等包装块。

## Claude Code

把 `integrations/claude-code/settings.example.json` 里的 `hooks` 合并进 `~/.claude/settings.json`。钩子环境里找不到 `zymor` 命令时，把命令改成它的绝对路径。使用云端模式时，`zymor connect <地址> --claude-code` 会自动完成这一步。

示例配置在三个事件上调用 `zymor hook capture`：`Stop`（每轮回答结束）、`PreCompact`（上下文压缩前）、`SessionEnd`（会话结束）。

`zymor hook capture` 的行为：

- 从 stdin 读取 Hook 事件 JSON，使用其中的 `transcript_path`、`session_id`、`cwd`。
- 配置了服务器（`[remote].url`）时，把对话上传到服务器，不写本地库，见 [`cloud.md`](cloud.md)。
- 没有配置服务器时，从 `cwd` 向上查找 `.zymor/vault`。找不到项目库时写入用户库 `~/.zymor/vault`。
- 只导入上次导入之后新增的完整行。重复触发不产生重复事件。
- 任何错误只写 stderr，退出码恒为 0。Hook 不会打断会话。

## 导入对话文件

```bash
zymor ingest ~/.claude/projects/<项目目录>/          # 目录：导入其中全部 .jsonl 与 .json
zymor ingest chat.jsonl --session weekly-review      # 单个文件，指定会话标识
zymor ingest export.json --format generic
zymor ingest session.jsonl --include-tools           # 同时导入工具调用与结果的摘要
```

支持两种格式，`--format auto` 按首条记录判断：

- `claude-code`：Claude Code 的会话 JSONL。跳过思考块、子代理消息和元消息。
- `generic`：每行一个 `{"role": "...", "content": "...", "ts": "..."}` 的 JSONL；或一个 JSON 文件，内容是消息数组，或是含 `messages` 数组的对象。`role` 接受 `user`、`human`、`assistant`、`ai`、`model`、`system`，其余角色跳过。

导入进度记录在 `<vault>/capture/ingest.json`。文件被替换或截断时，Zymor 从头导入该文件。

默认不导入工具调用与工具结果，因为它们体积大、可提炼的内容少。

## HTTP 推送

`zymor mcp --transport http` 启动后提供 `POST /ingest`，鉴权方式与 MCP 相同（Bearer token）：

```bash
curl -X POST https://memory.example.com/ingest \
  -H "Authorization: Bearer $ZYMOR_MCP_HTTP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "chatgpt-2026-11-11", "messages": [{"role": "user", "content": "以后默认用中文"}]}'
```

- 请求体上限 2 MB，单次最多 2000 条消息。
- 调用方负责只推送新消息。该入口不去重。
- 只读模式（`--read-only`）不提供该入口；`[mcp].ingest_endpoint = false` 可单独关闭它。

## 捕获之后

对话进入 L0 后不会立即变成记忆节点。`zymor dream` 运行时才提炼，见 [`dream.md`](dream.md)。提炼需要配置 LLM，最少只需一项：

```toml
[llm.models]
default = "<模型名>"
```

## 边界

- Zymor 不读取网页版聊天产品的对话。这类产品要先导出文件，或由你写的脚本调用 `/ingest`。
- 导入的对话保存在 L0，默认 90 天后清理（`dream.l0_retention_days`）。L0 目录默认不进 git。
- 密钥检测基于模式匹配，不保证覆盖所有格式。不要把含凭证的对话导入共享的库。
