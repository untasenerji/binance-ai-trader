"""Restart recovery derived from durable audit replay and typed reconciliation evidence."""

import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Literal

from app.domain.decimal_math import ZERO
from app.exchange.contracts import ReconciliationSnapshot
from app.observability.recovery import RecoveryAction
from app.persistence.audit import AuditRepository
from app.persistence.replay import ReplayRunner
from app.simulation.intent_ledger import DurableIntentLedger, DurableIntentStatus


class StopProtectionEvidence(StrEnum):
    """Last locally recorded protection state; this module never queries an exchange."""

    REMOTE_CONFIRMED = "REMOTE_CONFIRMED"
    UNCONFIRMED = "UNCONFIRMED"
    MISSING = "MISSING"


class RecoveryDisposition(StrEnum):
    RECONCILED = "RECONCILED"
    PAUSED = "PAUSED"
    HARD_HALTED = "HARD_HALTED"


class RecoveryCheckpointError(ValueError):
    """Raised when durable local recovery evidence is absent or invalid."""


@dataclass(frozen=True, slots=True)
class SimulatedOpenPosition:
    plan_id: str
    symbol: str
    quantity: Decimal
    stop_protection: StopProtectionEvidence
    stop_reference: str | None

    def __post_init__(self) -> None:
        if not self.plan_id or not self.symbol or self.quantity <= ZERO:
            raise ValueError("simulated position needs plan, symbol, and positive quantity")
        if self.stop_protection is StopProtectionEvidence.REMOTE_CONFIRMED:
            if not self.stop_reference:
                raise ValueError("confirmed stop protection requires a reference")
        elif self.stop_reference is not None:
            raise ValueError("only confirmed stop protection may retain a reference")


@dataclass(frozen=True, slots=True)
class RecoveryCheckpoint:
    """Only position/stop facts are checkpointed; audit health is recomputed on restart."""

    positions: tuple[SimulatedOpenPosition, ...]
    schema_version: Literal[2] = 2

    def __post_init__(self) -> None:
        plan_ids = tuple(position.plan_id for position in self.positions)
        if len(plan_ids) != len(set(plan_ids)):
            raise ValueError("recovery checkpoint cannot contain duplicate plan IDs")


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    disposition: RecoveryDisposition
    local_reconciliation_complete: bool
    stop_protection_invariant_holds: bool
    entry_authority_enabled: Literal[False]
    actions: tuple[RecoveryAction, ...]
    reason: str | None


class RecoveryJournal:
    """Writes an atomic local checkpoint that can be read by a fresh process."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def save(self, checkpoint: RecoveryCheckpoint) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._path.with_name(f".{self._path.name}.tmp")
        payload = self._encode(checkpoint)
        try:
            with temporary_path.open("w", encoding="ascii", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(self._path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def load(self) -> RecoveryCheckpoint:
        try:
            with self._path.open(encoding="ascii") as handle:
                payload: object = json.load(handle)
        except FileNotFoundError as error:
            raise RecoveryCheckpointError("recovery checkpoint is missing") from error
        except json.JSONDecodeError as error:
            raise RecoveryCheckpointError("recovery checkpoint is not valid JSON") from error
        return self._decode(payload)

    @staticmethod
    def _encode(checkpoint: RecoveryCheckpoint) -> dict[str, object]:
        return {
            "positions": [
                {
                    "plan_id": position.plan_id,
                    "quantity": format(position.quantity, "f"),
                    "stop_protection": position.stop_protection.value,
                    "stop_reference": position.stop_reference,
                    "symbol": position.symbol,
                }
                for position in checkpoint.positions
            ],
            "schema_version": checkpoint.schema_version,
        }

    @staticmethod
    def _decode(payload: object) -> RecoveryCheckpoint:
        if not isinstance(payload, dict):
            raise RecoveryCheckpointError("recovery checkpoint must be an object")
        expected_keys = {"positions", "schema_version"}
        if set(payload) != expected_keys or payload["schema_version"] != 2:
            raise RecoveryCheckpointError("recovery checkpoint schema is not supported")
        positions_payload = payload["positions"]
        if not isinstance(positions_payload, list):
            raise RecoveryCheckpointError("recovery checkpoint positions must be a list")
        positions = tuple(
            RecoveryJournal._decode_position(position) for position in positions_payload
        )
        try:
            return RecoveryCheckpoint(positions=positions)
        except ValueError as error:
            raise RecoveryCheckpointError("recovery checkpoint failed validation") from error

    @staticmethod
    def _decode_position(payload: object) -> SimulatedOpenPosition:
        if not isinstance(payload, dict):
            raise RecoveryCheckpointError("recovery position must be an object")
        expected_keys = {"plan_id", "quantity", "stop_protection", "stop_reference", "symbol"}
        if set(payload) != expected_keys:
            raise RecoveryCheckpointError("recovery position schema is not supported")

        plan_id = payload["plan_id"]
        symbol = payload["symbol"]
        quantity = payload["quantity"]
        protection = payload["stop_protection"]
        stop_reference = payload["stop_reference"]
        if (
            not isinstance(plan_id, str)
            or not isinstance(symbol, str)
            or not isinstance(quantity, str)
        ):
            raise RecoveryCheckpointError("recovery position fields are invalid")
        if not isinstance(protection, str) or not isinstance(stop_reference, str | type(None)):
            raise RecoveryCheckpointError("recovery stop evidence is invalid")
        try:
            parsed_quantity = Decimal(quantity)
            parsed_protection = StopProtectionEvidence(protection)
        except (InvalidOperation, ValueError) as error:
            raise RecoveryCheckpointError("recovery position values are invalid") from error
        return SimulatedOpenPosition(
            plan_id=plan_id,
            symbol=symbol,
            quantity=parsed_quantity,
            stop_protection=parsed_protection,
            stop_reference=stop_reference,
        )


class LocalRecoveryCoordinator:
    """Fails closed after a restart until actual local audit and reconciliation checks succeed."""

    def recover_after_restart(
        self,
        checkpoint: RecoveryCheckpoint,
        *,
        audit_repository: AuditRepository,
        reconciliation_snapshot: ReconciliationSnapshot,
        intent_ledger: DurableIntentLedger,
    ) -> RecoveryResult:
        if not isinstance(reconciliation_snapshot, ReconciliationSnapshot):
            raise TypeError("reconciliation_snapshot must be ReconciliationSnapshot")
        if not isinstance(intent_ledger, DurableIntentLedger):
            raise TypeError("intent_ledger must be DurableIntentLedger")
        replay = ReplayRunner().replay(
            audit_repository.list_audit_events(),
            audit_repository.audit_chain_head(),
        )
        if not replay.is_valid:
            return self._hard_halt("AUDIT_CHAIN_OR_REPLAY_INVALID", checkpoint)
        projected_states = audit_repository.projected_states()
        replayed_states = {plan_id: state.value for plan_id, state in replay.plan_states.items()}
        if projected_states != replayed_states:
            return RecoveryResult(
                disposition=RecoveryDisposition.PAUSED,
                local_reconciliation_complete=False,
                stop_protection_invariant_holds=self._all_stops_confirmed(checkpoint),
                entry_authority_enabled=False,
                actions=(RecoveryAction.PAUSE_NEW_ENTRIES, RecoveryAction.RECONCILE_REQUIRED),
                reason="LOCAL_PROJECTION_MISMATCH",
            )
        unresolved_counts = intent_ledger.unresolved_counts()
        if unresolved_counts:
            reason = (
                "DURABLE_UNKNOWN_INTENT"
                if unresolved_counts.get(DurableIntentStatus.UNKNOWN, 0) > 0
                else "DURABLE_UNRESOLVED_INTENT"
            )
            return RecoveryResult(
                disposition=RecoveryDisposition.PAUSED,
                local_reconciliation_complete=False,
                stop_protection_invariant_holds=self._all_stops_confirmed(checkpoint),
                entry_authority_enabled=False,
                actions=(RecoveryAction.PAUSE_NEW_ENTRIES, RecoveryAction.RECONCILE_REQUIRED),
                reason=reason,
            )
        if not self._all_stops_confirmed(checkpoint):
            return self._hard_halt("STOP_PROTECTION_UNCONFIRMED", checkpoint)
        durable_facts = intent_ledger.reconciliation_facts()
        if self._checkpoint_positions(checkpoint) != {
            symbol: abs(quantity) for symbol, quantity in durable_facts.positions_by_symbol.items()
        }:
            return RecoveryResult(
                disposition=RecoveryDisposition.PAUSED,
                local_reconciliation_complete=False,
                stop_protection_invariant_holds=True,
                entry_authority_enabled=False,
                actions=(RecoveryAction.PAUSE_NEW_ENTRIES, RecoveryAction.RECONCILE_REQUIRED),
                reason="DURABLE_POSITION_CHECKPOINT_MISMATCH",
            )
        reconciliation_outcome = audit_repository.derive_reconciliation_outcome(
            intent_ledger=intent_ledger,
            reconciliation_snapshot=reconciliation_snapshot,
        )
        if not reconciliation_outcome.is_clean:
            return RecoveryResult(
                disposition=RecoveryDisposition.PAUSED,
                local_reconciliation_complete=False,
                stop_protection_invariant_holds=self._all_stops_confirmed(checkpoint),
                entry_authority_enabled=False,
                actions=(RecoveryAction.PAUSE_NEW_ENTRIES, RecoveryAction.RECONCILE_REQUIRED),
                reason="RECONCILIATION_MISMATCH",
            )
        return RecoveryResult(
            disposition=RecoveryDisposition.RECONCILED,
            local_reconciliation_complete=True,
            stop_protection_invariant_holds=True,
            entry_authority_enabled=False,
            actions=(),
            reason=None,
        )

    def network_partition(self, checkpoint: RecoveryCheckpoint) -> RecoveryResult:
        if not self._all_stops_confirmed(checkpoint):
            return self._hard_halt("NETWORK_PARTITION_STOP_UNCONFIRMED", checkpoint)
        actions: tuple[RecoveryAction, ...] = (
            RecoveryAction.PAUSE_NEW_ENTRIES,
            RecoveryAction.RECONCILE_REQUIRED,
            RecoveryAction.VERIFY_STOP_PROTECTION,
        )
        return RecoveryResult(
            disposition=RecoveryDisposition.PAUSED,
            local_reconciliation_complete=False,
            stop_protection_invariant_holds=True,
            entry_authority_enabled=False,
            actions=actions,
            reason="NETWORK_PARTITION",
        )

    @staticmethod
    def _all_stops_confirmed(checkpoint: RecoveryCheckpoint) -> bool:
        return all(
            position.stop_protection is StopProtectionEvidence.REMOTE_CONFIRMED
            for position in checkpoint.positions
        )

    @staticmethod
    def _checkpoint_positions(checkpoint: RecoveryCheckpoint) -> dict[str, Decimal]:
        positions_by_symbol: dict[str, Decimal] = {}
        for position in checkpoint.positions:
            positions_by_symbol[position.symbol] = (
                positions_by_symbol.get(position.symbol, ZERO) + position.quantity
            )
        return positions_by_symbol

    def _hard_halt(self, reason: str, checkpoint: RecoveryCheckpoint) -> RecoveryResult:
        return RecoveryResult(
            disposition=RecoveryDisposition.HARD_HALTED,
            local_reconciliation_complete=False,
            stop_protection_invariant_holds=self._all_stops_confirmed(checkpoint),
            entry_authority_enabled=False,
            actions=(
                RecoveryAction.PAUSE_NEW_ENTRIES,
                RecoveryAction.RECONCILE_REQUIRED,
                RecoveryAction.VERIFY_STOP_PROTECTION,
                RecoveryAction.HARD_HALT,
            ),
            reason=reason,
        )
