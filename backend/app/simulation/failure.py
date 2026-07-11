"""Failure handling contract shared by local simulator and later adapters."""

from dataclasses import dataclass
from enum import StrEnum

from app.simulation.models import SimulatedFault


class FailureAction(StrEnum):
    PAUSE_NEW_ENTRIES = "PAUSE_NEW_ENTRIES"
    HARD_HALT = "HARD_HALT"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    RESYNC_CLOCK = "RESYNC_CLOCK"
    CANCEL_PENDING_ENTRIES = "CANCEL_PENDING_ENTRIES"
    EMERGENCY_REDUCE = "EMERGENCY_REDUCE"


@dataclass(slots=True)
class FailureCoordinator:
    new_entries_paused: bool = False
    hard_halted: bool = False
    reconciliation_required: bool = False

    def handle(self, fault: SimulatedFault) -> tuple[FailureAction, ...]:
        if fault is SimulatedFault.UNKNOWN_503:
            self.new_entries_paused = True
            self.reconciliation_required = True
            return (FailureAction.PAUSE_NEW_ENTRIES, FailureAction.RECONCILE_REQUIRED)
        if fault is SimulatedFault.RATE_LIMIT_429:
            self.new_entries_paused = True
            return (FailureAction.PAUSE_NEW_ENTRIES,)
        if fault is SimulatedFault.IP_BAN_418:
            self.new_entries_paused = True
            self.hard_halted = True
            return (FailureAction.HARD_HALT,)
        if fault is SimulatedFault.TIMESTAMP_1021:
            return (FailureAction.RESYNC_CLOCK,)
        if fault is SimulatedFault.DISCONNECT:
            self.new_entries_paused = True
            self.reconciliation_required = True
            return (FailureAction.PAUSE_NEW_ENTRIES, FailureAction.RECONCILE_REQUIRED)
        if fault is SimulatedFault.REJECTED_STOP:
            self.new_entries_paused = True
            self.hard_halted = True
            return (
                FailureAction.CANCEL_PENDING_ENTRIES,
                FailureAction.EMERGENCY_REDUCE,
                FailureAction.HARD_HALT,
            )
        return ()

    def mark_reconciled(self) -> None:
        if not self.hard_halted:
            self.reconciliation_required = False
            self.new_entries_paused = False
