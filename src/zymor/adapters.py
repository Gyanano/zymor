from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from zymor.errors import ValidationError
from zymor.model.node import MemoryNode
from zymor.security import detect_sensitive
from zymor.store.nodes import write_node
from zymor.vault import Vault


@dataclass(frozen=True)
class AdapterNode:
    """外部记忆系统映射到 Zymor 的最小记录。"""

    external_id: str
    markdown: str


class MemoryAdapter(Protocol):
    """第三方记忆适配器接口。只负责读取并转换为 Zymor Markdown。"""

    name: str

    def read(self, source: Path) -> Iterable[AdapterNode]:
        """入参：外部数据源。出参：AdapterNode 迭代器。"""
        ...


def import_from_adapter(
    vault: Vault,
    adapter: MemoryAdapter,
    source: Path,
    *,
    mode: str = "skip",
) -> dict[str, Any]:
    """入参：vault、适配器、源路径与冲突模式。出参：导入统计。"""
    imported = 0
    skipped = 0
    for item in adapter.read(source):
        try:
            node = MemoryNode.from_markdown(item.markdown)
        except Exception as error:
            raise ValidationError(
                f"适配器 {adapter.name} 返回的节点无法解析: {item.external_id}: {error}",
                field="adapter",
            ) from error
        labels = detect_sensitive("\n".join([node.title, node.summary, node.body, *node.evidence]))
        if labels:
            raise ValidationError(
                f"适配器 {adapter.name} 返回的节点含敏感信息: {item.external_id}",
                field="adapter",
            )
        if vault.find_node_path(node.id) is not None and mode == "skip":
            skipped += 1
            continue
        write_node(vault, node, command="adapter-import")
        imported += 1
    return {"adapter": adapter.name, "imported": imported, "skipped": skipped, "mode": mode}


class FakeMemoryAdapter:
    """测试用适配器；不读取任何外部系统。"""

    name = "fake"

    def __init__(self, nodes: list[AdapterNode]) -> None:
        self._nodes = list(nodes)

    def read(self, source: Path) -> Iterable[AdapterNode]:
        del source
        return iter(self._nodes)
