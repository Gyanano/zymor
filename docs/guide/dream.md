# 做梦（Dream）

做梦是 Zymor 的离线巩固层，四个阶段：**NREM 清洗与巩固 → 认知失调仲裁 → REM 跨域联想 → 人格提炼（L4）**。v0 不内置常驻守护进程；每次 `zymor dream` 是可重复、幂等、可中断、有预算的一次 run。

## 运行

```bash
uv run zymor dream --dry-run                 # 计划：事件量、候选、预计 LLM 调用
uv run zymor dream                           # NREM + 仲裁 + REM（按配置启用）
uv run zymor dream --phase nrem              # 单独阶段
uv run zymor dream --phase arbitration
uv run zymor dream --budget-usd 0.5          # 覆盖本次费用上限
uv run zymor dream --watch --interval 300    # 可选：循环运行，Ctrl+C 停止
```

- 锁：`<vault>/.dream.lock`，同一 vault 同时只跑一个 run；stale 锁超过 2 小时自动清理。
- 水位线：`dream/watermarks.json` 记录每会话 `last_seq`；只处理水位线之后的 L0 事件。
- 预算：`[dream.budget]` 的 `max_llm_calls / max_usd / max_nodes_changed` 超限后优雅停止。
- 审计：每次 run 写 `dream/runs/<run_id>.json` 与 `dream/log.jsonl`；收尾刷新 ATLAS/catalog。
- 历史：`zymor dream --history 10` 列出最近 run 的 ok/status/phases；`zymor stats` 汇总 run 数、失败数与最近时间。

## 阶段做什么

| 阶段 | 输入 | 产出 |
| --- | --- | --- |
| NREM | 未消化 L0 事件、近期 hypothesis 节点、待定池 | 复发门控后的候选节点；四动作（new / append-evidence / supersede / conflict）；L2→L3 晋升；过期焦点补胶囊 |
| 仲裁 | disputed 节点 + 晨间队列 | 四裁决（uphold-new / uphold-old / defer / unverifiable）；旧节点 Tombstone；队列 attempts/state |
| REM | 近期 L2/L3 × 远期跨域 L3 | 共享可观察结构的同构洞察（L3 semantic hypothesis，confidence 0.3） |
| 人格 | user_message 中的纠正/偏好/重复模式 | 满足「≥2 会话 + 用户确认」的 L4 facet；未确认规则进晨间队列（draft L4 hypothesis） |

## 复发门控与待定池

NREM 只把「出现过不止一次」的候选写成节点。判定规则：

1. 候选与既有节点相关，或与同一批的另一个候选相关 → 进入巩固。
2. 候选与待定池里来自别的事件的记录相关 → 两者合并后进入巩固，该记录移出待定池。
3. 以上都不满足 → 候选写入待定池 `dream/pending.jsonl`，不生成节点。

「相关」的门槛：标题加摘要的词项中，共享词项 ≥ 3 个，且共享数占较短一方的 40% 以上；双方有相同标签时放宽为 ≥ 2 个且 20% 以上。只共享一个词项不算相关。

待定记录默认保留 90 天（`dream.pending_retention_days`），最多 500 条（`dream.pending_max`），超出时丢弃最旧的。`dream.eager_singletons = true` 时跳过门控，单次候选直接写成 hypothesis。

同一批事件重跑时提取出的同一个候选，不算它自己的复发。

## 晨间追问

冲突对与待确认人格规则写入 `dream/dissonance.md`；`memory_context` 会把队列前 3 条摘要带给宿主。答复：

```bash
uv run zymor answer Q1 --text "采纳新规则"                  # 冲突对：追加证据并置 answered
uv run zymor answer Q2 --text "确认" --decision accept      # 人格草案：draft 升 verified
uv run zymor answer Q2 --text "不采纳" --decision dismiss
uv run zymor answer Q2 --text "再想想" --decision defer      # attempts +1
```

## 配置（节选）

```toml
[dream]
batch_events = 200
max_events_per_run = 2000
max_candidates = 300
max_arbitrations = 20
max_arbitration_attempts = 3
eager_singletons = false
l0_retention_days = 90

[dream.updates]
alpha = 0.40          # 确认后 confidence ← min(0.99, c + (1-c)·α)
arousal_lambda = 0.7  # 确认后 arousal ← max(0.1, a·λ)；胶囊闲置降权同样用 λ
arousal_min = 0.1
confidence_cap = 0.99

[dream.rem]
enabled = true
max_pairs = 3

[dream.budget]
max_llm_calls = 100
max_usd = 2
max_tokens = 2000000   # 输入加输出的 token 总量上限
max_nodes_changed = 200
```

`max_usd` 按 `[llm.pricing."<模型名>"]` 的单价估算费用。模型没有配置单价时费用按 0 计，`max_usd` 不会触发，此时由 `max_tokens` 限流；run 报告会给出一条提示并在 `budget.unpriced_calls` 里计数。

空闲超过 `[lifecycle].capsule_downweight_after_days`（默认 30 天）的 L2 胶囊，每次真实 run 会把 arousal 乘一次 λ（下限 `arousal_min`），不推进 `updated_at`、不改状态。

## LLM 角色

| 角色 | 用途 |
| --- | --- |
| `extract` | NREM 事件 → 候选 |
| `dedupe` | 候选簇四选一（门控与去重合并为同一次调用） |
| `arbitration` | 失调裁决（建议强模型） |
| `dream` | REM 联想与人格提炼 |

配置见 `[llm.models]`：`default` 是所有角色的兜底模型，只配这一项即可运行；也可以按角色单独指定。每个角色可用 `[llm.roles.<role>]` 覆盖 `model/base_url/api_key_env`。无通道时做梦不伪造结果。

## 失败与边界

- 单批失败重试 2 次仍失败：记录并跳过该批，该会话水位线不推进。
- 节点写入失败只回滚该节点；`--dry-run` 永不写文件、不调 LLM。
- L0 保留期到期的日期文件在成功 run 后清理；节点不受影响。
- 已知限制：只读测试命令执行、跨项目 L3→用户库晋升、胶囊降权的连续时间函数不在 v0（见 README 已知限制）。
