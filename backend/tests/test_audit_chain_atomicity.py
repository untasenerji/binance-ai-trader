from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.persistence.audit import AuditDeliveryStatus, AuditRepository, verify_hash_chain
from app.persistence.database import create_database_engine, create_schema, create_session_factory


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'atomic-audit.sqlite'}")
    create_schema(engine)
    return create_session_factory(engine)


def _candidate_payload() -> dict[str, object]:
    return {
        "plan_id": "plan-atomic",
        "from_state": "DRAFT",
        "to_state": "CANDIDATE",
        "plan_version": 1,
        "source_sequence": 1,
        "transition_evidence": {
            "risk_permits_entry": False,
            "stop_confirmed": False,
            "reduction_only": False,
        },
    }


def test_exact_duplicate_is_audit_visible_but_mutates_projection_only_once(
    session_factory: sessionmaker[Session],
) -> None:
    repository = AuditRepository(session_factory)
    occurred_at = datetime(2026, 7, 12, tzinfo=UTC)

    first = repository.record_delivery(
        event_id="evt-exact-duplicate",
        source="strategy",
        event_type="state_transition",
        occurred_at=occurred_at,
        payload=_candidate_payload(),
    )
    duplicate = repository.record_delivery(
        event_id="evt-exact-duplicate",
        source="strategy",
        event_type="state_transition",
        occurred_at=occurred_at,
        payload=_candidate_payload(),
    )
    events = repository.list_audit_events()
    head = repository.audit_chain_head()

    assert first.delivery_status is AuditDeliveryStatus.CANONICAL
    assert first.projection_mutated
    assert duplicate.delivery_status is AuditDeliveryStatus.EXACT_DUPLICATE
    assert not duplicate.projection_mutated
    assert repository.processed_event_count() == 1
    assert len(events) == 2
    assert [event.chain_sequence for event in events] == [1, 2]
    assert verify_hash_chain(
        events,
        expected_count=head.event_count,
        expected_last_record_hash=head.last_record_hash,
    )


def test_semantic_conflict_is_audited_without_projection_mutation(
    session_factory: sessionmaker[Session],
) -> None:
    repository = AuditRepository(session_factory)
    occurred_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository.record_delivery(
        event_id="evt-conflict",
        source="strategy",
        event_type="state_transition",
        occurred_at=occurred_at,
        payload=_candidate_payload(),
    )

    conflict = repository.record_delivery(
        event_id="evt-conflict",
        source="strategy",
        event_type="state_transition",
        occurred_at=occurred_at,
        payload={**_candidate_payload(), "event_note": "semantic-conflict"},
    )

    events = repository.list_audit_events()
    assert conflict.delivery_status is AuditDeliveryStatus.SEMANTIC_CONFLICT
    assert conflict.reconciliation_required
    assert not conflict.projection_mutated
    assert [event.delivery_status for event in events] == [
        AuditDeliveryStatus.CANONICAL.value,
        AuditDeliveryStatus.SEMANTIC_CONFLICT.value,
    ]
    assert repository.projected_state("plan-atomic") == "CANDIDATE"
    assert repository.processed_event_count() == 1
