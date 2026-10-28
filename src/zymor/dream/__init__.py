"""Dream engine: NREM consolidation, dissonance arbitration, REM association."""

from zymor.dream.engine import build_plan, new_run_id, run_dream
from zymor.dream.types import (
    DREAM_PHASES,
    DreamBudget,
    DreamBudgetExceeded,
    DreamClients,
    DreamContext,
    DreamPlan,
    DreamReport,
    PhaseResult,
)

__all__ = [
    "DREAM_PHASES",
    "DreamBudget",
    "DreamBudgetExceeded",
    "DreamClients",
    "DreamContext",
    "DreamPlan",
    "DreamReport",
    "PhaseResult",
    "build_plan",
    "new_run_id",
    "run_dream",
]
