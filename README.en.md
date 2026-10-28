<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/zymor-horizontal-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/zymor-horizontal-light.svg">
    <img alt="Zymor Logo" src="assets/zymor-horizontal-light.svg" width="800">
  </picture>
</p>

**Layered, Long-Lived Memory Engine for AI Agents**

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-Server-6f42c1)](https://modelcontextprotocol.io/)
[![Version](https://img.shields.io/badge/Version-v0.1.0-brightgreen)](CHANGELOG.md)
![Storage](https://img.shields.io/badge/Storage-Markdown%20%2B%20SQLite-4b8bbe)

[中文](README.md) | **English**

**Zymor** is a harness-agnostic memory system for AI agents. Experience is captured as searchable, correctable, portable layered memory; a nightly dream engine consolidates it, resolves contradictions, and distills working style. The vault is plain Markdown plus a rebuildable SQLite FTS5 index — readable, diffable, portable.

## Status

**v0.1.0 — memory kernel complete.** The current implementation covers:

- **Memory kernel**: L0 event stream, L1 focus, L2 capsules, L3 knowledge nodes, L4 persona facets;
- **Retrieval and context**: FTS5 with identifier subtokens, scoring/filtering, progressive disclosure, ATLAS/catalog;
- **Write protocol**: gate, dedupe, state machine, audit, privacy deletion;
- **Dream engine**: NREM consolidation, dissonance arbitration, REM association, persona distillation;
- **MCP server**: stdio / Streamable HTTP, read-write and read-only modes;
- **Long-term memory**: cross-project L3 promotion, `projects` provenance, file/dependency/API-version anchors, stale exclusion, optional embedding semantic search and hybrid retrieval, `memory_rules` and `memory_rule_check`.

## Why Zymor

- **Layered, not a vector soup**: an always-on micro-kernel (persona facets + current focus + active capsules) plus pointer indexes; deeper memory unfolds on demand.
- **Correctable, not append-only**: contradictions are verified, superseded, or tombstoned instead of silently overwritten.
- **Dreaming, not just retrieval**: offline consolidation, arbitration, cross-domain association, and persona distillation.
- **Progressive disclosure**: directory navigation + precise retrieval + link following, instead of stuffing the whole vault into context.

## Quick start

```bash
git clone <repo> zymor && cd zymor
uv sync

cd my-project
uv run --project <repo> zymor init            # project vault: ./.zymor/vault
uv run --project <repo> zymor init --global   # user vault: ~/.zymor/vault
uv run --project <repo> zymor remember "Use CJK bigrams for Chinese retrieval"
uv run --project <repo> zymor search "bigram"
uv run --project <repo> zymor dream --phase all
uv run --project <repo> zymor mcp             # MCP server (stdio)
```

## Architecture

```
Host agent / MCP client
        │  memory_context / rules / search / semantic_search / read / ...
        ▼
┌──────────────────────────────────────────────┐
│  Zymor MCP Server                            │
│  write protocol · retrieval · dream engine   │
└──────────────────────────────────────────────┘
        │
        ▼
  .zymor/vault/                ~/.zymor/vault/
  L0 / L1 / L2 / L3            L4 + promoted L3
  Markdown facts + SQLite FTS5 index
```

- **Project vault**: `<project>/.zymor/vault`; **user vault**: `~/.zymor/vault`.
- L4 persona facets live only in the user vault; L3 stays in the project vault until cross-project promotion.
- Files are the source of truth; the index is a rebuildable cache.

## CLI

| Command | Purpose |
| --- | --- |
| `zymor init [--global]` | initialize the project/user vault |
| `zymor write --file PATH \| --stdin` | write a full Markdown node |
| `zymor remember TEXT [--category ...]` | quick capture through the write protocol |
| `zymor search QUERY [--semantic]` | retrieval (optional hybrid semantic) |
| `zymor semantic QUERY` | embedding-based semantic search |
| `zymor read ID` | read a node |
| `zymor log --kind KIND --content TEXT` | append an L0 event |
| `zymor focus --session ID` | view/update the L1 focus file |
| `zymor index sync\|rebuild\|catalog\|embed` | index, catalog, and vector maintenance |
| `zymor rules / rule-check` | load/execute verified project rules |
| `zymor review / answer` | human review and morning questions |
| `zymor promote / adopt / anchor / stale` | cross-project promotion, adoption, staleness anchors |
| `zymor dream [--phase ...]` | run the dream engine |
| `zymor mcp` | start the MCP server |
| `zymor doctor / stats` | self-check and statistics |

## MCP integration

```bash
uv run zymor mcp                       # stdio (local host)
export ZYMOR_MCP_HTTP_TOKEN=<random>
uv run zymor mcp --transport http      # Streamable HTTP
uv run zymor mcp --read-only           # read-only mode
```

Claude Code:

```bash
claude mcp add zymor -- uv run --directory <ZYMOR_REPO> zymor mcp
```

Full DSH / Cursor configuration and acceptance steps: [`docs/guide/mcp-integration.md`](docs/guide/mcp-integration.md).

MCP tools: `memory_context`, `memory_rules`, `memory_rule_check`, `memory_search`, `memory_semantic_search`, `memory_read`, `memory_browse`, `memory_links`, `memory_write`, `memory_focus`, `memory_close`, `memory_log`, `memory_answer`, `memory_dream`, `memory_promote`, `memory_adopt`, `memory_stale`.

## Testing

The minimal smoke suite checks basic functionality only (write/search/read/context/stale anchors/rule checks/cross-project promotion), not quality or effectiveness:

```bash
uv sync --extra dev
uv run pytest tests/test_open_smoke.py -q
```

## Documentation

| Path | Contents |
| --- | --- |
| `docs/guide/` | Quick start, dreaming, MCP integration, rule checks, troubleshooting |
| `GLOSSARY.md` | Domain vocabulary |
| `CHANGELOG.md` | Release changes |

## Known limitations

- **No background daemon**: dreaming is triggered by CLI, `memory_dream`, or `dream.auto_after_close=true` on close.
- **L0 capture depends on the agent**: `memory_close` adds a session-close event, but event-level capture still requires `memory_log`.
- **Rule checks are explicit**: `memory_rules` loads and `memory_rule_check` verifies; no check runs unless the agent calls it.
- **Cross-project promotion is tag/manual gated**: only `cross-project`-tagged verified L3 nodes auto-promote; `promotion.min_projects` can require multi-project evidence.
- **External API versions use a local manifest**: `zymor anchor --api` checks a JSON/TOML version manifest in the repo; it does not poll remote services.
- **Semantic retrieval is optional**: configure `[embedding]` and run `zymor index embed` first.
- **MCP HTTP auth is a static bearer token**: no expiry/scopes/revocation; stdio relies on local trust.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
