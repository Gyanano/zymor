from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from zymor.config import Config
from zymor.dream.promote import promote_l2_to_l3
from zymor.dream.state import Watermarks, load_unprocessed_events, log_action
from zymor.dream.types import (
    DreamBudgetExceeded,
    DreamContext,
    DreamPlan,
    PhaseResult,
)
from zymor.errors import NotFoundError, ValidationError, ZymorError
from zymor.llm import JsonChatClient
from zymor.model.event import EventEntry
from zymor.model.focus import FocusFile
from zymor.model.node import MAX_SUMMARY, MAX_TITLE, REQUIRED_H2, MemoryNode
from zymor.promotion import auto_promote
from zymor.store.index import Index
from zymor.store.nodes import read_node, update_node
from zymor.tokenizer import build_match_query, tokenize_terms
from zymor.util.ids import is_memory_id
from zymor.util.time import days_ago_iso, now_iso
from zymor.vault import Vault
from zymor.write_service import WriteOutcome, append_dissonance, memory_close, memory_write

PHASE = "nrem"
HEDGES = ("可能", "据说", "待验证")
DECISIONS = ("create", "append-evidence", "supersede", "conflict")
VALID_LAYERS = ("L2", "L3", "L4")
VALID_CATEGORIES = tuple(REQUIRED_H2)
INVALID_TARGET_STATUSES = {"deprecated", "disputed"}
NO_CLIENT_MARKERS = ("未提供 LLM 客户端", "已跳过去重合并")
MAX_EVENT_CHARS = 1200
MAX_TOOL_EVENT_CHARS = 500
MAX_TARGETS = 8
MAX_BODY_EXCERPT = 600
RECENT_WINDOW_DAYS = 30
DEFAULT_BATCH_EVENTS = 200
DEFAULT_MAX_EVENTS = 2000
DEFAULT_MAX_CANDIDATES = 300
POINTER_RE = re.compile(r"^l0:[^:]+:\d+$")
GATE_RUBRIC = (
    "丢弃：预训练常识；当前仓库 grep 或 git 一次调用即可获知的事实；"
    "已解决的一次性报错；寒暄与过程对话。"
    "保留：踩坑因果（动作、失败原因、避开方法至少含后两者）；"
    "架构权衡（选定项、放弃项、约束）；新概念与新 API 约束；"
    "用户偏好或纠正；跨任务经验。"
    "日期、数量、专名与 hedge（可能 / 据说 / 待验证）必须保留，禁止去含糊化。"
)


@dataclass
class _Candidate:
    title: str
    summary: str
    body: str
    category: str
    layer: str
    confidence: float
    evidence: list[str]
    tags: list[str]
    links: list[str]
    session_id: str


@dataclass
class _Cluster:
    candidates: list[_Candidate]
    targets: list[dict[str, Any]]
    target_ids: list[str]


@dataclass
class _Decision:
    action: str
    target_id: str | None
    reason: str
    summary_patch: str | None


def run_nrem(ctx: DreamContext, plan: DreamPlan, watermarks: Watermarks) -> PhaseResult:
    """入参：做梦上下文、计划与水位线。出参：NREM 阶段结果。消化 L0 事件并维护 L2/L3 与焦点胶囊。"""
    result = PhaseResult(phase=PHASE)
    details: dict[str, Any] = {
        "processed_session_seq": {},
        "stopped": None,
        "dropped_singletons": 0,
        "clusters": 0,
        "actions": {
            "create": 0,
            "append-evidence": 0,
            "supersede": 0,
            "conflict": 0,
            "degraded": 0,
            "skipped": 0,
        },
        "batches": 0,
        "skipped_l4": 0,
        "promotions": [],
        "stale_focuses": [],
        "extract_client": False,
        "dedupe_client": False,
    }
    warnings = result.warnings
    stopped: str | None = None
    processed_seq: dict[str, int] = {}
    batch_size = max(1, _config_int(ctx.config, "dream.batch_events", DEFAULT_BATCH_EVENTS))
    event_limit = max(0, _config_int(ctx.config, "dream.max_events_per_run", DEFAULT_MAX_EVENTS))
    events = load_unprocessed_events(ctx.vault, watermarks, limit=event_limit)
    extract = ctx.client("extract")
    details["extract_client"] = extract is not None
    details["dedupe_client"] = ctx.client("dedupe") is not None
    if events and extract is None:
        warnings.append("extract 角色未配置 LLM 客户端：跳过 L0 事件消化，不推进任何会话水位线")
    if extract is not None:
        for session_id, session_events in _group_by_session(events).items():
            session_ok = True
            session_max = max(event.seq for event in session_events)
            project = _session_project(ctx.vault, session_id)
            for batch in _chunks(session_events, batch_size):
                if stopped is not None:
                    session_ok = False
                    break
                tokens_before, usd_before = _budget_totals(ctx)
                try:
                    candidates = _extract_candidates(extract, batch, warnings)
                except DreamBudgetExceeded as error:
                    stopped = _stop_phase(ctx, error)
                    session_ok = False
                    break
                except Exception as error:
                    result.errors.append(f"会话 {session_id} 候选提取失败：{_error_text(error)}")
                    session_ok = False
                    break
                details["batches"] += 1
                result.processed_events += len(batch)
                result.candidates += len(candidates)
                _log_llm_action(
                    ctx,
                    "extract",
                    {
                        "session_id": session_id,
                        "events": len(batch),
                        "candidates": len(candidates),
                    },
                    tokens_before,
                    usd_before,
                )
                try:
                    _consolidate(
                        ctx,
                        session_id,
                        project,
                        candidates,
                        details,
                        result,
                        warnings,
                    )
                except DreamBudgetExceeded as error:
                    stopped = _stop_phase(ctx, error)
                    session_ok = False
                    break
                except Exception as error:
                    result.errors.append(f"会话 {session_id} 候选巩固失败：{_error_text(error)}")
                    session_ok = False
                    break
            if session_ok and stopped is None:
                processed_seq[session_id] = session_max
    promotion = promote_l2_to_l3(ctx)
    result.nodes_updated += len(promotion.promoted)
    details["promotions"] = promotion.promoted
    warnings.extend(promotion.warnings)
    if promotion.stopped is not None:
        stopped = stopped or promotion.stopped
        details["stopped_by"] = "promote"
    cross = auto_promote(ctx)
    result.nodes_updated += len(cross.promoted)
    details["cross_project_promotion"] = cross.to_dict()
    warnings.extend(cross.warnings)
    if cross.stopped is not None:
        stopped = stopped or cross.stopped
        details["stopped_by"] = details.get("stopped_by") or "cross-promote"
    for session_id in plan.stale_focuses:
        entry: dict[str, Any] = {"session_id": session_id}
        if not ctx.budget.can_write():
            stopped = stopped or (ctx.budget.exhausted() or "节点变更达到上限")
            ctx.budget.stopped_reason = stopped
            entry["action"] = "skipped-budget"
            details["stale_focuses"].append(entry)
            details["stopped_by"] = "stale_focus"
            continue
        try:
            closed = _close_stale_focus(ctx, session_id)
        except ZymorError as error:
            warnings.append(f"补写焦点胶囊失败 {session_id}：{error.message}")
            entry["action"] = "error"
            entry["error"] = error.message
        except Exception as error:
            warnings.append(f"补写焦点胶囊失败 {session_id}：{_error_text(error)}")
            entry["action"] = "error"
            entry["error"] = _error_text(error)
        else:
            action_name = str(closed.get("action"))
            entry["action"] = action_name
            entry["capsule_id"] = closed.get("id")
            if action_name == "appended":
                result.nodes_updated += 1
            else:
                result.nodes_created += 1
            ctx.budget.record_write()
            log_action(
                ctx.vault,
                ctx.run_id,
                PHASE,
                "capsule",
                node_id=closed.get("id"),
                details={"session_id": session_id, "action": closed.get("action")},
            )
        details["stale_focuses"].append(entry)
    if stopped is not None and ctx.budget.stopped_reason is None:
        ctx.budget.stopped_reason = stopped
    details["processed_session_seq"] = processed_seq
    details["stopped"] = stopped
    result.details = details
    log_action(
        ctx.vault,
        ctx.run_id,
        PHASE,
        "finish",
        details={
            "processed_events": result.processed_events,
            "candidates": result.candidates,
            "nodes_created": result.nodes_created,
            "nodes_updated": result.nodes_updated,
            "dropped_singletons": details["dropped_singletons"],
            "stopped": stopped,
        },
    )
    return result


def _extract_candidates(
    client: JsonChatClient,
    batch: list[EventEntry],
    warnings: list[str],
) -> list[_Candidate]:
    messages = _extract_messages(batch)
    parsed, _response = client.complete_json(messages, caller="dream:extract")
    if not isinstance(parsed, dict):
        raise ValidationError("extract 输出必须是 JSON 对象")
    raw_candidates = parsed.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ValidationError("extract 输出缺少 candidates 数组")
    session_id = batch[0].session_id
    candidates: list[_Candidate] = []
    for item in raw_candidates:
        if not isinstance(item, dict):
            warnings.append("extract 返回了非对象候选，已跳过")
            continue
        candidate = _sanitize_candidate(item, session_id, warnings)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _extract_messages(batch: list[EventEntry]) -> list[dict[str, str]]:
    lines = [
        "你是 Zymor 做梦引擎 NREM 阶段的候选提取器。只输出一个 JSON 对象，不要代码块与解释。",
        "",
        "## 写作门控清单",
        GATE_RUBRIC,
        "",
        "## L0 事件（长内容只保留头尾摘要，原文留在 L0；指针形式 l0:<session_id>:<seq>）",
    ]
    lines.extend(_render_event(event) for event in batch)
    lines.extend(
        [
            "",
            "## 输出 JSON",
            (
                '{"candidates": [{"title": "", "summary": "", "body": "", '
                '"category": "episodic|semantic|procedural|axioms", "layer": "L2|L3|L4", '
                '"confidence": 0.4, "evidence": [], "source_event_ids": ["l0:<session_id>:<seq>"], '
                '"tags": [], "links": []}]}'
            ),
            "要求：",
            '- 每个候选是一个可复用认知单元；没有值得保留的内容时输出 {"candidates": []}。',
            (
                "- title ≤80 字符、summary ≤400 字符且单段；body 不含 H1，必须含该 category "
                "的必需 H2（episodic: 背景/决策/结果与教训；semantic: 结论/证据；"
                "procedural: 适用场景/步骤；axioms: 触发情境/行为指引/正例/反例）。"
            ),
            "- 日期、数量、专名与 hedge（可能 / 据说 / 待验证）逐字保留，不做归一化改写。",
            "- source_event_ids 必须使用 l0:<session_id>:<seq> 指针形式。",
        ]
    )
    return [
        {"role": "system", "content": "你是严谨的记忆候选提取器，只输出 JSON。"},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _render_event(event: EventEntry) -> str:
    pointer = f"l0:{event.session_id}:{event.seq}"
    content = " ".join(event.content.split())
    limit = MAX_TOOL_EVENT_CHARS if event.kind in {"tool_call", "tool_result"} else MAX_EVENT_CHARS
    if len(content) > limit:
        head = content[: int(limit * 0.6)]
        tail = content[-int(limit * 0.3) :]
        content = f"{head} …（已截断，原文见 {pointer}）… {tail}"
    meta_text = ""
    if event.meta:
        tool = event.meta.get("tool")
        if tool:
            meta_text += f" tool={tool}"
        path = event.meta.get("path")
        if path:
            meta_text += f" path={path}"
        exit_code = event.meta.get("exit_code")
        if exit_code is not None:
            meta_text += f" exit_code={exit_code}"
    return f"- {pointer} [{event.ts}] kind={event.kind}{meta_text}: {content}"


def _sanitize_candidate(
    item: dict[str, Any], session_id: str, warnings: list[str]
) -> _Candidate | None:
    title = " ".join(str(item.get("title") or "").split())[:MAX_TITLE]
    if not title:
        warnings.append("extract 候选缺少 title，已跳过")
        return None
    summary = " ".join(str(item.get("summary") or "").split())[:MAX_SUMMARY]
    if not summary:
        summary = title
    category_raw = item.get("category")
    category = str(category_raw) if isinstance(category_raw, str) else ""
    if category not in VALID_CATEGORIES:
        warnings.append(f"候选 {title} 的 category 非法，已按 semantic 处理")
        category = "semantic"
    layer_raw = item.get("layer")
    layer = str(layer_raw) if isinstance(layer_raw, str) else "L3"
    if layer not in VALID_LAYERS:
        warnings.append(f"候选 {title} 的 layer 非法，已按 L3 处理")
        layer = "L3"
    body = _sanitize_body(str(item.get("body") or ""))
    if not body:
        body = summary
    confidence_raw = item.get("confidence")
    confidence = 0.4
    if isinstance(confidence_raw, (int, float)) and not isinstance(confidence_raw, bool):
        confidence = min(1.0, max(0.0, float(confidence_raw)))
    evidence = _string_list(item.get("evidence"))
    evidence.extend(_source_pointers(item.get("source_event_ids"), session_id))
    tags = _normalize_tags(item.get("tags"))
    links = [str(value) for value in _string_list(item.get("links")) if is_memory_id(value)]
    return _Candidate(
        title=title,
        summary=summary,
        body=body,
        category=category,
        layer=layer,
        confidence=confidence,
        evidence=evidence,
        tags=tags,
        links=links,
        session_id=session_id,
    )


def _sanitize_body(body: str) -> str:
    lines: list[str] = []
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip().startswith("# "):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _source_pointers(values: Any, session_id: str) -> list[str]:
    pointers: list[str] = []
    for value in _string_list(values):
        if POINTER_RE.match(value):
            pointers.append(value)
        elif value.isdigit():
            pointers.append(f"l0:{session_id}:{value}")
        else:
            pointers.append(value)
    return pointers


def _normalize_tags(values: Any) -> list[str]:
    tags: list[str] = []
    for value in _string_list(values):
        tag = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def _consolidate(
    ctx: DreamContext,
    session_id: str,
    project: str | None,
    candidates: list[_Candidate],
    details: dict[str, Any],
    result: PhaseResult,
    warnings: list[str],
) -> None:
    clusters, dropped = _build_clusters(ctx, candidates)
    details["dropped_singletons"] += dropped
    result.skipped += dropped
    details["clusters"] += len(clusters)
    if dropped:
        log_action(
            ctx.vault,
            ctx.run_id,
            PHASE,
            "gate-dropped",
            details={"count": dropped, "session_id": session_id},
        )
    eager = bool(ctx.config.get("dream.eager_singletons", False))
    for cluster in clusters:
        if not cluster.targets and len(cluster.candidates) == 1 and eager:
            _execute_create(ctx, cluster, session_id, project, details, result, warnings)
            continue
        dedupe = ctx.client("dedupe")
        if dedupe is None:
            _fallback_create(
                ctx,
                cluster,
                session_id,
                project,
                details,
                result,
                warnings,
                "dedupe 角色未配置 LLM 客户端，已按 §4.8 写新 hypothesis",
            )
            continue
        decision = _ask_consolidation(ctx, cluster, dedupe, warnings)
        if decision is None:
            _fallback_create(
                ctx,
                cluster,
                session_id,
                project,
                details,
                result,
                warnings,
                "巩固 LLM 调用失败或输出不合法，已按 §4.8 写新 hypothesis",
            )
            continue
        _execute_decision(ctx, cluster, decision, session_id, project, details, result, warnings)


def _build_clusters(ctx: DreamContext, candidates: list[_Candidate]) -> tuple[list[_Cluster], int]:
    if not candidates:
        return [], 0
    index = Index(ctx.vault.root)
    recent = _recent_rows(ctx, index)
    hits = [_find_hits(index, candidate, recent) for candidate in candidates]
    groups = _group_candidates(candidates, hits)
    eager = bool(ctx.config.get("dream.eager_singletons", False))
    clusters: list[_Cluster] = []
    dropped = 0
    for group in groups:
        target_map: dict[str, dict[str, Any]] = {}
        for position in group:
            for row in hits[position]:
                target_map.setdefault(str(row["id"]), row)
        targets = _sort_targets(list(target_map.values()))[:MAX_TARGETS]
        if not targets and len(group) == 1 and not eager:
            dropped += 1
            continue
        clusters.append(
            _Cluster(
                candidates=[candidates[position] for position in group],
                targets=targets,
                target_ids=[str(row["id"]) for row in targets],
            )
        )
    return clusters, dropped


def _recent_rows(ctx: DreamContext, index: Index) -> list[dict[str, Any]]:
    limit = max(1, _config_int(ctx.config, "dream.max_candidates", DEFAULT_MAX_CANDIDATES))
    try:
        return index.recent_rows(days_ago_iso(RECENT_WINDOW_DAYS), limit=limit)
    except ZymorError:
        return []


def _find_hits(
    index: Index, candidate: _Candidate, recent: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    hits: dict[str, dict[str, Any]] = {}
    query_text = " ".join([candidate.title, candidate.summary, *candidate.tags]).strip()
    query = build_match_query(query_text)
    if query:
        try:
            for row in index.candidate_rows(query, limit=MAX_TARGETS * 4):
                hits[str(row["id"])] = row
        except ZymorError:
            pass
    for row in recent:
        node_id = str(row["id"])
        if node_id in hits:
            continue
        if _row_overlaps(row, candidate):
            hits[node_id] = row
    ranked = sorted(hits.values(), key=lambda row: _target_key(row, candidate))
    return ranked[:MAX_TARGETS]


def _target_key(row: dict[str, Any], candidate: _Candidate) -> tuple[int, int, float, str]:
    title_equal = str(row.get("title") or "").strip() == candidate.title.strip()
    tag_overlap = bool(set(candidate.tags) & set(row.get("tags") or []))
    rank = row.get("rank")
    return (
        0 if title_equal else 1,
        0 if tag_overlap else 1,
        float(rank) if rank is not None else 1e9,
        str(row.get("updated_at") or ""),
    )


def _sort_targets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: str(row.get("updated_at") or ""), reverse=True)


def _row_overlaps(row: dict[str, Any], candidate: _Candidate) -> bool:
    if set(candidate.tags) & set(row.get("tags") or []):
        return True
    row_tokens = _tokens(str(row.get("title") or ""))
    return bool(row_tokens & _tokens(candidate.title))


def _group_candidates(
    candidates: list[_Candidate], hits: list[list[dict[str, Any]]]
) -> list[list[int]]:
    parent = list(range(len(candidates)))

    def find(position: int) -> int:
        while parent[position] != position:
            parent[position] = parent[parent[position]]
            position = parent[position]
        return position

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    hit_ids = [{str(row["id"]) for row in rows} for rows in hits]
    for left in range(len(candidates)):
        for right in range(left + 1, len(candidates)):
            if hit_ids[left] & hit_ids[right]:
                union(left, right)
            elif _candidates_overlap(candidates[left], candidates[right]):
                union(left, right)
    groups: dict[int, list[int]] = {}
    for position in range(len(candidates)):
        groups.setdefault(find(position), []).append(position)
    return list(groups.values())


def _candidates_overlap(left: _Candidate, right: _Candidate) -> bool:
    if set(left.tags) & set(right.tags):
        return True
    left_tokens = _tokens(" ".join([left.title, left.summary]))
    right_tokens = _tokens(" ".join([right.title, right.summary]))
    return bool(left_tokens & right_tokens)


def _tokens(text: str) -> set[str]:
    return {term for term in tokenize_terms(text) if len(term) >= 2}


def _ask_consolidation(
    ctx: DreamContext, cluster: _Cluster, client: JsonChatClient, warnings: list[str]
) -> _Decision | None:
    messages = _consolidation_messages(cluster)
    tokens_before, usd_before = _budget_totals(ctx)
    try:
        parsed, _response = client.complete_json(messages, caller="dream:dedupe")
    except DreamBudgetExceeded:
        raise
    except Exception as error:
        warnings.append(f"巩固 LLM 调用失败：{_error_text(error)}")
        return None
    _log_llm_action(
        ctx,
        "consolidate",
        {
            "candidates": len(cluster.candidates),
            "targets": cluster.target_ids,
        },
        tokens_before,
        usd_before,
    )
    return _parse_decision(parsed, cluster)


def _consolidation_messages(cluster: _Cluster) -> list[dict[str, str]]:
    lines = [
        "你是 Zymor 做梦引擎的巩固判定器。只输出一个 JSON 对象，不要代码块与解释。",
        "",
        "## 写作门控清单",
        GATE_RUBRIC,
        "",
        "## 候选",
    ]
    for position, candidate in enumerate(cluster.candidates, start=1):
        body = " ".join(candidate.body.split())[:MAX_BODY_EXCERPT]
        lines.append(
            f"{position}. title={candidate.title} category={candidate.category} "
            f"layer={candidate.layer} summary={candidate.summary} body={body}"
        )
    lines.append("")
    lines.append("## 既有节点（冲突检测范围：候选 + 最近 30 天 + disputed）")
    if cluster.targets:
        for row in cluster.targets:
            lines.append(
                f"- id={row['id']} status={row.get('status')} layer={row.get('layer')} "
                f"title={row.get('title')} summary={row.get('summary')}"
            )
    else:
        lines.append("无")
    lines.extend(
        [
            "",
            "## 输出 JSON",
            (
                '{"decision": "create|append-evidence|supersede|conflict", '
                '"target_id": null, "reason": "", "summary_patch": null, '
                '"confidence": null}'
            ),
            "规则：",
            "- decision 不是 create 时 target_id 必须是既有节点中的一个 id。",
            "- append-evidence 只在既有节点结论不变、仅补充证据时使用；"
            "summary_patch 可给新的 summary。",
            "- 结论变化用 supersede；与既有节点矛盾且未裁决用 conflict。",
        ]
    )
    return [
        {"role": "system", "content": "你是严谨的记忆巩固判定器，只输出 JSON。"},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _parse_decision(parsed: Any, cluster: _Cluster) -> _Decision | None:
    if not isinstance(parsed, dict):
        return None
    action = parsed.get("decision")
    if action not in DECISIONS:
        return None
    target_raw = parsed.get("target_id")
    target_id = target_raw if isinstance(target_raw, str) and target_raw else None
    if action != "create":
        if target_id is None or target_id not in cluster.target_ids:
            return None
    reason = str(parsed.get("reason") or "").strip()
    patch_raw = parsed.get("summary_patch")
    patch = patch_raw if isinstance(patch_raw, str) and patch_raw.strip() else None
    return _Decision(action=str(action), target_id=target_id, reason=reason, summary_patch=patch)


def _execute_decision(
    ctx: DreamContext,
    cluster: _Cluster,
    decision: _Decision,
    session_id: str,
    project: str | None,
    details: dict[str, Any],
    result: PhaseResult,
    warnings: list[str],
) -> None:
    if decision.action == "create":
        _execute_create(ctx, cluster, session_id, project, details, result, warnings)
    elif decision.action == "append-evidence":
        _execute_append(ctx, cluster, decision, session_id, project, details, result, warnings)
    elif decision.action == "supersede":
        _execute_supersede(ctx, cluster, decision, session_id, project, details, result, warnings)
    else:
        _execute_conflict(ctx, cluster, decision, session_id, project, details, result, warnings)


def _execute_create(
    ctx: DreamContext,
    cluster: _Cluster,
    session_id: str,
    project: str | None,
    details: dict[str, Any],
    result: PhaseResult,
    warnings: list[str],
) -> None:
    merged = _merge_candidates(cluster.candidates)
    if not _layer_writable(ctx, merged, project, details, result, warnings):
        return
    _ensure_write_budget(ctx)
    outcome = _write_via_service(
        ctx,
        merged,
        session_id=session_id,
        project=project,
        supersedes=None,
        warnings=warnings,
    )
    result.nodes_created += 1
    _record_writes(ctx, 1)
    _count_action(details, "create")
    if outcome.degraded:
        _count_action(details, "degraded")
    log_action(
        ctx.vault,
        ctx.run_id,
        PHASE,
        "create",
        node_id=outcome.node_id,
        details={
            "action": outcome.action,
            "candidates": len(cluster.candidates),
            "title": merged.title,
        },
    )


def _execute_append(
    ctx: DreamContext,
    cluster: _Cluster,
    decision: _Decision,
    session_id: str,
    project: str | None,
    details: dict[str, Any],
    result: PhaseResult,
    warnings: list[str],
) -> None:
    target = _read_target(ctx, decision.target_id)
    if target is None or target.status in INVALID_TARGET_STATUSES:
        _fallback_create(
            ctx,
            cluster,
            session_id,
            project,
            details,
            result,
            warnings,
            f"追加目标不可用：{decision.target_id}，已按 §4.8 写新 hypothesis",
        )
        return
    evidence = _merge_evidence(cluster.candidates)
    updates = _confirmation_updates(ctx, target, evidence, decision.summary_patch, warnings)
    _ensure_write_budget(ctx)
    updated = update_node(ctx.vault, target.id, updates, "dream")
    result.nodes_updated += 1
    _record_writes(ctx, 1)
    _count_action(details, "append-evidence")
    log_action(
        ctx.vault,
        ctx.run_id,
        PHASE,
        "append-evidence",
        node_id=target.id,
        details={
            "verify_count": updated.verify_count,
            "confidence": round(updated.confidence, 6),
            "arousal": round(updated.arousal, 6),
            "evidence_added": len(evidence),
        },
    )


def _execute_supersede(
    ctx: DreamContext,
    cluster: _Cluster,
    decision: _Decision,
    session_id: str,
    project: str | None,
    details: dict[str, Any],
    result: PhaseResult,
    warnings: list[str],
) -> None:
    target = _read_target(ctx, decision.target_id)
    if target is None or target.status in INVALID_TARGET_STATUSES:
        _fallback_create(
            ctx,
            cluster,
            session_id,
            project,
            details,
            result,
            warnings,
            f"替代目标不可用：{decision.target_id}，已按 §4.8 写新 hypothesis",
        )
        return
    merged = _merge_candidates(cluster.candidates)
    if not _layer_writable(ctx, merged, project, details, result, warnings):
        return
    _ensure_write_budget(ctx)
    outcome = _write_via_service(
        ctx,
        merged,
        session_id=session_id,
        project=project,
        supersedes=[target.id],
        warnings=warnings,
    )
    if outcome.action == "conflict":
        result.nodes_created += 1
        _record_writes(ctx, 1)
        _count_action(details, "conflict")
    else:
        result.nodes_created += 1
        result.nodes_updated += 1
        _record_writes(ctx, 2)
        _count_action(details, "supersede")
        if outcome.degraded:
            _count_action(details, "degraded")
    log_action(
        ctx.vault,
        ctx.run_id,
        PHASE,
        "supersede",
        node_id=outcome.node_id,
        details={"target_id": target.id, "action": outcome.action},
    )


def _execute_conflict(
    ctx: DreamContext,
    cluster: _Cluster,
    decision: _Decision,
    session_id: str,
    project: str | None,
    details: dict[str, Any],
    result: PhaseResult,
    warnings: list[str],
) -> None:
    target = _read_target(ctx, decision.target_id)
    if target is None:
        _fallback_create(
            ctx,
            cluster,
            session_id,
            project,
            details,
            result,
            warnings,
            f"冲突目标不存在：{decision.target_id}，已按 §4.8 写新 hypothesis",
        )
        return
    merged = _merge_candidates(cluster.candidates)
    if not _layer_writable(ctx, merged, project, details, result, warnings):
        return
    _ensure_write_budget(ctx)
    outcome = _write_via_service(
        ctx,
        merged,
        session_id=session_id,
        project=project,
        supersedes=None,
        warnings=warnings,
    )
    if outcome.node_id is None:
        raise ValidationError("冲突节点写入未返回 id", field="node_id")
    node, _ = read_node(ctx.vault, outcome.node_id)
    timestamp = now_iso()
    updated = update_node(
        ctx.vault,
        node.id,
        {
            "status": "disputed",
            "arousal": max(0.9, node.arousal),
            "updated_at": timestamp,
        },
        "dream",
    )
    append_dissonance(
        ctx.vault,
        new_id=updated.id,
        old_id=target.id,
        reason=decision.reason or "做梦巩固判定冲突",
    )
    result.nodes_created += 1
    result.nodes_updated += 1
    _record_writes(ctx, 2)
    _count_action(details, "conflict")
    log_action(
        ctx.vault,
        ctx.run_id,
        PHASE,
        "conflict",
        node_id=updated.id,
        details={"old_id": target.id, "reason": decision.reason},
    )


def _fallback_create(
    ctx: DreamContext,
    cluster: _Cluster,
    session_id: str,
    project: str | None,
    details: dict[str, Any],
    result: PhaseResult,
    warnings: list[str],
    reason: str,
) -> None:
    warnings.append(reason)
    _count_action(details, "degraded")
    log_action(
        ctx.vault,
        ctx.run_id,
        PHASE,
        "degraded",
        details={"reason": reason, "title": cluster.candidates[0].title},
    )
    _execute_create(ctx, cluster, session_id, project, details, result, warnings)


def _layer_writable(
    ctx: DreamContext,
    candidate: _Candidate,
    project: str | None,
    details: dict[str, Any],
    result: PhaseResult,
    warnings: list[str],
) -> bool:
    if candidate.layer == "L4":
        warnings.append(f"L4 候选由 REM/人格阶段产出，NREM 已跳过：{candidate.title}")
        _count_action(details, "skipped")
        details["skipped_l4"] = int(details.get("skipped_l4", 0)) + 1
        result.skipped += 1
        return False
    if candidate.layer == "L2" and not project:
        warnings.append(f"L2 候选缺少会话 project（无 L1 焦点），已跳过：{candidate.title}")
        _count_action(details, "skipped")
        result.skipped += 1
        return False
    return True


def _write_via_service(
    ctx: DreamContext,
    candidate: _Candidate,
    *,
    session_id: str,
    project: str | None,
    supersedes: list[str] | None,
    warnings: list[str],
) -> WriteOutcome:
    body = _body_with_links(candidate.body, candidate.links)
    outcome = memory_write(
        ctx.vault,
        ctx.config,
        title=candidate.title,
        summary=candidate.summary,
        body=body,
        category=candidate.category,
        layer=candidate.layer,
        session_id=session_id,
        project=project,
        tags=candidate.tags,
        evidence=candidate.evidence,
        supersedes=supersedes,
        status="hypothesis",
        confidence=candidate.confidence,
        client=None,
        command="dream",
        source=f"dream:{ctx.run_id}",
        created_by="dream",
    )
    for message in outcome.warnings:
        if any(marker in message for marker in NO_CLIENT_MARKERS):
            continue
        warnings.append(message)
    return outcome


def _merge_candidates(candidates: list[_Candidate]) -> _Candidate:
    primary = candidates[0]
    evidence = _merge_evidence(candidates)
    tags: list[str] = []
    links: list[str] = []
    for candidate in candidates:
        for tag in candidate.tags:
            if tag not in tags:
                tags.append(tag)
        for link in candidate.links:
            if link not in links:
                links.append(link)
    return _Candidate(
        title=primary.title,
        summary=primary.summary,
        body=primary.body,
        category=primary.category,
        layer=primary.layer,
        confidence=primary.confidence,
        evidence=evidence,
        tags=tags,
        links=links,
        session_id=primary.session_id,
    )


def _merge_evidence(candidates: list[_Candidate]) -> list[str]:
    evidence: list[str] = []
    for candidate in candidates:
        for item in candidate.evidence:
            if item not in evidence:
                evidence.append(item)
    return evidence


def _body_with_links(body: str, links: list[str]) -> str:
    filtered = [link for link in links if is_memory_id(link)]
    if not filtered:
        return body
    bullets = "\n".join(f"- [[{link}]]" for link in filtered)
    if any(line.strip() == "## 关联" for line in body.split("\n")):
        return f"{body.rstrip()}\n{bullets}"
    return f"{body.rstrip()}\n\n## 关联\n{bullets}"


def _confirmation_updates(
    ctx: DreamContext,
    node: MemoryNode,
    evidence: list[str],
    patch: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    timestamp = now_iso()
    merged_evidence = list(node.evidence)
    for item in evidence:
        if item not in merged_evidence:
            merged_evidence.append(item)
    alpha = _config_float(ctx.config, "dream.updates.alpha", 0.40)
    cap = _config_float(ctx.config, "dream.updates.confidence_cap", 0.99)
    arousal_lambda = _config_float(ctx.config, "dream.updates.arousal_lambda", 0.7)
    arousal_min = _config_float(ctx.config, "dream.updates.arousal_min", 0.1)
    updates: dict[str, Any] = {
        "evidence": merged_evidence,
        "verify_count": node.verify_count + 1,
        "confidence": min(cap, node.confidence + (1.0 - node.confidence) * alpha),
        "arousal": max(arousal_min, node.arousal * arousal_lambda),
        "last_verified": timestamp,
        "updated_at": timestamp,
    }
    if patch is not None:
        normalized = " ".join(patch.split())
        if len(normalized) > MAX_SUMMARY:
            warnings.append("summary_patch 超过 400 字符，已忽略")
        elif _drops_hedge(node.summary, normalized):
            warnings.append("summary_patch 删除了旧 summary 的 hedge，已拒绝补丁，只追加证据")
        else:
            updates["summary"] = normalized
    return updates


def _drops_hedge(old_summary: str, patch: str) -> bool:
    return any(hedge in old_summary and hedge not in patch for hedge in HEDGES)


def _read_target(ctx: DreamContext, target_id: str | None) -> MemoryNode | None:
    if target_id is None:
        return None
    try:
        node, _ = read_node(ctx.vault, target_id)
    except ZymorError:
        return None
    return node


def _close_stale_focus(ctx: DreamContext, session_id: str) -> dict[str, Any]:
    path = ctx.vault.focus_path(session_id)
    if not path.is_file():
        raise NotFoundError(f"焦点文件不存在：{session_id}", hint="跳过该过期焦点")
    focus = FocusFile.from_markdown(path.read_text(encoding="utf-8"))
    sections = _focus_sections(focus)
    summary = _first_paragraph(sections.get("当前目标", ""))
    if not summary:
        summary = focus.task
    summary = summary[:MAX_SUMMARY] or "会话关闭补写胶囊"
    outcome = "completed" if focus.status == "closed" else "ongoing"
    return memory_close(
        ctx.vault,
        session_id=session_id,
        outcome=outcome,
        summary=summary,
    )


def _focus_sections(focus: FocusFile) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in focus.body.split("\n"):
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = line[3:].strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


def _first_paragraph(text: str) -> str:
    paragraph = text.split("\n\n")[0]
    return " ".join(paragraph.split())


def _session_project(vault: Vault, session_id: str) -> str | None:
    path = vault.focus_path(session_id)
    if not path.is_file():
        return None
    try:
        focus = FocusFile.from_markdown(path.read_text(encoding="utf-8"))
    except (OSError, ZymorError):
        return None
    return focus.project


def _group_by_session(events: list[EventEntry]) -> dict[str, list[EventEntry]]:
    grouped: dict[str, list[EventEntry]] = {}
    for event in events:
        grouped.setdefault(event.session_id, []).append(event)
    for session_events in grouped.values():
        session_events.sort(key=lambda item: (item.ts, item.seq))
    return grouped


def _chunks(items: list[EventEntry], size: int) -> list[list[EventEntry]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _budget_totals(ctx: DreamContext) -> tuple[int, float]:
    return ctx.budget.tokens_in + ctx.budget.tokens_out, ctx.budget.usd


def _log_llm_action(
    ctx: DreamContext,
    action: str,
    details: dict[str, Any],
    tokens_before: int,
    usd_before: float,
) -> None:
    tokens = (ctx.budget.tokens_in + ctx.budget.tokens_out) - tokens_before
    usd = max(0.0, ctx.budget.usd - usd_before)
    log_action(
        ctx.vault,
        ctx.run_id,
        PHASE,
        action,
        details=details,
        tokens=max(0, tokens),
        usd=round(usd, 6),
    )


def _ensure_write_budget(ctx: DreamContext) -> None:
    if ctx.budget.can_write():
        return
    reason = ctx.budget.exhausted() or "节点变更达到上限"
    ctx.budget.stopped_reason = reason
    raise DreamBudgetExceeded(reason)


def _record_writes(ctx: DreamContext, count: int) -> None:
    for _ in range(count):
        ctx.budget.record_write()


def _count_action(details: dict[str, Any], name: str) -> None:
    actions = details["actions"]
    actions[name] = int(actions.get(name, 0)) + 1


def _stop_phase(ctx: DreamContext, error: DreamBudgetExceeded) -> str:
    reason = error.message
    ctx.budget.stopped_reason = reason
    return reason


def _error_text(error: Exception) -> str:
    if isinstance(error, ZymorError):
        return error.message
    return f"{type(error).__name__}: {error}"


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
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            result.append(str(item))
    return result


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
