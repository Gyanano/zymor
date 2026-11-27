from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from zymor.util.fs import sha256_hex

_SPLIT = re.compile(
    r"将(?:复杂的)?(?:内容|任务)分解为(?:较|更)小的(?:单元|步骤)"
    r"(?:[，,](?:以便于用户更容易地处理、理解和记忆|以减少认知负担))?"
    r"(?:[，,]仅适用于(?P<condition>[\u4e00-\u9fffA-Za-z0-9 _-]{1,24}))?[。.]?"
)
_QUEUE = {
    "教学首晚": re.compile(r"第一晚是教学夜，整晚同一时间只接待([一二两三四])位[。.]?"),
    "普通营业第二晚开始时": re.compile(
        r"普通营业从第二晚开始能同时接待([一二两三四])位"
        r"(?:，第四晚增至[一二两三四]位，第八晚起最多[一二两三四]位)?[。.]?"
    ),
    "无尽模式起步": re.compile(r"无尽模式第二晚解锁，起步就能同时接待([一二两三四])位[。.]?"),
}
_MAINTENANCE = re.compile(
    r"(?P<subject>[\w -]{1,30}（[^）\n]{1,20}）)(?:每(?:周|星期)(?P<a>[一二三四五六日天])维护"
    r"|的维护日是(?:周|星期)(?P<b>[一二三四五六日天]))[。.]?"
)
_SENTENCE_BREAK = re.compile(r"(?<=[。！？!?])")
_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_CONDITION_HEADING = re.compile(
    r"(?:模式|版本|阶段|场景|条件|范围|限制)(?:$|[：:])|^规则\s*v\d", re.IGNORECASE
)
_CONTEXT_CONDITION = re.compile(
    r"^(?:(?:此|该)(?:规则|建议|方法|结论|做法))?"
    r"(?:适用(?:条件|范围|对象|场景|前提|于)|仅适用于|只适用于|只限|仅在|只在|前提|例外|"
    r"(?:生效|使用)?条件[：:]|在.{1,24}(?:下|时)(?:$|[，,：:])|"
    r"(?:当|如果|若).{1,35}(?:时|[，,：:])|.{1,24}(?:不适用|才适用|才生效)[。.]?$)"
)


@dataclass(frozen=True, order=True)
class SemanticSlot:
    subject: str
    scope: str
    partition: str = ""

    @property
    def title(self) -> str:
        return f"{self.subject}（{self.scope}）"

    @property
    def key(self) -> str:
        return slot_key(self.title, self.partition)


@dataclass(frozen=True)
class Claim:
    slot: SemanticSlot
    value: str
    quote: str


def slot_key(title: str, partition: str = "") -> str:
    return sha256_hex(f"{title}\n{partition}")[:12]


def _lines(text: str) -> list[str]:
    return [
        line.strip().removeprefix("- ").removeprefix("> ")
        for line in text.splitlines()
        if line.strip()
    ]


def _has_condition(text: str) -> bool:
    return any(
        _CONTEXT_CONDITION.match(sentence.strip(" *"))
        for line in _lines(text)
        for sentence in _SENTENCE_BREAK.split(line)
    )


def extract_claims(
    fragments: Sequence[str], source_key: str, *, markdown: bool
) -> list[tuple[int, Claim]]:
    """只提炼明确支持的表述，保留可定位的单句原文。"""
    claims = []
    scoped_headings: list[int] = []
    contextual_conditions = [_has_condition(text) for text in fragments]
    for index, text in enumerate(fragments):
        scoped = bool(scoped_headings)
        if markdown:
            for line in text.splitlines():
                if heading := _HEADING.fullmatch(line.strip()):
                    level = len(heading[1])
                    scoped_headings = [depth for depth in scoped_headings if depth < level]
                    if _CONDITION_HEADING.search(heading[2]):
                        scoped_headings.append(level)
                    scoped |= bool(scoped_headings)
        adjacent_conditions = (
            contextual_conditions[max(0, index - 1) : index + 2]
            if markdown
            else contextual_conditions[index : index + 1]
        )
        if scoped or any(adjacent_conditions):
            continue
        for line in _lines(text):
            if match := _SPLIT.fullmatch(line):
                condition = (match["condition"] or "").strip()
                scope = f"仅适用于{condition}" if condition else "界面内容或任务组织"
                slot = SemanticSlot("复杂内容或任务的拆分方式", scope)
                claims.append((index, Claim(slot, "较小的单元或步骤", line)))
            if match := _MAINTENANCE.fullmatch(line):
                day = (match["a"] or match["b"]).replace("天", "日")
                slot = SemanticSlot(match["subject"], "每周例行维护日")
                claims.append((index, Claim(slot, f"周{day}", line)))
            for sentence in _SENTENCE_BREAK.split(line):
                quote = sentence.strip()
                for scope, pattern in _QUEUE.items():
                    if match := pattern.fullmatch(quote):
                        slot = SemanticSlot("游戏同时接待人数", scope, source_key)
                        number = match[1].replace("二", "两")
                        claims.append((index, Claim(slot, f"{number}位", quote)))
    return claims


def statement_for(title: str, values: Sequence[str]) -> str:
    if len(values) == 1:
        return f"{title}为{values[0]}。"
    return f"{title}存在不同口径，{'／'.join(sorted(values))}，待核对。"
