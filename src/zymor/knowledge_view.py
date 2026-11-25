from __future__ import annotations

from pathlib import Path

from zymor.errors import ValidationError
from zymor.knowledge import KnowledgeStore, Revision
from zymor.util.fs import atomic_write_text
from zymor.util.yamlutil import dump_yaml, split_frontmatter


def render_markdown(revision: Revision) -> str:
    metadata = dump_yaml(
        {
            "zymor_item": revision.item_id,
            "zymor_revision": revision.revision,
            "zymor_approved_at": revision.approved_at,
        }
    )
    return f"---\n{metadata}---\n\n# {revision.title}\n\n{revision.statement}\n"


def export_markdown(store: KnowledgeStore, item_id: str, directory: Path) -> Path:
    """生成用于阅读的 Markdown。检测到手工修改时保留现有文件。"""
    with store.publication_snapshot(item_id) as current:
        target = directory / f"zymor-{current.item_id}.md"
        expected = render_markdown(current)
        if target.is_symlink():
            raise ValidationError("请为生成的笔记选择普通文件路径", field="directory")
        if target.exists():
            existing = target.read_text(encoding="utf-8")
            if existing == expected:
                return target
            metadata, _ = split_frontmatter(existing)
            previous_item, previous_revision = _article_reference(metadata)
            previous = (
                store.read(item_id, previous_revision) if previous_item == item_id else None
            )
            if previous is None or render_markdown(previous) != existing:
                raise ValidationError(
                    "目标笔记已有手工改动或来自其他条目",
                    field="directory",
                    hint="保留手工稿，选择新的生成目录，或先把手工稿另存为提案来源",
                )
        atomic_write_text(target, expected)
        return target


def propose_article_edit(store: KnowledgeStore, article: Path, statement: str) -> int:
    """把编辑稿和明确选中的新结论带回审核。"""
    text = article.read_text(encoding="utf-8-sig")
    metadata, body = split_frontmatter(text)
    item_id, base_revision = _article_reference(metadata)
    statement = statement.strip()
    if not statement or statement not in body:
        raise ValidationError("新结论应为文章正文中的一段文本", field="statement")
    source_id = store.capture("article_edit", f"{item_id}@{base_revision}", text)
    return store.propose(
        item_id,
        statement=statement,
        source_id=source_id,
        evidence=statement,
        base_revision=base_revision,
    )


def _article_reference(metadata: dict[str, object]) -> tuple[str, int]:
    item_id = metadata.get("zymor_item")
    revision = metadata.get("zymor_revision")
    if not isinstance(item_id, str) or type(revision) is not int or revision < 1:
        raise ValidationError("请保留生成稿的条目标识和正整数版本号", field="frontmatter")
    return item_id, revision
