"""Append-only delivery audit with idempotency and deterministic hash chaining."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.persistence.circuit_breaker import (
    PersistenceCircuitBreaker,
    PersistenceRecoveryEvidence,
)
from app.persistence.models import (
    AuditChainHead,
    AuditEvent,
    ProcessedEvent,
    ReconciliationRun,
    TradePlanProjection,
)
from app.persistence.reducer import (
    ProjectionState,
    StateTransitionEvent,
    TransitionReduction,
    parse_state_transition_payload,
    projection_state_from_value,
    reduce_state_transition,
)

if TYPE_CHECKING:
    from app.exchange.contracts import ReconciliationOutcome, ReconciliationSnapshot
    from app.simulation.intent_ledger import DurableIntentLedger

type AuditJSONValue = None | bool | int | str | list["AuditJSONValue"] | dict[str, "AuditJSONValue"]


class _UnspecifiedAuditHash:
    pass


_UNSPECIFIED_AUDIT_HASH = _UnspecifiedAuditHash()


class AuditPayloadValidationError(ValueError):
    """Raised before a transaction when audit evidence cannot be represented exactly."""


class AuditDeliveryStatus(StrEnum):
    CANONICAL = "CANONICAL"
    EXACT_DUPLICATE = "EXACT_DUPLICATE"
    SEMANTIC_CONFLICT = "SEMANTIC_CONFLICT"


def _normalize_audit_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise AuditPayloadValidationError("audit timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _utc_iso(value: datetime, *, allow_naive_utc: bool = False) -> str:
    if value.tzinfo is None:
        if not allow_naive_utc:
            raise AuditPayloadValidationError("audit timestamps must be timezone-aware")
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _normalize_audit_value(value: object, *, path: str) -> AuditJSONValue:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        raise AuditPayloadValidationError(f"{path} must not use binary floating point")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise AuditPayloadValidationError(f"{path} must be a finite Decimal")
        return format(value, "f")
    if isinstance(value, datetime):
        return _utc_iso(_normalize_audit_timestamp(value))
    if isinstance(value, Mapping):
        normalized_mapping: dict[str, AuditJSONValue] = {}
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise AuditPayloadValidationError(f"{path} has a non-string mapping key")
            normalized_mapping[key] = _normalize_audit_value(nested_value, path=f"{path}.{key}")
        return normalized_mapping
    if isinstance(value, (list, tuple)):
        return [
            _normalize_audit_value(nested_value, path=f"{path}[{index}]")
            for index, nested_value in enumerate(value)
        ]
    raise AuditPayloadValidationError(f"{path} cannot contain {type(value).__name__}")


def normalize_audit_payload(payload: Mapping[str, object]) -> dict[str, AuditJSONValue]:
    """Return a recursive JSON-safe payload without lossy financial coercion."""
    normalized = _normalize_audit_value(payload, path="payload")
    if not isinstance(normalized, dict):
        raise AuditPayloadValidationError("audit payload must be a mapping")
    return normalized


def _canonical_semantic_record(
    *,
    event_id: str,
    source: str,
    event_type: str,
    occurred_at: datetime,
    payload: Mapping[str, AuditJSONValue],
    allow_naive_utc: bool = False,
) -> str:
    return json.dumps(
        {
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": _utc_iso(occurred_at, allow_naive_utc=allow_naive_utc),
            "payload": payload,
            "source": source,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_record(
    *,
    event_id: str,
    source: str,
    event_type: str,
    occurred_at: datetime,
    payload: Mapping[str, AuditJSONValue],
    chain_sequence: int,
    delivery_status: AuditDeliveryStatus,
    semantic_fingerprint: str,
    allow_naive_utc: bool = False,
) -> str:
    """Canonical evidence for a chain link, including replay-relevant metadata."""
    return json.dumps(
        {
            "chain_sequence": chain_sequence,
            "delivery_status": delivery_status.value,
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": _utc_iso(occurred_at, allow_naive_utc=allow_naive_utc),
            "payload": payload,
            "semantic_fingerprint": semantic_fingerprint,
            "source": source,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class AuditReceipt:
    audit_id: int
    event_id: str
    is_duplicate: bool
    record_hash: str
    delivery_status: AuditDeliveryStatus
    projection_mutated: bool
    reconciliation_required: bool


@dataclass(frozen=True, slots=True)
class AuditChainHeadSnapshot:
    event_count: int
    last_sequence: int
    last_record_hash: str | None


class AuditRepository:
    """Persists every delivery but applies a state change only once per event ID."""

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

    def record_delivery(
        self,
        *,
        event_id: str,
        source: str,
        event_type: str,
        occurred_at: datetime,
        payload: Mapping[str, object],
    ) -> AuditReceipt:
        if not event_id or not source or not event_type:
            raise ValueError("event_id, source, and event_type are required")

        normalized_occurred_at = _normalize_audit_timestamp(occurred_at)
        normalized_payload = normalize_audit_payload(payload)
        try:
            with self._session_factory.begin() as session:
                head = self._chain_head_for_update(session)
                semantic_fingerprint = self._semantic_fingerprint(
                    event_id=event_id,
                    source=source,
                    event_type=event_type,
                    occurred_at=normalized_occurred_at,
                    payload=normalized_payload,
                )
                delivery_status = self._delivery_status(
                    session,
                    event_id=event_id,
                    source=source,
                    semantic_fingerprint=semantic_fingerprint,
                )
                chain_sequence = head.last_sequence + 1
                canonical = _canonical_record(
                    event_id=event_id,
                    source=source,
                    event_type=event_type,
                    occurred_at=normalized_occurred_at,
                    payload=normalized_payload,
                    chain_sequence=chain_sequence,
                    delivery_status=delivery_status,
                    semantic_fingerprint=semantic_fingerprint,
                )
                record_hash = hashlib.sha256(
                    f"{head.last_record_hash or ''}{canonical}".encode()
                ).hexdigest()
                # A malformed canonical transition must roll back its claim. Exact duplicates and
                # semantic conflicts are evidence, not a request to re-run the reducer.
                reduction = (
                    self._reduce_projection(
                        session,
                        self._parse_transition_event(event_type, normalized_payload),
                    )
                    if delivery_status is AuditDeliveryStatus.CANONICAL
                    else None
                )
                audit_event = AuditEvent(
                    event_id=event_id,
                    source=source,
                    event_type=event_type,
                    occurred_at=normalized_occurred_at,
                    payload=cast(dict[str, object], normalized_payload),
                    chain_sequence=chain_sequence,
                    semantic_fingerprint=semantic_fingerprint,
                    delivery_status=delivery_status.value,
                    previous_hash=head.last_record_hash,
                    record_hash=record_hash,
                )
                session.add(audit_event)
                session.flush()

                projection_mutated = False
                if delivery_status is AuditDeliveryStatus.CANONICAL and reduction is not None:
                    projection_mutated = self._apply_projection(
                        session,
                        event_id=event_id,
                        reduction=reduction,
                    )
                reconciliation_required = delivery_status is AuditDeliveryStatus.SEMANTIC_CONFLICT
                if reconciliation_required:
                    session.add(
                        ReconciliationRun(
                            run_id=f"audit-conflict-{record_hash}",
                            status="RECONCILIATION_REQUIRED",
                            reason="DUPLICATE_SEMANTIC_CONFLICT",
                        )
                    )

                head.event_count += 1
                head.last_sequence = audit_event.chain_sequence
                head.last_record_hash = record_hash
                head.updated_at = datetime.now(UTC)
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

        return AuditReceipt(
            audit_id=audit_event.id,
            event_id=event_id,
            is_duplicate=delivery_status is not AuditDeliveryStatus.CANONICAL,
            record_hash=record_hash,
            delivery_status=delivery_status,
            projection_mutated=projection_mutated,
            reconciliation_required=reconciliation_required,
        )

    def list_audit_events(self) -> tuple[AuditEvent, ...]:
        with self._session_factory() as session:
            statement = select(AuditEvent).order_by(AuditEvent.chain_sequence.asc())
            return tuple(session.scalars(statement))

    def processed_event_count(self) -> int:
        with self._session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(ProcessedEvent)) or 0)

    def audit_chain_head(self) -> AuditChainHeadSnapshot:
        with self._session_factory() as session:
            head = session.get(AuditChainHead, 1)
            if head is None:
                return AuditChainHeadSnapshot(event_count=0, last_sequence=0, last_record_hash=None)
            return AuditChainHeadSnapshot(
                event_count=head.event_count,
                last_sequence=head.last_sequence,
                last_record_hash=head.last_record_hash,
            )

    def projected_state(self, plan_id: str) -> str | None:
        with self._session_factory() as session:
            projection = session.get(TradePlanProjection, plan_id)
            return projection.state if projection is not None else None

    def projected_states(self) -> dict[str, str]:
        with self._session_factory() as session:
            projections = session.scalars(select(TradePlanProjection))
            return {projection.plan_id: projection.state for projection in projections}

    def derive_reconciliation_outcome(
        self,
        *,
        intent_ledger: "DurableIntentLedger",
        reconciliation_snapshot: "ReconciliationSnapshot",
    ) -> "ReconciliationOutcome":
        """Build reconciliation from durable facts, not caller-provided health booleans."""
        from app.exchange.contracts import (
            LocalReconciliationState,
            ReconciliationOutcome,
            ReconciliationSnapshot,
            reconcile_local_state,
        )
        from app.persistence.replay import ReplayRunner
        from app.simulation.intent_ledger import DurableIntentLedger

        if not isinstance(intent_ledger, DurableIntentLedger):
            raise TypeError("intent_ledger must be a durable intent ledger")
        if not isinstance(reconciliation_snapshot, ReconciliationSnapshot):
            raise TypeError("reconciliation_snapshot must be a typed exchange observation")
        events = self.list_audit_events()
        head = self.audit_chain_head()
        replay = ReplayRunner().replay(events, head)
        audit_chain_valid = verify_hash_chain(
            events,
            expected_count=head.event_count,
            expected_last_sequence=head.last_sequence,
            expected_last_record_hash=head.last_record_hash,
        )
        reconciliation_facts = intent_ledger.reconciliation_facts()
        outcome = reconcile_local_state(
            local=LocalReconciliationState(
                positions_by_symbol=reconciliation_facts.positions_by_symbol,
                normal_order_client_ids=reconciliation_facts.normal_order_client_ids,
                algo_order_client_ids=reconciliation_facts.algo_order_client_ids,
                required_stop_symbols=reconciliation_facts.required_stop_symbols,
                unresolved_unknown_intent_ids=(reconciliation_facts.unresolved_unknown_intent_ids),
                audit_chain_valid=audit_chain_valid,
                replay_valid=replay.is_valid,
                expected_stop_contracts=reconciliation_facts.expected_stop_contracts,
            ),
            snapshot=reconciliation_snapshot,
        )
        if not isinstance(outcome, ReconciliationOutcome):
            raise RuntimeError("reconciliation derivation did not return a typed outcome")
        return outcome

    def collect_persistence_recovery_evidence(
        self,
        *,
        intent_ledger: "DurableIntentLedger",
        reconciliation_snapshot: "ReconciliationSnapshot",
    ) -> PersistenceRecoveryEvidence:
        """Derive reset evidence from committed storage and typed observed records."""
        from app.exchange.contracts import (
            LocalReconciliationState,
            ReconciliationSnapshot,
            reconcile_local_state,
        )
        from app.persistence.replay import ReplayRunner
        from app.simulation.intent_ledger import DurableIntentLedger

        if not isinstance(intent_ledger, DurableIntentLedger):
            raise TypeError("intent_ledger must be a durable intent ledger")
        if not isinstance(reconciliation_snapshot, ReconciliationSnapshot):
            raise TypeError("reconciliation_snapshot must be a typed exchange observation")

        probe_event_id = f"persistence-recovery-probe-{uuid4().hex}"
        self.record_delivery(
            event_id=probe_event_id,
            source="persistence",
            event_type="persistence_recovery_probe",
            occurred_at=datetime.now(UTC),
            payload={"probe_id": probe_event_id},
        )
        events = self.list_audit_events()
        head = self.audit_chain_head()
        replay = ReplayRunner().replay(events, head)
        replayed_states = {plan_id: state.value for plan_id, state in replay.plan_states.items()}
        audit_chain_valid = verify_hash_chain(
            events,
            expected_count=head.event_count,
            expected_last_sequence=head.last_sequence,
            expected_last_record_hash=head.last_record_hash,
        )
        reconciliation_facts = intent_ledger.reconciliation_facts()
        reconciliation_outcome = reconcile_local_state(
            local=LocalReconciliationState(
                positions_by_symbol=reconciliation_facts.positions_by_symbol,
                normal_order_client_ids=reconciliation_facts.normal_order_client_ids,
                algo_order_client_ids=reconciliation_facts.algo_order_client_ids,
                required_stop_symbols=reconciliation_facts.required_stop_symbols,
                unresolved_unknown_intent_ids=(reconciliation_facts.unresolved_unknown_intent_ids),
                audit_chain_valid=audit_chain_valid,
                replay_valid=replay.is_valid,
                expected_stop_contracts=reconciliation_facts.expected_stop_contracts,
            ),
            snapshot=reconciliation_snapshot,
        )
        return PersistenceRecoveryEvidence(
            write_probe_event_id=probe_event_id,
            audit_event_count=head.event_count,
            audit_last_sequence=head.last_sequence,
            audit_last_record_hash=head.last_record_hash,
            replay_valid=replay.is_valid,
            projection_matches_replay=replay.is_valid
            and self.projected_states() == replayed_states,
            reconciliation_outcome=reconciliation_outcome,
            unresolved_intent_ids=tuple(sorted(reconciliation_facts.unresolved_unknown_intent_ids)),
        )

    @staticmethod
    def _chain_head_for_update(session: Session) -> AuditChainHead:
        statement = select(AuditChainHead).where(AuditChainHead.chain_id == 1)
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "postgresql":
            statement = statement.with_for_update()
        head = session.scalar(statement)
        if head is not None:
            return head

        if dialect_name == "postgresql":
            session.execute(
                postgresql_insert(AuditChainHead)
                .values(
                    chain_id=1,
                    event_count=0,
                    last_sequence=0,
                    last_record_hash=None,
                )
                .on_conflict_do_nothing(index_elements=[AuditChainHead.chain_id])
            )
            locked_head = session.scalar(
                select(AuditChainHead).where(AuditChainHead.chain_id == 1).with_for_update()
            )
            if locked_head is None:
                raise RuntimeError("audit chain head could not be initialized")
            return locked_head

        head = AuditChainHead(
            chain_id=1,
            event_count=0,
            last_sequence=0,
            last_record_hash=None,
        )
        session.add(head)
        session.flush()
        return head

    @staticmethod
    def _semantic_fingerprint(
        *,
        event_id: str,
        source: str,
        event_type: str,
        occurred_at: datetime,
        payload: Mapping[str, AuditJSONValue],
    ) -> str:
        canonical = _canonical_semantic_record(
            event_id=event_id,
            source=source,
            event_type=event_type,
            occurred_at=occurred_at,
            payload=payload,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _claim_processed_event(
        session: Session,
        *,
        event_id: str,
        source: str,
        semantic_fingerprint: str,
    ) -> bool:
        values = {
            "event_id": event_id,
            "source": source,
            "semantic_fingerprint": semantic_fingerprint,
        }
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "postgresql":
            statement = (
                postgresql_insert(ProcessedEvent)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[ProcessedEvent.event_id])
                .returning(ProcessedEvent.event_id)
            )
        elif dialect_name == "sqlite":
            statement = (
                sqlite_insert(ProcessedEvent)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[ProcessedEvent.event_id])
                .returning(ProcessedEvent.event_id)
            )
        else:
            raise RuntimeError(f"atomic processed-event claim is unsupported for {dialect_name}")
        claimed_event_id = session.scalar(statement)
        return claimed_event_id is not None

    def _delivery_status(
        self,
        session: Session,
        *,
        event_id: str,
        source: str,
        semantic_fingerprint: str,
    ) -> AuditDeliveryStatus:
        if self._claim_processed_event(
            session,
            event_id=event_id,
            source=source,
            semantic_fingerprint=semantic_fingerprint,
        ):
            return AuditDeliveryStatus.CANONICAL
        processed_event = session.get(ProcessedEvent, event_id)
        if processed_event is None:
            raise RuntimeError("atomic processed-event claim did not leave a durable row")
        if semantic_fingerprint in {
            processed_event.semantic_fingerprint,
            processed_event.legacy_semantic_fingerprint,
        }:
            return AuditDeliveryStatus.EXACT_DUPLICATE
        return AuditDeliveryStatus.SEMANTIC_CONFLICT

    @staticmethod
    def _parse_transition_event(
        event_type: str,
        payload: Mapping[str, AuditJSONValue],
    ) -> StateTransitionEvent | None:
        if event_type != "state_transition":
            return None
        return parse_state_transition_payload(cast(Mapping[str, object], payload))

    @staticmethod
    def _reduce_projection(
        session: Session,
        event: StateTransitionEvent | None,
    ) -> TransitionReduction | None:
        if event is None:
            return None
        projection = session.get(TradePlanProjection, event.plan_id)
        current = (
            None
            if projection is None
            else ProjectionState(
                plan_id=projection.plan_id,
                state=projection_state_from_value(projection.state),
                plan_version=projection.plan_version,
                source_sequence=projection.source_sequence,
            )
        )
        return reduce_state_transition(current, event)

    @staticmethod
    def _apply_projection(
        session: Session,
        *,
        event_id: str,
        reduction: TransitionReduction,
    ) -> bool:
        if not reduction.mutated:
            return False
        projection_state = reduction.projection
        projection = session.get(TradePlanProjection, projection_state.plan_id)
        if projection is None:
            session.add(
                TradePlanProjection(
                    plan_id=projection_state.plan_id,
                    state=projection_state.state.value,
                    plan_version=projection_state.plan_version,
                    source_sequence=projection_state.source_sequence,
                    last_event_id=event_id,
                )
            )
            return True

        projection.state = projection_state.state.value
        projection.plan_version = projection_state.plan_version
        projection.source_sequence = projection_state.source_sequence
        projection.last_event_id = event_id
        projection.updated_at = datetime.now(UTC)
        return True


def verify_hash_chain(
    events: tuple[AuditEvent, ...],
    *,
    expected_count: int | None = None,
    expected_last_sequence: int | None = None,
    expected_last_record_hash: str | None | _UnspecifiedAuditHash = _UNSPECIFIED_AUDIT_HASH,
) -> bool:
    if expected_count is not None and len(events) != expected_count:
        return False
    previous_hash: str | None = None
    expected_sequence = 1
    for event in events:
        if event.chain_sequence != expected_sequence:
            return False
        try:
            normalized_payload = normalize_audit_payload(event.payload)
        except AuditPayloadValidationError:
            return False
        if event.payload != normalized_payload:
            return False
        expected_semantic_fingerprint = hashlib.sha256(
            _canonical_semantic_record(
                event_id=event.event_id,
                source=event.source,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                payload=normalized_payload,
                allow_naive_utc=True,
            ).encode()
        ).hexdigest()
        if event.semantic_fingerprint != expected_semantic_fingerprint:
            return False
        try:
            delivery_status = AuditDeliveryStatus(event.delivery_status)
        except ValueError:
            return False
        canonical = _canonical_record(
            event_id=event.event_id,
            source=event.source,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            payload=normalized_payload,
            chain_sequence=event.chain_sequence,
            delivery_status=delivery_status,
            semantic_fingerprint=event.semantic_fingerprint,
            allow_naive_utc=True,
        )
        expected_hash = hashlib.sha256(f"{previous_hash or ''}{canonical}".encode()).hexdigest()
        if event.previous_hash != previous_hash or event.record_hash != expected_hash:
            return False
        previous_hash = event.record_hash
        expected_sequence += 1
    actual_last_sequence = expected_sequence - 1
    if expected_last_sequence is not None and actual_last_sequence != expected_last_sequence:
        return False
    if isinstance(expected_last_record_hash, _UnspecifiedAuditHash):
        return True
    if not events:
        return expected_last_record_hash is None
    return isinstance(expected_last_record_hash, str) and previous_hash == expected_last_record_hash
