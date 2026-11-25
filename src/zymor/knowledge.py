from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from zymor.errors import NotFoundError, ValidationError
from zymor.security import detect_sensitive
from zymor.util.fs import sha256_hex
from zymor.util.time import now_iso

_APPLICATION_ID = 0x5A594D32
_ITEM_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_SCHEMA = (
    """
    CREATE TABLE sources (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('dialogue', 'article_edit')),
        source_key TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        content TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        UNIQUE (kind, source_key, sha256)
    )
    """,
    """
    CREATE TABLE proposals (
        id INTEGER PRIMARY KEY,
        item_id TEXT NOT NULL,
        base_revision INTEGER NOT NULL CHECK (base_revision >= 0),
        title TEXT NOT NULL,
        statement TEXT NOT NULL,
        source_id INTEGER NOT NULL REFERENCES sources(id),
        evidence TEXT NOT NULL,
        proposed_at TEXT NOT NULL,
        UNIQUE (item_id, base_revision, title, statement, source_id, evidence)
    )
    """,
    """
    CREATE TABLE revisions (
        item_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision > 0),
        proposal_id INTEGER NOT NULL UNIQUE REFERENCES proposals(id),
        approved_at TEXT NOT NULL,
        PRIMARY KEY (item_id, revision)
    )
    """,
)


@dataclass(frozen=True)
class Revision:
    item_id: str
    revision: int
    title: str
    statement: str
    approved_at: str


class RevisionConflict(ValidationError):
    kind = "revision_conflict"


class KnowledgeStore:
    """单人试点的来源、待审提案和已核准版本。"""

    def __init__(self, database: Path) -> None:
        self.database = database.resolve()

    def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            marker = connection.execute("PRAGMA application_id").fetchone()[0]
            if marker == _APPLICATION_ID:
                return
            if marker or connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                raise ValidationError("请为知识试点选择独立数据库", field="db")
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self.database.is_file():
            raise NotFoundError(
                "知识试点库尚未初始化", hint="先运行 zymor knowledge --db PATH init"
            )
        mode = "rw" if write else "ro"
        uri = f"{self.database.as_uri()}?mode={mode}"
        with closing(sqlite3.connect(uri, uri=True)) as connection, connection:
            connection.row_factory = sqlite3.Row
            if connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID:
                raise ValidationError("所选数据库不属于 Zymor 知识试点", field="db")
            if write:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("BEGIN IMMEDIATE")
            yield connection

    def capture(
        self, kind: Literal["dialogue", "article_edit"], source_key: str, content: str
    ) -> int:
        source_key = source_key.strip()
        if not source_key or not content.strip():
            raise ValidationError("来源标识和正文均不能为空", field="source")
        if detect_sensitive(source_key + "\n" + content):
            raise ValidationError("来源含疑似凭证", field="source")
        identity = (kind, source_key, sha256_hex(content))
        with self._connect(write=True) as connection:
            connection.execute(
                "INSERT INTO sources (kind, source_key, sha256, content, captured_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (kind, source_key, sha256) DO NOTHING",
                (*identity, content, now_iso()),
            )
            row = connection.execute(
                "SELECT id FROM sources WHERE kind = ? AND source_key = ? AND sha256 = ?", identity
            ).fetchone()
            return int(row["id"])

    def propose(
        self,
        item_id: str,
        *,
        statement: str,
        source_id: int,
        evidence: str,
        base_revision: int = 0,
        title: str | None = None,
    ) -> int:
        item_id = _validate_item_id(item_id)
        statement, evidence = statement.strip(), evidence.strip()
        if not statement or not evidence:
            raise ValidationError("提案须包含结论和原文摘录", field="statement")
        with self._connect(write=True) as connection:
            current = _read_revision(connection, item_id)
            _check_base(current, base_revision)
            if current is not None and current.statement == statement:
                raise ValidationError("结论与当前版本相同", field="statement")
            title = (title if title is not None else current.title if current else "").strip()
            if not title or len(title.splitlines()) != 1:
                raise ValidationError("请提供单行标题", field="title")
            if detect_sensitive(title + "\n" + statement):
                raise ValidationError("提案含疑似凭证", field="statement")
            source = connection.execute(
                "SELECT content FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
            if source is None:
                raise NotFoundError(f"找不到来源 {source_id}")
            if evidence not in source["content"]:
                raise ValidationError("原文摘录应能在已采集的来源中定位", field="evidence")
            identity = (item_id, base_revision, title, statement, source_id, evidence)
            connection.execute(
                "INSERT INTO proposals"
                " (item_id, base_revision, title, statement, source_id, evidence, proposed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (*identity, now_iso()),
            )
            row = connection.execute(
                "SELECT id FROM proposals"
                " WHERE item_id = ? AND base_revision = ? AND title = ? AND statement = ?"
                " AND source_id = ? AND evidence = ?",
                identity,
            ).fetchone()
            return int(row["id"])

    def review(self, proposal_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            proposal = connection.execute(
                """
                SELECT proposals.*, sources.kind AS source_kind, sources.source_key,
                       sources.content AS source_text, revisions.revision AS approved_revision
                FROM proposals JOIN sources ON sources.id = proposals.source_id
                LEFT JOIN revisions ON revisions.proposal_id = proposals.id
                WHERE proposals.id = ?
                """,
                (proposal_id,),
            ).fetchone()
            if proposal is None:
                raise NotFoundError(f"找不到提案 {proposal_id}")
            return dict(proposal)

    def approve(self, proposal_id: int) -> Revision:
        with self._connect(write=True) as connection:
            proposal = connection.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if proposal is None:
                raise NotFoundError(f"找不到提案 {proposal_id}")
            committed = connection.execute(
                "SELECT revision, approved_at FROM revisions WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            item_id = str(proposal["item_id"])
            if committed is None:
                base_revision = int(proposal["base_revision"])
                _check_base(_read_revision(connection, item_id), base_revision)
                revision = base_revision + 1
                approved_at = now_iso()
                connection.execute(
                    "INSERT INTO revisions VALUES (?, ?, ?, ?)",
                    (item_id, revision, proposal_id, approved_at),
                )
            else:
                revision = int(committed["revision"])
                approved_at = str(committed["approved_at"])
            return Revision(
                item_id, revision, str(proposal["title"]), str(proposal["statement"]), approved_at
            )

    def read(self, item_id: str, revision: int | None = None) -> Revision | None:
        with self._connect() as connection:
            return _read_revision(connection, _validate_item_id(item_id), revision)

    @contextmanager
    def publication_snapshot(self, item_id: str) -> Iterator[Revision]:
        """本地投影更新期间锁定当前版本，避免较旧的生成任务覆盖新版。"""
        with self._connect(write=True) as connection:
            current = _read_revision(connection, _validate_item_id(item_id))
            if current is None:
                raise NotFoundError(f"找不到已核准条目 {item_id}")
            yield current


def _validate_item_id(item_id: str) -> str:
    if not _ITEM_ID.fullmatch(item_id):
        raise ValidationError(
            "条目标识须由小写字母、数字、连字符或下划线组成，并以字母或数字开头",
            field="item_id",
        )
    return item_id


def _read_revision(
    connection: sqlite3.Connection, item_id: str, revision: int | None = None
) -> Revision | None:
    sql = """
        SELECT revisions.item_id, revisions.revision, proposals.title, proposals.statement,
               revisions.approved_at
        FROM revisions JOIN proposals ON proposals.id = revisions.proposal_id
        WHERE revisions.item_id = ?
    """
    if revision is None:
        row = connection.execute(sql + " ORDER BY revision DESC LIMIT 1", (item_id,)).fetchone()
    else:
        row = connection.execute(
            sql + " AND revisions.revision = ?", (item_id, revision)
        ).fetchone()
    if row is None:
        return None
    return Revision(
        str(row["item_id"]),
        int(row["revision"]),
        str(row["title"]),
        str(row["statement"]),
        str(row["approved_at"]),
    )


def _check_base(current: Revision | None, base_revision: int) -> None:
    latest = current.revision if current is not None else 0
    if base_revision != latest:
        raise RevisionConflict(
            f"提案基于版本 {base_revision}，当前版本为 {latest}",
            field="base_revision",
            hint="保留原稿，从当前版本重新比较并提交",
        )
