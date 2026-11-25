from __future__ import annotations

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult

from zymor import knowledge_view
from zymor.cli import main

ITEM = "pilot-schedule"
ORIGINAL = "测试资料中的蓝线在周三维护。"
CORRECTED = "测试资料中的蓝线在周五维护。"


def _invoke(
    database: Path, capsys: Any, *args: str, expected_code: int = 0
) -> dict[str, Any]:
    code = main(["--json", "knowledge", "--db", str(database), *args])
    output = capsys.readouterr()
    assert code == expected_code, output
    return json.loads(output.out)


def _draft_from_dialogue(database: Path, capsys: Any, tmp_path: Path) -> str:
    dialogue = tmp_path / "dialogue.txt"
    dialogue.write_text(f"用户：蓝线何时维护？\n助手：{ORIGINAL}\n", encoding="utf-8")
    _invoke(database, capsys, "init")
    receipt = _invoke(database, capsys, "capture", str(dialogue), "--key", "synthetic-dialogue")
    assert _invoke(
        database, capsys, "capture", str(dialogue), "--key", "synthetic-dialogue"
    ) == receipt
    proposal = _invoke(
        database,
        capsys,
        "propose",
        ITEM,
        "--title",
        "蓝线维护时间",
        "--statement",
        ORIGINAL,
        "--source",
        str(receipt["source_id"]),
        "--evidence",
        ORIGINAL,
    )
    return str(proposal["proposal_id"])


def test_knowledge_pilot_cli_updates_two_connected_mcp_clients_and_markdown(
    tmp_path: Path, capsys: Any
) -> None:
    database = tmp_path / "pilot.sqlite3"
    first_id = _draft_from_dialogue(database, capsys, tmp_path)
    notes = tmp_path / "Test Obsidian Vault"
    note = notes / f"zymor-{ITEM}.md"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "zymor", "knowledge", "--db", str(database), "mcp"],
        env={"PYTHONUTF8": "1"},
    )

    async def verify() -> None:
        async with AsyncExitStack() as stack:
            clients: list[ClientSession] = []
            for _ in range(2):
                streams = await stack.enter_async_context(stdio_client(parameters))
                client = await stack.enter_async_context(ClientSession(*streams))
                await client.initialize()
                assert [tool.name for tool in (await client.list_tools()).tools] == [
                    "knowledge_get"
                ]
                clients.append(client)

            async def read_both(version: int, statement: str) -> None:
                results = await asyncio.gather(
                    *(client.call_tool("knowledge_get", {"item_id": ITEM}) for client in clients)
                )
                for result in results:
                    assert isinstance(result, CallToolResult) and not result.is_error
                    record = result.structured_content
                    if version == 0:
                        assert record == {"found": False, "item_id": ITEM}
                    else:
                        assert record["item_id"] == ITEM
                        assert (record["revision"], record["statement"]) == (version, statement)

            await read_both(0, "")
            reviewed = _invoke(database, capsys, "review", first_id)
            assert reviewed["source_kind"] == "dialogue" and reviewed["evidence"] == ORIGINAL
            assert reviewed["approved_revision"] is None
            _invoke(database, capsys, "approve", first_id, "--markdown-dir", str(notes))
            await read_both(1, ORIGINAL)
            first_markdown = note.read_text(encoding="utf-8")
            assert "zymor_revision: 1" in first_markdown and ORIGINAL in first_markdown

            edit = tmp_path / "website-edit.md"
            edit.write_text(
                first_markdown.replace(ORIGINAL, CORRECTED) + "\n编辑稿的额外说明。\n",
                encoding="utf-8",
            )
            proposal = _invoke(
                database, capsys, "propose-article", str(edit), "--statement", CORRECTED
            )
            assert proposal["status"] == "pending"
            second_id = str(proposal["proposal_id"])
            reviewed = _invoke(database, capsys, "review", second_id)
            assert reviewed["source_kind"] == "article_edit" and reviewed["base_revision"] == 1
            await read_both(1, ORIGINAL)
            assert note.read_text(encoding="utf-8") == first_markdown

            update = _invoke(database, capsys, "approve", second_id, "--markdown-dir", str(notes))
            assert update["revision"] == 2
            assert update["markdown_revision"] == 2
            assert _invoke(database, capsys, "approve", second_id)["revision"] == 2
            replay = _invoke(database, capsys, "approve", first_id, "--markdown-dir", str(notes))
            assert (replay["revision"], replay["markdown_revision"]) == (1, 2)
            await read_both(2, CORRECTED)
            final_markdown = note.read_text(encoding="utf-8")
            assert "zymor_revision: 2" in final_markdown and CORRECTED in final_markdown
            assert ORIGINAL not in final_markdown and "额外说明" not in final_markdown

    asyncio.run(verify())


def test_article_conflict_and_publication_retry_preserve_human_edits(
    tmp_path: Path, capsys: Any
) -> None:
    database = tmp_path / "pilot.sqlite3"
    first_id = _draft_from_dialogue(database, capsys, tmp_path)
    notes = tmp_path / "notes"
    note = notes / f"zymor-{ITEM}.md"
    _invoke(database, capsys, "approve", first_id, "--markdown-dir", str(notes))
    original = note.read_text(encoding="utf-8")
    edited = original + "\n这里只改排版和说明。\n"
    note.write_text(edited, encoding="utf-8")
    unchanged = _invoke(
        database, capsys, "propose-article", str(note), "--statement", ORIGINAL, expected_code=4
    )
    assert unchanged["field"] == "statement"

    copied_note = tmp_path / "website-edit.md"

    def propose(statement: str) -> str:
        copied_note.write_text(original.replace(ORIGINAL, statement), encoding="utf-8")
        result = _invoke(
            database, capsys, "propose-article", str(copied_note), "--statement", statement
        )
        return str(result["proposal_id"])

    winning_id = propose(CORRECTED)
    other_id = propose("测试资料中的蓝线在周六维护。")
    error = _invoke(
        database, capsys, "approve", winning_id, "--markdown-dir", str(notes), expected_code=1
    )
    assert "已核准版本 2" in error["message"]
    assert note.read_text(encoding="utf-8") == edited
    assert _invoke(database, capsys, "show", ITEM)["revision"] == 2

    conflict = _invoke(database, capsys, "approve", other_id, expected_code=4)
    assert conflict["error"] == "revision_conflict"
    corrected_notes = tmp_path / "regenerated"
    path = _invoke(database, capsys, "export", ITEM, str(corrected_notes))["markdown"]
    assert CORRECTED in Path(path).read_text(encoding="utf-8")


def test_projection_preserves_edit_detected_before_replacing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "pilot.sqlite3"
    first_id = _draft_from_dialogue(database, capsys, tmp_path)
    notes = tmp_path / "notes"
    _invoke(database, capsys, "approve", first_id, "--markdown-dir", str(notes))
    note = notes / f"zymor-{ITEM}.md"
    original = note.read_text(encoding="utf-8")
    edited = original + "\n生成期间的人工修改。\n"
    draft = tmp_path / "website-edit.md"
    draft.write_text(original.replace(ORIGINAL, CORRECTED), encoding="utf-8")
    receipt = _invoke(database, capsys, "propose-article", str(draft), "--statement", CORRECTED)
    write_markdown = knowledge_view.atomic_write_text

    def edit_before_replace(path: Path, text: str, **kwargs: Any) -> None:
        path.write_text(edited, encoding="utf-8")
        write_markdown(path, text, **kwargs)

    monkeypatch.setattr(knowledge_view, "atomic_write_text", edit_before_replace)
    result = _invoke(
        database, capsys, "approve", str(receipt["proposal_id"]),
        "--markdown-dir", str(notes), expected_code=1,
    )
    assert "目标笔记在生成期间发生变化" in result["message"]
    assert note.read_text(encoding="utf-8") == edited
    assert _invoke(database, capsys, "show", ITEM)["revision"] == 2
