"""Dream engine types: budget, clients, context, plan and reports."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zymor.config import Config
from zymor.errors import ZymorError
from zymor.llm import JsonChatClient, LlmClient, LlmError, LlmResponse, resolve_role
from zymor.util.time import now_iso
from zymor.vault import Vault

DREAM_PHASES = ("nrem", "arbitration", "rem")


class DreamBudgetExceeded(ZymorError):
    exit_code = 6
    kind = "dream_budget_exceeded"

    def __init__(self, reason: str) -> None:
        super().__init__(f"做梦预算超限：{reason}", hint="降低批大小或用 --budget-usd 调整后重试")


@dataclass
class DreamBudget:
    max_llm_calls: int = 100
    max_usd: float = 2.0
    max_nodes_changed: int = 200
    llm_calls: int = 0
    usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    nodes_changed: int = 0
    stopped_reason: str | None = None

    @classmethod
    def from_config(cls, config: Config, *, budget_usd: float | None = None) -> DreamBudget:
        """入参：配置与可选预算覆盖。出参：本次 run 的预算账本。"""
        max_usd = budget_usd if budget_usd is not None else config.get("dream.budget.max_usd")
        return cls(
            max_llm_calls=int(config.get("dream.budget.max_llm_calls")),
            max_usd=float(max_usd),
            max_nodes_changed=int(config.get("dream.budget.max_nodes_changed")),
        )

    def can_call_llm(self) -> bool:
        """入参：无。出参：当前预算是否还允许一次 LLM 调用。"""
        return self.llm_calls < self.max_llm_calls and self.usd < self.max_usd

    def record_llm(self, response: LlmResponse | None) -> None:
        """入参：LLM 响应或 None。出参：无。累计调用次数、token 与费用。"""
        self.llm_calls += 1
        if response is None:
            return
        self.usd += response.usd_estimate
        self.tokens_in += response.tokens_in
        self.tokens_out += response.tokens_out

    def can_write(self) -> bool:
        """入参：无。出参：当前预算是否还允许写一个节点。"""
        return self.nodes_changed < self.max_nodes_changed

    def record_write(self) -> None:
        """入参：无。出参：无。累计节点变更数。"""
        self.nodes_changed += 1

    def exhausted(self) -> str | None:
        """入参：无。出参：首个超限原因；未超限返回 None。"""
        if self.llm_calls >= self.max_llm_calls:
            return f"LLM 调用达到上限 {self.max_llm_calls}"
        if self.usd >= self.max_usd:
            return f"费用达到上限 {self.max_usd} USD"
        if self.nodes_changed >= self.max_nodes_changed:
            return f"节点变更达到上限 {self.max_nodes_changed}"
        return None

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：预算账本字典。"""
        return {
            "max_llm_calls": self.max_llm_calls,
            "max_usd": self.max_usd,
            "max_nodes_changed": self.max_nodes_changed,
            "llm_calls": self.llm_calls,
            "usd": round(self.usd, 6),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "nodes_changed": self.nodes_changed,
            "stopped_reason": self.stopped_reason,
        }


class _BudgetedClient:
    def __init__(self, client: JsonChatClient, budget: DreamBudget, role: str) -> None:
        self._client = client
        self._budget = budget
        self._role = role

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        caller: str = "dream",
        temperature: float | None = None,
    ) -> tuple[Any, LlmResponse]:
        if not self._budget.can_call_llm():
            raise DreamBudgetExceeded(self._budget.exhausted() or "预算不足")
        parsed, response = self._client.complete_json(
            messages, caller=caller, temperature=temperature
        )
        self._budget.record_llm(response)
        return parsed, response


@dataclass
class DreamClients:
    clients: dict[str, JsonChatClient] = field(default_factory=dict)

    def client(self, role: str, budget: DreamBudget) -> JsonChatClient | None:
        """入参：角色与预算。出参：带预算包装的客户端；未配置时返回 None。"""
        client = self.clients.get(role)
        if client is None:
            return None
        return _BudgetedClient(client, budget, role)

    @classmethod
    def from_config(
        cls,
        vault: Vault,
        config: Config,
        *,
        run_id: str,
        overrides: dict[str, JsonChatClient] | None = None,
    ) -> DreamClients:
        """入参：vault、配置与 run id。出参：按角色构造的客户端集合；缺模型/密钥的角色跳过。"""
        clients: dict[str, JsonChatClient] = dict(overrides or {})
        audit_path = vault.audit_dir / "llm.jsonl"
        for role in ("extract", "dedupe", "dream", "arbitration"):
            if role in clients:
                continue
            try:
                resolve_role(config, role)
                clients[role] = LlmClient(
                    config,
                    role,
                    audit_path=audit_path,
                    caller=f"dream:{run_id}:{role}",
                )
            except LlmError:
                continue
        return cls(clients=clients)


@dataclass
class DreamContext:
    vault: Vault
    config: Config
    run_id: str
    budget: DreamBudget
    clients: DreamClients
    global_vault: Vault | None = None
    dry_run: bool = False
    started_at: str = field(default_factory=now_iso)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def client(self, role: str) -> JsonChatClient | None:
        """入参：LLM 角色。出参：带预算的客户端；未配置返回 None。"""
        return self.clients.client(role, self.budget)

    def warn(self, message: str) -> None:
        """入参：提示文本。出参：无。记录一条 warning。"""
        self.warnings.append(message)

    def error(self, message: str) -> None:
        """入参：错误文本。出参：无。记录一条 error。"""
        self.errors.append(message)


@dataclass
class PhaseResult:
    phase: str
    processed_events: int = 0
    candidates: int = 0
    nodes_created: int = 0
    nodes_updated: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：阶段结果字典。"""
        return {
            "phase": self.phase,
            "processed_events": self.processed_events,
            "candidates": self.candidates,
            "nodes_created": self.nodes_created,
            "nodes_updated": self.nodes_updated,
            "skipped": self.skipped,
            "warnings": self.warnings,
            "errors": self.errors,
            "details": self.details,
        }


@dataclass
class DreamPlan:
    run_id: str
    phases: list[str]
    event_count: int = 0
    events_by_session: dict[str, int] = field(default_factory=dict)
    session_max_seq: dict[str, int] = field(default_factory=dict)
    hypothesis_nodes: int = 0
    disputed_nodes: int = 0
    open_questions: int = 0
    stale_focuses: list[str] = field(default_factory=list)
    estimated_llm_calls: int = 0
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：计划字典（dry-run 的人类可读计划）。"""
        return {
            "run_id": self.run_id,
            "phases": self.phases,
            "event_count": self.event_count,
            "events_by_session": self.events_by_session,
            "session_max_seq": self.session_max_seq,
            "hypothesis_nodes": self.hypothesis_nodes,
            "disputed_nodes": self.disputed_nodes,
            "open_questions": self.open_questions,
            "stale_focuses": self.stale_focuses,
            "estimated_llm_calls": self.estimated_llm_calls,
            "steps": self.steps,
        }


@dataclass
class DreamReport:
    run_id: str
    plan: DreamPlan
    dry_run: bool
    phases: list[PhaseResult] = field(default_factory=list)
    budget: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """入参：无。出参：本次 run 是否无 error。"""
        return not self.errors and all(not phase.errors for phase in self.phases)

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：报告字典。"""
        return {
            "run_id": self.run_id,
            "dry_run": self.dry_run,
            "ok": self.ok,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "plan": self.plan.to_dict(),
            "phases": [phase.to_dict() for phase in self.phases],
            "budget": self.budget,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def json_dumps(data: Any) -> str:
    """入参：可序列化对象。出参：紧凑 JSON 文本。写 run 记录与梦日志用。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def ensure_parent(path: Path) -> Path:
    """入参：文件路径。出参：同一路径。创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
