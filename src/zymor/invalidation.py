from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from zymor.errors import ZymorError
from zymor.model.node import MemoryNode
from zymor.store.index import Index
from zymor.store.nodes import read_node, update_node
from zymor.util.fs import sha256_hex
from zymor.util.time import now_iso
from zymor.vault import Vault

FILE_ANCHOR_RE = re.compile(r"^file:(?P<path>.+):(?P<sha>[0-9a-f]{64})$")
PACKAGE_ANCHOR_RE = re.compile(r"^package:(?P<path>.+)\|(?P<name>[^|]+)\|(?P<version>.+)$")
API_ANCHOR_RE = re.compile(r"^api:(?P<path>.+)\|(?P<key>[^|]+)\|(?P<version>.+)$")
ANCHOR_LAYERS = ("L2", "L3", "L4")
ANCHOR_STATUSES = ("hypothesis", "verified", "disputed")
PACKAGE_SECTIONS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)


def project_root(vault: Vault) -> Path:
    """入参：项目库或用户库。出参：项目根目录（由 vault.json 记录或按布局回退）。"""
    return vault.project_root


def file_anchor(vault: Vault, file_path: Path) -> str:
    """入参：vault 与文件路径。出参：`file:<project-relative>:<sha256>` 锚点。"""
    root = project_root(vault)
    resolved = file_path.expanduser().resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        relative = resolved
    if not resolved.is_file():
        raise ZymorError(f"锚定文件不存在: {resolved}")
    return f"file:{relative.as_posix()}:{sha256_hex(resolved.read_bytes())}"


def _requirement_version(requirement: str) -> tuple[str, str] | None:
    match = re.match(r"\s*([A-Za-z0-9_.\-/]+)", requirement)
    if match is None:
        return None
    name = match.group(1)
    rest = requirement[match.end() :].strip()
    return name, rest or "*"


def _manifest_versions(path: Path) -> dict[str, str]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ZymorError(f"依赖清单不可读: {path}: {error}") from error
    suffix = path.suffix.lower()
    versions: dict[str, str] = {}
    if suffix == ".json":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ZymorError(f"依赖清单 JSON 解析失败: {path}") from error
        if not isinstance(data, dict):
            raise ZymorError(f"依赖清单必须是对象: {path}")
        for section in PACKAGE_SECTIONS:
            values = data.get(section)
            if isinstance(values, dict):
                for name, version in values.items():
                    versions[str(name)] = str(version)
        return versions
    if suffix == ".toml":
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            raise ZymorError(f"依赖清单 TOML 解析失败: {path}") from error
        project = data.get("project")
        if isinstance(project, dict):
            for requirement in project.get("dependencies") or []:
                parsed = _requirement_version(str(requirement))
                if parsed is not None:
                    versions[parsed[0]] = parsed[1]
            optional = project.get("optional-dependencies")
            if isinstance(optional, dict):
                for requirements in optional.values():
                    for requirement in requirements or []:
                        parsed = _requirement_version(str(requirement))
                        if parsed is not None:
                            versions[parsed[0]] = parsed[1]
        poetry = data.get("tool", {}).get("poetry", {})
        if isinstance(poetry, dict):
            for section in ("dependencies", "dev-dependencies"):
                values = poetry.get(section)
                if isinstance(values, dict):
                    for name, version in values.items():
                        versions[str(name)] = str(version)
        return versions
    raise ZymorError(f"不支持的依赖清单格式: {path}", hint="支持 package.json / pyproject.toml")


def version_anchor(vault: Vault, manifest: Path, package: str) -> str:
    """入参：vault、依赖清单与包名。出参：`package:<path>|<name>|<version>` 锚点。"""
    root = project_root(vault)
    resolved = manifest.expanduser().resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        relative = resolved
    versions = _manifest_versions(resolved)
    if package not in versions:
        raise ZymorError(f"依赖清单中未找到 {package}: {resolved}")
    return f"package:{relative.as_posix()}|{package}|{versions[package]}"


def _manifest_values(path: Path) -> dict[str, str]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ZymorError(f"版本清单不可读: {path}: {error}") from error
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ZymorError(f"版本清单 JSON 解析失败: {path}") from error
    elif suffix == ".toml":
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            raise ZymorError(f"版本清单 TOML 解析失败: {path}") from error
    else:
        raise ZymorError(f"不支持的版本清单格式: {path}", hint="支持 JSON / TOML")
    if not isinstance(data, dict):
        raise ZymorError(f"版本清单必须是对象: {path}")
    flattened: dict[str, str] = {}

    def walk(node: dict[str, Any], prefix: str) -> None:
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                walk(value, dotted)
            elif isinstance(value, (str, int, float, bool)):
                flattened[dotted] = str(value)

    walk(data, "")
    return flattened


def api_anchor(vault: Vault, manifest: Path, key: str) -> str:
    """入参：vault、版本清单与点分 key。出参：`api:<path>|<key>|<version>` 锚点。"""
    root = project_root(vault)
    resolved = manifest.expanduser().resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        relative = resolved
    values = _manifest_values(resolved)
    if key not in values:
        raise ZymorError(f"版本清单中未找到 {key}: {resolved}")
    return f"api:{relative.as_posix()}|{key}|{values[key]}"


def stale_reasons(vault: Vault, node: MemoryNode) -> list[str]:
    """入参：vault 与节点。出参：失效原因列表；空列表表示锚点仍有效。"""
    if not node.anchors:
        return []
    root = project_root(vault)
    reasons: list[str] = []
    for anchor in node.anchors:
        file_match = FILE_ANCHOR_RE.match(anchor)
        if file_match is not None:
            target = Path(file_match.group("path"))
            if not target.is_absolute():
                target = root / target
            if not target.is_file():
                reasons.append(f"锚定文件不存在: {file_match.group('path')}")
                continue
            if sha256_hex(target.read_bytes()) != file_match.group("sha"):
                reasons.append(f"锚定文件已变化: {file_match.group('path')}")
            continue
        package_match = PACKAGE_ANCHOR_RE.match(anchor)
        if package_match is not None:
            target = Path(package_match.group("path"))
            if not target.is_absolute():
                target = root / target
            if not target.is_file():
                reasons.append(f"依赖清单不存在: {package_match.group('path')}")
                continue
            try:
                versions = _manifest_versions(target)
            except ZymorError as error:
                reasons.append(f"依赖清单不可解析: {error.message}")
                continue
            name = package_match.group("name")
            if name not in versions:
                reasons.append(f"依赖已移除: {name}")
                continue
            if versions[name] != package_match.group("version"):
                reasons.append(
                    f"依赖版本已变化: {name} {package_match.group('version')} → {versions[name]}"
                )
            continue
        api_match = API_ANCHOR_RE.match(anchor)
        if api_match is not None:
            target = Path(api_match.group("path"))
            if not target.is_absolute():
                target = root / target
            if not target.is_file():
                reasons.append(f"版本清单不存在: {api_match.group('path')}")
                continue
            try:
                values = _manifest_values(target)
            except ZymorError as error:
                reasons.append(f"版本清单不可解析: {error.message}")
                continue
            key = api_match.group("key")
            if key not in values:
                reasons.append(f"API 版本项已移除: {key}")
                continue
            if values[key] != api_match.group("version"):
                reasons.append(
                    f"API 版本已变化: {key} {api_match.group('version')} → {values[key]}"
                )
            continue
        reasons.append(f"无法识别的锚点: {anchor}")
    return reasons


def scan_stale(vault: Vault) -> list[dict[str, Any]]:
    """入参：vault。出参：所有锚点失效的节点摘要。"""
    index = Index(vault.root)
    results: list[dict[str, Any]] = []
    for layer in ANCHOR_LAYERS:
        rows = index.layer_rows(layer, statuses=list(ANCHOR_STATUSES), limit=10000)
        for row in rows:
            try:
                node, _ = read_node(vault, str(row["id"]))
            except ZymorError:
                continue
            reasons = stale_reasons(vault, node)
            if not reasons:
                continue
            results.append(
                {
                    "id": node.id,
                    "title": node.title,
                    "layer": node.layer,
                    "status": node.status,
                    "reasons": reasons,
                }
            )
    return results


def deprecate_stale(vault: Vault, stale: list[dict[str, Any]]) -> list[str]:
    """入参：vault 与失效节点摘要。出参：被标记 deprecated 的节点 id 列表。"""
    changed: list[str] = []
    timestamp = now_iso()
    for item in stale:
        node_id = str(item["id"])
        try:
            node, _ = read_node(vault, node_id)
        except ZymorError:
            continue
        evidence = list(node.evidence)
        marker = f"stale:{timestamp}"
        if marker not in evidence:
            evidence.append(marker)
        body = node.body
        if "## Tombstone" not in body:
            reason_text = "；".join(str(item.get("reasons") or []))
            body = body.rstrip() + f"\n\n## Tombstone\n失效：{reason_text}\n"
        update_node(
            vault,
            node_id,
            {
                "status": "deprecated",
                "valid_to": timestamp,
                "evidence": evidence,
                "body": body,
            },
            command="stale",
        )
        changed.append(node_id)
    return changed
