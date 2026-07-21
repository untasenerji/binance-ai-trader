"""Test-only strategy lineage builders; production accepts registry fit results only."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence

from app.strategy.models import (
    Candle,
    FrozenStrategy,
    SignalCandidate,
    StrategyFitResult,
    StrategyLineage,
    StrategySpecification,
    candle_dataset_fingerprint,
)


def make_strategy_lineage(strategy_id: str = "test_strategy") -> StrategyLineage:
    def fingerprint(label: str) -> str:
        return hashlib.sha256(f"{strategy_id}:{label}".encode()).hexdigest()

    return StrategyLineage(
        strategy_id=strategy_id,
        strategy_version="test-v1",
        strategy_specification_fingerprint=fingerprint("specification"),
        fit_result_fingerprint=fingerprint("fit"),
        train_dataset_fingerprint=fingerprint("train"),
        trainer_version="test-registry-v1",
    )


def make_frozen_strategy(
    candles: Sequence[Candle],
    evaluator: Callable[..., SignalCandidate | None],
) -> FrozenStrategy:
    series = tuple(candles)
    result = StrategyFitResult(
        specification=StrategySpecification.no_trade(),
        training_data_fingerprint=candle_dataset_fingerprint(series),
        training_candle_count=len(series),
        training_end_ms=series[-1].close_time_ms,
        timeframe=series[-1].timeframe,
    )
    return FrozenStrategy(fit_result=result, evaluator=evaluator)
