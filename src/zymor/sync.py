"""Cloud sync: the local vault stays the source of truth; a remote keeps an incremental copy.

Both sides exchange whole files identified by vault-relative path and sha256. The client keeps
the hashes of the last successful sync, which lets it tell "changed here" from "changed there"
and upload or download only what differs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from zymor.config import Config
from zymor.errors import ExternalError, UsageError, ValidationError, ZymorError
from zymor.model.node import MemoryNode
from zymor.security import detect_sensitive
from zymor.store.index import Index
from zymor.store.nodes import check_layer_vault
from zymor.util.fs import atomic_write_text, sha256_hex
from zymor.util.time import now_iso
from zymor.vault import Vault

NODE_PATH_RE = re.compile(
    r"^nodes/(?P<category>[a-z0-9_-]{1,32})/(?P<id>mem_\d{8}_[0-9a-f]{10})\.md$"
)
EVENT_PATH_RE = re.compile(r"^L0/\d{4}-\d{2}-\d{2}\.jsonl$")
SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SCOPE_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
STORE_DIR = "sync-store"
TOMBSTONE_FILE = ".deleted.json"
VAULT_SCOPE = "@vault"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_BATCH_BYTES = 3 * 1024 * 1024
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_PATHS_PER_REQUEST = 2000
MAX_SYNC_RESPONSE_BYTES = 64 * 1024 * 1024
MASS_DELETE_MIN = 20
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
ACTIONS = ("manifest", "push", "pull", "delete")


class SyncError(ExternalError):
    kind = "sync_error"


def is_sync_path(path: object) -> bool:
    """入参：任意值。出参：是否为允许同步的库内相对路径。拒绝一切带 `..` 或绝对路径的写法。"""
    return isinstance(path, str) and (
        NODE_PATH_RE.fullmatch(path) is not None or EVENT_PATH_RE.fullmatch(path) is not None
    )


def default_scope(vault: Vault) -> str:
    """入参：vault。出参：默认的远端分区名：用户库为 `global`，项目库为 `project-<名称>`。"""
    if vault.info.kind == "global":
        return "global"
    name = SCOPE_UNSAFE_RE.sub("-", vault.info.name).strip("-._") or "unnamed"
    return f"project-{name}"[:64]


def local_manifest(vault: Vault, *, include_events: bool = False) -> dict[str, str]:
    """入参：vault 与是否包含 L0。出参：相对路径到 sha256 的映射。生成物（`_` 开头）不参与同步。"""
    manifest: dict[str, str] = {}
    targets = [path for path in vault.nodes_dir.rglob("*.md") if not path.name.startswith("_")]
    if include_events:
        targets.extend(vault.l0_dir.glob("*.jsonl"))
    for path in sorted(targets):
        relative = path.relative_to(vault.root).as_posix()
        if not is_sync_path(relative):
            continue
        try:
            manifest[relative] = sha256_hex(path.read_bytes())
        except OSError:
            continue
    return manifest


# ---------- 服务端：只存文件副本，不并入服务器自己的库 ----------


@dataclass
class SyncStore:
    root: Path

    @classmethod
    def for_vault(cls, vault: Vault) -> SyncStore:
        """入参：服务器上的 vault。出参：它名下的同步副本区。"""
        return cls(vault.root / STORE_DIR)

    def _scope_dir(self, scope: str) -> Path:
        if not isinstance(scope, str) or SCOPE_RE.fullmatch(scope) is None:
            raise ValidationError(
                "非法分区名",
                field="scope",
                hint="字母数字开头，只含字母数字与 `._-`，至多 64 个字符",
            )
        return self.root / scope

    def manifest(self, scope: str) -> dict[str, str]:
        """入参：分区名。出参：该分区内相对路径到 sha256 的映射。"""
        base = self._scope_dir(scope)
        result: dict[str, str] = {}
        if not base.is_dir():
            return result
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(base).as_posix()
            if is_sync_path(relative):
                result[relative] = sha256_hex(path.read_bytes())
        return result

    def tombstones(self, scope: str) -> dict[str, str]:
        """入参：分区名。出参：被主动删除的路径到删除时间的映射。"""
        path = self._scope_dir(scope) / TOMBSTONE_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(name): str(value) for name, value in data.items() if is_sync_path(name)}

    def _save_tombstones(self, scope: str, entries: dict[str, str]) -> None:
        atomic_write_text(
            self._scope_dir(scope) / TOMBSTONE_FILE,
            json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        )

    def put(self, scope: str, files: list[Any]) -> list[str]:
        """入参：分区名与 `{path, content, sha256}` 列表。出参：已保存的路径。先整体校验再落盘。"""
        base = self._scope_dir(scope)
        checked: list[tuple[str, str]] = []
        for item in files:
            if not isinstance(item, dict):
                raise ValidationError("files 的元素必须是对象", field="files")
            path, content = item.get("path"), item.get("content")
            if not is_sync_path(path) or not isinstance(content, str):
                raise ValidationError(f"非法同步路径或内容: {path!r}", field="files")
            if len(content.encode("utf-8")) > MAX_FILE_BYTES:
                raise ValidationError(f"文件超过大小上限: {path}", field="files")
            if item.get("sha256") != sha256_hex(content):
                raise ValidationError(f"校验和不匹配: {path}", field="files")
            checked.append((str(path), content))
        for path, content in checked:
            atomic_write_text(base / path, content)
        entries = self.tombstones(scope)
        revived = [path for path, _ in checked if path in entries]
        if revived:
            # 重新上传说明这个文件又被需要了，撤销它的删除标记。
            for path in revived:
                del entries[path]
            self._save_tombstones(scope, entries)
        return [path for path, _ in checked]

    def get(self, scope: str, paths: list[Any]) -> list[dict[str, str]]:
        """入参：分区名与路径列表。出参：存在的文件及其内容。"""
        base = self._scope_dir(scope)
        found: list[dict[str, str]] = []
        for path in paths:
            if not is_sync_path(path):
                raise ValidationError(f"非法同步路径: {path!r}", field="paths")
            target = base / str(path)
            if target.is_file():
                content = target.read_text(encoding="utf-8")
                found.append({"path": str(path), "content": content, "sha256": sha256_hex(content)})
        return found

    def delete(self, scope: str, paths: list[Any]) -> list[str]:
        """入参：分区名与路径列表。出参：实际删除的路径。"""
        base = self._scope_dir(scope)
        removed: list[str] = []
        for path in paths:
            if not is_sync_path(path):
                raise ValidationError(f"非法同步路径: {path!r}", field="paths")
            target = base / str(path)
            if target.is_file():
                target.unlink()
                removed.append(str(path))
        if removed:
            # 记下「这是主动删除」，其他机器据此删除自己的副本，而不是把文件传回来。
            entries = self.tombstones(scope)
            stamp = now_iso()
            for path in removed:
                entries[path] = stamp
            self._save_tombstones(scope, entries)
        return removed


def handle_sync_request(
    vault: Vault, payload: Any, *, writable: bool = True
) -> tuple[int, dict[str, Any]]:
    """入参：服务器 vault、请求体与是否可写。出参：HTTP 状态码与响应体。"""
    if not isinstance(payload, dict):
        return 400, {"error": "invalid_payload", "hint": "请求体必须是 JSON 对象"}
    action = payload.get("action")
    if action not in ACTIONS:
        return 400, {"error": "invalid_action", "hint": f"可选：{', '.join(ACTIONS)}"}
    if action in ("push", "delete") and not writable:
        return 403, {"error": "read_only", "hint": "只读模式不接受上传与删除"}
    store = SyncStore.for_vault(vault)
    scope = payload.get("scope")
    if not isinstance(scope, str) or not scope:
        return 400, {"error": "invalid_scope", "hint": "缺少 scope"}
    if scope == VAULT_SCOPE:
        return _handle_vault_scope(vault, str(action), payload)
    items = payload.get("files" if action == "push" else "paths", [])
    if action != "manifest" and (not isinstance(items, list) or len(items) > MAX_PATHS_PER_REQUEST):
        return 400, {"error": "invalid_payload", "hint": "files / paths 必须是数组且不超过上限"}
    try:
        if action == "manifest":
            return 200, {
                "scope": scope,
                "files": store.manifest(str(scope)),
                "deleted": sorted(store.tombstones(str(scope))),
            }
        if action == "push":
            return 200, {"scope": scope, "saved": store.put(str(scope), items)}
        if action == "pull":
            return 200, {"scope": scope, "files": store.get(str(scope), items)}
        return 200, {"scope": scope, "deleted": store.delete(str(scope), items)}
    except ZymorError as error:
        return 400, error.to_dict()
    except (OSError, UnicodeError) as error:
        return 500, {"error": "storage_error", "message": str(error)}


def _handle_vault_scope(
    vault: Vault, action: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """`@vault` 分区是服务器自己的库，只读：用于把云端提炼出的知识下载成本地副本。"""
    if action not in ("manifest", "pull"):
        return 403, {"error": "read_only", "hint": "服务器的库只能下载，不能经同步入口改写"}
    if action == "manifest":
        return 200, {"scope": VAULT_SCOPE, "files": local_manifest(vault), "deleted": []}
    paths = payload.get("paths", [])
    if not isinstance(paths, list) or len(paths) > MAX_PATHS_PER_REQUEST:
        return 400, {"error": "invalid_payload", "hint": "paths 必须是数组且不超过上限"}
    files: list[dict[str, str]] = []
    for path in paths:
        if not isinstance(path, str) or NODE_PATH_RE.fullmatch(path) is None:
            return 400, {"error": "invalid_path", "hint": f"非法同步路径: {path!r}"}
        target = vault.root / path
        if target.is_file():
            content = target.read_text(encoding="utf-8")
            files.append({"path": path, "content": content, "sha256": sha256_hex(content)})
    return 200, {"scope": VAULT_SCOPE, "files": files}


# ---------- 客户端 ----------


class Remote(Protocol):
    def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        """入参：同步请求体。出参：响应体；失败时抛 SyncError。"""
        ...


@dataclass
class HttpRemote:
    url: str
    token: str
    ca_file: str | None = None
    timeout_s: float = 60.0

    def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        """入参：同步请求体。出参：响应体。经 HTTPS 调用服务器的 `/sync`。"""
        from zymor.remote import RemoteError, http_json

        try:
            return http_json(
                f"{self.url}/sync",
                self.token,
                payload,
                ca_file=self.ca_file,
                timeout_s=self.timeout_s,
                max_bytes=MAX_SYNC_RESPONSE_BYTES,
            )
        except RemoteError as error:
            raise SyncError(error.message, hint=error.hint) from error


@dataclass
class LocalRemote:
    """进程内的远端：直接调用服务端处理函数。供测试与本机目录备份使用。"""

    vault: Vault
    writable: bool = True

    def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        """入参：同步请求体。出参：响应体。"""
        status, body = handle_sync_request(self.vault, payload, writable=self.writable)
        if status != 200:
            raise SyncError(f"同步请求被拒绝（{status}）: {body.get('message') or body}")
        return body


def remote_from_config(config: Config) -> HttpRemote:
    """入参：配置。出参：HTTP 远端。地址与 token 缺失、或明文传输到非本机地址时拒绝。"""
    import os

    from zymor.remote import read_token

    # [sync] 没有单独配置时，沿用 `zymor connect` 保存的服务器地址与 token。
    url = str(config.get("sync.url", None) or config.get("remote.url", None) or "").rstrip("/")
    if not url:
        raise UsageError(
            "未配置同步服务器地址",
            hint='运行 `zymor connect <地址>`，或在配置里设置 [sync].url = "https://<域名>"',
        )
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise UsageError(f"同步服务器地址不合法: {url}")
    insecure_ok = bool(
        config.get("sync.allow_insecure_http", False)
        or config.get("remote.allow_insecure_http", False)
    )
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS and not insecure_ok:
        raise UsageError(
            "同步服务器地址必须使用 https，否则 token 与知识内容以明文传输",
            hint="改用 https 地址；链路已加密时可设 [sync].allow_insecure_http = true",
        )
    token_env = str(config.get("sync.token_env", "ZYMOR_SYNC_TOKEN") or "ZYMOR_SYNC_TOKEN")
    token = os.environ.get(token_env, "") or read_token(config)
    if not token:
        raise UsageError("未配置同步 token", hint=f"export {token_env}=<服务器的 HTTP token>")
    ca_file = config.get("sync.ca_file", None) or config.get("remote.ca_file", None)
    return HttpRemote(url=url, token=token, ca_file=str(ca_file) if ca_file else None)


@dataclass
class SyncPlan:
    upload: list[str] = field(default_factory=list)
    download: list[str] = field(default_factory=list)
    delete_remote: list[str] = field(default_factory=list)
    remote_deleted: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    in_sync: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：计划字典。"""
        return {
            "upload": self.upload,
            "download": self.download,
            "delete_remote": self.delete_remote,
            "remote_deleted": self.remote_deleted,
            "conflicts": self.conflicts,
            "in_sync": len(self.in_sync),
        }


def build_plan(
    local: dict[str, str],
    remote: dict[str, str],
    base: dict[str, str],
    deleted: set[str] | None = None,
) -> SyncPlan:
    """入参：本地、远端、上次同步时的哈希，以及远端的删除标记。出参：同步计划。

    base 是判断「哪一边改了」的依据：只有一边偏离 base 时跟随那一边；两边都偏离才算冲突。
    """
    plan = SyncPlan()
    tombstoned = deleted or set()
    for path in sorted(set(local) | set(remote)):
        here, there, last = local.get(path), remote.get(path), base.get(path)
        if here == there:
            plan.in_sync.append(path)
        elif there is None:
            # 远端没有。有删除标记且本地没再改过：别的机器主动删除了它，本地跟着删。
            # 其余情况（从未上传、服务器丢了文件、删除之后本地又改过）都以本地为准上传。
            if path in tombstoned and last is not None and last == here:
                plan.remote_deleted.append(path)
            else:
                plan.upload.append(path)
        elif here is None:
            # 本地没有：别的机器新增的，或者本地删除了。
            if last is not None and last == there:
                plan.delete_remote.append(path)
            else:
                plan.download.append(path)
        elif last == there:
            plan.upload.append(path)
        elif last == here:
            plan.download.append(path)
        else:
            plan.conflicts.append(path)
    return plan


def _state_path(vault: Vault) -> Path:
    return vault.root / "capture" / "sync.json"


def _load_base(vault: Vault, key: str) -> dict[str, str]:
    path = _state_path(vault)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    remotes = data.get("remotes") if isinstance(data, dict) else None
    entry = remotes.get(key) if isinstance(remotes, dict) else None
    files = entry.get("files") if isinstance(entry, dict) else None
    if not isinstance(files, dict):
        return {}
    return {str(name): str(value) for name, value in files.items()}


def _save_base(vault: Vault, key: str, files: dict[str, str]) -> None:
    path = _state_path(vault)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("remotes"), dict):
        data = {"schema_version": 1, "remotes": {}}
    data["remotes"][key] = {"files": files, "synced_at": now_iso()}
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


@dataclass
class SyncReport:
    scope: str
    action: str
    plan: SyncPlan
    uploaded: list[str] = field(default_factory=list)
    downloaded: list[str] = field(default_factory=list)
    deleted_remote: list[str] = field(default_factory=list)
    deleted_local: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：同步报告字典。"""
        return {
            "scope": self.scope,
            "action": self.action,
            "plan": self.plan.to_dict(),
            "uploaded": self.uploaded,
            "downloaded": self.downloaded,
            "deleted_remote": self.deleted_remote,
            "deleted_local": self.deleted_local,
            "warnings": self.warnings,
        }


def _validate_node(vault: Vault, path: str, content: str) -> None:
    match = NODE_PATH_RE.fullmatch(path)
    if match is None:
        raise ValidationError("只有节点文件可以从远端写回本地", field="path")
    node = MemoryNode.from_markdown(content)
    if node.id != match.group("id") or node.category != match.group("category"):
        raise ValidationError("节点内容与路径不一致", field="path")
    check_layer_vault(node, vault)
    if detect_sensitive("\n".join([node.title, node.summary, node.body, *node.evidence])):
        raise ValidationError("节点含疑似密钥", field="body")


def _upload(remote: Remote, vault: Vault, scope: str, paths: list[str]) -> dict[str, str]:
    """分批上传；返回成功上传的路径与哈希。"""
    done: dict[str, str] = {}
    batch: list[dict[str, str]] = []
    size = 0

    def flush() -> None:
        nonlocal batch, size
        if not batch:
            return
        remote.call({"action": "push", "scope": scope, "files": batch})
        for item in batch:
            done[item["path"]] = item["sha256"]
        batch, size = [], 0

    for path in paths:
        content = (vault.root / path).read_text(encoding="utf-8")
        encoded = len(content.encode("utf-8"))
        if size and size + encoded > MAX_BATCH_BYTES:
            flush()
        batch.append({"path": path, "content": content, "sha256": sha256_hex(content)})
        size += encoded
    flush()
    return done


def sync_vault(
    vault: Vault,
    remote: Remote,
    *,
    action: str = "run",
    scope: str | None = None,
    remote_key: str = "default",
    include_events: bool = False,
    force: bool = False,
) -> SyncReport:
    """入参：vault、远端与动作（status / push / pull / run）。出参：同步报告。

    本地库是事实源：冲突时默认保留两边不动并报告；`force` 让当前动作的方向覆盖对方。
    别的机器主动删除的节点在 pull 时从本地删除。L0 原始事件只上传，从不写回本地。
    """
    if action not in ("status", "push", "pull", "run"):
        raise UsageError(f"未知同步动作: {action}", hint="status、push、pull 或 run")
    resolved_scope = scope or default_scope(vault)
    if SCOPE_RE.fullmatch(resolved_scope) is None:
        raise UsageError(f"非法分区名: {resolved_scope}")
    key = f"{remote_key}#{resolved_scope}"
    local = local_manifest(vault, include_events=include_events)
    response = remote.call({"action": "manifest", "scope": resolved_scope})
    raw_remote = response.get("files")
    remote_files = (
        {str(name): str(value) for name, value in raw_remote.items() if is_sync_path(name)}
        if isinstance(raw_remote, dict)
        else {}
    )
    if not include_events:
        remote_files = {
            name: value for name, value in remote_files.items() if NODE_PATH_RE.fullmatch(name)
        }
    base = _load_base(vault, key)
    raw_deleted = response.get("deleted")
    tombstoned = {str(path) for path in raw_deleted} if isinstance(raw_deleted, list) else set()
    plan = build_plan(local, remote_files, base, tombstoned)
    # L0 只做备份：远端有而本地没有的事件文件不下载，也不因此删除远端。
    event_only_remote = [path for path in plan.download if EVENT_PATH_RE.fullmatch(path)]
    plan.download = [path for path in plan.download if path not in event_only_remote]
    plan.delete_remote = [path for path in plan.delete_remote if not EVENT_PATH_RE.fullmatch(path)]
    event_conflicts = [path for path in plan.conflicts if EVENT_PATH_RE.fullmatch(path)]
    plan.conflicts = [path for path in plan.conflicts if path not in event_conflicts]
    plan.upload.extend(event_conflicts)
    # 本地的原始对话永远不因远端的删除标记而删除。
    # 被保护规则拦下的路径仍要沿用旧 base。否则下次同步时它们看起来像「从未同步过」，
    # 会被重新上传，把别的机器的删除撤销掉。
    held = [path for path in plan.remote_deleted if EVENT_PATH_RE.fullmatch(path)]
    plan.remote_deleted = [path for path in plan.remote_deleted if path not in held]
    warnings: list[str] = []
    local_nodes = sum(1 for path in local if NODE_PATH_RE.fullmatch(path))
    if len(plan.remote_deleted) > MASS_DELETE_MIN and len(plan.remote_deleted) * 2 > local_nodes:
        # 远端一次要求删掉本地一半以上的节点，更像是服务器出错或被入侵，而不是正常的删除传播。
        warnings.append(
            f"远端要求删除 {len(plan.remote_deleted)} 个本地节点，超过本地总数的一半，已拒绝执行。"
            "确认无误后逐个运行 `zymor forget <id>`，或检查服务器状态"
        )
        held.extend(plan.remote_deleted)
        plan.remote_deleted = []
    report = SyncReport(scope=resolved_scope, action=action, plan=plan, warnings=warnings)
    if action == "status":
        return report

    new_base = {path: local[path] for path in plan.in_sync}
    # 没处理的条目沿用旧 base，下次还能判断是哪一边改的。
    for path in (*plan.upload, *plan.download, *plan.conflicts, *plan.remote_deleted, *held):
        if path in base:
            new_base[path] = base[path]
    for path in plan.delete_remote:
        new_base[path] = base[path]

    if action in ("pull", "run"):
        wanted = list(plan.download) + (list(plan.conflicts) if force and action == "pull" else [])
        if wanted or plan.remote_deleted:
            with vault.writing("sync"):
                for start in range(0, len(wanted), 200):
                    chunk = wanted[start : start + 200]
                    fetched = remote.call(
                        {"action": "pull", "scope": resolved_scope, "paths": chunk}
                    ).get("files", [])
                    for item in fetched if isinstance(fetched, list) else []:
                        path, content = str(item.get("path")), item.get("content")
                        try:
                            if path not in chunk or not isinstance(content, str):
                                raise ValidationError("远端返回了未请求的文件", field="path")
                            if item.get("sha256") != sha256_hex(content):
                                raise ValidationError("校验和不匹配", field="sha256")
                            _validate_node(vault, path, content)
                        except Exception as error:
                            message = error.message if isinstance(error, ZymorError) else str(error)
                            report.warnings.append(f"已跳过 {path}：{message}")
                            continue
                        atomic_write_text(vault.root / path, content)
                        new_base[path] = sha256_hex(content)
                        report.downloaded.append(path)
                for path in plan.remote_deleted:
                    (vault.root / path).unlink(missing_ok=True)
                    new_base.pop(path, None)
                    report.deleted_local.append(path)
            if report.downloaded or report.deleted_local:
                try:
                    Index(vault.root).sync()
                except ZymorError as error:
                    report.warnings.append(
                        f"索引同步失败，可运行 `zymor index sync`：{error.message}"
                    )

    if action in ("push", "run"):
        wanted = list(plan.upload) + (list(plan.conflicts) if force and action == "push" else [])
        new_base.update(_upload(remote, vault, resolved_scope, wanted))
        report.uploaded = wanted
        if plan.delete_remote:
            removed = remote.call(
                {"action": "delete", "scope": resolved_scope, "paths": plan.delete_remote}
            ).get("deleted", [])
            report.deleted_remote = (
                [str(path) for path in removed] if isinstance(removed, list) else []
            )
            for path in plan.delete_remote:
                new_base.pop(path, None)

    unresolved = [
        path for path in plan.conflicts if path not in (*report.uploaded, *report.downloaded)
    ]
    if unresolved:
        report.warnings.append(
            f"{len(unresolved)} 个文件两边都改过，已保持不动："
            + "、".join(unresolved[:5])
            + ("…" if len(unresolved) > 5 else "")
            + "。用 `zymor sync push --force` 以本地为准，或 `zymor sync pull --force` 以远端为准"
        )
    _save_base(vault, key, new_base)
    return report


def mirror_vault(remote: Remote, target: Path) -> dict[str, Any]:
    """入参：远端与本地目录。出参：镜像统计。把服务器的库下载成一份只读的 Markdown 副本。

    只下载内容有变化的节点；服务器上已不存在的节点从副本里删除。副本不是 vault，不参与检索。
    """
    target = target.expanduser()
    if (target / "vault.json").exists():
        raise UsageError(
            f"镜像目标是一个 Zymor 库: {target}",
            hint="镜像会删除目标里服务器没有的节点。换一个专用目录，例如 ~/.zymor/mirror",
        )
    response = remote.call({"action": "manifest", "scope": VAULT_SCOPE})
    raw = response.get("files")
    wanted = (
        {str(name): str(value) for name, value in raw.items() if NODE_PATH_RE.fullmatch(str(name))}
        if isinstance(raw, dict)
        else {}
    )
    existing: dict[str, str] = {}
    if (target / "nodes").is_dir():
        for file_path in (target / "nodes").rglob("*.md"):
            relative = file_path.relative_to(target).as_posix()
            if NODE_PATH_RE.fullmatch(relative):
                existing[relative] = sha256_hex(file_path.read_bytes())
    changed = [path for path, digest in wanted.items() if existing.get(path) != digest]
    downloaded: list[str] = []
    for start in range(0, len(changed), 200):
        chunk = changed[start : start + 200]
        fetched = remote.call({"action": "pull", "scope": VAULT_SCOPE, "paths": chunk}).get("files")
        for item in fetched if isinstance(fetched, list) else []:
            path, content = str(item.get("path")), item.get("content")
            if path not in chunk or not isinstance(content, str):
                continue
            if item.get("sha256") != sha256_hex(content):
                continue
            atomic_write_text(target / path, content)
            downloaded.append(path)
    deleted = [path for path in existing if path not in wanted]
    if existing and not wanted:
        # 服务器返回空清单时不清空已有副本：更可能是服务器出了问题。
        deleted = []
    for path in deleted:
        (target / path).unlink(missing_ok=True)
    return {
        "path": str(target),
        "downloaded": downloaded,
        "deleted": deleted,
        "total": len(wanted),
    }
