import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from zymor.cli import main


@pytest.fixture
def run_knowledge(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    def run(*args: str, expected: int = 0):
        status = main(["--json", "knowledge", "--db", str(tmp_path / "pilot.db"), *args])
        output = capsys.readouterr()
        assert status == expected, output
        return json.loads(output.out)

    return run


def test_semantic_proposals_require_review_and_conflicts_keep_both_sources(
    tmp_path: Path, run_knowledge
) -> None:
    run = run_knowledge
    notes = tmp_path / "notes"
    notes.mkdir()
    texts = {
        "a": "将内容分解为较小的单元。\n\n样线 A（规则 v1）每周五维护。",
        "b": "将复杂的任务分解为更小的步骤。\n\n样线 A（规则 v1）的维护日是星期六。",
        "c": "样线 A（规则 v2）的维护日是星期六。",
    }
    for name, value in texts.items():
        (notes / f"{name}.md").write_text(value, encoding="utf-8")
    assert run("import", str(notes))["added"] == 3
    (merged,) = run("distill-semantic", "split", "分解")["proposals"]
    assert merged["status"] == "pending"
    assert len(run("review", str(merged["proposal_id"]))["evidence_refs"]) == 2
    run("show", merged["item_id"], expected=3)
    conflict, separate = run("distill-semantic", "line", "样线 A")["proposals"]
    assert conflict["status"] == "pending_conflict"
    assert separate["status"] == "pending"
    review = run("review", str(conflict["proposal_id"]))
    assert set(review["conflict"]) == {"周五", "周六"}
    assert review["conflict"]["周五"][0]["title"] == "a"
    assert review["conflict"]["周六"][0]["quote"] == texts["b"].split("\n\n")[1]
    assert review["conflict"]["周六"][0]["locator"] == "L3-L3"
    assert "规则 v2" in run("review", str(separate["proposal_id"]))["statement"]
    run("approve", str(conflict["proposal_id"]), expected=4)
    run("show", conflict["item_id"], expected=3)
    assert run("distill-semantic", "line", "样线 A")["proposals"] == [conflict, separate]
    assert run("distill-semantic", "renamed", "每周五维护")["proposals"] == [conflict]
    approved = run("approve", str(separate["proposal_id"]))
    assert "规则 v2" in approved["statement"]
    assert run("distill-semantic", "line", "规则 v2")["proposals"] == []


def test_split_preserves_explicit_conditions_and_declines_ambiguous_context(
    tmp_path: Path, run_knowledge
) -> None:
    run = run_knowledge
    samples = {
        "edit-a": "将内容分解为较小的单元，**关于此句另附备注**。\n\n"
        "将内容分解为较小的单元，仅适用于编辑模式。",
        "edit-b": "将复杂的任务分解为更小的步骤，以减少认知负担，仅适用于编辑模式。",
        "review": "将内容分解为较小的单元，仅适用于审核模式。",
        "adjacent": "将内容分解为较小的单元。\n\n该建议只适用于离线模式。",
        "heading": "## 草稿模式\n\n先列出材料。\n\n将内容分解为较小的单元。",
        "unknown": "将内容分解为较小的单元（仅离线时）。",
        "same-line": "将内容分解为较小的单元。简易界面例外。",
    }
    for name, content in samples.items():
        (tmp_path / f"{name}.md").write_text(content, encoding="utf-8")
    run("import", str(tmp_path))
    drafts = run("distill-semantic", "split", "分解")["proposals"]
    assert {draft["scope"] for draft in drafts} == {"仅适用于编辑模式", "仅适用于审核模式"}
    by_scope = {
        draft["scope"]: {
            ref["title"] for ref in run("review", str(draft["proposal_id"]))["evidence_refs"]
        }
        for draft in drafts
    }
    assert by_scope == {"仅适用于编辑模式": {"edit-a", "edit-b"}, "仅适用于审核模式": {"review"}}
    assert len(run("search-sources", "分解")["hits"]) == 8


def test_queue_keeps_stages_and_sources_separate_and_detects_same_fragment_conflict(
    tmp_path: Path, run_knowledge
) -> None:
    run = run_knowledge
    a = tmp_path / "game-a.md"
    b = tmp_path / "game-b.md"
    a.write_text(
        "第一晚是教学夜，整晚同一时间只接待四位。"
        "普通营业从第二晚开始能同时接待一位，第四晚增至两位，第八晚起最多三位。"
        "无尽模式第二晚解锁，起步就能同时接待一位。"
        "无尽模式第二晚解锁，起步就能同时接待两位。",
        encoding="utf-8",
    )
    b.write_text("无尽模式第二晚解锁，起步就能同时接待四位。", encoding="utf-8")
    run("import", str(a), str(b))
    drafts = run("distill-semantic", "game", "接待")["proposals"]
    assert len(drafts) == 4
    assert {draft["scope"] for draft in drafts} == {
        "教学首晚",
        "普通营业第二晚开始时",
        "无尽模式起步",
    }
    (conflict,) = [draft for draft in drafts if draft["status"] == "pending_conflict"]
    options = run("review", str(conflict["proposal_id"]))["conflict"]
    assert set(options) == {"一位", "两位"}
    for value, refs in options.items():
        assert [(ref["title"], ref["quote"]) for ref in refs] == [
            ("game-a", f"无尽模式第二晚解锁，起步就能同时接待{value}。")
        ]


def test_late_conflict_blocks_old_and_manual_drafts_and_requires_explicit_choice(
    tmp_path: Path, run_knowledge
) -> None:
    run = run_knowledge
    original, second, other = (tmp_path / f"{name}.md" for name in ("a", "b", "c"))
    friday = "样线 A（规则 v1）每周五维护。"
    saturday = "样线 A（规则 v1）的维护日是星期六。"
    sunday_v2 = "样线 A（规则 v2）每周日维护。"
    for path, content in ((original, friday), (second, saturday), (other, sunday_v2)):
        path.write_text(content, encoding="utf-8")
    run("import", str(original), str(other))
    (draft,) = run("distill-semantic", "line", "每周五维护")["proposals"]
    item = draft["item_id"]
    wrong_source = run("search-sources", "规则 v2")["hits"][0]["source_id"]
    manual = run(
        "propose",
        item,
        "--title",
        "样线 A（规则 v1）",
        "--statement",
        "新安排为周日。",
        "--source",
        str(wrong_source),
        "--evidence",
        "周日",
    )
    run("approve", str(manual["proposal_id"]), expected=4)
    run("import", str(second))
    for pending in (draft, manual):
        run("approve", str(pending["proposal_id"]), expected=4)
    (conflict,) = run("distill-semantic", "different-prefix", "每周五维护")["proposals"]
    assert (conflict["item_id"], conflict["status"]) == (item, "pending_conflict")
    conflict_id = str(conflict["proposal_id"])
    run("approve", str(manual["proposal_id"]), expected=4)
    run("approve", conflict_id, "--choose", "周日", expected=4)
    run("show", item, expected=3)
    third = tmp_path / "d.md"
    third.write_text(saturday, encoding="utf-8")
    run("import", str(third))
    run("approve", conflict_id, "--choose", "周六", expected=4)
    (refreshed,) = run("distill-semantic", "line", "每周五维护")["proposals"]
    resolution = str(refreshed["proposal_id"])
    assert resolution != conflict_id
    run("approve", conflict_id, "--choose", "周六", expected=4)
    assert len(run("review", conflict_id)["conflict"]["周六"]) == 1
    review = run("review", resolution)
    assert len(review["conflict"]["周六"]) == 2
    approved = run("approve", resolution, "--choose", "周六")
    assert approved["revision"] == 1
    assert run("approve", resolution, "--choose", "周六") == approved
    published = run("show", item, "--evidence")
    assert published["statement"].endswith("为周六。")
    assert {ref["title"] for ref in published["evidence_refs"]} == {"b", "d"}
    with closing(sqlite3.connect(tmp_path / "pilot.db")) as connection:
        (selected_id,) = connection.execute("SELECT proposal_id FROM revisions").fetchone()
    assert run("review", str(selected_id))["resolves_conflict"] == refreshed["proposal_id"]
    assert run("review", resolution)["approved_revision"] is None
    assert run("distill-semantic", "line", "每周五维护")["proposals"] == []
    third.write_text("样线 A（规则 v1）每周六维护。\n\n测试材料更新。", encoding="utf-8")
    run("import", str(third))
    (reopened,) = run("distill-semantic", "line", "每周五维护")["proposals"]
    assert reopened["status"] == "pending_conflict"
    assert run("show", item)["revision"] == 1


def test_approved_item_reopens_for_conflict_and_article_edit_cannot_promote_it(
    tmp_path: Path, run_knowledge
) -> None:
    run = run_knowledge
    note = tmp_path / "a.md"
    note.write_text("样线 A（规则 v1）每周五维护。", encoding="utf-8")
    run("import", str(note))
    (pending,) = run("distill-semantic", "line", "样线 A")["proposals"]
    markdown = run(
        "approve", str(pending["proposal_id"]), "--markdown-dir", str(tmp_path / "obsidian")
    )
    other = tmp_path / "other.md"
    other.write_text("样线 A（规则 v2）每周日维护。", encoding="utf-8")
    run("import", str(other))
    (other_pending,) = run("distill-semantic", "line", "规则 v2")["proposals"]
    other_statement = run("approve", str(other_pending["proposal_id"]))["statement"]
    article = tmp_path / "article-copy.md"
    original = Path(markdown["markdown"]).read_text(encoding="utf-8")
    article.write_text(original.replace(markdown["statement"], other_statement), encoding="utf-8")
    wrong_scope = run("propose-article", str(article), "--statement", other_statement)
    run("approve", str(wrong_scope["proposal_id"]), expected=4)
    note = tmp_path / "b.md"
    note.write_text("样线 A（规则 v1）每周六维护。", encoding="utf-8")
    run("import", str(note))
    correction = original.replace("周五", "周六")
    article.write_text(correction, encoding="utf-8")
    edit = run("propose-article", str(article), "--statement", correction.splitlines()[-1])
    run("approve", str(edit["proposal_id"]), expected=4)
    assert run("show", pending["item_id"])["revision"] == 1
    (conflict,) = run("distill-semantic", "line", "规则 v1")["proposals"]
    assert run("review", str(conflict["proposal_id"]))["base_revision"] == 1
    approved = run("approve", str(conflict["proposal_id"]), "--choose", "周六")
    assert approved["revision"] == 2


def test_v3_pending_semantic_drafts_require_a_fresh_review(tmp_path: Path, run_knowledge) -> None:
    run = run_knowledge
    note = tmp_path / "a.md"
    note.write_text("样线 A（规则 v1）每周五维护。", encoding="utf-8")
    run("import", str(note))
    (draft,) = run("distill-semantic", "line", "样线 A")["proposals"]
    with closing(sqlite3.connect(tmp_path / "pilot.db")) as connection, connection:
        connection.execute("DROP TABLE semantic_proposals")
        connection.execute("PRAGMA user_version = 3")
    run("approve", str(draft["proposal_id"]), expected=4)
    (fresh,) = run("distill-semantic", "renamed", "样线 A")["proposals"]
    assert fresh["item_id"] == draft["item_id"] and fresh["proposal_id"] != draft["proposal_id"]
    run("approve", str(draft["proposal_id"]), expected=4)
    assert run("approve", str(fresh["proposal_id"]))["revision"] == 1
