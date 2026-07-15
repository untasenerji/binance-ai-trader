from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app.exchange.contracts import (
    LocalReconciliationState,
    ReconciliationOutcome,
    ReconciliationSnapshot,
    reconcile_local_state,
)
from app.persistence.audit import AuditRepository
from app.persistence.circuit_breaker import (
    EntryIntentAuthorizationGate,
    PersistenceCircuitBreaker,
    PersistenceRecoveryEvidence,
    PersistenceUnavailable,
)
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.simulation.intent_ledger import DurableIntentLedger


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'breaker.sqlite'}")
    create_schema(engine)
    return create_session_factory(engine)


def _transition_payload() -> dict[str, object]:
    return {
        "plan_id": "plan-breaker",
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


def _record(repository: AuditRepository, event_id: str = "evt-breaker") -> None:
    repository.record_delivery(
        event_id=event_id,
        source="strategy",
        event_type="state_transition",
        occurred_at=datetime(2026, 7, 12, tzinfo=UTC),
        payload=_transition_payload(),
    )


def _clean_reconciliation_snapshot() -> ReconciliationSnapshot:
    return ReconciliationSnapshot(
        positions_by_symbol={},
        normal_order_client_ids=frozenset(),
        algo_order_client_ids=frozenset(),
    )


def _clean_reconciliation_outcome() -> ReconciliationOutcome:
    snapshot = _clean_reconciliation_snapshot()
    return reconcile_local_state(
        local=LocalReconciliationState(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
            required_stop_symbols=frozenset(),
            unresolved_unknown_intent_ids=frozenset(),
            audit_chain_valid=True,
            replay_valid=True,
        ),
        snapshot=snapshot,
    )


def _recovery_components(
    session_factory: sessionmaker[Session],
) -> tuple[PersistenceCircuitBreaker, DurableIntentLedger, AuditRepository]:
    breaker = PersistenceCircuitBreaker()
    ledger = DurableIntentLedger(session_factory, persistence_breaker=breaker)
    repository = AuditRepository(session_factory, persistence_breaker=breaker)
    return breaker, ledger, repository


def _open_breaker(
    session_factory: sessionmaker[Session],
) -> tuple[PersistenceCircuitBreaker, DurableIntentLedger, AuditRepository]:
    breaker, ledger, repository = _recovery_components(session_factory)
    breaker.reset_after_verified_reconciliation(
        audit_repository=repository,
        intent_ledger=ledger,
        reconciliation_snapshot=_clean_reconciliation_snapshot(),
    )
    return breaker, ledger, repository


def test_breaker_starts_fail_closed_and_only_repository_derived_evidence_opens_it(
    session_factory: sessionmaker[Session],
) -> None:
    breaker, ledger, repository = _recovery_components(session_factory)
    gate = EntryIntentAuthorizationGate(breaker)

    with pytest.raises(PersistenceUnavailable, match="STARTUP_RECONCILIATION_REQUIRED"):
        gate.authorize_new_entry_intent()

    with pytest.raises(PersistenceUnavailable, match="RECOVERY_EVIDENCE_INCOMPLETE"):
        breaker.reset_after_verified_reconciliation(
            audit_repository=repository,
            intent_ledger=ledger,
            reconciliation_snapshot=ReconciliationSnapshot(
                positions_by_symbol={},
                normal_order_client_ids=frozenset({"unexpected-normal"}),
                algo_order_client_ids=frozenset(),
            ),
        )

    with pytest.raises(TypeError):
        breaker.reset_after_verified_reconciliation(  # type: ignore[misc, call-arg]
            {}
        )

    evidence = breaker.reset_after_verified_reconciliation(
        audit_repository=repository,
        intent_ledger=ledger,
        reconciliation_snapshot=_clean_reconciliation_snapshot(),
    )
    assert evidence.write_probe_event_id.startswith("persistence-recovery-probe-")
    assert evidence.replay_valid
    assert evidence.projection_matches_replay
    gate.authorize_new_entry_intent()


def test_recovery_evidence_rejects_missing_or_noncontiguous_audit_witnesses() -> None:
    outcome = _clean_reconciliation_outcome()

    with pytest.raises(ValueError, match="write-probe"):
        PersistenceRecoveryEvidence("", 1, 1, "a" * 64, True, True, outcome, ())
    with pytest.raises(ValueError, match="non-empty"):
        PersistenceRecoveryEvidence("probe", 0, 1, "a" * 64, True, True, outcome, ())
    with pytest.raises(ValueError, match="contiguous"):
        PersistenceRecoveryEvidence("probe", 2, 1, "a" * 64, True, True, outcome, ())
    with pytest.raises(ValueError, match="non-null"):
        PersistenceRecoveryEvidence("probe", 1, 1, None, True, True, outcome, ())
    with pytest.raises(ValueError, match="non-empty strings"):
        PersistenceRecoveryEvidence("probe", 1, 1, "a" * 64, True, True, outcome, ("",))


def test_breaker_rejects_nonconcrete_recovery_inputs(
    session_factory: sessionmaker[Session],
) -> None:
    breaker, ledger, repository = _recovery_components(session_factory)
    snapshot = _clean_reconciliation_snapshot()

    with pytest.raises(TypeError, match="concrete audit"):
        breaker.reset_after_verified_reconciliation(
            audit_repository=object(),
            intent_ledger=ledger,
            reconciliation_snapshot=snapshot,
        )
    with pytest.raises(TypeError, match="concrete durable"):
        breaker.reset_after_verified_reconciliation(
            audit_repository=repository,
            intent_ledger=object(),
            reconciliation_snapshot=snapshot,
        )
    with pytest.raises(TypeError, match="typed exchange"):
        breaker.reset_after_verified_reconciliation(
            audit_repository=repository,
            intent_ledger=ledger,
            reconciliation_snapshot=cast(ReconciliationSnapshot, object()),
        )


def test_breaker_remains_halted_when_repository_recovery_collection_fails(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    breaker, ledger, repository = _recovery_components(session_factory)

    def raise_collection_failure(*_: object, **__: object) -> PersistenceRecoveryEvidence:
        raise RuntimeError("simulated recovery collection failure")

    monkeypatch.setattr(
        AuditRepository,
        "collect_persistence_recovery_evidence",
        raise_collection_failure,
    )

    with pytest.raises(PersistenceUnavailable, match="RECOVERY_EVIDENCE_COLLECTION_FAILED"):
        breaker.reset_after_verified_reconciliation(
            audit_repository=repository,
            intent_ledger=ledger,
            reconciliation_snapshot=_clean_reconciliation_snapshot(),
        )

    assert not breaker.new_entries_allowed
    assert breaker.halted_reason == "DATABASE_AUDIT_FAILURE: RuntimeError"


def test_normalization_error_does_not_trip_an_open_breaker(
    session_factory: sessionmaker[Session],
) -> None:
    breaker, _, repository = _open_breaker(session_factory)

    with pytest.raises(ValueError, match="binary floating point"):
        repository.record_delivery(
            event_id="evt-invalid",
            source="strategy",
            event_type="state_transition",
            occurred_at=datetime(2026, 7, 12, tzinfo=UTC),
            payload={"invalid": 0.1},
        )

    assert breaker.new_entries_allowed


def test_failure_before_insert_rolls_back_and_trips_shared_breaker(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    breaker = PersistenceCircuitBreaker()
    repository = AuditRepository(session_factory, persistence_breaker=breaker)
    _record(repository, "evt-before")

    def fail_before_insert(_: Session) -> NoReturn:
        raise OSError("injected before insert failure")

    monkeypatch.setattr(AuditRepository, "_chain_head_for_update", staticmethod(fail_before_insert))

    with pytest.raises(OSError, match="before insert"):
        _record(repository, "evt-after-before")

    assert len(repository.list_audit_events()) == 1
    with pytest.raises(PersistenceUnavailable, match="DATABASE_AUDIT_FAILURE"):
        repository.entry_authorization_gate.authorize_new_entry_intent()


def test_flush_failure_rolls_back_and_trips_shared_breaker(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    breaker = PersistenceCircuitBreaker()
    repository = AuditRepository(session_factory, persistence_breaker=breaker)
    _record(repository, "evt-before-flush")
    original_flush = Session.flush

    def fail_flush(self: Session, objects: Sequence[Any] | None = None) -> None:
        if self.new:
            raise OSError("injected flush failure")
        original_flush(self, objects)

    monkeypatch.setattr(Session, "flush", fail_flush)

    with pytest.raises(OSError, match="flush failure"):
        _record(repository, "evt-after-flush")

    assert len(repository.list_audit_events()) == 1
    assert not breaker.new_entries_allowed


def test_projection_failure_rolls_back_and_trips_shared_breaker(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    breaker = PersistenceCircuitBreaker()
    repository = AuditRepository(session_factory, persistence_breaker=breaker)
    _record(repository, "evt-before-projection")

    def fail_projection(*_: object, **__: object) -> NoReturn:
        raise OSError("injected projection failure")

    monkeypatch.setattr(AuditRepository, "_reduce_projection", staticmethod(fail_projection))

    with pytest.raises(OSError, match="projection failure"):
        _record(repository, "evt-after-projection")

    assert len(repository.list_audit_events()) == 1
    assert not breaker.new_entries_allowed


def test_commit_failure_rolls_back_and_trips_shared_breaker(
    session_factory: sessionmaker[Session],
) -> None:
    breaker = PersistenceCircuitBreaker()
    repository = AuditRepository(session_factory, persistence_breaker=breaker)
    _record(repository, "evt-before-commit")

    def fail_commit(_: Session) -> NoReturn:
        raise OSError("injected commit failure")

    event.listen(Session, "before_commit", fail_commit)
    try:
        with pytest.raises(OSError, match="commit failure"):
            _record(repository, "evt-after-commit")
    finally:
        event.remove(Session, "before_commit", fail_commit)

    assert len(repository.list_audit_events()) == 1
    assert not breaker.new_entries_allowed


def test_restart_remains_closed_until_repository_evidence_is_recomputed(
    session_factory: sessionmaker[Session],
) -> None:
    restarted_breaker, restarted_ledger, restarted_repository = _recovery_components(
        session_factory
    )

    assert not restarted_breaker.new_entries_allowed
    with pytest.raises(PersistenceUnavailable):
        restarted_breaker.require_new_entries_allowed()

    restarted_breaker.reset_after_verified_reconciliation(
        audit_repository=restarted_repository,
        intent_ledger=restarted_ledger,
        reconciliation_snapshot=_clean_reconciliation_snapshot(),
    )

    assert restarted_breaker.new_entries_allowed
