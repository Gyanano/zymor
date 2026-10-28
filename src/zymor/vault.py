from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zymor import __version__
from zymor.errors import LockedError, NotFoundError, UserError, ValidationError
from zymor.util.fs import atomic_write_text, ensure_dir
from zymor.util.time import is_iso, now_iso

VAULT_VERSION = 1
VAULT_FILE = "vault.json"
LOCK_FILE = ".lock"
VAULT_SUBDIRS = (
    "nodes",
    "atlas",
    "index",
    "dream",
    "dream/runs",
    "audit",
    "L0",
    "L1/focus",
)
VAULT_GITIGNORE = "L0/\nL1/\nindex/\naudit/\ndream/watermarks.json\n.lock\n"
CONFIG_TEMPLATE = """# Zymor 配置模板；键与默认值见 project configuration documentation
[vault]
# path = ".zymor/vault"
"""


@dataclass(frozen=True)
class VaultInfo:
    schema_version: int
    vault_id: str
    kind: str
    name: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：vault.json 内容字典。"""
        return {
            "schema_version": self.schema_version,
            "vault_id": self.vault_id,
            "kind": self.kind,
            "name": self.name,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> VaultInfo:
        """入参：vault.json 解析结果。出参：校验通过的 VaultInfo。"""
        if not isinstance(data, dict):
            raise UserError(
                "vault.json 必须是 JSON 对象", hint="删除损坏的 vault.json 后重新执行 zymor init"
            )
        version = data.get("schema_version")
        if not isinstance(version, int):
            raise UserError("vault.json 缺少 schema_version", hint="检查 vault.json")
        if version > VAULT_VERSION:
            raise UserError(
                f"vault 布局版本 {version} 高于程序支持的 {VAULT_VERSION}",
                hint="升级 zymor 后再打开该 vault",
            )
        kind = data.get("kind")
        if kind not in {"global", "project"}:
            raise UserError(f"vault.json 的 kind 非法: {kind!r}", hint="检查 vault.json")
        for field_name in ("vault_id", "name", "created_at"):
            if not isinstance(data.get(field_name), str):
                raise UserError(f"vault.json 缺少字段 {field_name}", hint="检查 vault.json")
        if not is_iso(data["created_at"]):
            raise UserError("vault.json 的 created_at 不是合法时间", hint="检查 vault.json")
        return cls(
            schema_version=version,
            vault_id=data["vault_id"],
            kind=kind,
            name=data["name"],
            created_at=data["created_at"],
        )


class LockHandle:
    def __init__(self, path: Path, payload: dict[str, Any]) -> None:
        self.path = path
        self.payload = payload

    def release(self) -> None:
        """入参：无。出参：无。释放写入锁（仅当锁仍属于本进程时删除）。"""
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        if isinstance(current, dict):
            same_pid = current.get("pid") == self.payload.get("pid")
            same_start = current.get("started_at") == self.payload.get("started_at")
            if not (same_pid and same_start):
                return
        self.path.unlink(missing_ok=True)


class Vault:
    def __init__(self, root: Path, info: VaultInfo) -> None:
        self.root = root
        self.info = info

    @property
    def nodes_dir(self) -> Path:
        return self.root / "nodes"

    @property
    def atlas_dir(self) -> Path:
        return self.root / "atlas"

    @property
    def index_dir(self) -> Path:
        return self.root / "index"

    @property
    def index_path(self) -> Path:
        return self.index_dir / "memory.sqlite3"

    @property
    def dream_dir(self) -> Path:
        return self.root / "dream"

    @property
    def runs_dir(self) -> Path:
        return self.dream_dir / "runs"

    @property
    def dream_log_path(self) -> Path:
        return self.dream_dir / "log.jsonl"

    @property
    def watermarks_path(self) -> Path:
        return self.dream_dir / "watermarks.json"

    @property
    def dissonance_path(self) -> Path:
        return self.dream_dir / "dissonance.md"

    @property
    def audit_dir(self) -> Path:
        return self.root / "audit"

    @property
    def writes_audit_path(self) -> Path:
        return self.audit_dir / "writes.jsonl"

    @property
    def retrieval_audit_path(self) -> Path:
        return self.audit_dir / "retrieval.jsonl"

    @property
    def l0_dir(self) -> Path:
        return self.root / "L0"

    @property
    def focus_dir(self) -> Path:
        return self.root / "L1" / "focus"

    @property
    def lock_path(self) -> Path:
        return self.root / LOCK_FILE

    def relative(self, path: Path) -> str:
        """入参：vault 内路径。出参：相对于 vault 根的正斜杠路径。索引与审计存储用。"""
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def absolute(self, relative_path: str) -> Path:
        """入参：vault 内相对路径。出参：绝对路径。"""
        return self.root / relative_path

    def node_path(self, category: str, node_id: str) -> Path:
        """入参：分类与节点 id。出参：节点文件路径。"""
        return self.nodes_dir / category / f"{node_id}.md"

    def find_node_path(self, node_id: str) -> Path | None:
        """入参：节点 id。出参：节点文件路径；不存在返回 None。"""
        matches = sorted(self.nodes_dir.glob(f"*/{node_id}.md"))
        if not matches:
            return None
        if len(matches) > 1:
            duplicates = ", ".join(str(item.relative_to(self.root)) for item in matches)
            raise UserError(
                f"节点 {node_id} 存在多份文件: {duplicates}",
                hint="保留一份后执行 zymor index rebuild",
            )
        return matches[0]

    def focus_path(self, session_id: str) -> Path:
        return self.focus_dir / f"{session_id}.md"

    def is_locked(self) -> bool:
        """入参：无。出参：写入锁是否存在。"""
        return self.lock_path.exists()

    def lock_holder(self) -> dict[str, Any] | None:
        """入参：无。出参：锁持有者信息；无锁返回 None。"""
        try:
            raw = self.lock_path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
        return data if isinstance(data, dict) else {"raw": raw}

    @contextmanager
    def writing(self, command: str) -> Iterator[LockHandle]:
        """入参：命令名（写入审计用）。出参：锁句柄上下文。拿不到锁时抛 LockedError。"""
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": now_iso(),
            "command": command,
            "version": __version__,
        }
        ensure_dir(self.root)
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            holder = self.lock_holder() or {}
            holder_text = (
                f"pid={holder.get('pid')} host={holder.get('host')} command={holder.get('command')}"
            )
            raise LockedError(
                f"写入锁被占用: {holder_text}",
                holder=holder,
            ) from error
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
        except BaseException:
            self.lock_path.unlink(missing_ok=True)
            raise
        handle_out = LockHandle(self.lock_path, payload)
        try:
            yield handle_out
        finally:
            handle_out.release()

    @classmethod
    def open(cls, root: Path) -> Vault:
        """入参：vault 根路径。出参：打开并校验过的 Vault。不存在或版本不合法时抛错。"""
        resolved = root.expanduser().resolve()
        meta_path = resolved / VAULT_FILE
        if not meta_path.is_file():
            raise NotFoundError(
                f"不是 Zymor vault: {resolved}",
                hint="在项目根目录运行 `zymor init`，或 `zymor init --global` 初始化用户库",
            )
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise UserError(f"vault.json 不是合法 JSON: {error}", hint="检查 vault.json") from error
        return cls(resolved, VaultInfo.from_dict(data))

    @classmethod
    def init(cls, root: Path, kind: str, name: str) -> Vault:
        """入参：根路径、库类型（global/project）、名称。出参：初始化后的 Vault；重复执行幂等。"""
        if kind not in {"global", "project"}:
            raise ValidationError(f"未知 vault 类型: {kind}", field="kind")
        resolved = root.expanduser().resolve()
        meta_path = resolved / VAULT_FILE
        if meta_path.is_file():
            existing = cls.open(resolved)
            if existing.info.kind != kind:
                raise UserError(
                    f"该路径已是 {existing.info.kind} vault，不能按 {kind} 重新初始化",
                    hint="使用不同路径，或删除 vault.json 后重试",
                )
            return existing
        import secrets

        ensure_dir(resolved)
        for subdir in VAULT_SUBDIRS:
            ensure_dir(resolved / subdir)
        info = VaultInfo(
            schema_version=VAULT_VERSION,
            vault_id=f"vlt_{secrets.token_hex(8)}",
            kind=kind,
            name=name,
            created_at=now_iso(),
        )
        atomic_write_text(
            meta_path, json.dumps(info.to_dict(), ensure_ascii=False, indent=2) + "\n"
        )
        gitignore_path = resolved / ".gitignore"
        if not gitignore_path.exists():
            atomic_write_text(gitignore_path, VAULT_GITIGNORE)
        return cls(resolved, info)


def default_global_root() -> Path:
    """入参：无。出参：用户库默认路径 `~/.zymor/vault`。"""
    return Path("~/.zymor/vault").expanduser()


def project_root_for(cwd: Path) -> Path | None:
    """入参：起始目录。出参：最近的含 `.zymor/` 的祖先目录；找不到返回 None。"""
    from zymor.config import find_project_root

    return find_project_root(cwd)


def project_vault_root(cwd: Path) -> Path:
    """入参：当前目录。出参：项目库路径。项目根取含 `.zymor/` 的祖先或 cwd。"""
    base = project_root_for(cwd) or cwd.resolve()
    return base / ".zymor" / "vault"


def resolve_vault(
    config: Any,
    cli_vault: str | None,
    cwd: Path | None = None,
) -> Vault:
    """入参：配置、`--vault` 参数、当前目录。出参：按优先级定位到的 Vault。"""
    current = (cwd or Path.cwd()).resolve()
    if cli_vault:
        return Vault.open(Path(cli_vault))
    configured = config.vault_path
    if configured is not None:
        return Vault.open(configured)
    candidate = project_vault_root(current)
    if (candidate / VAULT_FILE).is_file():
        return Vault.open(candidate)
    global_candidate = Path(str(config.get("vault.global_path"))).expanduser()
    if (global_candidate / VAULT_FILE).is_file():
        return Vault.open(global_candidate)
    raise NotFoundError(
        "未找到可用的 Zymor vault",
        hint="在项目根目录运行 `zymor init`；用户库运行 `zymor init --global`；或用 --vault 指定",
    )
