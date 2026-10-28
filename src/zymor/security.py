from __future__ import annotations

import re

TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")),
    ("github-token", re.compile(r"\bghp_[A-Za-z0-9]{16,}\b")),
    ("github-token", re.compile(r"\bgho_[A-Za-z0-9]{16,}\b")),
    ("github-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
)

ASSIGNMENT_VALUE = r"(?:['\"]([^'\"\n]{2,512})['\"]|([^\s'\"&,;]{2,128}))"

ASSIGNMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "password",
        re.compile(r"(?i)\b(?:password|passwd|pwd)\s*[=:]\s*" + ASSIGNMENT_VALUE),
    ),
    (
        "api-key",
        re.compile(
            r"(?i)\b(?:api[_-]?key|apikey|secret[_-]?key|client[_-]?secret)"
            r"\s*[=:]\s*" + ASSIGNMENT_VALUE
        ),
    ),
    (
        "access-token",
        re.compile(
            r"(?i)\b(?:access[_-]?token|auth[_-]?token|refresh[_-]?token)"
            r"\s*[=:]\s*" + ASSIGNMENT_VALUE
        ),
    ),
)


def detect_sensitive(text: str) -> list[str]:
    """入参：任意文本。出参：命中的敏感信息类别列表（去重、按规则顺序）。扫描密钥与口令。"""
    if not text:
        return []
    labels: list[str] = []
    for label, pattern in TOKEN_PATTERNS:
        if pattern.search(text) is not None and label not in labels:
            labels.append(label)
    for label, pattern in ASSIGNMENT_PATTERNS:
        match = pattern.search(text)
        if match is not None and _assignment_value(match) is not None and label not in labels:
            labels.append(label)
    return labels


def redact(text: str) -> str:
    """入参：任意文本。出参：敏感串被替换为 `***` 的文本。用于工具返回前脱敏。"""
    if not text:
        return text
    result = text
    for _label, pattern in TOKEN_PATTERNS:
        result = pattern.sub("***", result)
    for _label, pattern in ASSIGNMENT_PATTERNS:
        result = pattern.sub(_redact_assignment, result)
    return result


def _assignment_value(match: re.Match[str]) -> tuple[int, str] | None:
    for group in (1, 2):
        value = match.group(group)
        if value:
            return match.start(group) - match.start(0), value
    return None


def _redact_assignment(match: re.Match[str]) -> str:
    found = _assignment_value(match)
    whole = match.group(0)
    if found is None:
        return whole
    offset, value = found
    return f"{whole[:offset]}***{whole[offset + len(value) :]}"
