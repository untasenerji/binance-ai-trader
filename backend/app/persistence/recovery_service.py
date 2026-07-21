"""Concrete persistence recovery evidence derivation with no caller health overrides."""

from dataclasses import dataclass

from app.exchange.contracts import ExchangeReconciliationObservationBatch
from app.persistence.audit import AuditRepository
from app.persistence.circuit_breaker import PersistenceRecoveryEvidence
from app.simulation.intent_ledger import DurableIntentLedger


@dataclass(frozen=True, slots=True)
class PersistenceRecoveryService:
    audit_repository: AuditRepository
    intent_ledger: DurableIntentLedger

    def __post_init__(self) -> None:
        if type(self.audit_repository) is not AuditRepository:
            raise TypeError("recovery service requires the concrete production audit repository")
        if type(self.intent_ledger) is not DurableIntentLedger:
            raise TypeError("recovery service requires the concrete durable intent ledger")

    def collect(
        self,
        snapshot: ExchangeReconciliationObservationBatch,
    ) -> PersistenceRecoveryEvidence:
        if type(snapshot) is not ExchangeReconciliationObservationBatch:
            raise TypeError("recovery service requires a concrete observation batch")
        snapshot.require_fresh()
        evidence = AuditRepository.collect_persistence_recovery_evidence(
            self.audit_repository,
            intent_ledger=self.intent_ledger,
            reconciliation_snapshot=snapshot,
        )
        if type(evidence) is not PersistenceRecoveryEvidence:
            raise TypeError("recovery service produced invalid persistence evidence")
        return evidence
