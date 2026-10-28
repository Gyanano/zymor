# Harness Hook / Skill 接入

Zymor 提供一个与宿主无关的 `HarnessAdapter` 契约。Claude Code、Codex 这类 Harness 通过 Hook/Skill 调用；DSH 插件作为独立项目实现同一契约，不在本仓库打包。

## 契约

`src/zymor/harness.py`：

```python
class HarnessAdapter(Protocol):
    name: str

    def session_start(self, vault, config, *, task=None, project=None,
                      budget_tokens=None, session_id=None) -> dict: ...

    def capture(self, vault, *, session_id, kind, content, meta=None) -> dict: ...

    def generation(self, vault, *, paths=None, content=None, task_type=None,
                   attempt=1, max_attempts=3, session_id=None) -> dict: ...
```

- `session_start`：返回 `memory_context` 的 `text` 与 token 估算；
- `capture`：向 L0 追加事件，`meta.source=harness`；
- `generation`：执行 verified 规则检查并返回 `next_action` / `repair_instructions`。

`LocalHarness` 是参考实现，直接复用 CLI/MCP 同一条代码路径。

## CLI Hook 入口

```bash
zymor hook context --task "实现登录" --project my-project --json
zymor hook context --task "实现登录" --budget-tokens 1500
```

JSON 输出包含 `text`、`tokens_estimate`、`section_tokens`、`task_type`、`degraded`、`warnings`。Hook 应把 `text` 注入会话上下文，不要改写其中的记忆内容。

## Claude Code / Codex

通用接入步骤：

1. SessionStart / 会话开始：调用 `zymor hook context --json`，把 `text` 加入上下文；
2. 工具/错误/会话结束：调用 `zymor log` 或 `zymor capture` 写入 L0；
3. 生成代码后：调用 `zymor rule-check --stdin --path <target> --json`（或 MCP `memory_rule_check`）；
4. `next_action=repair` 时按 `repair_instructions` 修复，再以 `attempt+1` 重试；
5. `next_action=stop` 时报告未修复项，不要静默通过。

宿主侧只负责调用，不在 Hook 中重写 Zymor 的检索、写入或规则逻辑。

## 参考集成

- Claude Code：`integrations/claude-code/session-start.sh` + `settings.example.json`；
- Codex：`integrations/codex/README.md`；
- DSH：独立插件项目按本契约接入。

Claude Code 的 SessionStart Hook 会把脚本 stdout 加入上下文；脚本直接输出 `zymor hook context` 的文本。

## 边界

- 不提供宿主专属扩展包；
- 不做 DSH 插件打包（独立项目）；
- 不把 Hook 写成宿主专属逻辑；
- 不在 Hook 中执行记忆内容里的命令。
