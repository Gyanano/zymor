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

- 退出码 0 表示所有检查通过；1 表示失败。
- 输出包含 rule id、title、path、kind、value、message。

MCP：`memory_rule_check(paths, task_type)`；路径必须在项目根目录内。

## 4. 边界

- 不做任意命令执行，避免从记忆内容执行 shell。
- 没有 `## 检查` 的规则只作为上下文注入，不参与检查。
- 规则检查通过只代表声明的文本约束满足，不代表任务测试通过。
