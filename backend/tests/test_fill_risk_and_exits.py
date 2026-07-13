from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.filters import SymbolFilters
from app.domain.types import Direction
from app.exchange.contracts import (
    AlgoOrderIntent,
    AlgoOrderType,
    NormalOrderIntent,
    NormalOrderRole,
    NormalOrderType,
    OrderSide,
)
from app.planning.exits import build_exit_plan
from app.planning.fills import (
    FillEvent,
    FillLedger,
    PositionQuantityMismatch,
    evaluate_confirmed_position_risk,
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


def _fill(
    *,
    trade_id: str,
    last_quantity: Decimal,
    cumulative_quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    occurred_at: datetime,
) -> FillEvent:
    return FillEvent(
        trade_id=trade_id,
        client_order_id="entry-1",
        last_quantity=last_quantity,
        cumulative_quantity=cumulative_quantity,
        fill_price=price,
        fee=fee,
        fee_asset="USDT",
        occurred_at=occurred_at,
    )


def test_fill_ledger_keeps_vwap_exact_for_out_of_order_duplicate_trade_ids() -> None:
    ledger = FillLedger()
    now = datetime(2026, 7, 12, tzinfo=UTC)
    first = _fill(
        trade_id="trade-1",
        last_quantity=Decimal("0.002"),
        cumulative_quantity=Decimal("0.002"),
        price=Decimal("100"),
        fee=Decimal("0.001"),
        occurred_at=now,
    )
    delayed = _fill(
        trade_id="trade-2",
        last_quantity=Decimal("0.003"),
        cumulative_quantity=Decimal("0.005"),
        price=Decimal("102"),
        fee=Decimal("0.002"),
        occurred_at=now - timedelta(seconds=1),
    )

    assert not ledger.record(first).is_duplicate
    assert not ledger.record(delayed).is_duplicate
    duplicate = ledger.record(first)

    assert duplicate.is_duplicate
    assert ledger.filled_quantity == Decimal("0.005")
    assert ledger.average_fill_price == Decimal("101.2")
    assert ledger.total_fee == Decimal("0.003")
    assert ledger.observed_cumulative_quantity == Decimal("0.005")


def test_confirmed_position_risk_is_authoritative_and_blocks_bad_states() -> None:
    ledger = FillLedger()
    now = datetime(2026, 7, 12, tzinfo=UTC)
    ledger.record(
        _fill(
            trade_id="trade-1",
            last_quantity=Decimal("0.005"),
            cumulative_quantity=Decimal("0.005"),
            price=Decimal("100"),
            fee=Decimal("0.001"),
            occurred_at=now,
        )
    )

    protected = evaluate_confirmed_position_risk(
        direction=Direction.LONG,
        signed_confirmed_position_quantity=Decimal("0.005"),
        fills=ledger,
        worst_stop_exit_price=Decimal("95"),
        exit_fee_rate=Decimal("0.001"),
        funding_buffer_rate=Decimal("0.001"),
        funding_interval_count=1,
        risk_budget=Decimal("0.10"),
        stop_confirmed=True,
    )
    breach = evaluate_confirmed_position_risk(
        direction=Direction.LONG,
        signed_confirmed_position_quantity=Decimal("0.005"),
        fills=ledger,
        worst_stop_exit_price=Decimal("95"),
        exit_fee_rate=Decimal("0.001"),
        funding_buffer_rate=Decimal("0.001"),
        funding_interval_count=1,
        risk_budget=Decimal("0.01"),
        stop_confirmed=True,
    )
    rejected_stop = evaluate_confirmed_position_risk(
        direction=Direction.LONG,
        signed_confirmed_position_quantity=Decimal("0.005"),
        fills=ledger,
        worst_stop_exit_price=Decimal("95"),
        exit_fee_rate=Decimal("0.001"),
        funding_buffer_rate=Decimal("0.001"),
        funding_interval_count=1,
        risk_budget=Decimal("0.10"),
        stop_confirmed=False,
    )

    assert protected.average_entry_price == Decimal("100")
    assert not protected.pending_entries_blocked
    assert breach.pending_entries_blocked
    assert breach.reason == "ACTUAL_STOP_RISK_BREACH"
    assert rejected_stop.pending_entries_blocked
    assert rejected_stop.hard_halted
    assert rejected_stop.reason == "STOP_UNCONFIRMED"
    with pytest.raises(PositionQuantityMismatch):
        evaluate_confirmed_position_risk(
            direction=Direction.LONG,
            signed_confirmed_position_quantity=Decimal("0.006"),
            fills=ledger,
            worst_stop_exit_price=Decimal("95"),
            exit_fee_rate=Decimal("0.001"),
            funding_buffer_rate=Decimal("0.001"),
            funding_interval_count=1,
            risk_budget=Decimal("0.10"),
            stop_confirmed=True,
        )


def test_exit_plan_uses_inverse_reduce_only_take_profits_and_close_all_stop(
    filters: SymbolFilters,
) -> None:
    long_plan = build_exit_plan(
        direction=Direction.LONG,
        confirmed_position_quantity=Decimal("0.013"),
        stop_price=Decimal("99.9"),
        take_profit_prices=(Decimal("101"), Decimal("102")),
        take_profit_weights=(Decimal("0.5"), Decimal("0.5")),
        filters=filters,
    )
    short_plan = build_exit_plan(
        direction=Direction.SHORT,
        confirmed_position_quantity=Decimal("0.013"),
        stop_price=Decimal("100.1"),
        take_profit_prices=(Decimal("99"), Decimal("98")),
        take_profit_weights=(Decimal("0.5"), Decimal("0.5")),
        filters=filters,
    )

    assert long_plan.stop.algo_type is AlgoOrderType.STOP_MARKET
    assert long_plan.stop.close_position
    assert long_plan.stop.quantity is None
    assert long_plan.stop.side is OrderSide.SELL
    assert all(leg.reduce_only and leg.side is OrderSide.SELL for leg in long_plan.take_profits)
    assert all(leg.reduce_only and leg.side is OrderSide.BUY for leg in short_plan.take_profits)
    assert sum((leg.quantity for leg in long_plan.take_profits), Decimal("0")) <= Decimal("0.013")


def test_dust_take_profit_is_merged_or_omitted_without_exceeding_confirmed_position() -> None:
    dust_filters = SymbolFilters(
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        min_price=Decimal("1"),
        max_price=Decimal("1000000"),
        step_size=Decimal("0.001"),
        min_quantity=Decimal("0.010"),
        max_quantity=Decimal("1000"),
        min_notional=Decimal("1"),
    )
    merged = build_exit_plan(
        direction=Direction.LONG,
        confirmed_position_quantity=Decimal("0.013"),
        stop_price=Decimal("99.9"),
        take_profit_prices=(Decimal("101"), Decimal("102")),
        take_profit_weights=(Decimal("0.5"), Decimal("0.5")),
        filters=dust_filters,
    )
    omitted = build_exit_plan(
        direction=Direction.LONG,
        confirmed_position_quantity=Decimal("0.005"),
        stop_price=Decimal("99.9"),
        take_profit_prices=(Decimal("101"),),
        take_profit_weights=(Decimal("1"),),
        filters=dust_filters,
    )

    assert tuple(leg.quantity for leg in merged.take_profits) == (Decimal("0.013"),)
    assert omitted.take_profits == ()


def test_order_contract_rejects_invalid_reduce_only_combinations() -> None:
    with pytest.raises(ValueError):
        NormalOrderIntent(
            client_order_id="tp-invalid",
            symbol="BTCUSDT",
            direction=Direction.LONG,
            order_type=NormalOrderType.REDUCE_ONLY_LIMIT,
            role=NormalOrderRole.TAKE_PROFIT,
            quantity=Decimal("0.001"),
            price=Decimal("101"),
            reduce_only=False,
        )
    with pytest.raises(ValueError):
        NormalOrderIntent(
            client_order_id="entry-invalid",
            symbol="BTCUSDT",
            direction=Direction.LONG,
            order_type=NormalOrderType.LIMIT,
            role=NormalOrderRole.ENTRY,
            quantity=Decimal("0.001"),
            price=Decimal("100"),
            reduce_only=True,
        )
    with pytest.raises(ValueError):
        AlgoOrderIntent(
            client_algo_id="stop-invalid",
            symbol="BTCUSDT",
            direction=Direction.LONG,
            algo_type=AlgoOrderType.STOP_MARKET,
            trigger_price=Decimal("99"),
            close_position=False,
        )
