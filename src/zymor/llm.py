from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from zymor import __version__
from zymor.config import Config
from zymor.errors import ExternalError
from zymor.util.time import now_iso

ROLE_TEMPERATURES = {
    "extract": 0.1,
    "dedupe": 0.0,
    "dream": 0.4,
    "arbitration": 0.0,
    "judge": 0.0,
    "answer": 0.0,
}

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LlmError(ExternalError):
    kind = "llm_error"


class TransportError(Exception):
    def __init__(
        self, message: str, *, retryable: bool = False, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class RoleSettings:
    role: str
    model: str
    base_url: str
    api_key_env: str
    timeout_s: float
    max_retries: int
    temperature: float


@dataclass
class LlmResponse:
    text: str
    role: str
    model: str
    tokens_in: int
    tokens_out: int
    usd_estimate: float
    latency_ms: float
    priced: bool

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：调用记录字典（审计与成本统计用）。"""
        return {
            "role": self.role,
            "model": self.model,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "usd_estimate": self.usd_estimate,
            "latency_ms": self.latency_ms,
            "priced": self.priced,
        }


class Transport(Protocol):
    def complete(
        self,
        *,
        base_url: str,
        api_key: str,
        payload: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]: ...


class OpenAITransport:
    def complete(
        self,
        *,
        base_url: str,
        api_key: str,
        payload: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        """入参：OpenAI 兼容端点参数。出参：原始响应字典。走官方 SDK 的 chat completions。"""
        try:
            from openai import OpenAI
        except ImportError as error:
            raise TransportError("未安装 openai 依赖", retryable=False) from error
        try:
            client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=0)
            response = client.chat.completions.create(**payload)
        except Exception as error:
            raise _classify_openai_error(error) from error
        raw: dict[str, Any] = response.model_dump()
        return raw


def _classify_openai_error(error: Exception) -> TransportError:
    status = getattr(error, "status_code", None)
    name = type(error).__name__
    if isinstance(status, int) and status in RETRYABLE_STATUS:
        return TransportError(str(error), retryable=True, status_code=status)
    if name in {"APITimeoutError", "APIConnectionError", "RateLimitError", "InternalServerError"}:
        return TransportError(str(error), retryable=True, status_code=status)
    return TransportError(str(error), retryable=False, status_code=status)


class JsonChatClient(Protocol):
    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        caller: str = "unknown",
        temperature: float | None = None,
    ) -> tuple[Any, LlmResponse]: ...


def resolve_role(config: Config, role: str) -> RoleSettings:
    """入参：配置与角色名。出参：该角色的解析后设置。角色级覆盖优先于全局设置。"""
    if role not in ROLE_TEMPERATURES:
        raise LlmError(f"未知 LLM 角色: {role}", retryable=False)
    role_section = config.get(f"llm.roles.{role}", {})
    if not isinstance(role_section, dict):
        role_section = {}
    model = role_section.get("model") or config.get(f"llm.models.{role}", None)
    if not model:
        raise LlmError(
            f"角色 {role} 未配置模型",
            retryable=False,
            hint=f"在配置中设置 [llm.models].{role} 或 [llm.roles.{role}].model",
        )
    base_url = str(role_section.get("base_url") or config.get("llm.base_url"))
    api_key_env = str(role_section.get("api_key_env") or config.get("llm.api_key_env"))
    timeout_s = float(role_section.get("timeout_s") or config.get("llm.timeout_s"))
    max_retries = int(role_section.get("max_retries") or config.get("llm.max_retries"))
    return RoleSettings(
        role=role,
        model=str(model),
        base_url=base_url,
        api_key_env=api_key_env,
        timeout_s=timeout_s,
        max_retries=max_retries,
        temperature=ROLE_TEMPERATURES[role],
    )


class LlmClient:
    def __init__(
        self,
        config: Config,
        role: str,
        *,
        transport: Transport | None = None,
        audit_path: Path | None = None,
        caller: str = "unknown",
        api_key: str | None = None,
    ) -> None:
        self.config = config
        self.settings = resolve_role(config, role)
        self.transport = transport or OpenAITransport()
        self.audit_path = audit_path
        self.caller = caller
        self._api_key = api_key

    def _key(self) -> str:
        if self._api_key is not None:
            return self._api_key
        key = os.environ.get(self.settings.api_key_env)
        if not key:
            raise LlmError(
                f"未配置 LLM API key：环境变量 {self.settings.api_key_env} 为空",
                retryable=False,
                hint=f"export {self.settings.api_key_env}=...",
            )
        return key

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        json_mode: bool = False,
        max_tokens: int | None = None,
        caller: str | None = None,
    ) -> LlmResponse:
        """入参：消息列表与采样参数。出参：模型响应。按 429/5xx/超时指数退避重试。"""
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature if temperature is None else temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        api_key = self._key()
        attempts = self.settings.max_retries + 1
        last_error: TransportError | None = None
        started = time.monotonic()
        for attempt in range(attempts):
            try:
                raw = self.transport.complete(
                    base_url=self.settings.base_url,
                    api_key=api_key,
                    payload=payload,
                    timeout_s=self.settings.timeout_s,
                )
                response = self._parse_response(raw, latency_ms=(time.monotonic() - started) * 1000)
                self._audit(response, caller or self.caller, ok=True)
                return response
            except TransportError as error:
                last_error = error
                self._audit_error(str(error), caller or self.caller)
                if not error.retryable or attempt == attempts - 1:
                    break
                time.sleep(min(2**attempt, 8))
        message = str(last_error) if last_error else "LLM 调用失败"
        raise LlmError(message, retryable=bool(last_error and last_error.retryable))

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        caller: str | None = None,
        temperature: float | None = None,
    ) -> tuple[Any, LlmResponse]:
        """入参：要求 JSON 输出的消息列表。出参：解析后的对象与响应。解析失败时做一次修复重试。"""
        response = self.complete(messages, temperature=temperature, json_mode=True, caller=caller)
        parsed = _parse_json_text(response.text)
        if parsed is not None:
            return parsed, response
        repair_messages: list[dict[str, str]] = list(messages) + [
            {"role": "assistant", "content": response.text},
            {
                "role": "user",
                "content": "上一次输出不是合法 JSON。只输出一个合法 JSON 对象，不要代码块或解释。",
            },
        ]
        repaired = self.complete(
            repair_messages,
            temperature=0.0 if temperature is None else temperature,
            json_mode=True,
            caller=caller,
        )
        parsed = _parse_json_text(repaired.text)
        if parsed is None:
            raise LlmError("LLM 两次输出均不是合法 JSON", retryable=False)
        return parsed, repaired

    def _parse_response(self, raw: dict[str, Any], latency_ms: float) -> LlmResponse:
        try:
            choice = raw["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as error:
            raise TransportError(f"响应结构不合法: {error}", retryable=False) from error
        usage = raw.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        usd, priced = _estimate_usd(self.config, self.settings.model, tokens_in, tokens_out)
        return LlmResponse(
            text=text,
            role=self.settings.role,
            model=str(raw.get("model") or self.settings.model),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            usd_estimate=usd,
            latency_ms=round(latency_ms, 3),
            priced=priced,
        )

    def _audit(self, response: LlmResponse, caller: str, *, ok: bool) -> None:
        self._write_audit(
            {
                "ts": now_iso(),
                "role": response.role,
                "model": response.model,
                "tokens_in": response.tokens_in,
                "tokens_out": response.tokens_out,
                "usd_estimate": response.usd_estimate,
                "caller": caller,
                "latency_ms": response.latency_ms,
                "ok": ok,
                "version": __version__,
            }
        )

    def _audit_error(self, message: str, caller: str) -> None:
        self._write_audit(
            {
                "ts": now_iso(),
                "role": self.settings.role,
                "model": self.settings.model,
                "caller": caller,
                "ok": False,
                "error": message,
                "version": __version__,
            }
        )

    def _write_audit(self, record: dict[str, Any]) -> None:
        if self.audit_path is None:
            return
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)


def _parse_json_text(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


def _estimate_usd(
    config: Config, model: str, tokens_in: int, tokens_out: int
) -> tuple[float, bool]:
    pricing = config.get("llm.pricing", {})
    if not isinstance(pricing, dict):
        return 0.0, False
    entry = pricing.get(model)
    if not isinstance(entry, dict):
        return 0.0, False
    try:
        rate_in = float(entry.get("input_per_1m", 0.0))
        rate_out = float(entry.get("output_per_1m", 0.0))
    except (TypeError, ValueError):
        return 0.0, False
    usd = (tokens_in * rate_in + tokens_out * rate_out) / 1_000_000
    return round(usd, 8), True
