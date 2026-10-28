from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zymor.errors import ValidationError
from zymor.tokenizer import estimate_tokens
from zymor.util.ids import is_session_id
from zymor.util.time import is_iso, now_iso
from zymor.util.yamlutil import dump_yaml, split_frontmatter

TASK_TYPES = ("debugging", "feature", "research", "refactor", "writing", "other")
FOCUS_STATUSES = ("active", "closed")
FOCUS_H2 = ("当前目标", "活跃假设", "已排除路径", "待验证", "最近上下文")
SECTION_TOKEN_LIMIT = 100


class FocusFile(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    session_id: str
    project: str
    task: str
    task_type: Literal["debugging", "feature", "research", "refactor", "writing", "other"]
    status: Literal["active", "closed"] = "active"
    created_at: str
    updated_at: str
    token_budget: int = Field(default=500, ge=1)
    body: str = Field(default="", exclude=True)

    @field_validator("session_id")
    @classmethod
    def _validate_session(cls, value: str) -> str:
        if not is_session_id(value):
            raise ValueError("会话标识必须字母数字开头，允许 `._-`，长度 ≤128")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def _validate_time(cls, value: str) -> str:
        if not is_iso(value):
            raise ValueError("必须是带时区的 ISO8601 文本")
        return value

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"v0 只支持 schema_version=1，收到 {value}")
        return value

    @model_validator(mode="after")
    def _validate_body(self) -> FocusFile:
        if not self.body.strip():
            raise ValueError("焦点文件正文不能为空")
        lines = [line.strip() for line in self.body.strip("\n").split("\n")]
        headings = {line for line in lines if line.startswith("## ")}
        for section in FOCUS_H2:
            if f"## {section}" not in headings:
                raise ValueError(f"缺少必需小节：`## {section}`")
        sections = _split_sections(lines)
        for name, content in sections.items():
            if estimate_tokens(content) > SECTION_TOKEN_LIMIT:
                raise ValueError(f"小节 `{name}` 超过 {SECTION_TOKEN_LIMIT} tokens")
        return self

    def to_markdown(self) -> str:
        """入参：无。出参：焦点文件 Markdown 全文。"""
        front = dump_yaml(self.to_frontmatter())
        body = self.body.rstrip("\n") + "\n"
        return f"---\n{front}---\n\n{body}"

    def to_frontmatter(self) -> dict[str, Any]:
        """入参：无。出参：焦点文件 frontmatter 字典。"""
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "project": self.project,
            "task": self.task,
            "task_type": self.task_type,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "token_budget": self.token_budget,
            **(self.model_extra or {}),
        }

    @classmethod
    def from_markdown(cls, text: str) -> FocusFile:
        """入参：焦点文件 Markdown 全文。出参：校验通过的焦点文件。"""
        data, body = split_frontmatter(text)
        payload: dict[str, Any] = dict(data)
        payload["body"] = body
        try:
            return cls.model_validate(payload)
        except Exception as error:
            if isinstance(error, ValidationError):
                raise
            raise ValidationError(f"焦点文件校验失败: {error}", field="focus") from None


def _split_sections(lines: list[str]) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = line[3:].strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


def new_focus(
    session_id: str,
    project: str,
    task: str,
    task_type: str = "other",
    token_budget: int = 500,
) -> FocusFile:
    """入参：会话、项目、任务与类型。出参：带空小节模板的新焦点文件。"""
    created = now_iso()
    body_lines = ["# 焦点", ""]
    for section in FOCUS_H2:
        body_lines.extend([f"## {section}", ""])
    payload: dict[str, Any] = {
        "schema_version": 1,
        "session_id": session_id,
        "project": project,
        "task": task,
        "task_type": task_type,
        "status": "active",
        "created_at": created,
        "updated_at": created,
        "token_budget": token_budget,
        "body": "\n".join(body_lines),
    }
    try:
        return FocusFile.model_validate(payload)
    except Exception as error:
        raise ValidationError(f"焦点文件构造失败: {error}", field="focus") from None
