# Contributing to Zymor

Thanks for your interest. This project favors small, verifiable changes over large rewrites.

## Before you start

- For bugs, open an issue using the bug report template.
- For features, open a feature request first and describe the problem, not only the solution.
- For security issues, do **not** open a public issue; follow [`SECURITY.md`](SECURITY.md).
- For design changes that affect the memory model, storage format, MCP tools, or public CLI, open an issue first and get agreement before writing code.

## Development setup

```bash
uv sync --extra dev
uv run pytest tests/test_open_smoke.py -q
uv run ruff check src tests
uv run mypy src/zymor
```

The public repository ships a minimal functional smoke suite. Full evaluation harnesses and datasets are intentionally not part of the open-source release.

## Branch and commits

- Branch from `main`: `feat/<short-name>`, `fix/<short-name>`, `docs/<short-name>`, `chore/<short-name>`.
- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.
- Keep commits reviewable; avoid mixing refactors and behavior changes.
- Do not commit secrets, local vaults, `.env.local`, datasets, or generated caches.
- AI-assisted contributions are welcome; you are responsible for reviewing and testing the result.

## Pull requests

A good PR:

- links the issue (`Closes #123`);
- states the behavior change and how it was verified;
- adds or updates a test when behavior changes;
- updates user-facing docs when CLI/MCP behavior changes;
- updates `CHANGELOG.md` for user-visible changes;
- passes CI (`ruff`, `mypy`, smoke tests).

The maintainer merges with squash merge. Please do not force-push a branch after review starts unless asked.

## Code and documentation rules

- Code comments are intentionally sparse; prefer self-explanatory code and short interface docstrings.
- Documentation is the source of current behavior: update it instead of describing history.
- Do not add network calls, API keys, or provider-specific endpoints to tests.
- Do not add evaluation data or test reports to this repository.

## Review and release

- One maintainer review is required while the project has a single maintainer; this may become two approvals later.
- Releases follow [Semantic Versioning](https://semver.org/) and [Keep a Changelog](https://keepachangelog.com/).
- Release tags use `vMAJOR.MINOR.PATCH`; release notes come from `CHANGELOG.md`.

## Community

By participating you agree to follow [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
