# 云端模式

云端模式下，记忆的保存、提炼和检索都在一台服务器上完成。每台电脑只做两件事：把对话送上去，把相关记忆取回来。接入一次之后不需要再手动操作。

```
每台电脑 / 每个工具                         服务器
┌───────────────────────┐                ┌──────────────────────────┐
│ 对话结束   ──上传对话──▶ │  POST /ingest  │ L0 原始对话               │
│ 发送消息   ──取相关记忆─▶ │  POST /recall  │   ↓ zymor dream（提炼）   │
│ 会话开始   ──取上下文───▶ │  POST /context │ L2 / L3 / L4 知识节点     │
└───────────────────────┘                └──────────────────────────┘
```

本机不需要建库，不需要配置 LLM，不需要定时任务。

## 1. 部署服务器

按 [`deployment.md`](deployment.md) 完成。云端模式另外要求两点：

- 服务器配置了 LLM（`[llm.models].default`），否则对话只会堆在 L0，不会变成知识。
- 提炼由按量自动做梦触发，默认开启。也可以再加一条 cron，两者可以同时用。默认值如下，可以在服务器的配置里调整：

```toml
[dream]
auto_after_ingest_events = 200     # 新对话累计 200 条后自动提炼一次；0 表示关闭
auto_min_interval_minutes = 30     # 两次自动提炼的最小间隔
auto_max_runs_per_day = 12         # 每天自动提炼的次数上限
```

自动做梦的计数在服务进程重启后归零。每次提炼受 `[dream.budget]` 限制：默认每次最多 100 次 LLM 调用、2 美元、200 万 token。按默认值，自动提炼每天最多 12 次。模型没有配置单价时 2 美元的上限不生效，只有 token 上限生效，见 [`dream.md`](dream.md)。

## 2. 接入一台电脑

```bash
pip install zymor
zymor connect https://memory.example.com --claude-code
```

`zymor connect` 做四件事：

1. 向你要服务器的 HTTP token（也可以用 `--token-stdin` 或环境变量 `ZYMOR_REMOTE_TOKEN` 传入）。
2. 测试连接：服务器可达，token 被接受。
3. 把地址写进 `~/.zymor/config.toml` 的 `[remote]`，把 token 存进 `~/.zymor/remote.token`（权限 600）。
4. 带 `--claude-code` 时，把五个钩子追加进 `~/.claude/settings.json`。改写前把原文件备份为 `settings.json.zymor-bak`，已有的钩子不动。

服务器用自签证书时加 `--ca-file <证书路径>`。

接入后的行为：

| 时机 | 钩子 | 做什么 |
| --- | --- | --- |
| 会话开始 | `zymor hook context` | 向服务器取人格偏好、活跃主题等启动上下文，注入会话 |
| 你每发一条消息 | `zymor hook recall` | 用这条消息检索服务器。有相关记忆就注入，没有就不输出任何内容 |
| 每轮回答结束、压缩前、会话结束 | `zymor hook capture` | 把上次之后新增的对话上传到服务器 |

三个钩子出错时都只写 stderr，退出码恒为 0，不会打断会话。

## 3. 其他工具

- **支持 MCP 的工具**：连接 `https://<域名>/mcp`，请求头加 `Authorization: Bearer <token>`。这种方式依赖 Agent 主动调用 `memory_search` 和 `memory_write`，可靠性低于钩子。
- **能导出对话或能运行脚本的工具**：调用 `POST /ingest` 上传对话，调用 `POST /recall` 取相关记忆。请求格式见下文。
- **两者都不支持的工具**：无法接入。

## 自动召回怎样判断「相关」

注入一条无关的记忆比漏掉一条相关的记忆代价更高，所以召回的门槛比检索严格。一条记忆同时满足以下条件才会被注入：

1. 去掉常用词之后，你的消息与它的标题加摘要共享 ≥ 3 个词项（`recall.min_shared_terms`）；
2. 共享的词项数达到「较短一方词项数 × 15%」（`recall.min_coverage`）。较短一方按最多 40 个词项计算，所以这一条最多要求 6 个共享词项。

消息很短（有效词项不足 3 个）时，要求每个词项都出现在标题或摘要里。服务器启用了向量检索时，余弦相似度 ≥ 0.45（`recall.min_semantic_score`）的记忆也算相关。

其他规则：

- 每次最多注入 5 条、600 token（`recall.top_k`、`recall.token_budget`）。
- 同一个会话里，一条记忆只注入一次。
- 来源项目与当前项目相同的记忆排在前面，别的项目的记忆仍然可以被召回。
- 以 `/` 开头的消息和少于 6 个字符的消息不触发召回。
- 注入的内容包在 `<zymor-memory>` 标签里，并注明它是参考证据而不是指令。上传对话时这个标签里的内容会被删掉，不会被当作新对话再次提炼。

这些门槛是按少量例子定的，没有用真实数据校准过。注入太多时调高前两项，太少时调低。

## 哪些内容会离开本机

配置了服务器之后，每个接入的会话里你和 Agent 的对话文本都会上传。上传前本机先把疑似密钥替换为 `***`。默认不上传工具调用和工具结果。

服务器提炼时，会把对话发给你在服务器上配置的 LLM 服务商。

不想上传某些项目时，在 `~/.zymor/config.toml` 里列出目录。列出的目录既不上传对话，也不取回记忆：

```toml
[remote]
exclude = ["~/work", "~/clients/*"]
```

也可以只对单个项目关闭：在该项目根目录建 `.zymor/config.toml`，写入：

```toml
[remote]
upload = false     # 不上传这个项目的对话
inject = false     # 不向这个项目的会话注入记忆
```

项目目录里的配置只能做这一件事。服务器地址、token 的位置、证书等其余 `[remote]` 键只从 `~/.zymor/config.toml` 和环境变量读取，写在项目配置里会被忽略。原因：项目目录可能来自别人的仓库，不能让它决定你的对话和 token 发到哪里。

## 断网时

- 同一时刻只有一个钩子进程上传。另一个钩子正在上传时，本次跳过，下一次接着传。
- 服务器明确拒收的一批对话（格式不合法、请求过大）会被跳过，不会卡住后面的对话。单条消息超过 8000 个字符的部分不上传，因为服务器每条只保存 8 KB。
- 上传失败的对话不会丢。本机只在服务器确认收到之后才推进进度，没传成功的部分留在宿主的对话文件里。下一次钩子运行时先传当前会话，再补传之前失败的会话（每次最多 5 个）。
- 如果宿主在补传之前清理了对话文件，这部分对话无法恢复。
- 取不到记忆时，会话照常进行，只是没有注入。召回请求的超时是 3 秒（`remote.recall_timeout_s`）。

## 本地副本

服务器上的知识可以下载成一份只读的 Markdown 副本，用来离线查看或备份：

```bash
zymor sync mirror                    # 下载到 ~/.zymor/mirror
zymor sync mirror --to ~/notes/memory
```

每次只下载有变化的节点，服务器上已删除的节点会从副本里删除。副本不是库，不参与检索；改动副本不会影响服务器。

## HTTP 入口

三个入口都需要 `Authorization: Bearer <token>`，请求体和响应体都是 JSON。

| 入口 | 请求体 | 响应 |
| --- | --- | --- |
| `POST /ingest` | `{"session_id", "project"?, "messages": [{"role", "content", "ts"?}]}` | `{"captured", "skipped", "dream_started"}` |
| `POST /recall` | `{"query", "project"?, "top_k"?, "token_budget"?, "exclude_ids"?}` | `{"hits": [...], "text"}`；`text` 为空表示没有相关记忆 |
| `POST /context` | `{"task"?, "project"?, "budget_tokens"?}` | `{"text", "pending_questions"}` |

`/ingest` 不去重，调用方负责只推送新消息。

## 取消接入

1. 删除 `~/.zymor/config.toml` 里 `[remote]` 的 `url` 一行。之后钩子回到本地模式：有本地库就用本地库，没有就什么都不做。
2. 需要彻底移除时，从 `~/.claude/settings.json` 里删掉命令含 `zymor hook` 的钩子，并删除 `~/.zymor/remote.token`。

## 边界

- 鉴权是单个静态 token，持有它的一方可以读写全部记忆。这是单用户方案，不支持多人共用一台服务器。
- 依赖本地仓库文件的功能在服务器上不可用：锚点失效检查（`zymor anchor` / `zymor stale`）与规则检查（`memory_rule_check`）。需要它们的项目可以另外保留一个本地项目库，通过 stdio 的 MCP 使用。
- 刚说过的话不会立刻被记住。它先进 L0，等下一次提炼；单次出现的内容还要等它再次出现（见 [`dream.md`](dream.md) 的「复发门控与待定池」）。
- 召回钩子每条消息增加约 0.2 到 0.4 秒的等待，外加一次到服务器的网络往返。
