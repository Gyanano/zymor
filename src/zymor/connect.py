"""`zymor connect`: point this machine at a Zymor server and wire up the host hooks."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

from zymor.errors import UsageError, ValidationError
from zymor.remote import RemoteError, http_json
from zymor.util.fs import atomic_write_text

HOOK_EVENTS: tuple[tuple[str, str], ...] = (
    ("SessionStart", "context"),
    ("UserPromptSubmit", "recall"),
    ("Stop", "capture"),
    ("PreCompact", "capture"),
    ("SessionEnd", "capture"),
)
SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*(#.*)?$")


def user_config_path() -> Path:
    """入参：无。出参：用户级配置文件路径。"""
    return Path("~/.zymor/config.toml").expanduser()


def zymor_command() -> str:
    """入参：无。出参：钩子里调用 zymor 的命令前缀。优先用 PATH 上的可执行文件的绝对路径。"""
    found = shutil.which("zymor")
    if found:
        return shlex.quote(str(Path(found).resolve()))
    return f"{shlex.quote(sys.executable)} -m zymor"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def set_toml_values(text: str, section: str, values: dict[str, str]) -> str:
    """入参：TOML 文本、表名与字符串键值。出参：更新后的文本。

    只改指定表里的指定键，其余内容（含注释）原样保留。表不存在时追加到末尾。
    """
    lines = text.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if (match := SECTION_RE.match(line)) and match.group("name").strip() == section
        ),
        None,
    )
    if start is None:
        block = [
            f"[{section}]",
            *(f"{key} = {_toml_string(value)}" for key, value in values.items()),
        ]
        prefix = lines + [""] if lines and lines[-1].strip() else lines
        return "\n".join([*prefix, *block]) + "\n"
    end = next(
        (index for index in range(start + 1, len(lines)) if SECTION_RE.match(lines[index])),
        len(lines),
    )
    remaining = dict(values)
    for index in range(start + 1, end):
        key = lines[index].split("=", 1)[0].strip()
        if key in remaining and "=" in lines[index] and not lines[index].lstrip().startswith("#"):
            lines[index] = f"{key} = {_toml_string(remaining.pop(key))}"
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines[insert_at:insert_at] = [
        f"{key} = {_toml_string(value)}" for key, value in remaining.items()
    ]
    return "\n".join(lines) + "\n"


def save_connection(
    url: str, token: str, *, ca_file: str | None = None, token_file: Path | None = None
) -> dict[str, str]:
    """入参：服务器地址、token 与可选 CA 文件。出参：写入的文件路径。token 文件权限为 600。"""
    config_path = _real_path(user_config_path())
    existing = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    values = {"url": url}
    if ca_file:
        values["ca_file"] = ca_file
    updated = set_toml_values(existing, "remote", values)
    # 行级编辑认不出所有 TOML 写法（点分键、带引号的键）。写入前重新解析，
    # 结果不对就不写，避免把用户的配置文件改坏。
    try:
        parsed = tomllib.loads(updated).get("remote", {})
    except tomllib.TOMLDecodeError:
        parsed = None
    if not isinstance(parsed, dict) or any(parsed.get(k) != v for k, v in values.items()):
        raise UsageError(
            f"无法自动修改 {config_path}",
            hint=f'请手工在其中的 [remote] 表里设置 url = "{url}"，然后重新运行本命令',
        )
    if existing and updated != existing:
        shutil.copy2(config_path, config_path.with_name(config_path.name + ".zymor-bak"))
    atomic_write_text(config_path, updated)
    target = token_file or Path("~/.zymor/remote.token").expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    # 先删旧文件再以 600 新建：不沿用旧文件可能过宽的权限，也不跟随软链接。
    target.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return {"config": str(config_path), "token_file": str(target)}


def _real_path(path: Path) -> Path:
    """入参：路径。出参：软链接指向的真实路径。原子替换会把软链接换成普通文件，所以写真实文件。"""
    return path.resolve() if path.is_symlink() else path


def verify_connection(url: str, token: str, *, ca_file: str | None = None) -> None:
    """入参：服务器地址与 token。出参：无。服务器不可达或 token 不被接受时抛错。"""
    try:
        health = http_json(f"{url}/healthz", "", None, ca_file=ca_file, timeout_s=8)
    except RemoteError as error:
        raise UsageError(f"连不上服务器：{error.message}", hint="检查地址、证书与网络") from error
    if health.get("status") != "ok":
        raise UsageError(
            "这个地址不是 Zymor 服务器", hint="地址应当指向 `zymor mcp --transport http`"
        )
    try:
        http_json(f"{url}/recall", token, {"query": "zymor connect"}, ca_file=ca_file, timeout_s=8)
    except RemoteError as error:
        raise UsageError(f"服务器不接受这个 token：{error.message}", hint=error.hint) from error


def install_claude_code_hooks(settings_path: Path, command: str) -> dict[str, Any]:
    """入参：Claude Code 的 settings.json 路径与 zymor 命令前缀。出参：改动摘要。

    只追加缺少的钩子，不动已有内容；改写前把原文件备份为 `<文件名>.zymor-bak`。
    """
    settings_path = _real_path(settings_path)
    data: dict[str, Any] = {}
    if settings_path.is_file():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValidationError(
                f"无法解析 {settings_path}: {error}",
                field="settings",
                hint="修复这个 JSON 文件后重试，或按文档手工添加钩子",
            ) from error
        if not isinstance(loaded, dict):
            raise ValidationError(f"{settings_path} 的顶层必须是对象", field="settings")
        data = loaded
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValidationError("settings.json 的 hooks 必须是对象", field="settings")
    added: list[str] = []
    for event, subcommand in HOOK_EVENTS:
        wanted = f"{command} hook {subcommand}"
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValidationError(f"hooks.{event} 必须是数组", field="settings")
        present = any(
            isinstance(item, dict)
            and str(item.get("command", "")).endswith(f"hook {subcommand}")
            and "zymor" in str(item.get("command", ""))
            for group in groups
            if isinstance(group, dict)
            for item in (group.get("hooks") or [])
        )
        if present:
            continue
        groups.append({"hooks": [{"type": "command", "command": wanted}]})
        added.append(event)
    backup: str | None = None
    if added:
        if settings_path.is_file():
            backup_path = settings_path.with_name(settings_path.name + ".zymor-bak")
            shutil.copy2(settings_path, backup_path)
            backup = str(backup_path)
        atomic_write_text(settings_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return {"settings": str(settings_path), "added": added, "backup": backup}
