from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from zymor.cli import main
from zymor.config import Config
from zymor.dream.engine import run_dream
from zymor.dream.nrem import _Candidate, _candidates_overlap, _recall_pending
from zymor.dream.pending import PendingPool, pending_path
from zymor.dream.types import DreamBudget, DreamClients
from zymor.embeddings import build_embeddings, hybrid_search
from zymor.errors import UsageError
from zymor.ingest import ingest_hook_payload, ingest_transcript
from zymor.llm import LlmResponse, resolve_role
from zymor.mcp_server import (
    allowed_hosts,
    ingest_payload,
    resolve_http_settings,
    token_matches,
)
from zymor.model.event import read_events
from zymor.model.node import new_node
from zymor.retrieval import RetrievalSettings, SearchFilters, memory_search
from zymor.store.index import Index
from zymor.store.nodes import write_node
from zymor.vault import Vault
from zymor.write_service import memory_log


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return Vault.init(tmp_path / "project" / ".zymor" / "vault", "project", "demo")


def _write(vault: Vault, title: str, summary: str) -> str:
    node = new_node(
        title=title,
        summary=summary,
        category="semantic",
        layer="L3",
        body=f"# {title}\n\n## 结论\n{summary}\n\n## 证据\n测试。\n",
        source="session:test",
        created_by="agent",
    )
    write_node(vault, node)
    Index(vault.root).sync()
    return node.id


def _cc_line(role: str, content: Any, **extra: Any) -> str:
    record = {
        "type": role,
        "sessionId": "sess-abc",
        "uuid": f"u-{role}-{len(str(content))}",
        "timestamp": "2026-11-01T10:00:00+00:00",
        "message": {"role": role, "content": content},
        **extra,
    }
    return json.dumps(record, ensure_ascii=False) + "\n"


# ---------- 写入端：对话导入 ----------


def test_ingest_claude_code_is_incremental_and_clean(vault: Vault, tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        _cc_line("user", "<system-reminder>忽略我</system-reminder>以后回答先给结论")
        + _cc_line(
            "assistant", [{"type": "thinking", "thinking": "x"}, {"type": "text", "text": "好"}]
        )
        + _cc_line("user", "子代理内容", isSidechain=True)
        + _cc_line("user", "key 是 sk-abcdefghijklmnopqrstuvwx"),
        encoding="utf-8",
    )
    first = ingest_transcript(vault, transcript)
    assert first.format == "claude-code"
    assert first.session_id == "sess-abc"
    assert first.captured == 3
    events = read_events(vault.l0_dir)
    assert [event.kind for event in events] == ["user_message", "assistant_message", "user_message"]
    assert events[0].content == "以后回答先给结论"
    assert "sk-abcdefghijklmnopqrstuvwx" not in events[2].content

    # 重复导入不产生重复事件；没写完的行留到下次。
    assert ingest_transcript(vault, transcript).captured == 0
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(_cc_line("user", "第二轮")[:-1])
    assert ingest_transcript(vault, transcript).captured == 0
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert ingest_transcript(vault, transcript).captured == 1
    seqs = [event.seq for event in read_events(vault.l0_dir)]
    assert seqs == sorted(set(seqs))


def test_ingest_generic_json_and_hook_payload(vault: Vault, tmp_path: Path) -> None:
    export = tmp_path / "chat export.json"
    export.write_text(
        json.dumps(
            {"messages": [{"role": "user", "content": "你好"}, {"role": "tool", "content": "x"}]}
        ),
        encoding="utf-8",
    )
    result = ingest_transcript(vault, export)
    assert (result.captured, result.skipped, result.session_id) == (1, 1, "chat-export")
    assert ingest_transcript(vault, export).captured == 0

    transcript = tmp_path / "hook.jsonl"
    transcript.write_text(_cc_line("user", "记住：提交前跑测试"), encoding="utf-8")
    hooked = ingest_hook_payload(
        vault, {"session_id": "hook-1", "transcript_path": str(transcript), "cwd": str(tmp_path)}
    )
    assert hooked is not None and hooked.captured == 1 and hooked.session_id == "hook-1"
    assert ingest_hook_payload(vault, {"session_id": "x"}) is None


def test_cli_ingest_and_hook_capture(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transcript = tmp_path / "cli.jsonl"
    transcript.write_text(_cc_line("user", "偏好中文回复"), encoding="utf-8")
    assert main(["ingest", str(transcript), "--vault", str(vault.root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["captured"] == 1

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(_cc_line("assistant", "收到"))
    payload = {"transcript_path": str(transcript), "session_id": "sess-abc"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert main(["hook", "capture", "--vault", str(vault.root)]) == 0
    assert len(read_events(vault.l0_dir)) == 2

    # Hook 遇到坏输入也不能让宿主失败。
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert main(["hook", "capture", "--vault", str(vault.root)]) == 0


# ---------- 复发门控 ----------


class _FakeExtract:
    def __init__(self, candidates_by_session: dict[str, list[dict[str, Any]]]) -> None:
        self.candidates_by_session = candidates_by_session

    def complete_json(
        self, messages: list[dict[str, str]], *, caller: str = "", temperature: float | None = None
    ) -> tuple[Any, LlmResponse]:
        prompt = messages[-1]["content"]
        found: list[dict[str, Any]] = []
        for session_id, candidates in self.candidates_by_session.items():
            if f"l0:{session_id}:" in prompt or f"[{session_id}" in prompt or session_id in prompt:
                found = candidates
        response = LlmResponse(
            text="{}",
            role="extract",
            model="fake",
            tokens_in=10,
            tokens_out=10,
            usd_estimate=0.0,
            latency_ms=1.0,
            priced=False,
        )
        return {"candidates": found}, response


def _candidate(title: str, summary: str, seq: int = 1) -> dict[str, Any]:
    return {
        "title": title,
        "summary": summary,
        "body": f"## 结论\n{summary}\n\n## 证据\n对话。",
        "category": "semantic",
        "layer": "L3",
        "confidence": 0.5,
        "source_event_ids": [str(seq)],
        "tags": [],
    }


def test_singleton_waits_in_pool_until_it_recurs_in_another_session(vault: Vault) -> None:
    config = Config()
    extract = _FakeExtract(
        {
            "sess-one": [
                _candidate("中文检索用 bigram 预分词", "中文检索采用 CJK bigram 预分词方案")
            ],
            "sess-two": [_candidate("中文检索的分词方案", "中文检索继续采用 bigram 预分词")],
        }
    )
    clients = DreamClients(clients={"extract": extract})

    memory_log(vault, session_id="sess-one", kind="user_message", content="sess-one 讨论分词")
    first = run_dream(vault, config, phases=("nrem",), clients=clients)
    assert first.ok
    assert first.phases[0].nodes_created == 0
    assert first.phases[0].details["deferred_singletons"] == 1
    assert len(PendingPool.load(vault).entries) == 1

    memory_log(vault, session_id="sess-two", kind="user_message", content="sess-two 讨论分词")
    second = run_dream(vault, config, phases=("nrem",), clients=clients)
    assert second.ok
    assert second.phases[0].nodes_created == 1
    assert second.phases[0].details["recalled_pending"] == 1
    assert PendingPool.load(vault).entries == []
    assert second.budget["unpriced_calls"] >= 1
    assert any("没有配置单价" in warning for warning in second.warnings)


def test_unrelated_candidates_do_not_help_each_other(vault: Vault) -> None:
    config = Config()
    extract = _FakeExtract(
        {
            "sess-mix": [
                _candidate("用户偏好简洁回答", "用户要求回答先给结论再给理由", 1),
                _candidate("用户要求先写测试", "修改代码之前必须先补失败用例", 2),
            ]
        }
    )
    memory_log(vault, session_id="sess-mix", kind="user_message", content="sess-mix")
    report = run_dream(
        vault, config, phases=("nrem",), clients=DreamClients(clients={"extract": extract})
    )
    assert report.phases[0].nodes_created == 0
    assert report.phases[0].details["deferred_singletons"] == 2
    assert pending_path(vault).is_file()


def test_replayed_candidate_is_not_its_own_recurrence() -> None:
    def make(
        session: str, evidence: list[str], title: str = "中文检索用 bigram 预分词"
    ) -> _Candidate:
        return _Candidate(
            title=title,
            summary="中文检索采用 CJK bigram 预分词方案",
            body="b",
            category="semantic",
            layer="L3",
            confidence=0.4,
            evidence=evidence,
            tags=[],
            links=[],
            session_id=session,
        )

    stored = make("s1", ["l0:s1:1"])
    pool = PendingPool()
    pool.add(stored.__dict__.copy())
    assert _recall_pending(pool, [make("s1", ["l0:s1:1"])]) == []
    assert len(_recall_pending(pool, [make("s2", ["l0:s2:1"])])) == 1
    unrelated = make("s2", [], title="数据库迁移脚本要可回滚")
    unrelated.summary = "每个迁移都要有 down 步骤"
    assert not _candidates_overlap(stored, unrelated)


# ---------- 检索 ----------


def test_search_relaxes_when_not_every_term_matches(vault: Vault) -> None:
    target = _write(vault, "中文检索用 bigram 预分词", "FTS5 之前先把中文切成重叠二元组")
    _write(vault, "数据库迁移要可回滚", "每个迁移脚本都要带 down 步骤")
    settings = RetrievalSettings.from_config(Config())
    result = memory_search(
        [("project", vault)],
        "中文检索到底怎么做分词",
        settings=settings,
        filters=SearchFilters(),
        record_audit=False,
    )
    assert [hit.id for hit in result.hits] == [target]
    assert "partial-match" in result.hits[0].why

    exact = memory_search(
        [("project", vault)],
        "bigram",
        settings=settings,
        filters=SearchFilters(),
        record_audit=False,
    )
    assert exact.hits[0].id == target
    assert "partial-match" not in exact.hits[0].why


class _FakeEmbeddings:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        return [[1.0, 0.0] if "分词" in text else [0.0, 1.0] for text in texts]


def test_hybrid_uses_rank_fusion_and_embeds_only_changes(vault: Vault) -> None:
    config = Config()
    target = _write(vault, "中文检索用 bigram 预分词", "先切二元组再建索引，分词不依赖词典")
    other = _write(vault, "数据库迁移要可回滚", "每个迁移脚本都要带 down 步骤")
    client = _FakeEmbeddings()
    assert build_embeddings(vault, config, client=client) == 2
    assert build_embeddings(vault, config, client=client) == 0

    result = hybrid_search(
        [("project", vault)], "bigram 分词", config=config, client=client, top_k=5
    )
    ids = [hit["id"] for hit in result["hits"]]
    assert ids[0] == target and other not in ids[:1]
    top = result["hits"][0]
    assert top["score"] == 1.0  # 两路都排第一
    assert "semantic" in top["why"] and "semantic_score" in top and "lexical_score" in top


# ---------- 预算与默认模型 ----------


def test_default_model_covers_every_role_and_tokens_are_capped() -> None:
    config = Config()
    with pytest.raises(Exception, match="未配置模型"):
        resolve_role(config, "dedupe")
    config.data["llm"]["models"]["default"] = "any-model"
    assert resolve_role(config, "dedupe").model == "any-model"
    assert resolve_role(config, "extract").model == "any-model"

    budget = DreamBudget(max_tokens=100)
    response = LlmResponse(
        text="",
        role="extract",
        model="m",
        tokens_in=80,
        tokens_out=30,
        usd_estimate=0.0,
        latency_ms=1.0,
        priced=False,
    )
    assert budget.can_call_llm()
    budget.record_llm(response)
    assert not budget.can_call_llm()
    assert budget.unpriced_calls == 1
    assert "token" in (budget.exhausted() or "")


# ---------- HTTP / TLS ----------


def test_http_settings_require_tls_off_loopback(tmp_path: Path) -> None:
    config = Config()
    local = resolve_http_settings(config)
    assert local.loopback and not local.tls and local.public_url == "http://127.0.0.1:8765"

    with pytest.raises(UsageError, match="必须启用 TLS"):
        resolve_http_settings(config, host="0.0.0.0")

    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.write_text("cert", encoding="utf-8")
    with pytest.raises(UsageError, match="同时提供"):
        resolve_http_settings(config, host="0.0.0.0", tls_cert=str(cert))
    with pytest.raises(UsageError, match="不存在"):
        resolve_http_settings(config, host="0.0.0.0", tls_cert=str(cert), tls_key=str(key))
    key.write_text("key", encoding="utf-8")
    secure = resolve_http_settings(config, host="0.0.0.0", tls_cert=str(cert), tls_key=str(key))
    assert secure.tls and secure.public_url.startswith("https://")

    config.data["mcp"]["allow_insecure_http"] = True
    config.data["mcp"]["public_url"] = "https://memory.example.com/"
    proxied = resolve_http_settings(config, host="0.0.0.0")
    assert proxied.public_url == "https://memory.example.com"
    assert allowed_hosts(proxied)[0] == "memory.example.com"
    # 没有可用域名时不校验 Host；本机监听时始终包含本机地址。
    assert allowed_hosts(secure) == []
    assert "127.0.0.1" in allowed_hosts(local)


def test_token_compare_and_ingest_endpoint_payload(vault: Vault) -> None:
    assert token_matches("s3cret-token", "s3cret-token")
    assert not token_matches("s3cret-token", "s3cret-tokeN")
    assert not token_matches("", "")

    status, body = ingest_payload(
        vault,
        {
            "session_id": "chat web/1",
            "messages": [
                {"role": "user", "content": "以后默认用中文"},
                {"role": "assistant", "content": "好的"},
            ],
        },
    )
    assert status == 200 and body["captured"] == 2 and body["session_id"] == "chat-web-1"
    assert len(read_events(vault.l0_dir)) == 2
    assert ingest_payload(vault, {"messages": []})[0] == 400
    assert ingest_payload(vault, ["x"])[0] == 400
