"""Durable local intent/outbox ledger for simulator-only submission scenarios."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.decimal_math import ZERO
from app.persistence.circuit_breaker import EntryIntentAuthorizationGate, PersistenceCircuitBreaker
from app.persistence.models import DurableOrderIntent
from app.simulation.models import OrderRole, SimulatedOrderIntent


class DurableIntentStatus(StrEnum):
    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    UNKNOWN = "UNKNOWN"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    ABSENT = "ABSENT"
    REJECTED = "REJECTED"


_UNRESOLVED_STATUSES = frozenset(
    {
        DurableIntentStatus.PREPARED,
        DurableIntentStatus.SUBMITTING,
        DurableIntentStatus.UNKNOWN,
    }
)
_KNOWN_OUTCOME_STATUSES = frozenset(
    {
        DurableIntentStatus.NEW,
        DurableIntentStatus.PARTIALLY_FILLED,
        DurableIntentStatus.FILLED,
        DurableIntentStatus.CANCELLED,
        DurableIntentStatus.REJECTED,
    }
)


class IntentLedgerError(RuntimeError):
    pass


class UnresolvedEconomicAction(IntentLedgerError):
    pass


class IntentLifecycleError(IntentLedgerError):
    pass


class BoundedAbsenceEvidenceError(IntentLedgerError):
    pass


@dataclass(frozen=True, slots=True)
class BoundedAbsenceEvidence:
    """Multiple separated not-found observations tied to one immutable intent identity."""

    client_order_id: str
    economic_key: str
    first_not_found_at_ms: int
    last_not_found_at_ms: int
    not_found_observation_count: int

    minimum_observation_count: ClassVar[int] = 2
    minimum_observation_window_ms: ClassVar[int] = 1_000

    def __post_init__(self) -> None:
        if not self.client_order_id or not self.economic_key:
            raise ValueError("absence evidence must identify a client order and economic action")
        integer_values = (
            self.first_not_found_at_ms,
            self.last_not_found_at_ms,
            self.not_found_observation_count,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in integer_values
        ):
            raise ValueError("absence evidence observations must be non-negative integers")
        if self.last_not_found_at_ms < self.first_not_found_at_ms:
            raise ValueError("absence evidence time cannot move backward")

    @property
    def is_sufficient(self) -> bool:
        return (
            self.not_found_observation_count >= self.minimum_observation_count
            and self.last_not_found_at_ms - self.first_not_found_at_ms
            >= self.minimum_observation_window_ms
        )


@dataclass(frozen=True, slots=True)
class DurableIntentRecord:
    client_order_id: str
    economic_key: str
    attempt_number: int
    plan_id: str
    symbol: str
    role: OrderRole
    stage_index: int
    quantity: Decimal
    price: Decimal
    filled_quantity: Decimal
    status: DurableIntentStatus


class DurableIntentLedger:
    """No simulator submit is permitted until an intent has reached durable SUBMITTING state."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        persistence_breaker: PersistenceCircuitBreaker | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._persistence_breaker = (
            persistence_breaker if persistence_breaker is not None else PersistenceCircuitBreaker()
        )
        self._entry_gate = EntryIntentAuthorizationGate(self._persistence_breaker)

    @property
    def persistence_breaker(self) -> PersistenceCircuitBreaker:
        return self._persistence_breaker

    def reopen_after_restart(self) -> "DurableIntentLedger":
        """Return a fresh local ledger instance over the same durable store."""
        return DurableIntentLedger(self._session_factory)

    def prepare(self, intent: SimulatedOrderIntent) -> DurableIntentRecord:
        if intent.role is OrderRole.ENTRY:
            self._entry_gate.authorize_new_entry_intent()
        try:
            with self._session_factory.begin() as session:
                latest = self._latest_for_economic_key(session, intent.economic_key)
                if (
                    latest is not None
                    and DurableIntentStatus(latest.status) is not DurableIntentStatus.ABSENT
                ):
                    raise UnresolvedEconomicAction(
                        "an existing economic action must be reconciled before another attempt"
                    )
                attempt_number = 1 if latest is None else latest.attempt_number + 1
                duplicate_client = session.scalar(
                    select(DurableOrderIntent).where(
                        DurableOrderIntent.client_order_id == intent.client_order_id
                    )
                )
                if duplicate_client is not None:
                    raise UnresolvedEconomicAction("client order ID already has a durable intent")
                record = DurableOrderIntent(
                    economic_key=intent.economic_key,
                    attempt_number=attempt_number,
                    client_order_id=intent.client_order_id,
                    plan_id=intent.plan_id,
                    symbol=intent.symbol,
                    direction=intent.direction.value,
                    role=intent.role.value,
                    stage_index=intent.stage_index,
                    quantity=format(intent.quantity, "f"),
                    price=format(intent.price, "f"),
                    filled_quantity=format(ZERO, "f"),
                    status=DurableIntentStatus.PREPARED.value,
                )
                session.add(record)
                session.flush()
                return self._to_record(record)
        except UnresolvedEconomicAction:
            raise
        except IntegrityError as error:
            raise UnresolvedEconomicAction(
                "concurrent economic action already created a durable intent"
            ) from error
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    def mark_submitting(self, client_order_id: str) -> DurableIntentRecord:
        return self._transition_status(
            client_order_id,
            expected_statuses=frozenset({DurableIntentStatus.PREPARED}),
            target_status=DurableIntentStatus.SUBMITTING,
        )

    def mark_unknown(self, client_order_id: str) -> DurableIntentRecord:
        return self._transition_status(
            client_order_id,
            expected_statuses=frozenset({DurableIntentStatus.SUBMITTING}),
            target_status=DurableIntentStatus.UNKNOWN,
        )

    def record_exchange_outcome(
        self,
        client_order_id: str,
        *,
        status: DurableIntentStatus,
        filled_quantity: Decimal,
    ) -> DurableIntentRecord:
        if status not in _KNOWN_OUTCOME_STATUSES:
            raise ValueError("only a known exchange outcome can resolve a durable intent")
        if not isinstance(filled_quantity, Decimal) or not filled_quantity.is_finite():
            raise ValueError("filled quantity must be a finite Decimal")
        try:
            with self._session_factory.begin() as session:
                record = self._record_for_update(session, client_order_id)
                current_status = DurableIntentStatus(record.status)
                if current_status not in {
                    DurableIntentStatus.SUBMITTING,
                    DurableIntentStatus.UNKNOWN,
                    DurableIntentStatus.NEW,
                    DurableIntentStatus.PARTIALLY_FILLED,
                }:
                    raise IntentLifecycleError("known outcome requires SUBMITTING or UNKNOWN state")
                if (
                    current_status
                    in {
                        DurableIntentStatus.NEW,
                        DurableIntentStatus.PARTIALLY_FILLED,
                    }
                    and status is not DurableIntentStatus.CANCELLED
                ):
                    raise IntentLifecycleError(
                        "known active intents can only transition to CANCELLED"
                    )
                self._validate_filled_quantity(
                    quantity=Decimal(record.quantity),
                    filled_quantity=filled_quantity,
                    status=status,
                )
                record.status = status.value
                record.filled_quantity = format(filled_quantity, "f")
                record.updated_at = datetime.now(UTC)
                session.flush()
                return self._to_record(record)
        except IntentLifecycleError:
            raise
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    def resolve_unknown_as_absent(
        self,
        client_order_id: str,
        evidence: BoundedAbsenceEvidence,
    ) -> DurableIntentRecord:
        if not isinstance(evidence, BoundedAbsenceEvidence):
            raise TypeError("evidence must be BoundedAbsenceEvidence")
        if not evidence.is_sufficient:
            raise BoundedAbsenceEvidenceError("absence evidence is not yet bounded")
        try:
            with self._session_factory.begin() as session:
                record = self._record_for_update(session, client_order_id)
                if DurableIntentStatus(record.status) is not DurableIntentStatus.UNKNOWN:
                    raise IntentLifecycleError("only UNKNOWN intents can resolve as absent")
                if (
                    record.client_order_id != evidence.client_order_id
                    or record.economic_key != evidence.economic_key
                ):
                    raise BoundedAbsenceEvidenceError(
                        "absence evidence does not match the durable intent identity"
                    )
                record.status = DurableIntentStatus.ABSENT.value
                record.updated_at = datetime.now(UTC)
                session.flush()
                return self._to_record(record)
        except (BoundedAbsenceEvidenceError, IntentLifecycleError):
            raise
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    def intent(self, client_order_id: str) -> DurableIntentRecord:
        with self._session_factory() as session:
            record = session.scalar(
                select(DurableOrderIntent).where(
                    DurableOrderIntent.client_order_id == client_order_id
                )
            )
            if record is None:
                raise KeyError(f"no durable intent for {client_order_id}")
            return self._to_record(record)

    def list_intents(self) -> tuple[DurableIntentRecord, ...]:
        with self._session_factory() as session:
            records = session.scalars(
                select(DurableOrderIntent).order_by(DurableOrderIntent.id.asc())
            )
            return tuple(self._to_record(record) for record in records)

    def unresolved_client_order_ids(self) -> tuple[str, ...]:
        return tuple(
            record.client_order_id
            for record in self.list_intents()
            if record.status in _UNRESOLVED_STATUSES
        )

    def unresolved_counts(self) -> dict[DurableIntentStatus, int]:
        records = self.list_intents()
        return {
            status: sum(record.status is status for record in records)
            for status in (
                DurableIntentStatus.PREPARED,
                DurableIntentStatus.SUBMITTING,
                DurableIntentStatus.UNKNOWN,
            )
            if any(record.status is status for record in records)
        }

    def _transition_status(
        self,
        client_order_id: str,
        *,
        expected_statuses: frozenset[DurableIntentStatus],
        target_status: DurableIntentStatus,
    ) -> DurableIntentRecord:
        try:
            with self._session_factory.begin() as session:
                record = self._record_for_update(session, client_order_id)
                if DurableIntentStatus(record.status) not in expected_statuses:
                    raise IntentLifecycleError(
                        "durable intent is not in the required lifecycle state"
                    )
                record.status = target_status.value
                record.updated_at = datetime.now(UTC)
                session.flush()
                return self._to_record(record)
        except IntentLifecycleError:
            raise
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    @staticmethod
    def _validate_filled_quantity(
        *,
        quantity: Decimal,
        filled_quantity: Decimal,
        status: DurableIntentStatus,
    ) -> None:
        if filled_quantity < ZERO or filled_quantity > quantity:
            raise IntentLifecycleError("filled quantity must remain within planned quantity")
        if status is DurableIntentStatus.PARTIALLY_FILLED:
            if filled_quantity <= ZERO or filled_quantity >= quantity:
                raise IntentLifecycleError("PARTIALLY_FILLED requires a strict partial quantity")
        elif status is DurableIntentStatus.FILLED and filled_quantity != quantity:
            raise IntentLifecycleError("FILLED requires the full planned quantity")
        elif (
            status in {DurableIntentStatus.NEW, DurableIntentStatus.REJECTED}
            and filled_quantity != ZERO
        ):
            raise IntentLifecycleError(f"{status.value} must not carry a filled quantity")

    @staticmethod
    def _to_record(record: DurableOrderIntent) -> DurableIntentRecord:
        return DurableIntentRecord(
            client_order_id=record.client_order_id,
            economic_key=record.economic_key,
            attempt_number=record.attempt_number,
            plan_id=record.plan_id,
            symbol=record.symbol,
            role=OrderRole(record.role),
            stage_index=record.stage_index,
            quantity=Decimal(record.quantity),
            price=Decimal(record.price),
            filled_quantity=Decimal(record.filled_quantity),
            status=DurableIntentStatus(record.status),
        )

    @staticmethod
    def _record_for_update(session: Session, client_order_id: str) -> DurableOrderIntent:
        statement = select(DurableOrderIntent).where(
            DurableOrderIntent.client_order_id == client_order_id
        )
        if session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
        record = session.scalar(statement)
        if record is None:
            raise IntentLifecycleError("durable intent was not found")
        return record

    @staticmethod
    def _latest_for_economic_key(session: Session, economic_key: str) -> DurableOrderIntent | None:
        statement = (
            select(DurableOrderIntent)
            .where(DurableOrderIntent.economic_key == economic_key)
            .order_by(DurableOrderIntent.attempt_number.desc())
            .limit(1)
        )
        if session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
        return session.scalar(statement)
