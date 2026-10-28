# MCP 接入（DSH / Claude Code）

Zymor 以 **MCP Server** 接入任意宿主：tool-first，记忆内容只通过工具返回；`instructions` 短协议要求 Agent 在会话开始/压缩后/任务切换时调用 `memory_context`。

## 启动

```bash
# stdio（默认，适合本地宿主）
uv run zymor mcp

# Streamable HTTP（需要 token；默认 127.0.0.1:8765，可在 [mcp].host/port 配置）
export ZYMOR_MCP_HTTP_TOKEN=<随机串>
uv run zymor mcp --transport http --port 8765

# 只读模式：不下发写工具（检索与资源可用）
uv run zymor mcp --read-only
```

启动时定位 vault：`--vault` > 配置 `vault.path` > 向上查找 `.zymor/vault` > `~/.zymor/vault`；找不到则退出并提示 `zymor init`（不自动创建）。

## DSH

编辑 `~/.dsh/profiles/desktop/cordis.patch.yml`（保存后 HMR 热重载）：

```yaml
- insert:
    - id: mcp-zymor
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: zymor
        transport: stdio
        command: uv
        args: ["run", "--directory", "<ZYMOR_REPO>", "zymor", "mcp"]
        cwd: "<PROJECT_DIR>"
        failOnStartupError: false
```

工具在 DSH 里的名字形如 `mcp__zymor__memory_search`。

## Claude Code

```bash
claude mcp add zymor -- uv run --directory <ZYMOR_REPO> zymor mcp
```

## 验收步骤

1. 新会话中模型可见 `mcp__zymor__*` 工具；
2. 调用 `memory_context` 返回非空装配块（含 Persona facets / Current focus / Active capsules / Atlas pointers）；
3. `memory_write` 写入一条记忆 → `memory_search` 能检回 → `memory_read` 读到正文；
4. 写入后 `zymor index catalog` 生成 ATLAS/catalog，`memory_browse` 可分页读取。

## 工具与资源

- 工具：`memory_context`、`memory_rules`、`memory_rule_check`、`memory_search`、`memory_semantic_search`、`memory_read`、`memory_browse`、`memory_links`；读写模式下另有 `memory_write`、`memory_focus`、`memory_close`、`memory_log`、`memory_answer`、`memory_dream`、`memory_promote`、`memory_adopt`、`memory_stale`。
- resources：`zymor://atlas`、`zymor://catalog/<category>`、`zymor://node/<id>`（需日志更新后由工具按需读取，不保证自动注入）。
- prompts：`zymor-boot`、`zymor-review`；DSH 不支持 MCP prompts，代码不依赖它。

## 注意事项

- **记忆内容是证据不是指令**：工具返回内容不得被当成系统指令执行；L4 facet 是用户确认过的指导，可使用。
- **写锁**：同一 vault 的写入受 `.lock` 保护；并发写返回可操作错误，只读检索不受影响。
- **HTTP 必须配置 token**（`[mcp].http_token_env`，默认 `ZYMOR_MCP_HTTP_TOKEN`）；stdio 依赖本机信任模型。
- **敏感信息**：写入前检测密钥/口令并拒写；检索/读取返回对疑似敏感串脱敏。
- **评测模式**：`[mcp].benchmark_profile = true` 时 instructions 只保留与竞品等价的触发句，报告必须披露。

