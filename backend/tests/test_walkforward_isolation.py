from collections.abc import Sequence
from decimal import Decimal

import pytest

from app.domain.types import Direction
from app.strategy.backtest import BacktestCosts, WalkForwardRunner, WalkForwardTrainingError
from app.strategy.models import (
    Candle,
    FrozenStrategy,
    SignalCandidate,
    StrategySpecification,
    TrainableStrategy,
)
from tests.strategy_factory import make_frozen_strategy, make_strategy_lineage


def _candle(index: int, close_price: Decimal | None = None) -> Candle:
    close = Decimal("100") + Decimal(index) if close_price is None else close_price
    return Candle(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time_ms=index * 60_000,
        close_time_ms=(index + 1) * 60_000 - 1,
        open_price=close,
        high_price=close + Decimal("1"),
        low_price=close - Decimal("1"),
        close_price=close,
        volume=Decimal("1"),
    )


def _signal(candle: Candle) -> SignalCandidate:
    return SignalCandidate.from_lineage(
        make_strategy_lineage("stateful"),
        symbol=candle.symbol,
        direction=Direction.LONG,
        reference_price=candle.close_price,
        invalidation_price=candle.low_price,
        timeframe=candle.timeframe,
        valid_until_ms=candle.close_time_ms + 60_000,
        reason_codes=("STATEFUL",),
    )


def _costs() -> BacktestCosts:
    return BacktestCosts(
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        slippage_bps=Decimal("0"),
    )


class StatefulPoisonStrategy:
    strategy_id = "stateful"

    def __init__(self) -> None:
        self.has_signaled = False

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        del timeframe
        if len(candles) == 4 and not self.has_signaled:
            self.has_signaled = True
            return _signal(candles[-1])
        return None

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        return make_frozen_strategy(candles, self.evaluate)


class ExternalMutationStrategy:
    strategy_id = "external-mutation"

    def __init__(self, source: list[Candle], observed_closes: list[Decimal]) -> None:
        self._source = source
        self._observed_closes = observed_closes

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        del timeframe
        self._observed_closes.append(candles[-1].close_price)
        self._source[-1] = _candle(len(self._source) - 1, Decimal("999"))
        return None

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        del timeframe
        return make_frozen_strategy(candles, self.evaluate)


def test_walk_forward_uses_one_frozen_specification_and_global_entry_indexes() -> None:
    candles = tuple(_candle(index, Decimal("100") + Decimal(index * 2)) for index in range(10))

    windows = WalkForwardRunner(train_size=4, test_size=3, step_size=3).run(
        StrategySpecification.volatility_breakout(lookback=2),
        candles,
        timeframe="1m",
        costs=_costs(),
    )

    assert len(windows) == 2
    assert all(window.result.trades for window in windows)
    assert all(
        window.test_start <= trade.entry_bar_index < window.test_end
        for window in windows
        for trade in window.result.trades
    )


def test_walk_forward_rejects_a_strategy_that_retains_future_candle_data() -> None:
    source = [_candle(index) for index in range(10)]
    observed_closes: list[Decimal] = []

    def factory() -> TrainableStrategy:
        return ExternalMutationStrategy(source, observed_closes)

    with pytest.raises(WalkForwardTrainingError, match="held-out"):
        WalkForwardRunner(train_size=4, test_size=3, step_size=3).run(
            factory,  # type: ignore[arg-type]
            source,
            timeframe="1m",
            costs=_costs(),
        )

    assert observed_closes == []
    assert source[-1].close_price == Decimal("109")


def test_walk_forward_rejects_overlapping_test_windows() -> None:
    with pytest.raises(ValueError, match="overlap"):
        WalkForwardRunner(train_size=4, test_size=3, step_size=2)
