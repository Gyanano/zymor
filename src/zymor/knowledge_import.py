from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any, Literal
from zipfile import BadZipFile, ZipFile

from zymor.errors import NotFoundError, ValidationError
from zymor.knowledge import EvidenceReference, KnowledgeStore
from zymor.util.yamlutil import split_frontmatter

_CHATGPT_MEMBER = re.compile(r"conversations(?:[-_]?\d+)?\.json", re.IGNORECASE)
_SENTENCE_BREAK = re.compile(r"(?<=[。！？!?])|(?<=\.)\s+|\n")


@dataclass(frozen=True)
class _Material:
    kind: Literal["dialogue", "document"]
    key: str
    title: str
    content: str
    fragments: list[tuple[str, str]]


@dataclass
class _ImportReport:
    sources: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, str]] = field(default_factory=list)

    def issue(self, path: str, reason: str) -> None:
        self.issues.append({"path": path, "reason": reason})

    def capture(self, store: KnowledgeStore, material: _Material, label: str) -> None:
        try:
            source_id, created = store.capture_imported(
                material.kind,
                material.key,
                material.content,
                title=material.title,
                fragments=material.fragments,
            )
        except ValidationError as error:
            self.issue(label, error.message)
            return
        except UnicodeError:
            self.issue(label, "正文包含无法保存的字符")
            return
        self.sources.append(
            {
                "source_id": source_id,
                "title": material.title,
                "status": "added" if created else "duplicate",
            }
        )

    def result(self) -> dict[str, Any]:
        added = sum(row["status"] == "added" for row in self.sources)
        return {
            "added": added,
            "duplicates": len(self.sources) - added,
            "sources": self.sources,
            "issues": self.issues,
        }


def import_materials(store: KnowledgeStore, paths: Sequence[Path]) -> dict[str, Any]:
    """归档明确指定的 ChatGPT 对话导出与 Markdown，逐份返回导入结果。"""
    store.initialize()
    report = _ImportReport()
    for path in paths:
        if path.is_dir():
            root = path.resolve()
            files = sorted(
                child
                for child in path.rglob("*")
                if child.suffix.lower() == ".md"
                and child.is_file()
                and not child.is_symlink()
                and not {".obsidian", ".trash"}.intersection(child.relative_to(path).parts[:-1])
                and child.resolve().is_relative_to(root)
            )
            if not files:
                report.issue(str(path), "目录中没有找到 Markdown 文件")
        else:
            files = [path]
        for file in files:
            _import_file(store, report, file)
    return report.result()


def _import_file(store: KnowledgeStore, report: _ImportReport, path: Path) -> None:
    label = str(path)
    try:
        match path.suffix.lower():
            case ".zip":
                with ZipFile(path) as archive:
                    members = [
                        member
                        for member in archive.infolist()
                        if not member.is_dir()
                        and _CHATGPT_MEMBER.fullmatch(PurePosixPath(member.filename).name)
                    ]
                    if not members:
                        report.issue(label, "导出包中没有找到对话 JSON")
                    for member in members:
                        member_label = f"{label}!{member.filename}"
                        try:
                            with archive.open(member) as data:
                                _import_conversations(store, report, member_label, data)
                        except (BadZipFile, OSError, RuntimeError) as error:
                            report.issue(member_label, str(error))
            case ".json":
                with path.open("rb") as data:
                    _import_conversations(store, report, label, data)
            case ".md":
                report.capture(store, _markdown(path), label)
            case _:
                report.issue(label, "试点接收对话 ZIP、对话 JSON 或 Markdown 文件")
    except (BadZipFile, OSError, UnicodeError, ValidationError) as error:
        report.issue(label, str(error))


def _import_conversations(
    store: KnowledgeStore, report: _ImportReport, label: str, data: IO[bytes]
) -> None:
    try:
        conversations = json.load(data)
    except (json.JSONDecodeError, UnicodeError) as error:
        report.issue(label, f"对话 JSON 无法读取，{error}")
        return
    if not isinstance(conversations, list) or not conversations:
        report.issue(label, "需要包含会话记录的非空 ChatGPT 导出数组")
        return
    for index, conversation in enumerate(conversations, start=1):
        row_label = f"{label}#{index}"
        try:
            material = _chatgpt_conversation(conversation)
        except ValidationError as error:
            report.issue(row_label, error.message)
            continue
        report.capture(store, material, row_label)


def _chatgpt_conversation(conversation: object) -> _Material:
    if not isinstance(conversation, dict):
        raise ValidationError("会话记录应为对象")
    identifier = conversation.get("conversation_id") or conversation.get("id")
    mapping = conversation.get("mapping")
    if not isinstance(identifier, str) or not identifier.strip() or not isinstance(mapping, dict):
        raise ValidationError("会话缺少稳定 ID 或消息映射")
    fragments: list[tuple[str, str]] = []
    for node_id, node in mapping.items():
        message = node.get("message") if isinstance(node, dict) else None
        if not isinstance(message, dict):
            continue
        author = message.get("author")
        if not isinstance(author, dict) or author.get("role") not in ("user", "assistant"):
            continue
        content = message.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part_index, part in enumerate(parts, start=1):
            if isinstance(part, str) and part.strip():
                fragments.append((f"message:{node_id}:part:{part_index}", part))
    title = conversation.get("title")
    return _Material(
        "dialogue",
        f"chatgpt:{identifier.strip()}",
        title.strip() if isinstance(title, str) and title.strip() else identifier,
        json.dumps(conversation, ensure_ascii=False, sort_keys=True),
        fragments,
    )


def _markdown(path: Path) -> _Material:
    content = path.read_text(encoding="utf-8-sig")
    if content.startswith("---\n"):
        try:
            metadata, _ = split_frontmatter(content)
        except ValidationError:
            metadata = {}
        if "zymor_item" in metadata and "zymor_revision" in metadata:
            raise ValidationError("这个 Markdown 是 Zymor 生成的知识视图")
    return _Material(
        "document",
        f"obsidian:{os.path.normcase(str(path.resolve()))}",
        path.stem,
        content,
        list(_markdown_fragments(content)),
    )


def _markdown_fragments(content: str) -> Iterator[tuple[str, str]]:
    lines = content.split("\n")
    start: int | None = None
    for index, line in enumerate([*lines, ""]):
        if line.strip():
            if start is None:
                start = index
        elif start is not None:
            yield f"L{start + 1}-L{index}", "\n".join(lines[start:index])
            start = None


def distill_shared_statement(
    store: KnowledgeStore,
    item_id: str,
    query: str,
    *,
    title: str | None = None,
    base_revision: int = 0,
) -> int:
    """提取多份材料中共同出现的一条原句，提交待审提案。"""
    groups: dict[str, dict[tuple[str, str], EvidenceReference]] = {}
    needle = query.strip().casefold()
    for hit in store.search_sources(query, limit=None):
        source_key = (hit["source_kind"], hit["source_key"])
        for part in _SENTENCE_BREAK.split(hit["text"]):
            sentence = part.strip()
            folded = sentence.casefold()
            if needle not in folded or sentence.startswith(("#", "```", "~~~")):
                continue
            group = groups.setdefault(folded, {})
            group.setdefault(
                source_key,
                EvidenceReference(hit["source_id"], hit["locator"], sentence),
            )
    shared = [tuple(group.values()) for group in groups.values() if len(group) >= 2]
    if not shared:
        raise NotFoundError("没有找到同时出现在至少两份材料中的同一句结论")
    if len(shared) > 1:
        raise ValidationError("找到多条共有结论，请用更具体的查询词逐条提案", field="query")
    references = shared[0]
    primary = references[0]
    return store.propose(
        item_id,
        statement=primary.quote,
        source_id=primary.source_id,
        evidence=primary.quote,
        base_revision=base_revision,
        title=title,
        references=references,
    )
