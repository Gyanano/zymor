from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from itertools import groupby
from pathlib import Path
from typing import Any, Literal

from zymor.errors import NotFoundError, ValidationError
from zymor.knowledge_semantic import SemanticSlot, extract_claims, slot_key, statement_for
from zymor.security import detect_sensitive
from zymor.util.fs import sha256_hex
from zymor.util.time import now_iso

_APPLICATION_ID = 0x5A594D32
_SCHEMA_VERSION = 4
_ITEM_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_TOPIC = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
_SOURCE_COLUMNS = """
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('dialogue', 'article_edit', 'document')),
        source_key TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        content TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        title TEXT,
        UNIQUE (kind, source_key, sha256)
"""
_PROPOSAL_COLUMNS = "id, item_id, base_revision, title, statement, source_id, evidence, proposed_at"


def _proposals_schema(table: str) -> str:
    return f"""
    CREATE TABLE {table} (
        id INTEGER PRIMARY KEY,
        item_id TEXT NOT NULL,
        base_revision INTEGER NOT NULL CHECK (base_revision >= 0),
        title TEXT NOT NULL,
        statement TEXT NOT NULL,
        source_id INTEGER NOT NULL REFERENCES sources(id),
        evidence TEXT NOT NULL,
        proposed_at TEXT NOT NULL,
        draft_key TEXT NOT NULL DEFAULT '',
        UNIQUE (item_id, base_revision, title, statement, source_id, evidence, draft_key)
    )
    """


_SCHEMA = (
    f"CREATE TABLE sources ({_SOURCE_COLUMNS})",
    _proposals_schema("proposals"),
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
_IMPORT_SCHEMA = (
    """
    CREATE TABLE source_fragments (
        source_id INTEGER NOT NULL REFERENCES sources(id),
        position INTEGER NOT NULL,
        locator TEXT NOT NULL,
        text TEXT NOT NULL,
        PRIMARY KEY (source_id, locator)
    )
    """,
    """
    CREATE TABLE proposal_evidence (
        proposal_id INTEGER NOT NULL REFERENCES proposals(id),
        source_id INTEGER NOT NULL,
        locator TEXT NOT NULL,
        quote TEXT NOT NULL,
        PRIMARY KEY (proposal_id, source_id, locator, quote),
        FOREIGN KEY (source_id, locator) REFERENCES source_fragments(source_id, locator)
    )
    """,
)
_CURRENT_SOURCES_SCHEMA = """
    CREATE TABLE current_sources (
        kind TEXT NOT NULL,
        source_key TEXT NOT NULL,
        source_id INTEGER NOT NULL UNIQUE REFERENCES sources(id),
        PRIMARY KEY (kind, source_key)
    )
"""


@dataclass(frozen=True)
class EvidenceReference:
    source_id: int
    locator: str
    quote: str


@dataclass
class _SemanticGroup:
    slot: SemanticSlot
    alternatives: dict[str, list[EvidenceReference]]

    @property
    def snapshot(self) -> str:
        return sha256_hex(_encode_alternatives(self.alternatives))


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
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            marker = connection.execute("PRAGMA application_id").fetchone()[0]
            if marker == _APPLICATION_ID:
                version = _schema_version(connection)
                if version == _SCHEMA_VERSION:
                    return
                if version not in (0, 1, 2, 3):
                    raise ValidationError("当前版本无法打开该知识试点库", field="db")
                connection.execute(_proposals_schema("proposals_upgrade"))
                connection.execute(
                    f"INSERT INTO proposals_upgrade ({_PROPOSAL_COLUMNS})"
                    f" SELECT {_PROPOSAL_COLUMNS} FROM proposals"
                )
                connection.execute("DROP TABLE proposals")
                connection.execute("ALTER TABLE proposals_upgrade RENAME TO proposals")
                if version == 0:
                    connection.execute(f"CREATE TABLE sources_upgrade ({_SOURCE_COLUMNS})")
                    connection.execute(
                        "INSERT INTO sources_upgrade"
                        " (id, kind, source_key, sha256, content, captured_at)"
                        " SELECT id, kind, source_key, sha256, content, captured_at FROM sources"
                    )
                    connection.execute("DROP TABLE sources")
                    connection.execute("ALTER TABLE sources_upgrade RENAME TO sources")
            else:
                if marker or connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                    raise ValidationError("请为知识试点选择独立数据库", field="db")
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                version = 0
            if version == 0:
                for statement in _IMPORT_SCHEMA:
                    connection.execute(statement)
            if version < 2:
                connection.execute(_CURRENT_SOURCES_SCHEMA)
                connection.execute(
                    """
                    INSERT INTO current_sources (kind, source_key, source_id)
                    SELECT s.kind, s.source_key, max(s.id)
                    FROM sources s
                    WHERE EXISTS (SELECT 1 FROM source_fragments f WHERE f.source_id = s.id)
                    GROUP BY s.kind, s.source_key
                    """
                )
            if version < 3:
                connection.execute(
                    "CREATE TABLE proposal_conflicts ("
                    "proposal_id INTEGER PRIMARY KEY REFERENCES proposals(id), "
                    "alternatives TEXT NOT NULL)"
                )
            _upgrade_semantics(connection)
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValidationError("知识试点库的来源或版本引用无法核对", field="db")

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
            if _schema_version(connection) > _SCHEMA_VERSION:
                raise ValidationError("当前版本无法打开该知识试点库", field="db")
            if write:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("BEGIN IMMEDIATE")
            else:
                connection.execute("BEGIN")
            yield connection

    def _upgrade_existing(self) -> None:
        with self._connect() as connection:
            if _schema_version(connection) == _SCHEMA_VERSION:
                return
        self.initialize()

    def capture(
        self, kind: Literal["dialogue", "article_edit"], source_key: str, content: str
    ) -> int:
        source_id, _ = self._capture(kind, source_key, content)
        return source_id

    def capture_imported(
        self,
        kind: Literal["dialogue", "document"],
        source_key: str,
        content: str,
        *,
        title: str,
        fragments: Sequence[tuple[str, str]],
    ) -> tuple[int, bool]:
        if not fragments:
            raise ValidationError("来源缺少可搜索的正文", field="source")
        return self._capture(kind, source_key, content, title=title, fragments=fragments)

    def _capture(
        self,
        kind: Literal["dialogue", "article_edit", "document"],
        source_key: str,
        content: str,
        *,
        title: str | None = None,
        fragments: Sequence[tuple[str, str]] = (),
    ) -> tuple[int, bool]:
        source_key = source_key.strip()
        if not source_key or not content.strip():
            raise ValidationError("来源标识和正文均不能为空", field="source")
        if detect_sensitive("\n".join((source_key, title or "", content))):
            raise ValidationError("来源含疑似凭证", field="source")
        identity = (kind, source_key, sha256_hex(content))
        with self._connect(write=True) as connection:
            insertion = connection.execute(
                "INSERT INTO sources (kind, source_key, sha256, content, captured_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (kind, source_key, sha256) DO NOTHING",
                (*identity, content, now_iso()),
            )
            row = connection.execute(
                "SELECT id FROM sources WHERE kind = ? AND source_key = ? AND sha256 = ?", identity
            ).fetchone()
            source_id = int(row["id"])
            if fragments:
                connection.execute(
                    "UPDATE sources SET title = ? WHERE id = ? AND title IS NULL",
                    (title, source_id),
                )
                connection.executemany(
                    "INSERT INTO source_fragments (source_id, position, locator, text)"
                    " VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    [(source_id, index, *fragment) for index, fragment in enumerate(fragments)],
                )
                connection.execute(
                    "INSERT INTO current_sources (kind, source_key, source_id) VALUES (?, ?, ?)"
                    " ON CONFLICT (kind, source_key) DO UPDATE SET source_id = excluded.source_id"
                    " WHERE current_sources.source_id <> excluded.source_id",
                    (kind, source_key, source_id),
                )
            return source_id, insertion.rowcount == 1

    def search_sources(self, query: str, *, limit: int | None = 20) -> list[dict[str, Any]]:
        query = query.strip()
        if not query or (limit is not None and limit < 1):
            raise ValidationError("请输入查询词和正整数条数", field="query")
        with self._connect() as connection:
            return _source_fragments(connection, query, limit)

    def read_source(self, source_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            source = connection.execute(
                "SELECT * FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
            if source is None:
                raise NotFoundError(f"找不到来源 {source_id}")
            return dict(source)

    def propose(
        self,
        item_id: str,
        *,
        statement: str,
        source_id: int,
        evidence: str,
        base_revision: int = 0,
        title: str | None = None,
        references: Sequence[EvidenceReference] = (),
    ) -> int:
        item_id = _validate_item_id(item_id)
        self._upgrade_existing()
        with self._connect(write=True) as connection:
            current = _read_revision(connection, item_id)
            _check_base(current, base_revision)
            if current is not None and current.statement == statement.strip():
                raise ValidationError("结论与当前版本相同", field="statement")
            return _save_proposal(
                connection,
                item_id,
                base_revision=base_revision,
                title=title if title is not None else current.title if current else "",
                statement=statement,
                source_id=source_id,
                evidence=evidence,
                references=references,
            )

    def distill_semantic(self, item_prefix: str, query: str) -> list[dict[str, int | str]]:
        if not _TOPIC.fullmatch(item_prefix):
            raise ValidationError("主题标识须以小写字母或数字开头，最多 48 位，可用连字符及下划线")
        if not query.strip():
            raise ValidationError("请输入查询词", field="query")
        self._upgrade_existing()
        with self._connect(write=True) as connection:
            groups, selected = _semantic_groups(connection, query=query)
            if not selected:
                raise NotFoundError("搜索结果中没有找到试点支持的表述")
            results: list[dict[str, int | str]] = []
            for group in sorted((groups[key] for key in selected), key=lambda group: group.slot):
                slot = group.slot
                existing = connection.execute(
                    """
                    SELECT p.item_id FROM semantic_proposals sp
                    JOIN proposals p ON p.id = sp.proposal_id
                    WHERE sp.slot_key = ? ORDER BY p.id LIMIT 1
                    """,
                    (slot.key,),
                ).fetchone()
                item_id = existing["item_id"] if existing else f"{item_prefix}-{slot.key}"
                current = _read_revision(connection, item_id)
                conflict = len(group.alternatives) > 1
                statement = statement_for(slot.title, tuple(group.alternatives))
                if current is not None:
                    if current.statement == statement or (
                        conflict and _resolution_snapshot(connection, current) == group.snapshot
                    ):
                        continue
                proposal_id = _save_semantic_proposal(
                    connection, item_id, current.revision if current else 0, group
                )
                results.append(
                    {
                        "proposal_id": proposal_id,
                        "item_id": item_id,
                        "status": "pending_conflict" if conflict else "pending",
                        "scope": slot.scope,
                    }
                )
            return results

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
            result = dict(proposal)
            result.pop("draft_key", None)
            result["evidence_refs"] = _proposal_evidence(connection, proposal)
            conflict = _conflict(connection, proposal_id)
            if conflict:
                indexed_evidence = {
                    (ref["source_id"], ref["locator"], ref["quote"]): ref
                    for ref in result["evidence_refs"]
                }
                for refs in conflict.values():
                    for ref in refs:
                        ref.update(indexed_evidence[ref["source_id"], ref["locator"], ref["quote"]])
                result["conflict"] = conflict
            semantic = _semantic_proposal(connection, proposal_id)
            if semantic is not None and semantic["resolves_conflict"] is not None:
                result["resolves_conflict"] = semantic["resolves_conflict"]
            return result

    def revision_evidence(self, item_id: str, revision: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            proposal = connection.execute(
                """
                SELECT p.*, s.kind AS source_kind, s.source_key
                FROM revisions r JOIN proposals p ON p.id = r.proposal_id
                JOIN sources s ON s.id = p.source_id
                WHERE r.item_id = ? AND r.revision = ?
                """,
                (_validate_item_id(item_id), revision),
            ).fetchone()
            if proposal is None:
                raise NotFoundError(f"找不到已核准版本 {item_id}@{revision}")
            return _proposal_evidence(connection, proposal)

    def approve(self, proposal_id: int, *, choose: str | None = None) -> Revision:
        self._upgrade_existing()
        with self._connect(write=True) as connection:
            proposal = connection.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if proposal is None:
                raise NotFoundError(f"找不到提案 {proposal_id}")
            if choose is not None:
                return _approve_conflict_choice(connection, proposal, choose)
            if _conflict(connection, proposal_id):
                raise ValidationError("请用 review 核对各方原文，再用 approve --choose 选择口径")
            committed = _committed_revision(connection, proposal_id)
            if committed is not None:
                return committed
            item_id = str(proposal["item_id"])
            _check_base(_read_revision(connection, item_id), int(proposal["base_revision"]))
            _check_semantic_approval(connection, proposal)
            return _record_approval(connection, proposal)

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


def _schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _upgrade_semantics(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE semantic_proposals (
            proposal_id INTEGER PRIMARY KEY REFERENCES proposals(id),
            slot_key TEXT NOT NULL,
            resolves_conflict INTEGER UNIQUE REFERENCES proposal_conflicts(proposal_id)
        )
        """
    )
    if _schema_version(connection) == 3:
        # Recover the original generated IDs. Empty snapshots require re-distillation
        # under the stricter rules before a pending draft can be approved.
        for proposal_id, item_id, title, statement, source_key in connection.execute(
            """
            SELECT p.id, p.item_id, p.title, p.statement, s.source_key
            FROM proposals p JOIN sources s ON s.id = p.source_id
            WHERE EXISTS (SELECT 1 FROM proposal_evidence e WHERE e.proposal_id = p.id)
            """
        ):
            key = item_id.rpartition("-")[2]
            generated = statement.startswith((f"{title}为", f"{title}存在不同口径，"))
            if generated and key in (slot_key(title), slot_key(title, source_key)):
                connection.execute(
                    "INSERT INTO semantic_proposals VALUES (?, ?, NULL)",
                    (proposal_id, key),
                )
    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


def _source_fragments(
    connection: sqlite3.Connection, query: str, limit: int | None
) -> list[dict[str, Any]]:
    version = _schema_version(connection)
    if version == 0:
        return []
    is_current = "s.id IN (SELECT source_id FROM current_sources)"
    if version == 1:
        is_current = """
            NOT EXISTS (
                SELECT 1 FROM sources newer
                JOIN source_fragments nf ON nf.source_id = newer.id
                WHERE newer.kind = s.kind AND newer.source_key = s.source_key
                  AND newer.id > s.id
            )
        """
    rows = connection.execute(
        f"""
        SELECT s.id AS source_id, s.kind AS source_kind, s.source_key, s.title, f.locator, f.text
        FROM source_fragments f JOIN sources s ON s.id = f.source_id
        WHERE instr(lower(f.text), lower(?)) > 0 AND {is_current}
        ORDER BY s.id, f.position
        LIMIT ?
        """,
        (query, -1 if limit is None else limit),
    )
    return [dict(row) for row in rows]


def _semantic_groups(
    connection: sqlite3.Connection, *, query: str = ""
) -> tuple[dict[str, _SemanticGroup], set[str]]:
    groups: dict[str, _SemanticGroup] = {}
    selected: set[str] = set()
    needle = query.strip().casefold()
    for source_id, fragments in groupby(
        _source_fragments(connection, "", None), key=lambda row: row["source_id"]
    ):
        rows = list(fragments)
        texts = [row["text"] for row in rows]
        claims = extract_claims(
            texts, rows[0]["source_key"], markdown=rows[0]["source_kind"] == "document"
        )
        for index, claim in claims:
            key = claim.slot.key
            group = groups.setdefault(key, _SemanticGroup(claim.slot, {}))
            if needle and needle in texts[index].casefold():
                selected.add(key)
            refs = group.alternatives.setdefault(claim.value, [])
            ref = EvidenceReference(source_id, rows[index]["locator"], claim.quote)
            if ref not in refs:
                refs.append(ref)
    return groups, selected


def _encode_alternatives(alternatives: Mapping[str, Sequence[EvidenceReference]]) -> str:
    return json.dumps(
        {value: [asdict(ref) for ref in refs] for value, refs in alternatives.items()},
        ensure_ascii=False,
        sort_keys=True,
    )


def _save_proposal(
    connection: sqlite3.Connection,
    item_id: str,
    *,
    base_revision: int,
    title: str,
    statement: str,
    source_id: int,
    evidence: str,
    references: Sequence[EvidenceReference],
    draft_key: str = "",
) -> int:
    title, statement, evidence = title.strip(), statement.strip(), evidence.strip()
    if not statement or not evidence:
        raise ValidationError("提案须包含结论和原文摘录", field="statement")
    if not title or len(title.splitlines()) != 1:
        raise ValidationError("请提供单行标题", field="title")
    if detect_sensitive(title + "\n" + statement):
        raise ValidationError("提案含疑似凭证", field="statement")
    _validate_evidence(connection, source_id, evidence, references)
    identity = (item_id, base_revision, title, statement, source_id, evidence, draft_key)
    connection.execute(
        "INSERT INTO proposals"
        " (item_id, base_revision, title, statement, source_id, evidence, draft_key, proposed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (*identity, now_iso()),
    )
    row = connection.execute(
        "SELECT id FROM proposals"
        " WHERE item_id = ? AND base_revision = ? AND title = ? AND statement = ?"
        " AND source_id = ? AND evidence = ? AND draft_key = ?",
        identity,
    ).fetchone()
    proposal_id = int(row["id"])
    if references:
        connection.executemany(
            "INSERT INTO proposal_evidence (proposal_id, source_id, locator, quote)"
            " VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
            [(proposal_id, ref.source_id, ref.locator, ref.quote) for ref in references],
        )
    return proposal_id


def _save_semantic_proposal(
    connection: sqlite3.Connection,
    item_id: str,
    base_revision: int,
    group: _SemanticGroup,
    *,
    choose: str | None = None,
    resolves_conflict: int | None = None,
) -> int:
    values = (choose,) if choose is not None else tuple(group.alternatives)
    refs = list(dict.fromkeys(ref for value in values for ref in group.alternatives[value]))
    primary = refs[0]
    proposal_id = _save_proposal(
        connection,
        item_id,
        base_revision=base_revision,
        title=group.slot.title,
        statement=statement_for(group.slot.title, values),
        source_id=primary.source_id,
        evidence=primary.quote,
        references=refs,
        draft_key=group.snapshot,
    )
    connection.execute(
        "INSERT INTO semantic_proposals VALUES (?, ?, ?) ON CONFLICT (proposal_id) DO NOTHING",
        (proposal_id, group.slot.key, resolves_conflict),
    )
    if len(values) > 1:
        connection.execute(
            "INSERT INTO proposal_conflicts VALUES (?, ?) ON CONFLICT (proposal_id) DO NOTHING",
            (proposal_id, _encode_alternatives(group.alternatives)),
        )
    return proposal_id


def _semantic_proposal(connection: sqlite3.Connection, proposal_id: int) -> sqlite3.Row | None:
    if _schema_version(connection) < 4:
        return None
    row: sqlite3.Row | None = connection.execute(
        "SELECT * FROM semantic_proposals WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    return row


def _check_semantic_approval(connection: sqlite3.Connection, proposal: sqlite3.Row) -> None:
    draft = _semantic_proposal(connection, proposal["id"])
    semantic = connection.execute(
        """
        SELECT sp.slot_key, p.title FROM semantic_proposals sp
        JOIN proposals p ON p.id = sp.proposal_id
        WHERE p.item_id = ? ORDER BY p.id LIMIT 1
        """,
        (proposal["item_id"],),
    ).fetchone()
    if semantic is None:
        return
    groups, _ = _semantic_groups(connection)
    group = groups.get(semantic["slot_key"])
    if group is not None and len(group.alternatives) > 1:
        raise ValidationError(
            "该条目当前有冲突口径",
            hint="重新运行 distill-semantic，查看冲突提案并用 approve --choose 选择",
        )
    if draft is not None and (group is None or group.snapshot != proposal["draft_key"]):
        raise ValidationError("提案的当前证据已变动，请重新运行 distill-semantic 后审核")
    if draft is None:
        _check_evidence_scope(connection, proposal, semantic)


def _check_evidence_scope(
    connection: sqlite3.Connection, proposal: sqlite3.Row, semantic: sqlite3.Row
) -> None:
    evidence = str(proposal["evidence"])
    other_view = connection.execute(
        """
        SELECT 1 FROM semantic_proposals sp JOIN proposals p ON p.id = sp.proposal_id
        WHERE p.title <> ? AND (instr(?, p.title || '为') = 1 OR instr(?, p.title || '为') = 1)
        LIMIT 1
        """,
        (semantic["title"], evidence, proposal["statement"]),
    ).fetchone()
    if other_view is not None:
        raise ValidationError("结论或摘录引用了另一个条目的知识视图", field="evidence")
    rows = connection.execute(
        """
        SELECT s.kind, s.source_key, f.text FROM sources s
        LEFT JOIN source_fragments f ON f.source_id = s.id
        WHERE s.id = ? ORDER BY f.position
        """,
        (proposal["source_id"],),
    ).fetchall()
    source = rows[0]
    claims = extract_claims(
        [row["text"] for row in rows if row["text"] is not None] or [evidence],
        source["source_key"],
        markdown=source["kind"] == "document",
    )
    for _, claim in claims:
        if evidence not in claim.quote and claim.quote not in evidence:
            continue
        if source["kind"] == "article_edit":
            same_scope = claim.slot.title == semantic["title"]
        else:
            same_scope = claim.slot.key == semantic["slot_key"]
        if not same_scope:
            raise ValidationError("摘录对应另一个条目的主体或适用条件", field="evidence")


def _resolution_snapshot(connection: sqlite3.Connection, current: Revision) -> str | None:
    row = connection.execute(
        """
        SELECT p.draft_key FROM revisions r JOIN proposals p ON p.id = r.proposal_id
        JOIN semantic_proposals sp ON sp.proposal_id = p.id
        WHERE r.item_id = ? AND r.revision = ? AND sp.resolves_conflict IS NOT NULL
        """,
        (current.item_id, current.revision),
    ).fetchone()
    return str(row["draft_key"]) if row is not None else None


def _approve_conflict_choice(
    connection: sqlite3.Connection, proposal: sqlite3.Row, value: str
) -> Revision:
    proposal_id = int(proposal["id"])
    if not _conflict(connection, proposal_id):
        raise ValidationError("--choose 只用于经过 review 的冲突提案")
    previous = connection.execute(
        "SELECT proposal_id FROM semantic_proposals WHERE resolves_conflict = ?", (proposal_id,)
    ).fetchone()
    if previous is not None and (committed := _committed_revision(connection, previous[0])):
        if committed.statement != statement_for(committed.title, (value,)):
            raise ValidationError("此冲突提案已有审核决定，请导入更新的证据后重新提炼")
        return committed
    item_id = str(proposal["item_id"])
    base_revision = int(proposal["base_revision"])
    _check_base(_read_revision(connection, item_id), base_revision)
    draft = _semantic_proposal(connection, proposal_id)
    if draft is None:
        raise ValidationError("请重新运行 distill-semantic 后审核")
    groups, _ = _semantic_groups(connection)
    group = groups.get(draft["slot_key"])
    if group is None or len(group.alternatives) < 2 or group.snapshot != proposal["draft_key"]:
        raise ValidationError("冲突的当前证据已变动，请重新运行 distill-semantic 后审核")
    if value not in group.alternatives:
        raise ValidationError("请选择 review 列出的一个冲突口径", field="choose")
    selected_id = _save_semantic_proposal(
        connection, item_id, base_revision, group, choose=value, resolves_conflict=proposal_id
    )
    selected = connection.execute("SELECT * FROM proposals WHERE id = ?", (selected_id,)).fetchone()
    return _record_approval(connection, selected)


def _committed_revision(connection: sqlite3.Connection, proposal_id: int) -> Revision | None:
    row = connection.execute(
        """
        SELECT r.item_id, r.revision, p.title, p.statement, r.approved_at
        FROM revisions r JOIN proposals p ON p.id = r.proposal_id WHERE p.id = ?
        """,
        (proposal_id,),
    ).fetchone()
    return Revision(*row) if row is not None else None


def _record_approval(connection: sqlite3.Connection, proposal: sqlite3.Row) -> Revision:
    item_id = str(proposal["item_id"])
    revision = int(proposal["base_revision"]) + 1
    approved_at = now_iso()
    connection.execute(
        "INSERT INTO revisions VALUES (?, ?, ?, ?)",
        (item_id, revision, proposal["id"], approved_at),
    )
    return Revision(
        item_id, revision, str(proposal["title"]), str(proposal["statement"]), approved_at
    )


def _conflict(connection: sqlite3.Connection, proposal_id: int) -> dict[str, Any]:
    if _schema_version(connection) < 3:
        return {}
    row = connection.execute(
        "SELECT alternatives FROM proposal_conflicts WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    return dict(json.loads(row[0])) if row else {}


def _validate_evidence(
    connection: sqlite3.Connection,
    source_id: int,
    quote: str,
    references: Sequence[EvidenceReference],
) -> None:
    if not references:
        source = connection.execute(
            "SELECT content FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        if source is None:
            raise NotFoundError(f"找不到来源 {source_id}")
        if quote not in source["content"]:
            raise ValidationError("原文摘录应能在已采集的来源中定位", field="evidence")
        return
    primary = references[0]
    if (source_id, quote) != (primary.source_id, primary.quote):
        raise ValidationError("主摘录须对应首条来源引用", field="evidence")
    for reference in references:
        fragment = connection.execute(
            "SELECT text FROM source_fragments WHERE source_id = ? AND locator = ?",
            (reference.source_id, reference.locator),
        ).fetchone()
        if (
            fragment is None
            or not reference.quote.strip()
            or reference.quote not in fragment["text"]
        ):
            raise ValidationError("原文摘录与来源位置不匹配", field="evidence")


def _proposal_evidence(
    connection: sqlite3.Connection, proposal: sqlite3.Row
) -> list[dict[str, Any]]:
    if _schema_version(connection):
        rows = connection.execute(
            """
            SELECT e.source_id, s.kind AS source_kind, s.source_key, s.title, e.locator, e.quote
            FROM proposal_evidence e JOIN sources s ON s.id = e.source_id
            WHERE e.proposal_id = ? ORDER BY e.source_id, e.locator, e.quote
            """,
            (proposal["id"],),
        ).fetchall()
        if rows:
            return [dict(row) for row in rows]
    return [
        {
            "source_id": proposal["source_id"],
            "source_kind": proposal["source_kind"],
            "source_key": proposal["source_key"],
            "title": None,
            "locator": None,
            "quote": proposal["evidence"],
        }
    ]


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
