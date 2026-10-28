# 发布与 PyPI Trusted Publishing

本文记录从版本号到 PyPI 的发布流程。目标：只通过 Git tag + GitHub Actions 发布，不在仓库或 CI 中保存 PyPI token。

## 1. 一次性准备

### 1.1 GitHub environment

仓库已创建环境 `pypi`，required reviewer 为 `@Gyanano`：发布 job 需要维护者在 GitHub 上人工批准。如需重建：

在 GitHub 仓库 **Settings → Environments** 新建环境：

- 名称：`pypi`
- 可添加 required reviewers（推荐，发布前人工确认）
- 不需要保存任何 PyPI secret

### 1.2 PyPI pending trusted publisher

首次发布前，在 PyPI 账号的 [Publishing](https://pypi.org/manage/account/publishing/) 页面创建 pending publisher：

| 字段 | 值 |
| --- | --- |
| PyPI Project Name | `zymor` |
| Owner | `Gyanano` |
| Repository name | `zymor` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

pending publisher 在第一次成功发布前不会占用项目名；发布成功后自动转为正式 publisher。之后不需要长期 token。

> 如果 `zymor` 已被别人注册，需要更换包名；先执行 `curl -s -o /dev/null -w "%{http_code}" https://pypi.org/pypi/zymor/json` 检查（404 表示可用）。

## 2. 每次发布

1. 确认 main 通过 CI，工作区干净。
2. 更新版本号（`pyproject.toml` 和 `CHANGELOG.md`）：

   ```bash
   uv version 0.2.0
   ```

3. 本地验证并构建：

   ```bash
   uv sync --extra dev
   uv run pytest tests/test_open_smoke.py -q
   uv run ruff check src tests
   uv run mypy src/zymor
   uv build
   ```

4. 通过 PR 合入版本变更；main 受 ruleset 保护。
5. 在 main 上打 tag 并推送：

   ```bash
   git tag -a v0.2.0 -m "Zymor v0.2.0"
   git push origin v0.2.0
   ```

6. `.github/workflows/release.yml` 自动：
   - 校验 tag 与 `pyproject.toml` 版本一致；
   - 构建 sdist/wheel；
   - 通过 OIDC 发布到 PyPI，无需 token。
7. 在 GitHub Releases 创建 release，正文来自 `CHANGELOG.md`。

## 3. 失败处理

- tag 与版本不一致：删除本地/远端 tag，修正版本后重新打 tag。
- PyPI publisher 不匹配：检查 owner/repo/workflow/environment 与 PyPI pending publisher 完全一致。
- 首次发布失败：`zymor` 名称不会被占用；修正后重新推 tag 即可。
- 发布后不要移动已发布 tag；tag ruleset 会阻止更新/删除。

## 4. 本地构建检查

```bash
uv build
uv run --with twine twine check dist/*
```

当前仓库已准备 release workflow，但首次 PyPI 发布需要先完成上面的 PyPI pending publisher 配置。
