from decimal import Decimal

import pytest

from app.domain.filters import SymbolFilters
from app.domain.types import Direction
from app.planning.exits import build_exit_plan
from app.planning.ladder import (
    LadderPlanningError,
    atr_ladder,
    equal_percent_ladder,
    order_book_liquidity_ladder,
    technical_level_ladder,
    time_sliced_ladder,
)
from app.planning.risk import CostAssumptions, risk_per_unit, solve_ladder


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
        entry_fee_rate=Decimal("0.0004"),
        exit_fee_rate=Decimal("0.0004"),
        slippage_bps=Decimal("1"),
        funding_buffer_rate=Decimal("0.0001"),
    )


def test_all_ladder_variants_are_explicit_and_order_book_is_disabled_by_default() -> None:
    weights = (Decimal("0.5"), Decimal("0.5"))
    equal = equal_percent_ladder(
        direction=Direction.LONG,
        reference_price=Decimal("1000"),
        stage_count=2,
        spacing_percent=Decimal("0.01"),
        weights=weights,
    )
    atr = atr_ladder(
        direction=Direction.SHORT,
        reference_price=Decimal("1000"),
        atr=Decimal("10"),
        stage_count=2,
        atr_multiple=Decimal("1"),
        weights=weights,
    )
    technical = technical_level_ladder(
        direction=Direction.LONG,
        levels=(Decimal("1000"), Decimal("995")),
        stop_price=Decimal("990"),
        weights=weights,
    )
    sliced = time_sliced_ladder(
        reference_price=Decimal("1000"),
        stage_count=2,
        start_at_ms=1_000,
        interval_ms=500,
        weights=weights,
    )

    assert equal[1].entry_price == Decimal("990.00")
    assert atr[1].entry_price == Decimal("1010")
    assert technical[0].entry_price == Decimal("1000")
    assert sliced[1].scheduled_at_ms == 1_500
    with pytest.raises(LadderPlanningError, match="disabled"):
        order_book_liquidity_ladder(
            direction=Direction.LONG,
            levels=((Decimal("1000"), Decimal("3")), (Decimal("995"), Decimal("5"))),
            stop_price=Decimal("990"),
            weights=weights,
        )
    enabled = order_book_liquidity_ladder(
        direction=Direction.LONG,
        levels=((Decimal("1000"), Decimal("3")), (Decimal("995"), Decimal("5"))),
        stop_price=Decimal("990"),
        weights=weights,
        enabled=True,
    )
    assert enabled[0].entry_price == Decimal("995")


def test_total_all_fill_loss_never_exceeds_budget(
    filters: SymbolFilters, costs: CostAssumptions
) -> None:
    blueprints = equal_percent_ladder(
        direction=Direction.LONG,
        reference_price=Decimal("1000"),
        stage_count=2,
        spacing_percent=Decimal("0.005"),
        weights=(Decimal("0.5"), Decimal("0.5")),
    )
    plan = solve_ladder(
        direction=Direction.LONG,
        blueprints=blueprints,
        stop_price=Decimal("990"),
        risk_budget=Decimal("0.10"),
        filters=filters,
        costs=costs,
        max_stages=2,
    )

    assert not plan.is_skipped
    assert plan.projected_total_loss <= plan.risk_budget
    assert len(plan.stages) == 2


def test_min_notional_failure_skips_instead_of_increasing_risk(costs: CostAssumptions) -> None:
    restrictive_filters = SymbolFilters(
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        min_price=Decimal("1"),
        max_price=Decimal("1000000"),
        step_size=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("1000"),
        min_notional=Decimal("100"),
    )
    blueprints = equal_percent_ladder(
        direction=Direction.LONG,
        reference_price=Decimal("1000"),
        stage_count=1,
        spacing_percent=Decimal("0"),
        weights=(Decimal("1"),),
    )

    plan = solve_ladder(
        direction=Direction.LONG,
        blueprints=blueprints,
        stop_price=Decimal("990"),
        risk_budget=Decimal("0.10"),
        filters=restrictive_filters,
        costs=costs,
        max_stages=2,
    )

    assert plan.is_skipped
    assert plan.skip_reason == "MIN_NOTIONAL_OR_FILTER_FAILURE"
    assert plan.stages == ()


def test_long_and_short_price_loss_are_symmetric_without_costs() -> None:
    zero_costs = CostAssumptions(
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        slippage_bps=Decimal("0"),
        funding_buffer_rate=Decimal("0"),
    )

    long_loss = risk_per_unit(
        direction=Direction.LONG,
        entry_price=Decimal("100"),
        stop_price=Decimal("90"),
        costs=zero_costs,
    )
    short_loss = risk_per_unit(
        direction=Direction.SHORT,
        entry_price=Decimal("100"),
        stop_price=Decimal("110"),
        costs=zero_costs,
    )

    assert long_loss == short_loss == Decimal("10")


def test_partial_fill_exit_quantities_never_exceed_confirmed_position(
    filters: SymbolFilters,
) -> None:
    exit_plan = build_exit_plan(
        direction=Direction.LONG,
        confirmed_position_quantity=Decimal("0.013"),
        stop_price=Decimal("990"),
        take_profit_prices=(Decimal("1010"), Decimal("1020")),
        take_profit_weights=(Decimal("0.5"), Decimal("0.5")),
        filters=filters,
    )

    total_take_profit_quantity = sum((leg.quantity for leg in exit_plan.take_profits), Decimal("0"))
    assert exit_plan.stop.close_position
    assert exit_plan.stop.quantity is None
    assert exit_plan.cancel_pending_entries
    assert total_take_profit_quantity == Decimal("0.013")
    assert total_take_profit_quantity <= exit_plan.confirmed_position_quantity
