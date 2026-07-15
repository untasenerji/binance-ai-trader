from decimal import Decimal
from pathlib import Path

import pytest
from conftest import actual_risk_policy_for

from app.domain.types import Direction
from app.exchange.contracts import (
    AlgoOrderIntent,
    ReconciliationSnapshot,
)
from app.observability.recovery import RecoveryAction
from app.persistence.audit import AuditRepository
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.security.recovery import (
    LocalRecoveryCoordinator,
    RecoveryCheckpoint,
    RecoveryCheckpointError,
    RecoveryDisposition,
    RecoveryJournal,
    SimulatedPositionCheckpoint,
    SimulatedProtectionStatus,
)
from app.simulation.intent_ledger import DurableIntentLedger
from app.simulation.models import OrderRole, SimulatedFault, SimulatedOrderIntent
from app.simulation.simulator import ExchangeSimulator, FaultPlan


def _confirmed_checkpoint() -> RecoveryCheckpoint:
    return RecoveryCheckpoint(
        positions=(
            SimulatedPositionCheckpoint(
                plan_id="plan-recovery",
                symbol="BTCUSDT",
                quantity=Decimal("0.005"),
                simulated_protection=SimulatedProtectionStatus.REHEARSAL_READY,
                simulated_stop_reference="simulated-stop-1",
            ),
        ),
    )


@pytest.fixture
def audit_repository(tmp_path: Path) -> AuditRepository:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'security-recovery.sqlite'}")
    create_schema(engine)
    return AuditRepository(create_session_factory(engine))


def _reconciliation_snapshot(ledger: DurableIntentLedger) -> ReconciliationSnapshot:
    contract = ledger.reconciliation_facts().expected_stop_contracts[0]
    return ReconciliationSnapshot(
        positions_by_symbol={"BTCUSDT": Decimal("0.005")},
        normal_order_client_ids=frozenset({"UTA1-recovery-EN-1"}),
        algo_order_client_ids=frozenset({"recovery-simulated-stop"}),
        algo_orders=(
            AlgoOrderIntent(
                client_algo_id=contract.client_algo_id,
                symbol=contract.symbol,
                direction=contract.position_side,
                algo_type=contract.algo_type,
                trigger_price=contract.trigger_price,
                close_position=contract.close_position,
                working_type=contract.working_type,
                status=contract.active_status,
                plan_id=contract.plan_id,
                policy_version=contract.policy_version,
                account_envelope_version=contract.account_envelope_version,
                policy_fingerprint=contract.policy_fingerprint,
                account_envelope_fingerprint=contract.account_envelope_fingerprint,
                stop_contract_fingerprint=contract.fingerprint,
            ),
        ),
    )


def test_restart_matches_typed_snapshot_but_cannot_enable_live_authority(
    tmp_path: Path,
    durable_intent_ledger: DurableIntentLedger,
    audit_repository: AuditRepository,
) -> None:
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=actual_risk_policy_for("recovery"),
        fault_plan=FaultPlan.from_faults((SimulatedFault.PARTIAL_FILL,)),
    )
    partial = simulator.submit(
        SimulatedOrderIntent(
            client_order_id="UTA1-recovery-EN-1",
            plan_id="recovery",
            symbol="BTCUSDT",
            direction=Direction.LONG,
            role=OrderRole.ENTRY,
            stage_index=1,
            quantity=Decimal("0.010"),
            price=Decimal("1000"),
        )
    )
    checkpoint = _confirmed_checkpoint()
    assert partial.filled_quantity == checkpoint.positions[0].quantity

    journal_path = tmp_path / "recovery-checkpoint.json"
    RecoveryJournal(journal_path).save(checkpoint)
    restored_checkpoint = RecoveryJournal(journal_path).load()
    result = LocalRecoveryCoordinator().recover_after_restart(
        restored_checkpoint,
        audit_repository=audit_repository,
        reconciliation_snapshot=_reconciliation_snapshot(durable_intent_ledger),
        intent_ledger=durable_intent_ledger,
    )

    assert result.disposition is RecoveryDisposition.RECONCILED
    assert result.local_reconciliation_complete
    assert result.simulated_protection_invariant_holds
    assert result.entry_authority_enabled is False
    assert result.actions == ()
    assert result.reason is None


def test_network_partition_pauses_entries_and_requires_stop_reverification() -> None:
    result = LocalRecoveryCoordinator().network_partition(_confirmed_checkpoint())

    assert result.disposition is RecoveryDisposition.PAUSED
    assert result.simulated_protection_invariant_holds
    assert result.entry_authority_enabled is False
    assert RecoveryAction.PAUSE_NEW_ENTRIES in result.actions
    assert RecoveryAction.RECONCILE_REQUIRED in result.actions
    assert RecoveryAction.VERIFY_STOP_PROTECTION in result.actions


def test_missing_stop_hard_halts_recovery() -> None:
    missing_stop = RecoveryCheckpoint(
        positions=(
            SimulatedPositionCheckpoint(
                plan_id="plan-unprotected",
                symbol="BTCUSDT",
                quantity=Decimal("0.005"),
                simulated_protection=SimulatedProtectionStatus.MISSING,
                simulated_stop_reference=None,
            ),
        ),
    )
    engine = create_database_engine("sqlite://")
    create_schema(engine)
    intent_ledger = DurableIntentLedger(create_session_factory(engine))
    result = LocalRecoveryCoordinator().recover_after_restart(
        missing_stop,
        audit_repository=AuditRepository(create_session_factory(engine)),
        reconciliation_snapshot=ReconciliationSnapshot(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
        ),
        intent_ledger=intent_ledger,
    )

    assert result.disposition is RecoveryDisposition.HARD_HALTED
    assert result.entry_authority_enabled is False
    assert RecoveryAction.HARD_HALT in result.actions


def test_checkpoint_rejects_incompatible_or_tampered_content(tmp_path: Path) -> None:
    journal_path = tmp_path / "recovery-checkpoint.json"
    journal_path.write_text(
        '{"audit_hash_chain_valid":true,"local_projection_consistent":true,'
        '"positions":[],"schema_version":1}',
        encoding="ascii",
    )

    with pytest.raises(RecoveryCheckpointError):
        RecoveryJournal(journal_path).load()
