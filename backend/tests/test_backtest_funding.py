from collections.abc import Sequence
from decimal import Decimal

import pytest

from app.domain.types import Direction
from app.strategy.backtest import (
    BacktestCosts,
    BacktestDataError,
    BacktestEngine,
    FundingSettlement,
)
from app.strategy.models import Candle, SignalCandidate


def _candle(index: int, *, open_price: Decimal, close_price: Decimal) -> Candle:
    return Candle(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time_ms=index * 60_000,
        close_time_ms=(index + 1) * 60_000 - 1,
        open_price=open_price,
        high_price=max(open_price, close_price) + Decimal("1"),
        low_price=min(open_price, close_price) - Decimal("1"),
        close_price=close_price,
        volume=Decimal("1"),
    )


class FirstSignal:
    strategy_id = "funding-test"

    def __init__(self, direction: Direction) -> None:
        self._direction = direction

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        if len(candles) != 1:
            return None
        candle = candles[-1]
        invalidation = candle.low_price if self._direction is Direction.LONG else candle.high_price
        return SignalCandidate(
            strategy_id=self.strategy_id,
            symbol=candle.symbol,
            direction=self._direction,
            reference_price=candle.close_price,
            invalidation_price=invalidation,
            timeframe=timeframe,
            valid_until_ms=candle.close_time_ms + 60_000,
            reason_codes=("FUNDING",),
        )


def _costs() -> BacktestCosts:
    return BacktestCosts(
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        slippage_bps=Decimal("0"),
    )


def _candles() -> tuple[Candle, ...]:
    return (
        _candle(0, open_price=Decimal("100"), close_price=Decimal("100")),
        _candle(1, open_price=Decimal("100"), close_price=Decimal("105")),
        _candle(2, open_price=Decimal("105"), close_price=Decimal("110")),
    )


def test_no_settlement_has_zero_signed_funding_and_exact_accounting_identity() -> None:
    candles = _candles()
    trade = (
        BacktestEngine()
        .run(
            FirstSignal(Direction.LONG),
            candles,
            timeframe="1m",
            costs=_costs(),
            evaluation_time_ms=candles[-1].close_time_ms,
            holding_bars=2,
        )
        .trades[0]
    )

    assert trade.funding_pnl == Decimal("0")
    assert trade.net_pnl == (
        trade.market_gross_pnl
        - trade.slippage_cost
        - trade.entry_fee
        - trade.exit_fee
        + trade.funding_pnl
    )


@pytest.mark.parametrize(
    ("direction", "rate", "expected_funding"),
    (
        (Direction.LONG, Decimal("0.01"), Decimal("-2")),
        (Direction.LONG, Decimal("-0.01"), Decimal("2")),
        (Direction.SHORT, Decimal("0.01"), Decimal("2")),
        (Direction.SHORT, Decimal("-0.01"), Decimal("-2")),
    ),
)
def test_signed_funding_supports_long_short_and_positive_negative_rates(
    direction: Direction, rate: Decimal, expected_funding: Decimal
) -> None:
    candles = _candles()
    trade = (
        BacktestEngine()
        .run(
            FirstSignal(direction),
            candles,
            timeframe="1m",
            costs=_costs(),
            evaluation_time_ms=candles[-1].close_time_ms,
            holding_bars=2,
            quantity=Decimal("2"),
            funding_settlements=(
                FundingSettlement(
                    symbol="BTCUSDT",
                    timeframe="1m",
                    settled_at_ms=90_000,
                    rate=rate,
                    mark_price=Decimal("100"),
                ),
            ),
        )
        .trades[0]
    )

    assert trade.funding_pnl == expected_funding


def test_multiple_settlements_use_each_timestamped_mark_notional() -> None:
    candles = _candles()
    trade = (
        BacktestEngine()
        .run(
            FirstSignal(Direction.LONG),
            candles,
            timeframe="1m",
            costs=_costs(),
            evaluation_time_ms=candles[-1].close_time_ms,
            holding_bars=2,
            funding_settlements=(
                FundingSettlement(
                    symbol="BTCUSDT",
                    timeframe="1m",
                    settled_at_ms=90_000,
                    rate=Decimal("0.01"),
                    mark_price=Decimal("100"),
                ),
                FundingSettlement(
                    symbol="BTCUSDT",
                    timeframe="1m",
                    settled_at_ms=110_000,
                    rate=Decimal("-0.005"),
                    mark_price=Decimal("200"),
                ),
            ),
        )
        .trades[0]
    )

    assert trade.funding_pnl == Decimal("0")


def test_fees_use_executed_notional_and_slippage_is_not_double_counted() -> None:
    candles = _candles()
    costs = BacktestCosts(
        entry_fee_rate=Decimal("0.01"),
        exit_fee_rate=Decimal("0.02"),
        slippage_bps=Decimal("10"),
    )
    trade = (
        BacktestEngine()
        .run(
            FirstSignal(Direction.LONG),
            candles,
            timeframe="1m",
            costs=costs,
            evaluation_time_ms=candles[-1].close_time_ms,
            holding_bars=2,
        )
        .trades[0]
    )

    assert trade.entry_price == Decimal("100.100")
    assert trade.exit_price == Decimal("109.890")
    assert trade.entry_fee == trade.entry_price * costs.entry_fee_rate
    assert trade.exit_fee == trade.exit_price * costs.exit_fee_rate
    assert trade.execution_pnl == trade.market_gross_pnl - trade.slippage_cost
    assert trade.net_pnl == (
        trade.execution_pnl - trade.entry_fee - trade.exit_fee + trade.funding_pnl
    )


def test_funding_settlement_rejects_a_different_symbol_or_timeframe() -> None:
    candles = _candles()

    with pytest.raises(BacktestDataError, match="symbol"):
        BacktestEngine().run(
            FirstSignal(Direction.LONG),
            candles,
            timeframe="1m",
            costs=_costs(),
            evaluation_time_ms=candles[-1].close_time_ms,
            holding_bars=2,
            funding_settlements=(
                FundingSettlement(
                    symbol="ETHUSDT",
                    timeframe="1m",
                    settled_at_ms=90_000,
                    rate=Decimal("0.01"),
                    mark_price=Decimal("100"),
                ),
            ),
        )
