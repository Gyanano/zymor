from __future__ import annotations

import re

CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2EBEF),
    (0x30000, 0x3134F),
)

ASCII_CHAR_RE = re.compile(r"[A-Za-z0-9_]")
ASCII_TERM_RE = re.compile(r"[A-Za-z0-9_]+")


def identifier_parts(term: str) -> list[str]:
    """入参：ASCII 词项。出参：snake_case/camelCase 子词；无拆分时为空。"""
    if "_" not in term and not re.search(r"[a-z][A-Z]", term):
        return []
    chunks = [chunk for chunk in term.split("_") if chunk]
    parts: list[str] = []
    for chunk in chunks:
        if not chunk.isalpha():
            continue
        for part in _camel_parts(chunk):
            lowered = part.lower()
            if len(lowered) >= 2 and lowered not in parts:
                parts.append(lowered)
    return parts if len(parts) > 1 else []


def _camel_parts(chunk: str) -> list[str]:
    """入参：字母串。出参：camelCase 拆分结果。手工扫描避免多项式回溯正则。"""
    parts: list[str] = []
    index = 0
    size = len(chunk)
    while index < size:
        if chunk[index].isupper():
            end = index
            while end < size and chunk[end].isupper():
                end += 1
            if end - index > 1 and end < size and chunk[end].islower():
                parts.append(chunk[index : end - 1])
                index = end - 1
                continue
            if end - index == 1 and end < size and chunk[end].islower():
                while end < size and chunk[end].islower():
                    end += 1
                parts.append(chunk[index:end])
                index = end
                continue
            parts.append(chunk[index:end])
            index = end
            continue
        end = index
        while end < size and chunk[end].islower():
            end += 1
        parts.append(chunk[index:end])
        index = end
    return parts


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in CJK_RANGES)


def tokenize_terms(text: str) -> list[str]:
    """入参：混排文本。出参：按出现顺序排列的词项（CJK 重叠 bigram，ASCII 连写原样）。"""
    terms: list[str] = []
    cjk_run: list[str] = []
    ascii_run: list[str] = []

    def flush_cjk() -> None:
        if not cjk_run:
            return
        run = "".join(cjk_run)
        cjk_run.clear()
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[index : index + 2] for index in range(len(run) - 1))

    def flush_ascii() -> None:
        if not ascii_run:
            return
        term = "".join(ascii_run)
        ascii_run.clear()
        terms.append(term)
        terms.extend(identifier_parts(term))

    for char in text:
        if _is_cjk(char):
            flush_ascii()
            cjk_run.append(char)
        elif ASCII_CHAR_RE.fullmatch(char):
            flush_cjk()
            ascii_run.append(char)
        else:
            flush_cjk()
            flush_ascii()
    flush_cjk()
    flush_ascii()
    return terms


def tokenize_text(text: str) -> str:
    """入参：原文。出参：空格连接的索引词项串。把 CJK 串切成重叠 bigram。"""
    return " ".join(tokenize_terms(text))


def build_match_query(query: str) -> str:
    """入参：用户查询。出参：FTS5 MATCH 表达式；标识符同时匹配原词与子词。"""
    terms = tokenize_terms(query)
    covered: set[str] = set()
    for term in terms:
        covered.update(identifier_parts(term))
    groups: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        parts = identifier_parts(term)
        if not parts and term in covered:
            continue
        if parts:
            alternatives = " AND ".join(f'"{part}"' for part in parts)
            groups.append(f'("{term}" OR ({alternatives}))')
        else:
            groups.append(f'"{term}"')
    return " AND ".join(groups)


def estimate_tokens(text: str) -> int:
    """入参：文本。出参：token 估算（CJK 1 字 1，非 CJK 每 4 字符 1，向上取整）。"""
    cjk = sum(1 for char in text if _is_cjk(char))
    other = sum(1 for char in text if not _is_cjk(char) and not char.isspace())
    return cjk + (other + 3) // 4


def truncate_to_tokens(text: str, max_tokens: int) -> tuple[str, bool]:
    """入参：文本与 token 上限。出参：截断后的文本与是否发生截断。按行累积估算。"""
    if max_tokens <= 0:
        return "", bool(text.strip())
    lines = text.split("\n")
    kept: list[str] = []
    total = 0
    for line in lines:
        cost = estimate_tokens(line) + (1 if line else 0)
        if total + cost > max_tokens and kept:
            return "\n".join(kept), True
        kept.append(line)
        total += cost
    return text, False
