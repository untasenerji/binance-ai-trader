from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.domain.types import TradePlanState
from app.persistence.audit import AuditChainHeadSnapshot, AuditRepository
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.persistence.reducer import (
    ProjectionState,
    StateTransitionEvent,
    TransitionEventSchemaError,
    TransitionEvidence,
    reduce_state_transition,
)
from app.persistence.replay import ReplayRunner


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'reducer.sqlite'}")
    create_schema(engine)
    return create_session_factory(engine)


def _payload(
    *,
    from_state: str,
    to_state: str,
    version: int,
    source_sequence: int,
) -> dict[str, object]:
    return {
        "plan_id": "plan-reducer",
        "from_state": from_state,
        "to_state": to_state,
        "plan_version": version,
        "source_sequence": source_sequence,
        "transition_evidence": {
            "risk_permits_entry": to_state == "ENTRY_PENDING",
            "stop_confirmed": to_state
            in {"POSITION_PROTECTED", "MANAGING_POSITION", "ADD_PENDING"},
            "reduction_only": False,
        },
    }


def test_malformed_delivery_does_not_claim_event_id_before_valid_redelivery(
    session_factory: sessionmaker[Session],
) -> None:
    repository = AuditRepository(session_factory)
    occurred_at = datetime(2026, 7, 12, tzinfo=UTC)

    with pytest.raises(TransitionEventSchemaError):
        repository.record_delivery(
            event_id="evt-corrected",
            source="simulator",
            event_type="state_transition",
            occurred_at=occurred_at,
            payload={"plan_id": "plan-reducer", "to_state": "CANDIDATE"},
        )

    accepted = repository.record_delivery(
        event_id="evt-corrected",
        source="simulator",
        event_type="state_transition",
        occurred_at=occurred_at,
        payload=_payload(from_state="DRAFT", to_state="CANDIDATE", version=1, source_sequence=1),
    )

    assert accepted.projection_mutated
    assert repository.processed_event_count() == 1
    assert repository.projected_state("plan-reducer") == "CANDIDATE"


def test_terminal_projection_cannot_regress_from_a_delayed_event() -> None:
    current = ProjectionState(
        plan_id="plan-reducer",
        state=TradePlanState.CLOSED,
        plan_version=7,
        source_sequence=7,
    )
    delayed = StateTransitionEvent(
        plan_id="plan-reducer",
        from_state=TradePlanState.ENTRY_PENDING,
        to_state=TradePlanState.PARTIALLY_FILLED,
        plan_version=5,
        source_sequence=5,
        evidence=TransitionEvidence(),
    )

    result = reduce_state_transition(current, delayed)

    assert not result.mutated
    assert result.stale
    assert result.projection == current


def test_online_projection_and_replay_share_the_same_reducer_and_fail_closed_on_bad_head(
    session_factory: sessionmaker[Session],
) -> None:
    repository = AuditRepository(session_factory)
    occurred_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository.record_delivery(
        event_id="evt-1",
        source="simulator",
        event_type="state_transition",
        occurred_at=occurred_at,
        payload=_payload(from_state="DRAFT", to_state="CANDIDATE", version=1, source_sequence=1),
    )
    repository.record_delivery(
        event_id="evt-2",
        source="simulator",
        event_type="state_transition",
        occurred_at=occurred_at,
        payload=_payload(from_state="CANDIDATE", to_state="PLANNED", version=2, source_sequence=2),
    )
    repository.record_delivery(
        event_id="evt-delayed",
        source="simulator",
        event_type="state_transition",
        occurred_at=occurred_at,
        payload=_payload(from_state="DRAFT", to_state="CANDIDATE", version=1, source_sequence=1),
    )

    events = repository.list_audit_events()
    head = repository.audit_chain_head()
    replay = ReplayRunner().replay(events, head)
    bad_head_replay = ReplayRunner().replay(
        events,
        AuditChainHeadSnapshot(
            event_count=head.event_count + 1,
            last_sequence=head.last_sequence,
            last_record_hash=head.last_record_hash,
        ),
    )

    assert replay.is_valid
    assert replay.plan_states == {"plan-reducer": TradePlanState.PLANNED}
    assert repository.projected_state("plan-reducer") == replay.plan_states["plan-reducer"].value
    assert not bad_head_replay.is_valid
    assert bad_head_replay.plan_states == {}


def test_replay_rejects_reordered_tail_deleted_tampered_and_conflicting_audit_evidence(
    session_factory: sessionmaker[Session],
) -> None:
    repository = AuditRepository(session_factory)
    occurred_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository.record_delivery(
        event_id="evt-valid-1",
        source="simulator",
        event_type="state_transition",
        occurred_at=occurred_at,
        payload=_payload(from_state="DRAFT", to_state="CANDIDATE", version=1, source_sequence=1),
    )
    repository.record_delivery(
        event_id="evt-valid-2",
        source="simulator",
        event_type="state_transition",
        occurred_at=occurred_at,
        payload=_payload(from_state="CANDIDATE", to_state="PLANNED", version=2, source_sequence=2),
    )
    events = repository.list_audit_events()
    head = repository.audit_chain_head()

    assert not ReplayRunner().replay(tuple(reversed(events)), head).is_valid
    assert not ReplayRunner().replay(events[:-1], head).is_valid

    events[0].payload["to_state"] = "CLOSED"
    assert not ReplayRunner().replay(events, head).is_valid

    fresh_events = repository.list_audit_events()
    repository.record_delivery(
        event_id="evt-valid-1",
        source="simulator",
        event_type="state_transition",
        occurred_at=occurred_at,
        payload={
            **_payload(from_state="DRAFT", to_state="CANDIDATE", version=1, source_sequence=1),
            "semantic_note": "conflict",
        },
    )
    conflict_events = repository.list_audit_events()
    conflict_head = repository.audit_chain_head()

    assert ReplayRunner().replay(fresh_events, head).is_valid
    assert not ReplayRunner().replay(conflict_events, conflict_head).is_valid
