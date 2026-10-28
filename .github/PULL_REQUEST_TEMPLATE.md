## What changed

<!-- One or two sentences. Link the issue below. -->

Closes #

## Why

<!-- The problem this solves, not only the implementation. -->

## Verification

<!-- Commands and results. Do not include secrets. -->

```bash
uv sync --extra dev
uv run pytest tests/test_open_smoke.py -q
uv run ruff check src tests
uv run mypy src/zymor
```

## Checklist

- [ ] Behavior change has a test or a clear reason why it does not.
- [ ] CLI / MCP / config changes have docs updates.
- [ ] User-visible changes are in `CHANGELOG.md`.
- [ ] No secrets, private vaults, datasets, or generated caches are included.
- [ ] AI-assisted work was reviewed by the submitter.
