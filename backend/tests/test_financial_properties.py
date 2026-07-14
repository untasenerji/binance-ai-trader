"""Property coverage for multi-stage SHORT planning and exact fill accounting."""

from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from app.domain.filters import SymbolFilters
from app.domain.risk import RiskSettings
from app.domain.types import Direction
from app.planning.fills import FillEvent, FillLedger, evaluate_confirmed_position_risk
from app.planning.ladder import StageBlueprint
from app.planning.risk import CostAssumptions, PlanningContext, solve_ladder


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


def _short_context() -> PlanningContext:
    return PlanningContext(
        symbol="BTCUSDT",
        requested_settings=_settings(),
        verified_available_equity_usdt=Decimal("20"),
        verified_effective_leverage=2,
        leverage_bracket_notional_cap_usdt=Decimal("40"),
        required_reserve_usdt=Decimal("0"),
        existing_symbol_exposure_usdt=Decimal("0"),
        existing_total_exposure_usdt=Decimal("0"),
        symbol_exposure_cap_usdt=Decimal("40"),
        total_exposure_cap_usdt=Decimal("40"),
        existing_open_position_count=0,
        active_strategy_count=0,
        remaining_daily_loss_usdt=Decimal("0.30"),
        remaining_weekly_drawdown_usdt=Decimal("0.80"),
        consecutive_loss_count=0,
        isolated_margin_verified=True,
        one_way_mode_verified=True,
        server_stop_capable=True,
    )


def _non_power_of_ten_filters() -> SymbolFilters:
    return SymbolFilters(
        symbol="BTCUSDT",
        tick_size=Decimal("0.05"),
        min_price=Decimal("1"),
        max_price=Decimal("1_000_000"),
        step_size=Decimal("0.003"),
        min_quantity=Decimal("0.003"),
        max_quantity=Decimal("1000"),
        min_notional=Decimal("0.001"),
    )


def _costs() -> CostAssumptions:
    return CostAssumptions(
        entry_fee_rate=Decimal("0.0004"),
        exit_fee_rate=Decimal("0.0004"),
        entry_slippage_bps=Decimal("20"),
        stop_slippage_bps=Decimal("20"),
        funding_buffer_rate=Decimal("0.0001"),
        funding_interval_count=2,
    )


@given(
    entry_ticks=st.integers(min_value=400, max_value=2_000),
    spacing_ticks=st.integers(min_value=1, max_value=20),
)
def test_short_multistage_notional_and_margin_never_use_lower_adverse_fill(
    entry_ticks: int,
    spacing_ticks: int,
) -> None:
    entry_price = Decimal(entry_ticks) * Decimal("0.05")
    lower_stage_price = entry_price - Decimal(spacing_ticks) * Decimal("0.05")
    plan = solve_ladder(
        direction=Direction.SHORT,
        blueprints=(
            StageBlueprint(index=1, entry_price=entry_price, weight=Decimal("0.5")),
            StageBlueprint(index=2, entry_price=lower_stage_price, weight=Decimal("0.5")),
        ),
        stop_price=entry_price + Decimal("5"),
        filters=_non_power_of_ten_filters(),
        costs=_costs(),
        planning_context=_short_context(),
    )

    assert not plan.is_skipped
    exact_commitment = sum(
        (
            stage.quantity * max(stage.entry_price, stage.worst_entry_fill_price)
            for stage in plan.stages
        ),
        Decimal("0"),
    )
    assert plan.planned_notional_usdt == exact_commitment
    assert plan.required_margin_usdt == exact_commitment / Decimal("2")
    assert plan.planned_notional_usdt <= Decimal("40")
    assert all(stage.quantity % Decimal("0.003") == Decimal("0") for stage in plan.stages)


@given(
    first_millis=st.integers(min_value=3, max_value=80),
    second_millis=st.integers(min_value=3, max_value=80),
    first_price_cents=st.integers(min_value=5_000, max_value=20_000),
    second_price_cents=st.integers(min_value=5_000, max_value=20_000),
)
def test_short_multistage_vwap_fees_and_actual_stop_risk_match_decimal_oracle(
    first_millis: int,
    second_millis: int,
    first_price_cents: int,
    second_price_cents: int,
) -> None:
    first_quantity = Decimal(first_millis) / Decimal("1000")
    second_quantity = Decimal(second_millis) / Decimal("1000")
    first_price = Decimal(first_price_cents) / Decimal("100")
    second_price = Decimal(second_price_cents) / Decimal("100")
    first_fee = Decimal("0.001")
    second_fee = Decimal("0.002")
    ledger = FillLedger.from_events(
        (
            FillEvent(
                trade_id="short-stage-one",
                client_order_id="short-entry-1",
                last_quantity=first_quantity,
                cumulative_quantity=first_quantity,
                fill_price=first_price,
                fee=first_fee,
                fee_asset="USDT",
                occurred_at=datetime(2026, 7, 13, tzinfo=UTC),
            ),
            FillEvent(
                trade_id="short-stage-two",
                client_order_id="short-entry-2",
                last_quantity=second_quantity,
                cumulative_quantity=second_quantity,
                fill_price=second_price,
                fee=second_fee,
                fee_asset="USDT",
                occurred_at=datetime(2026, 7, 13, tzinfo=UTC),
            ),
        )
    )
    quantity = first_quantity + second_quantity
    expected_vwap = (first_quantity * first_price + second_quantity * second_price) / quantity
    stop_price = expected_vwap + Decimal("3")
    exit_fee_rate = Decimal("0.001")
    funding_rate = Decimal("0.0002")
    funding_count = 2
    expected_risk = (
        quantity * (stop_price - expected_vwap)
        + first_fee
        + second_fee
        + quantity * stop_price * exit_fee_rate
        + quantity * expected_vwap * funding_rate * funding_count
    )

    result = evaluate_confirmed_position_risk(
        direction=Direction.SHORT,
        signed_confirmed_position_quantity=-quantity,
        fills=ledger,
        worst_stop_exit_price=stop_price,
        exit_fee_rate=exit_fee_rate,
        funding_buffer_rate=funding_rate,
        funding_interval_count=funding_count,
        risk_budget=expected_risk,
        stop_confirmed=True,
    )

    assert ledger.average_fill_price == expected_vwap
    assert ledger.total_fee == first_fee + second_fee
    assert ledger.client_order_ids == frozenset({"short-entry-1", "short-entry-2"})
    assert result.actual_stop_risk == expected_risk
    assert not result.pending_entries_blocked
