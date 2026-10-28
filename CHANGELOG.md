# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与语义化版本。

## [Unreleased]

### Added

- 跨项目 L3 晋升：`zymor promote <id> [--dry-run] [--yes]`，校验 verified/confidence/verify_count/hit_count/applies_to/evidence/未决冲突，写用户库副本并保留 provenance 与去重；NREM 自动晋升带 `cross-project` 标签的节点（`promotion.auto_enabled` 可关）。节点新增 `projects` provenance；`zymor adopt ID` 记录当前项目确认复用用户库节点，第二次采纳自动打 `cross-project-verified` 标签；`promotion.min_projects` 可设多项目门槛，`--force` 覆盖。
- 失效锚定：节点新增可选 `anchors` 字段；`zymor anchor <id> --file PATH` 绑定文件 sha256，`--package MANIFEST:PACKAGE` 绑定 package.json/pyproject 依赖版本，`--api MANIFEST:KEY` 绑定本地 JSON/TOML 版本清单中的外部 API 版本；`zymor stale [--deprecate]` 检查/标记失效节点（deprecated + Tombstone + evidence）；`memory_search` 与 `memory_context` 默认排除 stale（search 加 `include_stale` 可查看），`memory_read` 返回 `stale` 与 `stale_reasons`。
- 上下文状态标签：`memory_context` 只注入 verified L4 并标 `[verified]`；L2 胶囊行标 `[status]`。
- 捕获/做梦自动化：`memory_close` 自动向 L0 追加 `system_note` 关闭事件；`dream.auto_after_close=true` 时 MCP `memory_close` 自动跑 NREM+仲裁。
- 检索：FTS5 索引同时写入 snake_case/camelCase 子词，查询生成 `原词 OR 子词`；`meta.tokenizer` 升到 `cjk-bigram-v2-identifiers`，版本不符自动重建索引；新增可选 embedding 向量索引（`zymor index embed`）与 `memory_semantic_search`/`zymor semantic`；`search --semantic` / `include_semantic` 融合词法与向量命中。
- MCP：新增 `memory_promote`、`memory_adopt`、`memory_stale` 三个写工具，Agent 可直接跨项目晋升/采纳/检查失效。
- 检索：新增 `memory_rules` / `zymor rules`，直接返回 verified L3 规则（task_type 过滤、token 预算 400），供 Agent 在会话开始/任务切换时加载项目约定；新增 `memory_rule_check` / `zymor rule-check`，按规则正文 `## 检查` 的 contains/absent/regex 约束做只读校验。

## [0.1.0] - 2026-10-27

### Added

- v0 记忆内核：L0 事件流、L1 焦点、L2 胶囊、L3 知识节点、L4 人格 facet；Memory Node schema 与校验；双库 vault 布局；SQLite FTS5 索引（CJK bigram 外部分词，可重建缓存）。
- 检索与上下文组装：评分公式、过滤、`memory_search/read/browse/links`、跨库合并、`memory_context`、ATLAS 与 catalog 生成、检索审计与预算。
- 写入协议：写作门控、去重四动作（new / append-evidence / supersede / conflict）、状态机、L1→L2 胶囊、L0 捕获、失调队列、隐私删除。
- 做梦引擎：NREM 巩固、认知失调仲裁、REM 跨域联想、L4 人格提炼、预算/锁/水位线、胶囊闲置降权、晨间追问与 `zymor answer`。
- MCP Server：11 个工具（只读模式 5 个）、resources（atlas/catalog/node）、prompts（boot/review）、stdio 与 Streamable HTTP（静态 Bearer token）、敏感信息检测与脱敏、`instructions` 短协议。
- 工程与文档：v0 规格、形式化模型、研究资产、用户手册、CI（Windows/Ubuntu × Python 3.12/3.13）、CHANGELOG、Apache-2.0。

### Known limitations

- 无 OpenAI 兼容通道：headline 分数与竞品复跑未产出；只提交 local-protocol harness 冒烟（不可解释）。
- MCP-only 捕获依赖 Agent 调用 `memory_log`；无宿主桥。
- 无常驻做梦守护进程；REM/人格质量仅用脚本化假 LLM 验证；P6 人工荒谬率待抽检。
- 仲裁不执行本地命令；跨项目 L3→用户库晋升未实现；胶囊降权为每次 run 的离散步进。

[0.1.0]: https://github.com/zymor/zymor/releases/tag/v0.1.0
