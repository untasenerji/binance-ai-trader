"""Deterministic, close-only strategy research with explicit cost accounting."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.domain.decimal_math import ZERO
from app.domain.types import Direction
from app.strategy.models import Candle, FrozenStrategy, SignalCandidate, Strategy, TrainableStrategy

_BPS_DENOMINATOR = Decimal("10000")


class BacktestDataError(ValueError):
    """Raised when a candle series cannot support a temporal-safe backtest."""


class BacktestSignalError(ValueError):
    """Raised when a strategy candidate cannot legally enter on the next bar."""


class WalkForwardTrainingError(ValueError):
    """Raised when a walk-forward trainer cannot prove a frozen train-only snapshot."""


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
        strategy_factory: Callable[[], TrainableStrategy],
        candles: Sequence[Candle],
        *,
        timeframe: str,
        costs: BacktestCosts,
    ) -> tuple[WalkForwardWindow, ...]:
        series = tuple(candles)
        windows: list[WalkForwardWindow] = []
        start = 0
        engine = BacktestEngine()
        while start + self.train_size + self.test_size <= len(series):
            train_end = start + self.train_size
            test_end = train_end + self.test_size
            segment = series[start:test_end]
            training_candles = tuple(series[start:train_end])
            trainer = strategy_factory()
            frozen_strategy = self._fit_train_only(
                trainer,
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
                    result=BacktestResult(trades=test_trades),
                )
            )
            start += self.step_size
        return tuple(windows)

    @staticmethod
    def _fit_train_only(
        trainer: TrainableStrategy,
        *,
        training_candles: tuple[Candle, ...],
        timeframe: str,
    ) -> FrozenStrategy:
        fit = getattr(trainer, "fit", None)
        if not callable(fit):
            raise WalkForwardTrainingError("walk-forward trainer must implement fit")
        frozen_strategy = fit(training_candles, timeframe=timeframe)
        if not isinstance(frozen_strategy, FrozenStrategy):
            raise WalkForwardTrainingError("walk-forward fit must return FrozenStrategy")
        if (
            frozen_strategy.training_candle_count != len(training_candles)
            or frozen_strategy.training_end_ms != training_candles[-1].close_time_ms
        ):
            raise WalkForwardTrainingError(
                "frozen strategy training provenance does not match the train slice"
            )
        return frozen_strategy
