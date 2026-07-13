"""Strategy research contracts that are intentionally separate from execution."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.domain.decimal_math import ZERO
from app.domain.types import Direction


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    open_time_ms: int
    close_time_ms: int
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal
    funding_rate: Decimal = ZERO
    timeframe: str = "1m"

    def __post_init__(self) -> None:
        if not self.symbol or not self.timeframe or self.close_time_ms <= self.open_time_ms:
            raise ValueError("candle identifiers and timestamps are invalid")
        if min(self.open_price, self.high_price, self.low_price, self.close_price) <= ZERO:
            raise ValueError("candle prices must be positive")
        if self.high_price < max(self.open_price, self.close_price):
            raise ValueError("candle high price is invalid")
        if self.low_price > min(self.open_price, self.close_price):
            raise ValueError("candle low price is invalid")
        if self.volume < ZERO:
            raise ValueError("candle volume must not be negative")


@dataclass(frozen=True, slots=True)
class SignalCandidate:
    strategy_id: str
    symbol: str
    direction: Direction
    reference_price: Decimal
    invalidation_price: Decimal
    timeframe: str
    valid_until_ms: int
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.symbol or not self.timeframe:
            raise ValueError("strategy_id, symbol, and timeframe are required")
        if self.reference_price <= ZERO or self.invalidation_price <= ZERO:
            raise ValueError("candidate prices must be positive")
        if self.direction is Direction.LONG and self.invalidation_price >= self.reference_price:
            raise ValueError("long invalidation must be below reference price")
        if self.direction is Direction.SHORT and self.invalidation_price <= self.reference_price:
            raise ValueError("short invalidation must be above reference price")
        if self.valid_until_ms <= 0 or not self.reason_codes:
            raise ValueError("candidate validity and reasons are required")


class Strategy(Protocol):
    strategy_id: str

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None: ...
