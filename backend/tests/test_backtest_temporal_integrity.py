from collections.abc import Sequence
from decimal import Decimal

import pytest

from app.domain.types import Direction
from app.strategy.backtest import (
    BacktestCosts,
    BacktestDataError,
    BacktestEngine,
    BacktestSignalError,
)
from app.strategy.models import Candle, SignalCandidate
from tests.strategy_factory import make_strategy_lineage


def _candle(
    index: int,
    *,
    symbol: str = "BTCUSDT",
    timeframe: str = "1m",
    open_time_ms: int | None = None,
    close_time_ms: int | None = None,
) -> Candle:
    start = index * 60_000 if open_time_ms is None else open_time_ms
    end = start + 59_999 if close_time_ms is None else close_time_ms
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time_ms=start,
        close_time_ms=end,
        open_price=Decimal("100"),
        high_price=Decimal("102"),
        low_price=Decimal("99"),
        close_price=Decimal("101"),
        volume=Decimal("1"),
    )


def _costs() -> BacktestCosts:
    return BacktestCosts(
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        slippage_bps=Decimal("0"),
    )


def _signal(
    candle: Candle,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    valid_until_ms: int | None = None,
) -> SignalCandidate:
    return SignalCandidate.from_lineage(
        make_strategy_lineage("temporal"),
        symbol=candle.symbol if symbol is None else symbol,
        direction=Direction.LONG,
        reference_price=candle.close_price,
        invalidation_price=candle.low_price,
        timeframe=candle.timeframe if timeframe is None else timeframe,
        valid_until_ms=(
            candle.close_time_ms + 60_000 if valid_until_ms is None else valid_until_ms
        ),
        reason_codes=("TEST",),
    )


class FirstBarSignal:
    strategy_id = "first-bar"

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        del timeframe
        return _signal(candles[-1]) if len(candles) == 1 else None


class InvalidSignal:
    strategy_id = "invalid"

    def __init__(
        self,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
        stale: bool = False,
    ) -> None:
        self._symbol = symbol
        self._timeframe = timeframe
        self._stale = stale

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        last = candles[-1]
        return _signal(
            last,
            symbol=self._symbol,
            timeframe=self._timeframe,
            valid_until_ms=(last.close_time_ms if self._stale else None),
        )


class FutureDataSentinel:
    strategy_id = "future-sentinel"

    def __init__(self) -> None:
        self.observed_close_times: list[tuple[int, ...]] = []

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        del timeframe
        close_times = tuple(candle.close_time_ms for candle in candles)
        assert close_times == tuple(sorted(close_times))
        self.observed_close_times.append(close_times)
        return _signal(candles[-1]) if len(candles) == 1 else None


class EveryBarSignal:
    strategy_id = "every-bar"

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        del timeframe
        return _signal(candles[-1])


@pytest.mark.parametrize(
    "candles",
    (
        (_candle(1), _candle(0)),
        (_candle(0), _candle(0)),
        (_candle(0), _candle(1, open_time_ms=50_000)),
        (_candle(0), _candle(1, symbol="ETHUSDT")),
        (_candle(0), _candle(1, timeframe="5m")),
    ),
)
def test_invalid_series_order_overlap_symbol_or_timeframe_fails_closed(
    candles: tuple[Candle, ...],
) -> None:
    with pytest.raises(BacktestDataError):
        BacktestEngine().run(
            FirstBarSignal(),
            candles,
            timeframe="1m",
            costs=_costs(),
            evaluation_time_ms=200_000,
        )


def test_incomplete_bar_at_evaluation_time_fails_closed() -> None:
    candles = (_candle(0), _candle(1))

    with pytest.raises(BacktestDataError, match="closed"):
        BacktestEngine().run(
            FirstBarSignal(),
            candles,
            timeframe="1m",
            costs=_costs(),
            evaluation_time_ms=60_000,
        )


@pytest.mark.parametrize(
    "strategy",
    (
        InvalidSignal(symbol="ETHUSDT"),
        InvalidSignal(timeframe="5m"),
        InvalidSignal(stale=True),
    ),
)
def test_signal_must_match_symbol_timeframe_and_next_bar_validity(strategy: InvalidSignal) -> None:
    candles = (_candle(0), _candle(1))

    with pytest.raises(BacktestSignalError):
        BacktestEngine().run(
            strategy,
            candles,
            timeframe="1m",
            costs=_costs(),
            evaluation_time_ms=candles[-1].close_time_ms,
        )


def test_strategy_receives_only_closed_prefixes_and_never_future_candles() -> None:
    candles = (_candle(0), _candle(1), _candle(2))
    sentinel = FutureDataSentinel()

    result = BacktestEngine().run(
        sentinel,
        candles,
        timeframe="1m",
        costs=_costs(),
        evaluation_time_ms=candles[-1].close_time_ms,
    )

    assert len(result.trades) == 1
    assert sentinel.observed_close_times == [
        (candles[0].close_time_ms,),
        (candles[0].close_time_ms, candles[1].close_time_ms),
    ]


def test_open_simulated_position_blocks_overlapping_entries_until_its_exit_bar() -> None:
    candles = tuple(_candle(index) for index in range(6))

    result = BacktestEngine().run(
        EveryBarSignal(),
        candles,
        timeframe="1m",
        costs=_costs(),
        evaluation_time_ms=candles[-1].close_time_ms,
        holding_bars=2,
    )

    assert [(trade.entry_bar_index, trade.exit_bar_index) for trade in result.trades] == [
        (1, 2),
        (3, 4),
    ]
