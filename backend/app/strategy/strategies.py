"""Deterministic candidate strategies for research and backtesting only."""

from collections.abc import Sequence
from decimal import Decimal

from app.domain.types import Direction
from app.strategy.models import Candle, SignalCandidate

_ONE_HUNDRED = Decimal("100")


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("mean requires at least one value")
    return sum(values, Decimal("0")) / Decimal(len(values))


class NoTradeBaseline:
    strategy_id = "no_trade_baseline_v1"

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        del candles, timeframe
        return None


class TrendPullbackStrategy:
    strategy_id = "trend_pullback_v1"

    def __init__(self, *, lookback: int = 3, validity_ms: int = 900_000) -> None:
        if lookback < 2 or validity_ms <= 0:
            raise ValueError("trend pullback configuration is invalid")
        self.lookback = lookback
        self.validity_ms = validity_ms

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


class VolatilityBreakoutStrategy:
    strategy_id = "volatility_breakout_v1"

    def __init__(self, *, lookback: int = 3, validity_ms: int = 900_000) -> None:
        if lookback < 2 or validity_ms <= 0:
            raise ValueError("breakout configuration is invalid")
        self.lookback = lookback
        self.validity_ms = validity_ms

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


class MeanReversionStrategy:
    strategy_id = "mean_reversion_v1"

    def __init__(
        self,
        *,
        lookback: int = 3,
        deviation_percent: Decimal = Decimal("2"),
        validity_ms: int = 900_000,
    ) -> None:
        if lookback < 2 or deviation_percent <= Decimal("0") or validity_ms <= 0:
            raise ValueError("mean reversion configuration is invalid")
        self.lookback = lookback
        self.deviation_percent = deviation_percent
        self.validity_ms = validity_ms

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
