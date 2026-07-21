from collections.abc import Sequence
from decimal import Decimal

from app.domain.types import Direction
from app.strategy.backtest import BacktestCosts, BacktestEngine, WalkForwardRunner
from app.strategy.models import Candle, FrozenStrategy, SignalCandidate, StrategySpecification
from app.strategy.strategies import (
    MeanReversionStrategy,
    NoTradeBaseline,
    TrendPullbackStrategy,
    VolatilityBreakoutStrategy,
)
from tests.strategy_factory import make_frozen_strategy, make_strategy_lineage


def _candle(index: int, *, open_price: str, high: str, low: str, close: str) -> Candle:
    return Candle(
        symbol="BTCUSDT",
        open_time_ms=index * 60_000,
        close_time_ms=(index + 1) * 60_000 - 1,
        open_price=Decimal(open_price),
        high_price=Decimal(high),
        low_price=Decimal(low),
        close_price=Decimal(close),
        volume=Decimal("10"),
    )


def _signal(candle: Candle, *, direction: Direction = Direction.LONG) -> SignalCandidate:
    return SignalCandidate.from_lineage(
        make_strategy_lineage("test_strategy"),
        symbol=candle.symbol,
        direction=direction,
        reference_price=candle.close_price,
        invalidation_price=(candle.low_price if direction is Direction.LONG else candle.high_price),
        timeframe="1m",
        valid_until_ms=candle.close_time_ms + 60_000,
        reason_codes=("TEST",),
    )


def test_candidate_strategy_families_and_no_trade_baseline_are_deterministic() -> None:
    trend_candles = (
        _candle(0, open_price="100", high="101", low="99", close="100"),
        _candle(1, open_price="101", high="102", low="100", close="101"),
        _candle(2, open_price="100", high="104", low="100", close="103"),
    )
    mean_reversion_candles = (
        _candle(0, open_price="100", high="101", low="99", close="100"),
        _candle(1, open_price="100", high="101", low="99", close="100"),
        _candle(2, open_price="98", high="98", low="96", close="97"),
    )

    assert NoTradeBaseline().evaluate(trend_candles, timeframe="1m") is None
    assert TrendPullbackStrategy().evaluate(trend_candles, timeframe="1m") is not None
    assert VolatilityBreakoutStrategy().evaluate(trend_candles, timeframe="1m") is not None
    assert MeanReversionStrategy().evaluate(mean_reversion_candles, timeframe="1m") is not None


class OneShotStrategy:
    strategy_id = "one_shot"

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        if len(candles) != 1:
            return None
        return _signal(candles[-1])


class RecordingStrategy:
    strategy_id = "recording"

    def __init__(self) -> None:
        self.observed_lengths: list[int] = []

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        del timeframe
        self.observed_lengths.append(len(candles))
        return None


class AlwaysSignalStrategy:
    strategy_id = "always_signal"

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        del timeframe
        return _signal(candles[-1])

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        del timeframe
        return make_frozen_strategy(candles, self.evaluate)


def test_backtest_uses_next_bar_and_accounts_for_costs_without_look_ahead() -> None:
    candles = (
        _candle(0, open_price="100", high="101", low="99", close="100"),
        _candle(1, open_price="100", high="111", low="99", close="110"),
        _candle(2, open_price="110", high="112", low="109", close="111"),
    )
    costs = BacktestCosts(
        entry_fee_rate=Decimal("0.001"),
        exit_fee_rate=Decimal("0.001"),
        slippage_bps=Decimal("10"),
    )
    result = BacktestEngine().run(
        OneShotStrategy(),
        candles,
        timeframe="1m",
        costs=costs,
        evaluation_time_ms=candles[-1].close_time_ms,
    )
    recording = RecordingStrategy()
    BacktestEngine().run(
        recording,
        candles,
        timeframe="1m",
        costs=costs,
        evaluation_time_ms=candles[-1].close_time_ms,
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.signal_bar_index == 0
    assert trade.entry_price > candles[1].open_price
    assert trade.exit_price < candles[1].close_price
    assert trade.net_pnl < trade.gross_pnl
    assert trade.entry_fee + trade.exit_fee + trade.slippage_cost > Decimal("0")
    assert recording.observed_lengths == [1, 2]


def test_walk_forward_returns_only_out_of_sample_trades_deterministically() -> None:
    candles = tuple(
        _candle(
            index,
            open_price=str(100 + index),
            high=str(101 + index),
            low=str(99 + index),
            close=str(101 + index),
        )
        for index in range(10)
    )
    costs = BacktestCosts(
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        slippage_bps=Decimal("0"),
    )
    windows = WalkForwardRunner(train_size=4, test_size=3, step_size=3).run(
        StrategySpecification.volatility_breakout(lookback=2),
        candles,
        timeframe="1m",
        costs=costs,
    )

    assert len(windows) == 2
    assert all(trade.entry_bar_index >= 4 for window in windows for trade in window.result.trades)
