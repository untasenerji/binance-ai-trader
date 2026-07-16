"""Strategy research contracts that are intentionally separate from execution."""

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
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


FrozenStrategyEvaluator = Callable[..., SignalCandidate | None]


@dataclass(frozen=True, slots=True)
class FrozenStrategy:
    """A train-derived, immutable strategy configuration allowed in test windows."""

    strategy_id: str
    training_candle_count: int
    training_end_ms: int
    configuration_fingerprint: str
    evaluator: FrozenStrategyEvaluator = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.configuration_fingerprint:
            raise ValueError("frozen strategy needs an identity and configuration fingerprint")
        if (
            not isinstance(self.training_candle_count, int)
            or isinstance(self.training_candle_count, bool)
            or self.training_candle_count < 1
            or not isinstance(self.training_end_ms, int)
            or isinstance(self.training_end_ms, bool)
            or self.training_end_ms < 0
        ):
            raise ValueError("frozen strategy training provenance is invalid")
        if not callable(self.evaluator):
            raise TypeError("frozen strategy evaluator must be callable")

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        return self.evaluator(tuple(candles), timeframe=timeframe)


class Strategy(Protocol):
    @property
    def strategy_id(self) -> str: ...

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None: ...


class TrainableStrategy(Protocol):
    @property
    def strategy_id(self) -> str: ...

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy: ...


class StrategyKind(StrEnum):
    NO_TRADE_BASELINE = "NO_TRADE_BASELINE"
    TREND_PULLBACK = "TREND_PULLBACK"
    VOLATILITY_BREAKOUT = "VOLATILITY_BREAKOUT"
    MEAN_REVERSION = "MEAN_REVERSION"


@dataclass(frozen=True, slots=True)
class StrategySpecification:
    """Serializable allowlisted configuration accepted by walk-forward research."""

    kind: StrategyKind
    lookback: int | None = None
    validity_ms: int | None = None
    deviation_percent: Decimal | None = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, StrategyKind):
            raise TypeError("strategy kind must be typed")
        if self.kind is StrategyKind.NO_TRADE_BASELINE:
            if any(
                value is not None
                for value in (self.lookback, self.validity_ms, self.deviation_percent)
            ):
                raise ValueError("no-trade baseline accepts no parameters")
        else:
            if (
                not isinstance(self.lookback, int)
                or isinstance(self.lookback, bool)
                or self.lookback < 2
                or not isinstance(self.validity_ms, int)
                or isinstance(self.validity_ms, bool)
                or self.validity_ms <= 0
            ):
                raise ValueError("strategy lookback and validity are invalid")
            if self.kind is StrategyKind.MEAN_REVERSION:
                if (
                    not isinstance(self.deviation_percent, Decimal)
                    or not self.deviation_percent.is_finite()
                    or self.deviation_percent <= ZERO
                ):
                    raise ValueError("mean-reversion deviation must be a positive Decimal")
            elif self.deviation_percent is not None:
                raise ValueError("deviation is allowlisted only for mean reversion")
        expected = self.expected_fingerprint()
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("strategy specification fingerprint is invalid")
        object.__setattr__(self, "fingerprint", expected)

    @classmethod
    def no_trade(cls) -> "StrategySpecification":
        return cls(kind=StrategyKind.NO_TRADE_BASELINE)

    @classmethod
    def volatility_breakout(
        cls,
        *,
        lookback: int = 3,
        validity_ms: int = 900_000,
    ) -> "StrategySpecification":
        return cls(
            kind=StrategyKind.VOLATILITY_BREAKOUT,
            lookback=lookback,
            validity_ms=validity_ms,
        )

    @classmethod
    def trend_pullback(
        cls,
        *,
        lookback: int = 3,
        validity_ms: int = 900_000,
    ) -> "StrategySpecification":
        return cls(
            kind=StrategyKind.TREND_PULLBACK,
            lookback=lookback,
            validity_ms=validity_ms,
        )

    @classmethod
    def mean_reversion(
        cls,
        *,
        lookback: int = 3,
        validity_ms: int = 900_000,
        deviation_percent: Decimal = Decimal("1"),
    ) -> "StrategySpecification":
        return cls(
            kind=StrategyKind.MEAN_REVERSION,
            lookback=lookback,
            validity_ms=validity_ms,
            deviation_percent=deviation_percent,
        )

    def canonical_record(self) -> dict[str, object]:
        return {
            "deviation_percent": (
                None if self.deviation_percent is None else format(self.deviation_percent, "f")
            ),
            "kind": self.kind.value,
            "lookback": self.lookback,
            "validity_ms": self.validity_ms,
        }

    def expected_fingerprint(self) -> str:
        canonical = json.dumps(
            self.canonical_record(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def verify_fingerprint(self) -> None:
        """Recheck a frozen object before each factory or execution boundary."""
        if self.fingerprint != self.expected_fingerprint():
            raise ValueError("strategy specification fingerprint is invalid")

    @property
    def serialized_specification(self) -> str:
        return json.dumps(
            self.canonical_record(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class StrategyFitResult:
    """Immutable train-only provenance; no callable or process state is serialized."""

    specification: StrategySpecification
    training_data_fingerprint: str
    training_candle_count: int
    training_end_ms: int
    timeframe: str
    trainer_version: str = "builtin-registry-v1"
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if type(self.specification) is not StrategySpecification:
            raise TypeError("fit result requires an exact strategy specification")
        if (
            len(self.training_data_fingerprint) != 64
            or not self.timeframe
            or self.trainer_version != "builtin-registry-v1"
        ):
            raise ValueError("fit result provenance is invalid")
        if (
            not isinstance(self.training_candle_count, int)
            or isinstance(self.training_candle_count, bool)
            or self.training_candle_count < 1
            or not isinstance(self.training_end_ms, int)
            or isinstance(self.training_end_ms, bool)
            or self.training_end_ms < 0
        ):
            raise ValueError("fit result training bounds are invalid")
        expected = self.expected_fingerprint()
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("strategy fit fingerprint is invalid")
        object.__setattr__(self, "fingerprint", expected)

    def expected_fingerprint(self) -> str:
        self.specification.verify_fingerprint()
        canonical = json.dumps(
            {
                "serialized_specification": self.specification.serialized_specification,
                "specification_fingerprint": self.specification.fingerprint,
                "timeframe": self.timeframe,
                "training_candle_count": self.training_candle_count,
                "training_data_fingerprint": self.training_data_fingerprint,
                "training_end_ms": self.training_end_ms,
                "trainer_version": self.trainer_version,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def verify_fingerprint(self) -> None:
        self.specification.verify_fingerprint()
        if self.fingerprint != self.expected_fingerprint():
            raise ValueError("strategy fit fingerprint is invalid")
