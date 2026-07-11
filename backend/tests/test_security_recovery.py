from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.types import Direction
from app.observability.recovery import RecoveryAction
from app.security.recovery import (
    LocalRecoveryCoordinator,
    RecoveryCheckpoint,
    RecoveryCheckpointError,
    RecoveryDisposition,
    RecoveryJournal,
    SimulatedOpenPosition,
    StopProtectionEvidence,
)
from app.simulation.models import OrderRole, SimulatedFault, SimulatedOrderIntent
from app.simulation.simulator import ExchangeSimulator, FaultPlan


def _confirmed_checkpoint(*, projection_consistent: bool = True) -> RecoveryCheckpoint:
    return RecoveryCheckpoint(
        audit_hash_chain_valid=True,
        local_projection_consistent=projection_consistent,
        positions=(
            SimulatedOpenPosition(
                plan_id="plan-recovery",
                symbol="BTCUSDT",
                quantity=Decimal("0.005"),
                stop_protection=StopProtectionEvidence.REMOTE_CONFIRMED,
                stop_reference="simulated-stop-1",
            ),
        ),
    )


def test_restart_recovers_a_partial_simulated_position_from_durable_checkpoint(
    tmp_path: Path,
) -> None:
    simulator = ExchangeSimulator(fault_plan=FaultPlan.from_faults((SimulatedFault.PARTIAL_FILL,)))
    partial = simulator.submit(
        SimulatedOrderIntent(
            client_order_id="UTA1-recovery-EN-1",
            economic_key="recovery:ENTRY:1",
            symbol="BTCUSDT",
            direction=Direction.LONG,
            role=OrderRole.ENTRY,
            quantity=Decimal("0.010"),
            price=Decimal("1000"),
        )
    )
    checkpoint = _confirmed_checkpoint()
    assert partial.filled_quantity == checkpoint.positions[0].quantity

    journal_path = tmp_path / "recovery-checkpoint.json"
    RecoveryJournal(journal_path).save(checkpoint)
    restored_checkpoint = RecoveryJournal(journal_path).load()
    result = LocalRecoveryCoordinator().recover_after_restart(restored_checkpoint)

    assert result.disposition is RecoveryDisposition.RECONCILED
    assert result.local_reconciliation_complete
    assert result.stop_protection_invariant_holds
    assert result.entry_authority_enabled is False
    assert result.actions == ()


def test_network_partition_pauses_entries_and_requires_stop_reverification() -> None:
    result = LocalRecoveryCoordinator().network_partition(_confirmed_checkpoint())

    assert result.disposition is RecoveryDisposition.PAUSED
    assert result.stop_protection_invariant_holds
    assert result.entry_authority_enabled is False
    assert RecoveryAction.PAUSE_NEW_ENTRIES in result.actions
    assert RecoveryAction.RECONCILE_REQUIRED in result.actions
    assert RecoveryAction.VERIFY_STOP_PROTECTION in result.actions


def test_missing_stop_or_invalid_audit_hard_halts_recovery() -> None:
    missing_stop = RecoveryCheckpoint(
        audit_hash_chain_valid=True,
        local_projection_consistent=True,
        positions=(
            SimulatedOpenPosition(
                plan_id="plan-unprotected",
                symbol="BTCUSDT",
                quantity=Decimal("0.005"),
                stop_protection=StopProtectionEvidence.MISSING,
                stop_reference=None,
            ),
        ),
    )
    invalid_audit = RecoveryCheckpoint(
        audit_hash_chain_valid=False,
        local_projection_consistent=True,
        positions=(),
    )
    coordinator = LocalRecoveryCoordinator()

    for checkpoint in (missing_stop, invalid_audit):
        result = coordinator.recover_after_restart(checkpoint)
        assert result.disposition is RecoveryDisposition.HARD_HALTED
        assert result.entry_authority_enabled is False
        assert RecoveryAction.HARD_HALT in result.actions


def test_checkpoint_rejects_incompatible_or_tampered_content(tmp_path: Path) -> None:
    journal_path = tmp_path / "recovery-checkpoint.json"
    journal_path.write_text('{"schema_version":2}', encoding="ascii")

    with pytest.raises(RecoveryCheckpointError):
        RecoveryJournal(journal_path).load()
