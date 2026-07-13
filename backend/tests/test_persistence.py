from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.domain.types import TradePlanState
from app.exchange.contracts import (
    LocalReconciliationState,
    ReconciliationSnapshot,
    reconcile_local_state,
)
from app.persistence.audit import AuditRepository, verify_hash_chain
from app.persistence.circuit_breaker import (
    PersistenceCircuitBreaker,
    PersistenceRecoveryEvidence,
    PersistenceUnavailable,
)
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.persistence.models import AppendOnlyViolation, AuditEvent
from app.persistence.replay import ReplayRunner, reconcile_projection


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    database_path = tmp_path / "phase04.sqlite"
    engine = create_database_engine(f"sqlite:///{database_path}")
    create_schema(engine)
    return create_session_factory(engine)


def _record_candidate(repository: AuditRepository, *, event_id: str = "evt-1") -> None:
    repository.record_delivery(
        event_id=event_id,
        source="strategy",
        event_type="state_transition",
        occurred_at=datetime(2026, 7, 10, tzinfo=UTC),
        payload={
            "plan_id": "plan-1",
            "from_state": "DRAFT",
            "to_state": "CANDIDATE",
            "plan_version": 1,
            "source_sequence": 1,
            "transition_evidence": {
                "risk_permits_entry": False,
                "stop_confirmed": False,
                "reduction_only": False,
            },
        },
    )


def test_audit_chain_is_append_only_and_duplicates_are_state_idempotent(
    session_factory: sessionmaker[Session],
) -> None:
    repository = AuditRepository(session_factory)
    _record_candidate(repository)
    duplicate = repository.record_delivery(
        event_id="evt-1",
        source="strategy",
        event_type="state_transition",
        occurred_at=datetime(2026, 7, 10, tzinfo=UTC),
        payload={
            "plan_id": "plan-1",
            "from_state": "DRAFT",
            "to_state": "CANDIDATE",
            "plan_version": 1,
            "source_sequence": 1,
            "transition_evidence": {
                "risk_permits_entry": False,
                "stop_confirmed": False,
                "reduction_only": False,
            },
        },
    )
    events = repository.list_audit_events()

    assert duplicate.is_duplicate
    assert len(events) == 2
    assert repository.processed_event_count() == 1
    assert verify_hash_chain(events)

    with pytest.raises(AppendOnlyViolation):
        with session_factory.begin() as session:
            audit_event = session.get(AuditEvent, events[0].id)
            assert audit_event is not None
            audit_event.source = "mutated"


def test_restart_replay_restores_state_and_ignores_duplicate_event_ids(tmp_path: Path) -> None:
    database_path = tmp_path / "restart.sqlite"
    database_url = f"sqlite:///{database_path}"

    first_engine = create_database_engine(database_url)
    create_schema(first_engine)
    first_repository = AuditRepository(create_session_factory(first_engine))
    _record_candidate(first_repository, event_id="evt-restart")
    first_engine.dispose()

    second_engine = create_database_engine(database_url)
    second_repository = AuditRepository(create_session_factory(second_engine))
    events = second_repository.list_audit_events()
    replay = ReplayRunner().replay(events, second_repository.audit_chain_head())

    assert replay.is_valid
    assert replay.plan_states["plan-1"] is TradePlanState.CANDIDATE
    assert replay.duplicate_event_ids == ()
    second_engine.dispose()


def test_database_failure_blocks_new_entries_until_reconciliation() -> None:
    breaker = PersistenceCircuitBreaker()
    breaker.record_write_failure(RuntimeError("disk unavailable"))

    assert not breaker.new_entries_allowed
    with pytest.raises(PersistenceUnavailable, match="DATABASE_AUDIT_FAILURE"):
        breaker.require_new_entries_allowed()

    breaker.reset_after_verified_reconciliation(
        PersistenceRecoveryEvidence(
            durable_write_probe_succeeded=True,
            audit_chain_valid=True,
            replay_valid=True,
            reconciliation_outcome=reconcile_local_state(
                local=LocalReconciliationState(
                    positions_by_symbol={},
                    normal_order_client_ids=frozenset(),
                    algo_order_client_ids=frozenset(),
                    required_stop_symbols=frozenset(),
                    unresolved_unknown_intent_ids=frozenset(),
                    audit_chain_valid=True,
                    replay_valid=True,
                ),
                snapshot=ReconciliationSnapshot(
                    positions_by_symbol={},
                    normal_order_client_ids=frozenset(),
                    algo_order_client_ids=frozenset(),
                ),
            ),
            unresolved_prepared_count=0,
            unresolved_submitting_count=0,
            unresolved_unknown_count=0,
        )
    )
    breaker.require_new_entries_allowed()


def test_audit_write_rejects_naive_timestamp(session_factory: sessionmaker[Session]) -> None:
    repository = AuditRepository(session_factory)

    with pytest.raises(ValueError, match="timezone-aware"):
        repository.record_delivery(
            event_id="evt-naive",
            source="strategy",
            event_type="state_transition",
            occurred_at=datetime(2026, 7, 10),
            payload={"plan_id": "plan-1", "to_state": "CANDIDATE"},
        )


def test_reconciliation_skeleton_halts_on_exchange_local_mismatch() -> None:
    result = reconcile_projection(
        local_state=TradePlanState.PLANNED,
        exchange_state=TradePlanState.PARTIALLY_FILLED,
    )

    assert not result.is_clean
    assert result.reason == "EXCHANGE_LOCAL_STATE_MISMATCH"
