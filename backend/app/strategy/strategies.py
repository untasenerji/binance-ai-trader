"""Deterministic candidate strategies for research and backtesting only."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import ClassVar

from app.domain.types import Direction
from app.strategy.models import (
    Candle,
    FrozenStrategy,
    SignalCandidate,
    Strategy,
    StrategyFitResult,
    StrategyKind,
)

_ONE_HUNDRED = Decimal("100")


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("mean requires at least one value")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _freeze(
    strategy: Strategy,
    candles: Sequence[Candle],
    *,
    timeframe: str,
) -> FrozenStrategy:
    training_candles = tuple(candles)
    if not training_candles:
        raise ValueError("strategy fitting requires at least one training candle")
    if any(candle.timeframe != timeframe for candle in training_candles):
        raise ValueError("strategy fitting timeframe does not match its training candles")
    return FrozenStrategy(
        strategy_id=strategy.strategy_id,
        training_candle_count=len(training_candles),
        training_end_ms=training_candles[-1].close_time_ms,
        configuration_fingerprint=sha256(repr(strategy).encode()).hexdigest(),
        evaluator=strategy.evaluate,
    )


@dataclass(frozen=True, slots=True)
class NoTradeBaseline:
    strategy_id: ClassVar[str] = "no_trade_baseline_v1"

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        del candles, timeframe
        return None

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        return _freeze(self, candles, timeframe=timeframe)


@dataclass(frozen=True, slots=True)
class TrendPullbackStrategy:
    strategy_id: ClassVar[str] = "trend_pullback_v1"
    lookback: int = 3
    validity_ms: int = 900_000

    def __post_init__(self) -> None:
        if self.lookback < 2 or self.validity_ms <= 0:
            raise ValueError("trend pullback configuration is invalid")

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        if len(candles) < self.lookback:
            return None
        window = candles[-self.lookback :]
        last = window[-1]
        mean_close = _mean([candle.close_price for candle in window[:-1]])
        if last.close_price > mean_close and last.low_price <= mean_close:
            return SignalCandidate(
                strategy_id=self.strategy_id,
                symbol=last.symbol,
                direction=Direction.LONG,
                reference_price=last.close_price,
                invalidation_price=last.low_price,
                timeframe=timeframe,
                valid_until_ms=last.close_time_ms + self.validity_ms,
                reason_codes=("TREND_PULLBACK",),
            )
        return None

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        return _freeze(self, candles, timeframe=timeframe)


@dataclass(frozen=True, slots=True)
class VolatilityBreakoutStrategy:
    strategy_id: ClassVar[str] = "volatility_breakout_v1"
    lookback: int = 3
    validity_ms: int = 900_000

    def __post_init__(self) -> None:
        if self.lookback < 2 or self.validity_ms <= 0:
            raise ValueError("breakout configuration is invalid")

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        if len(candles) < self.lookback:
            return None
        previous = candles[-self.lookback : -1]
        last = candles[-1]
        previous_high = max(candle.high_price for candle in previous)
        previous_low = min(candle.low_price for candle in previous)
        if last.close_price > previous_high:
            return SignalCandidate(
                strategy_id=self.strategy_id,
                symbol=last.symbol,
                direction=Direction.LONG,
                reference_price=last.close_price,
                invalidation_price=previous_low,
                timeframe=timeframe,
                valid_until_ms=last.close_time_ms + self.validity_ms,
                reason_codes=("UPSIDE_BREAKOUT",),
            )
        if last.close_price < previous_low:
            return SignalCandidate(
                strategy_id=self.strategy_id,
                symbol=last.symbol,
                direction=Direction.SHORT,
                reference_price=last.close_price,
                invalidation_price=previous_high,
                timeframe=timeframe,
                valid_until_ms=last.close_time_ms + self.validity_ms,
                reason_codes=("DOWNSIDE_BREAKOUT",),
            )
        return None

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        return _freeze(self, candles, timeframe=timeframe)


@dataclass(frozen=True, slots=True)
class MeanReversionStrategy:
    strategy_id: ClassVar[str] = "mean_reversion_v1"
    lookback: int = 3
    deviation_percent: Decimal = Decimal("2")
    validity_ms: int = 900_000

    def __post_init__(self) -> None:
        if self.lookback < 2 or self.deviation_percent <= Decimal("0") or self.validity_ms <= 0:
            raise ValueError("mean reversion configuration is invalid")

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        if len(candles) < self.lookback:
            return None
        previous = candles[-self.lookback : -1]
        last = candles[-1]
        mean_close = _mean([candle.close_price for candle in previous])
        lower_band = mean_close * (_ONE_HUNDRED - self.deviation_percent) / _ONE_HUNDRED
        upper_band = mean_close * (_ONE_HUNDRED + self.deviation_percent) / _ONE_HUNDRED
        if last.close_price < lower_band:
            return SignalCandidate(
                strategy_id=self.strategy_id,
                symbol=last.symbol,
                direction=Direction.LONG,
                reference_price=last.close_price,
                invalidation_price=last.low_price,
                timeframe=timeframe,
                valid_until_ms=last.close_time_ms + self.validity_ms,
                reason_codes=("LOWER_BAND_DEVIATION",),
            )
        if last.close_price > upper_band:
            return SignalCandidate(
                strategy_id=self.strategy_id,
                symbol=last.symbol,
                direction=Direction.SHORT,
                reference_price=last.close_price,
                invalidation_price=last.high_price,
                timeframe=timeframe,
                valid_until_ms=last.close_time_ms + self.validity_ms,
                reason_codes=("UPPER_BAND_DEVIATION",),
            )
        return None

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        return _freeze(self, candles, timeframe=timeframe)


def frozen_strategy_from_fit(result: StrategyFitResult) -> FrozenStrategy:
    """Build one reviewed evaluator solely from immutable fit provenance."""
    if type(result) is not StrategyFitResult:
        raise TypeError("walk-forward execution requires an exact fit result")
    result.verify_fingerprint()
    specification = result.specification
    specification.verify_fingerprint()
    if specification.kind is StrategyKind.NO_TRADE_BASELINE:
        trainer: Strategy = NoTradeBaseline()
    elif specification.kind is StrategyKind.TREND_PULLBACK:
        assert specification.lookback is not None
        assert specification.validity_ms is not None
        trainer = TrendPullbackStrategy(
            lookback=specification.lookback,
            validity_ms=specification.validity_ms,
        )
    elif specification.kind is StrategyKind.VOLATILITY_BREAKOUT:
        assert specification.lookback is not None
        assert specification.validity_ms is not None
        trainer = VolatilityBreakoutStrategy(
            lookback=specification.lookback,
            validity_ms=specification.validity_ms,
        )
    else:
        assert specification.kind is StrategyKind.MEAN_REVERSION
        assert specification.lookback is not None
        assert specification.validity_ms is not None
        assert specification.deviation_percent is not None
        trainer = MeanReversionStrategy(
            lookback=specification.lookback,
            validity_ms=specification.validity_ms,
            deviation_percent=specification.deviation_percent,
        )
    return FrozenStrategy(
        strategy_id=trainer.strategy_id,
        training_candle_count=result.training_candle_count,
        training_end_ms=result.training_end_ms,
        configuration_fingerprint=specification.fingerprint,
        evaluator=trainer.evaluate,
    )
