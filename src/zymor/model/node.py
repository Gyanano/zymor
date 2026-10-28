from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from zymor.errors import ValidationError
from zymor.tokenizer import estimate_tokens
from zymor.util.ids import is_memory_id
from zymor.util.time import is_iso, now_iso
from zymor.util.yamlutil import dump_yaml, split_frontmatter

CATEGORIES = ("episodic", "semantic", "procedural", "axioms")
LAYERS = ("L2", "L3", "L4")
STATUSES = ("hypothesis", "verified", "disputed", "deprecated")
CREATED_BY = ("agent", "user", "dream")

REQUIRED_H2: dict[str, tuple[str, ...]] = {
    "episodic": ("背景", "决策", "结果与教训"),
    "semantic": ("结论", "证据"),
    "procedural": ("适用场景", "步骤"),
    "axioms": ("触发情境", "行为指引", "正例", "反例"),
}
CAPSULE_H2 = ("目标", "当前状态", "下一步", "关键决策与坑")
TOMBSTONE_H2 = "Tombstone"

REQUIRED_FIELDS: tuple[str, ...] = (
    "schema_version",
    "id",
    "title",
    "summary",
    "category",
    "layer",
    "status",
    "confidence",
    "arousal",
    "hit_count",
    "verify_count",
    "created_at",
    "updated_at",
    "last_verified",
    "valid_from",
    "source",
    "created_by",
)
OPTIONAL_FIELDS: tuple[str, ...] = (
    "valid_to",
    "supersedes",
    "superseded_by",
    "tags",
    "evidence",
    "applies_to",
    "project",
    "anchors",
    "projects",
)

LINK_RE = re.compile(r"\[\[(mem_\d{8}_[0-9a-f]{10})\]\]")
KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

MAX_FILE_BYTES = 64 * 1024
MAX_TITLE = 80
MAX_SUMMARY = 400
MAX_BODY_TOKENS = 1000
MAX_BODY_CJK = 700


def extract_links(body: str) -> list[str]:
    """入参：节点正文。出参：去重后的 WikiLink 目标 id 列表（按出现顺序）。"""
    found: list[str] = []
    seen: set[str] = set()
    for match in LINK_RE.finditer(body):
        target = match.group(1)
        if target not in seen:
            seen.add(target)
            found.append(target)
    return found


def _cjk_count(text: str) -> int:
    from zymor.tokenizer import _is_cjk

    return sum(1 for char in text if _is_cjk(char))


class MemoryNode(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_version: int = 1
    id: str
    title: str
    summary: str
    category: Literal["episodic", "semantic", "procedural", "axioms"]
    layer: Literal["L2", "L3", "L4"]
    status: Literal["hypothesis", "verified", "disputed", "deprecated"]
    confidence: float = Field(ge=0.0, le=1.0)
    arousal: float = Field(ge=0.0, le=1.0)
    hit_count: int = Field(ge=0)
    verify_count: int = Field(ge=0)
    created_at: str
    updated_at: str
    last_verified: str
    valid_from: str
    source: str
    created_by: Literal["agent", "user", "dream"]
    valid_to: str | None = None
    supersedes: list[str] = Field(default_factory=list)
    superseded_by: str | None = None
    tags: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    applies_to: list[str] = Field(default_factory=list)
    project: str | None = None
    anchors: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    body: str = Field(default="", exclude=True)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not is_memory_id(value):
            raise ValueError("必须匹配 `mem_<YYYYMMDD>_<10位小写hex>`")
        return value

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"v0 只支持 schema_version=1，收到 {value}")
        return value

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title 不能为空")
        if "\n" in value:
            raise ValueError("title 必须是单行")
        if len(value) > MAX_TITLE:
            raise ValueError(f"title 超过 {MAX_TITLE} 字符")
        return value

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary 不能为空")
        if len(value) > MAX_SUMMARY:
            raise ValueError(f"summary 超过 {MAX_SUMMARY} 字符")
        return value

    @field_validator("created_at", "updated_at", "last_verified", "valid_from")
    @classmethod
    def _validate_required_time(cls, value: str) -> str:
        if not is_iso(value):
            raise ValueError("必须是带时区的 ISO8601 文本")
        return value

    @field_validator("valid_to")
    @classmethod
    def _validate_valid_to(cls, value: str | None) -> str | None:
        if value is not None and not is_iso(value):
            raise ValueError("必须是带时区的 ISO8601 文本")
        return value

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        if not re.match(r"^(session:.+|user|dream:.+|import:.+|research:.+)$", value):
            raise ValueError(
                "必须是 `session:<id>` / `user` / `dream:<id>` / "
                "`import:<path>` / `research:<ticket>`"
            )
        return value

    @field_validator("supersedes")
    @classmethod
    def _validate_supersedes(cls, value: list[str]) -> list[str]:
        for item in value:
            if not is_memory_id(item):
                raise ValueError(f"非法节点 id: {item}")
        return value

    @field_validator("superseded_by")
    @classmethod
    def _validate_superseded_by(cls, value: str | None) -> str | None:
        if value is not None and not is_memory_id(value):
            raise ValueError(f"非法节点 id: {value}")
        return value

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: list[str]) -> list[str]:
        for item in value:
            if not KEBAB_RE.match(item):
                raise ValueError(f"tag 必须 kebab-case: {item}")
        return value

    @field_validator("project")
    @classmethod
    def _validate_project(cls, value: str | None) -> str | None:
        if value is not None and not SLUG_RE.match(value):
            raise ValueError(f"project 必须是 slug: {value}")
        return value

    @model_validator(mode="after")
    def _validate_body(self) -> MemoryNode:
        self._check_identity_rules()
        self._check_body()
        return self

    def _check_identity_rules(self) -> None:
        if self.layer == "L4":
            if self.category != "axioms":
                raise ValueError("layer: L4 必须是 category: axioms")
            if not self.applies_to:
                raise ValueError("layer: L4 必须提供非空 applies_to")
        if self.layer == "L2":
            if self.category != "episodic":
                raise ValueError("layer: L2 必须是 category: episodic")
            if not self.project:
                raise ValueError("layer: L2 必须提供 project")
        if self.status == "deprecated":
            if not self.valid_to:
                raise ValueError("status: deprecated 必须提供 valid_to")
            if not self.evidence:
                raise ValueError("status: deprecated 必须提供 evidence")
        if self.superseded_by is not None and not self.valid_to:
            raise ValueError("superseded_by 非空时 valid_to 必须非空")

    def _check_body(self) -> None:
        body = self.body
        if not body.strip():
            raise ValueError("正文不能为空")
        lines = body.strip("\n").split("\n")
        expected_h1 = f"# {self.title}"
        if lines[0].strip() != expected_h1:
            raise ValueError(f"正文首行必须是 H1：`{expected_h1}`")
        headings = {line.strip() for line in lines}
        required = list(REQUIRED_H2[self.category])
        if self.layer == "L2":
            required.extend(CAPSULE_H2)
        if self.status == "deprecated":
            required.append(TOMBSTONE_H2)
        for section in required:
            if f"## {section}" not in headings:
                raise ValueError(f"缺少必需小节：`## {section}`")
        if estimate_tokens(body) > MAX_BODY_TOKENS:
            raise ValueError(f"正文超过 {MAX_BODY_TOKENS} tokens，必须拆分")
        if _cjk_count(body) > MAX_BODY_CJK:
            raise ValueError(f"正文超过 {MAX_BODY_CJK} 中文字，必须拆分")

    def links(self) -> list[str]:
        """入参：无。出参：正文链接目标 id（排除自身）。"""
        return [target for target in extract_links(self.body) if target != self.id]

    def unknown_fields(self) -> dict[str, Any]:
        """入参：无。出参：frontmatter 中的未知字段（原样保留）。"""
        return dict(self.model_extra or {})

    def to_frontmatter(self) -> dict[str, Any]:
        """入参：无。出参：按规格顺序排列的 frontmatter 字典（可选字段按需输出）。"""
        data: dict[str, Any] = {}
        for name in REQUIRED_FIELDS:
            data[name] = getattr(self, name)
        for name in OPTIONAL_FIELDS:
            value = getattr(self, name)
            if name in {"supersedes", "tags", "evidence", "applies_to", "anchors", "projects"}:
                if value:
                    data[name] = value
            elif value is not None:
                data[name] = value
        for name, value in (self.model_extra or {}).items():
            if name not in data:
                data[name] = value
        return data

    def to_markdown(self) -> str:
        """入参：无。出参：节点 Markdown 全文（frontmatter + 正文）。"""
        front = dump_yaml(self.to_frontmatter())
        body = self.body.rstrip("\n") + "\n"
        return f"---\n{front}---\n\n{body}"

    @classmethod
    def from_markdown(cls, text: str) -> MemoryNode:
        """入参：节点 Markdown 全文。出参：校验通过的节点。格式或约束不合法时抛 ValidationError。"""
        data, body = split_frontmatter(text)
        payload: dict[str, Any] = dict(data)
        payload["body"] = body
        try:
            node = cls.model_validate(payload)
        except PydanticValidationError as error:
            raise _as_validation_error(error) from None
        encoded = node.to_markdown().encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise ValidationError(f"节点文件超过 {MAX_FILE_BYTES} 字节，必须拆分", field="body")
        return node


def _as_validation_error(error: PydanticValidationError) -> ValidationError:
    first = error.errors()[0]
    location = ".".join(str(part) for part in first["loc"]) or "node"
    message = str(first["msg"])
    return ValidationError(f"{location}: {message}", field=location)


def new_node(
    *,
    title: str,
    summary: str,
    category: str,
    layer: str,
    body: str,
    source: str,
    created_by: str = "agent",
    project: str | None = None,
    applies_to: list[str] | None = None,
    tags: list[str] | None = None,
    evidence: list[str] | None = None,
    anchors: list[str] | None = None,
    projects: list[str] | None = None,
    status: str = "hypothesis",
    confidence: float = 0.4,
    arousal: float = 0.9,
    node_id: str | None = None,
) -> MemoryNode:
    """入参：新建节点的必需内容与来源。出参：通过校验的节点。用于写入路径构造新节点。"""
    from zymor.util.ids import new_memory_id

    created = now_iso()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "id": node_id or new_memory_id(),
        "title": title,
        "summary": summary,
        "category": category,
        "layer": layer,
        "status": status,
        "confidence": confidence,
        "arousal": arousal,
        "hit_count": 0,
        "verify_count": 0,
        "created_at": created,
        "updated_at": created,
        "last_verified": created,
        "valid_from": created,
        "source": source,
        "created_by": created_by,
        "tags": tags or [],
        "evidence": evidence or [],
        "applies_to": applies_to or [],
        "project": project,
        "anchors": anchors or [],
        "projects": projects or [],
        "body": body,
    }
    try:
        return MemoryNode.model_validate(payload)
    except PydanticValidationError as error:
        raise _as_validation_error(error) from None
