from __future__ import annotations

import json
import sqlite3
from array import array
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zymor import __version__
from zymor.errors import IndexFailure, ValidationError
from zymor.model.node import MemoryNode
from zymor.tokenizer import tokenize_text
from zymor.util.fs import sha256_hex
from zymor.util.time import now_iso

INDEX_VERSION = 2
TOKENIZER_VERSION = "cjk-bigram-v2-identifiers"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nodes(
  id TEXT PRIMARY KEY, path TEXT NOT NULL,
  layer TEXT NOT NULL, category TEXT NOT NULL, status TEXT NOT NULL,
  title TEXT NOT NULL, summary TEXT NOT NULL,
  confidence REAL NOT NULL, arousal REAL NOT NULL, hit_count INTEGER NOT NULL,
  verify_count INTEGER NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_verified TEXT NOT NULL,
  valid_from TEXT NOT NULL, valid_to TEXT,
  project TEXT, applies_to TEXT, tags TEXT,
  source TEXT NOT NULL, created_by TEXT NOT NULL,
  content_hash TEXT NOT NULL, indexed_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
  id UNINDEXED, title, summary, body, tags,
  tokenize = "unicode61 tokenchars '_'"
);
CREATE TABLE IF NOT EXISTS links(
  src TEXT NOT NULL, dst TEXT NOT NULL, PRIMARY KEY(src, dst)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS nodes_status_idx ON nodes(status);
CREATE INDEX IF NOT EXISTS nodes_layer_idx ON nodes(layer);
CREATE INDEX IF NOT EXISTS nodes_category_idx ON nodes(category);
CREATE INDEX IF NOT EXISTS nodes_updated_idx ON nodes(updated_at);
CREATE INDEX IF NOT EXISTS links_dst_idx ON links(dst);
CREATE TABLE IF NOT EXISTS embeddings(
  node_id TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS embeddings_model_idx ON embeddings(model);
"""


def _json_list(value: str | None) -> list[str]:
    """入参：JSON 数组文本。出参：字符串列表；不可解析时返回空列表。"""
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]


@dataclass
class Candidate:
    id: str
    path: str
    title: str
    summary: str
    layer: str
    category: str
    status: str
    confidence: float
    arousal: float
    rank: float
    project: str | None = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：可 JSON 序列化的候选节点摘要。"""
        return {
            "id": self.id,
            "path": self.path,
            "title": self.title,
            "summary": self.summary,
            "layer": self.layer,
            "category": self.category,
            "status": self.status,
            "confidence": self.confidence,
            "arousal": self.arousal,
            "rank": self.rank,
            "project": self.project,
            "tags": self.tags,
        }


@dataclass
class SyncReport:
    scanned: int = 0
    upserted: int = 0
    deleted: int = 0
    invalid: list[dict[str, str]] = field(default_factory=list)
    rebuilt: bool = False

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：同步报告字典。"""
        return {
            "scanned": self.scanned,
            "upserted": self.upserted,
            "deleted": self.deleted,
            "invalid": self.invalid,
            "rebuilt": self.rebuilt,
        }


class Index:
    def __init__(self, vault_root: Path) -> None:
        self.root = vault_root
        self.db_path = vault_root / "index" / "memory.sqlite3"
        self.nodes_dir = vault_root / "nodes"
        self.dirty_path = vault_root / "index" / ".dirty"

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """入参：无。出参：SQLite 连接上下文（WAL + synchronous=NORMAL，提交后关闭）。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(self.db_path)
        except sqlite3.Error as error:
            raise IndexFailure(f"无法打开索引数据库: {error}") from error
        connection.row_factory = sqlite3.Row
        try:
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error as error:
                raise IndexFailure(f"索引数据库不可用: {error}") from error
            yield connection
            try:
                connection.commit()
            except sqlite3.Error as error:
                raise IndexFailure(f"索引提交失败: {error}") from error
        finally:
            connection.close()

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(SCHEMA_SQL)

    def _metadata(self, connection: sqlite3.Connection) -> dict[str, str]:
        return {
            row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM meta")
        }

    def _set_metadata(self, connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _needs_rebuild(self) -> bool:
        if not self.db_path.is_file():
            return True
        try:
            with self.connect() as connection:
                self._create_schema(connection)
                metadata = self._metadata(connection)
        except sqlite3.DatabaseError:
            return True
        except IndexFailure:
            return True
        return (
            metadata.get("index_version") != str(INDEX_VERSION)
            or metadata.get("tokenizer") != TOKENIZER_VERSION
            or metadata.get("schema_version") != "1"
        )

    def ensure(self) -> None:
        """入参：无。出参：无。确保索引可用；缺失自动重建，dirty 自动同步。"""
        if self._needs_rebuild():
            self.rebuild()
            return
        with self.connect() as connection:
            self._create_schema(connection)
        if self.dirty_reason() is not None:
            self.sync(ensure_index=False)

    def _iter_node_files(self) -> Iterator[Path]:
        if not self.nodes_dir.is_dir():
            return
        for path in sorted(self.nodes_dir.glob("*/*.md")):
            if path.name.startswith("_"):
                continue
            yield path

    def _upsert(
        self,
        connection: sqlite3.Connection,
        node: MemoryNode,
        relative_path: str,
        content_hash: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO nodes(
              id, path, layer, category, status, title, summary,
              confidence, arousal, hit_count, verify_count,
              created_at, updated_at, last_verified, valid_from, valid_to,
              project, applies_to, tags, source, created_by, content_hash, indexed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              path=excluded.path, layer=excluded.layer, category=excluded.category,
              status=excluded.status, title=excluded.title, summary=excluded.summary,
              confidence=excluded.confidence, arousal=excluded.arousal,
              hit_count=excluded.hit_count, verify_count=excluded.verify_count,
              created_at=excluded.created_at, updated_at=excluded.updated_at,
              last_verified=excluded.last_verified, valid_from=excluded.valid_from,
              valid_to=excluded.valid_to, project=excluded.project,
              applies_to=excluded.applies_to, tags=excluded.tags,
              source=excluded.source, created_by=excluded.created_by,
              content_hash=excluded.content_hash, indexed_at=excluded.indexed_at
            """,
            (
                node.id,
                relative_path,
                node.layer,
                node.category,
                node.status,
                node.title,
                node.summary,
                node.confidence,
                node.arousal,
                node.hit_count,
                node.verify_count,
                node.created_at,
                node.updated_at,
                node.last_verified,
                node.valid_from,
                node.valid_to,
                node.project,
                json.dumps(node.applies_to, ensure_ascii=False),
                json.dumps(node.tags, ensure_ascii=False),
                node.source,
                node.created_by,
                content_hash,
                now_iso(),
            ),
        )
        connection.execute("DELETE FROM nodes_fts WHERE id = ?", (node.id,))
        connection.execute(
            "INSERT INTO nodes_fts(id, title, summary, body, tags) VALUES(?,?,?,?,?)",
            (
                node.id,
                tokenize_text(node.title),
                tokenize_text(node.summary),
                tokenize_text(node.body),
                tokenize_text(" ".join(node.tags)),
            ),
        )
        connection.execute("DELETE FROM links WHERE src = ?", (node.id,))
        for target in node.links():
            connection.execute(
                "INSERT OR IGNORE INTO links(src, dst) VALUES(?, ?)", (node.id, target)
            )

    def upsert_node(
        self, node: MemoryNode, relative_path: str, content_hash: str | None = None
    ) -> None:
        """入参：节点、相对路径、可选内容哈希。出参：无。单条写入索引（不重建）。"""
        digest = content_hash or sha256_hex(node.to_markdown())
        try:
            with self.connect() as connection:
                self._create_schema(connection)
                self._upsert(connection, node, relative_path, digest)
        except sqlite3.Error as error:
            raise IndexFailure(f"索引写入失败: {error}") from error

    def _delete(self, connection: sqlite3.Connection, node_id: str) -> None:
        connection.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        connection.execute("DELETE FROM nodes_fts WHERE id = ?", (node_id,))
        connection.execute("DELETE FROM links WHERE src = ?", (node_id,))

    def _scan_one(self, path: Path, report: SyncReport) -> MemoryNode | None:
        report.scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
            return MemoryNode.from_markdown(text)
        except (OSError, UnicodeError, ValidationError) as error:
            message = error.message if isinstance(error, ValidationError) else str(error)
            report.invalid.append(
                {"path": path.relative_to(self.root).as_posix(), "error": message}
            )
            return None

    def rebuild(self) -> SyncReport:
        """入参：无。出参：重建报告。丢弃并全量重建索引（事实源是节点文件）。"""
        report = SyncReport(rebuilt=True)
        try:
            with self.connect() as connection:
                connection.executescript(
                    "DROP TABLE IF EXISTS nodes_fts; "
                    "DROP TABLE IF EXISTS nodes; "
                    "DROP TABLE IF EXISTS links;"
                )
                self._create_schema(connection)
                for path in self._iter_node_files():
                    node = self._scan_one(path, report)
                    if node is None:
                        continue
                    text = path.read_text(encoding="utf-8")
                    self._upsert(
                        connection,
                        node,
                        path.relative_to(self.root).as_posix(),
                        sha256_hex(text),
                    )
                    report.upserted += 1
                self._set_metadata(connection, "schema_version", "1")
                self._set_metadata(connection, "index_version", str(INDEX_VERSION))
                self._set_metadata(connection, "tokenizer", TOKENIZER_VERSION)
                self._set_metadata(connection, "built_at", now_iso())
                self._set_metadata(connection, "builder", f"zymor {__version__}")
        except sqlite3.Error as error:
            raise IndexFailure(f"索引重建失败: {error}") from error
        self.clear_dirty()
        return report

    def sync(self, *, ensure_index: bool = True) -> SyncReport:
        """入参：可选是否先确保索引。出参：增量同步报告。按 mtime 与 content_hash 更新文件变化。"""
        report = SyncReport()
        if ensure_index:
            self.ensure()
        try:
            with self.connect() as connection:
                existing = {
                    row["id"]: (row["path"], row["content_hash"])
                    for row in connection.execute("SELECT id, path, content_hash FROM nodes")
                }
                seen: set[str] = set()
                for path in self._iter_node_files():
                    report.scanned += 1
                    relative = path.relative_to(self.root).as_posix()
                    try:
                        text = path.read_text(encoding="utf-8")
                        node = MemoryNode.from_markdown(text)
                    except (OSError, UnicodeError, ValidationError) as error:
                        message = (
                            error.message if isinstance(error, ValidationError) else str(error)
                        )
                        report.invalid.append({"path": relative, "error": message})
                        continue
                    seen.add(node.id)
                    digest = sha256_hex(text)
                    if existing.get(node.id) == (relative, digest):
                        continue
                    self._upsert(connection, node, relative, digest)
                    report.upserted += 1
                for node_id in existing:
                    if node_id not in seen:
                        self._delete(connection, node_id)
                        report.deleted += 1
        except sqlite3.Error as error:
            raise IndexFailure(f"索引同步失败: {error}") from error
        self.clear_dirty()
        return report

    def search(
        self,
        match_query: str,
        *,
        categories: list[str] | None = None,
        layers: list[str] | None = None,
        statuses: list[str] | None = None,
        project: str | None = None,
        limit: int = 8,
    ) -> list[Candidate]:
        """入参：FTS5 MATCH 表达式与过滤条件。出参：按 bm25 排序的候选节点（最多 limit+ 条）。"""
        if not match_query or limit <= 0:
            return []
        self.ensure()
        candidate_limit = max(limit * 10, 100)
        try:
            with self.connect() as connection:
                rows = connection.execute(
                    "SELECT id, bm25(nodes_fts) AS rank FROM nodes_fts "
                    "WHERE nodes_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match_query, candidate_limit),
                ).fetchall()
                ranks = {row["id"]: float(row["rank"]) for row in rows}
                if not ranks:
                    return []
                placeholders = ",".join("?" for _ in ranks)
                clauses = [f"id IN ({placeholders})"]
                parameters: list[Any] = list(ranks)
                if categories:
                    clauses.append(f"category IN ({','.join('?' for _ in categories)})")
                    parameters.extend(categories)
                if layers:
                    clauses.append(f"layer IN ({','.join('?' for _ in layers)})")
                    parameters.extend(layers)
                if statuses:
                    clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
                    parameters.extend(statuses)
                if project is not None:
                    clauses.append("project = ?")
                    parameters.append(project)
                sql = (
                    "SELECT id, path, title, summary, layer, category, status, "
                    "confidence, arousal, project, tags "
                    f"FROM nodes WHERE {' AND '.join(clauses)}"
                )
                found = {row["id"]: row for row in connection.execute(sql, parameters)}
        except sqlite3.Error as error:
            raise IndexFailure(f"检索失败: {error}") from error
        candidates: list[Candidate] = []
        for node_id, rank in ranks.items():
            row = found.get(node_id)
            if row is None:
                continue
            try:
                tags = json.loads(row["tags"]) if row["tags"] else []
            except json.JSONDecodeError:
                tags = []
            candidates.append(
                Candidate(
                    id=row["id"],
                    path=row["path"],
                    title=row["title"],
                    summary=row["summary"],
                    layer=row["layer"],
                    category=row["category"],
                    status=row["status"],
                    confidence=float(row["confidence"]),
                    arousal=float(row["arousal"]),
                    rank=rank,
                    project=row["project"],
                    tags=tags,
                )
            )
            if len(candidates) >= limit:
                break
        return candidates

    def candidate_rows(
        self,
        match_query: str,
        *,
        categories: list[str] | None = None,
        layers: list[str] | None = None,
        statuses: list[str] | None = None,
        project: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """入参：FTS5 MATCH 与过滤。出参：候选行字典（含 rank 与元数据），按 bm25 升序。"""
        if not match_query or limit <= 0:
            return []
        self.ensure()
        try:
            with self.connect() as connection:
                fts_rows = connection.execute(
                    "SELECT id, bm25(nodes_fts) AS rank FROM nodes_fts "
                    "WHERE nodes_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match_query, max(limit, 100)),
                ).fetchall()
                ranks = {row["id"]: float(row["rank"]) for row in fts_rows}
                if not ranks:
                    return []
                placeholders = ",".join("?" for _ in ranks)
                clauses = [f"id IN ({placeholders})"]
                parameters: list[Any] = list(ranks)
                if categories:
                    clauses.append(f"category IN ({','.join('?' for _ in categories)})")
                    parameters.extend(categories)
                if layers:
                    clauses.append(f"layer IN ({','.join('?' for _ in layers)})")
                    parameters.extend(layers)
                if statuses:
                    clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
                    parameters.extend(statuses)
                if project is not None:
                    clauses.append("project = ?")
                    parameters.append(project)
                sql = (
                    "SELECT id, path, layer, category, status, title, summary, "
                    "confidence, arousal, hit_count, updated_at, valid_to, project, "
                    "applies_to, tags FROM nodes "
                    f"WHERE {' AND '.join(clauses)}"
                )
                rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as error:
            raise IndexFailure(f"候选检索失败: {error}") from error
        found = {row["id"]: row for row in rows}
        result: list[dict[str, Any]] = []
        for node_id, rank in ranks.items():
            row = found.get(node_id)
            if row is None:
                continue
            data = dict(row)
            data["rank"] = rank
            data["applies_to"] = _json_list(row["applies_to"])
            data["tags"] = _json_list(row["tags"])
            result.append(data)
        return result

    def links(self, node_id: str) -> dict[str, list[str]]:
        """入参：节点 id。出参：`{"out": [...], "in": [...]}` 出链与入链 id 列表。"""
        self.ensure()
        try:
            with self.connect() as connection:
                out = [
                    row["dst"]
                    for row in connection.execute(
                        "SELECT dst FROM links WHERE src = ? ORDER BY dst", (node_id,)
                    )
                ]
                incoming = [
                    row["src"]
                    for row in connection.execute(
                        "SELECT src FROM links WHERE dst = ? ORDER BY src", (node_id,)
                    )
                ]
        except sqlite3.Error as error:
            raise IndexFailure(f"链接查询失败: {error}") from error
        return {"out": out, "in": incoming}

    def nodes_by_ids(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """入参：节点 id 列表。出参：id 到索引行的映射（缺失的 id 不出现）。"""
        if not ids:
            return {}
        self.ensure()
        try:
            with self.connect() as connection:
                placeholders = ",".join("?" for _ in ids)
                rows = connection.execute(
                    "SELECT id, path, layer, category, status, title, summary, "
                    "confidence, arousal, hit_count, updated_at, project, applies_to, tags "
                    f"FROM nodes WHERE id IN ({placeholders})",
                    ids,
                ).fetchall()
        except sqlite3.Error as error:
            raise IndexFailure(f"节点查询失败: {error}") from error
        return {row["id"]: dict(row) for row in rows}

    def recent_rows(self, updated_since: str, limit: int = 50) -> list[dict[str, Any]]:
        """入参：ISO8601 时间下界与上限。出参：该时刻后更新过的节点行，按 updated_at 降序。"""
        self.ensure()
        try:
            with self.connect() as connection:
                rows = connection.execute(
                    "SELECT id, path, layer, category, status, title, summary, "
                    "confidence, arousal, hit_count, updated_at, project, applies_to, tags "
                    "FROM nodes WHERE updated_at >= ? ORDER BY updated_at DESC LIMIT ?",
                    (updated_since, limit),
                ).fetchall()
        except sqlite3.Error as error:
            raise IndexFailure(f"近期节点查询失败: {error}") from error
        result: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data["applies_to"] = _json_list(row["applies_to"])
            data["tags"] = _json_list(row["tags"])
            result.append(data)
        return result

    def rows_for_category(self, category: str, limit: int = 5000) -> list[dict[str, Any]]:
        """入参：认知分类与上限。出参：该分类节点行，按 updated_at 降序。"""
        self.ensure()
        try:
            with self.connect() as connection:
                rows = connection.execute(
                    "SELECT id, path, layer, category, status, title, summary, "
                    "confidence, arousal, hit_count, updated_at, project, applies_to, tags "
                    "FROM nodes WHERE category = ? ORDER BY updated_at DESC LIMIT ?",
                    (category, limit),
                ).fetchall()
        except sqlite3.Error as error:
            raise IndexFailure(f"分类查询失败: {error}") from error
        return [dict(row) for row in rows]

    def layer_rows(
        self,
        layer: str,
        *,
        statuses: list[str] | None = None,
        project: str | None = None,
        updated_since: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """入参：层与过滤条件。出参：该层节点行，按 updated_at 降序。"""
        self.ensure()
        clauses = ["layer = ?"]
        parameters: list[Any] = [layer]
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            parameters.extend(statuses)
        if project is not None:
            clauses.append("project = ?")
            parameters.append(project)
        if updated_since is not None:
            clauses.append("updated_at >= ?")
            parameters.append(updated_since)
        parameters.append(limit)
        try:
            with self.connect() as connection:
                rows = connection.execute(
                    "SELECT id, path, layer, category, status, title, summary, "
                    "confidence, arousal, hit_count, updated_at, project, applies_to, tags "
                    f"FROM nodes WHERE {' AND '.join(clauses)} "
                    "ORDER BY updated_at DESC LIMIT ?",
                    parameters,
                ).fetchall()
        except sqlite3.Error as error:
            raise IndexFailure(f"层查询失败: {error}") from error
        result: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data["applies_to"] = _json_list(row["applies_to"])
            data["tags"] = _json_list(row["tags"])
            result.append(data)
        return result

    def tag_counts(self, limit: int = 100) -> list[tuple[str, int]]:
        """入参：上限。出参：按出现次数降序的标签计数。"""
        self.ensure()
        try:
            with self.connect() as connection:
                rows = connection.execute("SELECT tags FROM nodes").fetchall()
        except sqlite3.Error as error:
            raise IndexFailure(f"标签统计失败: {error}") from error
        counts: dict[str, int] = {}
        for row in rows:
            for tag in _json_list(row["tags"]):
                counts[tag] = counts.get(tag, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return ranked[:limit]

    def counts(self) -> dict[str, Any]:
        """入参：无。出参：节点计数（总数、按层/类/状态）。"""
        self.ensure()
        try:
            with self.connect() as connection:
                total = connection.execute("SELECT COUNT(*) AS count FROM nodes").fetchone()[
                    "count"
                ]
                layers = {
                    row["layer"]: row["count"]
                    for row in connection.execute(
                        "SELECT layer, COUNT(*) AS count FROM nodes GROUP BY layer"
                    )
                }
                categories = {
                    row["category"]: row["count"]
                    for row in connection.execute(
                        "SELECT category, COUNT(*) AS count FROM nodes GROUP BY category"
                    )
                }
                statuses = {
                    row["status"]: row["count"]
                    for row in connection.execute(
                        "SELECT status, COUNT(*) AS count FROM nodes GROUP BY status"
                    )
                }
                links = connection.execute("SELECT COUNT(*) AS count FROM links").fetchone()[
                    "count"
                ]
        except sqlite3.Error as error:
            raise IndexFailure(f"统计失败: {error}") from error
        return {
            "total": total,
            "layers": layers,
            "categories": categories,
            "statuses": statuses,
            "links": links,
        }

    def upsert_embedding(self, node_id: str, model: str, vector: list[float]) -> None:
        """入参：节点 id、模型名与向量。出参：无。写入或覆盖该节点的向量。"""
        if not vector:
            raise ValidationError("向量不能为空", field="vector")
        self.ensure()
        payload = array("f", [float(value) for value in vector]).tobytes()
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO embeddings(node_id, model, dim, vector, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(node_id) DO UPDATE SET
                      model = excluded.model,
                      dim = excluded.dim,
                      vector = excluded.vector,
                      updated_at = excluded.updated_at
                    """,
                    (node_id, model, len(vector), payload, now_iso()),
                )
        except sqlite3.Error as error:
            raise IndexFailure(f"向量写入失败: {error}") from error

    def embedding_rows(self, model: str | None = None) -> list[dict[str, Any]]:
        """入参：可选模型名。出参：节点 id、模型与向量列表。"""
        self.ensure()
        query = "SELECT node_id, model, dim, vector, updated_at FROM embeddings"
        params: tuple[Any, ...] = ()
        if model is not None:
            query += " WHERE model = ?"
            params = (model,)
        try:
            with self.connect() as connection:
                rows = list(connection.execute(query, params))
        except sqlite3.Error as error:
            raise IndexFailure(f"向量读取失败: {error}") from error
        results: list[dict[str, Any]] = []
        for row in rows:
            vector = array("f")
            vector.frombytes(row["vector"])
            results.append(
                {
                    "node_id": str(row["node_id"]),
                    "model": str(row["model"]),
                    "dim": int(row["dim"]),
                    "vector": [float(value) for value in vector],
                    "updated_at": str(row["updated_at"]),
                }
            )
        return results

    def embedding_count(self, model: str | None = None) -> int:
        """入参：可选模型名。出参：已写入向量的节点数。"""
        self.ensure()
        query = "SELECT COUNT(*) AS count FROM embeddings"
        params: tuple[Any, ...] = ()
        if model is not None:
            query += " WHERE model = ?"
            params = (model,)
        with self.connect() as connection:
            return int(connection.execute(query, params).fetchone()["count"])

    def mark_dirty(self, reason: str) -> None:
        self.dirty_path.parent.mkdir(parents=True, exist_ok=True)
        self.dirty_path.write_text(
            json.dumps({"ts": now_iso(), "reason": reason}, ensure_ascii=False),
            encoding="utf-8",
        )

    def clear_dirty(self) -> None:
        """入参：无。出参：无。清除 dirty 标记。"""
        self.dirty_path.unlink(missing_ok=True)

    def dirty_reason(self) -> str | None:
        """入参：无。出参：dirty 标记内容；未标记返回 None。"""
        if not self.dirty_path.is_file():
            return None
        try:
            data = json.loads(self.dirty_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "索引 dirty 标记不可解析"
        return str(data.get("reason", "unknown"))
