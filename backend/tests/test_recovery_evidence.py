from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from app.exchange.contracts import (
    LocalReconciliationState,
    ReconciliationOutcome,
    ReconciliationSnapshot,
    reconcile_local_state,
)
from app.persistence.audit import AuditRepository
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.persistence.models import DurableOrderIntent
from app.security.recovery import (
    LocalRecoveryCoordinator,
    RecoveryCheckpoint,
    RecoveryDisposition,
    SimulatedOpenPosition,
    StopProtectionEvidence,
)
from app.simulation.intent_ledger import DurableIntentLedger, DurableIntentStatus


@pytest.fixture
def audit_repository(tmp_path: Path) -> tuple[AuditRepository, Engine]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'recovery-evidence.sqlite'}")
    create_schema(engine)
    return AuditRepository(create_session_factory(engine)), engine


def _clean_reconciliation() -> ReconciliationOutcome:
    return reconcile_local_state(
        local=LocalReconciliationState(
            positions_by_symbol={"BTCUSDT": Decimal("0.005")},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
            required_stop_symbols=frozenset({"BTCUSDT"}),
            unresolved_unknown_intent_ids=frozenset(),
            audit_chain_valid=True,
            replay_valid=True,
        ),
        snapshot=ReconciliationSnapshot(
            positions_by_symbol={"BTCUSDT": Decimal("0.005")},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
            stop_protected_symbols=frozenset({"BTCUSDT"}),
        ),
    )


def _ledger(engine: Engine) -> DurableIntentLedger:
    return DurableIntentLedger(create_session_factory(engine))


def _record_candidate(audit_repository: AuditRepository) -> None:
    audit_repository.record_delivery(
        event_id="recovery-state-1",
        source="strategy",
        event_type="state_transition",
        occurred_at=datetime(2026, 7, 12, tzinfo=UTC),
        payload={
            "plan_id": "recovery-plan",
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


def test_restart_derives_recovery_from_actual_audit_replay_and_typed_reconciliation(
    audit_repository: tuple[AuditRepository, Engine],
) -> None:
    repository, engine = audit_repository
    checkpoint = RecoveryCheckpoint(
        positions=(
            SimulatedOpenPosition(
                plan_id="recovery-plan",
                symbol="BTCUSDT",
                quantity=Decimal("0.005"),
                stop_protection=StopProtectionEvidence.REMOTE_CONFIRMED,
                stop_reference="stop-1",
            ),
        ),
    )

    result = LocalRecoveryCoordinator().recover_after_restart(
        checkpoint,
        audit_repository=repository,
        reconciliation_outcome=_clean_reconciliation(),
        intent_ledger=_ledger(engine),
    )

    assert result.disposition is RecoveryDisposition.RECONCILED
    assert result.local_reconciliation_complete


def test_restart_hard_halts_when_a_privileged_audit_mutation_breaks_replay(
    audit_repository: tuple[AuditRepository, Engine],
) -> None:
    repository, engine = audit_repository
    _record_candidate(repository)

    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER prevent_audit_events_update"))
        connection.execute(text("UPDATE audit_events SET source = 'tampered' WHERE id = 1"))

    result = LocalRecoveryCoordinator().recover_after_restart(
        RecoveryCheckpoint(positions=()),
        audit_repository=repository,
        reconciliation_outcome=_clean_reconciliation(),
        intent_ledger=_ledger(engine),
    )

    assert result.disposition is RecoveryDisposition.HARD_HALTED
    assert result.reason == "AUDIT_CHAIN_OR_REPLAY_INVALID"


def test_restart_pauses_when_durable_projection_disagrees_with_valid_replay(
    audit_repository: tuple[AuditRepository, Engine],
) -> None:
    repository, engine = audit_repository
    _record_candidate(repository)

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE trade_plan_projections SET state = 'CLOSED' WHERE plan_id = 'recovery-plan'"
            )
        )

    result = LocalRecoveryCoordinator().recover_after_restart(
        RecoveryCheckpoint(positions=()),
        audit_repository=repository,
        reconciliation_outcome=_clean_reconciliation(),
        intent_ledger=_ledger(engine),
    )

    assert result.disposition is RecoveryDisposition.PAUSED
    assert result.reason == "LOCAL_PROJECTION_MISMATCH"


def test_restart_pauses_when_the_durable_ledger_still_has_an_unknown_intent(
    audit_repository: tuple[AuditRepository, Engine],
) -> None:
    repository, engine = audit_repository
    session_factory = create_session_factory(engine)
    with session_factory.begin() as session:
        session.add(
            DurableOrderIntent(
                economic_key="recovery-plan:BTCUSDT:LONG:ENTRY:1",
                attempt_number=1,
                client_order_id="recovery-unknown",
                plan_id="recovery-plan",
                symbol="BTCUSDT",
                direction="LONG",
                role="ENTRY",
                stage_index=1,
                quantity="0.005",
                price="100",
                filled_quantity="0",
                status=DurableIntentStatus.UNKNOWN.value,
            )
        )
    ledger = DurableIntentLedger(session_factory)

    result = LocalRecoveryCoordinator().recover_after_restart(
        RecoveryCheckpoint(positions=()),
        audit_repository=repository,
        reconciliation_outcome=_clean_reconciliation(),
        intent_ledger=ledger,
    )

    assert result.disposition is RecoveryDisposition.PAUSED
    assert result.reason == "DURABLE_UNKNOWN_INTENT"
