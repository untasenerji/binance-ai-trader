"""Fail-closed persistence authorization for any future entry-intent path."""

from dataclasses import dataclass

from app.exchange.contracts import ReconciliationOutcome


class PersistenceUnavailable(RuntimeError):
    code = "DATABASE_AUDIT_FAILURE"


@dataclass(frozen=True, slots=True)
class PersistenceRecoveryEvidence:
    """Required locally verified facts before a persistence pause can be lifted."""

    durable_write_probe_succeeded: bool
    audit_chain_valid: bool
    replay_valid: bool
    reconciliation_outcome: ReconciliationOutcome
    unresolved_prepared_count: int
    unresolved_submitting_count: int
    unresolved_unknown_count: int

    def __post_init__(self) -> None:
        boolean_fields = (
            self.durable_write_probe_succeeded,
            self.audit_chain_valid,
            self.replay_valid,
        )
        if not all(isinstance(value, bool) for value in boolean_fields):
            raise TypeError("persistence recovery verification fields must be boolean")
        if not isinstance(self.reconciliation_outcome, ReconciliationOutcome):
            raise TypeError("reconciliation_outcome must be ReconciliationOutcome")
        unresolved_counts = (
            self.unresolved_prepared_count,
            self.unresolved_submitting_count,
            self.unresolved_unknown_count,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in unresolved_counts
        ):
            raise ValueError("unresolved intent counts must be non-negative integers")

    @property
    def is_complete(self) -> bool:
        return (
            self.durable_write_probe_succeeded
            and self.audit_chain_valid
            and self.replay_valid
            and self.reconciliation_outcome.is_clean
            and self.unresolved_prepared_count == 0
            and self.unresolved_submitting_count == 0
            and self.unresolved_unknown_count == 0
        )


@dataclass(slots=True)
class PersistenceCircuitBreaker:
    """Starts closed and can only reopen after full, typed reconciliation evidence."""

    halted_reason: str | None = "STARTUP_RECONCILIATION_REQUIRED"

    @property
    def new_entries_allowed(self) -> bool:
        return self.halted_reason is None

    def record_write_failure(self, error: Exception) -> None:
        self.halted_reason = f"{PersistenceUnavailable.code}: {type(error).__name__}"

    def require_new_entries_allowed(self) -> None:
        if not self.new_entries_allowed:
            raise PersistenceUnavailable(self.halted_reason)

    def reset_after_verified_reconciliation(self, evidence: PersistenceRecoveryEvidence) -> None:
        if not isinstance(evidence, PersistenceRecoveryEvidence):
            raise TypeError("evidence must be PersistenceRecoveryEvidence")
        if not evidence.is_complete:
            raise PersistenceUnavailable("RECOVERY_EVIDENCE_INCOMPLETE")
        self.halted_reason = None


@dataclass(frozen=True, slots=True)
class EntryIntentAuthorizationGate:
    """Local-only gate; it deliberately has no order, network, or exchange behavior."""

    persistence_breaker: PersistenceCircuitBreaker

    def authorize_new_entry_intent(self) -> None:
        self.persistence_breaker.require_new_entries_allowed()
