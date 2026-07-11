"""Conservative daily AI budget accounting."""

from decimal import Decimal

from app.ai.models import AIUsage
from app.domain.decimal_math import ZERO


class AIBudgetExhausted(RuntimeError):
    code = "AI_DAILY_BUDGET_EXHAUSTED"


class AIUsageLedger:
    """A local ledger that can only consume, never raise, the configured budget."""

    def __init__(self, daily_budget_usd: Decimal) -> None:
        if daily_budget_usd < ZERO:
            raise ValueError("daily_budget_usd must not be negative")
        self._daily_budget_usd = daily_budget_usd
        self._records: list[AIUsage] = []
        self._hard_exhausted = False

    @property
    def total_estimated_cost_usd(self) -> Decimal:
        return sum((usage.estimated_cost_usd for usage in self._records), start=ZERO)

    @property
    def is_exhausted(self) -> bool:
        return self._hard_exhausted or self.total_estimated_cost_usd >= self._daily_budget_usd

    def can_start(self, maximum_request_cost_usd: Decimal) -> bool:
        if maximum_request_cost_usd < ZERO:
            raise ValueError("maximum_request_cost_usd must not be negative")
        return (
            not self._hard_exhausted
            and self.total_estimated_cost_usd + maximum_request_cost_usd <= self._daily_budget_usd
        )

    def record(self, usage: AIUsage) -> None:
        if self.total_estimated_cost_usd + usage.estimated_cost_usd > self._daily_budget_usd:
            self._hard_exhausted = True
            raise AIBudgetExhausted("AI usage would exceed the daily budget")
        self._records.append(usage)

    def exhaust(self) -> None:
        """A provider that violates its configured cost bound cannot be retried today."""
        self._hard_exhausted = True
