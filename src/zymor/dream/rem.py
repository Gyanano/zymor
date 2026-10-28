from __future__ import annotations

import re
from typing import Any

from zymor.dream.state import Watermarks, log_action
from zymor.dream.types import DreamBudgetExceeded, DreamContext, DreamPlan, PhaseResult
from zymor.store.index import Index
from zymor.store.nodes import read_node, update_node
from zymor.tokenizer import build_match_query
from zymor.util.time import days_ago_iso, now_iso
from zymor.write_service import memory_write

STRUCTURE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{2,}|[A-Z0-9]{2,}[A-Za-z0-9_.\-]*")
RECENT_DAYS = 14
POOL_LIMIT = 200
INSIGHT_CONFIDENCE = 0.3
MAX_INSIGHT_TITLE = 60
MAX_INSIGHT_SUMMARY = 400


def _pool_rows(ctx: DreamContext) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    index = Index(ctx.vault.root)
    recent = [
        row
        for row in index.recent_rows(days_ago_iso(RECENT_DAYS), limit=POOL_LIMIT)
        if row["layer"] in {"L2", "L3"}
    ]
    distant = [row for row in index.layer_rows("L3", limit=POOL_LIMIT)]
    recent_ids = {row["id"] for row in recent}
    distant = [row for row in distant if row["id"] not in recent_ids]
    return recent, distant


def _structure_tokens(row: dict[str, Any]) -> set[str]:
    text = f"{row['title']} {row['summary']} {' '.join(row['tags'])}"
    return set(STRUCTURE_TOKEN_RE.findall(text))


def _shared_structure(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    shared_tags = set(a["tags"]) & set(b["tags"])
    if len(shared_tags) > 1:
        return []
    shared = _structure_tokens(a) & _structure_tokens(b)
    if not shared:
        return []
    if a["category"] == b["category"] and a["project"] == b["project"]:
        return []
    return sorted(shared)


def _candidate_pairs(ctx: DreamContext) -> list[list[dict[str, Any]]]:
    recent, distant = _pool_rows(ctx)
    scored: list[tuple[int, list[dict[str, Any]]]] = []
    seen: set[frozenset[str]] = set()
    for left in recent:
        for right in distant:
            key = frozenset({str(left["id"]), str(right["id"])})
            if len(key) < 2 or key in seen:
                continue
            shared = _shared_structure(left, right)
            if not shared:
                continue
            seen.add(key)
            scored.append((len(shared), [left, right]))
    scored.sort(key=lambda item: item[0], reverse=True)
    limit = int(ctx.config.get("dream.rem.max_pairs"))
    return [pair for _, pair in scored[: max(0, limit)]]


def _insight_prompt(pair: list[dict[str, Any]]) -> list[dict[str, str]]:
    left, right = pair
    return [
        {
            "role": "system",
            "content": (
                "你是记忆联想器。只判断两个记忆节点是否共享可观察结构：实体名、错误签名、"
                "API 名、用户确认过的类比、同一失败模式。不同 tags 交集必须 ≤1；禁止随机联想。"
                "只输出 JSON 对象："
                '{"isomorphic": bool, "relation": str, "insight": str, '
                '"evidence": [节点 id], "confidence": 0.0}'
            ),
        },
        {
            "role": "user",
            "content": (
                f"节点 A：{left['id']} · {left['title']} · {left['summary']} · "
                f"tags={left['tags']} · category={left['category']} · project={left['project']}\n"
                f"节点 B：{right['id']} · {right['title']} · {right['summary']} · "
                f"tags={right['tags']} · category={right['category']} · "
                f"project={right['project']}\n"
                "若同构，insight 写一句跨域可复用的结论；evidence 只填输入节点 id 或已存在记忆 id。"
            ),
        },
    ]


def _parse_insight(parsed: Any) -> dict[str, Any] | None:
    if not isinstance(parsed, dict):
        return None
    if not isinstance(parsed.get("isomorphic"), bool):
        return None
    insight = parsed.get("insight")
    if parsed["isomorphic"] and (not isinstance(insight, str) or not insight.strip()):
        return None
    evidence = parsed.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    confidence = parsed.get("confidence")
    if not isinstance(confidence, (int, float)):
        confidence = INSIGHT_CONFIDENCE
    relation = parsed.get("relation")
    return {
        "isomorphic": bool(parsed["isomorphic"]),
        "insight": str(insight or "").strip(),
        "relation": str(relation or "").strip(),
        "evidence": [str(item) for item in evidence if isinstance(item, str)],
        "confidence": max(0.0, min(float(confidence), 1.0)),
    }


def _insight_body(insight: str, relation: str, pair: list[dict[str, Any]]) -> str:
    left, right = pair
    lines = [
        "## 结论",
        insight,
        "",
        "## 证据",
        f"同构依据：{relation or '共享可观察结构'}",
        f"- [[{left['id']}]]：{left['title']}",
        f"- [[{right['id']}]]：{right['title']}",
        "",
        "## 关联",
        f"[[{left['id']}]]",
        f"[[{right['id']}]]",
    ]
    return "\n".join(lines)


def _find_duplicate_insight(ctx: DreamContext, insight: str) -> str | None:
    index = Index(ctx.vault.root)
    rows = index.candidate_rows(build_match_query(insight[:80]), limit=10)
    wanted = set(insight.lower().split())
    for row in rows:
        if row["layer"] != "L3" or row["status"] == "deprecated":
            continue
        candidate = set(str(row["summary"]).lower().split())
        if not candidate or not wanted:
            continue
        overlap = len(wanted & candidate) / max(1, len(wanted | candidate))
        if overlap >= 0.5:
            return str(row["id"])
    return None


def _append_link(body: str, node_id: str) -> str:
    if f"[[{node_id}]]" in body:
        return body
    lines = body.rstrip("\n").split("\n")
    if "## 关联" in lines:
        index = lines.index("## 关联")
        insert_at = len(lines)
        for position in range(index + 1, len(lines)):
            if lines[position].startswith("## "):
                insert_at = position
                break
        lines.insert(insert_at, f"[[{node_id}]]")
    else:
        lines.extend(["", "## 关联", f"[[{node_id}]]"])
    return "\n".join(lines) + "\n"


def _link_sources(ctx: DreamContext, pair: list[dict[str, Any]], new_id: str) -> int:
    updated = 0
    for row in pair:
        try:
            node, _ = read_node(ctx.vault, str(row["id"]))
        except Exception as error:
            ctx.warn(f"REM 链接源节点失败 {row['id']}: {error}")
            continue
        new_body = _append_link(node.body, new_id)
        if new_body == node.body:
            continue
        try:
            update_node(ctx.vault, node.id, {"body": new_body}, command="dream:rem")
            ctx.budget.record_write()
            updated += 1
        except Exception as error:
            ctx.warn(f"REM 更新源节点失败 {node.id}: {error}")
    return updated


def _write_insight(
    ctx: DreamContext,
    pair: list[dict[str, Any]],
    parsed: dict[str, Any],
) -> tuple[str | None, bool, bool]:
    insight = parsed["insight"]
    title = insight.split("\n")[0].strip()[:MAX_INSIGHT_TITLE] or "跨域同构洞察"
    summary = insight[:MAX_INSIGHT_SUMMARY]
    duplicate = _find_duplicate_insight(ctx, insight)
    if duplicate is not None:
        try:
            node, _ = read_node(ctx.vault, duplicate)
            evidence = list(dict.fromkeys([*node.evidence, *(item["id"] for item in pair)]))
            update_node(
                ctx.vault,
                duplicate,
                {"evidence": evidence, "updated_at": now_iso()},
                command="dream:rem",
            )
            ctx.budget.record_write()
            return duplicate, False, False
        except Exception as error:
            ctx.warn(f"REM 追加洞察证据失败 {duplicate}: {error}")
            return None, False, False
    if not ctx.budget.can_write():
        ctx.budget.stopped_reason = ctx.budget.exhausted() or "节点预算不足"
        return None, False, True
    body = _insight_body(summary, parsed["relation"], pair)
    try:
        outcome = memory_write(
            ctx.vault,
            ctx.config,
            title=title,
            summary=summary,
            body=body,
            category="semantic",
            layer="L3",
            session_id=ctx.run_id,
            source=f"dream:{ctx.run_id}",
            created_by="dream",
            status="hypothesis",
            confidence=INSIGHT_CONFIDENCE,
            evidence=[str(item["id"]) for item in pair],
            client=None,
            command="dream:rem",
        )
    except Exception as error:
        ctx.warn(f"REM 写入洞察失败: {error}")
        return None, False, False
    ctx.budget.record_write()
    return outcome.node_id, True, False


def run_rem(ctx: DreamContext, plan: DreamPlan, watermarks: Watermarks) -> PhaseResult:
    """入参：做梦上下文、计划与水位线。出参：REM 联想与人格提炼的阶段结果。按 §6 抽样并产出洞察。"""
    result = PhaseResult(phase="rem")
    if not bool(ctx.config.get("dream.rem.enabled")):
        result.warnings.append("REM 已关闭（dream.rem.enabled=false）")
        return result
    client = ctx.client("dream")
    if client is None:
        result.warnings.append("未配置 dream 模型，跳过 REM 联想")
        return result
    pairs = _candidate_pairs(ctx)
    result.candidates = len(pairs)
    result.details["pairs"] = []
    result.details["known_limits"] = ["persona-confirmation-queue: not-implemented"]
    for pair in pairs:
        if ctx.budget.exhausted() is not None:
            result.details["stopped"] = ctx.budget.exhausted()
            break
        detail: dict[str, Any] = {
            "a": pair[0]["id"],
            "b": pair[1]["id"],
            "shared": _shared_structure(pair[0], pair[1]),
        }
        try:
            parsed, _response = client.complete_json(
                _insight_prompt(pair), caller=f"dream:{ctx.run_id}:rem"
            )
        except DreamBudgetExceeded as error:
            result.details["stopped"] = str(error.message)
            result.warnings.append(str(error.message))
            break
        except Exception as error:
            result.warnings.append(f"REM 判定失败: {error}")
            detail["error"] = str(error)
            result.details["pairs"].append(detail)
            result.skipped += 1
            continue
        insight = _parse_insight(parsed)
        if insight is None:
            result.warnings.append("REM 判定输出不合法，已跳过该对")
            result.skipped += 1
            result.details["pairs"].append(detail)
            continue
        detail["isomorphic"] = insight["isomorphic"]
        if not insight["isomorphic"]:
            result.skipped += 1
            result.details["pairs"].append(detail)
            log_action(
                ctx.vault,
                ctx.run_id,
                "rem",
                "not-isomorphic",
                details={"a": detail["a"], "b": detail["b"]},
            )
            continue
        new_id, created, stopped = _write_insight(ctx, pair, insight)
        if stopped:
            result.details["stopped"] = ctx.budget.stopped_reason or "预算不足"
            break
        if new_id is None:
            result.skipped += 1
            result.details["pairs"].append(detail)
            continue
        detail["insight_id"] = new_id
        detail["appended"] = not created
        detail["links_added"] = _link_sources(ctx, pair, new_id)
        if created:
            result.nodes_created += 1
        else:
            result.nodes_updated += 1
        result.nodes_updated += int(detail["links_added"])
        result.details["pairs"].append(detail)
        log_action(
            ctx.vault,
            ctx.run_id,
            "rem",
            "insight",
            node_id=new_id,
            details={"a": detail["a"], "b": detail["b"], "shared": detail["shared"]},
        )
    persona = _run_persona(ctx, plan, watermarks)
    if persona is not None:
        result.warnings.extend(persona.warnings)
        result.errors.extend(persona.errors)
        result.nodes_created += persona.nodes_created
        result.nodes_updated += persona.nodes_updated
        result.candidates += persona.candidates
        result.skipped += persona.skipped
        result.details["persona"] = persona.to_dict()
    return result


def _run_persona(ctx: DreamContext, plan: DreamPlan, watermarks: Watermarks) -> PhaseResult | None:
    try:
        from zymor.dream.persona import run_persona
    except ImportError:
        return PhaseResult(phase="persona", warnings=["人格提炼在 M3 后续实现"])
    try:
        return run_persona(ctx, plan, watermarks)
    except DreamBudgetExceeded as error:
        return PhaseResult(phase="persona", warnings=[str(error.message)])
    except Exception as error:
        return PhaseResult(phase="persona", errors=[f"人格提炼失败: {error}"])
