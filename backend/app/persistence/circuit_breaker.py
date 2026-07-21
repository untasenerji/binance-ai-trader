"""Fail-closed persistence authorization for any future entry-intent path."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.exchange.contracts import (
    ExchangeReconciliationObservationBatch,
    ReconciliationOutcome,
)


class PersistenceUnavailable(RuntimeError):
    code = "DATABASE_AUDIT_FAILURE"


@dataclass(frozen=True, slots=True)
class EntryAuthorizationCapability:
    """A short-lived authorization derived from durable evidence for one check."""

    grant_id: str
    issued_at: datetime
    expires_at: datetime
    audit_last_record_hash: str
    reconciliation_fingerprint: str
    exchange_snapshot_fingerprint: str
    account_envelope_fingerprints: tuple[str, ...]
    fingerprint: str
    account_id: str = "v1-primary"
    failure_epoch: int = 0
    recovery_epoch: int = 0
    envelope_version: int = 0
    grant_generation: int = 0

    def __post_init__(self) -> None:
        if not self.grant_id:
            raise ValueError("entry capability grant ID is required")
        if not self.account_id:
            raise ValueError("entry capability account ID is required")
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("entry capability timestamps must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("entry capability expiry must follow issuance")
        for value in (
            self.audit_last_record_hash,
            self.reconciliation_fingerprint,
            self.exchange_snapshot_fingerprint,
            self.fingerprint,
            *self.account_envelope_fingerprints,
        ):
            if not isinstance(value, str) or len(value) != 64:
                raise ValueError("entry capability fingerprints must be SHA-256 values")
        for epoch in (
            self.failure_epoch,
            self.recovery_epoch,
            self.envelope_version,
            self.grant_generation,
        ):
            if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
                raise ValueError("entry capability fencing epochs must be non-negative integers")

    def require_current(self, now: datetime | None = None) -> None:
        checked_at = now or datetime.now(UTC)
        if checked_at.tzinfo is None:
            raise ValueError("entry capability check time must be timezone-aware")
        if checked_at.astimezone(UTC) >= self.expires_at.astimezone(UTC):
            raise PersistenceUnavailable("ENTRY_AUTHORIZATION_CAPABILITY_EXPIRED")


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


class PersistenceCircuitBreaker:
    """Starts closed and can reopen only from repository-derived recovery evidence."""

    __slots__ = ("__durable_revokers", "__halted_reason")

    def __init__(self) -> None:
        self.__halted_reason: str | None = "STARTUP_RECONCILIATION_REQUIRED"
        self.__durable_revokers: list[Callable[[str], None]] = []

    @property
    def halted_reason(self) -> str | None:
        return self.__halted_reason

    @property
    def new_entries_allowed(self) -> bool:
        return self.__halted_reason is None

    def record_write_failure(self, error: Exception) -> None:
        self.__halted_reason = f"{PersistenceUnavailable.code}: {type(error).__name__}"
        for revoke in tuple(self.__durable_revokers):
            try:
                revoke(self.__halted_reason)
            except Exception:
                # The in-memory halt remains closed if durable storage is unavailable.
                continue

    def _bind_durable_revoker(self, revoke: Callable[[str], None]) -> None:
        """Bind a deny-only persistence callback; it cannot issue authorization."""
        if revoke not in self.__durable_revokers:
            self.__durable_revokers.append(revoke)

    def require_new_entries_allowed(self) -> None:
        if not self.new_entries_allowed:
            raise PersistenceUnavailable(self.halted_reason)

    def reset_after_verified_reconciliation(
        self,
        *,
        audit_repository: object,
        intent_ledger: object,
        reconciliation_snapshot: ExchangeReconciliationObservationBatch,
    ) -> PersistenceRecoveryEvidence:
        from app.persistence.audit import AuditRepository
        from app.persistence.recovery_service import PersistenceRecoveryService
        from app.simulation.intent_ledger import DurableIntentLedger

        if type(audit_repository) is not AuditRepository:
            raise TypeError("audit_repository must be the concrete audit repository")
        if type(intent_ledger) is not DurableIntentLedger:
            raise TypeError("intent_ledger must be the concrete durable intent ledger")
        if type(reconciliation_snapshot) is not ExchangeReconciliationObservationBatch:
            raise TypeError("reconciliation_snapshot must be an exchange observation batch")
        reconciliation_snapshot.require_fresh()
        try:
            evidence = PersistenceRecoveryService(
                audit_repository=audit_repository,
                intent_ledger=intent_ledger,
            ).collect(reconciliation_snapshot)
        except Exception as error:
            self.record_write_failure(error)
            raise PersistenceUnavailable("RECOVERY_EVIDENCE_COLLECTION_FAILED") from error
        if not isinstance(evidence, PersistenceRecoveryEvidence):
            raise TypeError("audit repository did not return PersistenceRecoveryEvidence")
        if not evidence.is_complete:
            raise PersistenceUnavailable("RECOVERY_EVIDENCE_INCOMPLETE")
        intent_ledger.issue_entry_authorization_capability(
            evidence=evidence,
            reconciliation_snapshot=reconciliation_snapshot,
        )
        self.__halted_reason = None
        return evidence


@dataclass(frozen=True, slots=True)
class EntryIntentAuthorizationGate:
    """Requests a freshly derived durable capability for every entry check."""

    capability_provider: Callable[[], EntryAuthorizationCapability]

    def authorize_new_entry_intent(self) -> EntryAuthorizationCapability:
        capability = self.capability_provider()
        if type(capability) is not EntryAuthorizationCapability:
            raise PersistenceUnavailable("ENTRY_AUTHORIZATION_CAPABILITY_INVALID")
        capability.require_current()
        return capability
