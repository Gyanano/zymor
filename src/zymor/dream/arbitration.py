from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from zymor.config import Config
from zymor.dream.evidence import KNOWN_LIMITS, collect_evidence
from zymor.dream.state import Watermarks, log_action
from zymor.dream.types import DreamBudgetExceeded, DreamContext, DreamPlan, PhaseResult
from zymor.errors import IndexFailure, NotFoundError, UserError, ValidationError, ZymorError
from zymor.llm import JsonChatClient
from zymor.model.node import MAX_BODY_CJK, MAX_BODY_TOKENS, TOMBSTONE_H2, MemoryNode
from zymor.store.index import Index
from zymor.store.nodes import read_node, update_node
from zymor.tokenizer import _is_cjk, estimate_tokens, tokenize_terms
from zymor.util.time import now_iso, parse_iso
from zymor.vault import Vault
from zymor.write_service import append_dissonance, read_dissonance, update_dissonance

ARBITRATION_VERDICTS = ("uphold-new", "uphold-old", "defer", "unverifiable")
COMMITTAL_VERDICTS = ("uphold-new", "uphold-old")
DEFAULT_MAX_ARBITRATIONS = 20
DEFAULT_MAX_ATTEMPTS = 3
MAX_PAIR_CANDIDATES = 40
RELATION_THRESHOLD = 2
MAX_RELATED_TERMS = 16
MAX_DISCOVERED_PER_PAIR = 4
PROMPT_BODY_CHARS = 600
ARBITRATION_SYSTEM_PROMPT = (
    "你是 Zymor 记忆系统的仲裁者。给定一对可能冲突的记忆节点及其只读证据，判断应采纳哪一方。"
    "只读、离线：不能执行命令、不能抓取网络，证据不足时不要臆造。"
    "只输出一个 JSON 对象，字段：verdict（uphold-new / uphold-old / defer / unverifiable）、"
    "reason（一句话理由）、evidence（字符串数组，引用可核验指针）。"
    "uphold-new 表示新节点正确、旧节点应废弃；uphold-old 表示旧节点正确、新节点应废弃；"
    "defer 表示证据不足但可等待用户确认；unverifiable 表示现有只读证据无法判定。"
)


class _BudgetStop(Exception):
    pass


@dataclass
class _Pair:
    new_id: str
    old_id: str
    source: str
    qid: str | None = None
    question: str = ""
    relation: int = 0
    new_node: MemoryNode | None = None
    old_node: MemoryNode | None = None


@dataclass
class _QueueState:
    qids: dict[frozenset[str], str] = field(default_factory=dict)
    states: dict[frozenset[str], str] = field(default_factory=dict)


def run_arbitration(ctx: DreamContext, plan: DreamPlan, watermarks: Watermarks) -> PhaseResult:
    """入参：上下文、计划与水位线。出参：仲裁结果与证据明细。只读实证后执行四裁决。"""
    del watermarks
    started = time.perf_counter()
    max_arbitrations = _config_int(ctx.config, "dream.max_arbitrations", DEFAULT_MAX_ARBITRATIONS)
    attempts_cap = max(
        1, _config_int(ctx.config, "dream.max_arbitration_attempts", DEFAULT_MAX_ATTEMPTS)
    )
    queue_entries = read_dissonance(ctx.vault)
    open_entries = [entry for entry in queue_entries if str(entry.get("state") or "") == "open"]
    disputed_rows, load_warnings = _load_disputed_rows(ctx.vault, max(50, max_arbitrations * 5))
    if not disputed_rows and not open_entries:
        return PhaseResult(phase="arbitration")
    result = PhaseResult(phase="arbitration")
    result.warnings.extend(load_warnings)
    result.details["known_limits"] = list(KNOWN_LIMITS)
    result.details["input"] = {
        "disputed": len(disputed_rows),
        "open_questions": len(open_entries),
        "plan_disputed": plan.disputed_nodes,
    }
    pairs = _build_pairs(ctx.vault, disputed_rows, open_entries, queue_entries, max_arbitrations)
    result.candidates = len(pairs)
    result.details["pairs"] = []
    if not pairs:
        result.details["elapsed_ms"] = _elapsed_ms(started)
        return result
    client = ctx.client("arbitration")
    if client is None:
        result.warnings.append("arbitration 模型未配置：跳过仲裁，未写入任何内容")
        result.skipped = len(pairs)
        result.details["elapsed_ms"] = _elapsed_ms(started)
        return result
    stopped: str | None = None
    for pair in pairs:
        if stopped is not None:
            break
        pair_details: dict[str, Any] = {
            "new_id": pair.new_id,
            "old_id": pair.old_id,
            "source": pair.source,
        }
        if pair.qid is not None:
            pair_details["qid"] = pair.qid
        result.details["pairs"].append(pair_details)
        try:
            bundle = _collect_pair_evidence(ctx.vault, pair)
        except UserError as error:
            message = f"证据读取被安全拒绝（{pair.new_id} ↔ {pair.old_id}）：{error.message}"
            ctx.warn(message)
            result.warnings.append(message)
            pair_details["blocked_evidence"] = {
                "message": error.message,
                "hint": error.hint,
            }
            result.skipped += 1
            continue
        except ZymorError as error:
            message = f"证据收集失败（{pair.new_id} ↔ {pair.old_id}）：{error.message}"
            result.errors.append(message)
            pair_details["error"] = message
            result.skipped += 1
            continue
        pair_details["evidence"] = bundle
        try:
            verdict_data = _ask_arbitration(ctx, client, pair, bundle)
        except DreamBudgetExceeded as error:
            stopped = error.message
            pair_details["error"] = f"预算停止：{error.message}"
            pair_details["skipped"] = True
            result.skipped += 1
            continue
        except ZymorError as error:
            message = f"仲裁调用失败（{pair.new_id} ↔ {pair.old_id}）：{error.message}"
            result.errors.append(message)
            pair_details["error"] = message
            result.skipped += 1
            continue
        verdict = str(verdict_data.get("verdict") or "").strip()
        reason = str(verdict_data.get("reason") or "").strip()
        evidence_items = _string_list(verdict_data.get("evidence"))
        if verdict not in ARBITRATION_VERDICTS:
            message = f"未知裁决 {verdict!r}，按 unverifiable 处理"
            result.warnings.append(message)
            pair_details["warning"] = message
            verdict = "unverifiable"
        pair_details["verdict"] = verdict
        pair_details["reason"] = reason
        pair_details["verdict_evidence"] = evidence_items
        qid = _ensure_queue_entry(ctx, pair, reason)
        if qid is None:
            message = f"无法定位或创建晨间追问条目（{pair.new_id} ↔ {pair.old_id}）"
            result.errors.append(message)
            pair_details["error"] = message
            result.skipped += 1
            continue
        pair_details["qid"] = qid
        writes: list[dict[str, str]] = []
        pair_details["writes"] = writes
        apply_error: ZymorError | None = None
        stop_reason: str | None = None
        try:
            _apply_verdict(
                ctx,
                pair,
                verdict,
                reason,
                evidence_items,
                qid,
                attempts_cap,
                pair_details,
                writes,
            )
        except _BudgetStop as error:
            stop_reason = str(error)
        except ZymorError as error:
            apply_error = error
        result.nodes_updated += len(writes)
        if apply_error is not None:
            message = f"裁决执行失败（{pair.new_id} ↔ {pair.old_id}）：{apply_error.message}"
            result.errors.append(message)
            pair_details["error"] = message
            result.skipped += 1
            continue
        if stop_reason is not None:
            stopped = stop_reason
            pair_details["stopped"] = stop_reason
            continue
        _log_verdict(ctx, pair, verdict, reason, qid, writes)
        pair_details["logged"] = True
    if stopped is not None:
        result.details["stopped"] = stopped
        ctx.budget.stopped_reason = stopped
    result.details["elapsed_ms"] = _elapsed_ms(started)
    return result


def _build_pairs(
    vault: Vault,
    disputed_rows: list[dict[str, Any]],
    open_entries: list[dict[str, Any]],
    queue_entries: list[dict[str, Any]],
    max_pairs: int,
) -> list[_Pair]:
    pairs: list[_Pair] = []
    if max_pairs <= 0:
        return pairs
    queue_state = _queue_state(queue_entries)
    seen: set[frozenset[str]] = set()
    for entry in open_entries:
        if len(pairs) >= max_pairs:
            break
        new_id = str(entry.get("new_id") or "")
        old_id = str(entry.get("old_id") or "")
        if not new_id or not old_id or new_id == old_id:
            continue
        key = frozenset({new_id, old_id})
        if key in seen:
            continue
        seen.add(key)
        pairs.append(
            _Pair(
                new_id=new_id,
                old_id=old_id,
                source="queue",
                qid=queue_state.qids.get(key),
                question=str(entry.get("question") or entry.get("reason") or ""),
                new_node=_read_optional(vault, new_id),
                old_node=_read_optional(vault, old_id),
            )
        )
    if len(pairs) >= max_pairs or not disputed_rows:
        return pairs
    index = Index(vault.root)
    discovered: dict[frozenset[str], tuple[int, str, str]] = {}
    discovery_warnings: list[str] = []
    for row in disputed_rows:
        if len(discovered) >= max_pairs * MAX_DISCOVERED_PER_PAIR:
            break
        node_id = str(row.get("id") or "")
        if not node_id:
            continue
        for candidate in _related_candidates(index, row, discovery_warnings):
            candidate_id = str(candidate.get("id") or "")
            if not candidate_id or candidate_id == node_id:
                continue
            key = frozenset({node_id, candidate_id})
            if key in seen or key in discovered:
                continue
            state = queue_state.states.get(key)
            if state is not None and state != "open":
                continue
            relation = _relation(row, candidate)
            if relation < RELATION_THRESHOLD:
                continue
            discovered[key] = (relation, node_id, candidate_id)
    ordered = sorted(discovered.values(), key=lambda item: (-item[0], item[1], item[2]))
    for relation, left_id, right_id in ordered:
        if len(pairs) >= max_pairs:
            break
        left_node = _read_optional(vault, left_id)
        right_node = _read_optional(vault, right_id)
        if left_node is None or right_node is None:
            continue
        new_id, new_node, old_id, old_node = _ordered_pair(left_id, left_node, right_id, right_node)
        key = frozenset({new_id, old_id})
        if key in seen:
            continue
        seen.add(key)
        pairs.append(
            _Pair(
                new_id=new_id,
                old_id=old_id,
                source="fts",
                relation=relation,
                new_node=new_node,
                old_node=old_node,
            )
        )
    return pairs


def _queue_state(entries: list[dict[str, Any]]) -> _QueueState:
    state = _QueueState()
    for entry in entries:
        new_id = str(entry.get("new_id") or "")
        old_id = str(entry.get("old_id") or "")
        if not new_id or not old_id or new_id == old_id:
            continue
        key = frozenset({new_id, old_id})
        state.qids[key] = str(entry.get("qid") or "")
        state.states[key] = str(entry.get("state") or "")
    return state


def _load_disputed_rows(vault: Vault, limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    index = Index(vault.root)
    warnings: list[str] = []
    try:
        index.ensure()
        with index.connect() as connection:
            fetched = connection.execute(
                "SELECT id, title, summary, status, layer, category, confidence, arousal, "
                "updated_at, project, applies_to, tags FROM nodes WHERE status = 'disputed' "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except (IndexFailure, sqlite3.Error) as error:
        warnings.append(f"读取 disputed 节点失败：{error}")
        return [], warnings
    rows: list[dict[str, Any]] = []
    for row in fetched:
        data: dict[str, Any] = dict(row)
        data["tags"] = _json_list(data.get("tags"))
        data["applies_to"] = _json_list(data.get("applies_to"))
        rows.append(data)
    return rows, warnings


def _related_candidates(
    index: Index, row: dict[str, Any], warnings: list[str]
) -> list[dict[str, Any]]:
    terms = _pair_terms(row)
    if not terms:
        return []
    query = " OR ".join(f'"{term}"' for term in terms[:MAX_RELATED_TERMS])
    try:
        candidates = index.candidate_rows(query, limit=MAX_PAIR_CANDIDATES)
    except ZymorError as error:
        warnings.append(f"候选对检索失败（{row.get('id')}）：{error.message}")
        return []
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        if str(candidate.get("status") or "") == "deprecated":
            continue
        result.append(candidate)
    return result


def _pair_terms(row: dict[str, Any]) -> list[str]:
    parts = [str(row.get("title") or ""), str(row.get("summary") or "")]
    parts.extend(str(tag) for tag in row.get("tags") or [])
    parts.extend(str(item) for item in row.get("applies_to") or [])
    terms = list(dict.fromkeys(tokenize_terms(" ".join(parts))))
    return [term for term in terms if len(term) > 1]


def _relation(left: dict[str, Any], right: dict[str, Any]) -> int:
    left_tags = {str(tag) for tag in left.get("tags") or []}
    right_tags = {str(tag) for tag in right.get("tags") or []}
    left_applies = {str(item) for item in left.get("applies_to") or []}
    right_applies = {str(item) for item in right.get("applies_to") or []}
    left_terms = {
        term
        for term in tokenize_terms(f"{left.get('title') or ''} {left.get('summary') or ''}")
        if len(term) > 1
    }
    right_terms = {
        term
        for term in tokenize_terms(f"{right.get('title') or ''} {right.get('summary') or ''}")
        if len(term) > 1
    }
    return (
        2 * len(left_tags & right_tags)
        + 2 * len(left_applies & right_applies)
        + len(left_terms & right_terms)
    )


def _ordered_pair(
    left_id: str, left_node: MemoryNode, right_id: str, right_node: MemoryNode
) -> tuple[str, MemoryNode, str, MemoryNode]:
    if _stamp(left_node.created_at) <= _stamp(right_node.created_at):
        return right_id, right_node, left_id, left_node
    return left_id, left_node, right_id, right_node


def _read_optional(vault: Vault, node_id: str) -> MemoryNode | None:
    try:
        node, _ = read_node(vault, node_id)
    except (NotFoundError, ValidationError):
        return None
    return node


def _collect_pair_evidence(vault: Vault, pair: _Pair) -> dict[str, Any]:
    bundle: dict[str, Any] = {}
    for side, node in (("new", pair.new_node), ("old", pair.old_node)):
        if node is None:
            bundle[side] = {"status": "missing"}
            continue
        bundle[side] = collect_evidence(vault, node)
    return bundle


def _node_view(node: MemoryNode | None, node_id: str) -> dict[str, Any]:
    if node is None:
        return {"id": node_id, "status": "missing"}
    return {
        "id": node.id,
        "title": node.title,
        "summary": node.summary,
        "status": node.status,
        "confidence": node.confidence,
        "arousal": node.arousal,
        "verify_count": node.verify_count,
        "tags": list(node.tags),
        "applies_to": list(node.applies_to),
        "evidence": list(node.evidence[:5]),
        "body_excerpt": node.body[:PROMPT_BODY_CHARS],
    }


def _prompt_payload(ctx: DreamContext, pair: _Pair, bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": "arbitrate-dissonance",
        "run_id": ctx.run_id,
        "pair": {
            "new_id": pair.new_id,
            "old_id": pair.old_id,
            "source": pair.source,
            "question": pair.question,
        },
        "new": _node_view(pair.new_node, pair.new_id),
        "old": _node_view(pair.old_node, pair.old_id),
        "readonly_evidence": bundle,
        "known_limits": list(KNOWN_LIMITS),
        "output_schema": {
            "verdict": "uphold-new | uphold-old | defer | unverifiable",
            "reason": "string",
            "evidence": ["string"],
        },
    }


def _ask_arbitration(
    ctx: DreamContext, client: JsonChatClient, pair: _Pair, bundle: dict[str, Any]
) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": ARBITRATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(_prompt_payload(ctx, pair, bundle), ensure_ascii=False),
        },
    ]
    parsed, _response = client.complete_json(
        messages,
        caller=f"dream:{ctx.run_id}:arbitration",
        temperature=0.0,
    )
    if not isinstance(parsed, dict):
        raise ValidationError(
            "仲裁模型输出不是 JSON 对象",
            field="verdict",
            hint='要求输出 {"verdict": "...", "reason": "...", "evidence": [...]}',
        )
    return parsed


def _ensure_queue_entry(ctx: DreamContext, pair: _Pair, reason: str) -> str | None:
    qid = _match_qid(read_dissonance(ctx.vault), pair.new_id, pair.old_id)
    if qid is not None:
        return qid
    try:
        append_dissonance(
            ctx.vault,
            new_id=pair.new_id,
            old_id=pair.old_id,
            reason=reason.strip() or "仲裁发现冲突对",
        )
    except ZymorError:
        return None
    return _match_qid(read_dissonance(ctx.vault), pair.new_id, pair.old_id)


def _match_qid(entries: list[dict[str, Any]], new_id: str, old_id: str) -> str | None:
    for entry in entries:
        if {entry.get("new_id"), entry.get("old_id")} != {new_id, old_id}:
            continue
        qid = str(entry.get("qid") or "")
        if qid:
            return qid
    return None


def _apply_verdict(
    ctx: DreamContext,
    pair: _Pair,
    verdict: str,
    reason: str,
    evidence_items: list[str],
    qid: str,
    attempts_cap: int,
    pair_details: dict[str, Any],
    writes: list[dict[str, str]],
) -> None:
    if verdict == "uphold-new":
        _uphold_new(ctx, pair, evidence_items, writes, pair_details)
    elif verdict == "uphold-old":
        _uphold_old(ctx, pair, evidence_items, writes, pair_details)
    elif verdict == "defer":
        _defer(ctx, pair, writes, pair_details)
    else:
        pair_details["new_status"] = pair.new_node.status if pair.new_node else "missing"
    _update_queue(ctx, qid, verdict, attempts_cap, pair_details)
    if reason:
        pair_details["reason"] = reason


def _uphold_new(
    ctx: DreamContext,
    pair: _Pair,
    evidence_items: list[str],
    writes: list[dict[str, str]],
    pair_details: dict[str, Any],
) -> None:
    new_node = pair.new_node
    old_node = pair.old_node
    if new_node is None:
        raise NotFoundError(
            f"新节点不存在，无法维持新节点: {pair.new_id}",
            hint="节点缺失时先重建索引或恢复文件，再重新仲裁",
        )
    timestamp = now_iso()
    if old_node is not None and old_node.status != "deprecated":
        tombstone = _tombstone_block(
            old_node, new_node.summary, new_node.id, evidence_items, ctx.run_id, timestamp
        )
        old_evidence = _merge_evidence(
            old_node.evidence, evidence_items, f"{new_node.id}；仲裁 {ctx.run_id}"
        )
        updates: dict[str, Any] = {
            "status": "deprecated",
            "valid_to": timestamp,
            "superseded_by": new_node.id,
            "evidence": old_evidence,
            "body": _append_tombstone(old_node, tombstone),
            "updated_at": timestamp,
        }
        _apply_node_update(ctx, writes, old_node.id, "deprecate", updates)
    if new_node.status == "deprecated":
        pair_details["warning"] = f"新节点已 deprecated，跳过确认: {new_node.id}"
        return
    confirmed = _confirmation_updates(ctx.config, new_node, timestamp)
    confirmed["evidence"] = _merge_evidence(
        new_node.evidence, evidence_items, f"仲裁 {ctx.run_id}: 维持 {new_node.id}"
    )
    if old_node is not None:
        confirmed["supersedes"] = _merge_ids(new_node.supersedes, [old_node.id])
    _apply_node_update(ctx, writes, new_node.id, "confirm", confirmed)


def _uphold_old(
    ctx: DreamContext,
    pair: _Pair,
    evidence_items: list[str],
    writes: list[dict[str, str]],
    pair_details: dict[str, Any],
) -> None:
    new_node = pair.new_node
    old_node = pair.old_node
    timestamp = now_iso()
    if new_node is None:
        pair_details["discarded"] = True
        pair_details["discard_reason"] = "候选新节点未入库，直接丢弃"
        return
    if new_node.status == "deprecated":
        pair_details["warning"] = f"新节点已 deprecated，跳过废弃: {new_node.id}"
        return
    replacement_id = old_node.id if old_node is not None else None
    replacement_summary = old_node.summary if old_node is not None else "旧节点不在库中"
    tombstone = _tombstone_block(
        new_node,
        replacement_summary,
        replacement_id,
        evidence_items,
        ctx.run_id,
        timestamp,
    )
    pointer = (
        f"{replacement_id}；仲裁 {ctx.run_id}"
        if replacement_id is not None
        else f"仲裁 {ctx.run_id}：旧节点不在库中"
    )
    evidence = _merge_evidence(new_node.evidence, evidence_items, pointer)
    updates: dict[str, Any] = {
        "status": "deprecated",
        "valid_to": timestamp,
        "superseded_by": replacement_id,
        "evidence": evidence,
        "body": _append_tombstone(new_node, tombstone),
        "updated_at": timestamp,
    }
    _apply_node_update(ctx, writes, new_node.id, "deprecate", updates)


def _defer(
    ctx: DreamContext,
    pair: _Pair,
    writes: list[dict[str, str]],
    pair_details: dict[str, Any],
) -> None:
    timestamp = now_iso()
    disputed: list[str] = []
    for node in (pair.new_node, pair.old_node):
        if node is None or node.status == "deprecated" or node.status == "disputed":
            continue
        updates: dict[str, Any] = {
            "status": "disputed",
            "arousal": max(node.arousal, 0.9),
            "updated_at": timestamp,
        }
        _apply_node_update(ctx, writes, node.id, "dispute", updates)
        disputed.append(node.id)
    pair_details["disputed"] = disputed


def _confirmation_updates(config: Config, node: MemoryNode, timestamp: str) -> dict[str, Any]:
    alpha = _config_float(config, "dream.updates.alpha", 0.40)
    cap = _config_float(config, "dream.updates.confidence_cap", 0.99)
    arousal_lambda = _config_float(config, "dream.updates.arousal_lambda", 0.7)
    arousal_min = _config_float(config, "dream.updates.arousal_min", 0.1)
    confidence = min(cap, node.confidence + (1.0 - node.confidence) * alpha)
    return {
        "status": "verified",
        "confidence": confidence,
        "arousal": max(arousal_min, node.arousal * arousal_lambda),
        "verify_count": node.verify_count + 1,
        "last_verified": timestamp,
        "updated_at": timestamp,
    }


def _apply_node_update(
    ctx: DreamContext,
    writes: list[dict[str, str]],
    node_id: str,
    action: str,
    updates: dict[str, Any],
) -> None:
    if not ctx.budget.can_write():
        raise _BudgetStop(ctx.budget.exhausted() or "节点变更预算不足")
    update_node(ctx.vault, node_id, updates, "dream")
    ctx.budget.record_write()
    writes.append({"node_id": node_id, "action": action})


def _update_queue(
    ctx: DreamContext,
    qid: str,
    verdict: str,
    attempts_cap: int,
    pair_details: dict[str, Any],
) -> None:
    entries = read_dissonance(ctx.vault)
    entry = next((item for item in entries if str(item.get("qid") or "") == qid), None)
    if entry is None:
        raise NotFoundError(f"追问不存在: {qid}", hint="查看 dream/dissonance.md 确认 qid 后重试")
    if verdict in COMMITTAL_VERDICTS:
        updated = update_dissonance(ctx.vault, qid, state="answered")
    else:
        attempts = int(entry.get("attempts") or 0) + 1
        state = "unverifiable" if attempts >= attempts_cap else "open"
        updated = update_dissonance(ctx.vault, qid, state=state, attempts=attempts)
    pair_details["queue"] = {
        "qid": qid,
        "state": str(updated.get("state") or ""),
        "attempts": int(updated.get("attempts") or 0),
    }


def _log_verdict(
    ctx: DreamContext,
    pair: _Pair,
    verdict: str,
    reason: str,
    qid: str,
    writes: list[dict[str, str]],
) -> None:
    log_action(
        ctx.vault,
        ctx.run_id,
        "arbitration",
        f"verdict:{verdict}",
        node_id=pair.new_id,
        details={
            "new_id": pair.new_id,
            "old_id": pair.old_id,
            "verdict": verdict,
            "reason": reason[:500],
            "qid": qid,
            "writes": [write["node_id"] for write in writes],
        },
    )


def _tombstone_block(
    deprecated: MemoryNode,
    replacement_summary: str,
    replacement_id: str | None,
    evidence_items: list[str],
    run_id: str,
    timestamp: str,
) -> str:
    heading = f"## {TOMBSTONE_H2}\n\n- 原结论：\n- 纠正：\n- 证据：\n- 替代：\n"
    token_budget = MAX_BODY_TOKENS - estimate_tokens(deprecated.body) - estimate_tokens(heading) - 2
    cjk_budget = MAX_BODY_CJK - _cjk_count(deprecated.body) - 6
    each_tokens = max(2, token_budget // 2)
    each_cjk = max(2, cjk_budget // 2)
    original = _clip_text(deprecated.summary, each_tokens, each_cjk)
    corrected = _clip_text(replacement_summary, each_tokens, each_cjk)
    evidence_text = "；".join(evidence_items[:3]) if evidence_items else f"仲裁 {run_id}"
    evidence_text = _clip_text(evidence_text, max(2, token_budget // 2), max(2, cjk_budget // 2))
    replacement = f"[[{replacement_id}]]" if replacement_id is not None else "（无替代节点）"
    return (
        f"## {TOMBSTONE_H2}\n\n"
        f"- 原结论：{original}\n"
        f"- 纠正：{corrected}\n"
        f"- 证据：{evidence_text}；{timestamp}\n"
        f"- 替代：{replacement}"
    )


def _append_tombstone(node: MemoryNode, tombstone: str) -> str:
    body = f"{node.body.rstrip()}\n\n{tombstone}\n"
    if estimate_tokens(body) <= MAX_BODY_TOKENS and _cjk_count(body) <= MAX_BODY_CJK:
        return body
    minimal = (
        f"## {TOMBSTONE_H2}\n\n"
        f"- 原结论：{_clip_text(node.summary, 4, 8)}\n"
        f"- 纠正：见仲裁记录\n"
        f"- 证据：仲裁 run\n"
        f"- 替代：见 superseded_by\n"
    )
    return f"{node.body.rstrip()}\n\n{minimal}\n"


def _clip_text(text: str, token_budget: int, cjk_budget: int) -> str:
    if token_budget <= 0 or cjk_budget <= 0:
        return ""
    kept: list[str] = []
    tokens = 0
    cjk = 0
    for char in text:
        if _is_cjk(char):
            token_cost = 1
            cjk_cost = 1
        elif char.isspace():
            token_cost = 0
            cjk_cost = 0
        else:
            token_cost = 1
            cjk_cost = 0
        if tokens + token_cost > token_budget or cjk + cjk_cost > cjk_budget:
            break
        kept.append(char)
        tokens += token_cost
        cjk += cjk_cost
    return "".join(kept).strip()


def _cjk_count(text: str) -> int:
    return sum(1 for char in text if _is_cjk(char))


def _merge_evidence(existing: list[str], additions: list[str], pointer: str | None) -> list[str]:
    merged = list(existing)
    for item in [*additions, pointer]:
        if item is None:
            continue
        text = str(item).strip()
        if text and text not in merged:
            merged.append(text)
    return merged


def _merge_ids(existing: list[str], additions: list[str]) -> list[str]:
    merged = list(existing)
    for item in additions:
        if item and item not in merged:
            merged.append(item)
    return merged


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result[:20]


def _json_list(value: Any) -> list[str]:
    if not isinstance(value, str) or not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]


def _stamp(value: str) -> float:
    try:
        return parse_iso(value).timestamp()
    except ValueError:
        return 0.0


def _config_int(config: Config, key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _config_float(config: Config, key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)
