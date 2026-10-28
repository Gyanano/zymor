from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zymor.errors import IndexFailure, NotFoundError, ValidationError
from zymor.model.node import MemoryNode
from zymor.store.audit import append_audit
from zymor.store.index import Index
from zymor.util.fs import atomic_write_text, sha256_hex
from zymor.util.time import now_iso
from zymor.vault import Vault


@dataclass
class WriteResult:
    node: MemoryNode
    path: Path
    created: bool

    def to_dict(self, vault: Vault) -> dict[str, Any]:
        """入参：vault（用于相对路径）。出参：写入结果字典。"""
        return {
            "id": self.node.id,
            "path": vault.relative(self.path),
            "created": self.created,
            "title": self.node.title,
        }


def check_layer_vault(node: MemoryNode, vault: Vault) -> None:
    """入参：节点与目标 vault。出参：无。校验层与库的强制对应关系。"""
    if node.layer == "L4" and vault.info.kind != "global":
        raise ValidationError(
            "L4 节点只能写入用户库",
            field="layer",
            hint="运行 `zymor init --global` 初始化用户库；项目库请使用 L2/L3",
        )
    if node.layer == "L2" and vault.info.kind != "project":
        raise ValidationError(
            "L2 胶囊只能写入项目库",
            field="layer",
            hint="切换到项目库或改用 L3",
        )


def read_node(vault: Vault, node_id: str) -> tuple[MemoryNode, Path]:
    """入参：vault 与节点 id。出参：解析后的节点与文件路径。不存在时抛 NotFoundError。"""
    path = vault.find_node_path(node_id)
    if path is None:
        raise NotFoundError(f"节点不存在: {node_id}", hint="用 `zymor search <query>` 查找 id")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise NotFoundError(f"节点文件不可读: {path}: {error}") from error
    except UnicodeDecodeError as error:
        raise ValidationError(f"节点文件不是合法 UTF-8: {path}", field="encoding") from error
    node = MemoryNode.from_markdown(text)
    return node, path


def _persist(
    vault: Vault,
    node: MemoryNode,
    command: str,
    created: bool,
    old_hash: str | None,
) -> Path:
    target = vault.node_path(node.category, node.id)
    relative = vault.relative(target)
    text = node.to_markdown()
    digest = sha256_hex(text)
    if not created and old_hash == digest:
        return target
    append_audit(
        vault,
        "writes.jsonl",
        {
            "ts": now_iso(),
            "op": "intent",
            "kind": "node_write",
            "node_id": node.id,
            "path": relative,
            "old_hash": old_hash,
            "new_hash": digest,
            "command": command,
        },
    )
    atomic_write_text(target, text)
    try:
        index = Index(vault.root)
        index.ensure()
        index.upsert_node(node, relative, digest)
        index.clear_dirty()
    except (sqlite3.Error, IndexFailure) as error:
        Index(vault.root).mark_dirty(f"写入 {node.id} 后索引更新失败: {error}")
    append_audit(
        vault,
        "writes.jsonl",
        {
            "ts": now_iso(),
            "op": "commit",
            "kind": "node_write",
            "node_id": node.id,
            "path": relative,
            "new_hash": digest,
            "command": command,
        },
    )
    return target


def write_node(vault: Vault, node: MemoryNode, command: str = "write") -> WriteResult:
    """入参：vault、校验通过的节点、命令名。出参：写入结果。持锁执行原子写与增量索引。"""
    check_layer_vault(node, vault)
    existing = vault.find_node_path(node.id)
    target = vault.node_path(node.category, node.id)
    if existing is not None and existing.resolve() != target.resolve():
        raise ValidationError(
            f"节点 {node.id} 已存在于 {vault.relative(existing)}，分类变更不允许原地修改",
            field="category",
            hint="保持原分类覆盖写入；认知演进请新建节点并用 supersedes 表达",
        )
    with vault.writing(command):
        created = existing is None
        old_hash = (
            sha256_hex(existing.read_text(encoding="utf-8")) if existing is not None else None
        )
        path = _persist(vault, node, command, created, old_hash)
    return WriteResult(node=node, path=path, created=created)


def record_hit(vault: Vault, node_id: str, command: str = "read") -> MemoryNode:
    """入参：vault、节点 id、命令名。出参：hit_count 递增后的节点。读取成功时调用。"""
    with vault.writing(command):
        node, path = read_node(vault, node_id)
        old_hash = sha256_hex(path.read_text(encoding="utf-8"))
        update = node.model_copy(update={"hit_count": node.hit_count + 1})
        _persist(vault, update, command, created=False, old_hash=old_hash)
    return update


def update_node(
    vault: Vault,
    node_id: str,
    updates: dict[str, Any],
    command: str = "update",
) -> MemoryNode:
    """入参：vault、节点 id、字段更新（`body` 可更新正文）。出参：更新并校验后的节点。"""
    with vault.writing(command):
        node, path = read_node(vault, node_id)
        old_hash = sha256_hex(path.read_text(encoding="utf-8"))
        payload = node.to_frontmatter()
        payload["body"] = node.body
        payload.update(updates)
        payload.setdefault("updated_at", now_iso())
        updated = MemoryNode.model_validate(payload)
        _persist(vault, updated, command, created=False, old_hash=old_hash)
    return updated
