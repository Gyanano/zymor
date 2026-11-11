# 配置项参考

配置按以下顺序合并，后者覆盖前者：内置默认值 → `~/.zymor/config.toml` → `<项目>/.zymor/config.toml` → 环境变量 → 命令行参数。

项目目录可能来自别人的仓库，所以项目级配置里以下键不生效，只能写在用户级配置或环境变量里：

- `[remote]` 的全部键，`upload = false` 与 `inject = false` 除外；
- `vault.global_path`；
- `[sync]` 的全部键，`scope` 除外；
- `[mcp]` 里决定监听地址、TLS、token 与 HTTP 入口的键（`transport`、`host`、`port`、`http_token_env`、`tls_cert`、`tls_key`、`public_url`、`allowed_hosts`、`allow_insecure_http`、`ingest_endpoint`、`sync_endpoint`）；
- `llm.base_url`、`llm.api_key_env`、`[llm.roles.<角色>]` 下的这两个键；
- `embedding.base_url`、`embedding.api_key_env`。

环境变量的写法：`ZYMOR_<组>__<键>`，层级之间用两个下划线，例如 `ZYMOR_MCP__PUBLIC_URL`、`ZYMOR_LLM__MODELS__DEFAULT`。数组类型的键（如 `mcp.allowed_hosts`）只能写在配置文件里。

本页列出常用的键。完整的默认值以 `src/zymor/config.py` 的 `DEFAULTS` 为准。

## `[vault]`

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `path` | 无 | 项目库路径。不设时从当前目录向上查找 `.zymor/vault` |
| `global_path` | `~/.zymor/vault` | 用户库路径 |

## `[llm]`

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `base_url` | `https://api.openai.com/v1` | OpenAI 兼容接口地址 |
| `api_key_env` | `OPENAI_API_KEY` | 存放 API key 的环境变量名 |
| `timeout_s` | 60 | 单次请求超时秒数 |
| `max_retries` | 3 | 429、5xx、超时的重试次数 |
| `models.default` | 无 | 所有角色的兜底模型 |
| `models.extract` | 无 | NREM：事件 → 候选 |
| `models.dedupe` | 无 | 候选巩固四选一；MCP 写入时的去重 |
| `models.arbitration` | 无 | 矛盾仲裁 |
| `models.dream` | 无 | REM 联想与人格提炼 |
| `pricing."<模型名>".input_per_1m` / `output_per_1m` | 无 | 每百万 token 的美元单价，用于估算费用 |

单个角色可以用 `[llm.roles.<角色>]` 覆盖 `model`、`base_url`、`api_key_env`、`timeout_s`、`max_retries`。

## `[embedding]`

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `enabled` | `false` | 是否启用向量检索 |
| `model` | `text-embedding-3-small` | 向量模型 |
| `base_url` | 同 `llm.base_url` | 向量接口地址 |
| `min_score` | 0.2 | 余弦相似度下限，低于它的节点不返回 |
| `batch_size` | 64 | 单次请求的节点数 |

## `[retrieval]`

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `weights.bm25` / `confidence` / `recency` / `arousal` / `hits` | 0.55 / 0.15 / 0.15 / 0.10 / 0.05 | 词法综合分的各项权重 |
| `half_life_days` | 30 | 时效分的半衰期 |
| `default_top_k` | 8 | 默认返回条数 |
| `token_budget` | 1200 | 单次检索结果的 token 上限 |

## `[context]`

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `total_tokens` | 1500 | `memory_context` 的总 token 上限 |
| `l4_tokens` / `l1_tokens` / `l2_tokens` | 200 / 500 / 400 | 人格、焦点、胶囊各段的上限 |
| `max_facets` / `max_capsules` | 3 / 8 | 注入的人格条数与胶囊条数上限 |

## `[dream]`

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `allow_tool_trigger` | `true` | 是否允许 MCP 工具 `memory_dream` 触发做梦 |
| `auto_after_close` | `false` | `memory_close` 之后是否自动运行 NREM 与仲裁 |
| `auto_after_ingest_events` | 200 | HTTP 服务累计收到多少条新对话后自动提炼一次；0 表示关闭 |
| `auto_min_interval_minutes` | 30 | 两次自动提炼的最小间隔 |
| `auto_max_runs_per_day` | 12 | 每天自动提炼的次数上限 |
| `batch_events` | 200 | 每次提取调用处理的事件数 |
| `max_events_per_run` | 2000 | 单次 run 处理的事件上限 |
| `l0_retention_days` | 90 | L0 日期文件的保留天数 |
| `eager_singletons` | `false` | 单次出现的候选是否跳过门控直接写入 |
| `pending_retention_days` | 90 | 待定候选的保留天数 |
| `pending_max` | 500 | 待定池容量，超出时丢弃最旧的 |
| `max_arbitrations` | 20 | 单次 run 的仲裁数上限 |
| `rem.enabled` | `true` | 是否运行 REM 联想 |
| `budget.max_llm_calls` | 100 | 单次 run 的 LLM 调用数上限 |
| `budget.max_usd` | 2 | 单次 run 的费用上限；依赖 `llm.pricing` |
| `budget.max_tokens` | 2000000 | 单次 run 的 token 总量上限 |
| `budget.max_nodes_changed` | 200 | 单次 run 的节点变更数上限 |

## `[mcp]`

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `transport` | `stdio` | `stdio` 或 `http` |
| `mode` | `rw` | `ro` 时不下发写工具 |
| `host` / `port` | `127.0.0.1` / 8765 | HTTP 监听地址 |
| `http_token_env` | `ZYMOR_MCP_HTTP_TOKEN` | 存放 Bearer token 的环境变量名 |
| `tls_cert` / `tls_key` | 无 | PEM 证书链与私钥路径；两者必须同时设置 |
| `public_url` | 按监听地址生成 | 客户端访问的地址；它的域名自动加入 Host 允许名单 |
| `allowed_hosts` | `[]` | 额外允许的 Host 头 |
| `allow_insecure_http` | `false` | 是否允许不加密地监听非回环地址 |
| `ingest_endpoint` | `true` | 是否提供 `POST /ingest` |
| `sync_endpoint` | `true` | 是否提供 `POST /sync` |

## `[remote]`

本机作为客户端连接服务器时使用。由 `zymor connect` 写入。只从用户级配置和环境变量读取；项目级配置里只有 `upload = false` 和 `inject = false` 生效。

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `url` | 无 | 服务器地址，必须是 `https`。设置后钩子进入云端模式 |
| `token_env` | `ZYMOR_REMOTE_TOKEN` | 存放 token 的环境变量名，优先于 token 文件 |
| `token_file` | `~/.zymor/remote.token` | token 文件路径 |
| `ca_file` | 无 | 自签证书的 CA 文件路径 |
| `upload` | `true` | 是否上传对话 |
| `inject` | `true` | 是否向会话注入记忆 |
| `exclude` | `[]` | 既不上传也不注入的目录，支持通配符 |
| `timeout_s` | 8 | 上传与取上下文的超时秒数 |
| `recall_timeout_s` | 3 | 每条消息召回的超时秒数 |
| `recall_min_chars` | 6 | 短于这个长度的消息不触发召回 |
| `allow_insecure_http` | `false` | 是否允许用 `http` 连接非本机地址 |

## `[recall]`

服务器判断「哪些记忆值得自动注入」时使用。

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `top_k` | 5 | 每次最多注入的条数 |
| `token_budget` | 600 | 每次注入的 token 上限 |
| `min_shared_terms` | 3 | 消息与记忆的标题加摘要至少共享的词项数 |
| `min_coverage` | 0.15 | 共享词项数与较短一方词项数（最多按 40 计）的最低比例 |
| `min_semantic_score` | 0.45 | 启用向量检索时，语义相似度的最低值 |

## `[sync]`

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `url` | 沿用 `remote.url` | 同步服务器地址，必须是 `https` |
| `token_env` | `ZYMOR_SYNC_TOKEN` | 存放 Bearer token 的环境变量名；没设置时沿用 `[remote]` 的 token |
| `scope` | 按库自动取 | 远端分区名：用户库为 `global`，项目库为 `project-<项目名>` |
| `include_events` | `false` | 是否同时上传 L0 原始对话 |
| `ca_file` | 无 | 自签证书的 CA 文件路径 |
| `allow_insecure_http` | `false` | 是否允许用 `http` 连接非本机地址 |

## `[promotion]` 与 `[lifecycle]`

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `promotion.min_confidence` | 0.85 | 跨项目晋升的置信度下限 |
| `promotion.min_verify_count` / `min_hit_count` | 3 / 3 | 验证次数与命中次数下限 |
| `promotion.min_projects` | 1 | 需要多少个项目确认过 |
| `lifecycle.focus_idle_close_after_hours` | 72 | 焦点闲置多久后由做梦补写胶囊 |
| `lifecycle.capsule_downweight_after_days` | 30 | 胶囊闲置多久后开始降权 |
