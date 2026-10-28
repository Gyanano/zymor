from __future__ import annotations

from typing import Any


class ZymorError(Exception):
    exit_code = 1
    kind = "error"

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：可 JSON 序列化的错误对象。供 CLI `--json` 输出错误。"""
        data: dict[str, Any] = {"error": self.kind, "message": self.message}
        if self.hint is not None:
            data["hint"] = self.hint
        return data


class UserError(ZymorError):
    exit_code = 1
    kind = "user_error"


class UsageError(ZymorError):
    exit_code = 2
    kind = "usage_error"


class NotFoundError(ZymorError):
    exit_code = 3
    kind = "not_found"


class ValidationError(ZymorError):
    exit_code = 4
    kind = "validation_error"

    def __init__(self, message: str, field: str | None = None, hint: str | None = None) -> None:
        super().__init__(message, hint)
        self.field = field

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        if self.field is not None:
            data["field"] = self.field
        return data


class LockedError(ZymorError):
    exit_code = 5
    kind = "locked"

    def __init__(
        self,
        message: str,
        holder: dict[str, Any] | None = None,
        hint: str | None = None,
    ) -> None:
        default_hint = "确认是否有其他 zymor 进程（如 MCP server）正在写；只读检索不受影响"
        super().__init__(message, hint or default_hint)
        self.holder = holder or {}

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        if self.holder:
            data["holder"] = self.holder
        return data


class BudgetExceeded(ZymorError):
    exit_code = 6
    kind = "budget_exceeded"


class IndexFailure(ZymorError):
    exit_code = 7
    kind = "index_error"


class ExternalError(ZymorError):
    exit_code = 1
    kind = "external_error"

    def __init__(self, message: str, retryable: bool = True, hint: str | None = None) -> None:
        super().__init__(message, hint)
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["retryable"] = self.retryable
        return data
