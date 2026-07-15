"""Exact fill accounting and confirmed-position stop-risk evaluation."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.domain.decimal_math import ZERO
from app.domain.types import Direction


class FillLedgerError(ValueError):
    pass


class PositionQuantityMismatch(FillLedgerError):
    pass


class FillSemanticConflict(FillLedgerError):
    pass


@dataclass(frozen=True, slots=True)
class FillEvent:
    trade_id: str
    client_order_id: str
    last_quantity: Decimal
    cumulative_quantity: Decimal
    fill_price: Decimal
    fee: Decimal
    fee_asset: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not self.trade_id or not self.client_order_id or not self.fee_asset:
            raise FillLedgerError("fill events need trade, client order, and fee asset identifiers")
        decimal_values = (
            self.last_quantity,
            self.cumulative_quantity,
            self.fill_price,
            self.fee,
        )
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in decimal_values):
            raise FillLedgerError("fill values must be finite Decimal values")
        if self.last_quantity <= ZERO or self.cumulative_quantity < self.last_quantity:
            raise FillLedgerError("fill quantities are inconsistent")
        if self.fill_price <= ZERO or self.fee < ZERO:
            raise FillLedgerError("fill price and fee are invalid")
        if self.occurred_at.tzinfo is None:
            raise FillLedgerError("fill timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class FillLedgerReceipt:
    is_duplicate: bool
    filled_quantity: Decimal
    average_fill_price: Decimal
    total_fee: Decimal


@dataclass(frozen=True, slots=True)
class ConfirmedPositionRisk:
    confirmed_position_quantity: Decimal
    average_entry_price: Decimal | None
    actual_notional_usdt: Decimal
    actual_required_margin_usdt: Decimal
    actual_stop_risk: Decimal
    pending_entries_blocked: bool
    hard_halted: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class ActualRiskPolicy:
    """Immutable local policy used to gate simulated entry stages after real fills."""

    plan_id: str
    symbol: str
    direction: Direction
    worst_stop_exit_price: Decimal
    exit_fee_rate: Decimal
    funding_buffer_rate: Decimal
    funding_interval_count: int
    risk_budget: Decimal
    max_symbol_exposure_usdt: Decimal
    max_total_exposure_usdt: Decimal
    existing_symbol_exposure_usdt: Decimal
    existing_total_exposure_usdt: Decimal
    effective_leverage: int
    required_reserve_usdt: Decimal
    effective_equity_usdt: Decimal
    protective_stop_reference: str
    reduce_only_exit_reference: str
    stop_confirmed: bool

    def __post_init__(self) -> None:
        if not self.plan_id or not self.symbol:
            raise FillLedgerError("actual-risk policy needs plan and symbol identifiers")
        if not self.protective_stop_reference or not self.reduce_only_exit_reference:
            raise FillLedgerError("actual-risk policy needs durable protection references")
        if not isinstance(self.direction, Direction):
            raise TypeError("actual-risk policy direction must be typed")
        if not isinstance(self.stop_confirmed, bool):
            raise TypeError("actual-risk policy stop confirmation must be boolean")
        # Keep policy validation aligned with the financial evaluator so a malformed
        # policy cannot silently skip the post-fill entry gate.
        evaluate_confirmed_position_risk(
            direction=self.direction,
            signed_confirmed_position_quantity=ZERO,
            fills=FillLedger(),
            worst_stop_exit_price=self.worst_stop_exit_price,
            exit_fee_rate=self.exit_fee_rate,
            funding_buffer_rate=self.funding_buffer_rate,
            funding_interval_count=self.funding_interval_count,
            risk_budget=self.risk_budget,
            stop_confirmed=self.stop_confirmed,
            max_symbol_exposure_usdt=self.max_symbol_exposure_usdt,
            max_total_exposure_usdt=self.max_total_exposure_usdt,
            existing_symbol_exposure_usdt=self.existing_symbol_exposure_usdt,
            existing_total_exposure_usdt=self.existing_total_exposure_usdt,
            effective_leverage=self.effective_leverage,
            required_reserve_usdt=self.required_reserve_usdt,
            effective_equity_usdt=self.effective_equity_usdt,
        )


@dataclass(slots=True)
class FillLedger:
    """Deduplicates exact exchange fills across all entry stages without financial gaps."""

    _events_by_trade_id: dict[str, FillEvent] = field(default_factory=dict)

    @classmethod
    def from_events(cls, events: Iterable[FillEvent]) -> "FillLedger":
        ledger = cls()
        for event in events:
            ledger.record(event)
        return ledger

    def record(self, event: FillEvent) -> FillLedgerReceipt:
        existing = self._events_by_trade_id.get(event.trade_id)
        if existing is not None:
            if existing != event:
                raise FillSemanticConflict("trade ID was redelivered with a semantic conflict")
            return self._receipt(is_duplicate=True)
        self._assert_contiguous_client_fills(
            (*self._events_for_client(event.client_order_id), event)
        )
        self._events_by_trade_id[event.trade_id] = event
        return self._receipt(is_duplicate=False)

    @property
    def filled_quantity(self) -> Decimal:
        return sum((event.last_quantity for event in self._events_by_trade_id.values()), ZERO)

    @property
    def average_fill_price(self) -> Decimal:
        if self.filled_quantity <= ZERO:
            raise FillLedgerError("average fill price requires at least one fill")
        filled_notional = sum(
            (event.last_quantity * event.fill_price for event in self._events_by_trade_id.values()),
            ZERO,
        )
        return filled_notional / self.filled_quantity

    @property
    def total_fee(self) -> Decimal:
        return sum((event.fee for event in self._events_by_trade_id.values()), ZERO)

    @property
    def observed_cumulative_quantity(self) -> Decimal:
        return sum(
            (
                max(event.cumulative_quantity for event in self._events_for_client(client_order_id))
                for client_order_id in self.client_order_ids
            ),
            ZERO,
        )

    @property
    def client_order_ids(self) -> frozenset[str]:
        return frozenset(event.client_order_id for event in self._events_by_trade_id.values())

    @property
    def fee_assets(self) -> frozenset[str]:
        return frozenset(event.fee_asset for event in self._events_by_trade_id.values())

    @property
    def events(self) -> tuple[FillEvent, ...]:
        return tuple(
            sorted(
                self._events_by_trade_id.values(),
                key=lambda event: (
                    event.client_order_id,
                    event.cumulative_quantity,
                    event.trade_id,
                ),
            )
        )

    def filled_quantity_for(self, client_order_id: str) -> Decimal:
        return sum(
            (event.last_quantity for event in self._events_for_client(client_order_id)),
            ZERO,
        )

    def _events_for_client(self, client_order_id: str) -> tuple[FillEvent, ...]:
        return tuple(
            event
            for event in self._events_by_trade_id.values()
            if event.client_order_id == client_order_id
        )

    @staticmethod
    def _assert_contiguous_client_fills(events: Iterable[FillEvent]) -> None:
        expected_cumulative = ZERO
        for event in sorted(
            events, key=lambda candidate: (candidate.cumulative_quantity, candidate.trade_id)
        ):
            if event.cumulative_quantity - event.last_quantity != expected_cumulative:
                raise FillLedgerError(
                    "fill cumulative quantities must be contiguous per client order"
                )
            expected_cumulative = event.cumulative_quantity

    def _receipt(self, *, is_duplicate: bool) -> FillLedgerReceipt:
        return FillLedgerReceipt(
            is_duplicate=is_duplicate,
            filled_quantity=self.filled_quantity,
            average_fill_price=self.average_fill_price,
            total_fee=self.total_fee,
        )


def evaluate_confirmed_position_risk(
    *,
    direction: Direction,
    signed_confirmed_position_quantity: Decimal,
    fills: FillLedger,
    worst_stop_exit_price: Decimal,
    exit_fee_rate: Decimal,
    funding_buffer_rate: Decimal,
    funding_interval_count: int,
    risk_budget: Decimal,
    stop_confirmed: bool,
    max_symbol_exposure_usdt: Decimal | None = None,
    max_total_exposure_usdt: Decimal | None = None,
    existing_symbol_exposure_usdt: Decimal = ZERO,
    existing_total_exposure_usdt: Decimal = ZERO,
    effective_leverage: int | None = None,
    required_reserve_usdt: Decimal = ZERO,
    effective_equity_usdt: Decimal | None = None,
) -> ConfirmedPositionRisk:
    """Use confirmed exchange quantity, never a local fill total, as the position authority."""
    decimal_values = (
        signed_confirmed_position_quantity,
        worst_stop_exit_price,
        exit_fee_rate,
        funding_buffer_rate,
        risk_budget,
        existing_symbol_exposure_usdt,
        existing_total_exposure_usdt,
        required_reserve_usdt,
    )
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in decimal_values):
        raise FillLedgerError("position-risk values must be finite Decimal values")
    if worst_stop_exit_price <= ZERO or min(exit_fee_rate, funding_buffer_rate, risk_budget) < ZERO:
        raise FillLedgerError("position-risk inputs are invalid")
    if (
        not isinstance(funding_interval_count, int)
        or isinstance(funding_interval_count, bool)
        or funding_interval_count < 0
    ):
        raise FillLedgerError("funding interval count is invalid")
    cap_values = (max_symbol_exposure_usdt, max_total_exposure_usdt, effective_equity_usdt)
    if any(
        value is not None and (not isinstance(value, Decimal) or not value.is_finite())
        for value in cap_values
    ):
        raise FillLedgerError("exposure and margin caps must be finite Decimal values")
    if (
        min(
            existing_symbol_exposure_usdt,
            existing_total_exposure_usdt,
            required_reserve_usdt,
        )
        < ZERO
    ):
        raise FillLedgerError("existing exposure and reserve values must be non-negative")
    has_cap_policy = any(value is not None for value in cap_values)
    if has_cap_policy:
        if any(value is None for value in cap_values):
            raise FillLedgerError("exposure and margin caps must be supplied together")
        assert max_symbol_exposure_usdt is not None
        assert max_total_exposure_usdt is not None
        assert effective_equity_usdt is not None
        if min(max_symbol_exposure_usdt, max_total_exposure_usdt, effective_equity_usdt) < ZERO:
            raise FillLedgerError("exposure and equity caps must be non-negative")
        if (
            not isinstance(effective_leverage, int)
            or isinstance(effective_leverage, bool)
            or effective_leverage < 1
        ):
            raise FillLedgerError("effective leverage must be a positive integer")
    elif effective_leverage is not None:
        raise FillLedgerError("effective leverage requires complete exposure and margin caps")
    if signed_confirmed_position_quantity == ZERO:
        return ConfirmedPositionRisk(
            confirmed_position_quantity=ZERO,
            average_entry_price=None,
            actual_notional_usdt=ZERO,
            actual_required_margin_usdt=ZERO,
            actual_stop_risk=ZERO,
            pending_entries_blocked=False,
            hard_halted=False,
            reason=None,
        )
    if direction is Direction.LONG and signed_confirmed_position_quantity < ZERO:
        raise PositionQuantityMismatch("long position quantity must be positive")
    if direction is Direction.SHORT and signed_confirmed_position_quantity > ZERO:
        raise PositionQuantityMismatch("short position quantity must be negative")
    confirmed_quantity = abs(signed_confirmed_position_quantity)
    if fills.filled_quantity <= ZERO or confirmed_quantity > fills.filled_quantity:
        raise PositionQuantityMismatch("confirmed exchange position exceeds local entry fills")
    if fills.fee_assets != frozenset({"USDT"}):
        raise FillLedgerError("non-USDT fees need a verified conversion before risk evaluation")

    average_entry = fills.average_fill_price
    price_loss_per_unit = (
        average_entry - worst_stop_exit_price
        if direction is Direction.LONG
        else worst_stop_exit_price - average_entry
    )
    if price_loss_per_unit < ZERO:
        raise FillLedgerError("stop is on the non-loss side of the confirmed entry")
    allocated_entry_fee = fills.total_fee * confirmed_quantity / fills.filled_quantity
    projected_exit_fee = confirmed_quantity * worst_stop_exit_price * exit_fee_rate
    funding_buffer = (
        confirmed_quantity * average_entry * funding_buffer_rate * funding_interval_count
    )
    actual_risk = (
        confirmed_quantity * price_loss_per_unit
        + allocated_entry_fee
        + projected_exit_fee
        + funding_buffer
    )
    actual_notional = confirmed_quantity * average_entry
    actual_required_margin = (
        (existing_total_exposure_usdt + actual_notional) / Decimal(effective_leverage)
        + required_reserve_usdt
        if effective_leverage is not None
        else ZERO
    )
    if not stop_confirmed:
        return ConfirmedPositionRisk(
            confirmed_position_quantity=confirmed_quantity,
            average_entry_price=average_entry,
            actual_notional_usdt=actual_notional,
            actual_required_margin_usdt=actual_required_margin,
            actual_stop_risk=actual_risk,
            pending_entries_blocked=True,
            hard_halted=True,
            reason="STOP_UNCONFIRMED",
        )
    if has_cap_policy:
        assert max_symbol_exposure_usdt is not None
        assert max_total_exposure_usdt is not None
        assert effective_equity_usdt is not None
        if (
            existing_symbol_exposure_usdt + actual_notional > max_symbol_exposure_usdt
            or existing_total_exposure_usdt + actual_notional > max_total_exposure_usdt
        ):
            return ConfirmedPositionRisk(
                confirmed_position_quantity=confirmed_quantity,
                average_entry_price=average_entry,
                actual_notional_usdt=actual_notional,
                actual_required_margin_usdt=actual_required_margin,
                actual_stop_risk=actual_risk,
                pending_entries_blocked=True,
                hard_halted=False,
                reason="ACTUAL_EXPOSURE_LIMIT_BREACH",
            )
        if actual_required_margin > effective_equity_usdt:
            return ConfirmedPositionRisk(
                confirmed_position_quantity=confirmed_quantity,
                average_entry_price=average_entry,
                actual_notional_usdt=actual_notional,
                actual_required_margin_usdt=actual_required_margin,
                actual_stop_risk=actual_risk,
                pending_entries_blocked=True,
                hard_halted=False,
                reason="ACTUAL_MARGIN_REQUIREMENT_BREACH",
            )
    if actual_risk > risk_budget:
        return ConfirmedPositionRisk(
            confirmed_position_quantity=confirmed_quantity,
            average_entry_price=average_entry,
            actual_notional_usdt=actual_notional,
            actual_required_margin_usdt=actual_required_margin,
            actual_stop_risk=actual_risk,
            pending_entries_blocked=True,
            hard_halted=False,
            reason="ACTUAL_STOP_RISK_BREACH",
        )
    return ConfirmedPositionRisk(
        confirmed_position_quantity=confirmed_quantity,
        average_entry_price=average_entry,
        actual_notional_usdt=actual_notional,
        actual_required_margin_usdt=actual_required_margin,
        actual_stop_risk=actual_risk,
        pending_entries_blocked=False,
        hard_halted=False,
        reason=None,
    )
