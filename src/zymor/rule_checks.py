from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any

from zymor.errors import ZymorError
from zymor.invalidation import stale_reasons
from zymor.store.audit import append_audit
from zymor.store.index import Index
from zymor.store.nodes import read_node
from zymor.util.time import now_iso
from zymor.vault import Vault

CHECK_KINDS = ("contains", "absent", "regex")
CHECK_SECTION = "检查"
MAX_CHECK_LINE = 300


@dataclass
class RuleCheckDiagnostic:
    rule_id: str
    title: str
    path: str
    kind: str
    value: str
    passed: bool
    message: str = ""
    repair: str = ""

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：可序列化的检查诊断。"""
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "path": self.path,
            "kind": self.kind,
            "value": self.value,
            "passed": self.passed,
            "message": self.message,
            "repair": self.repair,
        }


@dataclass
class RuleCheckReport:
    diagnostics: list[RuleCheckDiagnostic] = field(default_factory=list)
    checked_rules: int = 0
    skipped_rules: int = 0
    skipped_checks: int = 0
    unmatched: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    attempt: int = 1
    max_attempts: int = 3

    @property
    def passed(self) -> bool:
        """入参：无。出参：是否全部检查通过。"""
        return all(item.passed for item in self.diagnostics)

    @property
    def next_action(self) -> str:
        """入参：无。出参：pass、repair 或 stop。"""
        if self.passed:
            return "pass"
        return "repair" if self.attempt < self.max_attempts else "stop"

    @property
    def repair_instructions(self) -> list[str]:
        """入参：无。出参：去重后的修复提示。"""
        return list(
            dict.fromkeys(
                item.repair for item in self.diagnostics if not item.passed and item.repair
            )
        )

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：可序列化报告。"""
        failures = [item for item in self.diagnostics if not item.passed]
        failure_dicts = [item.to_dict() for item in failures]
        return {
            "passed": self.passed,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "next_action": self.next_action,
            "checked_rules": self.checked_rules,
            "skipped_rules": self.skipped_rules,
            "skipped_checks": self.skipped_checks,
            "unmatched": self.unmatched,
            "checks": len(self.diagnostics),
            "failures": failure_dicts,
            "violations": failure_dicts,
            "repair_instructions": self.repair_instructions,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "warnings": self.warnings,
        }


def parse_checks(body: str) -> list[tuple[str, str, str]]:
    """入参：节点正文。出参：`## 检查` 小节里的 (glob, kind, value)。"""
    lines = body.splitlines()
    inside = False
    checks: list[tuple[str, str, str]] = []
    for line in lines:
        stripped = line.strip()
        if stripped == f"## {CHECK_SECTION}":
            inside = True
            continue
        if inside and stripped.startswith("## "):
            break
        if not inside or not stripped:
            continue
        if len(stripped) > MAX_CHECK_LINE:
            checks.append(("<invalid>", "<invalid>", "检查行超过长度上限"))
            continue
        parts = [part.strip() for part in stripped.lstrip("- ").split("|")]
        if len(parts) != 3 or not all(parts):
            checks.append(("<invalid>", "<invalid>", f"无法解析检查行: {stripped}"))
            continue
        checks.append((parts[0], parts[1], parts[2]))
    return checks


def _match_files(files: dict[str, str], pattern: str) -> list[tuple[str, str]]:
    normalized = pattern.replace("\\", "/")
    return [
        (path, text)
        for path, text in files.items()
        if fnmatch.fnmatch(path.replace("\\", "/"), normalized)
    ]


def check_rules(
    scopes: list[tuple[str, Vault]],
    files: dict[str, str],
    *,
    task_type: str | None = None,
    attempt: int = 1,
    max_attempts: int = 3,
) -> RuleCheckReport:
    """入参：scope、文件内容映射、任务类型与尝试次数。出参：verified L3 规则的检查报告。"""
    report = RuleCheckReport(attempt=max(1, attempt), max_attempts=max(1, max_attempts))
    seen: set[str] = set()
    for _label, vault in scopes:
        rows = Index(vault.root).layer_rows("L3", statuses=["verified"], limit=5000)
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
            if stale_reasons(vault, node):
                continue
            checks = parse_checks(node.body)
            if not checks:
                report.skipped_rules += 1
                continue
            seen.add(node_id)
            report.checked_rules += 1
            for pattern, kind, value in checks:
                if kind not in CHECK_KINDS:
                    message = f"未知检查类型: {kind}"
                    report.diagnostics.append(
                        RuleCheckDiagnostic(
                            node.id,
                            node.title,
                            pattern,
                            kind,
                            value,
                            False,
                            message,
                            f"修正规则 {node.title} 的检查类型：{message}",
                        )
                    )
                    continue
                matched = _match_files(files, pattern)
                if not matched:
                    report.skipped_checks += 1
                    report.unmatched.append(
                        {
                            "rule_id": node.id,
                            "title": node.title,
                            "pattern": pattern,
                            "message": "没有匹配的文件",
                        }
                    )
                    continue
                for path, text in matched:
                    passed, message = _evaluate(path, text, kind, value)
                    repair = "" if passed else _repair_instruction(path, kind, value, message)
                    report.diagnostics.append(
                        RuleCheckDiagnostic(
                            node.id, node.title, path, kind, value, passed, message, repair
                        )
                    )
    return report


def record_rule_check(
    vault: Vault,
    report: RuleCheckReport,
    *,
    session_id: str | None = None,
    task_type: str | None = None,
    paths: list[str] | None = None,
) -> None:
    """入参：vault、报告、session、任务类型与检查路径。出参：无。追加规则检查审计。"""
    append_audit(
        vault,
        "rules.jsonl",
        {
            "ts": now_iso(),
            "kind": "rule_check",
            "session_id": session_id,
            "task_type": task_type,
            "attempt": report.attempt,
            "max_attempts": report.max_attempts,
            "passed": report.passed,
            "next_action": report.next_action,
            "checked_rules": report.checked_rules,
            "skipped_rules": report.skipped_rules,
            "skipped_checks": report.skipped_checks,
            "unmatched": report.unmatched,
            "checks": len(report.diagnostics),
            "paths": paths or [],
            "failures": [item.to_dict() for item in report.diagnostics if not item.passed],
        },
    )


def _repair_instruction(path: str, kind: str, value: str, message: str) -> str:
    if path == "<invalid>":
        return f"修正规则检查定义：{message}"
    if kind == "contains":
        return f"在 {path} 中加入 {value!r}"
    if kind == "absent":
        return f"从 {path} 中移除 {value!r}"
    if kind == "regex":
        return f"让 {path} 匹配正则 {value!r}"
    return f"修正 {path} 的检查：{message}"


def _evaluate(path: str, text: str, kind: str, value: str) -> tuple[bool, str]:
    if kind == "contains":
        passed = value in text
        return passed, "" if passed else f"{path} 缺少 {value!r}"
    if kind == "absent":
        passed = value not in text
        return passed, "" if passed else f"{path} 不应包含 {value!r}"
    try:
        passed = re.search(value, text) is not None
    except re.error as error:
        return False, f"正则无效: {error}"
    return passed, "" if passed else f"{path} 不匹配正则 {value!r}"
