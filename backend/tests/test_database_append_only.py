from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.persistence.audit import AuditRepository, verify_hash_chain
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.persistence.models import AuditEvent
from app.persistence.replay import ReplayRunner


@pytest.fixture
def repository(tmp_path: Path) -> tuple[AuditRepository, sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'append-only.sqlite'}")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    return AuditRepository(session_factory), session_factory


def _record(repository: AuditRepository, event_id: str, plan_version: int) -> None:
    transition = {
        1: ("DRAFT", "CANDIDATE"),
        2: ("CANDIDATE", "PLANNED"),
        3: ("PLANNED", "ARMED"),
    }[plan_version]
    repository.record_delivery(
        event_id=event_id,
        source="strategy",
        event_type="state_transition",
        occurred_at=datetime(2026, 7, 12, tzinfo=UTC),
        payload={
            "plan_id": "append-plan",
            "from_state": transition[0],
            "to_state": transition[1],
            "plan_version": plan_version,
            "source_sequence": plan_version,
            "transition_evidence": {
                "risk_permits_entry": False,
                "stop_confirmed": False,
                "reduction_only": False,
            },
        },
    )


def test_database_triggers_block_core_and_raw_mutations(
    repository: tuple[AuditRepository, sessionmaker[Session]],
) -> None:
    audit_repository, session_factory = repository
    _record(audit_repository, "append-1", 1)
    event = audit_repository.list_audit_events()[0]
    engine = session_factory.kw["bind"]

    assert engine is not None
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                update(AuditEvent).where(AuditEvent.id == event.id).values(source="core-tampered")
            )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE audit_events SET source = 'tampered' WHERE id = :id"), {"id": event.id}
            )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM audit_events WHERE id = :id"), {"id": event.id})

    assert verify_hash_chain(
        audit_repository.list_audit_events(),
        expected_count=audit_repository.audit_chain_head().event_count,
        expected_last_record_hash=audit_repository.audit_chain_head().last_record_hash,
    )


@pytest.mark.parametrize("event_position", (1, -1), ids=("middle", "tail"))
def test_privileged_middle_or_tail_delete_is_detected_by_chain_head_and_count(
    repository: tuple[AuditRepository, sessionmaker[Session]],
    event_position: int,
) -> None:
    audit_repository, session_factory = repository
    _record(audit_repository, "append-1", 1)
    _record(audit_repository, "append-2", 2)
    _record(audit_repository, "append-3", 3)
    head = audit_repository.audit_chain_head()
    victim = audit_repository.list_audit_events()[event_position]
    engine = session_factory.kw["bind"]

    assert engine is not None
    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER prevent_audit_events_delete"))
        connection.execute(text("DELETE FROM audit_events WHERE id = :id"), {"id": victim.id})

    events = audit_repository.list_audit_events()
    assert not verify_hash_chain(
        events,
        expected_count=head.event_count,
        expected_last_record_hash=head.last_record_hash,
    )


def test_privileged_chain_head_count_tamper_invalidates_replay(
    repository: tuple[AuditRepository, sessionmaker[Session]],
) -> None:
    audit_repository, session_factory = repository
    _record(audit_repository, "append-1", 1)
    engine = session_factory.kw["bind"]

    assert engine is not None
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE audit_chain_heads SET event_count = 999 WHERE chain_id = 1")
        )

    replay = ReplayRunner().replay(
        audit_repository.list_audit_events(),
        audit_repository.audit_chain_head(),
    )

    assert not replay.is_valid
    assert replay.reason == "AUDIT_CHAIN_OR_HEAD_INVALID"
