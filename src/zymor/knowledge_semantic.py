from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from zymor.errors import NotFoundError, ValidationError
from zymor.knowledge import EvidenceReference, KnowledgeStore
from zymor.util.fs import sha256_hex

_TOPIC = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
_SPLIT = re.compile(r"将(?:复杂的)?(?:内容|任务)分解为(?:较|更)小的(?:单元|步骤)")
_QUEUE = {
    "教学首晚": re.compile(r"第一晚是教学夜，整晚同一时间只接待([一二两三四])位"),
    "普通营业第二晚开始时": re.compile(r"普通营业从第二晚开始能同时接待([一二两三四])位"),
    "无尽模式起步": re.compile(r"无尽模式第二晚解锁，起步就能同时接待([一二两三四])位"),
}
_MAINTENANCE = re.compile(
    r"^(?P<subject>[\w -]{1,30}（[^）\n]{1,20}）)(?:每(?:周|星期)(?P<a>[一二三四五六日天])维护"
    r"|的维护日是(?:周|星期)(?P<b>[一二三四五六日天]))[。.!]?$"
)


@dataclass(frozen=True)
class Claim:
    subject: str
    scope: str
    value: str
    partition: str = ""


def _claims(text: str, source_key: str) -> list[Claim]:
    claims = []
    lines = [line.strip().removeprefix("- ").removeprefix("> ") for line in text.splitlines()]
    if any(_SPLIT.match(line) for line in lines):
        claims.append(Claim("复杂内容或任务的拆分方式", "界面内容或任务组织", "较小的单元或步骤"))
    for scope, pattern in _QUEUE.items():
        if match := pattern.search(text):
            number = match[1].replace("二", "两")
            claims.append(Claim("游戏同时接待人数", scope, f"{number}位", source_key))
    for line in lines:
        if match := _MAINTENANCE.fullmatch(line):
            day = (match["a"] or match["b"]).replace("天", "日")
            claims.append(Claim(match["subject"], "每周例行维护日", f"周{day}"))
    return claims


def propose_semantic(
    store: KnowledgeStore, item_prefix: str, query: str
) -> list[dict[str, int | str]]:
    """在指定主题中归一已支持的表述，并生成待审提案。"""
    if not _TOPIC.fullmatch(item_prefix):
        raise ValidationError("主题标识须为不超过 48 位的小写字母、数字或连字符")
    groups: defaultdict[tuple[str, str, str], dict[str, list[EvidenceReference]]] = defaultdict(
        dict
    )
    for hit in store.search_sources(query, limit=None):
        reference = EvidenceReference(hit["source_id"], hit["locator"], hit["text"])
        for claim in _claims(reference.quote, hit["source_key"]):
            refs = groups[claim.subject, claim.scope, claim.partition].setdefault(claim.value, [])
            if reference not in refs:
                refs.append(reference)
    if not groups:
        raise NotFoundError("搜索结果中没有找到试点支持的表述")

    store.initialize()
    results: list[dict[str, int | str]] = []
    for (subject, scope, partition), alternatives in sorted(groups.items()):
        title = f"{subject}（{scope}）"
        suffix = sha256_hex(f"{title}\n{partition}")[:12]
        item_id = f"{item_prefix}-{suffix}"
        conflict = len(alternatives) > 1
        values = "／".join(sorted(alternatives))
        statement = (
            f"{title}存在不同口径，{values}，待核对。" if conflict else f"{title}为{values}。"
        )
        refs = list(dict.fromkeys(ref for sources in alternatives.values() for ref in sources))
        proposal_id = store.propose(
            item_id,
            title=title,
            statement=statement,
            source_id=refs[0].source_id,
            evidence=refs[0].quote,
            references=refs,
            alternatives=alternatives if conflict else None,
        )
        results.append(
            {
                "proposal_id": proposal_id,
                "item_id": item_id,
                "status": "pending_conflict" if conflict else "pending",
                "scope": scope,
            }
        )
    return results
