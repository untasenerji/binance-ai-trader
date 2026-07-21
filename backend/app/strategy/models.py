"""Strategy research contracts that are intentionally separate from execution."""

from __future__ import annotations

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
class StrategyLineage:
    """Immutable identity joining a specification, train slice, fit, and trainer."""

    strategy_id: str
    strategy_version: str
    strategy_specification_fingerprint: str
    fit_result_fingerprint: str
    train_dataset_fingerprint: str
    trainer_version: str

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.strategy_version or not self.trainer_version:
            raise ValueError("strategy lineage identity is incomplete")
        fingerprints = (
            self.strategy_specification_fingerprint,
            self.fit_result_fingerprint,
            self.train_dataset_fingerprint,
        )
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value.lower())
            for value in fingerprints
        ):
            raise ValueError("strategy lineage fingerprints must be SHA-256 hex")

    @classmethod
    def from_fit_result(cls, fit_result: StrategyFitResult) -> StrategyLineage:
        if type(fit_result) is not StrategyFitResult:
            raise TypeError("strategy lineage requires an exact fit result")
        fit_result.verify_fingerprint()
        return cls(
            strategy_id=fit_result.strategy_id,
            strategy_version=fit_result.strategy_version,
            strategy_specification_fingerprint=fit_result.specification.fingerprint,
            fit_result_fingerprint=fit_result.fingerprint,
            train_dataset_fingerprint=fit_result.training_data_fingerprint,
            trainer_version=fit_result.trainer_version,
        )


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


def candle_dataset_fingerprint(candles: Sequence[Candle]) -> str:
    series = tuple(candles)
    if not series:
        raise ValueError("strategy dataset fingerprint requires candles")
    canonical = json.dumps(
        [
            {
                "close_price": format(candle.close_price, "f"),
                "close_time_ms": candle.close_time_ms,
                "funding_rate": format(candle.funding_rate, "f"),
                "high_price": format(candle.high_price, "f"),
                "low_price": format(candle.low_price, "f"),
                "open_price": format(candle.open_price, "f"),
                "open_time_ms": candle.open_time_ms,
                "symbol": candle.symbol,
                "timeframe": candle.timeframe,
                "volume": format(candle.volume, "f"),
            }
            for candle in series
        ],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SignalCandidate:
    strategy_id: str
    strategy_version: str
    strategy_specification_fingerprint: str
    fit_result_fingerprint: str
    train_dataset_fingerprint: str
    trainer_version: str
    symbol: str
    direction: Direction
    reference_price: Decimal
    invalidation_price: Decimal
    timeframe: str
    valid_until_ms: int
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not self.strategy_id
            or not self.strategy_version
            or not self.symbol
            or not self.timeframe
        ):
            raise ValueError("strategy identity, symbol, and timeframe are required")
        StrategyLineage(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            strategy_specification_fingerprint=self.strategy_specification_fingerprint,
            fit_result_fingerprint=self.fit_result_fingerprint,
            train_dataset_fingerprint=self.train_dataset_fingerprint,
            trainer_version=self.trainer_version,
        )
        if self.reference_price <= ZERO or self.invalidation_price <= ZERO:
            raise ValueError("candidate prices must be positive")
        if self.direction is Direction.LONG and self.invalidation_price >= self.reference_price:
            raise ValueError("long invalidation must be below reference price")
        if self.direction is Direction.SHORT and self.invalidation_price <= self.reference_price:
            raise ValueError("short invalidation must be above reference price")
        if self.valid_until_ms <= 0 or not self.reason_codes:
            raise ValueError("candidate validity and reasons are required")

    @property
    def lineage(self) -> StrategyLineage:
        return StrategyLineage(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            strategy_specification_fingerprint=self.strategy_specification_fingerprint,
            fit_result_fingerprint=self.fit_result_fingerprint,
            train_dataset_fingerprint=self.train_dataset_fingerprint,
            trainer_version=self.trainer_version,
        )

    @classmethod
    def from_lineage(
        cls,
        lineage: StrategyLineage,
        *,
        symbol: str,
        direction: Direction,
        reference_price: Decimal,
        invalidation_price: Decimal,
        timeframe: str,
        valid_until_ms: int,
        reason_codes: tuple[str, ...],
    ) -> SignalCandidate:
        if type(lineage) is not StrategyLineage:
            raise TypeError("signal candidate requires exact strategy lineage")
        return cls(
            strategy_id=lineage.strategy_id,
            strategy_version=lineage.strategy_version,
            strategy_specification_fingerprint=(lineage.strategy_specification_fingerprint),
            fit_result_fingerprint=lineage.fit_result_fingerprint,
            train_dataset_fingerprint=lineage.train_dataset_fingerprint,
            trainer_version=lineage.trainer_version,
            symbol=symbol,
            direction=direction,
            reference_price=reference_price,
            invalidation_price=invalidation_price,
            timeframe=timeframe,
            valid_until_ms=valid_until_ms,
            reason_codes=reason_codes,
        )


FrozenStrategyEvaluator = Callable[..., SignalCandidate | None]


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


_STRATEGY_IDENTITIES: dict[StrategyKind, tuple[str, str]] = {
    StrategyKind.NO_TRADE_BASELINE: ("no_trade_baseline_v1", "v1"),
    StrategyKind.TREND_PULLBACK: ("trend_pullback_v1", "v1"),
    StrategyKind.VOLATILITY_BREAKOUT: ("volatility_breakout_v1", "v1"),
    StrategyKind.MEAN_REVERSION: ("mean_reversion_v1", "v1"),
}


def strategy_identity_for_specification(
    specification: StrategySpecification,
) -> tuple[str, str]:
    if type(specification) is not StrategySpecification:
        raise TypeError("strategy identity requires an exact specification")
    specification.verify_fingerprint()
    return _STRATEGY_IDENTITIES[specification.kind]


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
    def no_trade(cls) -> StrategySpecification:
        return cls(kind=StrategyKind.NO_TRADE_BASELINE)

    @classmethod
    def volatility_breakout(
        cls,
        *,
        lookback: int = 3,
        validity_ms: int = 900_000,
    ) -> StrategySpecification:
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
    ) -> StrategySpecification:
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
    ) -> StrategySpecification:
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
    strategy_id: str = ""
    strategy_version: str = ""
    trainer_version: str = "builtin-registry-v1"
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if type(self.specification) is not StrategySpecification:
            raise TypeError("fit result requires an exact strategy specification")
        expected_strategy_id, expected_strategy_version = strategy_identity_for_specification(
            self.specification
        )
        if self.strategy_id and self.strategy_id != expected_strategy_id:
            raise ValueError("fit result strategy ID does not match its specification")
        if self.strategy_version and self.strategy_version != expected_strategy_version:
            raise ValueError("fit result strategy version does not match its specification")
        object.__setattr__(self, "strategy_id", expected_strategy_id)
        object.__setattr__(self, "strategy_version", expected_strategy_version)
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
                "strategy_id": self.strategy_id,
                "strategy_version": self.strategy_version,
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
        expected_strategy_id, expected_strategy_version = strategy_identity_for_specification(
            self.specification
        )
        if (
            self.strategy_id != expected_strategy_id
            or self.strategy_version != expected_strategy_version
        ):
            raise ValueError("strategy fit identity is invalid")
        if self.fingerprint != self.expected_fingerprint():
            raise ValueError("strategy fit fingerprint is invalid")


@dataclass(frozen=True, slots=True)
class FrozenStrategy:
    """An evaluator cryptographically bound to one immutable train-only fit result."""

    fit_result: StrategyFitResult
    evaluator: FrozenStrategyEvaluator = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.fit_result) is not StrategyFitResult:
            raise TypeError("frozen strategy requires an exact fit result")
        self.fit_result.verify_fingerprint()
        if not callable(self.evaluator):
            raise TypeError("frozen strategy evaluator must be callable")

    @property
    def lineage(self) -> StrategyLineage:
        return StrategyLineage.from_fit_result(self.fit_result)

    @property
    def strategy_id(self) -> str:
        return self.fit_result.strategy_id

    @property
    def strategy_version(self) -> str:
        return self.fit_result.strategy_version

    @property
    def training_candle_count(self) -> int:
        return self.fit_result.training_candle_count

    @property
    def training_end_ms(self) -> int:
        return self.fit_result.training_end_ms

    @property
    def configuration_fingerprint(self) -> str:
        return self.fit_result.specification.fingerprint

    @property
    def strategy_specification_fingerprint(self) -> str:
        return self.fit_result.specification.fingerprint

    @property
    def fit_result_fingerprint(self) -> str:
        return self.fit_result.fingerprint

    @property
    def train_dataset_fingerprint(self) -> str:
        return self.fit_result.training_data_fingerprint

    @property
    def trainer_version(self) -> str:
        return self.fit_result.trainer_version

    def evaluate(self, candles: Sequence[Candle], *, timeframe: str) -> SignalCandidate | None:
        self.fit_result.verify_fingerprint()
        candidate = self.evaluator(tuple(candles), timeframe=timeframe)
        if candidate is None:
            return None
        if type(candidate) is not SignalCandidate:
            raise TypeError("frozen strategy evaluator returned an untyped candidate")
        if candidate.lineage != self.lineage:
            raise ValueError("frozen strategy candidate lineage does not match its fit result")
        return candidate
