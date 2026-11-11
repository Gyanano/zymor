"""Thin client for a Zymor server: upload conversations, fetch context, recall memories.

Used by host hooks. Everything here must be safe to call on every turn of a conversation:
bounded timeouts, no local vault required, and failures never raise into the host.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import http.client
import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from zymor.config import Config
from zymor.errors import ExternalError, UsageError
from zymor.util.fs import atomic_write_text
from zymor.util.ids import safe_project, safe_session_id
from zymor.util.time import now_iso

if TYPE_CHECKING:
    from zymor.ingest import IngestItem

# 本模块在每条用户消息上都会被加载一次，顶层只引用轻量模块；
# 解析对话文件要用的 zymor.ingest（连带 pydantic、yaml）在上传时才导入。

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
UPLOAD_BATCH_MESSAGES = 400
UPLOAD_BATCH_BYTES = 1024 * 1024
MAX_PENDING_RETRIES = 5
MAX_RECALL_IDS = 300
KIND_ROLES = {"user_message": "user", "assistant_message": "assistant", "system_note": "system"}


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_UPLOAD_CONTENT_CHARS = 8000
MAX_INJECT_CHARS = 8000
# 服务器明确拒收的请求（格式不对、太大）重试也不会成功；跳过这一批，不能让它卡住后面的对话。
POISON_STATUS = (400, 413, 422)


class RemoteError(ExternalError):
    kind = "remote_error"

    def __init__(self, message: str, hint: str | None = None, status: int | None = None) -> None:
        super().__init__(message, hint=hint)
        self.status = status


def client_dir() -> Path:
    """入参：无。出参：本机客户端状态目录。不依赖任何 vault。"""
    return Path(os.environ.get("ZYMOR_CLIENT_DIR") or "~/.zymor/client").expanduser()


def http_json(
    url: str,
    token: str,
    payload: dict[str, Any] | None,
    *,
    ca_file: str | None = None,
    timeout_s: float = 8.0,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """入参：地址、token、请求体（None 表示 GET）。出参：JSON 响应。任何失败都抛 RemoteError。

    响应体有大小上限：服务器异常或被入侵时，不能让钩子无限读下去。
    """
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=data, method="GET" if data is None else "POST", headers=headers
    )
    context = ssl.create_default_context(cafile=ca_file) if ca_file else None
    try:
        with urllib.request.urlopen(request, timeout=timeout_s, context=context) as response:
            raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise RemoteError("服务器的响应超过大小上限")
        body = json.loads(raw)
    except urllib.error.HTTPError as error:
        detail = error.read(2000).decode("utf-8", "replace")[:300]
        hint = "检查 token 是否与服务器一致" if error.code == 401 else None
        raise RemoteError(
            f"服务器拒绝了请求（HTTP {error.code}）: {detail}", hint=hint, status=error.code
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
        raise RemoteError(f"无法连接服务器 {url}: {error}") from error
    except ValueError as error:
        raise RemoteError("服务器返回的不是合法 JSON") from error
    if not isinstance(body, dict):
        raise RemoteError("服务器返回的结构不合法")
    return body


def check_url(url: str, *, allow_insecure: bool = False) -> str:
    """入参：服务器地址。出参：去掉结尾斜杠的地址。拒绝明文连接非本机地址。"""
    cleaned = url.strip().rstrip("/")
    parts = urlsplit(cleaned)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise UsageError(f"服务器地址不合法: {url}", hint="形如 https://memory.example.com")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS and not allow_insecure:
        raise UsageError(
            "服务器地址必须使用 https，否则 token 与对话内容以明文传输",
            hint="改用 https 地址；链路已加密时可设 [remote].allow_insecure_http = true",
        )
    return cleaned


def read_token(config: Config) -> str:
    """入参：配置。出参：token；先读环境变量，再读 token 文件；都没有时返回空串。"""
    env_name = str(config.get("remote.token_env", "ZYMOR_REMOTE_TOKEN") or "ZYMOR_REMOTE_TOKEN")
    token = os.environ.get(env_name, "").strip()
    if token:
        return token
    raw_file = config.get("remote.token_file", None)
    if raw_file:
        try:
            return Path(str(raw_file)).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


@dataclass
class RemoteSettings:
    url: str
    token: str
    ca_file: str | None
    upload: bool
    inject: bool
    exclude: tuple[str, ...]
    timeout_s: float
    recall_timeout_s: float
    recall_min_chars: int

    @classmethod
    def from_config(cls, config: Config) -> RemoteSettings | None:
        """入参：配置。出参：远端设置；没有配置服务器地址时返回 None（表示使用本地库）。"""
        raw_url = str(config.get("remote.url", None) or "").strip()
        if not raw_url:
            return None
        url = check_url(
            raw_url, allow_insecure=bool(config.get("remote.allow_insecure_http", False))
        )
        token = read_token(config)
        if not token:
            raise UsageError(
                "已配置服务器地址，但没有 token",
                hint="运行 `zymor connect <地址>` 保存 token，或设置环境变量 ZYMOR_REMOTE_TOKEN",
            )
        raw_exclude = config.get("remote.exclude", []) or []
        ca_file = config.get("remote.ca_file", None)
        return cls(
            url=url,
            token=token,
            ca_file=str(Path(str(ca_file)).expanduser()) if ca_file else None,
            upload=bool(config.get("remote.upload", True)),
            inject=bool(config.get("remote.inject", True)),
            exclude=tuple(str(item) for item in raw_exclude)
            if isinstance(raw_exclude, list)
            else (),
            timeout_s=float(config.get("remote.timeout_s", 8)),
            recall_timeout_s=float(config.get("remote.recall_timeout_s", 3)),
            recall_min_chars=int(config.get("remote.recall_min_chars", 6)),
        )

    def post(self, path: str, payload: dict[str, Any], *, timeout_s: float | None = None) -> Any:
        """入参：入口路径与请求体。出参：JSON 响应。"""
        return http_json(
            f"{self.url}{path}",
            self.token,
            payload,
            ca_file=self.ca_file,
            timeout_s=timeout_s or self.timeout_s,
        )

    def excludes(self, cwd: Path | None) -> bool:
        """入参：会话的工作目录。出参：该目录是否被配置为不上传、不取回。"""
        if cwd is None:
            return False
        text = str(cwd.expanduser())
        for pattern in self.exclude:
            expanded = str(Path(pattern).expanduser())
            if text == expanded or text.startswith(expanded.rstrip("/") + "/"):
                return True
            if fnmatch.fnmatch(text, expanded):
                return True
        return False


def project_name(cwd: Path | None) -> str | None:
    """入参：工作目录。出参：项目名：最近的含 `.git` 或 `.zymor` 的祖先目录名，否则当前目录名。"""
    if cwd is None:
        return None
    try:
        current = cwd.expanduser().resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists() or (candidate / ".zymor").is_dir():
            return safe_project(candidate.name)
    return safe_project(current.name)


# ---------- 上传对话 ----------


def _state_file() -> Path:
    return client_dir() / "upload.json"


def _load_upload_state() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    return {str(key): dict(value) for key, value in files.items() if isinstance(value, dict)}


def _save_upload_state(files: dict[str, dict[str, Any]]) -> None:
    payload = {"schema_version": 1, "updated_at": now_iso(), "files": files}
    atomic_write_text(_state_file(), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


@contextlib.contextmanager
def _upload_lock() -> Any:
    """本机同一时刻只允许一个进程读改写上传进度。拿不到锁时产出 False，调用方本次跳过。"""
    path = client_dir() / "upload.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # 非 POSIX 平台没有 flock，退回到不加锁。
        yield True
        return
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _message(item: IngestItem) -> dict[str, Any]:
    # 服务器单条事件只存 8 KB，超出的部分传上去也会被截掉。
    message: dict[str, Any] = {
        "role": KIND_ROLES.get(item.kind, "tool"),
        "kind": item.kind,
        "content": item.content[:MAX_UPLOAD_CONTENT_CHARS],
    }
    if item.ts:
        message["ts"] = item.ts
    return message


def upload_transcript(
    settings: RemoteSettings,
    path: Path,
    *,
    session_id: str | None = None,
    project: str | None = None,
    state: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """入参：远端设置、对话文件、会话与项目。出参：`{uploaded, pending}`。

    按行分批上传。每批成功后才推进本地记录的偏移，所以失败不丢内容、重试不重复：
    没传成功的部分留在对话文件里，下一次从同一位置继续。
    """
    from zymor.ingest import progress_entry, resume_offset, scan_transcript

    owns_state = state is None
    files = _load_upload_state() if state is None else state
    path = path.expanduser().resolve()
    key = str(path)
    entry = files.get(key, {})
    scan = scan_transcript(
        path,
        offset=resume_offset(path, entry),
        fmt=str(entry.get("format") or "claude-code"),
        session_id=session_id or entry.get("session_id"),
    )
    final_session = safe_session_id(str(scan.session_id or path.stem))
    resolved_project = project or entry.get("project")
    uploaded = 0
    dropped = 0
    committed = resume_offset(path, entry)
    error: RemoteError | None = None
    batch: list[dict[str, Any]] = []
    batch_bytes = 0
    batch_start = committed
    batch_end = committed

    def flush() -> None:
        nonlocal batch, batch_bytes, committed, uploaded, dropped, batch_start
        if not batch:
            committed = batch_end
            batch_start = batch_end
            return
        # batch_id 由「哪个文件的哪一段」决定。请求超时后重发同一段时，服务器据此识别并跳过。
        digest = hashlib.sha256(f"{key}:{batch_start}:{batch_end}".encode()).hexdigest()
        payload: dict[str, Any] = {
            "session_id": final_session,
            "messages": batch,
            "batch_id": digest[:32],
        }
        if resolved_project:
            payload["project"] = resolved_project
        try:
            settings.post("/ingest", payload)
            uploaded += len(batch)
        except RemoteError as failure:
            if failure.status not in POISON_STATUS:
                raise
            dropped += len(batch)
        committed = batch_end
        batch_start = batch_end
        batch, batch_bytes = [], 0

    try:
        for line_end, items in scan.lines:
            messages = [_message(item) for item in items]
            size = sum(
                len(json.dumps(message, ensure_ascii=False).encode("utf-8")) for message in messages
            )
            if batch and (
                len(batch) + len(messages) > UPLOAD_BATCH_MESSAGES
                or batch_bytes + size > UPLOAD_BATCH_BYTES
            ):
                flush()
            batch.extend(messages)
            batch_bytes += size
            batch_end = line_end
        flush()
        committed = scan.offset
    except RemoteError as failure:
        error = failure
    files[key] = progress_entry(
        path,
        committed,
        format=scan.format,
        session_id=final_session,
        project=resolved_project,
        pending=error is not None,
    )
    if owns_state:
        _save_upload_state(files)
    result: dict[str, Any] = {
        "session_id": final_session,
        "uploaded": uploaded,
        "pending": error is not None,
    }
    if dropped:
        result["dropped"] = dropped
    if error is not None:
        result["error"] = error.message
    return result


def upload_hook_payload(settings: RemoteSettings, payload: dict[str, Any]) -> dict[str, Any] | None:
    """入参：远端设置与宿主 Hook 的 stdin JSON。出参：上传结果；不该上传时返回 None。

    处理完当前会话后，顺带重试之前因断网没传完的其他会话。
    """
    raw_path = payload.get("transcript_path")
    raw_cwd = payload.get("cwd")
    cwd = Path(raw_cwd) if isinstance(raw_cwd, str) and raw_cwd else None
    if not settings.upload or settings.excludes(cwd):
        return None
    if not isinstance(raw_path, str) or not Path(raw_path).expanduser().is_file():
        return None
    with _upload_lock() as acquired:
        if not acquired:
            # 另一个钩子进程正在上传。本次跳过，下一次钩子会从记录的位置接着传。
            return {"uploaded": 0, "pending": False, "busy": True, "retried": 0}
        return _upload_locked(settings, payload, raw_path, cwd)


def _upload_locked(
    settings: RemoteSettings, payload: dict[str, Any], raw_path: str, cwd: Path | None
) -> dict[str, Any]:
    files = _load_upload_state()
    session = payload.get("session_id")
    current = str(Path(raw_path).expanduser().resolve())
    result = upload_transcript(
        settings,
        Path(raw_path),
        session_id=session if isinstance(session, str) and session else None,
        project=project_name(cwd),
        state=files,
    )
    retried = 0
    if not result["pending"]:
        for key, entry in list(files.items()):
            if retried >= MAX_PENDING_RETRIES:
                break
            if key == current or not entry.get("pending"):
                continue
            if not Path(key).is_file():
                # 对话文件已被宿主清理，没传完的部分无法恢复。
                entry["pending"] = False
                continue
            retried += 1
            if upload_transcript(settings, Path(key), state=files)["pending"]:
                break
    _save_upload_state(files)
    result["retried"] = retried
    return result


# ---------- 取回记忆 ----------


def fetch_context(
    settings: RemoteSettings, *, task: str | None, project: str | None, budget_tokens: int | None
) -> dict[str, Any]:
    """入参：远端设置、任务、项目与预算。出参：服务器装配的会话启动上下文。"""
    payload: dict[str, Any] = {}
    if task:
        payload["task"] = task
    if project:
        payload["project"] = project
    if budget_tokens:
        payload["budget_tokens"] = budget_tokens
    result = settings.post("/context", payload)
    return result if isinstance(result, dict) else {}


def _recall_state(session_id: str) -> Path:
    return client_dir() / "recall" / f"{safe_session_id(session_id, fallback='session')}.json"


def recall_for_prompt(settings: RemoteSettings, payload: dict[str, Any]) -> str:
    """入参：远端设置与 UserPromptSubmit Hook 的 stdin JSON。出参：要注入的文本；没有则为空串。

    同一会话里已经注入过的记忆不重复注入。
    """
    prompt = payload.get("prompt")
    raw_cwd = payload.get("cwd")
    cwd = Path(raw_cwd) if isinstance(raw_cwd, str) and raw_cwd else None
    if not settings.inject or settings.excludes(cwd) or not isinstance(prompt, str):
        return ""
    text = prompt.strip()
    if len(text) < settings.recall_min_chars or text.startswith("/"):
        return ""
    session = payload.get("session_id")
    state_path = _recall_state(session) if isinstance(session, str) and session else None
    seen: list[str] = []
    if state_path is not None:
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            seen = [str(item) for item in loaded] if isinstance(loaded, list) else []
        except (OSError, json.JSONDecodeError):
            seen = []
    request: dict[str, Any] = {"query": text, "exclude_ids": seen[-MAX_RECALL_IDS:]}
    project = project_name(cwd)
    if project:
        request["project"] = project
    result = settings.post("/recall", request, timeout_s=settings.recall_timeout_s)
    hits = result.get("hits") if isinstance(result, dict) else None
    rendered = result.get("text") if isinstance(result, dict) else None
    if not isinstance(hits, list) or not hits or not isinstance(rendered, str):
        return ""
    if state_path is not None:
        seen.extend(str(hit.get("id")) for hit in hits if isinstance(hit, dict))
        atomic_write_text(state_path, json.dumps(seen[-MAX_RECALL_IDS:]) + "\n")
    return rendered[:MAX_INJECT_CHARS]
