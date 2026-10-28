from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from zymor.config import Config
from zymor.errors import NotFoundError, UserError, ValidationError, ZymorError
from zymor.llm import JsonChatClient
from zymor.model.event import EVENT_KINDS, append_event
from zymor.model.focus import (
    FOCUS_H2,
    SECTION_TOKEN_LIMIT,
    TASK_TYPES,
    FocusFile,
    new_focus,
)
from zymor.model.node import (
    CAPSULE_H2,
    MAX_BODY_CJK,
    MAX_BODY_TOKENS,
    MAX_SUMMARY,
    MAX_TITLE,
    REQUIRED_H2,
    TOMBSTONE_H2,
    MemoryNode,
)
from zymor.store.audit import append_audit
from zymor.store.index import Index
from zymor.store.nodes import read_node, update_node, write_node
from zymor.tokenizer import _is_cjk, build_match_query, estimate_tokens, tokenize_terms
from zymor.util.fs import atomic_write_text, sha256_hex
from zymor.util.ids import is_memory_id, is_session_id, new_memory_id
from zymor.util.time import days_ago_iso, is_iso, now_iso, parse_iso
from zymor.vault import Vault

HEDGES = ("可能", "据说", "待验证")
GATE_VALUES = ("keep", "discard")
DEDUPE_DECISIONS = ("new", "append-evidence", "supersede", "conflict")
VALID_DISSONANCE_STATES = ("open", "answered", "dismissed", "unverifiable")
VALID_DISSONANCE_TYPES = ("conflict", "persona-proposal")
CAPSULE_ORDER = ("目标", "当前状态", "背景", "决策", "结果与教训", "下一步", "关键决策与坑")
MISSING_SECTION_TEXT = "未提供。"
NO_DECISION_TEXT = "本次关闭未单列决策。"
DEFAULT_CAPSULE_TITLE = "会话胶囊"
DEFAULT_FOCUS_TASK = "未命名任务"
AUDIT_PATH = "writes.jsonl"
GATE_RUBRIC = (
    "丢弃：预训练常识；当前仓库 grep 或 git 一次调用即可获知的事实；"
    "已解决的一次性报错；寒暄与过程对话。"
    "保留：踩坑因果（动作、失败原因、避开方法至少含后两者）；"
    "架构权衡（选定项、放弃项、约束）；新概念与新 API 约束；"
    "用户偏好或纠正；跨任务经验。"
    "日期、数量、专名与 hedge（可能 / 据说 / 待验证）必须保留，禁止去含糊化。"
)
TAG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
QID_RE = re.compile(r"^###\s+(Q\d+)\s+·\s+(.+?)\s*$")
BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
MEMORY_ID_RE = re.compile(r"mem_\d{8}_[0-9a-f]{10}")


@dataclass
class WriteOutcome:
    action: str
    node_id: str | None
    path: str | None
    conflicts: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：可 JSON 序列化的写入结果。"""
        return {
            "action": self.action,
            "node_id": self.node_id,
            "path": self.path,
            "conflicts": self.conflicts,
            "duplicates": self.duplicates,
            "warnings": self.warnings,
            "degraded": self.degraded,
        }


@dataclass
class _Prepared:
    title: str
    summary: str
    body: str
    category: str
    layer: str
    session_id: str
    project: str | None
    applies_to: list[str]
    tags: list[str]
    evidence: list[str]
    supersedes: list[str]
    requested_status: str
    valid_from: str
    warnings: list[str]
    degraded: bool
    source: str | None = None
    created_by: str = "agent"
    confidence: float | None = None


@dataclass
class _WriteState:
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class _LlmResult:
    gate: str | None
    decision: str
    target_id: str | None
    reason: str
    summary_patch: str | None
    confidence: float | None


def memory_write(
    vault: Vault,
    config: Config,
    *,
    title: str,
    summary: str,
    body: str,
    category: str,
    layer: str,
    session_id: str,
    project: str | None = None,
    applies_to: list[str] | None = None,
    tags: list[str] | None = None,
    evidence: list[str] | None = None,
    supersedes: list[str] | None = None,
    status: str = "hypothesis",
    valid_from: str | None = None,
    client: JsonChatClient | None = None,
    command: str = "write",
    source: str | None = None,
    created_by: str = "agent",
    confidence: float | None = None,
) -> WriteOutcome:
    """入参：vault、配置与 §4.2 写入参数。出参：写入结果。执行门控、去重四选一、校验与原子落盘。"""
    started = time.monotonic()
    state = _WriteState()
    try:
        outcome = _run_write(
            vault,
            config,
            state,
            title=title,
            summary=summary,
            body=body,
            category=category,
            layer=layer,
            session_id=session_id,
            project=project,
            applies_to=applies_to,
            tags=tags,
            evidence=evidence,
            supersedes=supersedes,
            status=status,
            valid_from=valid_from,
            client=client,
            command=command,
            source=source,
            created_by=created_by,
            confidence=confidence,
        )
    except ZymorError as error:
        _audit_write_attempt(
            vault,
            action="error",
            node_id=state.extra.get("node_id"),
            llm_calls=state.llm_calls,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            duration_ms=(time.monotonic() - started) * 1000.0,
            error=error.message,
            degraded=bool(state.extra.get("degraded")),
            extra={"warnings": state.extra.get("warnings"), "command": command},
        )
        raise
    except OSError as error:
        _audit_write_attempt(
            vault,
            action="error",
            node_id=state.extra.get("node_id"),
            llm_calls=state.llm_calls,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            duration_ms=(time.monotonic() - started) * 1000.0,
            error=str(error),
            degraded=bool(state.extra.get("degraded")),
        )
        raise UserError(f"写入失败：{error}", hint="检查磁盘空间与文件权限后重试") from error
    except Exception as error:
        _audit_write_attempt(
            vault,
            action="error",
            node_id=state.extra.get("node_id"),
            llm_calls=state.llm_calls,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            duration_ms=(time.monotonic() - started) * 1000.0,
            error=f"{type(error).__name__}: {error}",
            degraded=bool(state.extra.get("degraded")),
        )
        raise UserError(
            f"写入失败：{type(error).__name__}: {error}", hint="该内容未落盘，修正后重试"
        ) from error
    _audit_write_attempt(
        vault,
        action=outcome.action,
        node_id=outcome.node_id,
        llm_calls=state.llm_calls,
        input_tokens=state.input_tokens,
        output_tokens=state.output_tokens,
        duration_ms=(time.monotonic() - started) * 1000.0,
        error=None,
        degraded=outcome.degraded,
        extra={**state.extra, "warnings": outcome.warnings},
    )
    return outcome


def _run_write(
    vault: Vault,
    config: Config,
    state: _WriteState,
    *,
    title: str,
    summary: str,
    body: str,
    category: str,
    layer: str,
    session_id: str,
    project: str | None,
    applies_to: list[str] | None,
    tags: list[str] | None,
    evidence: list[str] | None,
    supersedes: list[str] | None,
    status: str,
    valid_from: str | None,
    client: JsonChatClient | None,
    command: str,
    source: str | None = None,
    created_by: str = "agent",
    confidence: float | None = None,
) -> WriteOutcome:
    warnings: list[str] = []
    prepared = _prepare_write(
        vault,
        config,
        title=title,
        summary=summary,
        body=body,
        category=category,
        layer=layer,
        session_id=session_id,
        project=project,
        applies_to=applies_to,
        tags=tags,
        evidence=evidence,
        supersedes=supersedes,
        status=status,
        valid_from=valid_from,
        warnings=warnings,
        source=source,
        created_by=created_by,
        confidence=confidence,
    )
    if len(prepared.supersedes) > 1:
        warnings.append("supersedes 只执行首个目标，其余仅记录在 frontmatter")
    targets = _load_supersede_targets(vault, prepared.supersedes)
    candidates = _gather_candidates(vault, config, prepared, warnings)
    candidate_ids = [str(row["id"]) for row in candidates]
    review = bool(config.get("write.salience_review", False))
    degraded = prepared.degraded
    llm_failed = False
    decision: _LlmResult | None = None
    if review or candidates:
        if client is None:
            if review:
                degraded = True
                warnings.append(
                    "salience_review 已开启但未提供 LLM 客户端，已跳过门控并按 hypothesis 写入"
                )
            else:
                warnings.append("未提供 LLM 客户端，已跳过去重合并")
        else:
            decision = _call_review(client, prepared, candidates, review, state)
            if decision is None:
                llm_failed = True
                degraded = True
                warnings.append(
                    "门控/去重 LLM 调用失败或输出不合法，按 §4.8 写 hypothesis 并跳过合并"
                )
    if review and decision is not None and decision.gate == "discard":
        state.extra["gate"] = "discard"
        return WriteOutcome(
            action="rejected-gate",
            node_id=None,
            path=None,
            conflicts=[],
            duplicates=candidate_ids,
            warnings=warnings,
            degraded=False,
        )
    supersede_target = targets[0] if targets else None
    force_hypothesis = degraded
    action = "new"
    target: MemoryNode | None = None
    conflict_reason = "写入路径判定冲突"
    if decision is not None and candidates:
        if decision.decision == "conflict":
            action = "conflict"
            conflict_reason = decision.reason or conflict_reason
            if supersede_target is not None:
                warnings.append("LLM 判定 conflict，调用方传入的 supersedes 未执行")
            target = _read_candidate(vault, decision.target_id)
            if target is None:
                action = "new"
                force_hypothesis = True
                degraded = True
                warnings.append(f"冲突目标不存在：{decision.target_id}，已按 §4.8 写新 hypothesis")
        elif supersede_target is not None:
            action = "supersede"
            target = supersede_target
            if decision.decision != "supersede":
                warnings.append("调用方传入 supersedes，优先于 LLM 的判定")
        elif decision.decision == "new":
            action = "new"
        elif decision.decision == "append-evidence":
            action = "append-evidence"
            target = _read_candidate(vault, decision.target_id)
            if target is None or target.status in {"deprecated", "disputed"}:
                action = "new"
                force_hypothesis = True
                degraded = True
                warnings.append(f"追加目标不可用：{decision.target_id}，已按 §4.8 写新 hypothesis")
        else:
            action = "supersede"
            target = _read_candidate(vault, decision.target_id)
            if target is None or target.status in {"deprecated", "disputed"}:
                action = "new"
                force_hypothesis = True
                degraded = True
                warnings.append(f"替代目标不可用：{decision.target_id}，已按 §4.8 写新 hypothesis")
    elif llm_failed:
        if supersede_target is not None:
            action = "supersede"
            target = supersede_target
        else:
            action = "new"
    elif supersede_target is not None:
        action = "supersede"
        target = supersede_target
    if action == "conflict" and target is not None:
        node = _build_node(
            vault,
            prepared,
            status="disputed",
            confidence=0.4,
            verify_count=0,
            body=prepared.body,
            supersedes=[],
        )
        state.extra["node_id"] = node.id
        state.extra["old_id"] = target.id
        write_node(vault, node, command)
        path = vault.node_path(node.category, node.id)
        warning = _enqueue_dissonance(vault, node.id, target.id, conflict_reason)
        if warning is not None:
            warnings.append(warning)
        warnings.append("已判定冲突：新节点 disputed，旧节点未改动，冲突对已写入晨间追问队列")
        _index_pending(vault, warnings)
        return WriteOutcome(
            action="conflict",
            node_id=node.id,
            path=vault.relative(path),
            conflicts=[target.id],
            duplicates=candidate_ids,
            warnings=warnings,
            degraded=False,
        )
    if action == "append-evidence" and target is not None:
        updated = _apply_append_evidence(
            vault, prepared, decision, target, command, warnings, state
        )
        _index_pending(vault, warnings)
        updated_path = vault.find_node_path(updated.id)
        return WriteOutcome(
            action="append-evidence",
            node_id=updated.id,
            path=vault.relative(updated_path) if updated_path is not None else None,
            conflicts=[],
            duplicates=candidate_ids,
            warnings=warnings,
            degraded=degraded,
        )
    final_status, confidence, verify_count = _resolve_status(prepared, force_hypothesis)
    if action == "supersede" and target is not None:
        if target.superseded_by is not None:
            node = _build_node(
                vault,
                prepared,
                status="disputed",
                confidence=0.4,
                verify_count=0,
                body=prepared.body,
                supersedes=prepared.supersedes,
            )
            state.extra["node_id"] = node.id
            state.extra["old_id"] = target.id
            write_node(vault, node, command)
            path = vault.node_path(node.category, node.id)
            warnings.append(
                f"旧节点 {target.id} 已有 superseded_by={target.superseded_by}；"
                "新节点改为 disputed 并写入失调队列"
            )
            warning = _enqueue_dissonance(vault, node.id, target.id, "同一旧节点被重复替代")
            if warning is not None:
                warnings.append(warning)
            _index_pending(vault, warnings)
            return WriteOutcome(
                action="conflict",
                node_id=node.id,
                path=vault.relative(path),
                conflicts=[target.id],
                duplicates=candidate_ids,
                warnings=warnings,
                degraded=False,
            )
        node = _build_node(
            vault,
            prepared,
            status=final_status,
            confidence=confidence,
            verify_count=verify_count,
            body=prepared.body,
            supersedes=prepared.supersedes,
        )
        state.extra["node_id"] = node.id
        write_node(vault, node, command)
        tombstone = _deprecate_node(vault, target, node, command)
        state.extra["tombstone"] = tombstone
        _index_pending(vault, warnings)
        path = vault.node_path(node.category, node.id)
        return WriteOutcome(
            action="degraded" if degraded else "supersede",
            node_id=node.id,
            path=vault.relative(path),
            conflicts=[],
            duplicates=candidate_ids,
            warnings=warnings,
            degraded=degraded,
        )
    node = _build_node(
        vault,
        prepared,
        status=final_status,
        confidence=confidence,
        verify_count=verify_count,
        body=prepared.body,
        supersedes=[],
    )
    state.extra["node_id"] = node.id
    write_node(vault, node, command)
    _index_pending(vault, warnings)
    path = vault.node_path(node.category, node.id)
    return WriteOutcome(
        action="degraded" if degraded else "new",
        node_id=node.id,
        path=vault.relative(path),
        conflicts=[],
        duplicates=candidate_ids,
        warnings=warnings,
        degraded=degraded,
    )


def _prepare_write(
    vault: Vault,
    config: Config,
    *,
    title: str,
    summary: str,
    body: str,
    category: str,
    layer: str,
    session_id: str,
    project: str | None,
    applies_to: list[str] | None,
    tags: list[str] | None,
    evidence: list[str] | None,
    supersedes: list[str] | None,
    status: str,
    valid_from: str | None,
    warnings: list[str],
    source: str | None = None,
    created_by: str = "agent",
    confidence: float | None = None,
) -> _Prepared:
    if not is_session_id(session_id):
        raise ValidationError(
            "非法会话标识",
            field="session_id",
            hint="session_id 必填：字母数字开头，允许 . _ -，长度不超过 128",
        )
    if not isinstance(title, str) or not title.strip():
        raise ValidationError("title 不能为空", field="title")
    title = title.strip()
    if "\n" in title or "\r" in title:
        raise ValidationError("title 必须是单行", field="title")
    if len(title) > MAX_TITLE:
        raise ValidationError(f"title 超过 {MAX_TITLE} 字符", field="title")
    if not isinstance(summary, str) or not summary.strip():
        raise ValidationError("summary 不能为空", field="summary")
    summary = summary.strip()
    if "\n" in summary or "\r" in summary:
        raise ValidationError("summary 必须是单段", field="summary")
    if len(summary) > MAX_SUMMARY:
        raise ValidationError(f"summary 超过 {MAX_SUMMARY} 字符", field="summary")
    if not isinstance(body, str) or not body.strip():
        raise ValidationError("body 不能为空", field="body")
    if category not in REQUIRED_H2:
        raise ValidationError(
            f"未知 category: {category}",
            field="category",
            hint=f"可选：{', '.join(REQUIRED_H2)}",
        )
    if layer not in {"L2", "L3", "L4"}:
        raise ValidationError(f"未知 layer: {layer}", field="layer", hint="可选：L2、L3、L4")
    if status not in {"hypothesis", "verified"}:
        raise ValidationError(
            "status 只支持 hypothesis 或 verified", field="status", hint="缺省为 hypothesis"
        )
    if layer == "L4" and category != "axioms":
        raise ValidationError("layer: L4 必须是 category: axioms", field="category")
    if layer == "L2" and category != "episodic":
        raise ValidationError("layer: L2 必须是 category: episodic", field="category")
    tags = _string_list(tags, "tags")
    applies_to = _string_list(applies_to, "applies_to")
    evidence = _string_list(evidence, "evidence")
    supersedes = _string_list(supersedes, "supersedes")
    for item in tags:
        if not TAG_RE.match(item):
            raise ValidationError(f"tag 必须 kebab-case: {item}", field="tags")
    for item in supersedes:
        if not is_memory_id(item):
            raise ValidationError(f"非法节点 id: {item}", field="supersedes")
    if layer == "L4" and not applies_to:
        raise ValidationError(
            "layer: L4 必须提供非空 applies_to",
            field="applies_to",
            hint="例如 [debugging, research]",
        )
    if project is None and layer == "L2":
        focus = _load_focus(vault, session_id)
        if focus is None:
            raise ValidationError(
                "layer: L2 必须提供 project",
                field="project",
                hint=f"先调用 memory_focus(session_id={session_id}) 或显式传入 project",
            )
        project = focus.project
    if project is not None and not PROJECT_RE.match(project):
        raise ValidationError(f"project 必须是 slug: {project}", field="project")
    if valid_from is not None and not is_iso(valid_from):
        raise ValidationError("valid_from 必须是带时区的 ISO8601 文本", field="valid_from")
    created = now_iso()
    required = list(REQUIRED_H2[category])
    if layer == "L2":
        required.extend(CAPSULE_H2)
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.strip("\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip().startswith("# "):
        lines = lines[1:]
    if any(line.strip().startswith("# ") for line in lines):
        raise ValidationError("body 不能包含 H1；服务端按 title 生成 H1", field="body")
    preamble, pairs = _split_body("\n".join(lines))
    names = {name for name, _ in pairs}
    missing = [name for name in required if name not in names]
    degraded = False
    if missing:
        warnings.append(f"body 缺少必需小节：{', '.join(missing)}；已按 §4.8 补齐并降级")
        pairs = _fill_required_sections(preamble, pairs, required)
        preamble = ""
        degraded = True
    body_text = _compose_body(title, pairs, preamble)
    max_input = _config_int(config, "write.max_input_tokens", 2000)
    overhead = estimate_tokens(title) + estimate_tokens(summary) + 8
    body_budget = max(20, min(MAX_BODY_TOKENS, max_input - overhead))
    if _body_exceeds(body_text, body_budget) or _body_exceeds(body_text, MAX_BODY_TOKENS):
        if preamble:
            pairs = _fold_preamble(preamble, pairs, required)
            preamble = ""
        pairs, _ = _fit_pairs(title, pairs, body_budget, MAX_BODY_CJK)
        body_text = _compose_body(title, pairs)
        if _body_exceeds(body_text, MAX_BODY_TOKENS, MAX_BODY_CJK):
            pairs = [(name, MISSING_SECTION_TEXT) for name, _ in pairs]
            body_text = _compose_body(title, pairs)
        degraded = True
        warnings.append("正文超过写入 token 上限，已截断并降级为 hypothesis")
    requested_verified = status == "verified"
    if requested_verified and not evidence:
        warnings.append("status: verified 需要非空 evidence；已降为 hypothesis")
    if requested_verified and degraded:
        warnings.append("写入发生降级，status 已降为 hypothesis")
    if (
        source is not None
        and re.match(r"^(session:.+|user|dream:.+|import:.+|research:.+)$", source) is None
    ):
        raise ValidationError(
            "非法 source",
            field="source",
            hint="source 必须是 session:/user/dream:/import:/research: 形式",
        )
    if created_by not in {"agent", "user", "dream"}:
        raise ValidationError(
            f"非法 created_by: {created_by}",
            field="created_by",
            hint="created_by 可选 agent / user / dream",
        )
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError) as error:
            raise ValidationError("confidence 必须是数字", field="confidence") from error
        if not 0.0 <= confidence <= 1.0:
            raise ValidationError("confidence 必须在 [0,1]", field="confidence")
    return _Prepared(
        title=title,
        summary=summary,
        body=body_text,
        category=category,
        layer=layer,
        session_id=session_id,
        project=project,
        applies_to=applies_to,
        tags=tags,
        evidence=evidence,
        supersedes=supersedes,
        requested_status=status,
        valid_from=valid_from or created,
        warnings=warnings,
        degraded=degraded,
        source=source,
        created_by=created_by,
        confidence=confidence,
    )


def _resolve_status(prepared: _Prepared, force_hypothesis: bool) -> tuple[str, float, int]:
    if force_hypothesis:
        return "hypothesis", prepared.confidence if prepared.confidence is not None else 0.4, 0
    if prepared.requested_status == "verified":
        if not prepared.evidence:
            return "hypothesis", prepared.confidence if prepared.confidence is not None else 0.4, 0
        return "verified", max(0.7, prepared.confidence or 0.7), 1
    return "hypothesis", prepared.confidence if prepared.confidence is not None else 0.4, 0


def _build_node(
    vault: Vault,
    prepared: _Prepared,
    *,
    status: str,
    confidence: float,
    verify_count: int,
    body: str,
    supersedes: list[str],
) -> MemoryNode:
    timestamp = now_iso()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "id": _fresh_node_id(vault),
        "title": prepared.title,
        "summary": prepared.summary,
        "category": prepared.category,
        "layer": prepared.layer,
        "status": status,
        "confidence": confidence,
        "arousal": 0.9,
        "hit_count": 0,
        "verify_count": verify_count,
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_verified": timestamp,
        "valid_from": prepared.valid_from,
        "source": prepared.source or f"session:{prepared.session_id}",
        "created_by": prepared.created_by,
        "tags": prepared.tags,
        "evidence": prepared.evidence,
        "applies_to": prepared.applies_to,
        "project": prepared.project,
        "supersedes": supersedes,
        "body": body,
    }
    return _validate_node(payload)


def _read_candidate(vault: Vault, node_id: str | None) -> MemoryNode | None:
    if node_id is None:
        return None
    try:
        node, _ = read_node(vault, node_id)
    except NotFoundError:
        return None
    return node


def _load_supersede_targets(vault: Vault, ids: list[str]) -> list[MemoryNode]:
    targets: list[MemoryNode] = []
    for node_id in ids:
        node, _ = read_node(vault, node_id)
        if node.superseded_by is not None:
            targets.append(node)
            continue
        if node.status in {"deprecated", "disputed"}:
            raise ValidationError(
                f"supersedes 目标 {node_id} 状态为 {node.status}，不能作为替代目标",
                field="supersedes",
                hint="deprecated / disputed 节点不可被追加或替代",
            )
        targets.append(node)
    return targets


def _gather_candidates(
    vault: Vault, config: Config, prepared: _Prepared, warnings: list[str]
) -> list[dict[str, Any]]:
    index = Index(vault.root)
    topk = max(1, _config_int(config, "write.dedup_topk", 8))
    window_hours = _config_float(config, "write.dedup_window_hours", 720.0)
    found: dict[str, dict[str, Any]] = {}
    queries: list[str] = []
    if prepared.title.strip():
        query = build_match_query(prepared.title)
        if query:
            queries.append(query)
    if prepared.tags:
        query = _or_match_query(" ".join(prepared.tags))
        if query:
            queries.append(query)
    semantic = " ".join([prepared.title, prepared.summary, *prepared.tags]).strip()
    query = _or_match_query(semantic)
    if query:
        queries.append(query)
    try:
        for query_text in dict.fromkeys(queries):
            for row in index.candidate_rows(query_text, limit=max(topk * 10, 50)):
                found.setdefault(str(row["id"]), dict(row))
        if window_hours > 0:
            since = days_ago_iso(window_hours / 24.0)
            for row in index.recent_rows(since, limit=max(topk * 10, 50)):
                found.setdefault(str(row["id"]), dict(row))
    except ZymorError as error:
        warnings.append(f"去重候选检索失败，已跳过合并：{error.message}")
        return []
    title_key = prepared.title.strip()
    tag_set = set(prepared.tags)

    def rank_key(row: dict[str, Any]) -> tuple[int, int, float, float]:
        title_equal = str(row.get("title") or "").strip() == title_key
        tag_overlap = bool(tag_set & set(row.get("tags") or []))
        rank = row.get("rank")
        updated = str(row.get("updated_at") or "")
        try:
            stamp = -parse_iso(updated).timestamp()
        except ValueError:
            stamp = 0.0
        return (
            0 if title_equal else 1,
            0 if tag_overlap else 1,
            float(rank) if rank is not None else 1e9,
            stamp,
        )

    ranked = sorted(found.values(), key=rank_key)
    return ranked[:topk]


def _or_match_query(text: str) -> str:
    unique: list[str] = []
    seen: set[str] = set()
    for term in tokenize_terms(text):
        if term in seen:
            continue
        seen.add(term)
        unique.append(term)
    selected = unique[:12]
    return " OR ".join(f'"{term}"' for term in selected)


def _call_review(
    client: JsonChatClient,
    prepared: _Prepared,
    candidates: list[dict[str, Any]],
    review: bool,
    state: _WriteState,
) -> _LlmResult | None:
    messages = _review_messages(prepared, candidates, review)
    state.llm_calls += 1
    try:
        parsed, response = client.complete_json(messages, caller="dedupe")
    except Exception:
        return None
    if response is not None:
        state.input_tokens += int(getattr(response, "tokens_in", 0) or 0)
        state.output_tokens += int(getattr(response, "tokens_out", 0) or 0)
    return _parse_llm_result(parsed, candidates, review)


def _review_messages(
    prepared: _Prepared, candidates: list[dict[str, Any]], review: bool
) -> list[dict[str, str]]:
    lines = [
        "你是 Zymor 记忆写入的复核与去重判定器。只输出一个 JSON 对象，不要代码块与解释。",
        "",
        "## 写作门控清单",
        GATE_RUBRIC,
        "",
        "## 本次写入",
        f"- title: {prepared.title}",
        f"- summary: {prepared.summary}",
        f"- category: {prepared.category} / layer: {prepared.layer}",
        f"- tags: {', '.join(prepared.tags) or '无'}",
        "",
        "## 候选节点",
    ]
    if candidates:
        for position, row in enumerate(candidates, start=1):
            tags = ",".join(str(item) for item in (row.get("tags") or [])) or "无"
            lines.append(
                f"{position}. id={row['id']} status={row.get('status')} layer={row.get('layer')} "
                f"title={row.get('title')} summary={row.get('summary')} tags={tags}"
            )
    else:
        lines.append("无")
    lines.extend(["", "## 本次正文", prepared.body, "", "## 输出 JSON 字段"])
    if review:
        lines.append('- gate: "keep" 或 "discard"（按门控清单判定本次写入是否值得保存）')
    if candidates:
        lines.extend(
            [
                '- decision: "new" / "append-evidence" / "supersede" / "conflict"',
                "- target_id: decision 不是 new 时必填，且必须是候选节点中的一个 id",
                "- reason: 一句话理由",
                "- summary_patch: 仅 append-evidence 时可给新的 summary，否则 null",
                "- confidence: 0 到 1 的数字或 null",
            ]
        )
    else:
        lines.append('- decision: "new"')
    return [
        {"role": "system", "content": "你是严谨的记忆写入去重判定器，只输出 JSON。"},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _parse_llm_result(
    parsed: Any, candidates: list[dict[str, Any]], review: bool
) -> _LlmResult | None:
    if not isinstance(parsed, dict):
        return None
    gate: str | None = None
    if review:
        raw_gate = parsed.get("gate")
        if raw_gate not in GATE_VALUES:
            return None
        gate = str(raw_gate)
    candidate_ids = {str(row["id"]) for row in candidates}
    raw_target = parsed.get("target_id")
    target_id = raw_target if isinstance(raw_target, str) and raw_target else None
    raw_decision = parsed.get("decision")
    decision = raw_decision if isinstance(raw_decision, str) else None
    if candidates:
        if decision not in DEDUPE_DECISIONS:
            return None
        if decision != "new" and target_id not in candidate_ids:
            return None
    else:
        decision = "new"
    raw_patch = parsed.get("summary_patch")
    summary_patch = raw_patch if isinstance(raw_patch, str) and raw_patch.strip() else None
    raw_confidence = parsed.get("confidence")
    confidence: float | None = None
    if isinstance(raw_confidence, (int, float)) and not isinstance(raw_confidence, bool):
        confidence = float(raw_confidence)
    reason = str(parsed.get("reason") or "").strip()
    return _LlmResult(
        gate=gate,
        decision=str(decision),
        target_id=target_id,
        reason=reason,
        summary_patch=summary_patch,
        confidence=confidence,
    )


def _apply_append_evidence(
    vault: Vault,
    prepared: _Prepared,
    decision: _LlmResult | None,
    target: MemoryNode,
    command: str,
    warnings: list[str],
    state: _WriteState,
) -> MemoryNode:
    timestamp = now_iso()
    evidence = list(target.evidence)
    added: list[str] = []
    for item in prepared.evidence:
        if item not in evidence:
            evidence.append(item)
            added.append(item)
    applied_summary = target.summary
    if decision is not None and decision.summary_patch is not None:
        patch = decision.summary_patch.strip()
        if len(patch) > MAX_SUMMARY:
            warnings.append("summary_patch 超过 400 字符，已忽略")
        elif _drops_hedge(target.summary, patch):
            warnings.append("summary_patch 删除了旧 summary 的 hedge，已拒绝补丁，只追加证据")
        else:
            applied_summary = patch
    confidence = target.confidence
    if decision is not None and decision.confidence is not None:
        confidence = min(1.0, max(0.0, decision.confidence))
    updates: dict[str, Any] = {
        "evidence": evidence,
        "summary": applied_summary,
        "confidence": confidence,
        "last_verified": timestamp,
        "updated_at": timestamp,
    }
    updated = update_node(vault, target.id, updates, command)
    if not added and applied_summary == target.summary and confidence == target.confidence:
        warnings.append("append-evidence 没有新增证据或摘要变化，仅刷新 last_verified")
    state.extra["summary_after"] = updated.summary
    state.extra["old_summary_hash"] = sha256_hex(target.summary)
    state.extra["evidence_added"] = added
    state.extra["confidence_before"] = target.confidence
    state.extra["confidence_after"] = updated.confidence
    return updated


def _drops_hedge(old_summary: str, patch: str) -> bool:
    return any(hedge in old_summary and hedge not in patch for hedge in HEDGES)


def _deprecate_node(vault: Vault, old: MemoryNode, new: MemoryNode, command: str) -> str:
    timestamp = now_iso()
    tombstone = _tombstone_block(old, new, timestamp)
    body = f"{old.body.rstrip()}\n\n{tombstone}\n"
    evidence = list(old.evidence)
    pointer = f"{new.id} ({timestamp})"
    if pointer not in evidence:
        evidence.append(pointer)
    updates: dict[str, Any] = {
        "status": "deprecated",
        "valid_to": timestamp,
        "superseded_by": new.id,
        "evidence": evidence,
        "body": body,
        "updated_at": timestamp,
    }
    update_node(vault, old.id, updates, command)
    return tombstone


def _tombstone_block(old: MemoryNode, new: MemoryNode, timestamp: str) -> str:
    heading = f"## {TOMBSTONE_H2}\n\n- 原结论：\n- 纠正：\n- 证据：\n- 替代：\n"
    token_budget = MAX_BODY_TOKENS - estimate_tokens(old.body) - estimate_tokens(heading) - 4
    cjk_budget = MAX_BODY_CJK - _cjk_count(old.body) - _cjk_count(heading) - 4
    token_each = max(4, token_budget // 2)
    cjk_each = max(4, cjk_budget // 2)
    original, _ = _clip_text(old.summary, token_each, cjk_each)
    corrected, _ = _clip_text(new.summary, token_each, cjk_each)
    return (
        f"## {TOMBSTONE_H2}\n\n"
        f"- 原结论：{original}\n"
        f"- 纠正：{corrected}\n"
        f"- 证据：{new.id}；{timestamp}\n"
        f"- 替代：[[{new.id}]]"
    )


def _enqueue_dissonance(vault: Vault, new_id: str, old_id: str, reason: str) -> str | None:
    try:
        append_dissonance(vault, new_id=new_id, old_id=old_id, reason=reason)
    except ZymorError as error:
        return f"失调队列写入失败：{error.message}"
    return None


def memory_focus(
    vault: Vault,
    *,
    session_id: str,
    mode: str = "patch",
    sections: dict[str, str] | None = None,
    project: str | None = None,
    task: str | None = None,
    task_type: str | None = None,
) -> dict[str, Any]:
    """入参：vault、会话与五段更新。出参：焦点更新结果。创建或更新 L1 焦点文件，不调用 LLM。"""
    if not is_session_id(session_id):
        raise ValidationError("非法会话标识", field="session_id")
    if mode not in {"patch", "replace"}:
        raise ValidationError("mode 只支持 patch 或 replace", field="mode")
    if task_type is not None and task_type not in TASK_TYPES:
        raise ValidationError(
            f"未知 task_type: {task_type}", field="task_type", hint=f"可选：{', '.join(TASK_TYPES)}"
        )
    provided = _normalize_focus_sections(sections)
    if mode == "replace":
        missing = [name for name in FOCUS_H2 if name not in provided]
        if missing:
            raise ValidationError(
                f"mode=replace 时必须提供全部五段，缺少：{', '.join(missing)}",
                field="sections",
            )
    warnings: list[str] = []
    path = vault.focus_path(session_id)
    changed = True
    if path.is_file():
        focus = _load_focus(vault, session_id)
        if focus is None:
            raise NotFoundError(
                f"焦点文件不存在：{session_id}", hint="先调用 memory_focus 创建焦点"
            )
        if focus.status == "closed":
            raise UserError(
                f"焦点文件已关闭：{session_id}",
                hint="新任务请使用新的 session_id；恢复既有任务用 memory_close 带 capsule_id",
            )
        created = False
        existing = _focus_sections(focus)
        merged = dict(existing)
        if mode == "replace":
            merged = {name: provided[name].strip() for name in FOCUS_H2}
        else:
            for name, value in provided.items():
                merged[name] = value.strip()
        changed = any(merged.get(name, "") != existing.get(name, "") for name in FOCUS_H2)
        for field_name, field_value in (
            ("project", project),
            ("task", task),
            ("task_type", task_type),
        ):
            if (
                field_value is not None
                and str(focus.to_frontmatter().get(field_name)) != field_value
            ):
                warnings.append(f"{field_name} 只在创建焦点时写入，本次已忽略")
    else:
        created = True
        merged = {name: "" for name in FOCUS_H2}
        merged.update({name: value.strip() for name, value in provided.items()})
        focus = new_focus(
            session_id,
            project or vault.info.name,
            task or DEFAULT_FOCUS_TASK,
            task_type or "other",
        )
    body = _compose_focus_body(merged)
    _check_focus_limits(body, focus.token_budget)
    if not changed:
        return _focus_result(vault, focus, path, merged, body, created, changed, mode, warnings)
    payload = focus.to_frontmatter()
    payload["updated_at"] = now_iso()
    payload["body"] = body
    updated = _validate_focus(payload)
    try:
        with vault.writing("focus"):
            atomic_write_text(path, updated.to_markdown())
    except OSError as error:
        raise UserError(f"焦点写入失败：{error}", hint="检查磁盘空间与文件权限后重试") from error
    _audit_write_attempt(
        vault,
        path="focus",
        action="created" if created else mode,
        node_id=None,
        extra={"session_id": session_id},
    )
    return _focus_result(vault, updated, path, merged, body, created, changed, mode, warnings)


def _focus_result(
    vault: Vault,
    focus: FocusFile,
    path: Path,
    sections: dict[str, str],
    body: str,
    created: bool,
    changed: bool,
    mode: str,
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "session_id": focus.session_id,
        "path": vault.relative(path),
        "status": focus.status,
        "project": focus.project,
        "task": focus.task,
        "task_type": focus.task_type,
        "created": created,
        "changed": changed,
        "mode": mode,
        "sections": {name: sections.get(name, "") for name in FOCUS_H2},
        "tokens_estimate": estimate_tokens(body),
        "body": body,
        "warnings": warnings,
    }


def _normalize_focus_sections(sections: dict[str, str] | None) -> dict[str, str]:
    if sections is None:
        return {}
    if not isinstance(sections, dict):
        raise ValidationError("sections 必须是五段名称到文本的映射", field="sections")
    provided: dict[str, str] = {}
    for name, value in sections.items():
        if name not in FOCUS_H2:
            raise ValidationError(
                f"未知焦点小节：{name}",
                field="sections",
                hint=f"可选：{', '.join(FOCUS_H2)}",
            )
        if value is None:
            provided[name] = ""
        elif isinstance(value, str):
            provided[name] = value
        else:
            raise ValidationError(f"焦点小节 {name} 必须是字符串", field="sections")
    return provided


def _compose_focus_body(sections: dict[str, str]) -> str:
    lines = ["# 焦点", ""]
    for name in FOCUS_H2:
        lines.extend([f"## {name}", sections.get(name, "").strip(), ""])
    return "\n".join(lines).rstrip("\n") + "\n"


def _check_focus_limits(body: str, token_budget: int) -> None:
    _, pairs = _split_body(body)
    for name, content in pairs:
        if estimate_tokens(content) > SECTION_TOKEN_LIMIT:
            raise ValidationError(
                f"焦点小节 `{name}` 超过 {SECTION_TOKEN_LIMIT} tokens，本次未写入",
                field="sections",
            )
    total = estimate_tokens(body)
    if total > token_budget:
        raise ValidationError(
            f"焦点全文 {total} tokens 超过预算 {token_budget}，本次未写入",
            field="sections",
            hint="精简五段后重试；服务端禁止写半截焦点",
        )


def _validate_focus(payload: dict[str, Any]) -> FocusFile:
    try:
        return FocusFile.model_validate(payload)
    except PydanticValidationError as error:
        first = error.errors()[0]
        raise ValidationError(f"焦点校验失败：{first['msg']}", field="focus") from None


def _load_focus(vault: Vault, session_id: str) -> FocusFile | None:
    path = vault.focus_path(session_id)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise UserError(f"焦点文件不可读：{error}", hint="检查文件权限") from error
    return FocusFile.from_markdown(text)


def memory_close(
    vault: Vault,
    *,
    session_id: str,
    outcome: str,
    summary: str,
    capsule_id: str | None = None,
    decisions: list[str] | None = None,
) -> dict[str, Any]:
    """入参：vault、会话、结果与摘要。出参：胶囊结果。关闭焦点并创建或追写 L2 胶囊。"""
    started = time.monotonic()
    if not is_session_id(session_id):
        raise ValidationError("非法会话标识", field="session_id")
    if outcome not in {"ongoing", "completed"}:
        raise ValidationError("outcome 只支持 ongoing 或 completed", field="outcome")
    if not isinstance(summary, str) or not summary.strip():
        raise ValidationError("summary 不能为空", field="summary")
    summary = summary.strip()
    if len(summary) > MAX_SUMMARY:
        raise ValidationError(f"summary 超过 {MAX_SUMMARY} 字符", field="summary")
    decision_list = _string_list(decisions, "decisions")
    focus = _load_focus(vault, session_id)
    if focus is None:
        raise NotFoundError(
            f"焦点文件不存在：{session_id}",
            hint="先调用 memory_focus 创建焦点后再关闭",
        )
    warnings: list[str] = []
    explicit: MemoryNode | None = None
    if capsule_id is not None:
        if not is_memory_id(capsule_id):
            raise ValidationError(f"非法节点 id: {capsule_id}", field="capsule_id")
        explicit, _ = read_node(vault, capsule_id)
        if explicit.layer != "L2" or explicit.category != "episodic":
            raise ValidationError("capsule_id 必须是 L2 episodic 胶囊", field="capsule_id")
        if explicit.status != "hypothesis":
            raise ValidationError(
                f"只能恢复进行中（hypothesis）胶囊：{capsule_id} 当前为 {explicit.status}",
                field="capsule_id",
                hint="需要更新已完成胶囊时另写节点并用 supersedes",
            )
    existing = [] if explicit is not None else _find_capsules(vault, focus)
    if explicit is not None:
        action, node, extra = _append_capsule(
            vault, focus, explicit, summary, decision_list, outcome
        )
    elif existing:
        latest = existing[0]
        for other in existing[1:]:
            warnings.append(f"同 project+task 另有胶囊 {other.id}，本次只更新 {latest.id}")
        if latest.status == "hypothesis":
            action, node, extra = _append_capsule(
                vault, focus, latest, summary, decision_list, outcome
            )
        elif latest.status == "verified":
            action, node, extra = _create_capsule(
                vault,
                focus,
                summary,
                decision_list,
                outcome,
                warnings,
                supersede=latest,
            )
        else:
            warnings.append(f"既有胶囊 {latest.id} 状态为 {latest.status}，已另建新胶囊")
            action, node, extra = _create_capsule(
                vault, focus, summary, decision_list, outcome, warnings
            )
    else:
        action, node, extra = _create_capsule(
            vault, focus, summary, decision_list, outcome, warnings
        )
    focus_payload = focus.to_frontmatter()
    focus_payload["status"] = "closed"
    focus_payload["updated_at"] = now_iso()
    focus_payload["body"] = focus.body
    closed = _validate_focus(focus_payload)
    try:
        with vault.writing("close"):
            atomic_write_text(vault.focus_path(session_id), closed.to_markdown())
    except OSError as error:
        raise UserError(
            f"焦点关闭失败：{error}", hint="胶囊已写入，重试 memory_close 可继续"
        ) from error
    _index_pending(vault, warnings)
    capsule_path = vault.find_node_path(node.id)
    payload = {
        "id": node.id,
        "capsule_id": node.id,
        "path": vault.relative(capsule_path) if capsule_path is not None else None,
        "action": action,
        "outcome": outcome,
        "status": node.status,
        "warnings": warnings,
        "degraded": bool(extra.get("degraded", False)),
    }
    _audit_write_attempt(
        vault,
        path="close",
        action=action,
        node_id=node.id,
        duration_ms=(time.monotonic() - started) * 1000.0,
        degraded=bool(extra.get("degraded", False)),
        extra={"session_id": session_id, **extra},
    )
    try:
        capture = memory_log(
            vault,
            session_id=session_id,
            kind="system_note",
            content=f"会话关闭（{outcome}）：{summary}",
            meta={
                "event": "close",
                "capsule_id": node.id,
                "outcome": outcome,
                "decisions": decision_list,
            },
        )
        payload["l0_seq"] = capture["seq"]
        warnings.extend(capture["warnings"])
    except ZymorError as error:
        warnings.append(f"关闭事件未写入 L0：{error.message}")
    return payload


def _find_capsules(vault: Vault, focus: FocusFile) -> list[MemoryNode]:
    directory = vault.nodes_dir / "episodic"
    if not directory.is_dir():
        return []
    found: list[MemoryNode] = []
    for path in sorted(directory.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            node = MemoryNode.from_markdown(text)
        except (OSError, ZymorError):
            continue
        if node.layer != "L2" or node.category != "episodic":
            continue
        if node.status == "deprecated":
            continue
        if node.project != focus.project or node.title != _capsule_title(focus.task):
            continue
        found.append(node)
    found.sort(key=lambda item: _sort_stamp(item.updated_at), reverse=True)
    return found


def _sort_stamp(value: str) -> float:
    try:
        return parse_iso(value).timestamp()
    except ValueError:
        return 0.0


def _create_capsule(
    vault: Vault,
    focus: FocusFile,
    summary: str,
    decisions: list[str],
    outcome: str,
    warnings: list[str],
    supersede: MemoryNode | None = None,
) -> tuple[str, MemoryNode, dict[str, Any]]:
    title = _capsule_title(focus.task)
    pointer = vault.relative(vault.focus_path(focus.session_id))
    body = build_capsule_body(focus, summary, decisions)
    body, degraded = _fit_capsule_body(title, body, pointer)
    if degraded:
        warnings.append("胶囊正文超过 §1.4 上限，已保留必需 H2 并把完整焦点记为 evidence 指针")
    status = "verified" if outcome == "completed" else "hypothesis"
    confidence = 0.7 if status == "verified" else 0.4
    verify_count = 1 if status == "verified" else 0
    supersedes = [supersede.id] if supersede is not None else []
    node = _build_capsule_node(
        vault,
        focus,
        title=title,
        summary=summary,
        body=body,
        status=status,
        confidence=confidence,
        verify_count=verify_count,
        supersedes=supersedes,
        pointer=pointer,
    )
    write_node(vault, node, "close")
    extra: dict[str, Any] = {"degraded": degraded}
    if supersede is not None:
        tombstone = _deprecate_node(vault, supersede, node, "close")
        extra["tombstone"] = tombstone
        warnings.append(f"既有胶囊 {supersede.id} 为 verified，已另写节点并用 supersedes 替代")
    action = "degraded" if degraded else "created"
    return action, node, extra


def _append_capsule(
    vault: Vault,
    focus: FocusFile,
    node: MemoryNode,
    summary: str,
    decisions: list[str],
    outcome: str,
) -> tuple[str, MemoryNode, dict[str, Any]]:
    pointer = vault.relative(vault.focus_path(focus.session_id))
    next_step = _focus_sections(focus).get("待验证") or MISSING_SECTION_TEXT
    body = _update_capsule_body(
        node.body,
        {"当前状态": summary, "下一步": next_step},
        {"关键决策与坑": decisions},
    )
    degraded = False
    if _body_exceeds(body, MAX_BODY_TOKENS, MAX_BODY_CJK):
        body, degraded = _fit_capsule_body(node.title, body, pointer)
    timestamp = now_iso()
    evidence = list(node.evidence)
    if pointer and pointer not in evidence:
        evidence.append(pointer)
    updates: dict[str, Any] = {
        "body": body,
        "evidence": evidence,
        "updated_at": timestamp,
    }
    if outcome == "completed":
        updates.update(
            {
                "status": "verified",
                "confidence": max(0.7, node.confidence),
                "verify_count": max(1, node.verify_count),
                "last_verified": timestamp,
            }
        )
    updated = update_node(vault, node.id, updates, "close")
    extra: dict[str, Any] = {
        "degraded": degraded,
        "sections_after": {"当前状态": summary, "下一步": next_step},
    }
    action = "degraded" if degraded else "appended"
    return action, updated, extra


def _build_capsule_node(
    vault: Vault,
    focus: FocusFile,
    *,
    title: str,
    summary: str,
    body: str,
    status: str,
    confidence: float,
    verify_count: int,
    supersedes: list[str],
    pointer: str,
) -> MemoryNode:
    timestamp = now_iso()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "id": _fresh_node_id(vault),
        "title": title,
        "summary": summary,
        "category": "episodic",
        "layer": "L2",
        "status": status,
        "confidence": confidence,
        "arousal": 0.5,
        "hit_count": 0,
        "verify_count": verify_count,
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_verified": timestamp,
        "valid_from": timestamp,
        "source": f"session:{focus.session_id}",
        "created_by": "agent",
        "project": focus.project,
        "evidence": [pointer] if pointer else [],
        "supersedes": supersedes,
        "body": body,
    }
    return _validate_node(payload)


def build_capsule_body(
    focus: FocusFile | dict[str, Any], summary: str, decisions: list[str] | None
) -> str:
    """入参：焦点、关闭摘要与决策列表。出参：胶囊正文。按 §4.5 固定顺序组装七段正文。"""
    sections = _focus_sections(focus)
    decision_list = [str(item).strip() for item in (decisions or []) if str(item).strip()]
    current = summary.strip() or MISSING_SECTION_TEXT
    target = sections.get("当前目标") or MISSING_SECTION_TEXT
    background = sections.get("最近上下文") or MISSING_SECTION_TEXT
    decision_line = decision_list[0] if decision_list else NO_DECISION_TEXT
    next_step = sections.get("待验证") or MISSING_SECTION_TEXT
    pitfall_parts: list[str] = []
    excluded = sections.get("已排除路径")
    if excluded:
        pitfall_parts.append(excluded)
    pitfall_parts.extend(f"- {item}" for item in decision_list)
    pitfalls = "\n".join(pitfall_parts) or MISSING_SECTION_TEXT
    title = _capsule_title(_focus_task(focus))
    pairs = [
        ("目标", target),
        ("当前状态", current),
        ("背景", background),
        ("决策", decision_line),
        ("结果与教训", current),
        ("下一步", next_step),
        ("关键决策与坑", pitfalls),
    ]
    return _compose_body(title, pairs)


def _capsule_title(task: str) -> str:
    text = " ".join(str(task).split())
    return text[:MAX_TITLE] or DEFAULT_CAPSULE_TITLE


def _focus_task(focus: FocusFile | dict[str, Any]) -> str:
    if isinstance(focus, FocusFile):
        return focus.task
    task = focus.get("task") if isinstance(focus, dict) else None
    return str(task) if task else ""


def _focus_sections(focus: FocusFile | dict[str, Any]) -> dict[str, str]:
    return _sections_map(_focus_body_text(focus))


def _focus_body_text(focus: FocusFile | dict[str, Any]) -> str:
    if isinstance(focus, FocusFile):
        return focus.body
    if isinstance(focus, dict):
        raw_body = focus.get("body")
        if isinstance(raw_body, str) and raw_body.strip():
            return raw_body
        raw_sections = focus.get("sections")
        if isinstance(raw_sections, dict):
            return _compose_focus_body(
                {str(name): str(value) for name, value in raw_sections.items()}
            )
    raise ValidationError("focus 必须是 FocusFile 或含 body/sections 的字典", field="focus")


def _sections_map(body: str) -> dict[str, str]:
    _, pairs = _split_body(body)
    return {name: content for name, content in pairs}


def _update_capsule_body(
    old_body: str, updates: dict[str, str], appends: dict[str, list[str]]
) -> str:
    preamble, pairs = _split_body(old_body)
    title = _preamble_title(preamble) or DEFAULT_CAPSULE_TITLE
    names = {name for name, _ in pairs}
    result: list[tuple[str, str]] = []
    for name, content in pairs:
        value = content
        if name in updates:
            value = updates[name]
        if name in appends:
            additions = [f"- {item}" for item in appends[name] if f"- {item}" not in value]
            if additions:
                value = f"{value}\n" + "\n".join(additions) if value else "\n".join(additions)
        result.append((name, value))
    for name in CAPSULE_ORDER:
        if name in names:
            continue
        missing_value = updates.get(name)
        if missing_value is None and name in appends:
            missing_value = "\n".join(f"- {item}" for item in appends[name])
        result.append((name, missing_value or MISSING_SECTION_TEXT))
    return _compose_body(title, result)


def _preamble_title(preamble: str) -> str:
    for line in preamble.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def memory_log(
    vault: Vault,
    *,
    session_id: str,
    kind: str,
    content: str,
    meta: dict[str, Any] | None = None,
    ts: str | None = None,
    seq: int | None = None,
) -> dict[str, Any]:
    """入参：vault、会话、事件与元数据。出参：追加结果。向 L0 追加一条事件（尽力而为）。"""
    started = time.monotonic()
    if not is_session_id(session_id):
        raise ValidationError("非法会话标识", field="session_id")
    if kind not in EVENT_KINDS:
        raise ValidationError(
            f"未知事件类型: {kind}", field="kind", hint=f"可选：{', '.join(EVENT_KINDS)}"
        )
    if not isinstance(content, str):
        raise ValidationError("content 必须是字符串", field="content")
    if meta is not None and not isinstance(meta, dict):
        raise ValidationError("meta 必须是对象", field="meta")
    if ts is not None and not is_iso(ts):
        raise ValidationError("ts 必须是带时区的 ISO8601 文本", field="ts")
    if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool)):
        raise ValidationError("seq 必须是整数", field="seq")
    try:
        with vault.writing("log"):
            entry = append_event(vault.l0_dir, session_id, kind, content, meta, ts=ts, seq=seq)
    except OSError as error:
        raise UserError(f"L0 追加失败：{error}", hint="检查磁盘空间与文件权限后重试") from error
    warnings: list[str] = []
    if seq is not None and entry.seq != seq:
        warnings.append(f"seq={seq} 不大于已有最大值，已改派为 {entry.seq}")
    truncated = bool(entry.meta.get("truncated", False))
    _audit_write_attempt(
        vault,
        path="l0_mcp",
        action="append",
        node_id=None,
        duration_ms=(time.monotonic() - started) * 1000.0,
        extra={"session_id": session_id, "seq": entry.seq, "kind": entry.kind},
    )
    return {
        "session_id": entry.session_id,
        "seq": entry.seq,
        "ts": entry.ts,
        "truncated": truncated,
        "warnings": warnings,
    }


def append_dissonance(
    vault: Vault,
    *,
    new_id: str,
    old_id: str | None = None,
    reason: str,
    entry_type: str = "conflict",
    question: str | None = None,
) -> None:
    """入参：vault、节点 id 与原因。出参：无。向晨间追问队列追加 pending 条目。"""
    if entry_type not in VALID_DISSONANCE_TYPES:
        raise ValidationError(
            f"未知队列条目类型: {entry_type}",
            field="entry_type",
            hint=f"可选：{', '.join(VALID_DISSONANCE_TYPES)}",
        )
    if not is_memory_id(new_id):
        raise ValidationError(f"非法节点 id: {new_id}", field="new_id")
    if entry_type == "conflict":
        if old_id is None:
            raise ValidationError(
                "conflict 条目必须提供 old_id",
                field="old_id",
                hint="人格候选请使用 entry_type=persona-proposal 且 old_id=None",
            )
        if not is_memory_id(old_id):
            raise ValidationError(f"非法节点 id: {old_id}", field="old_id")
    elif old_id is not None:
        raise ValidationError("persona-proposal 条目的 old_id 必须为空", field="old_id")
    if not isinstance(reason, str):
        raise ValidationError("reason 必须是字符串", field="reason")
    if question is not None and not isinstance(question, str):
        raise ValidationError("question 必须是字符串", field="question")
    existing = read_dissonance(vault)
    for entry in existing:
        if entry.get("state") != "open":
            continue
        if entry.get("type", "conflict") != entry_type:
            continue
        if entry_type == "conflict":
            if {entry.get("new_id"), entry.get("old_id")} == {new_id, old_id}:
                return
        elif entry.get("new_id") == new_id:
            return
    path = vault.dissonance_path
    qid = _next_qid(existing)
    created_at = now_iso()
    entry_text = _dissonance_entry(qid, created_at, new_id, old_id, reason, entry_type, question)
    if path.is_file():
        try:
            current = path.read_text(encoding="utf-8")
        except OSError as error:
            raise UserError(f"失调队列不可读：{error}", hint="检查文件权限") from error
        content = current.rstrip("\n") + "\n\n" + entry_text
    else:
        content = DISSONANCE_HEADER + entry_text
    try:
        atomic_write_text(path, content)
    except OSError as error:
        raise UserError(f"失调队列写入失败：{error}", hint="检查磁盘空间与文件权限") from error


def read_dissonance(vault: Vault) -> list[dict[str, Any]]:
    """入参：vault。出参：队列条目列表。解析 dream/dissonance.md 的晨间追问队列。"""
    path = vault.dissonance_path
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    raw_entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        header = QID_RE.match(stripped)
        if header:
            if current is not None:
                raw_entries.append(current)
            current = {
                "qid": header.group(1),
                "created_at": header.group(2).strip(),
                "fields": {},
            }
            continue
        if stripped.startswith("### "):
            if current is not None:
                raw_entries.append(current)
            current = None
            continue
        if current is None:
            continue
        bullet = BULLET_RE.match(stripped)
        if not bullet:
            continue
        item = bullet.group(1)
        if "：" in item:
            key, value = item.split("：", 1)
        elif ":" in item:
            key, value = item.split(":", 1)
        else:
            continue
        current["fields"][key.strip()] = value.strip()
    if current is not None:
        raw_entries.append(current)
    entries: list[dict[str, Any]] = []
    for raw in raw_entries:
        entry = _finalize_dissonance(raw)
        if entry is not None:
            entries.append(entry)
    return entries


def _finalize_dissonance(raw: dict[str, Any]) -> dict[str, Any] | None:
    fields = raw["fields"]
    type_raw = fields.get("类型", "conflict").strip() or "conflict"
    entry_type = type_raw if type_raw in VALID_DISSONANCE_TYPES else "conflict"
    node_field = fields.get("冲突节点", "")
    new_match = re.search(r"new\s*=\s*(mem_\d{8}_[0-9a-f]{10})", node_field)
    old_match = re.search(r"old\s*=\s*(mem_\d{8}_[0-9a-f]{10})", node_field)
    new_id = new_match.group(1) if new_match else None
    old_id = old_match.group(1) if old_match else None
    if new_id is None:
        return None
    if entry_type == "conflict" and old_id is None:
        return None
    attempts_raw = fields.get("attempts", "0")
    match = re.search(r"\d+", attempts_raw)
    attempts = int(match.group(0)) if match else 0
    state = fields.get("状态", "open").strip() or "open"
    return {
        "qid": raw["qid"],
        "created_at": raw["created_at"],
        "type": entry_type,
        "question": fields.get("问题", ""),
        "new_id": new_id,
        "old_id": old_id,
        "attempts": attempts,
        "state": state,
        "reason": fields.get("原因", ""),
    }


def update_dissonance(
    vault: Vault,
    qid: str,
    *,
    state: str | None = None,
    attempts: int | None = None,
) -> dict[str, Any]:
    """入参：vault、qid、可选状态与 attempts。出参：更新后的条目。只改目标条目的对应行。"""
    if state is not None and state not in VALID_DISSONANCE_STATES:
        raise ValidationError(
            f"非法追问状态: {state}",
            field="state",
            hint="open / answered / dismissed / unverifiable",
        )
    path = vault.dissonance_path
    if not path.is_file():
        raise NotFoundError(f"追问队列不存在: {path}", hint="队列由写入冲突或做梦仲裁创建")
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^(?=### )", text)
    found = False
    for index, block in enumerate(parts):
        header = re.match(r"^###\s+(Q\d+)\s+·", block)
        if header is None or header.group(1) != qid:
            continue
        if state is not None:
            block = re.sub(r"(?m)^- 状态：.*$", f"- 状态：{state}", block, count=1)
        if attempts is not None:
            block = re.sub(r"(?m)^- attempts:.*$", f"- attempts: {int(attempts)}", block, count=1)
        parts[index] = block
        found = True
        break
    if not found:
        raise NotFoundError(f"追问不存在: {qid}", hint="查看 dream/dissonance.md 确认 qid")
    atomic_write_text(path, "".join(parts))
    for item in read_dissonance(vault):
        if item.get("qid") == qid:
            return item
    raise NotFoundError(f"追问不存在: {qid}", hint="查看 dream/dissonance.md 确认 qid")


def _next_qid(existing: list[dict[str, Any]]) -> str:
    highest = 0
    for entry in existing:
        qid = str(entry.get("qid") or "")
        match = re.search(r"\d+", qid)
        if match:
            highest = max(highest, int(match.group(0)))
    return f"Q{highest + 1}"


def _dissonance_entry(
    qid: str,
    created_at: str,
    new_id: str,
    old_id: str | None,
    reason: str,
    entry_type: str = "conflict",
    question: str | None = None,
) -> str:
    if entry_type == "persona-proposal":
        shown_question = (
            question.strip()
            if question and question.strip()
            else f"人格规则候选 `{new_id}` 待确认，确认后升为 L4 verified。"
        )
        lines = [
            f"### {qid} · {created_at}",
            "",
            f"- 问题：{shown_question}",
            "- 类型：persona-proposal",
            f"- 冲突节点：new={new_id}；old=-",
            f"- 原因：{reason.strip() or '未满足人格规则确认门槛'}",
            "- 候选选项：确认采纳 / 继续观察 / 忽略（不改写用户原话）",
            "- attempts: 0",
            "- 状态：open",
            "",
        ]
        return "\n".join(lines)
    shown_question = (
        question.strip()
        if question and question.strip()
        else f"`{new_id}` 与 `{old_id}` 的结论可能冲突，请确认以哪条为准。"
    )
    lines = [
        f"### {qid} · {created_at}",
        "",
        f"- 问题：{shown_question}",
        f"- 冲突节点：new={new_id}；old={old_id}",
        f"- 原因：{reason.strip() or '写入路径判定冲突'}",
        "- 候选选项：采纳新节点 / 保留旧节点 / 两者都不成立",
        "- attempts: 0",
        "- 状态：open",
        "",
    ]
    return "\n".join(lines)


DISSONANCE_HEADER = (
    "# 晨间追问队列\n\n"
    "冲突节点对与人格候选规则的待决问题；由做梦仲裁或 `memory_answer` 消费，"
    "服务端不主动推入宿主上下文。\n\n"
)


def _validate_node(payload: dict[str, Any]) -> MemoryNode:
    try:
        return MemoryNode.model_validate(payload)
    except PydanticValidationError as error:
        first = error.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "node"
        raise ValidationError(f"{location}: {first['msg']}", field=location) from None


def _fresh_node_id(vault: Vault) -> str:
    for _ in range(16):
        candidate = new_memory_id()
        if vault.find_node_path(candidate) is None:
            return candidate
    raise UserError("无法生成唯一节点 id", hint="检查 vault 内节点文件后重试")


def _split_body(text: str) -> tuple[str, list[tuple[str, str]]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in normalized.split("\n"):
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    merged: list[tuple[str, str]] = []
    positions: dict[str, int] = {}
    for name, buffer in sections:
        content = "\n".join(buffer).strip()
        if name in positions:
            position = positions[name]
            previous = merged[position][1]
            merged[position] = (name, f"{previous}\n{content}".strip())
        else:
            positions[name] = len(merged)
            merged.append((name, content))
    return "\n".join(preamble).strip(), merged


def _compose_body(title: str, pairs: list[tuple[str, str]], preamble: str = "") -> str:
    lines = [f"# {title}", ""]
    if preamble.strip():
        lines.extend([preamble.strip(), ""])
    for name, content in pairs:
        lines.extend([f"## {name}", content.strip(), ""])
    return "\n".join(lines).rstrip("\n") + "\n"


def _fill_required_sections(
    preamble: str, pairs: list[tuple[str, str]], required: list[str]
) -> list[tuple[str, str]]:
    folded = _fold_preamble(preamble, pairs, required)
    names = {name for name, _ in folded}
    result = list(folded)
    for name in required:
        if name not in names:
            result.append((name, MISSING_SECTION_TEXT))
    return result


def _fold_preamble(
    preamble: str, pairs: list[tuple[str, str]], required: list[str]
) -> list[tuple[str, str]]:
    result = list(pairs)
    text = preamble.strip()
    if not text:
        return result
    first = required[0]
    for position, (name, content) in enumerate(result):
        if name == first:
            result[position] = (name, f"{text}\n{content}".strip())
            return result
    return [(first, text), *result]


def _cjk_count(text: str) -> int:
    return sum(1 for char in text if _is_cjk(char))


def _body_exceeds(text: str, token_limit: int, cjk_limit: int = MAX_BODY_CJK) -> bool:
    return estimate_tokens(text) > token_limit or _cjk_count(text) > cjk_limit


def _fit_pairs(
    title: str, pairs: list[tuple[str, str]], token_limit: int, cjk_limit: int
) -> tuple[list[tuple[str, str]], bool]:
    heading = f"# {title}\n" + "".join(f"## {name}\n" for name, _ in pairs)
    fixed_tokens = estimate_tokens(heading)
    fixed_cjk = _cjk_count(heading)
    count = max(1, len(pairs))
    margin = count * 6
    token_pool = max(0, token_limit - fixed_tokens - margin)
    cjk_pool = max(0, cjk_limit - fixed_cjk - margin)
    per_token = max(1, token_pool // count)
    per_cjk = max(1, cjk_pool // count)
    fitted: list[tuple[str, str]] = []
    truncated = False
    for name, content in pairs:
        text = content.strip()
        if not text:
            fitted.append((name, ""))
            continue
        clipped, changed = _clip_text(text, per_token, per_cjk)
        fitted.append((name, clipped))
        truncated = truncated or changed
    return fitted, truncated


def _clip_text(text: str, token_budget: int, cjk_budget: int) -> tuple[str, bool]:
    if not text:
        return "", False
    if estimate_tokens(text) <= token_budget and _cjk_count(text) <= cjk_budget:
        return text, False
    kept: list[str] = []
    lines = text.split("\n")
    for line in lines:
        candidate = "\n".join([*kept, line])
        fits = estimate_tokens(candidate) <= token_budget and _cjk_count(candidate) <= cjk_budget
        if kept:
            if not fits:
                break
            kept.append(line)
            continue
        if fits:
            kept.append(line)
            continue
        partial = ""
        for char in line:
            trial = partial + char
            if estimate_tokens(trial) <= token_budget and _cjk_count(trial) <= cjk_budget:
                partial = trial
            else:
                break
        if partial:
            kept.append(partial)
        break
    clipped = "\n".join(kept).strip()
    return clipped or MISSING_SECTION_TEXT, True


def _fit_capsule_body(title: str, body: str, pointer: str) -> tuple[str, bool]:
    if not _body_exceeds(body, MAX_BODY_TOKENS, MAX_BODY_CJK):
        return body, False
    _, pairs = _split_body(body)
    extended = [*pairs, ("关联", f"- 完整焦点：{pointer}")]
    fitted, _ = _fit_pairs(title, extended, MAX_BODY_TOKENS, MAX_BODY_CJK)
    result = _compose_body(title, fitted)
    if _body_exceeds(result, MAX_BODY_TOKENS, MAX_BODY_CJK):
        strict = [(name, MISSING_SECTION_TEXT) for name, _ in extended]
        result = _compose_body(title, strict)
    return result, True


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValidationError(f"{field_name} 必须是字符串列表", field=field_name)
    result: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ValidationError(f"{field_name} 必须是字符串列表", field=field_name)
        text = item.strip()
        if text:
            result.append(text)
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


def _audit_write_attempt(
    vault: Vault,
    *,
    action: str,
    node_id: str | None = None,
    llm_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    duration_ms: float = 0.0,
    error: str | None = None,
    degraded: bool = False,
    path: str = "explicit",
    extra: dict[str, Any] | None = None,
) -> None:
    record: dict[str, Any] = {
        "ts": now_iso(),
        "path": path,
        "action": action,
        "id": node_id,
        "llm_calls": llm_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": round(duration_ms, 3),
        "error": error,
        "degraded": degraded,
    }
    if extra:
        for key, value in extra.items():
            if key in record or value is None:
                continue
            record[key] = value
    try:
        append_audit(vault, AUDIT_PATH, record)
    except OSError:
        return


def _index_pending(vault: Vault, warnings: list[str]) -> None:
    try:
        reason = Index(vault.root).dirty_reason()
    except ZymorError:
        return
    if reason:
        warnings.append(f"index-pending: 索引更新失败，文件已保留：{reason}")
