from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from zymor.config import Config
from zymor.errors import IndexFailure, ZymorError
from zymor.invalidation import stale_reasons as node_stale_reasons
from zymor.model.focus import FOCUS_H2, FocusFile
from zymor.model.node import CATEGORIES
from zymor.store.index import Index
from zymor.store.nodes import read_node
from zymor.tasktype import task_type_from_text
from zymor.tokenizer import estimate_tokens, truncate_to_tokens
from zymor.util.time import days_ago_iso, parse_iso, utc_now
from zymor.vault import Vault

PENDING_LIMIT = 3
PENDING_ITEM_TOKENS = 60
CAPSULE_RECENT_DAYS = 30
CAPSULE_LINE_TOKENS = 60
TRUNCATION_MARKER = "…（已截断以满足 {budget} tokens 预算）"


@dataclass
class ContextResult:
    text: str
    tokens_estimate: int
    section_tokens: dict[str, int]
    task_type: str
    degraded: bool
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：上下文组装结果字典。"""
        return {
            "text": self.text,
            "tokens_estimate": self.tokens_estimate,
            "section_tokens": self.section_tokens,
            "task_type": self.task_type,
            "degraded": self.degraded,
            "warnings": self.warnings,
        }


def _split_sections(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in body.splitlines():
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


def _load_focus(vault: Vault, session_id: str | None) -> FocusFile | None:
    if session_id is not None:
        path = vault.focus_path(session_id)
        candidates = [path] if path.is_file() else []
    else:
        candidates = sorted(vault.focus_dir.glob("*.md"))
    focuses: list[FocusFile] = []
    for path in candidates:
        try:
            focus = FocusFile.from_markdown(path.read_text(encoding="utf-8"))
        except (OSError, ZymorError):
            continue
        if focus.status == "active":
            focuses.append(focus)
    if not focuses:
        return None
    return max(focuses, key=lambda item: item.updated_at)


def _render_focus(focus: FocusFile, cap: int) -> str:
    sections = _split_sections(focus.body)
    priority = ["当前目标", "待验证"]
    priority.extend(name for name in FOCUS_H2 if name not in priority)
    lines: list[str] = []
    used = 0
    truncated = False
    for name in priority:
        content = sections.get(name, "").strip()
        heading_cost = estimate_tokens(f"## {name}")
        available = cap - used - heading_cost
        if available <= 1:
            truncated = truncated or bool(content)
            continue
        fitted, clipped = truncate_to_tokens(content, available)
        block = f"## {name}\n{fitted}".rstrip()
        cost = estimate_tokens(block) + 1
        if clipped:
            block += " …"
            cost = estimate_tokens(block) + 1
            truncated = True
        if used + cost > cap:
            truncated = True
            continue
        lines.append(block)
        used += cost
    if truncated:
        lines.append("…（L1 焦点已截断）")
    text = "\n\n".join(lines)
    if estimate_tokens(text) > cap:
        fitted, _ = truncate_to_tokens(text, cap)
        text = fitted + " …"
    return text


def _render_facets(
    global_vault: Vault | None,
    task_type: str,
    max_facets: int,
    cap: int,
) -> str:
    if global_vault is None or task_type == "other":
        return ""
    index = Index(global_vault.root)
    rows = index.layer_rows("L4", statuses=["verified"], limit=max_facets * 5)
    scored: list[tuple[float, str]] = []
    now = utc_now()
    for row in rows:
        applies_to = [str(item) for item in row.get("applies_to", [])]
        if task_type not in applies_to:
            continue
        try:
            age_days = max(0.0, (now - parse_iso(str(row["updated_at"]))).total_seconds() / 86400)
        except ValueError:
            age_days = 0.0
        recency = math.exp(-age_days / 30.0)
        scored.append((float(row["confidence"]) * recency, str(row["id"])))
    scored.sort(key=lambda item: item[0], reverse=True)
    lines: list[str] = []
    used = 0
    for _, node_id in scored[:max_facets]:
        try:
            node, _ = read_node(global_vault, node_id)
        except ZymorError:
            continue
        if node_stale_reasons(global_vault, node):
            continue
        guidance = _section_first_line(node.body, "行为指引") or node.summary
        applies = ", ".join(node.applies_to)
        line = f"- [verified] {node.title}：{guidance}（applies_to: {applies}）"
        cost = estimate_tokens(line) + 1
        if used + cost > cap:
            fitted, _ = truncate_to_tokens(line, max(1, cap - used - 1))
            line = fitted + " …"
            cost = estimate_tokens(line) + 1
        if used + cost > cap:
            break
        lines.append(line)
        used += cost
    return "\n".join(lines)


def _section_first_line(body: str, section: str) -> str:
    lines = body.splitlines()
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == f"## {section}":
            inside = True
            continue
        if inside and stripped.startswith("## "):
            return ""
        if inside and stripped:
            return stripped.lstrip("- ").strip()
    return ""


def _render_capsules(
    project_vault: Vault,
    project: str | None,
    max_capsules: int,
    cap: int,
) -> str:
    index = Index(project_vault.root)
    rows = index.layer_rows(
        "L2",
        statuses=["hypothesis", "verified", "disputed"],
        project=project,
        updated_since=days_ago_iso(CAPSULE_RECENT_DAYS),
        limit=max(max_capsules * 3, max_capsules),
    )
    lines: list[str] = []
    used = 0
    for row in rows:
        if len(lines) >= max_capsules:
            break
        try:
            capsule_node, _ = read_node(project_vault, str(row["id"]))
        except ZymorError:
            continue
        if node_stale_reasons(project_vault, capsule_node):
            continue
        label = str(row.get("project") or project or "项目")
        status = str(row.get("status") or "hypothesis")
        updated = str(row["updated_at"])[:10]
        summary, _ = truncate_to_tokens(str(row["summary"]), CAPSULE_LINE_TOKENS)
        line = f"- [{status}] {label}：{summary}（updated {updated}）"
        cost = estimate_tokens(line) + 1
        if used + cost > cap:
            break
        lines.append(line)
        used += cost
    return "\n".join(lines)


def _merged_counts(
    project_vault: Vault,
    global_vault: Vault | None,
) -> tuple[dict[str, Any], list[tuple[str, int]]]:
    indexes = [Index(project_vault.root)]
    if global_vault is not None:
        indexes.append(Index(global_vault.root))
    total = 0
    categories: dict[str, int] = {}
    tag_totals: dict[str, int] = {}
    for index in indexes:
        counts = index.counts()
        total += int(counts.get("total", 0))
        for category, count in counts.get("categories", {}).items():
            categories[category] = categories.get(category, 0) + int(count)
        for tag, count in index.tag_counts(limit=50):
            tag_totals[tag] = tag_totals.get(tag, 0) + count
    tags = sorted(tag_totals.items(), key=lambda item: (-item[1], item[0]))[:8]
    return {"total": total, "categories": categories}, tags


def _render_atlas_pointer(counts: dict[str, Any], tags: list[tuple[str, int]], cap: int) -> str:
    categories = counts.get("categories", {})
    category_text = " / ".join(
        f"{category} {categories.get(category, 0)}" for category in CATEGORIES
    )
    tag_text = ", ".join(f"{tag}({count})" for tag, count in tags) or "（无）"
    text = (
        f"categories: {category_text}；top tags: {tag_text}\n"
        "调用 memory_browse 查看目录，memory_search 做精确检索。"
    )
    if estimate_tokens(text) <= cap:
        return text
    for keep in range(len(tags) - 1, -1, -1):
        tag_text = ", ".join(f"{tag}({count})" for tag, count in tags[:keep]) or "（无）"
        text = (
            f"categories: {category_text}；top tags: {tag_text}\n"
            "调用 memory_browse 查看目录，memory_search 做精确检索。"
        )
        if estimate_tokens(text) <= cap:
            return text
    return truncate_to_tokens(text, cap)[0] + " …"


def _render_pending(
    items: list[dict[str, Any]],
    cap: int,
) -> str:
    lines: list[str] = []
    used = 0
    shown = 0
    for item in items:
        if shown >= PENDING_LIMIT:
            break
        question = str(item.get("question") or "").strip()
        new_id = str(item.get("new_id") or "")
        old_id = str(item.get("old_id") or "")
        entry_type = str(item.get("type") or "conflict")
        qid = str(item.get("qid") or item.get("id") or "")
        fitted, _ = truncate_to_tokens(question, PENDING_ITEM_TOKENS)
        if old_id:
            line = f"- Q{qid} · {fitted}（{new_id} vs {old_id}）"
        elif entry_type == "persona-proposal":
            line = f"- Q{qid} · {fitted}（人格规则待确认：{new_id}）"
        else:
            line = f"- Q{qid} · {fitted}"
        cost = estimate_tokens(line) + 1
        if used + cost > cap:
            break
        lines.append(line)
        used += cost
        shown += 1
    if len(items) > shown:
        lines.append(f"- 另有 {len(items) - shown} 条未决追问")
    return "\n".join(lines)


def _assemble(
    sections: list[tuple[str, str, int]],
    budget: int,
) -> tuple[str, dict[str, int], bool]:
    active = [(name, text, priority) for name, text, priority in sections if text]
    trimmed = False
    while estimate_tokens("\n\n".join(text for _, text, _ in active)) > budget:
        droppable = [item for item in active if item[2] > 1]
        if not droppable:
            break
        victim = max(droppable, key=lambda item: item[2])
        active.remove(victim)
        trimmed = True
    if trimmed:
        marker = TRUNCATION_MARKER.format(budget=budget)
        active.append(("truncated", marker, 0))
        while estimate_tokens("\n\n".join(text for _, text, _ in active)) > budget:
            droppable = [item for item in active if item[0] != "truncated" and item[2] > 1]
            if not droppable:
                break
            active.remove(max(droppable, key=lambda item: item[2]))
    text = "\n\n".join(text for _, text, _ in active)
    estimate = estimate_tokens(text)
    if estimate > budget:
        text, _ = truncate_to_tokens(text, budget)
        trimmed = True
        text = text + " …"
        estimate = estimate_tokens(text)
    section_tokens = {name: estimate_tokens(text_value) for name, text_value, _ in active}
    return text, section_tokens, trimmed


def memory_context(
    *,
    project_vault: Vault,
    config: Config,
    global_vault: Vault | None = None,
    task: str | None = None,
    project: str | None = None,
    budget_tokens: int | None = None,
    session_id: str | None = None,
    task_type: str | None = None,
    dissonance_provider: Callable[[Vault], list[dict[str, Any]]] | None = None,
) -> ContextResult:
    """入参：项目库/用户库、任务与预算。出参：Tier 0 常驻装配的 Markdown 与 token 估算。"""
    focus: FocusFile | None = None
    warnings: list[str] = []
    try:
        focus = _load_focus(project_vault, session_id)
    except ZymorError as error:
        warnings.append(f"L1 焦点读取失败: {error.message}")
    resolved_task_type = task_type
    if resolved_task_type is None and focus is not None:
        resolved_task_type = focus.task_type
    if resolved_task_type is None:
        resolved_task_type = task_type_from_text(task or "")
    budget = int(budget_tokens or config.get("context.total_tokens"))
    header = "## Zymor Memory"
    sections: list[tuple[str, str, int]] = [("header", header, 0)]

    try:
        l1_cap = int(config.get("context.l1_tokens"))
        if focus is not None:
            focus_text = _render_focus(focus, l1_cap)
            sections.append(("focus", f"### Current focus (L1)\n{focus_text}", 1))
        else:
            warnings.append("没有 active 焦点文件（L1）")

        facets_cap = int(config.get("context.l4_tokens"))
        facets_text = _render_facets(
            global_vault,
            resolved_task_type,
            int(config.get("context.max_facets")),
            facets_cap,
        )
        if facets_text:
            sections.append(("facets", f"### Persona facets (L4)\n{facets_text}", 2))

        capsules_cap = int(config.get("context.l2_tokens"))
        capsules_text = _render_capsules(
            project_vault,
            project,
            int(config.get("context.max_capsules")),
            capsules_cap,
        )
        if capsules_text:
            sections.append(("capsules", f"### Active capsules (L2)\n{capsules_text}", 3))

        atlas_cap = int(config.get("context.atlas_tokens"))
        counts, tags = _merged_counts(project_vault, global_vault)
        atlas_text = _render_atlas_pointer(counts, tags, atlas_cap)
        sections.append(("atlas", f"### Atlas pointers\n{atlas_text}", 4))

        pending_cap = int(config.get("context.pending_questions_tokens"))
        if dissonance_provider is not None:
            try:
                pending_items = dissonance_provider(project_vault)
            except Exception as error:
                pending_items = []
                warnings.append(f"追问队列读取失败: {error}")
            pending_text = _render_pending(list(pending_items), pending_cap)
            if pending_text:
                sections.append(("pending", f"### Pending questions\n{pending_text}", 5))
    except (IndexFailure, ZymorError) as error:
        warnings.append(f"记忆索引不可用: {error.message}")
        fallback = [header]
        if focus is not None:
            fallback.append(f"### Current focus (L1)\n{_render_focus(focus, l1_cap)}")
        fallback.append(f"> 记忆索引不可用：{error.message}。请运行 `zymor index sync` 后重试。")
        text = "\n\n".join(fallback)
        return ContextResult(
            text=text,
            tokens_estimate=estimate_tokens(text),
            section_tokens={"fallback": estimate_tokens(text)},
            task_type=resolved_task_type,
            degraded=True,
            warnings=warnings,
        )

    text, section_tokens, trimmed = _assemble(sections, budget)
    if trimmed:
        section_tokens["truncated"] = estimate_tokens(TRUNCATION_MARKER.format(budget=budget))
    return ContextResult(
        text=text,
        tokens_estimate=estimate_tokens(text),
        section_tokens=section_tokens,
        task_type=resolved_task_type,
        degraded=False,
        warnings=warnings,
    )
