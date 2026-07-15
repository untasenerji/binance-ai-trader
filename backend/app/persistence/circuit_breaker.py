"""Fail-closed persistence authorization for any future entry-intent path."""

from dataclasses import dataclass

from app.exchange.contracts import ReconciliationOutcome, ReconciliationSnapshot


class PersistenceUnavailable(RuntimeError):
    code = "DATABASE_AUDIT_FAILURE"


@dataclass(frozen=True, slots=True)
class PersistenceRecoveryEvidence:
    """Repository-generated facts required before a persistence pause can be lifted."""

    write_probe_event_id: str
    audit_event_count: int
    audit_last_sequence: int
    audit_last_record_hash: str | None
    replay_valid: bool
    projection_matches_replay: bool
    reconciliation_outcome: ReconciliationOutcome
    unresolved_intent_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.write_probe_event_id:
            raise ValueError("recovery evidence requires a durable write-probe event ID")
        if self.audit_event_count < 1 or self.audit_last_sequence < 1:
            raise ValueError("recovery evidence requires a non-empty audited chain")
        if self.audit_last_sequence != self.audit_event_count:
            raise ValueError("recovery evidence requires a contiguous audited chain")
        if (
            not isinstance(self.audit_last_record_hash, str)
            or len(self.audit_last_record_hash) != 64
        ):
            raise ValueError("recovery evidence requires a non-null audited head hash")
        if not isinstance(self.reconciliation_outcome, ReconciliationOutcome):
            raise TypeError("recovery evidence requires a typed reconciliation outcome")
        if any(
            not isinstance(intent_id, str) or not intent_id
            for intent_id in self.unresolved_intent_ids
        ):
            raise ValueError("unresolved intent IDs must be non-empty strings")

    @property
    def is_complete(self) -> bool:
        return (
            self.replay_valid
            and self.projection_matches_replay
            and self.reconciliation_outcome.is_clean
            and not self.unresolved_intent_ids
        )


@dataclass(slots=True)
class PersistenceCircuitBreaker:
    """Starts closed and can reopen only from repository-derived recovery evidence."""

    halted_reason: str | None = "STARTUP_RECONCILIATION_REQUIRED"

    @property
    def new_entries_allowed(self) -> bool:
        return self.halted_reason is None

    def record_write_failure(self, error: Exception) -> None:
        self.halted_reason = f"{PersistenceUnavailable.code}: {type(error).__name__}"

    def require_new_entries_allowed(self) -> None:
        if not self.new_entries_allowed:
            raise PersistenceUnavailable(self.halted_reason)

    def reset_after_verified_reconciliation(
        self,
        *,
        audit_repository: object,
        intent_ledger: object,
        reconciliation_snapshot: ReconciliationSnapshot,
    ) -> PersistenceRecoveryEvidence:
        from app.persistence.audit import AuditRepository
        from app.simulation.intent_ledger import DurableIntentLedger

        if not isinstance(audit_repository, AuditRepository):
            raise TypeError("audit_repository must be the concrete audit repository")
        if not isinstance(intent_ledger, DurableIntentLedger):
            raise TypeError("intent_ledger must be the concrete durable intent ledger")
        if not isinstance(reconciliation_snapshot, ReconciliationSnapshot):
            raise TypeError("reconciliation_snapshot must be a typed exchange observation")
        try:
            evidence = audit_repository.collect_persistence_recovery_evidence(
                intent_ledger=intent_ledger,
                reconciliation_snapshot=reconciliation_snapshot,
            )
        except Exception as error:
            self.record_write_failure(error)
            raise PersistenceUnavailable("RECOVERY_EVIDENCE_COLLECTION_FAILED") from error
        if not isinstance(evidence, PersistenceRecoveryEvidence):
            raise TypeError("audit repository did not return PersistenceRecoveryEvidence")
        if not evidence.is_complete:
            raise PersistenceUnavailable("RECOVERY_EVIDENCE_INCOMPLETE")
        self.halted_reason = None
        return evidence


@dataclass(frozen=True, slots=True)
class EntryIntentAuthorizationGate:
    """Local-only gate; it deliberately has no order, network, or exchange behavior."""

    persistence_breaker: PersistenceCircuitBreaker

    def authorize_new_entry_intent(self) -> None:
        self.persistence_breaker.require_new_entries_allowed()
