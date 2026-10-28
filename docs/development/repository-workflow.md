# 仓库工作流与开源协作规范

本文记录 Zymor 借鉴主流开源项目后采用的**最小但完整**的仓库管理流程。目标不是照搬 Kubernetes/Rust 的重型治理，而是让一个早期项目具备可预期、可审查、可发布的节奏。

## 1. 参考模型（只借鉴适用部分）

| 来源 | 值得借鉴 | 本项目采用方式 |
| --- | --- | --- |
| [GitHub Open Source Guides](https://opensource.guide/) | README、LICENSE、CONTRIBUTING、CODE_OF_CONDUCT、issue/PR 模板、安全策略 | 全部采用；见仓库根目录与 `.github/` |
| [Contributor Covenant 2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/) | 行为准则与举报渠道 | 采用，见 `CODE_OF_CONDUCT.md` |
| [Semantic Versioning](https://semver.org/) | `MAJOR.MINOR.PATCH`，0.x 允许快速演进 | 对外发布使用 SemVer；0.x 的 API 可能小步调整 |
| [Conventional Commits](https://www.conventionalcommits.org/) | 提交前缀驱动 changelog 与语义化版本 | 提交信息统一用 `feat/fix/docs/test/refactor/chore` |
| [Keep a Changelog](https://keepachangelog.com/) | 用户可读的版本变更 | `CHANGELOG.md` 的 Unreleased/版本小节 |
| Kubernetes | issue/PR 模板、OWNERS、发布分支、SIG 分工 | 早期只采用 issue/PR 模板与发布分支；OWNERS/SIG 等规模变大后再引入 |
| Rust | RFC/设计先行的文化、小步 review | 影响记忆模型/存储格式/MCP/CLI 的设计先开 issue 讨论，不静默改设计 |
| React / Python | 问题模板、可复现步骤、行为准则 | 采用 issue 模板与可复现要求；Python 的 PEP 思想用于“设计变更先记录” |

**原则**：流程只保留能实际执行的部分。项目变大后再增加独立 OWNERS、SIG、发布经理、多级审批。

## 2. Issue 流程

1. **先搜再开**：避免重复；bug 必须给版本/commit、复现步骤、期望行为。
2. **标签**：`bug`、`enhancement`、`documentation`、`security`、`good first issue`、`needs-design`。
3. **分流**：维护者尽量在几个工作日内确认/打标签；不承诺 SLA。
4. **设计类 issue**：涉及 L0–L4 模型、存储格式、MCP 工具、公共 CLI、迁移策略时，先达成设计结论，再写代码。
5. **安全类**：不走公开 issue，走 `SECURITY.md` 的私密渠道。

## 3. 分支与提交

- 从 `main` 开分支：`feat/<name>`、`fix/<name>`、`docs/<name>`、`chore/<name>`。
- 一个 PR 只做一件事；重构与行为改变分开。
- Conventional Commits：
  - `feat: ...` 用户可见能力；
  - `fix: ...` 缺陷修复；
  - `docs: ...` 文档；
  - `test: ...` 测试；
  - `refactor:` 不改变行为；
  - `chore:` 构建/依赖/杂项。
- 禁止提交：真实密钥、`.env.local`、个人 vault、数据集、缓存、生成产物。
- AI 辅助提交允许，但提交者负责复审和验证。

## 4. Pull Request 流程

PR 必须包含：

- 关联 issue（`Closes #...`）；
- 行为变化与验证方式；
- 新增/更新的测试（行为变化时）；
- CLI/MCP/配置变化对应的文档更新；
- 用户可见变化的 `CHANGELOG.md` 条目；
- 通过 `ruff`、`mypy`、最小 smoke 测试。

评审规则：

- 当前单人维护：至少 1 名维护者批准；
- 规模扩大后：核心路径 2 人批准；
- 合并策略：**squash merge**，删除分支；
- 评审开始后不要强推分支，除非维护者要求。

## 5. main 分支保护（仓库设置清单）

建议在 GitHub Settings 中开启：

- [x] Require a pull request before merging；
- [x] Require status checks：CI（ruff / mypy / smoke quick test）；
- [x] Require branches to be up to date before merging；
- [x] Do not allow bypassing the above settings（维护者紧急修复除外，需事后说明）；
- [x] Secret scanning / push protection（公开仓库）；
- [x] Private vulnerability reporting；
- [x] Dependabot alerts；
- [x] Delete head branches after merge。

## 6. 版本与发布

- SemVer：`0.x` 为快速演进；用户可见功能升 `MINOR`，修复升 `PATCH`，破坏性变更升 `MAJOR`（1.0 后）。
- 每次发布：
  1. 更新 `CHANGELOG.md`：把 Unreleased 移到 `[x.y.z] - YYYY-MM-DD`；
  2. 更新 `pyproject.toml` 的 `version`；
  3. 跑 smoke 测试与静态检查；
  4. 打 tag `vX.Y.Z`；
  5. 创建 GitHub Release，正文来自 changelog；
  6. 需要时发布 PyPI（当前尚未接入）。
- 发布分支：仅在需要维护旧版本时创建 `release/x.y`；早期项目直接从 `main` 发布。

## 7. 安全与依赖

- 安全问题走私密报告；修复后写 changelog，必要时发 advisory。
- Dependabot 更新依赖；升级后跑 CI 与 smoke。
- 不把 provider 名、私钥、真实 API URL 写进仓库；配置通过环境变量/`.env.example`。
- 外部 API 版本用本地 manifest 锚定，不依赖远端轮询。

## 8. 文档与设计变更

- 文档是当前事实源：改行为就改文档，不写“以前如何”的历史对比。
- 设计变更先开 issue/讨论，结论落在文档或 `CHANGELOG.md`；禁止实现中静默改设计。
- 公共接口（CLI 参数、MCP 工具、节点 frontmatter、存储格式）变化需明确迁移说明。

## 9. 贡献者成长

- 标记 `good first issue`：范围小、验收标准清晰；
- review 中解释“为什么”，不只给结论；
- 首次贡献者可先做文档/测试/小修复；
- 维护者定期把重复问题沉淀为文档或测试。

## 10. 当前项目的最小落地清单

- [x] `README.md` / `README.en.md`（中文默认 + 英文）
- [x] `LICENSE`（Apache-2.0）
- [x] `CONTRIBUTING.md`
- [x] `CODE_OF_CONDUCT.md`
- [x] `SECURITY.md`
- [x] `.github/ISSUE_TEMPLATE/`（bug / feature / config）
- [x] `.github/PULL_REQUEST_TEMPLATE.md`
- [x] `.github/workflows/ci.yml`（ruff + mypy + smoke）
- [x] `CHANGELOG.md`
- [ ] 分支保护（需在 GitHub Settings 手动开启）
- [ ] Dependabot / secret scanning（需在 GitHub Settings 手动开启）
- [ ] PyPI 发布（尚未接入）
