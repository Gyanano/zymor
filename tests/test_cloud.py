from __future__ import annotations

import io
import json
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from zymor import remote as remote_module
from zymor.autodream import AutoDreamer
from zymor.cli import main
from zymor.config import Config
from zymor.connect import install_claude_code_hooks, save_connection, set_toml_values
from zymor.dream.engine import run_dream
from zymor.dream.types import DreamClients
from zymor.errors import UsageError, ValidationError
from zymor.ingest import ingest_transcript
from zymor.llm import LlmResponse
from zymor.mcp_server import context_request, ingest_payload, recall_request
from zymor.model.event import read_events
from zymor.model.node import new_node
from zymor.recall import recall
from zymor.remote import (
    RemoteError,
    RemoteSettings,
    project_name,
    recall_for_prompt,
    upload_hook_payload,
)
from zymor.store.index import Index
from zymor.store.nodes import write_node
from zymor.sync import LocalRemote, handle_sync_request, mirror_vault
from zymor.vault import Vault

URL = "https://memory.example.com"


@pytest.fixture
def server(tmp_path: Path) -> Vault:
    return Vault.init(tmp_path / "server", "global", "server")


class FakeServer:
    """把客户端的 HTTP 调用直接交给服务端处理函数，不开端口。"""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault
        self.config = Config()
        self.down = False
        self.calls: list[str] = []

    def __call__(self, url: str, token: str, payload: Any, **_: Any) -> dict[str, Any]:
        path = url.removeprefix(URL)
        self.calls.append(path)
        if self.down:
            raise RemoteError("无法连接服务器")
        if token != "secret-token":
            raise RemoteError("服务器拒绝了请求（HTTP 401）")
        if path == "/healthz":
            return {"status": "ok"}
        handlers = {
            "/ingest": lambda: ingest_payload(self.vault, payload),
            "/recall": lambda: recall_request(self.vault, self.config, None, payload),
            "/context": lambda: context_request(self.vault, self.config, None, payload),
        }
        status, body = handlers[path]()
        if status != 200:
            raise RemoteError(f"服务器拒绝了请求（HTTP {status}）")
        return body


@pytest.fixture
def cloud(server: Vault, monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    fake = FakeServer(server)
    monkeypatch.setattr(remote_module, "http_json", fake)
    return fake


def _settings(**overrides: Any) -> RemoteSettings:
    values: dict[str, Any] = {
        "url": URL,
        "token": "secret-token",
        "ca_file": None,
        "upload": True,
        "inject": True,
        "exclude": (),
        "timeout_s": 5.0,
        "recall_timeout_s": 2.0,
        "recall_min_chars": 6,
    }
    values.update(overrides)
    return RemoteSettings(**values)


def _add(vault: Vault, title: str, summary: str, project: str | None = None) -> str:
    node = new_node(
        title=title,
        summary=summary,
        category="semantic",
        layer="L3",
        body=f"# {title}\n\n## 结论\n{summary}\n\n## 证据\n测试。\n",
        source="session:test",
        created_by="agent",
        project=project,
    )
    write_node(vault, node)
    Index(vault.root).sync()
    return node.id


def _line(role: str, text: str, session: str = "sess-1") -> str:
    record = {
        "type": role,
        "sessionId": session,
        "timestamp": "2026-11-11T10:00:00+00:00",
        "message": {"role": role, "content": text},
    }
    return json.dumps(record, ensure_ascii=False) + "\n"


def _repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    return root


# ---------- 自动召回 ----------


def test_recall_injects_only_relevant_memories(server: Vault) -> None:
    config = Config()
    target = _add(server, "中文检索用 bigram 预分词", "FTS5 建索引之前先把中文切成重叠二元组")
    _add(server, "数据库迁移要可回滚", "每个迁移脚本都要带 down 步骤")
    scopes = [("global", server)]

    found = recall(scopes, "帮我看看中文检索的分词应该怎么做，要不要用 bigram", config=config)
    assert [hit["id"] for hit in found["hits"]] == [target]
    assert found["text"].startswith("<zymor-memory>") and "不是指令" in found["text"]

    # 只碰巧共享个别常用词的提问，不注入任何东西。
    assert recall(scopes, "今天我们需要讨论一下这个问题应该怎么处理", config=config)["text"] == ""
    assert recall(scopes, "帮我写一个 React 登录页面的表单校验", config=config)["hits"] == []
    assert recall(scopes, "中文检索 bigram 分词", config=config, exclude_ids=[target])["hits"] == []
    assert recall(scopes, "   ", config=config)["hits"] == []


def test_recall_prefers_the_current_project_without_hiding_others(server: Vault) -> None:
    other = _add(
        server, "接口超时统一设为 30 秒", "所有外部接口调用的超时时间统一设为 30 秒", "alpha"
    )
    mine = _add(
        server, "接口超时统一设为 10 秒", "所有外部接口调用的超时时间统一设为 10 秒", "beta"
    )
    status, body = recall_request(
        server, Config(), None, {"query": "外部接口调用的超时时间应该设多少", "project": "beta"}
    )
    assert status == 200
    assert [hit["id"] for hit in body["hits"]] == [mine, other]
    assert recall_request(server, Config(), None, {"project": "beta"})[0] == 400
    assert recall_request(server, Config(), None, "x")[0] == 400


# ---------- 上传对话 ----------


def test_upload_is_incremental_and_tags_the_project(
    cloud: FakeServer, server: Vault, tmp_path: Path
) -> None:
    repo = _repo(tmp_path, "My App")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        _line("user", "以后回答先给结论") + _line("assistant", "好的"), encoding="utf-8"
    )
    hook = {"transcript_path": str(transcript), "session_id": "sess-1", "cwd": str(repo / "src")}

    first = upload_hook_payload(_settings(), hook)
    assert first is not None and first["uploaded"] == 2 and not first["pending"]
    events = read_events(server.l0_dir)
    assert [event.kind for event in events] == ["user_message", "assistant_message"]
    assert {event.meta.get("project") for event in events} == {"My-App"}
    assert project_name(repo / "src") == "My-App"

    second = upload_hook_payload(_settings(), hook)
    assert second is not None and second["uploaded"] == 0
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(_line("user", "第二轮提问"))
    third = upload_hook_payload(_settings(), hook)
    assert third is not None and third["uploaded"] == 1
    assert len(read_events(server.l0_dir)) == 3


def test_failed_upload_is_retried_without_loss_or_duplicates(
    cloud: FakeServer, server: Vault, tmp_path: Path
) -> None:
    repo = _repo(tmp_path, "app")
    offline = tmp_path / "offline.jsonl"
    offline.write_text(_line("user", "断网时说的话", "sess-off"), encoding="utf-8")
    later = tmp_path / "later.jsonl"
    later.write_text(_line("user", "联网后说的话", "sess-on"), encoding="utf-8")

    cloud.down = True
    failed = upload_hook_payload(
        _settings(), {"transcript_path": str(offline), "session_id": "sess-off", "cwd": str(repo)}
    )
    assert failed is not None and failed["pending"] and failed["uploaded"] == 0
    assert read_events(server.l0_dir) == []

    cloud.down = False
    recovered = upload_hook_payload(
        _settings(), {"transcript_path": str(later), "session_id": "sess-on", "cwd": str(repo)}
    )
    # 当前会话传完后，顺带把之前没传成功的会话补传。
    assert recovered is not None and recovered["uploaded"] == 1 and recovered["retried"] == 1
    contents = sorted(event.content for event in read_events(server.l0_dir))
    assert contents == ["断网时说的话", "联网后说的话"]

    again = upload_hook_payload(
        _settings(), {"transcript_path": str(later), "session_id": "sess-on", "cwd": str(repo)}
    )
    assert again is not None and again["uploaded"] == 0 and again["retried"] == 0
    assert len(read_events(server.l0_dir)) == 2


def test_excluded_projects_never_leave_the_machine(
    cloud: FakeServer, server: Vault, tmp_path: Path
) -> None:
    work = _repo(tmp_path, "work")
    transcript = tmp_path / "w.jsonl"
    transcript.write_text(_line("user", "公司项目里的对话"), encoding="utf-8")
    hook = {
        "transcript_path": str(transcript),
        "session_id": "s",
        "cwd": str(work / "src"),
        "prompt": "公司项目里的接口超时时间应该设多少",
    }
    for settings in (_settings(exclude=(str(work),)), _settings(exclude=(str(tmp_path / "wor*"),))):
        assert upload_hook_payload(settings, hook) is None
        assert recall_for_prompt(settings, hook) == ""
    assert upload_hook_payload(_settings(upload=False), hook) is None
    assert cloud.calls == [] and read_events(server.l0_dir) == []


def test_prompt_recall_skips_noise_and_does_not_repeat(
    cloud: FakeServer, server: Vault, tmp_path: Path
) -> None:
    target = _add(server, "中文检索用 bigram 预分词", "FTS5 建索引之前先把中文切成重叠二元组")
    repo = _repo(tmp_path, "app")
    base = {"session_id": "sess-9", "cwd": str(repo)}
    prompt = "中文检索的分词要不要用 bigram"

    text = recall_for_prompt(_settings(), {**base, "prompt": prompt})
    assert target in text
    assert recall_for_prompt(_settings(), {**base, "prompt": prompt}) == ""  # 同一会话不重复注入
    assert target in recall_for_prompt(_settings(), {"session_id": "sess-10", "prompt": prompt})

    calls = len(cloud.calls)
    for skipped in ("/clear", "好的", 42):
        assert recall_for_prompt(_settings(), {**base, "prompt": skipped}) == ""
    assert recall_for_prompt(_settings(inject=False), {**base, "prompt": prompt}) == ""
    assert len(cloud.calls) == calls  # 这些情况不发请求


# ---------- 从上传到召回的完整链路 ----------


class _Extract:
    def complete_json(
        self, messages: list[dict[str, str]], *, caller: str = "", temperature: float | None = None
    ) -> tuple[Any, LlmResponse]:
        prompt = messages[-1]["content"]
        session = "tool-a" if "l0:tool-a:" in prompt or "tool-a" in prompt else "tool-b"
        candidate = {
            "title": "接口超时统一设为 30 秒",
            "summary": f"所有外部接口调用的超时时间统一设为 30 秒（来自 {session}）",
            "body": "## 结论\n超时统一 30 秒。\n\n## 证据\n对话。",
            "category": "semantic",
            "layer": "L3",
            "source_event_ids": ["1"],
        }
        response = LlmResponse(
            text="{}",
            role="extract",
            model="fake",
            tokens_in=1,
            tokens_out=1,
            usd_estimate=0.0,
            latency_ms=1.0,
            priced=False,
        )
        return {"candidates": [candidate]}, response


def test_two_tools_feed_one_memory_that_is_recalled_later(
    cloud: FakeServer, server: Vault, tmp_path: Path
) -> None:
    repo = _repo(tmp_path, "shop")
    clients = DreamClients(clients={"extract": _Extract()})
    for session in ("tool-a", "tool-b"):
        transcript = tmp_path / f"{session}.jsonl"
        transcript.write_text(
            _line("user", f"{session}: 接口超时设成 30 秒", session), encoding="utf-8"
        )
        upload_hook_payload(
            _settings(),
            {"transcript_path": str(transcript), "session_id": session, "cwd": str(repo)},
        )
        report = run_dream(server, cloud.config, phases=("nrem",), clients=clients)
        assert report.ok
    # 第一次只进待定池，第二个工具里再次出现后才成为节点，并带上来源项目。
    assert report.phases[0].nodes_created == 1
    rows = Index(server.root).layer_rows("L3", statuses=["hypothesis"], limit=10)
    assert [row["project"] for row in rows] == ["shop"]

    text = recall_for_prompt(
        _settings(),
        {"session_id": "new", "cwd": str(repo), "prompt": "外部接口调用的超时时间设多少"},
    )
    assert "接口超时统一设为 30 秒" in text


def test_ingest_drops_injected_memory_blocks(server: Vault, tmp_path: Path) -> None:
    transcript = tmp_path / "loop.jsonl"
    transcript.write_text(
        _line("user", "<zymor-memory>\n- [verified] 旧记忆\n</zymor-memory>\n真正的提问"),
        encoding="utf-8",
    )
    ingest_transcript(server, transcript)
    assert [event.content for event in read_events(server.l0_dir)] == ["真正的提问"]


# ---------- 自动做梦 ----------


def test_auto_dream_fires_once_per_threshold_and_respects_limits(
    server: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[tuple[str, ...]] = []

    class _Report:
        ok = True
        errors: list[str] = []

    def fake_run(vault: Vault, config: Config, *, phases: tuple[str, ...]) -> _Report:
        runs.append(phases)
        return _Report()

    monkeypatch.setattr("zymor.dream.engine.run_dream", fake_run)
    config = Config()
    assert AutoDreamer(server, config).threshold == 200  # 默认开启
    config.data["dream"]["auto_after_ingest_events"] = 0
    assert AutoDreamer(server, config).notify(10_000) is False  # 设为 0 关闭

    config.data["dream"]["auto_after_ingest_events"] = 5
    dreamer = AutoDreamer(server, config)
    assert dreamer.notify(3) is False
    assert dreamer.notify(2) is True
    deadline = time.monotonic() + 5
    while dreamer._running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runs == [("nrem", "arbitration")]
    assert dreamer.notify(50) is False  # 距上次不足最小间隔

    config.data["dream"]["auto_min_interval_minutes"] = 0
    config.data["dream"]["auto_max_runs_per_day"] = 1
    capped = AutoDreamer(server, config)
    assert capped.notify(5) is True
    while capped._running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert capped.notify(5) is False  # 达到每日上限


# ---------- 接入 ----------


def test_connect_saves_settings_without_clobbering(isolated_home: Path) -> None:
    text = (
        '# 我的配置\n[llm]\nbase_url = "https://x"\n\n'
        '[remote]\n# 备注\nurl = "https://old"\nupload = false\n'
    )
    updated = set_toml_values(text, "remote", {"url": URL, "ca_file": "/c.pem"})
    assert "# 我的配置" in updated and 'base_url = "https://x"' in updated and "# 备注" in updated
    assert f'url = "{URL}"' in updated and "https://old" not in updated
    assert "upload = false" in updated and 'ca_file = "/c.pem"' in updated
    assert set_toml_values("", "remote", {"url": URL}) == f'[remote]\nurl = "{URL}"\n'

    saved = save_connection(URL, "secret-token")
    token_file = Path(saved["token_file"])
    assert token_file.read_text(encoding="utf-8").strip() == "secret-token"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert str(isolated_home) in saved["config"]

    from zymor.config import load_config

    settings = RemoteSettings.from_config(load_config())
    assert settings is not None and settings.url == URL and settings.token == "secret-token"
    assert RemoteSettings.from_config(Config()) is None
    insecure = Config()
    insecure.data["remote"]["url"] = "http://memory.example.com"
    with pytest.raises(UsageError, match="必须使用 https"):
        RemoteSettings.from_config(insecure)


def test_claude_code_hooks_are_merged_idempotently(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    original = {
        "model": "x",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]},
    }
    settings.write_text(json.dumps(original), encoding="utf-8")

    first = install_claude_code_hooks(settings, "/usr/local/bin/zymor")
    assert first["added"] == [
        "SessionStart",
        "UserPromptSubmit",
        "Stop",
        "PreCompact",
        "SessionEnd",
    ]
    assert json.loads(Path(first["backup"]).read_text(encoding="utf-8")) == original
    merged = json.loads(settings.read_text(encoding="utf-8"))
    assert merged["model"] == "x"
    stop_commands = [
        item["command"] for group in merged["hooks"]["Stop"] for item in group["hooks"]
    ]
    assert stop_commands == ["say done", "/usr/local/bin/zymor hook capture"]
    assert merged["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("hook recall")

    second = install_claude_code_hooks(settings, "/usr/local/bin/zymor")
    assert second["added"] == [] and second["backup"] is None
    assert json.loads(settings.read_text(encoding="utf-8")) == merged

    fresh = install_claude_code_hooks(tmp_path / "new" / "settings.json", "zymor")
    assert len(fresh["added"]) == 5 and fresh["backup"] is None
    settings.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationError):
        install_claude_code_hooks(settings, "zymor")


def test_cli_hooks_use_the_server_and_never_fail_the_host(
    cloud: FakeServer,
    server: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = _add(server, "中文检索用 bigram 预分词", "FTS5 建索引之前先把中文切成重叠二元组")
    repo = _repo(tmp_path, "app")
    monkeypatch.setenv("ZYMOR_REMOTE__URL", URL)
    monkeypatch.setenv("ZYMOR_REMOTE_TOKEN", "secret-token")
    monkeypatch.chdir(repo)

    def run(argv: list[str], stdin: dict[str, Any] | str) -> str:
        text = stdin if isinstance(stdin, str) else json.dumps(stdin)
        monkeypatch.setattr("sys.stdin", io.StringIO(text))
        assert main(argv) == 0
        return capsys.readouterr().out

    out = run(["hook", "recall"], {"prompt": "中文检索的分词要不要用 bigram", "cwd": str(repo)})
    assert target in out and "<zymor-memory>" in out
    assert run(["hook", "recall"], {"prompt": "帮我写个登录页面", "cwd": str(repo)}) == ""

    transcript = tmp_path / "cli.jsonl"
    transcript.write_text(_line("user", "偏好中文回复"), encoding="utf-8")
    hook = {"transcript_path": str(transcript), "session_id": "sess-1", "cwd": str(repo)}
    assert json.loads(run(["hook", "capture", "--json"], hook))["uploaded"] == 1
    assert [event.content for event in read_events(server.l0_dir)] == ["偏好中文回复"]

    assert main(["hook", "context"]) == 0
    assert "/context" in cloud.calls
    capsys.readouterr()

    # 服务器不可用、输入损坏、token 缺失：都安静地退出 0，不输出内容。
    cloud.down = True
    assert run(["hook", "recall"], {"prompt": "中文检索的分词要不要用 bigram"}) == ""
    assert run(["hook", "capture"], hook) == ""
    assert main(["hook", "context"]) == 0 and capsys.readouterr().out == ""
    assert run(["hook", "recall"], "not json") == ""
    monkeypatch.delenv("ZYMOR_REMOTE_TOKEN")
    assert run(["hook", "recall"], {"prompt": "中文检索的分词要不要用 bigram"}) == ""


# ---------- 本地副本 ----------


def test_mirror_downloads_the_server_vault_read_only(server: Vault, tmp_path: Path) -> None:
    first = _add(server, "条目一", "第一条知识的摘要")
    second = _add(server, "条目二", "第二条知识的摘要")
    target = tmp_path / "mirror"
    remote = LocalRemote(server)

    initial = mirror_vault(remote, target)
    assert len(initial["downloaded"]) == 2 and initial["total"] == 2
    assert mirror_vault(remote, target)["downloaded"] == []

    (server.find_node_path(second) or Path()).unlink()
    pruned = mirror_vault(remote, target)
    assert len(pruned["deleted"]) == 1 and pruned["total"] == 1
    assert [path.stem for path in target.rglob("mem_*.md")] == [first]

    blocked = {"action": "push", "scope": "@vault", "files": []}
    assert handle_sync_request(server, blocked)[0] == 403
    bad = {"action": "pull", "scope": "@vault", "paths": ["../vault.json"]}
    assert handle_sync_request(server, bad)[0] == 400


def test_entry_point_fast_path_and_local_fallback(
    cloud: FakeServer,
    server: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from zymor.__main__ import main as entry

    target = _add(server, "中文检索用 bigram 预分词", "FTS5 建索引之前先把中文切成重叠二元组")
    payload = json.dumps({"prompt": "中文检索的分词要不要用 bigram", "session_id": "fast-1"})

    # 没配置服务器：快速路径把已读入的输入交给完整 CLI，在本地库里检索。
    local = Vault.init(tmp_path / "proj" / ".zymor" / "vault", "project", "proj")
    local_id = _add(local, "中文检索用 bigram 预分词", "本地库里的同名条目，切成重叠二元组")
    monkeypatch.chdir(tmp_path / "proj")
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert entry(["hook", "recall"]) == 0
    assert local_id in capsys.readouterr().out and cloud.calls == []

    # 配置了服务器：不加载完整 CLI，直接向服务器取。
    monkeypatch.setenv("ZYMOR_REMOTE__URL", URL)
    monkeypatch.setenv("ZYMOR_REMOTE_TOKEN", "secret-token")
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert entry(["hook", "recall"]) == 0
    out = capsys.readouterr().out
    assert target in out and local_id not in out

    monkeypatch.setattr("sys.stdin", io.StringIO("{broken"))
    assert entry(["hook", "recall"]) == 0 and capsys.readouterr().out == ""
