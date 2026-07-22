"""Deterministic local exchange behavior used to prove failure safety paths."""

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from app.domain.decimal_math import ZERO
from app.domain.types import Direction
from app.persistence.circuit_breaker import PersistenceUnavailable
from app.planning.fills import (
    ActualRiskPolicy,
    FillEvent,
    FillObservationSource,
    FillSide,
    PositionRiskAssessment,
)
from app.simulation.intent_ledger import (
    _SIMULATED_QUERY_WRITE_CAPABILITY,
    AbsenceEvidenceSource,
    BoundedAbsenceEvidence,
    DurableIntentLedger,
    DurableIntentRecord,
    DurableIntentStatus,
    EntryAdmissionDecision,
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


class EntryRiskBlocked(SimulatorError):
    code = "ACTUAL_ENTRY_RISK_BLOCKED"


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


class SimulatedUnknownRemoteState(StrEnum):
    """Injected rehearsal state; it is never exchange-confirmed evidence."""

    UNRESOLVED = "UNRESOLVED"
    ABSENT = "ABSENT"
    PRESENT = "PRESENT"


@dataclass(slots=True)
class SimulatedUnknownQueryPlan:
    states: deque[SimulatedUnknownRemoteState] = field(default_factory=deque)

    @classmethod
    def from_states(
        cls,
        states: Iterable[SimulatedUnknownRemoteState],
    ) -> "SimulatedUnknownQueryPlan":
        return cls(states=deque(states))

    def consume(self) -> SimulatedUnknownRemoteState:
        return self.states.popleft() if self.states else SimulatedUnknownRemoteState.UNRESOLVED


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
    unknown_query_plan: SimulatedUnknownQueryPlan = field(default_factory=SimulatedUnknownQueryPlan)
    intent_ledger: DurableIntentLedger | None = None
    actual_risk_policy: ActualRiskPolicy | None = None
    now_ms: int = 0
    _orders: dict[str, SimulatedOrder] = field(default_factory=dict)
    _economic_keys: dict[str, str] = field(default_factory=dict)
    _events: list[SimulatorEvent] = field(default_factory=list)
    _event_counter: int = 0
    _unknown_query_counter: int = 0
    _unknown_remote_states: dict[str, SimulatedUnknownRemoteState] = field(default_factory=dict)
    _actual_risk_by_plan: dict[str, PositionRiskAssessment] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.actual_risk_policy is None:
            return
        if self.intent_ledger is None:
            raise DurableIntentLedgerRequired("actual-risk policy requires a durable intent ledger")
        self.intent_ledger.register_actual_risk_policy(self.actual_risk_policy)

    @classmethod
    def reopen_after_restart(
        cls,
        *,
        intent_ledger: DurableIntentLedger,
        now_ms: int = 0,
        unknown_query_plan: SimulatedUnknownQueryPlan | None = None,
    ) -> "ExchangeSimulator":
        """Rebuild local simulator state solely from durable intent and risk evidence."""
        simulator = cls(
            intent_ledger=intent_ledger,
            now_ms=now_ms,
            unknown_query_plan=unknown_query_plan or SimulatedUnknownQueryPlan(),
        )
        for record in intent_ledger.list_intents():
            simulator._rehydrate_order(record)
        return simulator

    def submit(self, intent: SimulatedOrderIntent) -> SimulatedOrder:
        if self.intent_ledger is None:
            raise DurableIntentLedgerRequired(
                "simulated submission requires a durable intent ledger"
            )
        admission_decision = (
            self._authorize_entry_risk(intent) if intent.role is OrderRole.ENTRY else None
        )
        self.intent_ledger.prepare(intent, admission_decision=admission_decision)
        self.intent_ledger.mark_submitting(
            intent.client_order_id,
            submitted_at_ms=self.now_ms,
        )

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
            self._unknown_remote_states[intent.client_order_id] = self.unknown_query_plan.consume()
            self.intent_ledger.mark_unknown(intent.client_order_id, unknown_at_ms=self.now_ms)
            raise UnknownOrderOutcome("unknown exchange outcome must be reconciled before retry")

        fill_sequence = self.fill_plan.consume()
        fills_were_applied = fill_sequence is not None
        if fill_sequence is not None:
            self._apply_fill_sequence(order, fill_sequence)
        elif fault is SimulatedFault.PARTIAL_FILL:
            fills_were_applied = True
            self._apply_fill_sequence(
                order,
                (
                    SimulatedFill(
                        trade_id=f"{intent.client_order_id}-partial-fill",
                        last_quantity=intent.quantity / Decimal("2"),
                        cumulative_quantity=intent.quantity / Decimal("2"),
                        fill_price=intent.price,
                        fee=ZERO,
                        fee_asset="USDT",
                    ),
                ),
            )
        cancelled_order_ids = (
            self._enforce_actual_entry_risk(
                intent.plan_id,
                current_client_order_id=intent.client_order_id,
            )
            if intent.role is OrderRole.ENTRY
            else frozenset()
        )
        self.intent_ledger.record_exchange_outcome(
            intent.client_order_id,
            status=DurableIntentStatus(order.status.value),
            filled_quantity=order.filled_quantity,
        )
        if not fills_were_applied and intent.client_order_id not in cancelled_order_ids:
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
        evidence: BoundedAbsenceEvidence | None = None,
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

    def query_unknown_source(
        self,
        client_order_id: str,
        source: AbsenceEvidenceSource,
    ) -> None:
        if not isinstance(source, AbsenceEvidenceSource):
            raise TypeError("unknown query source must be typed")
        order = self._require_order(client_order_id)
        if order.status is not SimulatedOrderStatus.UNKNOWN:
            raise SimulatorError("only unknown orders can be queried")
        if self.intent_ledger is None:
            raise DurableIntentLedgerRequired(
                "simulated unknown queries require a durable intent ledger"
            )
        remote_state = self._unknown_remote_states.get(
            client_order_id,
            SimulatedUnknownRemoteState.UNRESOLVED,
        )
        if remote_state is SimulatedUnknownRemoteState.UNRESOLVED:
            raise SimulatorError("simulated remote state is unresolved; absence cannot be proven")
        self._unknown_query_counter += 1
        self.intent_ledger._record_simulated_query_observation(
            client_order_id,
            source=source,
            found=remote_state is SimulatedUnknownRemoteState.PRESENT,
            observed_at_ms=self.now_ms,
            query_reference=(
                f"sim-query:{client_order_id}:{source.value}:"
                f"{self.now_ms}:{self._unknown_query_counter}"
            ),
            capability=_SIMULATED_QUERY_WRITE_CAPABILITY,
        )

    def reconcile_unknown(
        self,
        client_order_id: str,
        *,
        status: SimulatedOrderStatus,
        filled_quantity: Decimal,
        fills: tuple[SimulatedFill, ...] = (),
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
        if status in {SimulatedOrderStatus.PARTIALLY_FILLED, SimulatedOrderStatus.FILLED}:
            if not fills:
                raise SimulatorError("filled UNKNOWN reconciliation requires durable fill facts")
            self._apply_fill_sequence(order, fills)
            if order.filled_quantity != filled_quantity or order.status is not status:
                raise SimulatorError(
                    "UNKNOWN reconciliation fills do not match the claimed outcome"
                )
        elif fills:
            raise SimulatorError("non-filled UNKNOWN reconciliation must not carry fill facts")
        self.intent_ledger.record_exchange_outcome(
            client_order_id,
            status=DurableIntentStatus(status.value),
            filled_quantity=filled_quantity,
        )
        order.status = status
        order.filled_quantity = filled_quantity
        if order.intent.role is OrderRole.ENTRY and order.filled_quantity > ZERO:
            cancelled_order_ids = self._enforce_actual_entry_risk(
                order.intent.plan_id,
                current_client_order_id=order.intent.client_order_id,
            )
            if order.intent.client_order_id in cancelled_order_ids:
                self.intent_ledger.record_exchange_outcome(
                    client_order_id,
                    status=DurableIntentStatus.CANCELLED,
                    filled_quantity=filled_quantity,
                )
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

    def actual_risk(self, plan_id: str) -> PositionRiskAssessment:
        policy = self._risk_policy_for(plan_id)
        if policy is None:
            raise SimulatorError("no actual-risk policy is configured for this plan")
        result = self._evaluate_actual_risk(policy)
        self._actual_risk_by_plan[plan_id] = result
        return result

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
        cumulative_quantity = order.filled_quantity
        for fill in fill_sequence:
            if fill.cumulative_quantity < cumulative_quantity:
                raise SimulatorError("simulated fill sequence cannot reduce cumulative quantity")
            if fill.cumulative_quantity == cumulative_quantity:
                self._persist_fill(order, fill)
                self._schedule_event(order, fill=fill, status=order.status)
                continue
            if fill.cumulative_quantity != cumulative_quantity + fill.last_quantity:
                raise SimulatorError("simulated fill sequence cumulative quantity is inconsistent")
            if fill.cumulative_quantity > order.intent.quantity:
                raise SimulatorError("simulated fill sequence exceeds planned quantity")
            cumulative_quantity = fill.cumulative_quantity
            self._persist_fill(order, fill)
            event_status = (
                SimulatedOrderStatus.FILLED
                if cumulative_quantity == order.intent.quantity
                else SimulatedOrderStatus.PARTIALLY_FILLED
            )
            self._schedule_event(order, fill=fill, status=event_status)
        order.filled_quantity = cumulative_quantity
        if order.status is not SimulatedOrderStatus.CANCELLED:
            order.status = (
                SimulatedOrderStatus.FILLED
                if cumulative_quantity == order.intent.quantity
                else SimulatedOrderStatus.PARTIALLY_FILLED
            )

    def _persist_fill(self, order: SimulatedOrder, fill: SimulatedFill) -> None:
        if self.intent_ledger is None:
            raise DurableIntentLedgerRequired("simulated fills require a durable intent ledger")
        self.intent_ledger.record_fill(
            FillEvent(
                account_id=order.intent.account_id,
                trade_id=fill.trade_id,
                client_order_id=order.intent.client_order_id,
                symbol=order.intent.symbol,
                side=(
                    FillSide.BUY
                    if (
                        order.intent.role is OrderRole.ENTRY
                        and order.intent.direction is Direction.LONG
                    )
                    or (
                        order.intent.role is not OrderRole.ENTRY
                        and order.intent.direction is Direction.SHORT
                    )
                    else FillSide.SELL
                ),
                last_quantity=fill.last_quantity,
                cumulative_quantity=fill.cumulative_quantity,
                fill_price=fill.fill_price,
                fee=fill.fee,
                fee_asset=fill.fee_asset,
                occurred_at=datetime.fromtimestamp(
                    (self.now_ms + fill.occurred_at_offset_ms) / 1_000,
                    tz=UTC,
                ),
                observation_source=FillObservationSource.SIMULATED_EXCHANGE,
                observation_reference=f"simulator:{fill.trade_id}",
            ),
            materialize_simulated_protection=order.intent.role is OrderRole.ENTRY,
        )

    def _authorize_entry_risk(self, intent: SimulatedOrderIntent) -> EntryAdmissionDecision:
        if self.intent_ledger is None:
            raise DurableIntentLedgerRequired(
                "simulated entry authorization requires a durable intent ledger"
            )
        try:
            return self.intent_ledger.admit_entry(intent)
        except PersistenceUnavailable as error:
            raise EntryRiskBlocked(str(error)) from error

    def _enforce_actual_entry_risk(
        self,
        plan_id: str,
        *,
        current_client_order_id: str,
    ) -> frozenset[str]:
        policy = self._risk_policy_for(plan_id)
        if policy is None:
            raise EntryRiskBlocked("RISK_POLICY_MISSING")
        result = self._evaluate_actual_risk(policy)
        self._actual_risk_by_plan[plan_id] = result
        if not result.pending_entries_blocked:
            return frozenset()
        cancelled_ids: set[str] = set()
        for order in self._orders.values():
            if order.intent.role is not OrderRole.ENTRY or order.status not in {
                SimulatedOrderStatus.NEW,
                SimulatedOrderStatus.PARTIALLY_FILLED,
            }:
                continue
            order.status = SimulatedOrderStatus.CANCELLED
            cancelled_ids.add(order.intent.client_order_id)
            if order.intent.client_order_id != current_client_order_id:
                if self.intent_ledger is None:
                    raise DurableIntentLedgerRequired(
                        "actual-risk cancellation requires a durable intent ledger"
                    )
                self.intent_ledger.record_exchange_outcome(
                    order.intent.client_order_id,
                    status=DurableIntentStatus.CANCELLED,
                    filled_quantity=order.filled_quantity,
                )
            self._schedule_event(order, include_fill=False)
        return frozenset(cancelled_ids)

    def _evaluate_actual_risk(self, policy: ActualRiskPolicy) -> PositionRiskAssessment:
        if self.intent_ledger is None:
            raise DurableIntentLedgerRequired(
                "actual-risk evaluation requires a durable intent ledger"
            )
        return self.intent_ledger.actual_risk_state(policy.plan_id)

    def _risk_policy_for(self, plan_id: str) -> ActualRiskPolicy | None:
        if self.actual_risk_policy is not None and self.actual_risk_policy.plan_id != plan_id:
            return None
        if self.intent_ledger is None:
            return None
        return self.intent_ledger.actual_risk_policy(plan_id)

    def _materialize_protection(self, plan_id: str) -> None:
        if self.intent_ledger is None:
            raise DurableIntentLedgerRequired(
                "simulated protection requires a durable intent ledger"
            )
        policy = self._risk_policy_for(plan_id)
        if policy is None:
            raise EntryRiskBlocked("RISK_POLICY_MISSING")
        fills = self.intent_ledger.fill_ledger_for_plan(plan_id)
        if fills.filled_quantity <= ZERO:
            return
        self.intent_ledger.record_simulated_protection(
            plan_id,
            protected_position_quantity=fills.filled_quantity,
        )

    def _rehydrate_order(self, record: DurableIntentRecord) -> None:
        status_by_durable = {
            DurableIntentStatus.PREPARED: SimulatedOrderStatus.UNKNOWN,
            DurableIntentStatus.SUBMITTING: SimulatedOrderStatus.UNKNOWN,
            DurableIntentStatus.UNKNOWN: SimulatedOrderStatus.UNKNOWN,
            DurableIntentStatus.NEW: SimulatedOrderStatus.NEW,
            DurableIntentStatus.PARTIALLY_FILLED: SimulatedOrderStatus.PARTIALLY_FILLED,
            DurableIntentStatus.CANCEL_REQUIRED: SimulatedOrderStatus.CANCELLED,
            DurableIntentStatus.FILLED: SimulatedOrderStatus.FILLED,
            DurableIntentStatus.CANCELLED: SimulatedOrderStatus.CANCELLED,
            DurableIntentStatus.REJECTED: SimulatedOrderStatus.REJECTED,
            DurableIntentStatus.ABSENT: SimulatedOrderStatus.CANCELLED,
        }
        intent = SimulatedOrderIntent(
            client_order_id=record.client_order_id,
            plan_id=record.plan_id,
            symbol=record.symbol,
            direction=record.direction,
            role=record.role,
            stage_index=record.stage_index,
            quantity=record.quantity,
            price=record.price,
        )
        self._orders[record.client_order_id] = SimulatedOrder(
            intent=intent,
            status=status_by_durable[record.status],
            filled_quantity=record.filled_quantity,
        )
        self._economic_keys[record.economic_key] = record.client_order_id
        if record.status in {
            DurableIntentStatus.PREPARED,
            DurableIntentStatus.SUBMITTING,
            DurableIntentStatus.UNKNOWN,
        }:
            self._unknown_remote_states[record.client_order_id] = self.unknown_query_plan.consume()

    def _require_order(self, client_order_id: str) -> SimulatedOrder:
        order = self._orders.get(client_order_id)
        if order is None:
            raise SimulatorError("unknown client order ID")
        return order
