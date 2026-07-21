from decimal import Decimal

from app.domain.types import Direction
from app.strategy.gate import CandidateGate, CandidateGateContext
from app.strategy.models import SignalCandidate
from tests.strategy_factory import make_strategy_lineage


def _signal() -> SignalCandidate:
    return SignalCandidate.from_lineage(
        make_strategy_lineage("fixture_strategy"),
        symbol="BTCUSDT",
        direction=Direction.LONG,
        reference_price=Decimal("100"),
        invalidation_price=Decimal("99"),
        timeframe="1m",
        valid_until_ms=10_000,
        reason_codes=("FIXTURE",),
    )


def _context(
    *,
    risk_allows: bool = True,
    data_is_fresh: bool = True,
    estimated_cost: Decimal = Decimal("0.01"),
    max_estimated_cost: Decimal = Decimal("0.02"),
    expected_net_value: Decimal = Decimal("0.03"),
    evaluated_at_ms: int = 1_000,
) -> CandidateGateContext:
    return CandidateGateContext(
        expected_lineage=make_strategy_lineage("fixture_strategy"),
        risk_allows=risk_allows,
        data_is_fresh=data_is_fresh,
        estimated_cost=estimated_cost,
        max_estimated_cost=max_estimated_cost,
        expected_net_value=expected_net_value,
        evaluated_at_ms=evaluated_at_ms,
    )


def test_candidate_gate_requires_all_d027_conditions() -> None:
    accepted = CandidateGate().evaluate(_signal(), context=_context())
    rejected = CandidateGate().evaluate(
        _signal(),
        context=_context(risk_allows=False, data_is_fresh=False, expected_net_value=Decimal("0")),
    )

    assert accepted.accepted
    assert accepted.candidate is not None
    assert not rejected.accepted
    assert rejected.candidate is None
    assert {"RISK_BLOCKED", "STALE_DATA", "NON_POSITIVE_EXPECTED_VALUE"} <= set(
        rejected.reason_codes
    )


def test_repeated_scans_without_signal_remain_zero_trade_behavior() -> None:
    gate = CandidateGate()
    decisions = tuple(gate.evaluate(None, context=_context()) for _ in range(100))

    assert all(not decision.accepted for decision in decisions)
    assert all(decision.candidate is None for decision in decisions)
    assert all("NO_STRATEGY_SIGNAL" in decision.reason_codes for decision in decisions)
