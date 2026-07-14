"""Durable local intent/outbox evidence for simulator-only submission scenarios."""

import hashlib
import json
from collections import defaultdict
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
from app.persistence.models import (
    DurableIntentAbsenceObservation,
    DurableIntentFill,
    DurableOrderIntent,
)
from app.planning.fills import FillEvent, FillLedger, FillLedgerError, FillLedgerReceipt
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


class AbsenceEvidenceSource(StrEnum):
    NORMAL_OPEN_ORDERS = "NORMAL_OPEN_ORDERS"
    ALGO_OPEN_ORDERS = "ALGO_OPEN_ORDERS"
    TRADE_HISTORY = "TRADE_HISTORY"
    USER_STREAM_WATERMARK = "USER_STREAM_WATERMARK"
    POSITION_SNAPSHOT = "POSITION_SNAPSHOT"


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
_TERMINAL_STATUSES = frozenset(
    {
        DurableIntentStatus.FILLED,
        DurableIntentStatus.CANCELLED,
        DurableIntentStatus.ABSENT,
        DurableIntentStatus.REJECTED,
    }
)
_REQUIRED_ABSENCE_SOURCES = frozenset(AbsenceEvidenceSource)


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
    """A summary derived from durable observations, never an authorization input."""

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
class UnknownIntentObservation:
    """One durable reconciliation fact from a required bounded-absence source."""

    source: AbsenceEvidenceSource
    observed_at_ms: int
    stream_watermark_ms: int
    found: bool

    def __post_init__(self) -> None:
        if not isinstance(self.source, AbsenceEvidenceSource):
            raise TypeError("absence observation source must be typed")
        integer_values = (self.observed_at_ms, self.stream_watermark_ms)
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in integer_values
        ):
            raise ValueError("absence observation timestamps must be non-negative integers")
        if self.stream_watermark_ms < self.observed_at_ms:
            raise ValueError("stream watermark cannot precede the observed snapshot")
        if not isinstance(self.found, bool):
            raise TypeError("absence observation found flag must be boolean")


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


@dataclass(frozen=True, slots=True)
class DurableAbsenceObservation:
    client_order_id: str
    economic_key: str
    source: AbsenceEvidenceSource
    observed_at_ms: int
    stream_watermark_ms: int
    found: bool


class DurableIntentLedger:
    """Durably gates simulator submissions and retains all uncertainty/fill evidence."""

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
        return DurableIntentLedger(
            self._session_factory, persistence_breaker=self._persistence_breaker
        )

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

    def record_fill(self, event: FillEvent) -> FillLedgerReceipt:
        """Persist one immutable trade fact before it can influence an outcome or risk gate."""
        try:
            with self._session_factory.begin() as session:
                intent = self._record_for_update(session, event.client_order_id)
                status = DurableIntentStatus(intent.status)
                if status in _TERMINAL_STATUSES:
                    raise IntentLifecycleError("terminal durable intents cannot receive new fills")
                existing_rows = tuple(
                    session.scalars(
                        select(DurableIntentFill).where(
                            DurableIntentFill.client_order_id == event.client_order_id
                        )
                    )
                )
                fill_ledger = FillLedger.from_events(
                    self._fill_event_from_row(row) for row in existing_rows
                )
                fingerprint = self._fill_fingerprint(event)
                existing = session.scalar(
                    select(DurableIntentFill).where(
                        DurableIntentFill.client_order_id == event.client_order_id,
                        DurableIntentFill.trade_id == event.trade_id,
                    )
                )
                if existing is not None:
                    if existing.semantic_fingerprint != fingerprint:
                        raise FillLedgerError(
                            "durable trade ID was redelivered with a semantic conflict"
                        )
                    return FillLedgerReceipt(
                        is_duplicate=True,
                        filled_quantity=fill_ledger.filled_quantity,
                        average_fill_price=fill_ledger.average_fill_price,
                        total_fee=fill_ledger.total_fee,
                    )
                receipt = fill_ledger.record(event)
                planned_quantity = Decimal(intent.quantity)
                if receipt.filled_quantity > planned_quantity:
                    raise IntentLifecycleError("durable fills exceed the planned quantity")
                session.add(
                    DurableIntentFill(
                        client_order_id=event.client_order_id,
                        trade_id=event.trade_id,
                        semantic_fingerprint=fingerprint,
                        last_quantity=format(event.last_quantity, "f"),
                        cumulative_quantity=format(event.cumulative_quantity, "f"),
                        fill_price=format(event.fill_price, "f"),
                        fee=format(event.fee, "f"),
                        fee_asset=event.fee_asset,
                        occurred_at=event.occurred_at.astimezone(UTC),
                    )
                )
                target_status = (
                    DurableIntentStatus.FILLED
                    if receipt.filled_quantity == planned_quantity
                    else DurableIntentStatus.PARTIALLY_FILLED
                )
                self._assert_known_outcome_transition(
                    current=status,
                    target=target_status,
                    prior_filled_quantity=Decimal(intent.filled_quantity),
                    filled_quantity=receipt.filled_quantity,
                )
                intent.status = target_status.value
                intent.filled_quantity = format(receipt.filled_quantity, "f")
                intent.updated_at = datetime.now(UTC)
                session.flush()
                return receipt
        except (FillLedgerError, IntentLifecycleError):
            raise
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

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
                durable_fills = self._intent_fill_ledger(session, client_order_id)
                if (
                    status
                    in {
                        DurableIntentStatus.PARTIALLY_FILLED,
                        DurableIntentStatus.FILLED,
                    }
                    and durable_fills.filled_quantity <= ZERO
                ):
                    raise IntentLifecycleError("filled outcomes require durable fill facts")
                if filled_quantity != durable_fills.filled_quantity:
                    raise IntentLifecycleError(
                        "exchange outcome quantity must equal durable fill evidence"
                    )
                self._validate_filled_quantity(
                    quantity=Decimal(record.quantity),
                    filled_quantity=filled_quantity,
                    status=status,
                )
                self._assert_known_outcome_transition(
                    current=DurableIntentStatus(record.status),
                    target=status,
                    prior_filled_quantity=Decimal(record.filled_quantity),
                    filled_quantity=filled_quantity,
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

    def record_absence_observation(
        self,
        client_order_id: str,
        observation: UnknownIntentObservation,
    ) -> DurableAbsenceObservation:
        """Store one source-specific absence/presence observation for an UNKNOWN intent."""
        if not isinstance(observation, UnknownIntentObservation):
            raise TypeError("observation must be UnknownIntentObservation")
        try:
            with self._session_factory.begin() as session:
                record = self._record_for_update(session, client_order_id)
                if DurableIntentStatus(record.status) is not DurableIntentStatus.UNKNOWN:
                    raise IntentLifecycleError(
                        "absence observations require an UNKNOWN durable intent"
                    )
                row = DurableIntentAbsenceObservation(
                    client_order_id=record.client_order_id,
                    economic_key=record.economic_key,
                    source=observation.source.value,
                    observed_at_ms=observation.observed_at_ms,
                    stream_watermark_ms=observation.stream_watermark_ms,
                    found=observation.found,
                )
                session.add(row)
                session.flush()
                return self._to_absence_observation(row)
        except IntentLifecycleError:
            raise
        except IntegrityError as error:
            raise BoundedAbsenceEvidenceError(
                "duplicate durable absence observation is not additional evidence"
            ) from error
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    def bounded_absence_evidence(self, client_order_id: str) -> BoundedAbsenceEvidence:
        with self._session_factory() as session:
            record = self._record_for_update(session, client_order_id)
            if DurableIntentStatus(record.status) is not DurableIntentStatus.UNKNOWN:
                raise IntentLifecycleError("only UNKNOWN intents can have absence evidence")
            return self._derive_bounded_absence_evidence(session, record)

    def resolve_unknown_as_absent(
        self,
        client_order_id: str,
        evidence: BoundedAbsenceEvidence | None = None,
    ) -> DurableIntentRecord:
        """Resolve only from queryable, persisted multi-source evidence; caller data is rejected."""
        if evidence is not None:
            raise BoundedAbsenceEvidenceError(
                "caller-supplied absence summaries cannot authorize a retry"
            )
        try:
            with self._session_factory.begin() as session:
                record = self._record_for_update(session, client_order_id)
                if DurableIntentStatus(record.status) is not DurableIntentStatus.UNKNOWN:
                    raise IntentLifecycleError("only UNKNOWN intents can resolve as absent")
                self._derive_bounded_absence_evidence(session, record)
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

    def list_absence_observations(
        self,
        client_order_id: str,
    ) -> tuple[DurableAbsenceObservation, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(DurableIntentAbsenceObservation)
                .where(DurableIntentAbsenceObservation.client_order_id == client_order_id)
                .order_by(
                    DurableIntentAbsenceObservation.observed_at_ms.asc(),
                    DurableIntentAbsenceObservation.source.asc(),
                )
            )
            return tuple(self._to_absence_observation(row) for row in rows)

    def fill_ledger_for_intent(self, client_order_id: str) -> FillLedger:
        with self._session_factory() as session:
            return self._intent_fill_ledger(session, client_order_id)

    def fill_ledger_for_plan(self, plan_id: str) -> FillLedger:
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(DurableIntentFill)
                    .join(
                        DurableOrderIntent,
                        DurableOrderIntent.client_order_id == DurableIntentFill.client_order_id,
                    )
                    .where(
                        DurableOrderIntent.plan_id == plan_id,
                        DurableOrderIntent.role == OrderRole.ENTRY.value,
                    )
                    .order_by(DurableIntentFill.id.asc())
                )
            )
            return FillLedger.from_events(self._fill_event_from_row(row) for row in rows)

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
    def _assert_known_outcome_transition(
        *,
        current: DurableIntentStatus,
        target: DurableIntentStatus,
        prior_filled_quantity: Decimal,
        filled_quantity: Decimal,
    ) -> None:
        if filled_quantity < prior_filled_quantity:
            raise IntentLifecycleError("known outcomes cannot reduce durable filled quantity")
        if current in _TERMINAL_STATUSES:
            if current is target and prior_filled_quantity == filled_quantity:
                return
            raise IntentLifecycleError("terminal durable intent cannot change outcome")
        if current is DurableIntentStatus.PREPARED:
            raise IntentLifecycleError(
                "known outcomes require SUBMITTING or a reconciled active state"
            )
        if current in {DurableIntentStatus.SUBMITTING, DurableIntentStatus.UNKNOWN}:
            return
        if current is DurableIntentStatus.NEW:
            if target in {
                DurableIntentStatus.NEW,
                DurableIntentStatus.PARTIALLY_FILLED,
                DurableIntentStatus.FILLED,
                DurableIntentStatus.CANCELLED,
            }:
                return
        if current is DurableIntentStatus.PARTIALLY_FILLED:
            if target in {
                DurableIntentStatus.PARTIALLY_FILLED,
                DurableIntentStatus.FILLED,
                DurableIntentStatus.CANCELLED,
            }:
                return
        raise IntentLifecycleError("known outcome violates the durable intent lifecycle")

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
    def _to_absence_observation(
        row: DurableIntentAbsenceObservation,
    ) -> DurableAbsenceObservation:
        return DurableAbsenceObservation(
            client_order_id=row.client_order_id,
            economic_key=row.economic_key,
            source=AbsenceEvidenceSource(row.source),
            observed_at_ms=row.observed_at_ms,
            stream_watermark_ms=row.stream_watermark_ms,
            found=row.found,
        )

    @staticmethod
    def _fill_event_from_row(row: DurableIntentFill) -> FillEvent:
        occurred_at = row.occurred_at
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)
        else:
            occurred_at = occurred_at.astimezone(UTC)
        return FillEvent(
            trade_id=row.trade_id,
            client_order_id=row.client_order_id,
            last_quantity=Decimal(row.last_quantity),
            cumulative_quantity=Decimal(row.cumulative_quantity),
            fill_price=Decimal(row.fill_price),
            fee=Decimal(row.fee),
            fee_asset=row.fee_asset,
            occurred_at=occurred_at,
        )

    @staticmethod
    def _fill_fingerprint(event: FillEvent) -> str:
        canonical = json.dumps(
            {
                "client_order_id": event.client_order_id,
                "cumulative_quantity": format(event.cumulative_quantity, "f"),
                "fee": format(event.fee, "f"),
                "fee_asset": event.fee_asset,
                "fill_price": format(event.fill_price, "f"),
                "last_quantity": format(event.last_quantity, "f"),
                "occurred_at": event.occurred_at.astimezone(UTC).isoformat(),
                "trade_id": event.trade_id,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

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

    @staticmethod
    def _intent_fill_ledger(session: Session, client_order_id: str) -> FillLedger:
        rows = tuple(
            session.scalars(
                select(DurableIntentFill)
                .where(DurableIntentFill.client_order_id == client_order_id)
                .order_by(DurableIntentFill.id.asc())
            )
        )
        return FillLedger.from_events(DurableIntentLedger._fill_event_from_row(row) for row in rows)

    @staticmethod
    def _derive_bounded_absence_evidence(
        session: Session,
        record: DurableOrderIntent,
    ) -> BoundedAbsenceEvidence:
        rows = tuple(
            session.scalars(
                select(DurableIntentAbsenceObservation)
                .where(DurableIntentAbsenceObservation.client_order_id == record.client_order_id)
                .order_by(DurableIntentAbsenceObservation.observed_at_ms.asc())
            )
        )
        if not rows:
            raise BoundedAbsenceEvidenceError("no durable absence observations exist")
        if any(row.economic_key != record.economic_key for row in rows):
            raise BoundedAbsenceEvidenceError(
                "absence evidence does not match durable economic identity"
            )
        if any(row.found for row in rows):
            raise BoundedAbsenceEvidenceError("presence evidence prevents an absence resolution")
        observations_by_source: defaultdict[
            AbsenceEvidenceSource, list[DurableIntentAbsenceObservation]
        ] = defaultdict(list)
        for row in rows:
            try:
                source = AbsenceEvidenceSource(row.source)
            except ValueError as error:
                raise BoundedAbsenceEvidenceError(
                    "absence observation source is invalid"
                ) from error
            if row.stream_watermark_ms < row.observed_at_ms:
                raise BoundedAbsenceEvidenceError("durable stream watermark is invalid")
            observations_by_source[source].append(row)
        missing_sources = _REQUIRED_ABSENCE_SOURCES - set(observations_by_source)
        if missing_sources:
            raise BoundedAbsenceEvidenceError(
                "all bounded-absence observation sources are required"
            )
        for source in _REQUIRED_ABSENCE_SOURCES:
            source_rows = observations_by_source[source]
            timestamps = [row.observed_at_ms for row in source_rows]
            if len(timestamps) < BoundedAbsenceEvidence.minimum_observation_count:
                raise BoundedAbsenceEvidenceError("each absence source needs repeated observations")
            if (
                max(timestamps) - min(timestamps)
                < BoundedAbsenceEvidence.minimum_observation_window_ms
            ):
                raise BoundedAbsenceEvidenceError("absence source observation window is too short")
        timestamps = [row.observed_at_ms for row in rows]
        return BoundedAbsenceEvidence(
            client_order_id=record.client_order_id,
            economic_key=record.economic_key,
            first_not_found_at_ms=min(timestamps),
            last_not_found_at_ms=max(timestamps),
            not_found_observation_count=len(rows),
        )
