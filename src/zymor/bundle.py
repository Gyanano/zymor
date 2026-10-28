from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from zymor.errors import UsageError, ValidationError
from zymor.model.node import MemoryNode
from zymor.security import detect_sensitive
from zymor.store.nodes import check_layer_vault, write_node
from zymor.util.time import now_iso
from zymor.vault import Vault

BUNDLE_FORMAT = "zymor-bundle"
BUNDLE_VERSION = 1
_EXPORT_MODES = ("skip", "overwrite")


def export_bundle(vault: Vault, output: Path) -> dict[str, Any]:
    """入参：vault 与输出目录。出参：导出统计。导出一个可校验的节点 bundle。"""
    if output.exists() and not output.is_dir():
        raise UsageError(f"输出路径不是目录: {output}")
    if output.exists() and any(output.iterdir()):
        raise UsageError(f"输出目录非空: {output}", hint="换一个空目录或先清理")
    output.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, str]] = []
    for path in sorted(vault.nodes_dir.rglob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ValidationError(f"导出节点不可读: {path}: {error}", field="nodes") from error
        try:
            node = MemoryNode.from_markdown(text)
        except Exception as error:
            raise ValidationError(f"导出节点无法解析: {path}: {error}", field="nodes") from error
        records.append({"id": node.id, "markdown": text})
    nodes_text = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records
    )
    nodes_path = output / "nodes.jsonl"
    nodes_path.write_text(nodes_text, encoding="utf-8", newline="\n")
    manifest = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "exported_at": now_iso(),
        "vault_kind": vault.info.kind,
        "vault_name": vault.info.name,
        "node_count": len(records),
        "nodes_sha256": sha256(nodes_text.encode("utf-8")).hexdigest(),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return {"path": str(output), **manifest}


def import_bundle(vault: Vault, source: Path, *, mode: str = "skip") -> dict[str, Any]:
    """入参：vault、bundle 目录与冲突模式。出参：导入统计。"""
    if mode not in _EXPORT_MODES:
        raise ValidationError(f"未知导入模式: {mode}", field="mode", hint="skip 或 overwrite")
    manifest_path = source / "manifest.json"
    nodes_path = source / "nodes.jsonl"
    if not manifest_path.is_file() or not nodes_path.is_file():
        raise ValidationError(f"不是完整 bundle: {source}", field="source")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"manifest 无法解析: {error}", field="manifest") from error
    if manifest.get("format") != BUNDLE_FORMAT or manifest.get("version") != BUNDLE_VERSION:
        raise ValidationError("bundle 格式或版本不兼容", field="manifest")
    nodes_text = nodes_path.read_text(encoding="utf-8")
    if manifest.get("nodes_sha256") != sha256(nodes_text.encode("utf-8")).hexdigest():
        raise ValidationError("bundle 校验和不匹配", field="manifest")
    nodes: list[MemoryNode] = []
    seen: set[str] = set()
    for line in nodes_text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            markdown = record["markdown"]
            if not isinstance(markdown, str):
                raise ValueError("markdown 必须是字符串")
            node = MemoryNode.from_markdown(markdown)
        except Exception as error:
            raise ValidationError(f"bundle 节点无法解析: {error}", field="nodes") from error
        if node.id in seen:
            raise ValidationError(f"bundle 含重复节点 id: {node.id}", field="nodes")
        seen.add(node.id)
        check_layer_vault(node, vault)
        labels = detect_sensitive("\n".join([node.title, node.summary, node.body, *node.evidence]))
        if labels:
            raise ValidationError(
                f"bundle 节点 {node.id} 含敏感信息",
                field="nodes",
                hint="先移除密钥/口令再导出或导入",
            )
        nodes.append(node)
    if manifest.get("node_count") != len(nodes):
        raise ValidationError("bundle 节点数与 manifest 不一致", field="manifest")
    to_write: list[MemoryNode] = []
    skipped = 0
    for node in nodes:
        existing = vault.find_node_path(node.id)
        if existing is not None:
            expected = vault.node_path(node.category, node.id)
            if existing.resolve() != expected.resolve():
                raise ValidationError(f"节点 {node.id} 的分类与现有文件不一致", field="category")
            if mode == "skip":
                skipped += 1
                continue
        to_write.append(node)
    imported = 0
    for node in to_write:
        write_node(vault, node, command="import")
        imported += 1
    return {"path": str(source), "imported": imported, "skipped": skipped, "mode": mode}
