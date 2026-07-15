"""Walk-forward fitting must be train-only and produce an immutable strategy snapshot."""

from collections.abc import Sequence
from decimal import Decimal

import pytest

from app.domain.types import Direction
from app.strategy.backtest import BacktestCosts, WalkForwardRunner, WalkForwardTrainingError
from app.strategy.models import Candle, FrozenStrategy, SignalCandidate
from app.strategy.strategies import VolatilityBreakoutStrategy


def _candle(index: int, close: Decimal) -> Candle:
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


def _costs() -> BacktestCosts:
    return BacktestCosts(
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        slippage_bps=Decimal("0"),
    )


class TrainOnlyThresholdStrategy:
    strategy_id = "train-only-threshold"

    def __init__(self, fitted_closes: list[tuple[Decimal, ...]]) -> None:
        self._fitted_closes = fitted_closes

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        closes = tuple(candle.close_price for candle in candles)
        self._fitted_closes.append(closes)
        threshold = max(closes)

        def evaluate(
            evaluation_candles: Sequence[Candle],
            *,
            timeframe: str,
        ) -> SignalCandidate | None:
            last = evaluation_candles[-1]
            if last.close_price <= threshold:
                return None
            return SignalCandidate(
                strategy_id=self.strategy_id,
                symbol=last.symbol,
                direction=Direction.LONG,
                reference_price=last.close_price,
                invalidation_price=last.low_price,
                timeframe=timeframe,
                valid_until_ms=last.close_time_ms + 60_000,
                reason_codes=("TRAIN_ONLY_THRESHOLD",),
            )

        return FrozenStrategy(
            strategy_id=self.strategy_id,
            training_candle_count=len(candles),
            training_end_ms=candles[-1].close_time_ms,
            configuration_fingerprint=f"threshold:{threshold}",
            evaluator=evaluate,
        )


class UnfrozenTrainer:
    strategy_id = "unfrozen"

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> "UnfrozenTrainer":
        del candles, timeframe
        return self


def test_walk_forward_fits_only_the_train_slice_then_uses_a_frozen_snapshot() -> None:
    candles = tuple(
        _candle(index, close)
        for index, close in enumerate(
            (
                Decimal("100"),
                Decimal("101"),
                Decimal("102"),
                Decimal("103"),
                Decimal("200"),
                Decimal("201"),
                Decimal("202"),
            )
        )
    )
    windows = WalkForwardRunner(train_size=4, test_size=3, step_size=3).run(
        lambda: VolatilityBreakoutStrategy(lookback=2),
        candles,
        timeframe="1m",
        costs=_costs(),
    )
    changed_held_out = candles[:4] + tuple(
        _candle(index, Decimal("500") + Decimal(index)) for index in range(4, 7)
    )
    changed_windows = WalkForwardRunner(train_size=4, test_size=3, step_size=3).run(
        lambda: VolatilityBreakoutStrategy(lookback=2),
        changed_held_out,
        timeframe="1m",
        costs=_costs(),
    )

    assert len(windows) == 1
    assert windows[0].training_data_fingerprint == changed_windows[0].training_data_fingerprint
    assert windows[0].configuration_fingerprint == changed_windows[0].configuration_fingerprint
    assert windows[0].train_end == 4
    assert windows[0].test_start == 4


def test_walk_forward_rejects_trainers_that_do_not_return_a_frozen_snapshot() -> None:
    candles = tuple(_candle(index, Decimal("100") + Decimal(index)) for index in range(7))

    with pytest.raises(WalkForwardTrainingError, match="custom|held-out|isolate"):
        WalkForwardRunner(train_size=4, test_size=3, step_size=3).run(
            UnfrozenTrainer,  # type: ignore[arg-type]
            candles,
            timeframe="1m",
            costs=_costs(),
        )
