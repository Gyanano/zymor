from __future__ import annotations

from pathlib import Path

import pytest

from zymor.assembly import memory_context
from zymor.bundle import export_bundle, import_bundle
from zymor.capture import FileChange, capture_changes
from zymor.config import Config
from zymor.harness import LocalHarness
from zymor.invalidation import file_anchor
from zymor.model.event import read_events
from zymor.model.node import MemoryNode, new_node
from zymor.panel import overview_payload, search_payload
from zymor.promotion import adopt_global, promote_to_global
from zymor.retrieval import (
    RetrievalSettings,
    SearchFilters,
    memory_read,
    memory_search,
)
from zymor.rule_checks import check_rules
from zymor.store.nodes import read_node, update_node, write_node
from zymor.vault import Vault


@pytest.fixture
def vaults(tmp_path: Path) -> tuple[Vault, Vault]:
    project = Vault.init(tmp_path / "project" / ".zymor" / "vault", "project", "demo")
    global_vault = Vault.init(tmp_path / "global", "global", "global")
    return project, global_vault


def _body(title: str, extra: str = "") -> str:
    return f"# {title}\n\n## 结论\n{title} 的结论。\n\n## 证据\n最小测试。\n{extra}"


def _node(title: str, **overrides: object) -> MemoryNode:
    fields: dict[str, object] = {
        "title": title,
        "summary": f"{title} 摘要。",
        "category": "semantic",
        "layer": "L3",
        "body": _body(title),
        "source": "session:smoke",
        "created_by": "agent",
    }
    fields.update(overrides)
    return new_node(**fields)


def test_write_search_read_and_context(vaults: tuple[Vault, Vault]) -> None:
    project, global_vault = vaults
    node = _node("缓存失效策略", tags=["cache"])
    write_node(project, node)
    settings = RetrievalSettings.from_config(Config())
    result = memory_search([("project", project)], "缓存", settings=settings)
    assert [hit.id for hit in result.hits] == [node.id]
    read = memory_read([("project", project)], node.id)
    assert read.node.title == "缓存失效策略"
    context = memory_context(
        project_vault=project,
        global_vault=global_vault,
        config=Config(),
    )
    assert "Zymor Memory" in context.text


def test_stale_anchor_is_excluded_by_default(vaults: tuple[Vault, Vault], tmp_path: Path) -> None:
    project, _ = vaults
    path = tmp_path / "project" / "demo.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("VALUE = 1\n", encoding="utf-8")
    node = _node("路径与边界条件", anchors=[file_anchor(project, path)])
    write_node(project, node)
    path.write_text("VALUE = 2\n", encoding="utf-8")
    settings = RetrievalSettings.from_config(Config())
    hidden = memory_search([("project", project)], "路径", settings=settings)
    assert hidden.hits == []
    visible = memory_search(
        [("project", project)],
        "路径",
        settings=settings,
        filters=SearchFilters(include_stale=True),
    )
    assert visible.hits and visible.hits[0].stale


def test_rule_check(vaults: tuple[Vault, Vault]) -> None:
    project, _ = vaults
    rule = _node(
        "配置规则",
        status="verified",
        applies_to=["coding"],
        evidence=["review:smoke"],
        body=_body(
            "配置规则",
            "\n## 检查\nconfig.py | contains | timeout_ms = 30000\n",
        ),
    )
    write_node(project, rule)
    passed = check_rules([("project", project)], {"config.py": "timeout_ms = 30000\n"})
    assert passed.passed
    failed = check_rules([("project", project)], {"config.py": "timeout_ms = 1\n"})
    assert not failed.passed


def test_cross_project_promotion(vaults: tuple[Vault, Vault]) -> None:
    project, global_vault = vaults
    node = _node(
        "跨项目调试结论",
        status="verified",
        confidence=0.9,
        applies_to=["coding"],
        tags=["cross-project"],
        evidence=["review:smoke"],
    )
    write_node(project, node)
    update_node(project, node.id, {"verify_count": 3, "hit_count": 3}, command="smoke")
    promoted = promote_to_global(project, global_vault, Config(), node.id, confirm=True, force=True)
    assert promoted.status == "promoted"
    assert promoted.global_id is not None
    global_node, _ = read_node(global_vault, promoted.global_id)
    assert global_node.projects == ["demo"]
    adopted = adopt_global(global_vault, "second-project", promoted.global_id)
    assert adopted.projects == ["demo", "second-project"]
    assert "cross-project-verified" in adopted.tags


def test_bundle_roundtrip(vaults: tuple[Vault, Vault], tmp_path: Path) -> None:
    project, _ = vaults
    node = _node("可迁移结论")
    write_node(project, node)
    bundle = tmp_path / "bundle"
    exported = export_bundle(project, bundle)
    assert exported["node_count"] == 1
    target = Vault.init(tmp_path / "target" / ".zymor" / "vault", "project", "target")
    imported = import_bundle(target, bundle)
    assert imported["imported"] == 1
    restored, _ = read_node(target, node.id)
    assert restored.title == "可迁移结论"


def test_capture_panel_and_harness(vaults: tuple[Vault, Vault], tmp_path: Path) -> None:
    project, _ = vaults
    captured = capture_changes(
        project,
        [FileChange("create", "smoke.py")],
        session_id="smoke-capture",
    )
    assert captured["captured"] == 1
    events = [event for event in read_events(project.l0_dir) if event.session_id == "smoke-capture"]
    assert events[0].meta["source"] == "capture"
    node = _node("面板检索", summary="面板基础检索。")
    write_node(project, node)
    config = Config()
    config.data["vault"]["global_path"] = str(tmp_path / "missing-global")
    overview = overview_payload(project)
    assert overview["counts"]["total"] == 1
    hits = search_payload(project, config, "面板")
    assert hits and hits[0]["id"] == node.id
    rule = _node(
        "Harness 配置规则",
        status="verified",
        applies_to=["coding"],
        evidence=["review:smoke"],
        body=_body(
            "Harness 配置规则",
            "\n## 检查\nconfig.py | contains | timeout_ms = 30000\n",
        ),
    )
    write_node(project, rule)
    report = LocalHarness().generation(
        project,
        content={"config.py": "timeout_ms = 1\n"},
        task_type="coding",
        config=config,
    )
    assert report["next_action"] == "repair"
    assert report["checked_rules"] == 1
