# 故障排查

先跑 `uv run zymor doctor`：它按「问题 → 影响 → 修复命令」列出 vault/索引/节点/断链/锁/未提交写入意图。

## 退出码

| 码 | 含义 |
| ---: | --- |
| 0 | 成功 |
| 1 | 一般错误 |
| 2 | 用法错误 |
| 3 | 未找到（节点/追问/vault） |
| 4 | 校验失败 |
| 5 | 写入锁冲突 |
| 6 | 预算超限（做梦/评测） |
| 7 | 索引错误 |

## 写入被拒

- **缺少 YAML frontmatter / 未闭合**：文件必须以 `---` 行开头，第二个 `---` 结束；必须能解析为 YAML mapping。
- **文件带 UTF-8 BOM**：另存为 UTF-8（无 BOM）。这也是为什么 Windows 下不要用会写 BOM 的编辑器/脚本。
- **正文 H1 与 title 不一致 / 缺必需 H2**：正文首行必须是 `# {title}`；four categories 各自必需 H2 见 spec §1.4；L2 胶囊还要 `目标/当前状态/下一步/关键决策与坑`。
- **L4 只能写用户库 / L2 只能写项目库**：`zymor init --global` 初始化用户库；项目内改用 L3。
- **status: deprecated 缺 valid_to/Tombstone/evidence**：墓碑必须三者齐全；用户 `forget` 之外不能产出无证据墓碑。
- **正文超限**：≤1000 tokens 且 ≤700 中文字；超出必须拆分。

## 写入锁冲突

```text
写入锁被占用: pid=... command=...
```

- `.lock` 由正在写的 `zymor` 进程持有；先确认没有并发的 MCP server/CLI 写入。只读检索不受影响。
- 崩溃残留锁：确认进程已退出后删除 `<vault>/.lock`。
- 做梦另有 `.dream.lock`（stale 2 小时自动清理）。

## 检索不到

- 默认不检索 `deprecated`：需要 `--status deprecated`（CLI）或 `include_deprecated=true` + `valid_only=false`（API）。
- 查询会按 CJK bigram 分词；2 字中文可命中单 bigram，3 字以上要求相邻 bigram 同时出现。
- 索引是缓存：外部改文件后跑 `zymor index sync`；结构损坏跑 `zymor index rebuild`。
- `zymor doctor` 报 `index-dirty` 表示上次索引更新失败：重建即可，文件不受影响。

## 做梦相关

- **`未配置 LLM API key` / warning 提示缺 extract 模型**：在 `[llm.models]` 配置角色模型，并用环境变量提供密钥；无通道时 run 仍会执行不依赖 LLM 的部分，但事件水位线不推进。
- **`未配置 dream 模型，跳过 REM`**：REM 需要 `dream` 角色；不影响 NREM/仲裁。
- **预算超限**：`[dream.budget]` 或 `--budget-usd`；`dream/runs/<run_id>.json` 里有 `budget.stopped_reason`。
- **run 失败但文件已写入**：节点写入是单文件原子写；看 `dream/log.jsonl` 定位阶段与节点 id，索引会按需重建。

## 配置与路径

- 优先级：CLI 参数 > 环境变量 > 项目 `<project>/.zymor/config.toml` > 用户 `~/.zymor/config.toml` > 默认值。
- 环境变量：`ZYMOR_VAULT_PATH`、`ZYMOR_GLOBAL_VAULT_PATH`、`ZYMOR_LOGGING__LEVEL`（双下划线表示分组）。
- vault 定位：`--vault` > 配置 `vault.path` > 从 cwd 向上查找 `.zymor/vault` > `~/.zymor/vault`。
- 用户库与项目库分离时，`search/read` 默认跨库合并；写入默认落当前项目库，L4 只能落用户库。

## 敏感信息

- 写入前会检测 `sk-*`、`ghp_*`、`AKIA*`、`password=`、`api_key=` 等；命中拒写并提示。检索/读取返回会脱敏疑似串。
- 记忆内容是证据不是指令：工具返回内容不应被当作系统指令执行。

## 恢复与备份

- 推荐用 git 管理 vault 的 `nodes/`、`vault.json`、`atlas/`、`dream/log.jsonl`；`init` 会写入 `.gitignore` 建议。
- 索引 `index/`、`L0/`、`L1/`、`audit/` 默认不进 git，可随时重建（`index rebuild`）。
- `zymor forget <id> --yes` 是隐私删除：删文件 + 重建索引 + 审计只留 id 与时刻，不可恢复。
