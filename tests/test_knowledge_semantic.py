import json
from pathlib import Path

import pytest

from zymor.cli import main


def test_semantic_proposals_require_review_and_conflicts_keep_both_sources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def run(*args: str, expected: int = 0):
        status = main(["--json", "knowledge", "--db", str(tmp_path / "pilot.db"), *args])
        output = capsys.readouterr()
        assert status == expected, output
        return json.loads(output.out)

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
