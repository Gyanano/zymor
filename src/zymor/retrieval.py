from __future__ import annotations

import functools
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from zymor.config import Config, load_config
from zymor.errors import IndexFailure, NotFoundError, ValidationError, ZymorError
from zymor.invalidation import stale_reasons as node_stale_reasons
from zymor.model.node import CATEGORIES, MemoryNode
from zymor.store.audit import append_audit, read_audit
from zymor.store.index import Index
from zymor.store.nodes import read_node, record_hit
from zymor.tasktype import task_type_from_text
from zymor.tokenizer import build_match_query, estimate_tokens, truncate_to_tokens
from zymor.util.time import now_iso, parse_iso, utc_now
from zymor.vault import Vault

__all__ = [
    "BrowseEntry",
    "BrowseResult",
    "LinksEntry",
    "LinksResult",
    "ReadResult",
    "RetrievalSettings",
    "SearchFilters",
    "SearchHit",
    "SearchResult",
    "memory_browse",
    "memory_links",
    "memory_read",
    "memory_search",
    "task_type_from_text",
]

DEFAULT_WEIGHTS: dict[str, float] = {
    "bm25": 0.55,
    "confidence": 0.15,
    "recency": 0.15,
    "arousal": 0.10,
    "hits": 0.05,
}
DEFAULT_STATUSES: tuple[str, ...] = ("hypothesis", "verified", "disputed")
STATUS_BOOSTS: dict[str, float] = {
    "verified": 0.10,
    "hypothesis": 0.0,
    "disputed": -0.10,
    "deprecated": -0.40,
}
DEFAULT_TOP_K = 8
TOP_K_MAX = 50
CANDIDATE_LIMIT = 100
HIT_REFERENCE = 50.0
FACET_BOOST = 0.10
CAPSULE_PROJECT_BOOST = 0.05
PROJECT_SCOPE_BOOST = 0.10
TRUNCATION_MARKER = "…（已截断）"
CATALOG_LINE_RE = re.compile(
    r"^- \[(?P<title>[^\]]*)\]\((?P<target>[^)]*)\)"
    r"\s*·\s*(?P<id>[^·]*?)\s*·\s*(?P<status>[^·]*?)\s*·\s*"
    r"(?P<confidence>[^·]*?)\s*·\s*(?P<updated_at>[^·]*?)\s*$"
)


@dataclass
class SearchFilters:
    categories: list[str] | None = None
    layers: list[str] | None = None
    statuses: list[str] | None = None
    tags: list[str] | None = None
    project: str | None = None
    applies_to: list[str] | None = None
    updated_since: str | None = None
    valid_only: bool = True
    include_deprecated: bool = False
    include_stale: bool = False


@dataclass
class SearchHit:
    id: str
    title: str
    summary: str
    layer: str
    category: str
    status: str
    confidence: float
    score: float
    path: str
    why: list[str] = field(default_factory=list)
    scope: str = ""
    updated_at: str = ""
    stale: bool = False
    stale_reasons: list[str] = field(default_factory=list)


@dataclass
class SearchResult:
    hits: list[SearchHit] = field(default_factory=list)
    tokens_estimate: int = 0
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class ReadResult:
    node: MemoryNode
    path: str
    scope: str
    tokens_estimate: int = 0
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)
    stale: bool = False
    stale_reasons: list[str] = field(default_factory=list)


@dataclass
class BrowseEntry:
    id: str | None
    title: str
    summary: str = ""
    status: str = ""
    confidence: float | None = None
    updated_at: str | None = None
    id_line: str | None = None


@dataclass
class BrowseResult:
    entries: list[BrowseEntry] = field(default_factory=list)
    next_offset: int | None = None
    tokens_estimate: int = 0
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class LinksEntry:
    id: str
    title: str
    summary: str
    status: str
    layer: str
    category: str


@dataclass
class LinksResult:
    out: list[LinksEntry] = field(default_factory=list)
    in_: list[LinksEntry] = field(default_factory=list)
    tokens_estimate: int = 0
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class RetrievalSettings:
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    half_life_days: float = 30.0
    default_top_k: int = DEFAULT_TOP_K
    token_budget: int = 1200
    links_token_budget: int = 800
    links_max_lines: int = 20

    @classmethod
    def from_config(cls, config: Config) -> RetrievalSettings:
        """入参：Config 配置对象。出参：回填默认值的 `[retrieval]` 设置。读取评分权重与工具预算。"""
        weights = dict(DEFAULT_WEIGHTS)
        raw_weights = config.get("retrieval.weights", {})
        if isinstance(raw_weights, dict):
            for name, value in raw_weights.items():
                if not isinstance(name, str):
                    continue
                try:
                    weights[name] = float(value)
                except (TypeError, ValueError):
                    continue
        half_life = config.get("retrieval.half_life_days", 30)
        default_top_k = config.get("retrieval.default_top_k", DEFAULT_TOP_K)
        token_budget = config.get("retrieval.token_budget", 1200)
        links_token_budget = config.get("retrieval.links_token_budget", 800)
        links_max_lines = config.get("retrieval.links_max_lines", 20)
        return cls(
            weights=weights,
            half_life_days=float(half_life),
            default_top_k=int(default_top_k),
            token_budget=int(token_budget),
            links_token_budget=int(links_token_budget),
            links_max_lines=int(links_max_lines),
        )


def memory_search(
    scopes: list[tuple[str, Vault]],
    query: str,
    *,
    settings: RetrievalSettings,
    filters: SearchFilters | None = None,
    top_k: int = 8,
    token_budget: int | None = None,
    task_type: str | None = None,
    session_id: str | None = None,
    record_audit: bool = True,
) -> SearchResult:
    """入参：scope 列表、查询、设置与过滤；`top_k=8` 表示未指定并用 `settings.default_top_k`，
    `include_deprecated=True` 与 `valid_only=True` 正交，检索 deprecated 需同时 `valid_only=False`。
    出参：命中与预算。按 §3.3 评分并跨库合并。
    """
    started = time.perf_counter()
    if not scopes:
        raise ValidationError(
            "scopes 不能为空",
            field="scopes",
            hint="至少传入一个 (scope, Vault)，如 [('project', vault)]",
        )
    audit_vault = scopes[0][1]
    scope_text = ",".join(label for label, _ in scopes)
    session_key = session_id or ""
    active_filters = filters or SearchFilters()
    budget = settings.token_budget if token_budget is None else max(0, token_budget)
    limit = settings.default_top_k if top_k == DEFAULT_TOP_K else top_k
    limit = max(0, min(limit, TOP_K_MAX))
    warnings: list[str] = []
    query_text = query.strip()
    match_query = build_match_query(query_text) if query_text else ""

    if not match_query:
        warnings.append("查询为空：请提供至少一个可检索词项（中英文均可）")
        if record_audit:
            _write_audit(
                audit_vault,
                kind="search",
                scope=scope_text,
                returned=[],
                tokens_estimate=0,
                latency_ms=_elapsed_ms(started),
                session_id=session_key,
                query=query_text,
                warnings=warnings,
            )
        return SearchResult(hits=[], tokens_estimate=0, truncated=False, warnings=warnings)

    statuses = _effective_statuses(active_filters)
    if not statuses:
        warnings.append(
            "status 过滤后没有可检索状态：deprecated 需同时设置 include_deprecated=True"
        )
        if record_audit:
            _write_audit(
                audit_vault,
                kind="search",
                scope=scope_text,
                returned=[],
                tokens_estimate=0,
                latency_ms=_elapsed_ms(started),
                session_id=session_key,
                query=query_text,
                warnings=warnings,
            )
        return SearchResult(hits=[], tokens_estimate=0, truncated=False, warnings=warnings)

    updated_since: datetime | None = None
    if active_filters.updated_since:
        try:
            updated_since = parse_iso(active_filters.updated_since)
        except ValueError as error:
            raise ValidationError(
                f"updated_since 不是合法的 ISO8601 时间: {active_filters.updated_since}",
                field="updated_since",
                hint="使用带时区的时间，例如 2026-10-01T00:00:00+08:00",
            ) from error

    now = utc_now()
    candidates: list[tuple[str, Vault, dict[str, Any]]] = []
    seen: dict[str, str] = {}
    for label, vault in scopes:
        index = Index(vault.root)
        rows = _with_index_recovery(
            index,
            functools.partial(_fetch_candidate_rows, index, match_query, active_filters, statuses),
        )
        for row in rows:
            if not _passes_filters(row, active_filters, statuses, now, updated_since):
                continue
            node_id = str(row["id"])
            if node_id in seen:
                warnings.append(
                    f"跨库 id 冲突：{node_id} 同时存在于 {seen[node_id]} 与 {label}，"
                    f"已保留 {seen[node_id]} 版本，需手工解决"
                )
                continue
            seen[node_id] = label
            candidates.append((label, vault, row))

    norms = _bm25_norms([float(row["rank"]) for _, _, row in candidates])
    ranked: list[SearchHit] = []
    for (label, vault, row), bm25_norm in zip(candidates, norms, strict=True):
        score, why = _score_candidate(
            row,
            label,
            vault,
            bm25_norm,
            active_filters,
            task_type,
            settings,
            now,
        )
        ranked.append(
            SearchHit(
                id=str(row["id"]),
                title=str(row["title"]),
                summary=str(row["summary"]),
                layer=str(row["layer"]),
                category=str(row["category"]),
                status=str(row["status"]),
                confidence=float(row["confidence"]),
                score=score,
                path=str(row["path"]),
                why=why,
                scope=label,
                updated_at=str(row["updated_at"]),
            )
        )
    ranked.sort(key=lambda hit: (-hit.score, -_updated_timestamp(hit.updated_at)))
    hits = ranked[:limit]
    costs = [estimate_tokens(_hit_line(hit)) for hit in hits]
    kept, truncated, tokens_estimate = _fit_costs(costs, budget)
    hits = hits[:kept]
    vault_by_id = {str(row["id"]): scope_vault for _label, scope_vault, row in candidates}
    for hit in hits:
        hit_vault = vault_by_id.get(hit.id)
        if hit_vault is None:
            continue
        try:
            node, _ = read_node(hit_vault, hit.id)
        except NotFoundError:
            continue
        reasons = node_stale_reasons(hit_vault, node)
        if reasons:
            hit.stale = True
            hit.stale_reasons = reasons
            warnings.append(f"节点 {hit.id} 锚点失效：" + "；".join(reasons))
    if not active_filters.include_stale:
        stale_ids = [hit.id for hit in hits if hit.stale]
        if stale_ids:
            warnings.append(
                "已排除 stale 节点：" + "、".join(stale_ids) + "（加 include_stale=true 可查看）"
            )
            hits = [hit for hit in hits if not hit.stale]
    if not ranked:
        warnings.append(
            "无匹配结果：可换关键词、放宽 status 过滤；需要历史节点时设置 "
            "include_deprecated=True 且 valid_only=False"
        )
    if record_audit:
        _write_audit(
            audit_vault,
            kind="search",
            scope=scope_text,
            returned=[hit.id for hit in hits],
            tokens_estimate=tokens_estimate,
            latency_ms=_elapsed_ms(started),
            session_id=session_key,
            query=query_text,
            warnings=warnings,
        )
    return SearchResult(
        hits=hits, tokens_estimate=tokens_estimate, truncated=truncated, warnings=warnings
    )


def memory_read(
    scopes: list[tuple[str, Vault]],
    node_id: str,
    *,
    max_tokens: int = 1500,
    session_id: str | None = None,
    count_hit: bool = True,
    record_audit: bool = True,
) -> ReadResult:
    """入参：scope 列表、节点 id、预算、会话与写入开关。出参：节点与截断信息。
    读取节点；`count_hit=True` 时累计 hit_count，`record_audit=False` 时跳过检索审计。
    """
    started = time.perf_counter()
    if not scopes:
        raise ValidationError(
            "scopes 不能为空",
            field="scopes",
            hint="至少传入一个 (scope, Vault)，如 [('project', vault)]",
        )
    audit_vault = scopes[0][1]
    found_label: str | None = None
    found_vault: Vault | None = None
    found_path: Path | None = None
    for label, vault in scopes:
        candidate_path = vault.find_node_path(node_id)
        if candidate_path is not None:
            found_label = label
            found_vault = vault
            found_path = candidate_path
            break
    if found_label is None or found_vault is None or found_path is None:
        raise NotFoundError(
            f"节点不存在: {node_id}",
            hint=(
                "用 `zymor search <query>` 或 memory_search 查找 id；"
                "跨库读取请同时传入项目库与用户库"
            ),
        )
    node, path = read_node(found_vault, node_id)
    session_key = session_id or ""
    counted = False
    if count_hit:
        counted = True
        if session_id:
            counted = not _already_counted(audit_vault, node_id, session_key)
        if counted:
            node = record_hit(found_vault, node_id)
    budget = max(0, max_tokens)
    warnings: list[str] = []
    summary_tokens = estimate_tokens(node.summary)
    body_tokens = estimate_tokens(node.body)
    if summary_tokens + body_tokens <= budget:
        truncated = False
        tokens_estimate = summary_tokens + body_tokens
        result_node = node
    else:
        truncated = True
        marker_tokens = estimate_tokens(TRUNCATION_MARKER)
        room = budget - summary_tokens - marker_tokens
        kept_body, _ = truncate_to_tokens(node.body, max(0, room))
        result_node = node.model_copy(update={"body": kept_body})
        tokens_estimate = summary_tokens + estimate_tokens(kept_body) + marker_tokens
        if tokens_estimate > budget:
            tokens_estimate = budget
        warnings.append("正文已按 max_tokens 截断：优先保留 summary")
    if record_audit:
        _write_audit(
            audit_vault,
            kind="read",
            scope=found_label,
            returned=[node_id],
            tokens_estimate=tokens_estimate,
            latency_ms=_elapsed_ms(started),
            session_id=session_key,
            node_id=node_id,
            dedup=counted,
            warnings=warnings,
        )
    return ReadResult(
        node=result_node,
        path=found_vault.relative(path),
        scope=found_label,
        tokens_estimate=tokens_estimate,
        truncated=truncated,
        warnings=warnings,
        stale=bool(node_stale_reasons(found_vault, node)),
        stale_reasons=node_stale_reasons(found_vault, node),
    )


def memory_rules(
    scopes: list[tuple[str, Vault]],
    *,
    task_type: str | None = None,
    limit: int = 5,
    token_budget: int = 400,
) -> dict[str, Any]:
    """入参：scope、任务类型、条数与预算。出参：verified L3 规则列表与 token 估算。"""
    candidates: list[tuple[str, MemoryNode]] = []
    seen: set[str] = set()
    for label, vault in scopes:
        rows = Index(vault.root).layer_rows("L3", statuses=["verified"], limit=max(limit * 5, 20))
        for row in rows:
            node_id = str(row["id"])
            if node_id in seen:
                continue
            try:
                node, _ = read_node(vault, node_id)
            except ZymorError:
                continue
            if task_type and node.applies_to and task_type not in node.applies_to:
                continue
            if node_stale_reasons(vault, node):
                continue
            seen.add(node_id)
            candidates.append((label, node))
    candidates.sort(key=lambda item: (-item[1].confidence, item[1].id))
    rules: list[dict[str, Any]] = []
    used = 0
    for label, node in candidates:
        if len(rules) >= limit:
            break
        line = f"[verified] {node.title}：{node.summary}"
        cost = estimate_tokens(line) + 1
        if used + cost > token_budget:
            break
        rules.append(
            {
                "id": node.id,
                "title": node.title,
                "summary": node.summary,
                "applies_to": list(node.applies_to),
                "scope": label,
                "confidence": node.confidence,
            }
        )
        used += cost
    return {
        "rules": rules,
        "count": len(rules),
        "tokens_estimate": used,
        "truncated": len(candidates) > len(rules),
    }


def memory_browse(
    vault: Vault,
    path: str,
    *,
    offset: int = 0,
    limit: int = 20,
    token_budget: int = 1500,
) -> BrowseResult:
    """入参：vault、ATLAS/category 路径与分页预算。出参：目录条目与下一页 offset。"""
    started = time.perf_counter()
    target_path, category = _browse_target(vault, path)
    if not target_path.is_file():
        raise NotFoundError(
            f"目录文件不存在: {vault.relative(target_path)}",
            hint="运行 `zymor index catalog` 生成 ATLAS 与 catalog 后重试",
        )
    text = _read_browse_text(target_path)
    entries, warnings = _parse_browse_entries(text, strict=category is not None)
    start = max(0, offset)
    size = max(0, limit)
    page = entries[start : start + size]
    costs = [estimate_tokens(entry.id_line or entry.title) for entry in page]
    kept, truncated, tokens_estimate = _fit_costs(costs, max(0, token_budget))
    page = page[:kept]
    next_offset = start + len(page) if start + len(page) < len(entries) else None
    if not entries:
        warnings.append("目录为空：运行 `zymor index catalog` 生成 ATLAS 与 catalog")
    returned = [entry.id for entry in page if entry.id is not None]
    _write_audit(
        vault,
        kind="browse",
        scope=vault.info.kind,
        returned=returned,
        tokens_estimate=tokens_estimate,
        latency_ms=_elapsed_ms(started),
        session_id="",
        warnings=warnings,
    )
    return BrowseResult(
        entries=page,
        next_offset=next_offset,
        tokens_estimate=tokens_estimate,
        truncated=truncated,
        warnings=warnings,
    )


def memory_links(
    scopes: list[tuple[str, Vault]],
    node_id: str,
    *,
    direction: Literal["out", "in", "both"] = "both",
    settings: RetrievalSettings | None = None,
) -> LinksResult:
    """入参：scope 列表、节点 id、方向与设置。出参：出链/入链摘要与预算。按双链顺藤摸瓜。"""
    started = time.perf_counter()
    if not scopes:
        raise ValidationError(
            "scopes 不能为空",
            field="scopes",
            hint="至少传入一个 (scope, Vault)，如 [('project', vault)]",
        )
    if direction not in {"out", "in", "both"}:
        raise ValidationError(f"direction 必须是 out/in/both: {direction}", field="direction")
    audit_vault = scopes[0][1]
    found_label: str | None = None
    found_vault: Vault | None = None
    for label, vault in scopes:
        if vault.find_node_path(node_id) is not None:
            found_label = label
            found_vault = vault
            break
    if found_label is None or found_vault is None:
        raise NotFoundError(
            f"节点不存在: {node_id}", hint="用 `zymor search <query>` 或 memory_search 查找 id"
        )
    active_settings = settings or RetrievalSettings.from_config(load_config())
    index = Index(found_vault.root)
    links = _with_index_recovery(index, lambda: index.links(node_id))
    warnings: list[str] = []
    max_lines = max(0, active_settings.links_max_lines)
    out_ids = list(links.get("out", [])) if direction in {"out", "both"} else []
    in_ids = list(links.get("in", [])) if direction in {"in", "both"} else []
    out_capped = len(out_ids) > max_lines
    in_capped = len(in_ids) > max_lines
    out_ids = out_ids[:max_lines]
    in_ids = in_ids[:max_lines]
    resolved = _resolve_link_nodes(scopes, found_vault, out_ids + in_ids)
    out_entries: list[LinksEntry] = []
    for link_id in out_ids:
        entry = _links_entry(resolved.get(link_id))
        if entry is None:
            warnings.append(f"链接目标节点不存在: {link_id}")
            continue
        out_entries.append(entry)
    in_entries: list[LinksEntry] = []
    for link_id in in_ids:
        entry = _links_entry(resolved.get(link_id))
        if entry is None:
            warnings.append(f"链接目标节点不存在: {link_id}")
            continue
        in_entries.append(entry)
    if out_capped:
        warnings.append(f"out 方向超过 {max_lines} 条上限，已截断")
    if in_capped:
        warnings.append(f"in 方向超过 {max_lines} 条上限，已截断")
    ordered = out_entries + in_entries
    costs = [estimate_tokens(_links_line(entry)) for entry in ordered]
    kept, budget_truncated, tokens_estimate = _fit_costs(
        costs, max(0, active_settings.links_token_budget)
    )
    kept_out = min(kept, len(out_entries))
    out_entries = out_entries[:kept_out]
    in_entries = in_entries[: max(0, kept - kept_out)]
    if budget_truncated:
        warnings.append("links 结果超出 token 预算，已截断")
    truncated = out_capped or in_capped or budget_truncated
    returned = list(dict.fromkeys(entry.id for entry in out_entries + in_entries))
    _write_audit(
        audit_vault,
        kind="links",
        scope=found_label,
        returned=returned,
        tokens_estimate=tokens_estimate,
        latency_ms=_elapsed_ms(started),
        session_id="",
        warnings=warnings,
    )
    return LinksResult(
        out=out_entries,
        in_=in_entries,
        tokens_estimate=tokens_estimate,
        truncated=truncated,
        warnings=warnings,
    )


def _weight(weights: dict[str, float], name: str) -> float:
    return float(weights.get(name, DEFAULT_WEIGHTS[name]))


def _bm25_norms(ranks: list[float]) -> list[float]:
    if not ranks:
        return []
    relevances = [-rank for rank in ranks]
    lowest = min(relevances)
    highest = max(relevances)
    if highest == lowest:
        return [1.0 for _ in ranks]
    span = highest - lowest
    return [(value - lowest) / span for value in relevances]


def _recency(updated_at: str, now: datetime, half_life_days: float) -> float:
    try:
        updated = parse_iso(updated_at)
    except ValueError:
        return 0.0
    age_days = (now - updated).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    if half_life_days <= 0:
        return 0.0
    return math.exp(-age_days / half_life_days)


def _hit_norm(hit_count: int) -> float:
    if hit_count <= 0:
        return 0.0
    return min(1.0, math.log1p(hit_count) / math.log1p(HIT_REFERENCE))


def _updated_timestamp(updated_at: str) -> float:
    try:
        return parse_iso(updated_at).timestamp()
    except ValueError:
        return float("-inf")


def _effective_statuses(filters: SearchFilters) -> list[str]:
    base = (
        list(DEFAULT_STATUSES) if not filters.statuses else [str(item) for item in filters.statuses]
    )
    statuses = [status for status in base if status != "deprecated"]
    if filters.include_deprecated:
        statuses.append("deprecated")
    return list(dict.fromkeys(statuses))


def _tag_matches(tags: list[str], wanted: list[str] | None) -> list[str]:
    if not wanted:
        return []
    wanted_set = set(wanted)
    return [tag for tag in tags if tag in wanted_set]


def _passes_filters(
    row: dict[str, Any],
    filters: SearchFilters,
    statuses: list[str],
    now: datetime,
    updated_since: datetime | None,
) -> bool:
    if str(row.get("status")) not in statuses:
        return False
    if updated_since is not None:
        try:
            updated = parse_iso(str(row.get("updated_at")))
        except ValueError:
            return False
        if updated < updated_since:
            return False
    if filters.tags:
        node_tags = [str(tag) for tag in row.get("tags") or []]
        if not set(node_tags) & set(filters.tags):
            return False
    if filters.applies_to:
        applies = [str(item) for item in row.get("applies_to") or []]
        if not set(applies) & set(filters.applies_to):
            return False
    if filters.valid_only:
        valid_to = row.get("valid_to")
        if valid_to is not None:
            try:
                expiry = parse_iso(str(valid_to))
            except ValueError:
                return False
            if expiry <= now:
                return False
    return True


def _score_candidate(
    row: dict[str, Any],
    scope_label: str,
    vault: Vault,
    bm25_norm: float,
    filters: SearchFilters,
    task_type: str | None,
    settings: RetrievalSettings,
    now: datetime,
) -> tuple[float, list[str]]:
    weights = settings.weights
    score = _weight(weights, "bm25") * bm25_norm
    score += _weight(weights, "confidence") * float(row["confidence"])
    score += _weight(weights, "recency") * _recency(
        str(row["updated_at"]), now, settings.half_life_days
    )
    score += _weight(weights, "arousal") * float(row["arousal"])
    score += _weight(weights, "hits") * _hit_norm(int(row["hit_count"]))
    score += STATUS_BOOSTS.get(str(row["status"]), 0.0)
    why = ["bm25"]
    node_tags = [str(tag) for tag in row.get("tags") or []]
    for tag in _tag_matches(node_tags, filters.tags):
        why.append(f"tag:{tag}")
    target_project = filters.project
    if target_project is None and scope_label == "project":
        target_project = vault.info.name
    if scope_label == "project":
        score += PROJECT_SCOPE_BOOST
        why.append("scope:project")
    applies_to = [str(item) for item in row.get("applies_to") or []]
    if (
        task_type is not None
        and task_type != "other"
        and str(row["layer"]) == "L4"
        and task_type in applies_to
    ):
        score += FACET_BOOST
        why.append(f"facet:{task_type}")
    if (
        str(row["layer"]) == "L2"
        and target_project is not None
        and row.get("project") == target_project
    ):
        score += CAPSULE_PROJECT_BOOST
        why.append(f"project:{target_project}")
    return score, why


def _hit_line(hit: SearchHit) -> str:
    return f"{hit.title} [{hit.id}] ({hit.layer}/{hit.category}) {hit.summary}"


def _links_line(entry: LinksEntry) -> str:
    return f"{entry.title} [{entry.id}] ({entry.layer}/{entry.category}) {entry.summary}"


def _fit_costs(costs: list[int], budget: int) -> tuple[int, bool, int]:
    total = sum(costs)
    if total <= budget:
        return len(costs), False, total
    marker = estimate_tokens(TRUNCATION_MARKER)
    used = 0
    kept = 0
    for cost in costs:
        if used + cost + marker > budget:
            break
        used += cost
        kept += 1
    estimate = used + marker
    if estimate > budget:
        estimate = budget
    return kept, True, max(0, estimate)


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _index_failure(error: IndexFailure) -> IndexFailure:
    return IndexFailure(
        f"索引不可用: {error.message}",
        hint="运行 `zymor index rebuild` 重建索引；若仍失败检查 vault 路径、磁盘权限与 SQLite 文件",
    )


def _with_index_recovery[T](index: Index, operation: Callable[[], T]) -> T:
    try:
        return operation()
    except IndexFailure:
        try:
            index.sync()
        except IndexFailure as sync_error:
            raise _index_failure(sync_error) from sync_error
        try:
            return operation()
        except IndexFailure as retry_error:
            raise _index_failure(retry_error) from retry_error


def _write_audit(
    vault: Vault,
    *,
    kind: str,
    scope: str,
    returned: list[str],
    tokens_estimate: int,
    latency_ms: float,
    session_id: str,
    query: str | None = None,
    node_id: str | None = None,
    dedup: bool | None = None,
    warnings: list[str] | None = None,
) -> None:
    record: dict[str, Any] = {"ts": now_iso(), "kind": kind}
    if query is not None:
        record["query"] = query
    record["returned"] = returned
    record["tokens_estimate"] = tokens_estimate
    record["latency_ms"] = round(latency_ms, 3)
    record["scope"] = scope
    record["session_id"] = session_id
    if node_id is not None:
        record["node_id"] = node_id
    if dedup is not None:
        record["dedup"] = dedup
    if warnings:
        record["warnings"] = list(warnings)
    append_audit(vault, "retrieval.jsonl", record)


def _already_counted(vault: Vault, node_id: str, session_key: str) -> bool:
    for record in read_audit(vault.retrieval_audit_path):
        if (
            record.get("kind") == "read"
            and record.get("node_id") == node_id
            and record.get("session_id") == session_key
            and record.get("dedup") is True
        ):
            return True
    return False


def _browse_target(vault: Vault, path: str) -> tuple[Path, str | None]:
    raw = path.strip()
    if not raw:
        raise NotFoundError(
            "memory_browse 的 path 不能为空",
            hint="使用 ATLAS 或 category 名（episodic/semantic/procedural/axioms）",
        )
    lowered = raw.lower()
    if lowered in {"atlas", "atlas.md"}:
        return vault.atlas_dir / "ATLAS.md", None
    category = lowered
    if lowered.startswith("catalog/"):
        category = lowered.split("/", 1)[1].strip()
    if category in CATEGORIES:
        return vault.nodes_dir / category / "_catalog.md", category
    raise NotFoundError(
        f"未知的 memory_browse path: {path}",
        hint=(
            "可用 path：ATLAS 或 category（episodic/semantic/procedural/axioms），"
            "也可用 catalog/<category>"
        ),
    )


def _read_browse_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValidationError(f"目录文件不是合法 UTF-8: {path}", field="encoding") from error
    except OSError as error:
        raise NotFoundError(
            f"目录文件不可读: {path}: {error}", hint="检查文件权限后重试"
        ) from error


def _parse_browse_entries(text: str, *, strict: bool) -> tuple[list[BrowseEntry], list[str]]:
    entries: list[BrowseEntry] = []
    warnings: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("<!--"):
            continue
        match = CATALOG_LINE_RE.match(line)
        if match is not None:
            title = match.group("title").strip()
            id_text = match.group("id").strip()
            target = match.group("target").strip()
            if not id_text and target.endswith(".md"):
                id_text = target[: -len(".md")]
            confidence_text = match.group("confidence").strip()
            try:
                confidence = float(confidence_text) if confidence_text else None
            except ValueError:
                confidence = None
            updated_text = match.group("updated_at").strip() or None
            entries.append(
                BrowseEntry(
                    id=id_text or None,
                    title=title,
                    summary="",
                    status=match.group("status").strip(),
                    confidence=confidence,
                    updated_at=updated_text,
                    id_line=line,
                )
            )
            continue
        if strict:
            warnings.append(f"无法解析的 catalog 行: {line}")
            continue
        title = line
        while title.startswith("#"):
            title = title[1:]
        title = title.strip()
        if title.startswith("- "):
            title = title[2:].strip()
        entries.append(
            BrowseEntry(
                id=None,
                title=title,
                summary="",
                status="",
                confidence=None,
                updated_at=None,
                id_line=line,
            )
        )
    return entries, warnings


def _links_entry(row: dict[str, Any] | None) -> LinksEntry | None:
    if row is None:
        return None
    return LinksEntry(
        id=str(row["id"]),
        title=str(row["title"]),
        summary=str(row["summary"]),
        status=str(row["status"]),
        layer=str(row["layer"]),
        category=str(row["category"]),
    )


def _fetch_candidate_rows(
    index: Index,
    match_query: str,
    filters: SearchFilters,
    statuses: list[str],
) -> list[dict[str, Any]]:
    return index.candidate_rows(
        match_query,
        categories=filters.categories,
        layers=filters.layers,
        statuses=statuses,
        project=filters.project,
        limit=CANDIDATE_LIMIT,
    )


def _fetch_nodes_by_ids(index: Index, ids: list[str]) -> dict[str, dict[str, Any]]:
    return index.nodes_by_ids(ids)


def _resolve_link_nodes(
    scopes: list[tuple[str, Vault]], preferred: Vault, ids: list[str]
) -> dict[str, dict[str, Any]]:
    resolved: dict[str, dict[str, Any]] = {}
    remaining = list(dict.fromkeys(link_id for link_id in ids if link_id))
    ordered_vaults: list[Vault] = [preferred]
    for _, vault in scopes:
        if vault.root != preferred.root:
            ordered_vaults.append(vault)
    for vault in ordered_vaults:
        if not remaining:
            break
        index = Index(vault.root)
        rows = _with_index_recovery(index, functools.partial(_fetch_nodes_by_ids, index, remaining))
        for link_id, row in rows.items():
            resolved[link_id] = row
        remaining = [link_id for link_id in remaining if link_id not in resolved]
    return resolved
