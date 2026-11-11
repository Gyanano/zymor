from __future__ import annotations

from pathlib import Path

import pytest

from zymor.config import Config
from zymor.errors import UsageError
from zymor.maintenance import forget_node
from zymor.model.node import new_node
from zymor.retrieval import RetrievalSettings, SearchFilters, memory_search
from zymor.store.index import Index
from zymor.store.nodes import read_node, update_node, write_node
from zymor.sync import (
    LocalRemote,
    SyncStore,
    build_plan,
    default_scope,
    handle_sync_request,
    remote_from_config,
    sync_vault,
)
from zymor.util.fs import sha256_hex
from zymor.vault import Vault
from zymor.write_service import memory_log


@pytest.fixture
def server(tmp_path: Path) -> Vault:
    return Vault.init(tmp_path / "server", "global", "server")


@pytest.fixture
def laptop(tmp_path: Path) -> Vault:
    return Vault.init(tmp_path / "laptop", "global", "laptop")


@pytest.fixture
def desktop(tmp_path: Path) -> Vault:
    return Vault.init(tmp_path / "desktop", "global", "desktop")


def _add(vault: Vault, title: str) -> str:
    node = new_node(
        title=title,
        summary=f"{title} 摘要。",
        category="semantic",
        layer="L3",
        body=f"# {title}\n\n## 结论\n{title} 的结论。\n\n## 证据\n测试。\n",
        source="session:test",
        created_by="agent",
    )
    write_node(vault, node)
    Index(vault.root).sync()
    return node.id


def _rel(vault: Vault, node_id: str) -> str:
    _, path = read_node(vault, node_id)
    return path.relative_to(vault.root).as_posix()


def test_push_uploads_only_what_changed(server: Vault, laptop: Vault) -> None:
    remote = LocalRemote(server)
    first, second = _add(laptop, "中文检索用 bigram"), _add(laptop, "迁移脚本要可回滚")

    assert len(sync_vault(laptop, remote, action="status").plan.upload) == 2
    assert SyncStore.for_vault(server).manifest("global") == {}  # status 不改任何东西

    assert len(sync_vault(laptop, remote, action="push").uploaded) == 2
    assert sync_vault(laptop, remote, action="push").uploaded == []

    update_node(laptop, first, {"summary": "改过的摘要。"})
    report = sync_vault(laptop, remote, action="push")
    assert report.uploaded == [_rel(laptop, first)]
    assert len(SyncStore.for_vault(server).manifest("global")) == 2
    assert second  # 第二个节点没有被重复上传
    # 副本放在独立区域，不并入服务器自己的库。
    assert not list(server.nodes_dir.rglob("mem_*.md"))


def test_two_machines_converge_through_the_remote(
    server: Vault, laptop: Vault, desktop: Vault
) -> None:
    remote = LocalRemote(server)
    from_laptop = _add(laptop, "中文检索用 bigram 预分词")
    sync_vault(laptop, remote)

    report = sync_vault(desktop, remote)
    assert report.downloaded == [_rel(laptop, from_laptop)]
    hits = memory_search(
        [("global", desktop)],
        "bigram",
        settings=RetrievalSettings.from_config(Config()),
        filters=SearchFilters(),
        record_audit=False,
    ).hits
    assert [hit.id for hit in hits] == [from_laptop]  # 下载后索引已更新，可直接检索

    from_desktop = _add(desktop, "提交前先跑测试")
    assert sync_vault(desktop, remote).uploaded == [_rel(desktop, from_desktop)]
    assert sync_vault(laptop, remote).downloaded == [_rel(desktop, from_desktop)]
    assert sync_vault(laptop, remote, action="status").plan.to_dict()["in_sync"] == 2


def test_conflict_keeps_both_sides_until_forced(
    server: Vault, laptop: Vault, desktop: Vault
) -> None:
    remote = LocalRemote(server)
    node_id = _add(laptop, "默认用中文回复")
    sync_vault(laptop, remote)
    sync_vault(desktop, remote)

    update_node(laptop, node_id, {"summary": "笔记本上的版本。"})
    update_node(desktop, node_id, {"summary": "台式机上的版本。"})
    sync_vault(laptop, remote)

    blocked = sync_vault(desktop, remote)
    assert blocked.uploaded == [] and blocked.downloaded == []
    assert any("两边都改过" in warning for warning in blocked.warnings)
    assert read_node(desktop, node_id)[0].summary == "台式机上的版本。"

    sync_vault(desktop, remote, action="pull", force=True)
    assert read_node(desktop, node_id)[0].summary == "笔记本上的版本。"
    assert sync_vault(desktop, remote, action="status").plan.conflicts == []


def test_deletion_propagates_but_lost_files_are_restored(
    server: Vault, laptop: Vault, desktop: Vault
) -> None:
    remote = LocalRemote(server)
    forgotten, edited, kept = (
        _add(laptop, "要删除的条目"),
        _add(laptop, "删后又改的条目"),
        _add(laptop, "保留的条目"),
    )
    sync_vault(laptop, remote)
    sync_vault(desktop, remote)
    forgotten_path, edited_path, kept_path = (
        _rel(laptop, item) for item in (forgotten, edited, kept)
    )

    # 笔记本主动删除两个条目；台式机在同步前改了其中一个。
    forget_node(laptop, forgotten)
    forget_node(laptop, edited)
    assert sorted(sync_vault(laptop, remote).deleted_remote) == sorted(
        [forgotten_path, edited_path]
    )
    update_node(desktop, edited, {"summary": "台式机还在用它。"})

    report = sync_vault(desktop, remote)
    assert report.deleted_local == [forgotten_path]
    assert desktop.find_node_path(forgotten) is None
    assert report.uploaded == [edited_path]  # 本地改过的不删，传回远端
    assert sync_vault(laptop, remote).downloaded == [edited_path]

    # 服务器丢了文件（没有删除标记）：本地是事实源，重新上传。
    (SyncStore.for_vault(server).root / "global" / kept_path).unlink()
    assert sync_vault(laptop, remote).uploaded == [kept_path]
    assert laptop.find_node_path(kept) is not None


def test_events_are_backed_up_but_never_pulled(
    server: Vault, laptop: Vault, desktop: Vault
) -> None:
    remote = LocalRemote(server)
    memory_log(laptop, session_id="s1", kind="user_message", content="原始对话")
    assert sync_vault(laptop, remote).uploaded == []  # 默认只同步知识节点
    uploaded = sync_vault(laptop, remote, include_events=True).uploaded
    assert len(uploaded) == 1 and uploaded[0].startswith("L0/")

    memory_log(laptop, session_id="s1", kind="user_message", content="第二条")
    assert sync_vault(laptop, remote, include_events=True).uploaded == uploaded

    pulled = sync_vault(desktop, remote, include_events=True)
    assert pulled.downloaded == [] and not list(desktop.l0_dir.glob("*.jsonl"))


def test_server_rejects_unsafe_requests(server: Vault, laptop: Vault) -> None:
    def push(path: str, content: str = "x", scope: str = "global") -> int:
        payload = {
            "action": "push",
            "scope": scope,
            "files": [{"path": path, "content": content, "sha256": sha256_hex(content)}],
        }
        return handle_sync_request(server, payload)[0]

    good = "nodes/semantic/mem_20261111_0123456789.md"
    assert push(good) == 200
    for bad in (
        "../outside.md",
        "nodes/../../vault.json",
        "/etc/passwd",
        "nodes/semantic/note.md",
        "vault.json",
        "nodes/semantic/mem_20261111_0123456789.md/../../x",
    ):
        assert push(bad) == 400, bad
    assert push(good, scope="../other") == 400
    assert handle_sync_request(server, {"action": "manifest", "scope": ""})[0] == 400
    assert handle_sync_request(server, {"action": "drop"})[0] == 400
    assert handle_sync_request(server, ["x"])[0] == 400
    mismatch = {
        "action": "push",
        "scope": "global",
        "files": [{"path": good, "content": "x", "sha256": "0"}],
    }
    assert handle_sync_request(server, mismatch)[0] == 400
    assert push(good) == 200
    assert (
        handle_sync_request(
            server, {"action": "delete", "scope": "global", "paths": [good]}, writable=False
        )[0]
        == 403
    )
    assert (
        handle_sync_request(server, {"action": "manifest", "scope": "global"}, writable=False)[0]
        == 200
    )

    # 远端给出损坏内容时，客户端跳过它，不写进本地库。
    report = sync_vault(laptop, LocalRemote(server))
    assert report.downloaded == [] and any("已跳过" in warning for warning in report.warnings)
    assert not list(laptop.nodes_dir.rglob("mem_*.md"))


def test_plan_scope_and_remote_config(
    laptop: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_plan(
        local={"a": "1", "b": "2", "c": "3", "e": "5", "g": "7"},
        remote={"a": "1", "b": "x", "d": "4", "e": "y", "f": "6"},
        base={"b": "x", "e": "0", "f": "6", "g": "7"},
        deleted={"g"},
    )
    assert plan.in_sync == ["a"]
    assert plan.upload == ["b", "c"]
    assert plan.download == ["d"]
    assert plan.conflicts == ["e"]
    assert plan.delete_remote == ["f"]
    assert plan.remote_deleted == ["g"]

    assert default_scope(laptop) == "global"
    project = Vault.init(tmp_path / "p" / ".zymor" / "vault", "project", "My App/前端")
    assert default_scope(project) == "project-My-App"

    config = Config()
    with pytest.raises(UsageError, match="未配置同步服务器地址"):
        remote_from_config(config)
    config.data["sync"]["url"] = "http://memory.example.com"
    with pytest.raises(UsageError, match="必须使用 https"):
        remote_from_config(config)
    config.data["sync"]["url"] = "https://memory.example.com/"
    monkeypatch.delenv("ZYMOR_SYNC_TOKEN", raising=False)
    with pytest.raises(UsageError, match="未配置同步 token"):
        remote_from_config(config)
    monkeypatch.setenv("ZYMOR_SYNC_TOKEN", "t" * 32)
    assert remote_from_config(config).url == "https://memory.example.com"
