"""Deterministic local exchange behavior used to prove failure safety paths."""

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from app.domain.decimal_math import ZERO
from app.domain.types import Direction
from app.simulation.intent_ledger import (
    BoundedAbsenceEvidence,
    DurableIntentLedger,
    DurableIntentStatus,
)
from app.simulation.models import (
    OrderRole,
    SimulatedFault,
    SimulatedFill,
    SimulatedOrder,
    SimulatedOrderIntent,
    SimulatedOrderStatus,
    SimulatorEvent,
)

_BPS_DENOMINATOR = Decimal("10000")


class SimulatorError(RuntimeError):
    code = "SIMULATOR_ERROR"


class UnknownOrderOutcome(SimulatorError):
    code = "ORDER_STATUS_UNKNOWN"


class DurableIntentLedgerRequired(SimulatorError):
    code = "DURABLE_INTENT_LEDGER_REQUIRED"


class InjectedExchangeError(SimulatorError):
    def __init__(self, fault: SimulatedFault) -> None:
        self.fault = fault
        super().__init__(fault.value)


@dataclass(slots=True)
class FaultPlan:
    faults: deque[SimulatedFault] = field(default_factory=deque)

    @classmethod
    def from_faults(cls, faults: Iterable[SimulatedFault]) -> "FaultPlan":
        return cls(faults=deque(faults))

    def consume(self) -> SimulatedFault | None:
        return self.faults.popleft() if self.faults else None


@dataclass(slots=True)
class FillSequencePlan:
    sequences: deque[tuple[SimulatedFill, ...]] = field(default_factory=deque)

    @classmethod
    def from_sequences(
        cls,
        sequences: Iterable[tuple[SimulatedFill, ...]],
    ) -> "FillSequencePlan":
        return cls(sequences=deque(sequences))

    def consume(self) -> tuple[SimulatedFill, ...] | None:
        return self.sequences.popleft() if self.sequences else None


@dataclass(frozen=True, slots=True)
class SpreadSlippageModel:
    bid_price: Decimal
    ask_price: Decimal
    extra_slippage_bps: Decimal = ZERO

    def __post_init__(self) -> None:
        if (
            self.bid_price <= ZERO
            or self.ask_price < self.bid_price
            or self.extra_slippage_bps < ZERO
        ):
            raise ValueError("spread and slippage inputs are invalid")

    @property
    def midpoint(self) -> Decimal:
        return (self.bid_price + self.ask_price) / Decimal("2")

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask_price - self.bid_price) * _BPS_DENOMINATOR / self.midpoint

    def execution_price(self, direction: Direction) -> Decimal:
        slippage_rate = self.extra_slippage_bps / _BPS_DENOMINATOR
        base_price = self.ask_price if direction is Direction.LONG else self.bid_price
        return (
            base_price * (Decimal("1") + slippage_rate)
            if direction is Direction.LONG
            else base_price * (Decimal("1") - slippage_rate)
        )


@dataclass(slots=True)
class ExchangeSimulator:
    fault_plan: FaultPlan = field(default_factory=FaultPlan)
    fill_plan: FillSequencePlan = field(default_factory=FillSequencePlan)
    intent_ledger: DurableIntentLedger | None = None
    now_ms: int = 0
    _orders: dict[str, SimulatedOrder] = field(default_factory=dict)
    _economic_keys: dict[str, str] = field(default_factory=dict)
    _events: list[SimulatorEvent] = field(default_factory=list)
    _event_counter: int = 0

    def submit(self, intent: SimulatedOrderIntent) -> SimulatedOrder:
        if self.intent_ledger is None:
            raise DurableIntentLedgerRequired(
                "simulated submission requires a durable intent ledger"
            )
        self.intent_ledger.prepare(intent)
        self.intent_ledger.mark_submitting(intent.client_order_id)

        fault = self.fault_plan.consume()
        if fault in {
            SimulatedFault.RATE_LIMIT_429,
            SimulatedFault.IP_BAN_418,
            SimulatedFault.TIMESTAMP_1021,
            SimulatedFault.DISCONNECT,
        }:
            raise InjectedExchangeError(fault)
        if fault is SimulatedFault.REJECTED_STOP and intent.role is OrderRole.STOP:
            rejected = SimulatedOrder(intent=intent, status=SimulatedOrderStatus.REJECTED)
            self._orders[intent.client_order_id] = rejected
            self._economic_keys[intent.economic_key] = intent.client_order_id
            self.intent_ledger.record_exchange_outcome(
                intent.client_order_id,
                status=DurableIntentStatus.REJECTED,
                filled_quantity=ZERO,
            )
            raise InjectedExchangeError(fault)

        order = SimulatedOrder(intent=intent, status=SimulatedOrderStatus.NEW)
        self._orders[intent.client_order_id] = order
        self._economic_keys[intent.economic_key] = intent.client_order_id
        if fault is SimulatedFault.UNKNOWN_503:
            order.status = SimulatedOrderStatus.UNKNOWN
            self.intent_ledger.mark_unknown(intent.client_order_id)
            raise UnknownOrderOutcome("unknown exchange outcome must be reconciled before retry")

        fill_sequence = self.fill_plan.consume()
        if fill_sequence is not None:
            self._apply_fill_sequence(order, fill_sequence)
        elif fault is SimulatedFault.PARTIAL_FILL:
            order.status = SimulatedOrderStatus.PARTIALLY_FILLED
            order.filled_quantity = intent.quantity / Decimal("2")
        self.intent_ledger.record_exchange_outcome(
            intent.client_order_id,
            status=DurableIntentStatus(order.status.value),
            filled_quantity=order.filled_quantity,
        )
        if fill_sequence is None:
            self._schedule_event(
                order,
                delayed=fault is SimulatedFault.DELAYED_EVENT,
                duplicate=fault is SimulatedFault.DUPLICATE_EVENT,
            )
        return order

    def cancel(self, client_order_id: str) -> SimulatedOrder:
        order = self._require_order(client_order_id)
        if order.status not in {SimulatedOrderStatus.NEW, SimulatedOrderStatus.PARTIALLY_FILLED}:
            raise SimulatorError("only known active orders can be cancelled")
        if self.intent_ledger is None:
            raise DurableIntentLedgerRequired(
                "simulated cancellation requires a durable intent ledger"
            )
        self.intent_ledger.record_exchange_outcome(
            client_order_id,
            status=DurableIntentStatus.CANCELLED,
            filled_quantity=order.filled_quantity,
        )
        order.status = SimulatedOrderStatus.CANCELLED
        self._schedule_event(order, include_fill=False)
        return order

    def resolve_unknown_as_absent(
        self,
        client_order_id: str,
        evidence: BoundedAbsenceEvidence,
    ) -> None:
        order = self._require_order(client_order_id)
        if order.status is not SimulatedOrderStatus.UNKNOWN:
            raise SimulatorError("only unknown orders can resolve as absent")
        if self.intent_ledger is None:
            raise DurableIntentLedgerRequired(
                "simulated absence resolution requires a durable intent ledger"
            )
        self.intent_ledger.resolve_unknown_as_absent(client_order_id, evidence)
        order.status = SimulatedOrderStatus.CANCELLED
        self._economic_keys.pop(order.intent.economic_key, None)
        self._schedule_event(order)

    def reconcile_unknown(
        self,
        client_order_id: str,
        *,
        status: SimulatedOrderStatus,
        filled_quantity: Decimal,
    ) -> None:
        if status not in {
            SimulatedOrderStatus.NEW,
            SimulatedOrderStatus.PARTIALLY_FILLED,
            SimulatedOrderStatus.FILLED,
            SimulatedOrderStatus.CANCELLED,
        }:
            raise SimulatorError("UNKNOWN can only reconcile to a known exchange status")
        order = self._require_order(client_order_id)
        if order.status is not SimulatedOrderStatus.UNKNOWN:
            raise SimulatorError("only unknown orders can be reconciled")
        if self.intent_ledger is None:
            raise DurableIntentLedgerRequired(
                "simulated reconciliation requires a durable intent ledger"
            )
        self.intent_ledger.record_exchange_outcome(
            client_order_id,
            status=DurableIntentStatus(status.value),
            filled_quantity=filled_quantity,
        )
        order.status = status
        order.filled_quantity = filled_quantity
        self._schedule_event(order)

    def advance_to(self, now_ms: int) -> tuple[SimulatorEvent, ...]:
        if now_ms < self.now_ms:
            raise ValueError("simulation clock cannot move backward")
        self.now_ms = now_ms
        due = tuple(
            sorted(
                (event for event in self._events if event.available_at_ms <= self.now_ms),
                key=lambda event: (event.available_at_ms, event.event_id),
            )
        )
        self._events = [event for event in self._events if event.available_at_ms > self.now_ms]
        return due

    def order(self, client_order_id: str) -> SimulatedOrder:
        return self._require_order(client_order_id)

    def _schedule_event(
        self,
        order: SimulatedOrder,
        *,
        delayed: bool = False,
        duplicate: bool = False,
        fill: SimulatedFill | None = None,
        status: SimulatedOrderStatus | None = None,
        include_fill: bool = True,
    ) -> None:
        self._event_counter += 1
        event_status = order.status if status is None else status
        is_fill = include_fill and order.filled_quantity > ZERO
        if fill is not None:
            event = SimulatorEvent(
                event_id=f"sim-{self._event_counter}",
                client_order_id=order.intent.client_order_id,
                status=event_status,
                filled_quantity=fill.cumulative_quantity,
                available_at_ms=self.now_ms + fill.delivery_delay_ms,
                trade_id=fill.trade_id,
                last_filled_quantity=fill.last_quantity,
                cumulative_filled_quantity=fill.cumulative_quantity,
                fill_price=fill.fill_price,
                fee=fill.fee,
                fee_asset=fill.fee_asset,
                occurred_at_ms=self.now_ms + fill.occurred_at_offset_ms,
            )
            self._events.append(event)
            if fill.duplicate_delivery:
                self._events.append(event)
            return
        event = SimulatorEvent(
            event_id=f"sim-{self._event_counter}",
            client_order_id=order.intent.client_order_id,
            status=event_status,
            filled_quantity=order.filled_quantity,
            available_at_ms=self.now_ms + (1_000 if delayed else 0),
            trade_id=(
                f"{order.intent.client_order_id}-trade-{self._event_counter}" if is_fill else None
            ),
            last_filled_quantity=order.filled_quantity if is_fill else ZERO,
            cumulative_filled_quantity=order.filled_quantity,
            fill_price=order.intent.price if is_fill else None,
            fee=ZERO,
            fee_asset="USDT" if is_fill else None,
            occurred_at_ms=self.now_ms,
        )
        self._events.append(event)
        if duplicate:
            self._events.append(event)

    def _apply_fill_sequence(
        self,
        order: SimulatedOrder,
        fill_sequence: tuple[SimulatedFill, ...],
    ) -> None:
        if not fill_sequence:
            return
        cumulative_quantity = ZERO
        for fill in fill_sequence:
            if fill.cumulative_quantity != cumulative_quantity + fill.last_quantity:
                raise SimulatorError("simulated fill sequence cumulative quantity is inconsistent")
            if fill.cumulative_quantity > order.intent.quantity:
                raise SimulatorError("simulated fill sequence exceeds planned quantity")
            cumulative_quantity = fill.cumulative_quantity
            event_status = (
                SimulatedOrderStatus.FILLED
                if cumulative_quantity == order.intent.quantity
                else SimulatedOrderStatus.PARTIALLY_FILLED
            )
            self._schedule_event(order, fill=fill, status=event_status)
        order.filled_quantity = cumulative_quantity
        order.status = (
            SimulatedOrderStatus.FILLED
            if cumulative_quantity == order.intent.quantity
            else SimulatedOrderStatus.PARTIALLY_FILLED
        )

    def _require_order(self, client_order_id: str) -> SimulatedOrder:
        order = self._orders.get(client_order_id)
        if order is None:
            raise SimulatorError("unknown client order ID")
        return order
