# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |

## Reporting a vulnerability

**Do not open a public GitHub issue for security problems.**

Use GitHub's private vulnerability reporting on this repository (Security → Report a vulnerability), or contact the maintainer through GitHub (`@Gyanano`). Include:

- affected version/commit;
- reproduction steps or a proof of concept;
- impact assessment;
- any suggested fix.

We aim to acknowledge reports within a few business days. This is a small project, so timelines are best-effort.

## Scope and design boundaries

- Zymor runs as a local process and MCP server; stdio mode relies on local trust.
- HTTP MCP mode uses a static bearer token; do not expose it to untrusted networks without a reverse proxy and TLS.
- The vault is plaintext Markdown by design; do not store secrets in memory nodes.
- `memory_rule_check` and anchors read local files; they do not execute arbitrary commands from memory content.
- External API version checks use a local manifest, not remote polling.
