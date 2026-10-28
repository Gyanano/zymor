from __future__ import annotations

from typing import Any

from zymor.errors import NotFoundError, ValidationError
from zymor.store.audit import append_audit, read_audit
from zymor.store.index import Index
from zymor.store.nodes import read_node, update_node
from zymor.util.time import now_iso
from zymor.vault import Vault

REVIEW_STATUSES = ("hypothesis", "disputed")


def review_nodes(
    vault: Vault,
    *,
    statuses: tuple[str, ...] = REVIEW_STATUSES,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """入参：vault、状态集合与上限。出参：待人工确认节点的 id/title/summary/status/updated_at。"""
    index = Index(vault.root)
    index.ensure()
    placeholders = ",".join("?" for _ in statuses)
    with index.connect() as connection:
        rows = connection.execute(
            "SELECT id, title, summary, status, updated_at, layer, category "
            f"FROM nodes WHERE status IN ({placeholders}) "
            "ORDER BY updated_at DESC LIMIT ?",
            (*statuses, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def forget_node(vault: Vault, node_id: str, *, command: str = "forget") -> dict[str, Any]:
    """入参：vault 与节点 id。出参：删除结果。删文件、重建索引、审计只留 id 与时刻。"""
    path = vault.find_node_path(node_id)
    if path is None:
        raise NotFoundError(f"节点不存在: {node_id}", hint="用 `zymor search <query>` 查找 id")
    with vault.writing(command):
        append_audit(
            vault,
            "writes.jsonl",
            {"ts": now_iso(), "op": "forget", "kind": "privacy_delete", "node_id": node_id},
        )
        path.unlink(missing_ok=True)
        Index(vault.root).rebuild()
    return {"id": node_id, "path": vault.relative(path), "deleted": True}


def write_stats(vault: Vault) -> dict[str, Any]:
    """入参：vault。出参：写入动作计数与 L0 事件数（stats 命令用）。"""
    counts: dict[str, int] = {}
    for record in read_audit(vault.writes_audit_path):
        if record.get("op") != "commit":
            continue
        action = str(record.get("action") or record.get("kind") or "unknown")
        counts[action] = counts.get(action, 0) + 1
    l0_files = sorted(vault.l0_dir.glob("*.jsonl"))
    l0_events = 0
    for path in l0_files:
        with path.open("r", encoding="utf-8") as handle:
            l0_events += sum(1 for line in handle if line.strip())
    gate_rejections = counts.get("rejected-gate", 0)
    return {
        "write_actions": counts,
        "gate_rejections": gate_rejections,
        "conflicts": counts.get("conflict", 0),
        "supersedes": counts.get("supersede", 0),
        "append_evidence": counts.get("append-evidence", 0),
        "degraded": counts.get("degraded", 0),
        "l0_events": l0_events,
    }


def confirm_persona_draft(
    queue_vault: Vault,
    draft_vault: Vault,
    *,
    qid: str,
    note: str,
) -> dict[str, Any]:
    """入参：队列 vault、用户库、追问 id 与依据。出参：升级结果。把 draft 升为 verified。"""
    from zymor.write_service import read_dissonance, update_dissonance

    items = read_dissonance(queue_vault)
    entry = next((item for item in items if item.get("qid") == qid), None)
    if entry is None:
        raise NotFoundError(f"追问不存在: {qid}", hint="查看 dream/dissonance.md 确认 qid")
    if entry.get("type") != "persona-proposal":
        raise ValidationError(
            f"追问 {qid} 不是人格规则草案",
            field="type",
            hint="冲突对请用 memory_answer 的默认流程处理",
        )
    draft_id = entry.get("new_id")
    if not draft_id:
        raise NotFoundError(f"追问 {qid} 缺少 draft 节点 id")
    node, _ = read_node(draft_vault, str(draft_id))
    if node.layer != "L4":
        raise ValidationError(f"draft 节点 {draft_id} 不是 L4 facet", field="layer")
    updates = {
        "status": "verified",
        "confidence": max(0.7, node.confidence),
        "last_verified": now_iso(),
        "updated_at": now_iso(),
        "evidence": [*node.evidence, note],
        "tags": [tag for tag in node.tags if tag != "pending-confirmation"],
    }
    updated = update_node(draft_vault, node.id, updates, command="answer")
    entry_after = update_dissonance(queue_vault, qid, state="answered")
    return {
        "qid": qid,
        "draft_id": updated.id,
        "status": updated.status,
        "confidence": updated.confidence,
        "state": entry_after.get("state"),
    }
