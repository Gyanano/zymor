from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zymor.vault import Vault


def append_audit(vault: Vault, name: str, record: dict[str, Any]) -> None:
    """入参：vault、审计文件名（writes/retrieval）、记录。出参：无。追加一行 JSON 审计。"""
    path = vault.audit_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")


def read_audit(path: Path) -> list[dict[str, Any]]:
    """入参：审计文件路径。出参：可解析的记录列表（跳过坏行）。"""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records
