"""Test-only strategy lineage builders; production accepts registry fit results only."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence

from app.strategy.models import Candle, FrozenStrategy, SignalCandidate, StrategyLineage


def make_strategy_lineage(strategy_id: str = "test_strategy") -> StrategyLineage:
    from app.strategy.models import StrategyFitResult, StrategySpecification

    return StrategyLineage.from_fit_result(
        StrategyFitResult(
            specification=StrategySpecification.volatility_breakout(lookback=2),
            training_data_fingerprint=hashlib.sha256(f"{strategy_id}:train".encode()).hexdigest(),
            training_candle_count=2,
            training_end_ms=119_999,
            timeframe="1m",
        )
    )


def make_frozen_strategy(
    candles: Sequence[Candle],
    evaluator: Callable[..., SignalCandidate | None],
) -> FrozenStrategy:
    del candles, evaluator
    raise TypeError("custom frozen strategy evaluators are not registry-allowed")
