"""Deterministic, close-only strategy research with explicit cost accounting."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.domain.decimal_math import ZERO
from app.domain.types import Direction
from app.strategy.models import Candle, SignalCandidate, Strategy

_BPS_DENOMINATOR = Decimal("10000")


@dataclass(frozen=True, slots=True)
class BacktestCosts:
    entry_fee_rate: Decimal
    exit_fee_rate: Decimal
    slippage_bps: Decimal
    funding_rate: Decimal

    def __post_init__(self) -> None:
        if (
            min(self.entry_fee_rate, self.exit_fee_rate, self.slippage_bps, self.funding_rate)
            < ZERO
        ):
            raise ValueError("backtest costs must not be negative")


@dataclass(frozen=True, slots=True)
class SimulatedTrade:
    strategy_id: str
    direction: Direction
    signal_bar_index: int
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    slippage_cost: Decimal
    funding_cost: Decimal
    net_pnl: Decimal


@dataclass(frozen=True, slots=True)
class BacktestResult:
    trades: tuple[SimulatedTrade, ...]

    @property
    def net_pnl(self) -> Decimal:
        return sum((trade.net_pnl for trade in self.trades), ZERO)

    @property
    def gross_pnl(self) -> Decimal:
        return sum((trade.gross_pnl for trade in self.trades), ZERO)


class BacktestEngine:
    """Executes a signal only on the next bar, preventing close-price look-ahead."""

    def run(
        self,
        strategy: Strategy,
        candles: Sequence[Candle],
        *,
        timeframe: str,
        costs: BacktestCosts,
        quantity: Decimal = Decimal("1"),
    ) -> BacktestResult:
        if quantity <= ZERO:
            raise ValueError("quantity must be positive")

        trades: list[SimulatedTrade] = []
        for signal_index in range(len(candles) - 1):
            historical_candles = candles[: signal_index + 1]
            signal = strategy.evaluate(historical_candles, timeframe=timeframe)
            if signal is None:
                continue
            next_candle = candles[signal_index + 1]
            trades.append(
                self._simulate_trade(
                    signal=signal,
                    signal_bar_index=signal_index,
                    entry_candle=next_candle,
                    exit_candle=next_candle,
                    costs=costs,
                    quantity=quantity,
                )
            )

        return BacktestResult(trades=tuple(trades))

    @staticmethod
    def _simulate_trade(
        *,
        signal: SignalCandidate,
        signal_bar_index: int,
        entry_candle: Candle,
        exit_candle: Candle,
        costs: BacktestCosts,
        quantity: Decimal,
    ) -> SimulatedTrade:
        raw_entry = entry_candle.open_price
        raw_exit = exit_candle.close_price
        slippage_rate = costs.slippage_bps / _BPS_DENOMINATOR
        if signal.direction is Direction.LONG:
            executed_entry = raw_entry * (Decimal("1") + slippage_rate)
            executed_exit = raw_exit * (Decimal("1") - slippage_rate)
            gross_pnl = (raw_exit - raw_entry) * quantity
        else:
            executed_entry = raw_entry * (Decimal("1") - slippage_rate)
            executed_exit = raw_exit * (Decimal("1") + slippage_rate)
            gross_pnl = (raw_entry - raw_exit) * quantity

        entry_fee = executed_entry * quantity * costs.entry_fee_rate
        exit_fee = executed_exit * quantity * costs.exit_fee_rate
        slippage_cost = (abs(executed_entry - raw_entry) + abs(executed_exit - raw_exit)) * quantity
        funding_cost = executed_entry * quantity * costs.funding_rate
        net_pnl = gross_pnl - entry_fee - exit_fee - slippage_cost - funding_cost
        return SimulatedTrade(
            strategy_id=signal.strategy_id,
            direction=signal.direction,
            signal_bar_index=signal_bar_index,
            entry_price=executed_entry,
            exit_price=executed_exit,
            gross_pnl=gross_pnl,
            entry_fee=entry_fee,
            exit_fee=exit_fee,
            slippage_cost=slippage_cost,
            funding_cost=funding_cost,
            net_pnl=net_pnl,
        )


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    result: BacktestResult


class WalkForwardRunner:
    """Runs deterministic out-of-sample slices; it does not optimize on test data."""

    def __init__(self, *, train_size: int, test_size: int, step_size: int) -> None:
        if min(train_size, test_size, step_size) <= 0:
            raise ValueError("walk-forward window sizes must be positive")
        self.train_size = train_size
        self.test_size = test_size
        self.step_size = step_size

    def run(
        self,
        strategy: Strategy,
        candles: Sequence[Candle],
        *,
        timeframe: str,
        costs: BacktestCosts,
    ) -> tuple[WalkForwardWindow, ...]:
        windows: list[WalkForwardWindow] = []
        start = 0
        engine = BacktestEngine()
        while start + self.train_size + self.test_size <= len(candles):
            train_end = start + self.train_size
            test_end = train_end + self.test_size
            segment = candles[start:test_end]
            raw_result = engine.run(strategy, segment, timeframe=timeframe, costs=costs)
            test_trades = tuple(
                trade for trade in raw_result.trades if trade.signal_bar_index >= self.train_size
            )
            windows.append(
                WalkForwardWindow(
                    train_start=start,
                    train_end=train_end,
                    test_start=train_end,
                    test_end=test_end,
                    result=BacktestResult(trades=test_trades),
                )
            )
            start += self.step_size
        return tuple(windows)
