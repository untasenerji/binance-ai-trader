"""Deterministic, close-only strategy research with explicit cost accounting."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from types import FunctionType, MethodType, ModuleType

from app.domain.decimal_math import ZERO
from app.domain.types import Direction
from app.strategy.models import (
    Candle,
    FrozenStrategy,
    SignalCandidate,
    Strategy,
    StrategyFitResult,
    StrategySpecification,
)

_BPS_DENOMINATOR = Decimal("10000")


class BacktestDataError(ValueError):
    """Raised when a candle series cannot support a temporal-safe backtest."""


class BacktestSignalError(ValueError):
    """Raised when a strategy candidate cannot legally enter on the next bar."""


class WalkForwardTrainingError(ValueError):
    """Raised when a walk-forward trainer cannot prove a frozen train-only snapshot."""


@dataclass(slots=True)
class _TrainingIsolationGuard:
    training_candles: tuple[Candle, ...]
    forbidden_scalar_tokens: frozenset[tuple[type[object], object]]
    reject_unproven_scalars: bool
    seen: set[int]

    @classmethod
    def for_window(
        cls,
        training_candles: tuple[Candle, ...],
        held_out_candles: tuple[Candle, ...],
        *,
        reject_unproven_scalars: bool = False,
    ) -> "_TrainingIsolationGuard":
        training_tokens = cls._market_scalar_tokens(training_candles)
        held_out_tokens = cls._market_scalar_tokens(held_out_candles)
        return cls(
            training_candles=training_candles,
            forbidden_scalar_tokens=frozenset(held_out_tokens - training_tokens),
            reject_unproven_scalars=reject_unproven_scalars,
            seen=set(),
        )

    @staticmethod
    def _market_scalar_tokens(candles: tuple[Candle, ...]) -> set[tuple[type[object], object]]:
        tokens: set[tuple[type[object], object]] = set()
        for candle in candles:
            for descriptor in fields(candle):
                value = getattr(candle, descriptor.name)
                if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
                    continue
                tokens.add((type(value), value))
                if isinstance(value, (int, Decimal)):
                    tokens.add((str, format(value, "f")))
                    tokens.add((float, float(value)))
        return tokens

    def inspect(self, value: object, *, path: str) -> None:
        if isinstance(value, Candle):
            if value not in self.training_candles:
                raise WalkForwardTrainingError(
                    f"{path} retains held-out market data outside the train slice"
                )
            return
        if value is None or isinstance(value, (bool, bytes, Direction)):
            return
        if isinstance(value, (int, float, str, Decimal)):
            if (type(value), value) in self.forbidden_scalar_tokens:
                raise WalkForwardTrainingError(
                    f"{path} retains a held-out market scalar outside the train slice"
                )
            if self.reject_unproven_scalars:
                raise WalkForwardTrainingError(
                    f"{path} contains scalar state whose train-only origin cannot be isolated"
                )
            return
        if isinstance(value, ModuleType):
            return

        value_id = id(value)
        if value_id in self.seen:
            return
        self.seen.add(value_id)

        if isinstance(value, Mapping):
            for key, nested_value in value.items():
                self.inspect(key, path=f"{path}.key")
                self.inspect(nested_value, path=f"{path}[{key!r}]")
            return
        if isinstance(value, (tuple, list, set, frozenset)):
            for index, nested_value in enumerate(value):
                self.inspect(nested_value, path=f"{path}[{index}]")
            return
        if isinstance(value, MethodType):
            self.inspect(value.__self__, path=f"{path}.__self__")
            self.inspect(value.__func__, path=f"{path}.__func__")
            return
        if isinstance(value, FunctionType):
            self._inspect_function(value, path=path)
            return
        if isinstance(value, type):
            self._inspect_class(value, path=path)
            return

        inspected = False
        if is_dataclass(value) and not isinstance(value, type):
            inspected = True
            for descriptor in fields(value):
                self.inspect(
                    getattr(value, descriptor.name),
                    path=f"{path}.{descriptor.name}",
                )
        attributes = getattr(value, "__dict__", None)
        if isinstance(attributes, dict):
            inspected = True
            for name, nested_value in attributes.items():
                self.inspect(nested_value, path=f"{path}.{name}")
        for owner in type(value).__mro__:
            slots = vars(owner).get("__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for slot in slots:
                if slot in {"__dict__", "__weakref__"} or not hasattr(value, slot):
                    continue
                inspected = True
                self.inspect(getattr(value, slot), path=f"{path}.{slot}")
        self._inspect_class(type(value), path=f"{path}.__class__")
        if not inspected and type(value).__module__ == "builtins":
            raise WalkForwardTrainingError(
                f"{path} contains opaque state that the train-only guard cannot isolate"
            )

    def _inspect_function(self, function: FunctionType, *, path: str) -> None:
        defaults = function.__defaults__ or ()
        self.inspect(defaults, path=f"{path}.__defaults__")
        for name, default in (function.__kwdefaults__ or {}).items():
            self.inspect(default, path=f"{path}.__kwdefaults__[{name!r}]")
        for index, cell in enumerate(function.__closure__ or ()):
            try:
                cell_value = cell.cell_contents
            except ValueError:
                continue
            self.inspect(cell_value, path=f"{path}.__closure__[{index}]")
        for name in function.__code__.co_names:
            if name not in function.__globals__:
                continue
            global_value = function.__globals__[name]
            if self._global_requires_inspection(global_value, function=function):
                self.inspect(global_value, path=f"{path}.__globals__[{name!r}]")

    @staticmethod
    def _global_requires_inspection(value: object, *, function: FunctionType) -> bool:
        if isinstance(
            value,
            (
                Candle,
                Decimal,
                FunctionType,
                Mapping,
                float,
                int,
                list,
                str,
                tuple,
                set,
                frozenset,
            ),
        ):
            return True
        if isinstance(value, type):
            return value.__module__ == function.__module__
        return type(value).__module__ == function.__module__

    def _inspect_class(self, value: type[object], *, path: str) -> None:
        for name, nested_value in vars(value).items():
            if name.startswith("__") and name not in {"__slots__"}:
                continue
            if name == "strategy_id":
                continue
            if isinstance(nested_value, (staticmethod, classmethod)):
                nested_value = nested_value.__func__
            elif isinstance(nested_value, property):
                nested_value = nested_value.fget
            if nested_value is None:
                continue
            if isinstance(
                nested_value,
                (
                    Candle,
                    Decimal,
                    FunctionType,
                    Mapping,
                    float,
                    int,
                    list,
                    str,
                    tuple,
                    set,
                    frozenset,
                ),
            ):
                self.inspect(nested_value, path=f"{path}.{name}")


@dataclass(frozen=True, slots=True)
class BacktestCosts:
    entry_fee_rate: Decimal
    exit_fee_rate: Decimal
    slippage_bps: Decimal

    def __post_init__(self) -> None:
        values = (self.entry_fee_rate, self.exit_fee_rate, self.slippage_bps)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
            raise ValueError("backtest costs must be finite Decimal values")
        if min(values) < ZERO:
            raise ValueError("backtest costs must not be negative")


@dataclass(frozen=True, slots=True)
class FundingSettlement:
    symbol: str
    timeframe: str
    settled_at_ms: int
    rate: Decimal
    mark_price: Decimal

    def __post_init__(self) -> None:
        if not self.symbol or not self.timeframe:
            raise ValueError("funding settlement symbol and timeframe are required")
        if (
            not isinstance(self.settled_at_ms, int)
            or isinstance(self.settled_at_ms, bool)
            or self.settled_at_ms < 0
        ):
            raise ValueError("funding settlement time must be a non-negative integer")
        if (
            not isinstance(self.rate, Decimal)
            or not self.rate.is_finite()
            or not isinstance(self.mark_price, Decimal)
            or not self.mark_price.is_finite()
            or self.mark_price <= ZERO
        ):
            raise ValueError("funding settlement values are invalid")


@dataclass(frozen=True, slots=True)
class SimulatedTrade:
    strategy_id: str
    direction: Direction
    signal_bar_index: int
    entry_bar_index: int
    exit_bar_index: int
    entry_price: Decimal
    exit_price: Decimal
    market_gross_pnl: Decimal
    execution_pnl: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    slippage_cost: Decimal
    funding_pnl: Decimal
    net_pnl: Decimal

    @property
    def gross_pnl(self) -> Decimal:
        return self.market_gross_pnl


@dataclass(frozen=True, slots=True)
class BacktestResult:
    trades: tuple[SimulatedTrade, ...]

    @property
    def net_pnl(self) -> Decimal:
        return sum((trade.net_pnl for trade in self.trades), ZERO)

    @property
    def gross_pnl(self) -> Decimal:
        return sum((trade.market_gross_pnl for trade in self.trades), ZERO)


class BacktestEngine:
    """Executes a signal only on the next bar, preventing close-price look-ahead."""

    def run(
        self,
        strategy: Strategy,
        candles: Sequence[Candle],
        *,
        timeframe: str,
        costs: BacktestCosts,
        evaluation_time_ms: int,
        quantity: Decimal = Decimal("1"),
        start_index: int = 0,
        holding_bars: int = 1,
        funding_settlements: Sequence[FundingSettlement] = (),
    ) -> BacktestResult:
        if quantity <= ZERO:
            raise ValueError("quantity must be positive")
        if start_index < 0:
            raise ValueError("start_index must be non-negative")
        if holding_bars < 1:
            raise ValueError("holding_bars must be positive")
        series = tuple(candles)
        self._validate_candles(series, timeframe=timeframe, evaluation_time_ms=evaluation_time_ms)
        settlements = tuple(funding_settlements)
        self._validate_funding_settlements(
            settlements,
            symbol=series[0].symbol,
            timeframe=timeframe,
        )

        trades: list[SimulatedTrade] = []
        next_available_signal_index = 0
        for signal_index in range(len(series) - holding_bars):
            if signal_index < next_available_signal_index:
                continue
            historical_candles = series[: signal_index + 1]
            signal = strategy.evaluate(historical_candles, timeframe=timeframe)
            if signal is None:
                continue
            next_candle = series[signal_index + 1]
            self._validate_signal(signal, entry_candle=next_candle, timeframe=timeframe)
            exit_candle = series[signal_index + holding_bars]
            trades.append(
                self._simulate_trade(
                    signal=signal,
                    signal_bar_index=start_index + signal_index,
                    entry_bar_index=start_index + signal_index + 1,
                    exit_bar_index=start_index + signal_index + holding_bars,
                    entry_candle=next_candle,
                    exit_candle=exit_candle,
                    costs=costs,
                    quantity=quantity,
                    funding_settlements=settlements,
                )
            )
            next_available_signal_index = signal_index + holding_bars

        return BacktestResult(trades=tuple(trades))

    @staticmethod
    def _validate_candles(
        candles: Sequence[Candle],
        *,
        timeframe: str,
        evaluation_time_ms: int,
    ) -> None:
        if not candles:
            raise BacktestDataError("backtest requires at least one candle")
        if evaluation_time_ms < 0:
            raise BacktestDataError("evaluation time must be non-negative")
        symbol = candles[0].symbol
        previous: Candle | None = None
        for candle in candles:
            if candle.symbol != symbol:
                raise BacktestDataError("candle series must contain exactly one symbol")
            if candle.timeframe != timeframe:
                raise BacktestDataError("candle timeframe does not match the backtest timeframe")
            if candle.close_time_ms > evaluation_time_ms:
                raise BacktestDataError("all evaluation candles must be closed")
            if previous is not None and candle.open_time_ms <= previous.close_time_ms:
                raise BacktestDataError(
                    "candle times must be strictly increasing and non-overlapping"
                )
            previous = candle

    @staticmethod
    def _validate_signal(
        signal: SignalCandidate,
        *,
        entry_candle: Candle,
        timeframe: str,
    ) -> None:
        if signal.symbol != entry_candle.symbol:
            raise BacktestSignalError("signal symbol does not match the next entry candle")
        if signal.timeframe != timeframe:
            raise BacktestSignalError("signal timeframe does not match the backtest timeframe")
        if signal.valid_until_ms < entry_candle.open_time_ms:
            raise BacktestSignalError("signal is stale at the next-bar entry time")

    @staticmethod
    def _validate_funding_settlements(
        settlements: Sequence[FundingSettlement],
        *,
        symbol: str,
        timeframe: str,
    ) -> None:
        previous_settlement_ms: int | None = None
        for settlement in settlements:
            if settlement.symbol != symbol:
                raise BacktestDataError("funding settlement symbol does not match candle series")
            if settlement.timeframe != timeframe:
                raise BacktestDataError("funding settlement timeframe does not match backtest")
            if (
                previous_settlement_ms is not None
                and settlement.settled_at_ms <= previous_settlement_ms
            ):
                raise BacktestDataError("funding settlements must be strictly increasing")
            previous_settlement_ms = settlement.settled_at_ms

    @staticmethod
    def _simulate_trade(
        *,
        signal: SignalCandidate,
        signal_bar_index: int,
        entry_bar_index: int,
        exit_bar_index: int,
        entry_candle: Candle,
        exit_candle: Candle,
        costs: BacktestCosts,
        quantity: Decimal,
        funding_settlements: Sequence[FundingSettlement],
    ) -> SimulatedTrade:
        raw_entry = entry_candle.open_price
        raw_exit = exit_candle.close_price
        slippage_rate = costs.slippage_bps / _BPS_DENOMINATOR
        if signal.direction is Direction.LONG:
            executed_entry = raw_entry * (Decimal("1") + slippage_rate)
            executed_exit = raw_exit * (Decimal("1") - slippage_rate)
            market_gross_pnl = (raw_exit - raw_entry) * quantity
            execution_pnl = (executed_exit - executed_entry) * quantity
        else:
            executed_entry = raw_entry * (Decimal("1") - slippage_rate)
            executed_exit = raw_exit * (Decimal("1") + slippage_rate)
            market_gross_pnl = (raw_entry - raw_exit) * quantity
            execution_pnl = (executed_entry - executed_exit) * quantity

        entry_fee = executed_entry * quantity * costs.entry_fee_rate
        exit_fee = executed_exit * quantity * costs.exit_fee_rate
        slippage_cost = market_gross_pnl - execution_pnl
        funding_pnl = sum(
            (
                BacktestEngine._signed_funding(
                    direction=signal.direction,
                    quantity=quantity,
                    settlement=settlement,
                )
                for settlement in funding_settlements
                if entry_candle.open_time_ms < settlement.settled_at_ms <= exit_candle.close_time_ms
            ),
            ZERO,
        )
        net_pnl = execution_pnl - entry_fee - exit_fee + funding_pnl
        return SimulatedTrade(
            strategy_id=signal.strategy_id,
            direction=signal.direction,
            signal_bar_index=signal_bar_index,
            entry_bar_index=entry_bar_index,
            exit_bar_index=exit_bar_index,
            entry_price=executed_entry,
            exit_price=executed_exit,
            market_gross_pnl=market_gross_pnl,
            execution_pnl=execution_pnl,
            entry_fee=entry_fee,
            exit_fee=exit_fee,
            slippage_cost=slippage_cost,
            funding_pnl=funding_pnl,
            net_pnl=net_pnl,
        )

    @staticmethod
    def _signed_funding(
        *,
        direction: Direction,
        quantity: Decimal,
        settlement: FundingSettlement,
    ) -> Decimal:
        funding_payment = quantity * settlement.mark_price * settlement.rate
        return -funding_payment if direction is Direction.LONG else funding_payment


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    configuration_fingerprint: str
    training_data_fingerprint: str
    result: BacktestResult


class WalkForwardRunner:
    """Runs deterministic out-of-sample slices; it does not optimize on test data."""

    def __init__(self, *, train_size: int, test_size: int, step_size: int) -> None:
        if min(train_size, test_size, step_size) <= 0:
            raise ValueError("walk-forward window sizes must be positive")
        if step_size < test_size:
            raise ValueError("walk-forward test windows must not overlap")
        self.train_size = train_size
        self.test_size = test_size
        self.step_size = step_size

    def run(
        self,
        strategy_specification: StrategySpecification,
        candles: Sequence[Candle],
        *,
        timeframe: str,
        costs: BacktestCosts,
        funding_settlements: Sequence[FundingSettlement] = (),
    ) -> tuple[WalkForwardWindow, ...]:
        if type(strategy_specification) is not StrategySpecification:
            raise WalkForwardTrainingError(
                "custom walk-forward factories cannot isolate held-out state; "
                "an exact frozen StrategySpecification is required"
            )
        try:
            strategy_specification.verify_fingerprint()
        except ValueError as error:
            raise WalkForwardTrainingError(
                "strategy specification fingerprint is invalid"
            ) from error
        series = tuple(candles)
        settlements = tuple(funding_settlements)
        windows: list[WalkForwardWindow] = []
        start = 0
        engine = BacktestEngine()
        while start + self.train_size + self.test_size <= len(series):
            train_end = start + self.train_size
            test_end = train_end + self.test_size
            segment = series[start:test_end]
            training_candles = tuple(series[start:train_end])
            frozen_strategy, training_data_fingerprint = self._fit_train_only(
                strategy_specification,
                training_candles=training_candles,
                timeframe=timeframe,
            )
            raw_result = engine.run(
                frozen_strategy,
                segment,
                timeframe=timeframe,
                costs=costs,
                evaluation_time_ms=segment[-1].close_time_ms,
                start_index=start,
                funding_settlements=settlements,
            )
            test_trades = tuple(
                trade
                for trade in raw_result.trades
                if train_end <= trade.entry_bar_index < test_end
            )
            windows.append(
                WalkForwardWindow(
                    train_start=start,
                    train_end=train_end,
                    test_start=train_end,
                    test_end=test_end,
                    configuration_fingerprint=frozen_strategy.configuration_fingerprint,
                    training_data_fingerprint=training_data_fingerprint,
                    result=BacktestResult(trades=test_trades),
                )
            )
            start += self.step_size
        return tuple(windows)

    @classmethod
    def _fit_train_only(
        cls,
        strategy_specification: StrategySpecification,
        *,
        training_candles: tuple[Candle, ...],
        timeframe: str,
    ) -> tuple[FrozenStrategy, str]:
        from app.strategy.strategies import frozen_strategy_from_fit

        if type(strategy_specification) is not StrategySpecification:
            raise WalkForwardTrainingError(
                "walk-forward fit requires an exact frozen StrategySpecification"
            )
        try:
            strategy_specification.verify_fingerprint()
        except ValueError as error:
            raise WalkForwardTrainingError(
                "strategy specification fingerprint is invalid"
            ) from error
        if not training_candles:
            raise WalkForwardTrainingError("walk-forward training slice cannot be empty")
        if any(candle.timeframe != timeframe for candle in training_candles):
            raise WalkForwardTrainingError(
                "walk-forward training timeframe does not match its immutable slice"
            )
        training_data_fingerprint = cls._training_data_fingerprint(training_candles)
        fit_result = StrategyFitResult(
            specification=strategy_specification,
            training_data_fingerprint=training_data_fingerprint,
            training_candle_count=len(training_candles),
            training_end_ms=training_candles[-1].close_time_ms,
            timeframe=timeframe,
        )
        try:
            fit_result.verify_fingerprint()
        except ValueError as error:
            raise WalkForwardTrainingError("train-only fit provenance is invalid") from error
        frozen_strategy = frozen_strategy_from_fit(fit_result)
        if (
            frozen_strategy.training_candle_count != len(training_candles)
            or frozen_strategy.training_end_ms != training_candles[-1].close_time_ms
        ):
            raise WalkForwardTrainingError(
                "frozen strategy training provenance does not match the train slice"
            )
        if frozen_strategy.configuration_fingerprint != strategy_specification.fingerprint:
            raise WalkForwardTrainingError("frozen strategy changed its specification")
        return frozen_strategy, training_data_fingerprint

    @staticmethod
    def _training_data_fingerprint(training_candles: tuple[Candle, ...]) -> str:
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
                for candle in training_candles
            ],
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()
