from __future__ import annotations

import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from zymor.config import Config
from zymor.dream.state import list_run_records
from zymor.errors import UsageError
from zymor.maintenance import review_nodes, write_stats
from zymor.retrieval import RetrievalSettings, memory_search
from zymor.store.audit import read_audit
from zymor.store.index import Index
from zymor.vault import Vault


def overview_payload(vault: Vault) -> dict[str, Any]:
    """入参：vault。出参：面板首页统计、review、dream 与规则健康数据。"""
    counts = Index(vault.root).counts()
    health = write_stats(vault)
    return {
        "vault": str(vault.root),
        "kind": vault.info.kind,
        "counts": counts,
        "health": health,
        "reviews": review_nodes(vault, limit=10),
        "dreams": list_run_records(vault, limit=10),
    }


def search_payload(vault: Vault, config: Config, query: str) -> list[dict[str, Any]]:
    """入参：vault、配置与查询。出参：面板搜索命中。"""
    if not query.strip():
        return []
    settings = RetrievalSettings.from_config(config)
    result = memory_search(
        [("project", vault)],
        query,
        settings=settings,
        top_k=20,
        session_id="panel",
        record_audit=False,
    )
    return [asdict(hit) for hit in result.hits]


def rules_payload(vault: Vault, *, limit: int = 20) -> list[dict[str, Any]]:
    """入参：vault 与上限。出参：最近的规则检查失败记录。"""
    records = read_audit(vault.audit_dir / "rules.jsonl")
    failures: list[dict[str, Any]] = []
    for record in reversed(records):
        for failure in record.get("failures", []):
            if isinstance(failure, dict):
                failures.append({**failure, "ts": record.get("ts")})
                if len(failures) >= limit:
                    return failures
    return failures


def _json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def make_handler(vault: Vault, config: Config) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    body = PANEL_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/api/overview":
                    data = overview_payload(vault)
                elif parsed.path == "/api/search":
                    query = parse_qs(parsed.query).get("q", [""])[0]
                    data = {"query": query, "hits": search_payload(vault, config, query)}
                elif parsed.path == "/api/rules":
                    data = {"failures": rules_payload(vault)}
                elif parsed.path == "/api/dreams":
                    data = {"runs": list_run_records(vault, limit=20)}
                else:
                    self.send_error(404)
                    return
                body = _json_bytes(data)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as error:
                body = _json_bytes({"error": str(error)})
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

    return Handler


PANEL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zymor Panel</title>
<style>
body { font-family: ui-sans-serif, system-ui, sans-serif; margin: 0; background: #0b1020; color: #e0e7ff; }
header { padding: 20px 28px; border-bottom: 1px solid #26325c; }
h1 { margin: 0; font-size: 22px; letter-spacing: .04em; }
main { max-width: 1100px; margin: 0 auto; padding: 24px; }
section { margin-bottom: 28px; }
h2 { font-size: 15px; color: #8b9ff0; letter-spacing: .08em; text-transform: uppercase; }
input { width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid #2d3a6b; background: #141a30; color: #e0e7ff; box-sizing: border-box; }
.card { background: #141a30; border: 1px solid #26325c; border-radius: 10px; padding: 12px; margin: 8px 0; }
.muted { color: #9aa8d8; font-size: 13px; }
.pill { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #1a2242; margin-right: 6px; font-size: 12px; }
.row { display: flex; justify-content: space-between; gap: 12px; }
pre { white-space: pre-wrap; word-break: break-word; font-size: 12px; color: #c7d2fe; }
</style>
</head>
<body>
<header><h1>ZYMOR / MEMORY PANEL</h1><div class="muted" id="vault"></div></header>
<main>
<section><h2>Search</h2><input id="q" placeholder="搜索记忆..."></section>
<section><h2>Health</h2><div id="health"></div></section>
<section><h2>Review Queue</h2><div id="reviews"></div></section>
<section><h2>Dream Runs</h2><div id="dreams"></div></section>
<section><h2>Rule Failures</h2><div id="rules"></div></section>
</main>
<script>
const $ = (id) => document.getElementById(id);
function pill(text) { const s = document.createElement('span'); s.className='pill'; s.textContent=text; return s; }
function card(title, summary, meta) {
  const d = document.createElement('div'); d.className='card';
  const r = document.createElement('div'); r.className='row';
  const h = document.createElement('strong'); h.textContent=title;
  r.appendChild(h); (meta||[]).forEach(x => r.appendChild(pill(x))); d.appendChild(r);
  if (summary) { const p=document.createElement('div'); p.className='muted'; p.textContent=summary; d.appendChild(p); }
  return d;
}
async function overview() {
  const data = await (await fetch('/api/overview')).json();
  $('vault').textContent = data.vault + ' (' + data.kind + ')';
  const h = data.health;
  const box = $('health'); box.replaceChildren();
  box.appendChild(card('Nodes', String(data.counts.total), Object.entries(data.counts.layers).map(([k,v]) => k+':'+v)));
  box.appendChild(card('Dreams', 'runs='+h.dream_runs+' failures='+h.dream_failures));
  box.appendChild(card('Rules', 'checks='+h.rule_checks+' failures='+h.rule_check_failures));
  box.appendChild(card('Retrieval', 'records='+h.retrievals+' reads='+h.retrieval_reads));
  box.appendChild(card('Stale', String(h.stale_nodes)));
  const rv = $('reviews'); rv.replaceChildren();
  data.reviews.forEach(n => rv.appendChild(card(n.title, n.summary, [n.status, n.layer, n.category])));
  const dr = $('dreams'); dr.replaceChildren();
  data.dreams.forEach(r => dr.appendChild(card(r.run_id, '', ['ok='+r.ok, r.status, (r.phases||[]).join(',')])));
  const rules = await (await fetch('/api/rules')).json();
  const rb = $('rules'); rb.replaceChildren();
  rules.failures.forEach(f => rb.appendChild(card(f.title || 'rule', f.message || '', [f.path || '', f.kind || ''])));
}
let timer;
$('q').addEventListener('input', (event) => {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    const data = await (await fetch('/api/search?q=' + encodeURIComponent(event.target.value))).json();
    const box = $('health'); box.replaceChildren();
    if (!data.hits.length) { box.appendChild(card('No hits', '没有匹配记忆')); return; }
    data.hits.forEach(h => box.appendChild(card(h.title, h.summary, [h.layer, h.status, 'score='+h.score.toFixed(2)])));
  }, 180);
});
overview();
</script>
</body>
</html>
"""


class _PanelServer(ThreadingHTTPServer):
    allow_reuse_address = False


def run_panel(vault: Vault, config: Config, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    """入参：vault、配置、监听地址与端口。出参：无。启动只读面板直到中断。"""
    if not 0 <= port <= 65535:
        raise UsageError(f"端口超出范围: {port}")
    try:
        server = _PanelServer((host, port), make_handler(vault, config))
    except OSError as error:
        raise UsageError(f"无法监听 {host}:{port}: {error}") from error
    server.daemon_threads = True
    print(f"Zymor panel: http://{host}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
