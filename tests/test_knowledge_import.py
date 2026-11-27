from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest

from zymor.cli import main

CLAIM = "虚构蓝线每周五维护。"


def _run(
    database: Path, capsys: pytest.CaptureFixture[str], *args: str, exit_code: int = 0
) -> dict[str, Any]:
    code = main(["--json", "knowledge", "--db", str(database), *args])
    output = capsys.readouterr()
    assert code == exit_code, output
    return json.loads(output.out)


def _conversation(identifier: str, title: str, answer: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "conversation_id": identifier,
        "title": title,
        "current_node": "answer",
        "mapping": {
            "root": {"message": None, "parent": None, "children": ["question"]},
            "question": {
                "message": {
                    "id": "question",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["这份测试记录说了什么？"]},
                },
                "parent": "root",
                "children": ["answer"],
            },
            "answer": {
                "message": {
                    "id": "answer",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": [answer]},
                },
                "parent": "question",
                "children": [],
            },
        },
    }


def test_import_search_dedup_and_review_shared_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "batch.sqlite3"
    export = tmp_path / "chatgpt-test-export.zip"
    chat = _conversation("synthetic-blue", "合成对话 蓝线维护", CLAIM)
    other_chat = _conversation("synthetic-platform", "合成对话 站台", "站台配有一座时钟。")
    with ZipFile(export, "w") as archive:
        archive.writestr("account.json", '{"sample":"仅用于验证文件选择"}')
        archive.writestr("conversations-001.json", json.dumps([chat]))
        archive.writestr("conversations-002.json", json.dumps([other_chat]))
    vault = tmp_path / "Obsidian Test"
    vault.mkdir()
    (vault / "maintenance.md").write_text(f"# 维护安排\n\n{CLAIM}\n", encoding="utf-8")
    (vault / "handover.md").write_text(f"# 交接记录\n\n- {CLAIM}\n", encoding="utf-8")
    (vault / "platform.md").write_text("# 站台说明\n\n站台时钟使用模拟时间。\n", encoding="utf-8")

    initial = _run(database, capsys, "import", str(vault), str(export))
    assert (initial["added"], initial["duplicates"], initial["issues"]) == (5, 0, [])
    unpacked = tmp_path / "renamed-conversation-export.json"
    unpacked.write_text(json.dumps([other_chat, chat], ensure_ascii=False), encoding="utf-8")
    again = _run(database, capsys, "import", str(unpacked), str(vault))
    assert (again["added"], again["duplicates"], again["issues"]) == (0, 5, [])
    assert {row["source_id"] for row in again["sources"]} == {
        row["source_id"] for row in initial["sources"]
    }
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM sources").fetchone()[0] == 5

    hits = _run(database, capsys, "search-sources", "虚构蓝线每周五")["hits"]
    assert {row["source_kind"] for row in hits} == {"dialogue", "document"}
    assert [row["locator"] for row in hits] == ["L3-L3", "L3-L3", "message:answer:part:1"]
    assert hits[0]["text"] == f"- {CLAIM}"
    for hit in hits:
        assert hit["text"].removeprefix("- ") == CLAIM
        source = _run(database, capsys, "source", str(hit["source_id"]))
        if hit["source_kind"] == "dialogue":
            assert json.loads(source["content"]) == chat
        else:
            assert source["content"].splitlines()[2] == hit["text"]
    assert _run(database, capsys, "search-sources", "仅用于验证文件选择")["hits"] == []

    draft = _run(
        database, capsys, "distill", "blue-line", "虚构蓝线每周五", "--title", "蓝线维护日"
    )
    assert draft["status"] == "pending"
    review = _run(database, capsys, "review", str(draft["proposal_id"]))
    assert review["statement"] == CLAIM and review["approved_revision"] is None
    references = review["evidence_refs"]
    assert [(ref["source_id"], ref["locator"], ref["quote"]) for ref in references] == [
        (hit["source_id"], hit["locator"], hit["text"]) for hit in hits
    ]
    assert _run(database, capsys, "show", "blue-line", exit_code=3)["error"] == "not_found"
    _run(database, capsys, "approve", str(draft["proposal_id"]))
    published = _run(database, capsys, "show", "blue-line", "--evidence")
    assert (published["revision"], published["evidence_refs"]) == (1, references)

    (vault / "maintenance.md").write_text("# 新维护安排\n\n已归档旧培训材料。\n", encoding="utf-8")
    new = _run(database, capsys, "import", str(vault))
    assert (new["added"], new["duplicates"]) == (1, 2)
    assert len(_run(database, capsys, "search-sources", "虚构蓝线每周五")["hits"]) == 2
    assert _run(database, capsys, "show", "blue-line", "--evidence")["evidence_refs"] == references
    (vault / "maintenance.md").write_text(f"# 维护安排\n\n{CLAIM}\n", encoding="utf-8")
    restored = _run(database, capsys, "import", str(vault))
    assert (restored["added"], restored["duplicates"]) == (0, 3)
    assert _run(database, capsys, "search-sources", "虚构蓝线每周五")["hits"] == hits
    assert _run(database, capsys, "show", "blue-line", "--evidence")["evidence_refs"] == references


def test_batch_upgrades_an_existing_pilot_and_reports_unusable_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "old-pilot.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(
            """
            PRAGMA application_id = 0x5A594D32;
            CREATE TABLE sources (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('dialogue', 'article_edit')),
                source_key TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                content TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                UNIQUE (kind, source_key, sha256)
            );
            CREATE TABLE proposals (
                id INTEGER PRIMARY KEY, item_id TEXT NOT NULL,
                base_revision INTEGER NOT NULL CHECK (base_revision >= 0),
                title TEXT NOT NULL, statement TEXT NOT NULL,
                source_id INTEGER NOT NULL REFERENCES sources(id),
                evidence TEXT NOT NULL, proposed_at TEXT NOT NULL,
                UNIQUE (item_id, base_revision, title, statement, source_id, evidence)
            );
            CREATE TABLE revisions (
                item_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision > 0),
                proposal_id INTEGER NOT NULL UNIQUE REFERENCES proposals(id),
                approved_at TEXT NOT NULL,
                PRIMARY KEY (item_id, revision)
            );
            INSERT INTO sources VALUES (7, 'dialogue', 'legacy', 'hash', '原始资料', '2026-01-01');
            INSERT INTO proposals VALUES (
                8, 'legacy', 0, '旧条目', '旧结论', 7, '原始资料', '2026-01-01'
            );
            INSERT INTO revisions VALUES ('legacy', 1, 8, '2026-01-01');
            """
        )
    old = _run(database, capsys, "show", "legacy", "--evidence")
    export = tmp_path / "partial.zip"
    with ZipFile(export, "w") as archive:
        archive.writestr("conversations-001.json", "invalid")
        archive.writestr(
            "conversations-002.json",
            json.dumps(
                [
                    {"title": "missing stable ID"},
                    _conversation("new-chat", "合成会话", CLAIM),
                ]
            ),
        )
    generated = tmp_path / "generated.md"
    generated.write_text(
        f"---\nzymor_item: generated\nzymor_revision: 1\n---\n\n{CLAIM}\n", encoding="utf-8"
    )
    imported = _run(database, capsys, "import", str(export), str(generated))
    assert (imported["added"], imported["duplicates"], len(imported["issues"])) == (1, 0, 3)
    assert _run(database, capsys, "show", "legacy", "--evidence") == old
    assert _run(database, capsys, "source", "7")["content"] == "原始资料"
    duplicate = _run(database, capsys, "import", str(export))
    assert (duplicate["added"], duplicate["duplicates"]) == (0, 1)
    assert _run(database, capsys, "distill", "new-item", "虚构蓝线", exit_code=3)["error"] == (
        "not_found"
    )
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_import_indexes_preexisting_capture_and_upgrades_indexed_snapshots(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "batch.sqlite3"
    chat = _conversation("previously-captured", "原有对话", CLAIM)
    raw = tmp_path / "raw.txt"
    raw.write_text(json.dumps(chat, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    _run(database, capsys, "init")
    original = _run(database, capsys, "capture", str(raw), "--key", "chatgpt:previously-captured")
    archive = tmp_path / "conversations.json"
    archive.write_text(json.dumps([chat], ensure_ascii=False), encoding="utf-8")
    imported = _run(database, capsys, "import", str(archive))
    assert (imported["added"], imported["duplicates"]) == (0, 1)
    hits = _run(database, capsys, "search-sources", "虚构蓝线每周五")["hits"]
    assert [hit["source_id"] for hit in hits] == [original["source_id"]]

    raw.write_text("仅作临时采集的另一份正文。", encoding="utf-8")
    _run(database, capsys, "capture", str(raw), "--key", "chatgpt:previously-captured")
    assert _run(database, capsys, "search-sources", "虚构蓝线每周五")["hits"] == hits
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TABLE semantic_proposals")
        connection.execute("DROP TABLE current_sources")
        connection.execute("DROP TABLE proposal_conflicts")
        connection.execute("PRAGMA user_version = 1")
    assert _run(database, capsys, "search-sources", "虚构蓝线每周五")["hits"] == hits
    _run(database, capsys, "init")
    assert _run(database, capsys, "search-sources", "虚构蓝线每周五")["hits"] == hits
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TABLE semantic_proposals")
        connection.execute("DROP TABLE proposal_conflicts")
        connection.execute("PRAGMA user_version = 2")
    _run(database, capsys, "init")
    assert _run(database, capsys, "search-sources", "虚构蓝线每周五")["hits"] == hits
