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

## 5. main 分支保护（当前设置）

GitHub 仓库已启用规则集 `Protect main`（默认分支）：

- [x] Require a pull request before merging；
- [x] Require status checks：`ruff + mypy + smoke (Python 3.12)`、`ruff + mypy + smoke (Python 3.13)`；
- [x] Require code scanning results：CodeQL，Security alerts `High or higher`，Alerts `Errors`；
- [x] Require branches to be up to date before merging；
- [x] Allowed merge methods：仅 Squash；
- [x] Block force pushes；
- [x] Restrict deletions；
- [x] Bypass list 为空；
- [ ] Require signed commits（可选，当前未启用）。

单人维护阶段 Required approvals 为 `0`，允许维护者自己合并；引入第二名常驻维护者后改为 `1`，并启用 up-to-date 与第二人 review。

发布 tag 由独立规则集 `Protect release tags` 保护：

- [x] Target：`v*`；
- [x] Restrict updates（已发布 tag 不可移动）；
- [x] Restrict deletions（已发布 tag 不可删除）；
- [x] Bypass list 为空。

安全与依赖设置（Advanced Security）：

- [x] Secret scanning + push protection；
- [x] Private vulnerability reporting；
- [x] Dependabot alerts + malware alerts + security updates；
- [x] CodeQL default setup（GitHub Actions + Python）；
- [x] Dependabot auto-triage 默认规则。

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
- 发布自动化与 PyPI Trusted Publishing 见 [`release.md`](release.md)。

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
- [x] GitHub ruleset `Protect main`（PR + 状态检查 + 禁止强推/删除）
- [x] Secret scanning / push protection
- [x] Private vulnerability reporting
- [x] Dependabot alerts / security updates
- [x] CodeQL default setup
- [x] `.github/dependabot.yml`（uv + GitHub Actions 每周更新）
- [x] `.github/workflows/release.yml`（tag 触发构建 + PyPI OIDC 发布）
- [x] tag ruleset `Protect release tags`（`v*`）
- [x] CodeQL merge gate（CodeQL / High+ / Errors）
- [x] GitHub environment `pypi` + required reviewer
- [ ] PyPI pending publisher / 首次发布（需要 PyPI 账号配置）
- [x] `CHANGELOG.md`
- [ ] PyPI 发布（尚未接入）
