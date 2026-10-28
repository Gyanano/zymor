from __future__ import annotations

from datetime import timedelta
from typing import Any

from zymor.dream.state import log_action
from zymor.dream.types import DreamContext
from zymor.store.index import Index
from zymor.store.nodes import read_node, update_node
from zymor.util.time import parse_iso, utc_now

CAPSULE_POOL_LIMIT = 500


def decay_stale_capsules(ctx: DreamContext) -> dict[str, Any]:
    """入参：做梦上下文。出参：降权统计。对闲置超过阈值的 L2 胶囊按 §5.4 参数执行一次降权。"""
    days = int(ctx.config.get("lifecycle.capsule_downweight_after_days"))
    lambda_ = float(ctx.config.get("dream.updates.arousal_lambda"))
    floor = float(ctx.config.get("dream.updates.arousal_min"))
    threshold = utc_now() - timedelta(days=days)
    index = Index(ctx.vault.root)
    rows = index.layer_rows(
        "L2",
        statuses=["hypothesis", "verified", "disputed"],
        limit=CAPSULE_POOL_LIMIT,
    )
    decayed: list[dict[str, Any]] = []
    stopped: str | None = None
    for row in rows:
        try:
            updated = parse_iso(str(row["updated_at"]))
        except ValueError:
            continue
        if updated >= threshold:
            continue
        if not ctx.budget.can_write():
            stopped = ctx.budget.exhausted() or "节点预算不足"
            ctx.budget.stopped_reason = stopped
            break
        try:
            node, _ = read_node(ctx.vault, str(row["id"]))
        except Exception as error:
            ctx.warn(f"胶囊降权读取失败 {row['id']}: {error}")
            continue
        current = node.arousal
        target = max(floor, current * lambda_)
        if target >= current:
            continue
        try:
            update_node(
                ctx.vault,
                node.id,
                {"arousal": round(target, 6), "updated_at": node.updated_at},
                command="dream:decay",
            )
        except Exception as error:
            ctx.warn(f"胶囊降权写入失败 {node.id}: {error}")
            continue
        ctx.budget.record_write()
        decayed.append({"id": node.id, "from": current, "to": round(target, 6)})
        log_action(
            ctx.vault,
            ctx.run_id,
            "decay",
            "capsule-downweight",
            node_id=node.id,
            details={"from": current, "to": round(target, 6), "lambda": lambda_, "floor": floor},
        )
    return {
        "decayed": decayed,
        "threshold_days": days,
        "lambda": lambda_,
        "floor": floor,
        "stopped": stopped,
    }


def stale_capsule_count(ctx: DreamContext) -> int:
    """入参：做梦上下文。出参：闲置超过阈值的 L2 胶囊数量（计划阶段用，不写文件）。"""
    days = int(ctx.config.get("lifecycle.capsule_downweight_after_days"))
    threshold = utc_now() - timedelta(days=days)
    index = Index(ctx.vault.root)
    count = 0
    for row in index.layer_rows(
        "L2",
        statuses=["hypothesis", "verified", "disputed"],
        limit=CAPSULE_POOL_LIMIT,
    ):
        try:
            updated = parse_iso(str(row["updated_at"]))
        except ValueError:
            continue
        if updated < threshold:
            count += 1
    return count
