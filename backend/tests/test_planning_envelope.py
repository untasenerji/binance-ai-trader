from dataclasses import replace
from decimal import Decimal

import pytest

from app.domain.filters import SymbolFilters
from app.domain.risk import RiskSettings
from app.domain.types import Direction
from app.planning.ladder import StageBlueprint
from app.planning.risk import (
    CostAssumptions,
    PlanningContext,
    RiskEnvelopeState,
    build_risk_envelope,
    solve_ladder,
)


@pytest.fixture
def filters() -> SymbolFilters:
    return SymbolFilters(
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        min_price=Decimal("1"),
        max_price=Decimal("1000000"),
        step_size=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("1000"),
        min_notional=Decimal("1"),
    )


@pytest.fixture
def costs() -> CostAssumptions:
    return CostAssumptions(
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        entry_slippage_bps=Decimal("0"),
        stop_slippage_bps=Decimal("0"),
        funding_buffer_rate=Decimal("0"),
        funding_interval_count=0,
    )


def _settings() -> RiskSettings:
    return RiskSettings(
        pilot_equity_cap_usdt=Decimal("20"),
        max_leverage=2,
        max_concurrent_positions=1,
        max_active_strategy_count=1,
        max_stages=2,
        risk_per_trade_usdt=Decimal("0.10"),
        daily_loss_limit_usdt=Decimal("0.30"),
        weekly_drawdown_limit_usdt=Decimal("0.80"),
        consecutive_loss_limit=3,
        openai_daily_budget_usd=Decimal("0.02"),
    )


def _context(**overrides: object) -> PlanningContext:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "requested_settings": _settings(),
        "verified_available_equity_usdt": Decimal("20"),
        "verified_effective_leverage": 2,
        "leverage_bracket_notional_cap_usdt": Decimal("40"),
        "required_reserve_usdt": Decimal("0"),
        "existing_symbol_exposure_usdt": Decimal("0"),
        "existing_total_exposure_usdt": Decimal("0"),
        "symbol_exposure_cap_usdt": Decimal("40"),
        "total_exposure_cap_usdt": Decimal("40"),
        "existing_open_position_count": 0,
        "active_strategy_count": 0,
        "remaining_daily_loss_usdt": Decimal("0.30"),
        "remaining_weekly_drawdown_usdt": Decimal("0.80"),
        "consecutive_loss_count": 0,
        "isolated_margin_verified": True,
        "one_way_mode_verified": True,
        "server_stop_capable": True,
    }
    values.update(overrides)
    return PlanningContext(**values)  # type: ignore[arg-type]


def _blueprint(entry_price: Decimal = Decimal("100")) -> tuple[StageBlueprint, ...]:
    return (StageBlueprint(index=1, entry_price=entry_price, weight=Decimal("1")),)


def test_verified_context_enforces_the_20_usdt_2x_and_010_risk_envelope() -> None:
    envelope = build_risk_envelope(_context())

    assert envelope.state is RiskEnvelopeState.READY
    assert envelope.effective_equity_usdt == Decimal("20")
    assert envelope.effective_leverage == 2
    assert envelope.risk_budget_usdt == Decimal("0.10")
    assert envelope.remaining_total_exposure_usdt == Decimal("40")


@pytest.mark.parametrize(
    ("overrides", "expected_state", "expected_reason"),
    (
        ({"verified_available_equity_usdt": None}, RiskEnvelopeState.SKIP, "MISSING_VERIFIED_DATA"),
        (
            {"required_reserve_usdt": Decimal("20")},
            RiskEnvelopeState.SKIP,
            "RESERVE_EXHAUSTS_EQUITY",
        ),
        ({"existing_open_position_count": 1}, RiskEnvelopeState.SKIP, "POSITION_LIMIT_REACHED"),
        ({"active_strategy_count": 1}, RiskEnvelopeState.SKIP, "ACTIVE_STRATEGY_LIMIT_REACHED"),
        (
            {"remaining_daily_loss_usdt": Decimal("0")},
            RiskEnvelopeState.SKIP,
            "DAILY_LOSS_LIMIT_REACHED",
        ),
        (
            {"remaining_weekly_drawdown_usdt": Decimal("0")},
            RiskEnvelopeState.SKIP,
            "WEEKLY_DRAWDOWN_LIMIT_REACHED",
        ),
        ({"consecutive_loss_count": 3}, RiskEnvelopeState.HALT, "CONSECUTIVE_LOSS_LIMIT_REACHED"),
    ),
)
def test_missing_or_exhausted_verified_limits_fail_closed(
    overrides: dict[str, object], expected_state: RiskEnvelopeState, expected_reason: str
) -> None:
    envelope = build_risk_envelope(_context(**overrides))

    assert envelope.state is expected_state
    assert envelope.reason == expected_reason


def test_narrow_stop_cannot_create_a_low_risk_but_excessive_notional_plan(
    filters: SymbolFilters, costs: CostAssumptions
) -> None:
    plan = solve_ladder(
        direction=Direction.LONG,
        blueprints=_blueprint(Decimal("1000")),
        stop_price=Decimal("999.9"),
        filters=filters,
        costs=costs,
        planning_context=_context(),
    )

    assert plan.is_skipped
    assert plan.skip_reason == "EXPOSURE_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    "overrides",
    (
        {"leverage_bracket_notional_cap_usdt": Decimal("0.5")},
        {"existing_symbol_exposure_usdt": Decimal("39.5")},
        {"existing_total_exposure_usdt": Decimal("39.5")},
    ),
)
def test_bracket_and_existing_exposure_are_proven_against_the_exact_plan(
    overrides: dict[str, object], filters: SymbolFilters, costs: CostAssumptions
) -> None:
    plan = solve_ladder(
        direction=Direction.LONG,
        blueprints=_blueprint(),
        stop_price=Decimal("90"),
        filters=filters,
        costs=costs,
        planning_context=_context(**overrides),
    )

    assert plan.is_skipped
    assert plan.skip_reason == "EXPOSURE_LIMIT_EXCEEDED"


def test_successful_plan_carries_exact_notional_and_required_margin_proof(
    filters: SymbolFilters, costs: CostAssumptions
) -> None:
    plan = solve_ladder(
        direction=Direction.LONG,
        blueprints=_blueprint(),
        stop_price=Decimal("90"),
        filters=filters,
        costs=costs,
        planning_context=_context(),
    )

    assert not plan.is_skipped
    assert plan.projected_total_loss <= plan.risk_budget
    assert plan.planned_notional_usdt == sum(
        (stage.quantity * stage.worst_entry_fill_price for stage in plan.stages), Decimal("0")
    )
    assert plan.required_margin_usdt <= plan.risk_envelope.effective_equity_usdt


def test_hard_cap_violation_cannot_be_hidden_inside_a_planning_context() -> None:
    settings = replace(_settings(), max_leverage=3)

    with pytest.raises(ValueError, match="HARD_RISK_LIMIT_EXCEEDED"):
        build_risk_envelope(_context(requested_settings=settings))
