from __future__ import annotations

import math
import secrets
from collections.abc import Callable
from pathlib import Path

from zymor.atlas import generate as generate_artifacts
from zymor.config import Config
from zymor.dream import state
from zymor.dream.state import DreamLock, Watermarks
from zymor.dream.types import (
    DreamBudget,
    DreamBudgetExceeded,
    DreamClients,
    DreamContext,
    DreamPlan,
    DreamReport,
    PhaseResult,
)
from zymor.errors import ZymorError
from zymor.store.index import Index
from zymor.util.time import now_iso
from zymor.vault import Vault

PhaseHandler = Callable[[DreamContext, DreamPlan, Watermarks], PhaseResult]


def new_run_id() -> str:
    """入参：无。出参：`dream_<UTC yyyymmdd_HHMMSS>_<4hex>` 运行标识。"""
    return f"dream_{state.utc_stamp()}_{secrets.token_hex(2)}"


def _phase_handlers() -> dict[str, PhaseHandler]:
    from zymor.dream.arbitration import run_arbitration
    from zymor.dream.nrem import run_nrem
    from zymor.dream.rem import run_rem

    return {"nrem": run_nrem, "arbitration": run_arbitration, "rem": run_rem}


def _resolve_global_vault(config: Config, project_vault: Vault) -> Vault | None:
    raw = config.get("vault.global_path", None)
    if not raw:
        return None
    root = Path(str(raw)).expanduser()
    if not (root / "vault.json").is_file():
        return None
    candidate = Vault.open(root)
    if candidate.root == project_vault.root:
        return None
    return candidate


def build_plan(ctx: DreamContext, watermarks: Watermarks) -> DreamPlan:
    """入参：做梦上下文与水位线。出参：本次 run 的可执行计划（不做任何写入）。"""
    vault = ctx.vault
    config = ctx.config
    limit_events = int(config.get("dream.max_events_per_run"))
    events = state.load_unprocessed_events(vault, watermarks, limit_events)
    events_by_session: dict[str, int] = {}
    session_max_seq: dict[str, int] = {}
    for event in events:
        events_by_session[event.session_id] = events_by_session.get(event.session_id, 0) + 1
        current = session_max_seq.get(event.session_id, 0)
        if event.seq > current:
            session_max_seq[event.session_id] = event.seq
    index = Index(vault.root)
    index.ensure()
    since = watermarks.last_success_at or "2000-01-01T00:00:00+00:00"
    recent = index.recent_rows(since, limit=int(config.get("dream.max_candidates")))
    hypothesis_nodes = sum(1 for row in recent if row["status"] == "hypothesis")
    counts = index.counts()
    disputed_nodes = int(counts["statuses"].get("disputed", 0))
    open_questions = 0
    try:
        from zymor.write_service import read_dissonance

        open_questions = sum(1 for item in read_dissonance(vault) if item.get("state") == "open")
    except Exception:
        open_questions = 0
    stale_focuses = state.find_stale_focuses(
        vault, idle_hours=int(config.get("lifecycle.focus_idle_close_after_hours"))
    )
    try:
        from zymor.dream.decay import stale_capsule_count

        stale_capsules = stale_capsule_count(ctx)
    except Exception:
        stale_capsules = 0
    batch_events = max(1, int(config.get("dream.batch_events")))
    batches = math.ceil(len(events) / batch_events) if events else 0
    arbitration_limit = min(disputed_nodes, int(config.get("dream.max_arbitrations")))
    estimated = batches + arbitration_limit + len(stale_focuses)
    steps: list[str] = []
    if events:
        steps.append(
            f"NREM: 处理 {len(events)} 条未消化 L0 事件（{len(events_by_session)} 个会话，"
            f"{batches} 批）"
        )
    if stale_focuses:
        steps.append(f"NREM: 为 {len(stale_focuses)} 个过期焦点补写 L2 胶囊")
    if disputed_nodes:
        steps.append(f"仲裁: 最多 {arbitration_limit} 对 disputed 节点")
    if open_questions:
        steps.append(f"仲裁: 队列中 {open_questions} 条开放追问")
    if stale_capsules:
        steps.append(f"元数据: {stale_capsules} 个闲置胶囊执行一次 arousal 降权")
    if not steps:
        steps.append("没有需要处理的内容")
    return DreamPlan(
        run_id=ctx.run_id,
        phases=[],
        event_count=len(events),
        events_by_session=events_by_session,
        session_max_seq=session_max_seq,
        hypothesis_nodes=hypothesis_nodes,
        disputed_nodes=disputed_nodes,
        open_questions=open_questions,
        stale_focuses=stale_focuses,
        estimated_llm_calls=estimated,
        steps=steps,
    )


def run_dream(
    vault: Vault,
    config: Config,
    *,
    phases: tuple[str, ...] = ("nrem", "arbitration"),
    dry_run: bool = False,
    budget_usd: float | None = None,
    clients: DreamClients | None = None,
) -> DreamReport:
    """入参：vault、配置、阶段、dry-run 与预算。出参：本次 run 报告。可重复、幂等、受预算约束。"""
    run_id = new_run_id()
    budget = DreamBudget.from_config(config, budget_usd=budget_usd)
    dream_clients = clients or DreamClients.from_config(vault, config, run_id=run_id)
    ctx = DreamContext(
        vault=vault,
        config=config,
        run_id=run_id,
        budget=budget,
        clients=dream_clients,
        global_vault=_resolve_global_vault(config, vault),
        dry_run=dry_run,
    )
    watermarks = Watermarks.load(vault)
    plan = build_plan(ctx, watermarks)
    plan.phases = list(phases)
    started = now_iso()
    if dry_run:
        return DreamReport(
            run_id=run_id,
            plan=plan,
            dry_run=True,
            budget=budget.to_dict(),
            started_at=started,
            finished_at=now_iso(),
            warnings=ctx.warnings,
        )
    lock = DreamLock.acquire(vault)
    try:
        state.write_run_record(
            vault,
            run_id,
            {
                "run_id": run_id,
                "status": "running",
                "started_at": started,
                "phases": list(phases),
                "plan": plan.to_dict(),
                "budget": budget.to_dict(),
            },
        )
        state.append_dream_log(
            vault,
            {
                "run_id": run_id,
                "ts": started,
                "phase": "run",
                "action": "start",
                "details": {"phases": list(phases), "events": plan.event_count},
                "tokens": 0,
                "usd": 0.0,
            },
        )
        handlers = _phase_handlers()
        results: list[PhaseResult] = []
        for phase in phases:
            if budget.exhausted() is not None:
                budget.stopped_reason = budget.exhausted()
                break
            handler = handlers.get(phase)
            if handler is None:
                results.append(PhaseResult(phase=phase, warnings=[f"未知阶段 {phase}"]))
                continue
            try:
                result = handler(ctx, plan, watermarks)
            except DreamBudgetExceeded as error:
                budget.stopped_reason = str(error.message)
                ctx.warn(str(error.message))
                result = PhaseResult(phase=phase)
            except ZymorError as error:
                ctx.error(f"{phase} 阶段失败: {error.message}")
                result = PhaseResult(phase=phase, errors=[error.message])
            except Exception as error:
                ctx.error(f"{phase} 阶段异常: {type(error).__name__}: {error}")
                result = PhaseResult(phase=phase, errors=[str(error)])
            results.append(result)
            ctx.warnings.extend(result.warnings)
            ctx.errors.extend(result.errors)
        try:
            from zymor.dream.decay import decay_stale_capsules

            decay = decay_stale_capsules(ctx)
            if decay["decayed"]:
                plan.steps.append(f"胶囊降权 {len(decay['decayed'])} 个")
        except ZymorError as error:
            ctx.warn(f"胶囊降权失败: {error.message}")
        nrem_result = next((item for item in results if item.phase == "nrem"), None)
        nrem_ok = nrem_result is not None and not nrem_result.errors
        if nrem_ok and nrem_result is not None:
            processed = nrem_result.details.get("processed_session_seq")
            advance: dict[str, int] = plan.session_max_seq if processed is None else processed
            for session_id, seq in advance.items():
                watermarks.advance(str(session_id), int(seq))
            nothing_pending = (
                not advance
                and plan.event_count == 0
                and not plan.stale_focuses
                and plan.hypothesis_nodes == 0
                and plan.disputed_nodes == 0
            )
            if advance or nothing_pending:
                watermarks.mark_success()
        watermarks.save(vault)
        try:
            plan.steps.append("刷新 ATLAS 与 catalog")
            generate_artifacts(vault)
        except (ZymorError, OSError) as error:
            ctx.warn(f"生成物刷新失败: {error}")
        if nrem_ok:
            removed = state.cleanup_l0(vault, int(config.get("dream.l0_retention_days")))
            if removed:
                state.append_dream_log(
                    vault,
                    {
                        "run_id": run_id,
                        "ts": now_iso(),
                        "phase": "nrem",
                        "action": "l0_cleanup",
                        "details": {"removed": removed},
                        "tokens": 0,
                        "usd": 0.0,
                    },
                )
        finished = now_iso()
        report = DreamReport(
            run_id=run_id,
            plan=plan,
            dry_run=False,
            phases=results,
            budget=budget.to_dict(),
            started_at=started,
            finished_at=finished,
            warnings=ctx.warnings,
            errors=ctx.errors,
        )
        state.write_run_record(
            vault,
            run_id,
            {
                "run_id": run_id,
                "status": "completed" if report.ok else "failed",
                "started_at": started,
                "finished_at": finished,
                "phases": list(phases),
                "plan": plan.to_dict(),
                "report": report.to_dict(),
                "budget": budget.to_dict(),
            },
        )
        state.append_dream_log(
            vault,
            {
                "run_id": run_id,
                "ts": finished,
                "phase": "run",
                "action": "finish",
                "details": {"ok": report.ok, "errors": report.errors},
                "tokens": budget.tokens_in + budget.tokens_out,
                "usd": round(budget.usd, 6),
            },
        )
        return report
    finally:
        lock.release()
