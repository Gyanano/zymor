from __future__ import annotations

import copy
import re
from typing import Any

import yaml

from zymor.errors import ValidationError

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


class _Loader(yaml.SafeLoader):
    pass


_Loader.yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for _initial in list(_Loader.yaml_implicit_resolvers):
    _Loader.yaml_implicit_resolvers[_initial] = [
        (tag, regexp)
        for tag, regexp in _Loader.yaml_implicit_resolvers[_initial]
        if tag != "tag:yaml.org,2002:timestamp"
    ]


class _Dumper(yaml.SafeDumper):
    pass


def _represent_str(dumper: _Dumper, value: str) -> yaml.Node:
    style = "'" if ISO_RE.match(value) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _represent_str)


def dump_yaml(data: dict[str, Any]) -> str:
    """入参：frontmatter 字典。出参：单行顺序保留的 YAML 文本。时间戳强制加引号以保持字符串类型。"""
    dumped: str = yaml.dump(
        data,
        Dumper=_Dumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    return dumped


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """入参：带 frontmatter 的全文。出参：frontmatter 与正文。不合法时抛 ValidationError。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.startswith("\ufeff"):
        raise ValidationError(
            "文件带 UTF-8 BOM；Zymor 只接受无 BOM 的 UTF-8",
            field="encoding",
            hint="用编辑器重新保存为 UTF-8（无 BOM）后重试",
        )
    if not normalized.startswith("---\n"):
        raise ValidationError("缺少 YAML frontmatter：文件必须以 `---` 行开头", field="frontmatter")
    lines = normalized[4:].split("\n")
    end: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == "---":
            end = index
            break
    if end is None:
        raise ValidationError("frontmatter 未闭合：缺少结束的 `---` 行", field="frontmatter")
    yaml_text = "\n".join(lines[:end])
    try:
        data = yaml.load(yaml_text, Loader=_Loader) if yaml_text.strip() else {}
    except yaml.YAMLError as error:
        raise ValidationError(f"frontmatter 不是合法 YAML: {error}", field="frontmatter") from error
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValidationError("frontmatter 必须是 YAML mapping", field="frontmatter")
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    if body and not body.endswith("\n"):
        body += "\n"
    return data, body
