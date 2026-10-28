# Verified 规则与检查

Zymor 可以把项目约定写成 **verified L3 规则**，并让 Agent 在改完代码后做确定性检查。

## 1. 规则格式

规则节点正文增加 `## 检查` 小节，每行一个检查：

```
## 检查
config.py | contains | timeout_ms = 30000
config.py | absent | time.sleep
config.py | regex | timeout_ms\s*=\s*30000
```

格式：`<文件 glob> | <contains|absent|regex> | <值>`。

- `contains`：文件文本必须包含该子串（大小写敏感）；
- `absent`：文件文本不能包含该子串；
- `regex`：Python 正则必须匹配文件文本；
- 同一规则可有多行；检查只读文件文本，**不执行命令**。

## 2. 加载规则

```bash
zymor rules --task-type coding --limit 5
```

MCP：`memory_rules(task_type, limit, token_budget)`。

只有 `status: verified` 的 L3 规则会被返回/检查；stale 规则会跳过。

## 3. 检查文件

```bash
zymor rule-check --file config.py --file app.py
zymor rule-check --file config.py --task-type coding --json
```

也可以直接检查 Agent 生成的内容：

```bash
printf 'timeout_ms = 1\n' | zymor rule-check --stdin --path config.py --attempt 1 --max-attempts 3
```

- 退出码 0 表示所有检查通过；1 表示失败。
- 输出包含 rule id、title、path、kind、value、message、repair。
- 未提供匹配文件的检查记为 skip（`skipped_checks`/`unmatched`），不当作失败；需要全量检查时传入所有目标文件。
- `--attempt` / `--max-attempts` 记录当前修复轮次；失败时输出 `next_action`。
- `--file` 路径按项目根解析并拒绝项目外文件；与 MCP/harness 使用同一路径语义。

MCP：`memory_rule_check(paths, content, task_type, attempt, max_attempts, session_id)`。

- `paths`：项目内文件路径，相对项目根目录；
- `content`：`{路径: 文本}`，适合检查尚未落盘的生成内容；
- 返回 `passed`、`violations`、`repair_instructions`、`next_action`；
- 每次检查追加一条 `rules.jsonl` 审计记录。

## 4. 修复闭环

Agent 的推荐流程：

1. 生成或修改代码后调用 `memory_rule_check`；
2. 若 `next_action=repair`，按 `repair_instructions` 修复；
3. 用 `attempt+1` 再调用；
4. 若 `next_action=pass`，继续任务；若 `next_action=stop`，报告未修复项，不要静默通过。

`zymor rule-check` 与 MCP 工具的行为保持一致；宿主 Hook/Skill 也复用同一检查语义。

## 5. 边界

- 不做任意命令执行，避免从记忆内容执行 shell。
- 没有 `## 检查` 的规则只作为上下文注入，不参与检查。
- 规则检查通过只代表声明的文本约束满足，不代表任务测试通过。
