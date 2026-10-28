# Codex 接入

Codex 通过 MCP 使用 Zymor，不需要宿主专属插件。

## MCP

`~/.codex/config.toml`：

```toml
[mcp_servers.zymor]
command = "uv"
args = ["run", "--directory", "<ZYMOR_REPO>", "zymor", "mcp"]
```

工具名通常带 `zymor` 前缀；在 Codex 中调用 `memory_context`、`memory_search`、`memory_rule_check`。

## 会话启动

在项目的 `AGENTS.md` 中加入：

```text
开始任务前：先调用 Zymor 的 memory_context，并加载 memory_rules。
修改代码后：调用 memory_rule_check；若 next_action=repair，按 repair_instructions 修复并重试。
遇到与历史决策/偏好相关的问题：先 memory_search，再 memory_read。
```

也可以让 Codex 执行：

```bash
zymor hook context --json
```

取其 `text` 注入当前上下文。

## 边界

- 不依赖 Codex 专属 hook；使用 MCP 与 AGENTS.md 即可；
- 不复制核心检索/写入逻辑到 Codex；
- DSH 插件由独立项目维护。
