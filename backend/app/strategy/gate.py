"""D-027 candidate gate: no scan cadence can create an order obligation."""

from dataclasses import dataclass
from decimal import Decimal

from app.strategy.models import SignalCandidate


@dataclass(frozen=True, slots=True)
class CandidateGateContext:
    risk_allows: bool
    data_is_fresh: bool
    estimated_cost: Decimal
    max_estimated_cost: Decimal
    expected_net_value: Decimal
    evaluated_at_ms: int

    def __post_init__(self) -> None:
        if self.estimated_cost < Decimal("0") or self.max_estimated_cost < Decimal("0"):
            raise ValueError("cost values must not be negative")
        if self.evaluated_at_ms < 0:
            raise ValueError("evaluated_at_ms must not be negative")


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    accepted: bool
    candidate: SignalCandidate | None
    reason_codes: tuple[str, ...]


class CandidateGate:
    """Accepts only the conjunction of strategy, risk, data, cost, and validity."""

    def evaluate(
        self,
        signal: SignalCandidate | None,
        *,
        context: CandidateGateContext,
    ) -> CandidateDecision:
        reason_codes: list[str] = []
        if signal is None:
            reason_codes.append("NO_STRATEGY_SIGNAL")
        if not context.risk_allows:
            reason_codes.append("RISK_BLOCKED")
        if not context.data_is_fresh:
            reason_codes.append("STALE_DATA")
        if context.estimated_cost > context.max_estimated_cost:
            reason_codes.append("COST_LIMIT_EXCEEDED")
        if signal is not None and signal.valid_until_ms <= context.evaluated_at_ms:
            reason_codes.append("SIGNAL_EXPIRED")
        if context.expected_net_value <= Decimal("0"):
            reason_codes.append("NON_POSITIVE_EXPECTED_VALUE")

        if reason_codes:
            return CandidateDecision(
                accepted=False, candidate=None, reason_codes=tuple(reason_codes)
            )
        return CandidateDecision(accepted=True, candidate=signal, reason_codes=())
