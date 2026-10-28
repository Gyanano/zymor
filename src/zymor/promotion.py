from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from zymor.config import Config
from zymor.errors import ValidationError, ZymorError
from zymor.model.node import MemoryNode, new_node
from zymor.store.index import Index
from zymor.store.nodes import read_node, update_node, write_node
from zymor.util.time import now_iso
from zymor.vault import Vault
from zymor.write_service import read_dissonance

DEFAULT_MIN_CONFIDENCE = 0.85
DEFAULT_MIN_VERIFY_COUNT = 3
DEFAULT_MIN_HIT_COUNT = 3
PROMOTED_TAG = "promoted"
CROSS_PROJECT_TAG = "cross-project"
CROSS_PROJECT_VERIFIED_TAG = "cross-project-verified"
SOURCE = "dream:promote"


@dataclass
class PromotionResult:
    status: str
    node_id: str
    global_id: str | None = None
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    title: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：可序列化的晋升结果。"""
        return {
            "status": self.status,
            "node_id": self.node_id,
            "global_id": self.global_id,
            "title": self.title,
            "reasons": self.reasons,
            "warnings": self.warnings,
        }


@dataclass
class AutoPromotionResult:
    promoted: list[str] = field(default_factory=list)
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    stopped: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：可序列化的自动晋升结果。"""
        return {
            "status": "ran",
            "promoted": self.promoted,
            "skipped": self.skipped,
            "warnings": self.warnings,
            "stopped": self.stopped,
        }


def _config_float(config: Config, key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _config_int(config: Config, key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _normalized_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip().lower()


def _project_tag(project: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", project.lower()).strip("-")
    return f"promoted-from-{slug[:40] or 'project'}"


def _pending_conflict(project_vault: Vault, node_id: str) -> bool:
    for entry in read_dissonance(project_vault):
        if str(entry.get("state") or "") != "open":
            continue
        if node_id in {str(entry.get("new_id") or ""), str(entry.get("old_id") or "")}:
            return True
    return False


def _existing_global(global_vault: Vault, title: str) -> MemoryNode | None:
    wanted = _normalized_title(title)
    rows = Index(global_vault.root).layer_rows(
        "L3", statuses=["hypothesis", "verified", "disputed"], limit=5000
    )
    for row in rows:
        if _normalized_title(str(row.get("title") or "")) != wanted:
            continue
        try:
            node, _ = read_node(global_vault, str(row["id"]))
        except ZymorError:
            continue
        return node
    return None


def evaluate_promotion(
    project_vault: Vault,
    global_vault: Vault,
    config: Config,
    node_id: str,
    *,
    force: bool = False,
) -> PromotionResult:
    """入参：项目库、用户库、配置与节点 id。出参：晋升资格评估结果（不写库）。"""
    try:
        node, _ = read_node(project_vault, node_id)
    except ZymorError as error:
        return PromotionResult("rejected", node_id, reasons=[error.message])
    reasons: list[str] = []
    if node.layer != "L3":
        reasons.append("只有 L3 节点可以跨项目晋升")
    if node.status != "verified":
        reasons.append("status 必须是 verified")
    min_confidence = _config_float(config, "promotion.min_confidence", DEFAULT_MIN_CONFIDENCE)
    if node.confidence < min_confidence:
        reasons.append(f"confidence 低于阈值 {min_confidence}")
    min_verify = _config_int(config, "promotion.min_verify_count", DEFAULT_MIN_VERIFY_COUNT)
    if node.verify_count < min_verify:
        reasons.append(f"verify_count 低于阈值 {min_verify}")
    min_hit = _config_int(config, "promotion.min_hit_count", DEFAULT_MIN_HIT_COUNT)
    if node.hit_count < min_hit:
        reasons.append(f"hit_count 低于阈值 {min_hit}")
    if not node.applies_to:
        reasons.append("缺少 applies_to：跨项目知识必须有适用范围")
    if not node.evidence:
        reasons.append("缺少 evidence：晋升必须可追溯")
    min_projects = _config_int(config, "promotion.min_projects", 1)
    known_projects = {*node.projects, project_vault.info.name}
    if not force and len(known_projects) < min_projects:
        reasons.append(f"项目证据不足：已知 {len(known_projects)} 个项目，要求 {min_projects}")
    if _pending_conflict(project_vault, node_id):
        reasons.append("存在未裁决的冲突，先处理晨间追问队列")
    if reasons:
        return PromotionResult("rejected", node_id, reasons=reasons, title=node.title)
    existing = _existing_global(global_vault, node.title)
    if existing is not None:
        return PromotionResult(
            "duplicate",
            node_id,
            global_id=existing.id,
            reasons=["用户库已有同标题节点"],
            title=node.title,
        )
    return PromotionResult("eligible", node_id, reasons=[], title=node.title)


def promote_to_global(
    project_vault: Vault,
    global_vault: Vault,
    config: Config,
    node_id: str,
    *,
    dry_run: bool = False,
    confirm: bool = False,
    force: bool = False,
) -> PromotionResult:
    """入参：项目库、用户库、配置、节点 id 与确认/强制开关。出参：晋升结果。"""
    result = evaluate_promotion(project_vault, global_vault, config, node_id, force=force)
    if result.status != "eligible":
        return result
    if dry_run:
        result.status = "dry-run"
        return result
    if not confirm:
        raise ValidationError(
            "跨项目晋升需要显式确认",
            field="confirm",
            hint="加 --yes 确认；先 --dry-run 查看资格",
        )
    node, _ = read_node(project_vault, node_id)
    project = project_vault.info.name
    tags = sorted({*node.tags, PROMOTED_TAG, _project_tag(project)})
    promoted = new_node(
        title=node.title,
        summary=node.summary,
        category=node.category,
        layer="L3",
        body=node.body,
        source=SOURCE,
        created_by="dream",
        applies_to=node.applies_to,
        tags=tags,
        evidence=node.evidence,
        projects=sorted({*node.projects, project}),
        status="verified",
        confidence=node.confidence,
        arousal=node.arousal,
    )
    write_node(global_vault, promoted, "promote")
    update_node(
        global_vault,
        promoted.id,
        {
            "verify_count": node.verify_count,
            "hit_count": node.hit_count,
            "last_verified": now_iso(),
        },
        command="promote",
    )
    result.status = "promoted"
    result.global_id = promoted.id
    return result


def adopt_global(global_vault: Vault, project: str, node_id: str) -> MemoryNode:
    """入参：用户库、项目名与节点 id。出参：更新后的节点。记录该项目确认复用全局节点。"""
    node, _ = read_node(global_vault, node_id)
    if node.layer not in {"L3", "L4"}:
        raise ValidationError("只有用户库的 L3/L4 节点可以被项目采纳", field="id")
    projects = sorted({*node.projects, project})
    if projects == node.projects:
        return node
    tags = set(node.tags)
    if len(projects) >= 2:
        tags.add(CROSS_PROJECT_VERIFIED_TAG)
    return update_node(
        global_vault,
        node_id,
        {"projects": projects, "tags": sorted(tags), "last_verified": now_iso()},
        command="adopt",
    )


def auto_promote(ctx: Any) -> AutoPromotionResult:
    """入参：dream 上下文（需含 vault/global_vault/config/budget）。出参：自动晋升结果。"""
    result = AutoPromotionResult()
    config: Config = ctx.config
    if not bool(config.get("promotion.auto_enabled", True)):
        result.warnings.append("promotion.auto_enabled=false，跳过自动跨项目晋升")
        return result
    global_vault = ctx.global_vault
    if global_vault is None:
        result.warnings.append("未解析到用户库，跳过自动跨项目晋升")
        return result
    limit = _config_int(config, "promotion.auto_scan_limit", 300)
    try:
        rows = Index(ctx.vault.root).layer_rows("L3", statuses=["verified"], limit=max(0, limit))
    except ZymorError as error:
        result.warnings.append(f"自动晋升扫描失败：{error.message}")
        return result
    for row in rows:
        node_id = str(row["id"])
        try:
            node, _ = read_node(ctx.vault, node_id)
        except ZymorError:
            continue
        if CROSS_PROJECT_TAG not in node.tags:
            continue
        if not ctx.budget.can_write():
            result.stopped = ctx.budget.exhausted() or "节点变更达到上限"
            ctx.budget.stopped_reason = result.stopped
            break
        outcome = promote_to_global(ctx.vault, global_vault, config, node_id, confirm=True)
        if outcome.status == "promoted":
            ctx.budget.record_write()
            result.promoted.append(node_id)
        elif outcome.status == "duplicate":
            result.skipped += 1
        else:
            result.warnings.append(f"{node_id} 未晋升：" + "；".join(outcome.reasons))
    return result
