from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.decimal_math import ceil_to_increment
from app.domain.filters import SymbolFilters
from app.domain.risk import RiskSettings
from app.domain.types import Direction
from app.planning.exits import build_exit_plan
from app.planning.ladder import StageBlueprint
from app.planning.risk import (
    CostAssumptions,
    PlanningContext,
    risk_per_unit,
    solve_ladder,
    worst_case_price_projection,
)
from tests.strategy_factory import make_strategy_lineage


@pytest.fixture
def filters() -> SymbolFilters:
    return _filters()


def _filters() -> SymbolFilters:
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


def _planning_context() -> PlanningContext:
    return PlanningContext(
        symbol="BTCUSDT",
        requested_settings=RiskSettings(
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
        ),
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


@pytest.fixture
def costs() -> CostAssumptions:
    return CostAssumptions(
        entry_fee_rate=Decimal("0.0004"),
        exit_fee_rate=Decimal("0.0004"),
        entry_slippage_bps=Decimal("10"),
        stop_slippage_bps=Decimal("20"),
        funding_buffer_rate=Decimal("0.0001"),
        funding_interval_count=2,
    )


def test_directional_tick_rounding_uses_adverse_prices_for_long_and_short(
    filters: SymbolFilters, costs: CostAssumptions
) -> None:
    long_prices = worst_case_price_projection(
        direction=Direction.LONG,
        entry_target=Decimal("100.09"),
        stop_target=Decimal("99.91"),
        filters=filters,
        costs=costs,
    )
    short_prices = worst_case_price_projection(
        direction=Direction.SHORT,
        entry_target=Decimal("100.09"),
        stop_target=Decimal("100.11"),
        filters=filters,
        costs=costs,
    )

    assert long_prices.entry_price == Decimal("100.1")
    assert long_prices.stop_price == Decimal("99.9")
    assert long_prices.worst_entry_fill_price == Decimal("100.3")
    assert long_prices.worst_stop_exit_price == Decimal("99.7")
    assert short_prices.entry_price == Decimal("100.0")
    assert short_prices.stop_price == Decimal("100.2")
    assert short_prices.worst_entry_fill_price == Decimal("99.9")
    assert short_prices.worst_stop_exit_price == Decimal("100.5")


def test_ceil_is_stable_for_an_already_aligned_tick(filters: SymbolFilters) -> None:
    assert ceil_to_increment(Decimal("100.1"), Decimal("0.1"), field="price") == Decimal("100.1")
    assert filters.round_price_up(Decimal("100.1")) == Decimal("100.1")


def test_rounding_that_crosses_the_stop_fails_closed(
    filters: SymbolFilters, costs: CostAssumptions
) -> None:
    plan = solve_ladder(
        strategy_lineage=make_strategy_lineage("directional-risk"),
        direction=Direction.LONG,
        blueprints=(StageBlueprint(index=1, entry_price=Decimal("100.01"), weight=Decimal("1")),),
        stop_price=Decimal("100.11"),
        filters=filters,
        costs=costs,
        planning_context=_planning_context(),
    )

    assert plan.is_skipped
    assert plan.skip_reason == "INVALID_STOP_RELATION"


@pytest.mark.parametrize(
    ("direction", "stop_price", "take_profit_price", "expected_stop", "expected_tp"),
    (
        (
            Direction.LONG,
            Decimal("99.91"),
            Decimal("101.09"),
            Decimal("99.9"),
            Decimal("101.0"),
        ),
        (
            Direction.SHORT,
            Decimal("100.09"),
            Decimal("98.91"),
            Decimal("100.1"),
            Decimal("99.0"),
        ),
    ),
)
def test_exit_prices_apply_the_same_directional_rounding(
    direction: Direction,
    stop_price: Decimal,
    take_profit_price: Decimal,
    expected_stop: Decimal,
    expected_tp: Decimal,
    filters: SymbolFilters,
) -> None:
    exit_plan = build_exit_plan(
        direction=direction,
        confirmed_position_quantity=Decimal("0.020"),
        stop_price=stop_price,
        take_profit_prices=(take_profit_price,),
        take_profit_weights=(Decimal("1"),),
        filters=filters,
    )

    assert exit_plan.stop.trigger_price == expected_stop
    assert exit_plan.take_profits[0].price == expected_tp


@given(
    entry_cents=st.integers(min_value=10_000, max_value=100_000),
    gap_cents=st.integers(min_value=100, max_value=5_000),
    entry_slippage_bps=st.integers(min_value=0, max_value=100),
    stop_slippage_bps=st.integers(min_value=0, max_value=100),
)
def test_risk_per_unit_matches_an_independent_directional_decimal_oracle(
    entry_cents: int,
    gap_cents: int,
    entry_slippage_bps: int,
    stop_slippage_bps: int,
) -> None:
    filters = _filters()
    entry_target = Decimal(entry_cents) / Decimal("100")
    long_stop_target = entry_target - Decimal(gap_cents) / Decimal("100")
    short_stop_target = entry_target + Decimal(gap_cents) / Decimal("100")
    costs = CostAssumptions(
        entry_fee_rate=Decimal("0.001"),
        exit_fee_rate=Decimal("0.002"),
        entry_slippage_bps=Decimal(entry_slippage_bps),
        stop_slippage_bps=Decimal(stop_slippage_bps),
        funding_buffer_rate=Decimal("0.0003"),
        funding_interval_count=3,
    )

    for direction, stop_target in (
        (Direction.LONG, long_stop_target),
        (Direction.SHORT, short_stop_target),
    ):
        projected = worst_case_price_projection(
            direction=direction,
            entry_target=entry_target,
            stop_target=stop_target,
            filters=filters,
            costs=costs,
        )
        entry_rounding = ROUND_CEILING if direction is Direction.LONG else ROUND_FLOOR
        stop_rounding = ROUND_FLOOR if direction is Direction.LONG else ROUND_CEILING
        entry_price = entry_target.quantize(Decimal("0.1"), rounding=entry_rounding)
        stop_price = stop_target.quantize(Decimal("0.1"), rounding=stop_rounding)
        entry_slippage = Decimal(entry_slippage_bps) / Decimal("10000")
        stop_slippage = Decimal(stop_slippage_bps) / Decimal("10000")
        if direction is Direction.LONG:
            worst_entry = (entry_price * (Decimal("1") + entry_slippage)).quantize(
                Decimal("0.1"), rounding=ROUND_CEILING
            )
            worst_stop = (stop_price * (Decimal("1") - stop_slippage)).quantize(
                Decimal("0.1"), rounding=ROUND_FLOOR
            )
            price_loss = worst_entry - worst_stop
        else:
            worst_entry = (entry_price * (Decimal("1") - entry_slippage)).quantize(
                Decimal("0.1"), rounding=ROUND_FLOOR
            )
            worst_stop = (stop_price * (Decimal("1") + stop_slippage)).quantize(
                Decimal("0.1"), rounding=ROUND_CEILING
            )
            price_loss = worst_stop - worst_entry
        oracle = (
            price_loss
            + worst_entry * costs.entry_fee_rate
            + worst_stop * costs.exit_fee_rate
            + worst_entry * costs.funding_buffer_rate * costs.funding_interval_count
        )

        assert projected.entry_price == entry_price
        assert projected.stop_price == stop_price
        assert projected.worst_entry_fill_price == worst_entry
        assert projected.worst_stop_exit_price == worst_stop
        assert (
            risk_per_unit(
                direction=direction,
                entry_price=entry_target,
                stop_price=stop_target,
                filters=filters,
                costs=costs,
            )
            == oracle
        )
