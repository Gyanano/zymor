from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zymor.config import Config
from zymor.dream.state import log_action
from zymor.dream.types import DreamContext
from zymor.errors import ZymorError
from zymor.model.node import MemoryNode
from zymor.store.index import Index
from zymor.store.nodes import read_node, update_node
from zymor.util.time import now_iso

MIN_CONFIDENCE = 0.85
MIN_VERIFY_COUNT = 3
MIN_HIT_COUNT = 3
DEFAULT_PROMOTION_LIMIT = 300


@dataclass
class PromotionResult:
    promoted: list[str] = field(default_factory=list)
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    stopped: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：晋升结果字典。"""
        return {
            "promoted": self.promoted,
            "skipped": self.skipped,
            "warnings": self.warnings,
            "stopped": self.stopped,
        }


def promote_l2_to_l3(ctx: DreamContext, *, limit: int | None = None) -> PromotionResult:
    """入参：做梦上下文与可选扫描上限。出参：晋升结果。把满足 §5.3 阈值的 L2 胶囊升为 L3。"""
    result = PromotionResult()
    scan_limit = (
        limit
        if limit is not None
        else _config_int(ctx.config, "dream.max_candidates", DEFAULT_PROMOTION_LIMIT)
    )
    try:
        rows = Index(ctx.vault.root).layer_rows(
            "L2", statuses=["verified"], limit=max(0, scan_limit)
        )
    except ZymorError as error:
        result.warnings.append(f"晋升扫描失败：{error.message}")
        return result
    for row in rows:
        node_id = str(row["id"])
        try:
            node, _ = read_node(ctx.vault, node_id)
        except ZymorError as error:
            result.warnings.append(f"晋升候选不可读 {node_id}：{error.message}")
            continue
        if not _eligible(node):
            result.skipped += 1
            continue
        if not ctx.budget.can_write():
            result.stopped = ctx.budget.exhausted() or "节点变更达到上限"
            ctx.budget.stopped_reason = result.stopped
            break
        try:
            update_node(
                ctx.vault,
                node.id,
                {"layer": "L3", "updated_at": now_iso()},
                "dream",
            )
        except ZymorError as error:
            result.warnings.append(f"晋升 {node.id} 失败：{error.message}")
            continue
        ctx.budget.record_write()
        result.promoted.append(node.id)
        log_action(
            ctx.vault,
            ctx.run_id,
            "nrem",
            "promote",
            node_id=node.id,
            details={
                "from": "L2",
                "to": "L3",
                "confidence": node.confidence,
                "verify_count": node.verify_count,
                "hit_count": node.hit_count,
            },
        )
    return result


def _eligible(node: MemoryNode) -> bool:
    if node.status != "verified":
        return False
    if node.confidence < MIN_CONFIDENCE:
        return False
    if node.verify_count < MIN_VERIFY_COUNT:
        return False
    return node.hit_count >= MIN_HIT_COUNT


def _config_int(config: Config, key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default
