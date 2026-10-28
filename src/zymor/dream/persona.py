from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from zymor.config import Config
from zymor.dream.state import Watermarks, load_unprocessed_events, log_action
from zymor.dream.types import DreamBudgetExceeded, DreamContext, DreamPlan, PhaseResult
from zymor.errors import ZymorError
from zymor.model.event import EventEntry
from zymor.model.node import MAX_BODY_CJK, MAX_BODY_TOKENS, MAX_SUMMARY, MAX_TITLE, MemoryNode
from zymor.store.index import Index
from zymor.store.nodes import read_node, update_node
from zymor.tokenizer import estimate_tokens, tokenize_terms
from zymor.util.time import now_iso
from zymor.vault import Vault
from zymor.write_service import (
    append_dissonance,
    memory_write,
    read_dissonance,
    update_dissonance,
)

PHASE = "persona"
MAX_EVENT_CHARS = 800
MAX_PROMPT_EVENTS = 200
MAX_PROPOSAL_QUESTION = 300
MISSING_SECTION_TEXT = "未提供。"
HISTORY_HEADER = "### 变更记录"
DRAFT_TAG = "pending-confirmation"
NO_CLIENT_MARKERS = ("未提供 LLM 客户端", "已跳过去重合并")
DEFAULT_RULE_TITLE = "未命名人格规则"
GATE_REASON_TEXT = {
    "insufficient-sessions": "不足 2 个不同会话",
    "unconfirmed": "用户未明确确认",
    "missing-applies-to": "缺少 applies_to",
}
SIGNAL_RE = re.compile(
    "不要|别|应该|以后|每次|总是|记住|偏好|喜欢|确认|正确|同意|就这样|请先|"
    "我希望|我要求|习惯|改成|不能|必须|优先|对[，,。]|好的|可以|"
    "don't|do not|always|never|remember|prefer|should",
    re.IGNORECASE,
)


@dataclass
class _Rule:
    title: str
    trigger_context: str
    guidance: str
    positive_example: str
    negative_example: str
    applies_to: list[str]
    evidence: list[str]
    sessions: list[str]
    confirmed: bool


@dataclass
class _Input:
    events: list[EventEntry]
    by_session: dict[str, list[EventEntry]]
    sessions: set[str]


def run_persona(ctx: DreamContext, plan: DreamPlan, watermarks: Watermarks) -> PhaseResult:
    """入参：做梦上下文、计划与水位线。出参：人格提炼阶段结果。提炼用户行为规则并写入 L4。"""
    result = PhaseResult(phase=PHASE)
    details: dict[str, Any] = {
        "l4_changes": [],
        "pending": [],
        "pending_conflict": [],
        "stopped": None,
        "global_vault": str(ctx.global_vault.root) if ctx.global_vault is not None else None,
        "signal_events": 0,
        "rules": 0,
    }
    result.details = details
    if ctx.global_vault is None:
        result.warnings.append(
            "未解析到用户库（vault.global_path）；人格提炼需要写入用户库，已跳过"
        )
        return result
    inputs = _collect_inputs(ctx, watermarks)
    result.processed_events = len(inputs.events)
    signal_events = [event for event in inputs.events if SIGNAL_RE.search(event.content)]
    details["signal_events"] = len(signal_events)
    if not signal_events:
        return result
    client = ctx.client("dream")
    if client is None:
        result.warnings.append("未配置 dream 模型，跳过人格提炼")
        return result
    try:
        parsed, _response = client.complete_json(
            _rules_messages(signal_events, inputs.sessions),
            caller=f"dream:{ctx.run_id}:persona",
        )
    except DreamBudgetExceeded as error:
        details["stopped"] = str(error.message)
        ctx.budget.stopped_reason = str(error.message)
        result.warnings.append(str(error.message))
        return result
    except Exception as error:
        result.warnings.append(f"人格提炼 LLM 调用失败：{_error_text(error)}")
        return result
    rules = _parse_rules(parsed, inputs, result.warnings)
    result.candidates = len(rules)
    details["rules"] = len(rules)
    if not rules:
        return result
    facets = _load_facets(ctx, ctx.global_vault)
    stopped: str | None = None
    for position, rule in enumerate(rules):
        if stopped is not None:
            result.skipped += len(rules) - position
            break
        try:
            _process_rule(ctx, rule, inputs, facets, details, result)
        except DreamBudgetExceeded as error:
            stopped = str(error.message)
            details["stopped"] = stopped
            ctx.budget.stopped_reason = stopped
            result.skipped += len(rules) - position
            break
    log_action(
        ctx.vault,
        ctx.run_id,
        PHASE,
        "finish",
        details={
            "rules": len(rules),
            "created": result.nodes_created,
            "updated": result.nodes_updated,
            "skipped": result.skipped,
            "stopped": details["stopped"],
        },
    )
    return result


def _process_rule(
    ctx: DreamContext,
    rule: _Rule,
    inputs: _Input,
    facets: list[dict[str, Any]],
    details: dict[str, Any],
    result: PhaseResult,
) -> None:
    gate = _gate(rule)
    if gate is not None:
        _record_pending(ctx, details, result, rule, gate, inputs, facets)
        return
    draft = _find_draft(facets, rule)
    if draft is not None:
        _confirm_draft(ctx, rule, draft, inputs, details, result)
        return
    match = _match_facet(facets, rule)
    if match is not None:
        if _guidance_conflicts(rule, match):
            _record_conflict(details, result, rule, match)
            return
        _ensure_write_budget(ctx)
        updated = _update_facet(ctx, rule, match, inputs, result.warnings)
        if updated is None:
            result.skipped += 1
            return
        result.nodes_updated += 1
        ctx.budget.record_write()
        details["l4_changes"].append(
            {"action": "updated", "id": updated.id, "title": updated.title}
        )
        log_action(
            ctx.vault,
            ctx.run_id,
            PHASE,
            "update",
            node_id=updated.id,
            details={"title": updated.title, "applies_to": rule.applies_to},
        )
        return
    _ensure_write_budget(ctx)
    created = _create_facet(ctx, rule, inputs, result.warnings)
    if created is None:
        result.skipped += 1
        return
    result.nodes_created += 1
    ctx.budget.record_write()
    details["l4_changes"].append({"action": "created", "id": created.id, "title": created.title})
    facets.append(_facet_row(created))
    log_action(
        ctx.vault,
        ctx.run_id,
        PHASE,
        "create",
        node_id=created.id,
        details={"title": created.title, "applies_to": rule.applies_to},
    )


def _collect_inputs(ctx: DreamContext, watermarks: Watermarks) -> _Input:
    limit = max(0, _config_int(ctx.config, "dream.max_events_per_run", 2000))
    events = [
        event
        for event in load_unprocessed_events(ctx.vault, watermarks, limit=limit)
        if event.kind == "user_message"
    ]
    by_session: dict[str, list[EventEntry]] = {}
    for event in events:
        by_session.setdefault(event.session_id, []).append(event)
    return _Input(events=events, by_session=by_session, sessions=set(by_session))


def _rules_messages(signal_events: list[EventEntry], sessions: set[str]) -> list[dict[str, str]]:
    shown = signal_events[-MAX_PROMPT_EVENTS:]
    lines = [
        "你是 Zymor 做梦引擎的人格/风格提炼器。只输出一个 JSON 对象，不要代码块与解释。",
        "",
        "## 任务",
        (
            "从用户消息中提炼可复用的 L4 行为规则（用户明确纠正、用户确认的偏好、"
            "跨任务重复出现的交互模式）。"
        ),
        "禁止把用户原话改写成风格描述；禁止声称某种风格提升成功率；只写用户确实表达过的规则。",
        "一条规则必须在 ≥2 个不同会话中出现，且用户明确确认过，才能写入 L4。",
        "同一会话内重复出现不算多会话；confirmed 仅在用户明确确认该规则时为 true。",
        "",
        f"## 已知会话（sessions 只能从这里取）：{', '.join(sorted(sessions)) or '无'}",
        "",
        "## 用户消息（指针形式 l0:<session_id>:<seq>）",
    ]
    for event in shown:
        content = " ".join(event.content.split())
        if len(content) > MAX_EVENT_CHARS:
            content = f"{content[: int(MAX_EVENT_CHARS * 0.7)]} …（已截断）"
        lines.append(f"- [session={event.session_id}] l0:{event.session_id}:{event.seq}: {content}")
    lines.extend(
        [
            "",
            "## 输出 JSON",
            (
                '{"rules": [{"title": "", "trigger_context": "", "guidance": "", '
                '"positive_example": "", "negative_example": "", "applies_to": [], '
                '"evidence": ["l0:<session_id>:<seq>"], "sessions": [], "confirmed": false}]}'
            ),
            "字段要求：",
            "- title：规则短名 ≤80 字符；trigger_context：何时适用；guidance：行为指引 ≤400 字符。",
            "- positive_example / negative_example：用户原话或可复核的具体例子，可留空字符串。",
            (
                "- applies_to：从 debugging/architecture/code-review/research/writing/"
                "refactor/feature/planning/testing 中选，至少一项。"
            ),
            (
                "- evidence：支撑该规则的消息指针；sessions：该规则出现的会话 id"
                "（≥2 且互不相同才可能入库）。"
            ),
            '- 没有满足门槛的规则时输出 {"rules": []}。',
        ]
    )
    return [
        {"role": "system", "content": "你是严谨的人格规则提炼器，只输出 JSON。"},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _parse_rules(parsed: Any, inputs: _Input, warnings: list[str]) -> list[_Rule]:
    if not isinstance(parsed, dict):
        warnings.append("人格提炼 LLM 输出不是 JSON 对象，已跳过")
        return []
    raw_rules = parsed.get("rules")
    if not isinstance(raw_rules, list):
        warnings.append("人格提炼 LLM 输出缺少 rules 数组，已跳过")
        return []
    rules: list[_Rule] = []
    for item in raw_rules:
        if not isinstance(item, dict):
            warnings.append("人格提炼返回了非对象规则，已跳过")
            continue
        rule = _sanitize_rule(item, inputs, warnings)
        if rule is not None:
            rules.append(rule)
    return rules


def _sanitize_rule(item: dict[str, Any], inputs: _Input, warnings: list[str]) -> _Rule | None:
    trigger = _clean_text(item.get("trigger_context"))
    guidance = _clean_text(item.get("guidance"))
    if not trigger or not guidance:
        warnings.append("人格规则缺少 trigger_context 或 guidance，已跳过")
        return None
    title = _clean_text(item.get("title"))[:MAX_TITLE] or _derive_title(trigger, guidance)
    positive = _clean_text(item.get("positive_example"))
    negative = _clean_text(item.get("negative_example"))
    applies_to = _normalize_applies_to(item.get("applies_to"))
    evidence = _string_list(item.get("evidence"))
    declared = [value for value in _string_list(item.get("sessions")) if value in inputs.sessions]
    derived = _sessions_from_evidence(evidence, inputs.sessions)
    sessions = list(dict.fromkeys([*declared, *derived]))
    confirmed = item.get("confirmed") is True
    return _Rule(
        title=title,
        trigger_context=trigger,
        guidance=guidance[:MAX_SUMMARY],
        positive_example=positive,
        negative_example=negative,
        applies_to=applies_to,
        evidence=evidence,
        sessions=sessions,
        confirmed=confirmed,
    )


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()


def _derive_title(trigger: str, guidance: str) -> str:
    source = trigger or guidance
    return source.split("。")[0].split("；")[0].strip()[:MAX_TITLE] or DEFAULT_RULE_TITLE


def _normalize_applies_to(value: Any) -> list[str]:
    applies_to: list[str] = []
    for item in _string_list(value):
        if item not in applies_to:
            applies_to.append(item)
    return applies_to


def _sessions_from_evidence(evidence: list[str], observed: set[str]) -> list[str]:
    sessions: list[str] = []
    for item in evidence:
        match = re.match(r"^l0:([^:]+):\d+$", item)
        if match and match.group(1) in observed and match.group(1) not in sessions:
            sessions.append(match.group(1))
    return sessions


def _gate(rule: _Rule) -> str | None:
    if len(set(rule.sessions)) < 2:
        return "insufficient-sessions"
    if not rule.confirmed:
        return "unconfirmed"
    if not rule.applies_to:
        return "missing-applies-to"
    return None


def _record_pending(
    ctx: DreamContext,
    details: dict[str, Any],
    result: PhaseResult,
    rule: _Rule,
    reason: str,
    inputs: _Input,
    facets: list[dict[str, Any]],
) -> None:
    entry: dict[str, Any] = {
        "action": "pending",
        "title": rule.title,
        "summary": rule.guidance[:200],
        "reason": reason,
        "sessions": rule.sessions,
        "confirmed": rule.confirmed,
    }
    details["pending"].append(entry)
    result.skipped += 1
    reason_text = GATE_REASON_TEXT.get(reason, reason)
    result.warnings.append(f"人格候选未达确认门槛（{reason_text}）：{rule.title}")
    if not rule.applies_to or ctx.global_vault is None:
        details["l4_changes"].append(entry)
        return
    draft = _find_draft(facets, rule)
    if draft is None:
        _ensure_write_budget(ctx)
        node = _create_draft(ctx, rule, inputs, result.warnings)
        if node is None:
            details["l4_changes"].append(entry)
            return
        result.nodes_created += 1
        ctx.budget.record_write()
        draft = _facet_row(node)
        facets.append(draft)
        log_action(
            ctx.vault,
            ctx.run_id,
            PHASE,
            "draft",
            node_id=node.id,
            details={"title": node.title, "reason": reason, "applies_to": rule.applies_to},
        )
    draft_id = str(draft["id"])
    qid = _enqueue_proposal(ctx, draft_id, rule, reason, result.warnings)
    entry["draft_id"] = draft_id
    entry["qid"] = qid
    details["l4_changes"].append(
        {
            "action": "draft",
            "id": draft_id,
            "title": rule.title,
            "qid": qid,
            "reason": reason,
        }
    )
    log_action(
        ctx.vault,
        ctx.run_id,
        PHASE,
        "queue-proposal",
        node_id=draft_id,
        details={"qid": qid, "reason": reason, "title": rule.title},
    )


def _find_draft(facets: list[dict[str, Any]], rule: _Rule) -> dict[str, Any] | None:
    best: tuple[int, str, dict[str, Any]] | None = None
    for row in facets:
        if str(row.get("status")) != "hypothesis":
            continue
        if DRAFT_TAG not in (row.get("tags") or []):
            continue
        if not set(row.get("applies_to") or []) & set(rule.applies_to):
            continue
        score = _title_similarity(rule.title, str(row.get("title") or ""))
        if score <= 0:
            continue
        key = (score, str(row.get("updated_at") or ""), row)
        if best is None or key[:2] > best[:2]:
            best = key
    return best[2] if best is not None else None


def _facet_row(node: MemoryNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "title": node.title,
        "summary": node.summary,
        "layer": node.layer,
        "status": node.status,
        "tags": list(node.tags),
        "applies_to": list(node.applies_to),
        "updated_at": node.updated_at,
    }


def _create_draft(
    ctx: DreamContext, rule: _Rule, inputs: _Input, warnings: list[str]
) -> MemoryNode | None:
    if ctx.global_vault is None:
        return None
    evidence = _rule_evidence(ctx, rule, inputs)
    body = _axioms_body(rule, warnings)
    try:
        outcome = memory_write(
            ctx.global_vault,
            ctx.config,
            title=rule.title,
            summary=rule.guidance[:MAX_SUMMARY],
            body=body,
            category="axioms",
            layer="L4",
            session_id=ctx.run_id,
            applies_to=rule.applies_to,
            tags=[DRAFT_TAG],
            status="hypothesis",
            confidence=0.4,
            evidence=evidence,
            source=f"dream:{ctx.run_id}",
            created_by="dream",
            client=None,
            command="dream:persona",
        )
    except ZymorError as error:
        warnings.append(f"人格 L4 draft 写入失败：{error.message}")
        return None
    _capture_outcome_warnings(outcome, warnings)
    if outcome.node_id is None:
        return None
    try:
        node, _ = read_node(ctx.global_vault, outcome.node_id)
    except ZymorError as error:
        warnings.append(f"人格 L4 draft 写入后回读失败：{error.message}")
        return None
    if node.layer != "L4" or node.status != "hypothesis" or DRAFT_TAG not in node.tags:
        warnings.append(f"人格 L4 draft 写入结果异常（layer={node.layer}, status={node.status}）")
        return None
    return node


def _enqueue_proposal(
    ctx: DreamContext,
    draft_id: str,
    rule: _Rule,
    reason: str,
    warnings: list[str],
) -> str | None:
    if ctx.global_vault is None:
        return None
    question = f"人格规则候选「{rule.title}」：{rule.guidance}（确认后升为 L4 verified）"
    try:
        append_dissonance(
            ctx.global_vault,
            new_id=draft_id,
            old_id=None,
            reason=GATE_REASON_TEXT.get(reason, reason),
            entry_type="persona-proposal",
            question=question[:MAX_PROPOSAL_QUESTION],
        )
    except ZymorError as error:
        warnings.append(f"人格候选进入晨间队列失败：{error.message}")
        return None
    return _queue_qid(ctx.global_vault, draft_id)


def _queue_qid(vault: Vault, draft_id: str) -> str | None:
    for entry in read_dissonance(vault):
        if entry.get("type") != "persona-proposal":
            continue
        if entry.get("new_id") != draft_id or entry.get("state") != "open":
            continue
        qid = entry.get("qid")
        return str(qid) if qid else None
    return None


def _confirm_draft(
    ctx: DreamContext,
    rule: _Rule,
    row: dict[str, Any],
    inputs: _Input,
    details: dict[str, Any],
    result: PhaseResult,
) -> None:
    if ctx.global_vault is None:
        return
    _ensure_write_budget(ctx)
    try:
        node, _ = read_node(ctx.global_vault, str(row["id"]))
    except ZymorError as error:
        result.warnings.append(f"人格 L4 draft 读取失败：{error.message}")
        result.skipped += 1
        return
    timestamp = now_iso()
    body = _merge_facet_body(node, rule, timestamp, result.warnings)
    if body is None:
        result.warnings.append(f"人格 L4 draft 合并后正文超限，已保留原节点：{node.id}")
        result.skipped += 1
        return
    evidence = list(dict.fromkeys([*node.evidence, *_rule_evidence(ctx, rule, inputs)]))
    applies_to = list(dict.fromkeys([*node.applies_to, *rule.applies_to]))
    tags = [tag for tag in node.tags if tag != DRAFT_TAG]
    updates: dict[str, Any] = {
        "body": body,
        "summary": rule.guidance[:MAX_SUMMARY],
        "evidence": evidence,
        "applies_to": applies_to,
        "tags": tags,
        "status": "verified",
        "confidence": max(0.7, node.confidence),
        "updated_at": timestamp,
    }
    try:
        updated = update_node(ctx.global_vault, node.id, updates, command="dream:persona")
    except ZymorError as error:
        result.warnings.append(f"人格 L4 draft 升级失败 {node.id}：{error.message}")
        result.skipped += 1
        return
    qid = _mark_proposal_answered(ctx, updated.id, result.warnings)
    result.nodes_updated += 1
    ctx.budget.record_write()
    row.update(_facet_row(updated))
    details["l4_changes"].append(
        {
            "action": "draft-confirmed",
            "id": updated.id,
            "title": updated.title,
            "qid": qid,
        }
    )
    log_action(
        ctx.vault,
        ctx.run_id,
        PHASE,
        "confirm-draft",
        node_id=updated.id,
        details={"qid": qid, "title": updated.title, "applies_to": rule.applies_to},
    )


def _mark_proposal_answered(ctx: DreamContext, draft_id: str, warnings: list[str]) -> str | None:
    if ctx.global_vault is None:
        return None
    for entry in read_dissonance(ctx.global_vault):
        if entry.get("type") != "persona-proposal":
            continue
        if entry.get("new_id") != draft_id or entry.get("state") != "open":
            continue
        qid = entry.get("qid")
        if qid is None:
            return None
        try:
            update_dissonance(ctx.global_vault, str(qid), state="answered")
        except ZymorError as error:
            warnings.append(f"队列项 {qid} 置 answered 失败：{error.message}")
        return str(qid)
    return None


def _ensure_write_budget(ctx: DreamContext) -> None:
    if ctx.budget.can_write():
        return
    reason = ctx.budget.exhausted() or "节点变更达到上限"
    ctx.budget.stopped_reason = reason
    raise DreamBudgetExceeded(reason)


def _record_conflict(
    details: dict[str, Any], result: PhaseResult, rule: _Rule, row: dict[str, Any]
) -> None:
    entry = {
        "action": "pending-conflict",
        "title": rule.title,
        "existing_id": str(row["id"]),
        "existing_title": str(row.get("title") or ""),
        "reason": "guidance-conflict",
    }
    details["pending_conflict"].append(entry)
    details["l4_changes"].append(entry)
    result.skipped += 1
    result.warnings.append(
        f"人格规则与既有 facet 冲突且无法合并，已进入待确认：{rule.title} ↔ {row['id']}"
    )


def _load_facets(ctx: DreamContext, global_vault: Vault) -> list[dict[str, Any]]:
    limit = max(1, _config_int(ctx.config, "dream.max_candidates", 300))
    try:
        return Index(global_vault.root).layer_rows(
            "L4", statuses=["verified", "hypothesis", "disputed"], limit=limit
        )
    except ZymorError as error:
        ctx.warn(f"人格目标扫描失败：{error.message}")
        return []


def _match_facet(facets: list[dict[str, Any]], rule: _Rule) -> dict[str, Any] | None:
    best: tuple[int, str, dict[str, Any]] | None = None
    for row in facets:
        applies_to = set(row.get("applies_to") or [])
        if not applies_to & set(rule.applies_to):
            continue
        score = _title_similarity(rule.title, str(row.get("title") or ""))
        if score <= 0:
            continue
        key = (score, str(row.get("updated_at") or ""), row)
        if best is None or key[:2] > best[:2]:
            best = key
    return best[2] if best is not None else None


def _title_similarity(left: str, right: str) -> int:
    left_norm = _normalize_title(left)
    right_norm = _normalize_title(right)
    if left_norm and left_norm == right_norm:
        return 100
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    return len(left_tokens & right_tokens)


def _normalize_title(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _guidance_conflicts(rule: _Rule, row: dict[str, Any]) -> bool:
    old_tokens = _tokens(str(row.get("summary") or ""))
    new_tokens = _tokens(rule.guidance)
    if not old_tokens or not new_tokens:
        return False
    return not (old_tokens & new_tokens)


def _create_facet(
    ctx: DreamContext, rule: _Rule, inputs: _Input, warnings: list[str]
) -> MemoryNode | None:
    if ctx.global_vault is None:
        return None
    evidence = _rule_evidence(ctx, rule, inputs)
    body = _axioms_body(rule, warnings)
    try:
        outcome = memory_write(
            ctx.global_vault,
            ctx.config,
            title=rule.title,
            summary=rule.guidance[:MAX_SUMMARY],
            body=body,
            category="axioms",
            layer="L4",
            session_id=ctx.run_id,
            applies_to=rule.applies_to,
            status="verified",
            confidence=0.7,
            evidence=evidence,
            source=f"dream:{ctx.run_id}",
            created_by="dream",
            client=None,
            command="dream:persona",
        )
    except ZymorError as error:
        warnings.append(f"人格规则写入失败：{error.message}")
        return None
    _capture_outcome_warnings(outcome, warnings)
    if outcome.node_id is None:
        return None
    try:
        node, _ = read_node(ctx.global_vault, outcome.node_id)
    except ZymorError as error:
        warnings.append(f"人格规则写入后回读失败：{error.message}")
        return None
    if node.layer != "L4" or node.status != "verified":
        warnings.append(f"人格规则写入结果异常（layer={node.layer}, status={node.status}）")
        return None
    return node


def _update_facet(
    ctx: DreamContext, rule: _Rule, row: dict[str, Any], inputs: _Input, warnings: list[str]
) -> MemoryNode | None:
    if ctx.global_vault is None:
        return None
    try:
        node, _ = read_node(ctx.global_vault, str(row["id"]))
    except ZymorError as error:
        warnings.append(f"人格 facet 读取失败：{error.message}")
        return None
    timestamp = now_iso()
    body = _merge_facet_body(node, rule, timestamp, warnings)
    if body is None:
        warnings.append(f"人格 facet 合并后正文超限，已保留原节点：{node.id}")
        return None
    evidence = list(dict.fromkeys([*node.evidence, *_rule_evidence(ctx, rule, inputs)]))
    applies_to = list(dict.fromkeys([*node.applies_to, *rule.applies_to]))
    updates: dict[str, Any] = {
        "body": body,
        "summary": rule.guidance[:MAX_SUMMARY],
        "evidence": evidence,
        "applies_to": applies_to,
        "status": "verified",
        "confidence": max(0.7, node.confidence),
        "updated_at": timestamp,
    }
    try:
        return update_node(ctx.global_vault, node.id, updates, command="dream:persona")
    except ZymorError as error:
        warnings.append(f"人格 facet 更新失败 {node.id}：{error.message}")
        return None


def _rule_evidence(ctx: DreamContext, rule: _Rule, inputs: _Input) -> list[str]:
    evidence = list(dict.fromkeys(rule.evidence))
    for session_id in rule.sessions:
        events = inputs.by_session.get(session_id) or []
        if not events:
            continue
        pointer = f"l0:{session_id}:{events[0].seq}"
        if pointer not in evidence:
            evidence.append(pointer)
    if not evidence:
        evidence.append(f"persona:{ctx.run_id}")
    return evidence


def _capture_outcome_warnings(outcome: Any, warnings: list[str]) -> None:
    for message in outcome.warnings:
        if any(marker in message for marker in NO_CLIENT_MARKERS):
            continue
        warnings.append(message)


def _axioms_body(rule: _Rule, warnings: list[str]) -> str:
    pairs = [
        ("触发情境", rule.trigger_context),
        ("行为指引", rule.guidance),
        ("正例", rule.positive_example or MISSING_SECTION_TEXT),
        ("反例", rule.negative_example or MISSING_SECTION_TEXT),
    ]
    return _fit_body(rule.title, pairs, warnings)


def _merge_facet_body(
    node: MemoryNode, rule: _Rule, timestamp: str, warnings: list[str]
) -> str | None:
    main, history = _split_history(node.body)
    preamble, sections = _split_sections(main)
    title = _preamble_title(preamble) or node.title
    values: dict[str, str] = {}
    order: list[str] = []
    for name, content in sections:
        if name not in values:
            values[name] = content
            order.append(name)
        elif content:
            values[name] = f"{values[name]}\n{content}".strip()
    old_guidance = values.get("行为指引", "")
    trigger = values.get("触发情境") or rule.trigger_context
    positive = values.get("正例", "")
    negative = values.get("反例", "")
    base_pairs = [
        ("触发情境", trigger),
        ("行为指引", rule.guidance),
        ("正例", positive or MISSING_SECTION_TEXT),
        ("反例", negative or MISSING_SECTION_TEXT),
    ]
    extras = [
        (name, values[name]) for name in order if name not in {pair[0] for pair in base_pairs}
    ]
    body = _compose_body(title, [*base_pairs, *extras])
    if not _body_fits(body):
        return None
    with_examples = _with_examples(base_pairs, rule)
    candidate = _compose_body(title, [*with_examples, *extras])
    if _body_fits(candidate):
        body = candidate
    record = (
        f"- {timestamp}：行为指引由「{_clip_text(old_guidance or '（空）', 60)}」"
        f"更新为「{_clip_text(rule.guidance, 60)}」"
    )
    for history_line in history:
        record = f"{history_line}\n{record}"
        candidate = _with_history(body, record)
        if _body_fits(candidate):
            return candidate
        record = record.split("\n")[-1]
    candidate = _with_history(body, record)
    if _body_fits(candidate):
        return candidate
    for clip in (40, 20, 8):
        short = f"- {timestamp}：行为指引更新为「{_clip_text(rule.guidance, clip)}」"
        candidate = _with_history(body, short)
        if _body_fits(candidate):
            warnings.append("人格 facet 变更记录因正文上限被压缩")
            return candidate
    return None


def _with_examples(pairs: list[tuple[str, str]], rule: _Rule) -> list[tuple[str, str]]:
    updated: list[tuple[str, str]] = []
    for name, content in pairs:
        if name == "正例" and rule.positive_example:
            content = _append_example(content, rule.positive_example)
        elif name == "反例" and rule.negative_example:
            content = _append_example(content, rule.negative_example)
        updated.append((name, content))
    return updated


def _append_example(existing: str, example: str) -> str:
    example = example.strip()
    if not example or example in existing:
        return existing
    if not existing or existing == MISSING_SECTION_TEXT:
        return example
    return f"{existing}\n- {example}"


def _with_history(body: str, record: str) -> str:
    return f"{body.rstrip()}\n\n{HISTORY_HEADER}\n{record.strip()}\n"


def _split_history(body: str) -> tuple[str, list[str]]:
    lines = body.split("\n")
    for position, line in enumerate(lines):
        if line.strip() == HISTORY_HEADER:
            main = "\n".join(lines[:position]).rstrip()
            history = [item.strip() for item in lines[position + 1 :] if item.strip()]
            return main, history
    return body.rstrip(), []


def _split_sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return (
        "\n".join(preamble).strip(),
        [(name, "\n".join(buffer).strip()) for name, buffer in sections],
    )


def _preamble_title(preamble: str) -> str:
    for line in preamble.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _compose_body(title: str, pairs: list[tuple[str, str]]) -> str:
    lines = [f"# {title}", ""]
    for name, content in pairs:
        lines.extend([f"## {name}", content.strip(), ""])
    return "\n".join(lines).rstrip("\n") + "\n"


def _fit_body(title: str, pairs: list[tuple[str, str]], warnings: list[str]) -> str:
    heading = f"# {title}\n" + "".join(f"## {name}\n" for name, _ in pairs)
    fixed_tokens = estimate_tokens(heading)
    fixed_cjk = _cjk_count(heading)
    count = max(1, len(pairs))
    margin = count * 6
    per_tokens = max(1, (MAX_BODY_TOKENS - fixed_tokens - margin) // count)
    per_cjk = max(1, (MAX_BODY_CJK - fixed_cjk - margin) // count)
    clipped_pairs: list[tuple[str, str]] = []
    truncated = False
    for name, content in pairs:
        text = content.strip() or MISSING_SECTION_TEXT
        clipped, changed = _clip_text_limits(text, per_tokens, per_cjk)
        clipped_pairs.append((name, clipped))
        truncated = truncated or changed
    if truncated:
        warnings.append("人格规则正文超过 §1.4 上限，已按小节截断")
    body = _compose_body(title, clipped_pairs)
    if _body_fits(body):
        return body
    fallback = [(name, MISSING_SECTION_TEXT) for name, _ in pairs]
    return _compose_body(title, fallback)


def _body_fits(body: str) -> bool:
    return estimate_tokens(body) <= MAX_BODY_TOKENS and _cjk_count(body) <= MAX_BODY_CJK


def _clip_text_limits(text: str, token_budget: int, cjk_budget: int) -> tuple[str, bool]:
    if estimate_tokens(text) <= token_budget and _cjk_count(text) <= cjk_budget:
        return text, False
    kept = ""
    for char in text:
        trial = kept + char
        if estimate_tokens(trial) <= token_budget and _cjk_count(trial) <= cjk_budget:
            kept = trial
        else:
            break
    return (kept.strip() or MISSING_SECTION_TEXT), True


def _clip_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


def _cjk_count(text: str) -> int:
    from zymor.tokenizer import _is_cjk

    return sum(1 for char in text if _is_cjk(char))


def _tokens(text: str) -> set[str]:
    return {term for term in tokenize_terms(text) if len(term) >= 2}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    result: list[str] = []
    for item in items:
        if isinstance(item, str):
            text = item.strip()
            if text:
                result.append(text)
    return result


def _error_text(error: Exception) -> str:
    if isinstance(error, ZymorError):
        return error.message
    return f"{type(error).__name__}: {error}"


def _config_int(config: Config, key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default
