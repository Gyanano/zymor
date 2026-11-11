# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与语义化版本。

## [Unreleased]

### Added

- 对话捕获：`zymor ingest` 把 Claude Code 会话 JSONL 或通用 `{role, content}` 记录增量导入 L0；`zymor hook capture` 供宿主 Hook 调用；Claude Code 参考集成新增 `Stop` / `PreCompact` / `SessionEnd` 捕获钩子。导入前脱敏并去除宿主注入的包装块。
- HTTP 入口：`POST /ingest`（Bearer 鉴权，推送对话）与 `GET /healthz`。
- 云端模式：`zymor connect <地址> [--claude-code]` 把本机接到服务器并安装钩子；`zymor hook capture` 在配置了服务器时上传对话（带来源项目，失败后自动补传）；新增 `zymor hook recall`，每条用户消息按内容取回相关记忆；`zymor hook context` 在配置了服务器时从服务器取启动上下文。`[remote].exclude` 与项目级 `[remote]` 配置可按目录关闭上传与注入。
- HTTP 入口 `POST /recall` 与 `POST /context`；`POST /ingest` 接受 `project`。
- 自动召回的相关性门槛（`[recall]`）：只注入与消息足够相关的记忆，同一会话不重复注入。
- 按量自动做梦：HTTP 服务每累计收到 200 条新对话自动提炼一次，默认开启；`dream.auto_after_ingest_events`（设为 0 关闭）、`auto_min_interval_minutes`、`auto_max_runs_per_day`。
- `zymor sync mirror`：把服务器的库下载成本地只读 Markdown 副本。
- 检索支持 `prefer_project`：来源项目相同的节点加分，不过滤其他项目。
- 云端同步：`zymor sync [status|push|pull]` 与服务端 `POST /sync`。本地库是事实源，远端按分区保存文件副本；按 sha256 增量传输；多台电脑同步到同一分区后互相补齐；主动删除通过删除标记传播；冲突不自动覆盖。`--include-events` 可额外备份 L0（只上传）。
- HTTPS：`zymor mcp --host/--tls-cert/--tls-key` 与 `[mcp].tls_cert/tls_key/public_url/allowed_hosts/allow_insecure_http`。
- 待定候选池 `dream/pending.jsonl`：单次出现的候选等待后续会话复发，`dream.pending_retention_days` / `dream.pending_max` 可调。
- `[llm.models].default`：所有 LLM 角色的兜底模型；`dedupe` 角色补入默认配置。
- `dream.budget.max_tokens`：token 总量上限；run 报告新增 `unpriced_calls`。
- 文档：`docs/guide/cloud.md`、`docs/guide/capture.md`、`docs/guide/deployment.md`、`docs/guide/sync.md`、`docs/reference/configuration.md`。

### Security

- 项目目录里的 `.zymor/config.toml` 不能再设置 `[remote]` 与 `[sync]` 的服务器地址、token 位置、证书等键；它只能关闭本项目的上传与注入，或改同步分区名。否则，克隆下来的仓库可以借此把对话和 token 发往任意地址。
- 同步：远端的删除标记不会删除本地的 L0；一次删除超过本地一半节点时拒绝执行；`zymor sync mirror` 拒绝以一个库为目标，服务器返回空清单时不清空副本；同步路径与分区名改用完整匹配。
- 服务端边读边限制请求体大小；`zymor connect` 写入前重新解析配置，无法安全修改时拒绝写入；token 文件以 600 新建，不沿用旧权限；钩子命令路径加引号；钩子捕获所有异常。
- 包装块清理用线性扫描实现，大量未闭合标签不会拖慢导入。

### Changed

- 命令行入口改为 `zymor.__main__:main`：`zymor hook recall` 在配置了服务器时走轻量路径，不加载完整 CLI。升级后需要重新安装一次（`pip install -U zymor` 或 `uv sync`）。
- Claude Code 示例配置直接调用 `zymor hook ...`，并新增 `UserPromptSubmit` 钩子。

- 检索：全部词项命中的结果不足 `top_k` 时放宽为命中任一词项，并按相关度下限过滤；放宽得到的命中在 `why` 里标 `partial-match`。
- 混合检索改用倒数排名融合（RRF）；命中同时返回 `lexical_score` / `semantic_score`。**`score` 的取值范围变为 0 到 1 的融合分**，不再是词法综合分或余弦相似度。
- 语义检索从索引批量取元数据，不再为每个向量读一次节点文件；`zymor index embed` 只为新增或内容变过的节点计算向量（按被向量化文本的哈希判断）。
- 监听非回环地址且没有 TLS 时，`zymor mcp` 拒绝启动；对外监听要求 token ≥ 24 个字符。

### Fixed

- 放宽检索只追加部分命中的节点，不替换完全命中的结果；相关度下限按库分别计算。
- 对话格式自动识别逐条判断，首行是元数据的文件不会被整份跳过；被截短的文件不会从头重复导入。
- 待定池：重跑同一批事件不会被当作复发（按事件 seq 范围判断）；一条待定记录只并入一个簇；巩固没有写出节点时保留待定记录。
- L0 旧文件被清理后，新事件的 seq 接在水位线之后，不会落到水位线以下而永远不被消化。
- 上传：同机并发的钩子不会回退彼此的进度；重发的批次由服务器按 `batch_id` 去重；被服务器拒收的批次不会卡住后续对话。

- 复发门控不再永久丢弃跨会话的知识：此前单次候选被丢弃后水位线照常推进，同一件事在后续会话再次出现也无法被识别。
- 候选相关性判定不再把「共享任意一个词项」当作相关，无关候选不会再互相帮对方通过门控。
- 模型没有配置单价时 `max_usd` 形同虚设的问题：增加 token 上限兜底并给出提示。
- 没有任何可用 LLM 角色时，`zymor dream` 给出明确提示，不再静默跳过。
- HTTP token 改用定长时间比较。

## [0.5.0] - 2026-10-29

### Added

- 规则执行/修复闭环：`memory_rule_check` 支持 content/attempt/repair instructions、`rules.jsonl` 审计。
- 文件捕获：`zymor capture`，持久化快照与待处理队列，默认只监听项目库。
- Bundle 导出/导入、`MemoryAdapter` 契约、Harness Hooks、本地只读面板。
- 自定义 `--vault` 在 `vault.json` 记录 `project_root`。

### Fixed

- L0 seq 改为会话级跨日期单调，cleanup 保留未消费事件。
- `export` 跳过 `_catalog.md` 等生成物；import 先校验再写入。
- 全新 vault 的 `zymor doctor` 自动初始化索引。
- rule-check 未匹配文件改为 skip，CLI 路径与 MCP/harness 一致。
- stats 恢复 conflict/supersede/gate/degraded 动作计数。
- 敏感信息检测下沉到 `write_node`，CLI search/read 输出脱敏。
- capture 跨重启保留快照与待处理事件，拒绝监听用户库。
- panel 搜索不再写检索审计；端口/参数错误返回可操作错误。
- 只读 MCP `memory_read` 不再递增 hit_count 或写检索审计。
- 只读 MCP `memory_search` 不再写检索审计；`update_node` 也执行敏感检测。
- vault 记录项目根的绝对与相对位置，目录迁移后仍能解析；子目录 init 记录真实项目根。
- rules 审计补 `skipped_checks` / `unmatched`；capture 状态目录加入 vault `.gitignore`。
- dream/写入锁增加初始化宽限与 stale 恢复；run 记录 `phases` 改为实际执行阶段。
- stats 的 dream 失败数与最近时间按状态/时区正确计算。
- `ruff format --check` 恢复通过。

## [0.2.1] - 2026-10-28

### Fixed

- `zymor --version` 现在从安装包元数据读取版本；0.2.0 wheel 中该值仍是旧的 0.1.0。

## [0.2.0] - 2026-10-28

### Added

- 跨项目 L3 晋升：`zymor promote <id> [--dry-run] [--yes]`，校验 verified/confidence/verify_count/hit_count/applies_to/evidence/未决冲突，写用户库副本并保留 provenance 与去重；NREM 自动晋升带 `cross-project` 标签的节点（`promotion.auto_enabled` 可关）。节点新增 `projects` provenance；`zymor adopt ID` 记录当前项目确认复用用户库节点，第二次采纳自动打 `cross-project-verified` 标签；`promotion.min_projects` 可设多项目门槛，`--force` 覆盖。
- 失效锚定：节点新增可选 `anchors` 字段；`zymor anchor <id> --file PATH` 绑定文件 sha256，`--package MANIFEST:PACKAGE` 绑定 package.json/pyproject 依赖版本，`--api MANIFEST:KEY` 绑定本地 JSON/TOML 版本清单中的外部 API 版本；`zymor stale [--deprecate]` 检查/标记失效节点（deprecated + Tombstone + evidence）；`memory_search` 与 `memory_context` 默认排除 stale（search 加 `include_stale` 可查看），`memory_read` 返回 `stale` 与 `stale_reasons`。
- 上下文状态标签：`memory_context` 只注入 verified L4 并标 `[verified]`；L2 胶囊行标 `[status]`。
- 捕获/做梦自动化：`memory_close` 自动向 L0 追加 `system_note` 关闭事件；`dream.auto_after_close=true` 时 MCP `memory_close` 自动跑 NREM+仲裁。
- 检索：FTS5 索引同时写入 snake_case/camelCase 子词，查询生成 `原词 OR 子词`；`meta.tokenizer` 升到 `cjk-bigram-v2-identifiers`，版本不符自动重建索引；新增可选 embedding 向量索引（`zymor index embed`）与 `memory_semantic_search`/`zymor semantic`；`search --semantic` / `include_semantic` 融合词法与向量命中。
- MCP：新增 `memory_promote`、`memory_adopt`、`memory_stale` 三个写工具，Agent 可直接跨项目晋升/采纳/检查失效。
- 检索：新增 `memory_rules` / `zymor rules`，直接返回 verified L3 规则（task_type 过滤、token 预算 400），供 Agent 在会话开始/任务切换时加载项目约定；新增 `memory_rule_check` / `zymor rule-check`，按规则正文 `## 检查` 的 contains/absent/regex 约束做只读校验。
- 开源发布工程：最小功能冒烟测试、GitHub Actions CI（ruff / mypy / Python 3.12 + 3.13）、Dependabot（uv + GitHub Actions）、CodeQL、main/tag ruleset、PyPI Trusted Publishing workflow。
- 协作与发布文档：中英双语 README、主题自适应 Logo、CONTRIBUTING、SECURITY、CODE_OF_CONDUCT、issue/PR 模板、仓库工作流与发布指南。

## [0.1.0] - 2026-10-27

### Added

- v0 记忆内核：L0 事件流、L1 焦点、L2 胶囊、L3 知识节点、L4 人格 facet；Memory Node schema 与校验；双库 vault 布局；SQLite FTS5 索引（CJK bigram 外部分词，可重建缓存）。
- 检索与上下文组装：评分公式、过滤、`memory_search/read/browse/links`、跨库合并、`memory_context`、ATLAS 与 catalog 生成、检索审计与预算。
- 写入协议：写作门控、去重四动作（new / append-evidence / supersede / conflict）、状态机、L1→L2 胶囊、L0 捕获、失调队列、隐私删除。
- 做梦引擎：NREM 巩固、认知失调仲裁、REM 跨域联想、L4 人格提炼、预算/锁/水位线、胶囊闲置降权、晨间追问与 `zymor answer`。
- MCP Server：11 个工具（只读模式 5 个）、resources（atlas/catalog/node）、prompts（boot/review）、stdio 与 Streamable HTTP（静态 Bearer token）、敏感信息检测与脱敏、`instructions` 短协议。
- 工程与文档：v0 规格、形式化模型、研究资产、用户手册、CI（Windows/Ubuntu × Python 3.12/3.13）、CHANGELOG、Apache-2.0。

### Known limitations

- 无 OpenAI 兼容通道时，headline 分数与竞品复跑不可用；只提交 local-protocol harness 冒烟。
- 捕获通过 `zymor capture`、CLI `memory_log` 或宿主 Hook 显式触发，不是全自动后台服务。
- `zymor dream --watch` 是前台循环，无常驻守护进程；胶囊降权按 run 离散步进。
- 仲裁不执行本地命令。

[0.1.0]: https://github.com/Gyanano/zymor/releases/tag/v0.1.0
[0.2.0]: https://github.com/Gyanano/zymor/releases/tag/v0.2.0
[0.2.1]: https://github.com/Gyanano/zymor/releases/tag/v0.2.1
[0.5.0]: https://github.com/Gyanano/zymor/releases/tag/v0.5.0
