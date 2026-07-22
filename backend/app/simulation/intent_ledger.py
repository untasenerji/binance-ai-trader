"""Durable local intent/outbox evidence for simulator-only submission scenarios."""

import hashlib
import json
import threading
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar
from uuid import uuid4

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.decimal_math import ZERO
from app.domain.types import Direction
from app.exchange.contracts import (
    AdapterQueryReceipt,
    AdapterQueryReceiptStatus,
    AlgoOrderStatus,
    AlgoOrderType,
    ExchangeAlgoOrderObservation,
    ExchangeReconciliationObservationBatch,
    ExpectedStopContract,
    LocalReconciliationState,
    OrderSide,
    ReconciliationOutcome,
    StopQuantitySemantics,
    StopWorkingType,
    reconcile_local_state,
)
from app.persistence.audit import AuditChainHeadSnapshot, verify_hash_chain
from app.persistence.circuit_breaker import (
    EntryAuthorizationCapability,
    EntryIntentAuthorizationGate,
    PersistenceCircuitBreaker,
    PersistenceRecoveryEvidence,
    PersistenceUnavailable,
)
from app.persistence.models import (
    AuditChainHead,
    AuditEvent,
    DurableAccountPortfolioEnvelope,
    DurableAccountSafetyState,
    DurableAccountScopeLock,
    DurableActualRiskPolicy,
    DurableActualRiskPolicyVersion,
    DurableActualRiskState,
    DurableAdapterQueryReceipt,
    DurableEntryAdmissionDecision,
    DurableEntryAuthorizationGrant,
    DurableEntryAuthorizationRevocation,
    DurableEvidenceQuarantine,
    DurableEvidenceQuarantineSource,
    DurableEvidenceQuarantineSourceResolution,
    DurableIntentAbsenceObservation,
    DurableIntentFill,
    DurableOrderIntent,
    DurablePortfolioEnvelopeHead,
    DurablePortfolioEnvelopeSupersession,
    DurableRiskReductionRequirement,
    DurableSimulatedProtection,
    ExchangeFillFactJournal,
    MigrationQuarantineRecord,
    TradePlanProjection,
)
from app.persistence.replay import ReplayRunner
from app.planning.fills import (
    V1_DEFAULT_ACCOUNT_ID,
    AccountPortfolioEnvelope,
    ActualRiskPolicy,
    ExposureSourceState,
    FillEvent,
    FillLedger,
    FillLedgerError,
    FillLedgerReceipt,
    FillObservationSource,
    FillSide,
    PortfolioExposureSlice,
    PortfolioRiskAssessment,
    PositionRiskAssessment,
    RiskReductionRequirement,
    RiskReductionStatus,
    evaluate_simulated_position_risk,
)
from app.simulation.models import OrderRole, SimulatedOrderIntent


class DurableIntentStatus(StrEnum):
    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    UNKNOWN = "UNKNOWN"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCEL_REQUIRED = "CANCEL_REQUIRED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    ABSENT = "ABSENT"
    REJECTED = "REJECTED"


class AbsenceEvidenceSource(StrEnum):
    NORMAL_OPEN_ORDERS = "NORMAL_OPEN_ORDERS"
    ALGO_OPEN_ORDERS = "ALGO_OPEN_ORDERS"
    TRADE_HISTORY = "TRADE_HISTORY"
    USER_STREAM_WATERMARK = "USER_STREAM_WATERMARK"
    POSITION_SNAPSHOT = "POSITION_SNAPSHOT"


class ClientOrderNamespace(StrEnum):
    NORMAL = "NORMAL"
    ALGO = "ALGO"


_UNRESOLVED_STATUSES = frozenset(
    {
        DurableIntentStatus.PREPARED,
        DurableIntentStatus.SUBMITTING,
        DurableIntentStatus.UNKNOWN,
        DurableIntentStatus.CANCEL_REQUIRED,
    }
)
_KNOWN_OUTCOME_STATUSES = frozenset(
    {
        DurableIntentStatus.NEW,
        DurableIntentStatus.PARTIALLY_FILLED,
        DurableIntentStatus.FILLED,
        DurableIntentStatus.CANCELLED,
        DurableIntentStatus.REJECTED,
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        DurableIntentStatus.FILLED,
        DurableIntentStatus.CANCELLED,
        DurableIntentStatus.ABSENT,
        DurableIntentStatus.REJECTED,
    }
)
_REQUIRED_ABSENCE_SOURCES = frozenset(AbsenceEvidenceSource)
_SIMULATED_QUERY_WRITE_CAPABILITY = object()
# A local AdapterQueryReceipt is data, not recovery authority.  The live adapter
# remains locked in this phase; only its future private integration boundary (and
# the explicit test fixture) may cross this capability check.
_ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY = object()
_ENTRY_CAPABILITY_TTL = timedelta(minutes=5)
_ACCOUNT_LOCKS_GUARD = threading.Lock()
_ACCOUNT_LOCKS: dict[str, threading.RLock] = {}


class IntentLedgerError(RuntimeError):
    pass


class UnresolvedEconomicAction(IntentLedgerError):
    pass


class IntentLifecycleError(IntentLedgerError):
    pass


class BoundedAbsenceEvidenceError(IntentLedgerError):
    pass


class DurableRiskPolicyError(IntentLedgerError):
    pass


class FillFactApplyStatus(StrEnum):
    PENDING = "PENDING"
    APPLIED = "APPLIED"
    APPLY_FAILED = "APPLY_FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


@dataclass(frozen=True, slots=True)
class EntryAdmissionDecision:
    """Immutable, centrally persisted permission for one entry intent only."""

    decision_id: str
    account_id: str
    client_order_id: str
    plan_id: str
    risk_policy_id: str
    risk_policy_version: int
    risk_policy_fingerprint: str
    envelope_version: int
    envelope_fingerprint: str
    symbol: str
    side: Direction
    max_quantity: Decimal
    max_notional_usdt: Decimal
    leverage: int
    expires_at: datetime
    grant_generation: int
    failure_epoch: int
    recovery_epoch: int
    query_epoch: int
    grant_id: str
    decision_fingerprint: str = ""

    def __post_init__(self) -> None:
        if not all(
            (
                self.decision_id,
                self.account_id,
                self.client_order_id,
                self.plan_id,
                self.risk_policy_id,
                self.envelope_fingerprint,
                self.symbol,
                self.grant_id,
            )
        ):
            raise ValueError("entry admission decision identity is incomplete")
        if type(self.side) is not Direction:
            raise TypeError("entry admission decision side must be Direction")
        if (
            not isinstance(self.max_quantity, Decimal)
            or not self.max_quantity.is_finite()
            or self.max_quantity <= ZERO
            or not isinstance(self.max_notional_usdt, Decimal)
            or not self.max_notional_usdt.is_finite()
            or self.max_notional_usdt <= ZERO
            or not isinstance(self.leverage, int)
            or isinstance(self.leverage, bool)
            or self.leverage < 1
        ):
            raise ValueError("entry admission decision limits are invalid")
        if self.expires_at.tzinfo is None:
            raise ValueError("entry admission decision expiry must be timezone-aware")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (
                self.risk_policy_version,
                self.envelope_version,
                self.grant_generation,
                self.failure_epoch,
                self.recovery_epoch,
                self.query_epoch,
            )
        ):
            raise ValueError("entry admission decision fencing values are invalid")
        for fingerprint in (self.risk_policy_fingerprint, self.envelope_fingerprint):
            if len(fingerprint) != 64 or any(
                character not in "0123456789abcdef" for character in fingerprint.lower()
            ):
                raise ValueError("entry admission decision fingerprints are invalid")
        object.__setattr__(self, "expires_at", self.expires_at.astimezone(UTC))
        expected = self.expected_fingerprint()
        if self.decision_fingerprint and self.decision_fingerprint != expected:
            raise ValueError("entry admission decision fingerprint is invalid")
        object.__setattr__(self, "decision_fingerprint", expected)

    def expected_fingerprint(self) -> str:
        canonical = json.dumps(
            {
                "account_id": self.account_id,
                "client_order_id": self.client_order_id,
                "decision_id": self.decision_id,
                "envelope_fingerprint": self.envelope_fingerprint,
                "envelope_version": self.envelope_version,
                "expires_at": self.expires_at.isoformat(),
                "failure_epoch": self.failure_epoch,
                "grant_generation": self.grant_generation,
                "grant_id": self.grant_id,
                "leverage": self.leverage,
                "max_notional_usdt": format(self.max_notional_usdt, "f"),
                "max_quantity": format(self.max_quantity, "f"),
                "plan_id": self.plan_id,
                "query_epoch": self.query_epoch,
                "recovery_epoch": self.recovery_epoch,
                "risk_policy_fingerprint": self.risk_policy_fingerprint,
                "risk_policy_id": self.risk_policy_id,
                "risk_policy_version": self.risk_policy_version,
                "side": self.side.value,
                "symbol": self.symbol,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def require_current(self, now: datetime | None = None) -> None:
        checked_at = (now or datetime.now(UTC)).astimezone(UTC)
        if checked_at >= self.expires_at:
            raise PersistenceUnavailable("ENTRY_ADMISSION_DECISION_EXPIRED")


@dataclass(frozen=True, slots=True)
class VerifiedQuarantineResolutionEvidence:
    """Fresh operator-attested reconciliation evidence for one quarantined action."""

    account_id: str
    economic_key: str
    client_order_id: str
    query_reference: str
    observed_at: datetime
    attempt_id: str = "attempt-1"
    source_row_id: str | None = None
    source: str = "operator_verified_reconciliation"
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if (
            self.source != "operator_verified_reconciliation"
            or not self.account_id
            or not self.economic_key
            or not self.client_order_id
            or not self.query_reference
            or not self.attempt_id
        ):
            raise ValueError("verified quarantine evidence needs durable reconciliation identity")
        if self.observed_at.tzinfo is None:
            raise ValueError("verified quarantine evidence timestamp must be timezone-aware")
        if self.source_row_id is not None and not self.source_row_id:
            raise ValueError("verified quarantine source row ID cannot be empty")
        if self.observed_at.astimezone(UTC) > datetime.now(UTC) + timedelta(seconds=5):
            raise ValueError("verified quarantine evidence cannot come from the future")
        expected = self.expected_fingerprint()
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("verified quarantine evidence fingerprint is invalid")
        object.__setattr__(self, "fingerprint", expected)

    def expected_fingerprint(self) -> str:
        canonical = json.dumps(
            {
                "account_id": self.account_id,
                "attempt_id": self.attempt_id,
                "client_order_id": self.client_order_id,
                "economic_key": self.economic_key,
                "observed_at": self.observed_at.astimezone(UTC).isoformat(),
                "query_reference": self.query_reference,
                "source_row_id": self.source_row_id,
                "source": self.source,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class BoundedAbsenceEvidence:
    """A summary derived from durable observations, never an authorization input."""

    client_order_id: str
    economic_key: str
    first_not_found_at_ms: int
    last_not_found_at_ms: int
    not_found_observation_count: int
    attempt_number: int = 1
    client_order_namespace: ClientOrderNamespace = ClientOrderNamespace.NORMAL

    minimum_observation_count: ClassVar[int] = 2
    minimum_observation_window_ms: ClassVar[int] = 1_000

    def __post_init__(self) -> None:
        if not self.client_order_id or not self.economic_key:
            raise ValueError("absence evidence must identify a client order and economic action")
        integer_values = (
            self.first_not_found_at_ms,
            self.last_not_found_at_ms,
            self.not_found_observation_count,
            self.attempt_number,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in integer_values
        ):
            raise ValueError("absence evidence observations must be non-negative integers")
        if self.last_not_found_at_ms < self.first_not_found_at_ms:
            raise ValueError("absence evidence time cannot move backward")
        if self.attempt_number < 1:
            raise ValueError("absence evidence attempt number must be positive")
        if not isinstance(self.client_order_namespace, ClientOrderNamespace):
            raise TypeError("absence evidence namespace must be typed")

    @property
    def is_sufficient(self) -> bool:
        return (
            self.not_found_observation_count >= self.minimum_observation_count
            and self.last_not_found_at_ms - self.first_not_found_at_ms
            >= self.minimum_observation_window_ms
        )


@dataclass(frozen=True, slots=True)
class UnknownIntentObservation:
    """One durable reconciliation fact from a required bounded-absence source."""

    source: AbsenceEvidenceSource
    observed_at_ms: int
    stream_watermark_ms: int
    found: bool
    query_reference: str
    query_client_order_id: str
    query_economic_key: str
    query_started_at_ms: int
    attempt_number: int = 1
    client_order_namespace: ClientOrderNamespace = ClientOrderNamespace.NORMAL
    provenance_fingerprint: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source, AbsenceEvidenceSource):
            raise TypeError("absence observation source must be typed")
        integer_values = (
            self.observed_at_ms,
            self.stream_watermark_ms,
            self.query_started_at_ms,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in integer_values
        ):
            raise ValueError("absence observation timestamps must be non-negative integers")
        if self.stream_watermark_ms < self.observed_at_ms:
            raise ValueError("stream watermark cannot precede the observed snapshot")
        if self.query_started_at_ms > self.observed_at_ms:
            raise ValueError("query start cannot follow its observation")
        if not isinstance(self.found, bool):
            raise TypeError("absence observation found flag must be boolean")
        if (
            not self.query_reference
            or not self.query_client_order_id
            or not self.query_economic_key
        ):
            raise ValueError("absence observations need durable query identity")
        if (
            not isinstance(self.attempt_number, int)
            or isinstance(self.attempt_number, bool)
            or self.attempt_number < 1
        ):
            raise ValueError("absence observation attempt number must be positive")
        if not isinstance(self.client_order_namespace, ClientOrderNamespace):
            raise TypeError("absence observation namespace must be typed")
        expected_fingerprint = self.expected_provenance_fingerprint()
        if self.provenance_fingerprint:
            if self.provenance_fingerprint != expected_fingerprint:
                raise ValueError("absence observation provenance fingerprint is invalid")
        else:
            object.__setattr__(self, "provenance_fingerprint", expected_fingerprint)

    def expected_provenance_fingerprint(self) -> str:
        canonical = json.dumps(
            {
                "attempt_number": self.attempt_number,
                "client_order_namespace": self.client_order_namespace.value,
                "economic_key": self.query_economic_key,
                "found": self.found,
                "observed_at_ms": self.observed_at_ms,
                "query_client_order_id": self.query_client_order_id,
                "query_economic_key": self.query_economic_key,
                "query_reference": self.query_reference,
                "query_started_at_ms": self.query_started_at_ms,
                "source": self.source.value,
                "stream_watermark_ms": self.stream_watermark_ms,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class DurableIntentRecord:
    account_id: str
    client_order_id: str
    economic_key: str
    attempt_number: int
    plan_id: str
    symbol: str
    direction: Direction
    role: OrderRole
    stage_index: int
    quantity: Decimal
    price: Decimal
    filled_quantity: Decimal
    status: DurableIntentStatus
    submitted_at_ms: int | None
    unknown_at_ms: int | None


@dataclass(frozen=True, slots=True)
class DurableAbsenceObservation:
    account_id: str
    client_order_id: str
    economic_key: str
    source: AbsenceEvidenceSource
    query_reference: str
    query_client_order_id: str
    query_economic_key: str
    query_started_at_ms: int
    attempt_number: int
    client_order_namespace: ClientOrderNamespace
    provenance_fingerprint: str
    observed_at_ms: int
    stream_watermark_ms: int
    found: bool


@dataclass(frozen=True, slots=True)
class SimulatedProtectionEvidence:
    account_id: str
    plan_id: str
    stop_intent_reference: str
    reduce_only_exit_intent_reference: str
    protected_position_quantity: Decimal
    stop_intent_ready: bool
    reduce_only_exit_intent_ready: bool

    @property
    def is_ready(self) -> bool:
        return self.stop_intent_ready and self.reduce_only_exit_intent_ready


@dataclass(frozen=True, slots=True)
class DurableReconciliationFacts:
    account_id: str
    positions_by_symbol: dict[str, Decimal]
    normal_order_client_ids: frozenset[str]
    algo_order_client_ids: frozenset[str]
    required_stop_symbols: frozenset[str]
    unresolved_unknown_intent_ids: frozenset[str]
    expected_stop_contracts: tuple[ExpectedStopContract, ...]


@dataclass(frozen=True, slots=True)
class PortfolioEnvelopeHead:
    """The current immutable risk authority for the single supported V1 account."""

    account_id: str
    version: int
    fingerprint: str
    effective_from: datetime
    superseded_by: int | None


class DurableIntentLedger:
    """Durably gates simulator submissions and retains all uncertainty/fill evidence."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        persistence_breaker: PersistenceCircuitBreaker | None = None,
        account_id: str = V1_DEFAULT_ACCOUNT_ID,
    ) -> None:
        if account_id != V1_DEFAULT_ACCOUNT_ID:
            raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
        self._session_factory = session_factory
        self._account_id = account_id
        self._persistence_breaker = (
            persistence_breaker if persistence_breaker is not None else PersistenceCircuitBreaker()
        )
        self._entry_gate = EntryIntentAuthorizationGate(self._derive_entry_authorization_capability)
        self._persistence_breaker._bind_durable_revoker(  # noqa: SLF001
            self._record_entry_authorization_revocation
        )

    def begin_adapter_query_receipt(self, receipt: AdapterQueryReceipt) -> None:
        """Reject public receipt writes: data construction must not grant recovery authority."""
        del receipt
        raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_PUBLIC_WRITE_FORBIDDEN")

    def complete_adapter_query_receipt(self, receipt: AdapterQueryReceipt) -> None:
        """Reject public receipt writes: a completed local receipt is not adapter provenance."""
        del receipt
        raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_PUBLIC_WRITE_FORBIDDEN")

    def record_adapter_query_receipt(self, receipt: AdapterQueryReceipt) -> None:
        """Reject the former public completion shortcut for the same reason."""
        del receipt
        raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_PUBLIC_WRITE_FORBIDDEN")

    def _record_adapter_query_receipt_from_authenticated_adapter(
        self,
        receipt: AdapterQueryReceipt,
        *,
        capability: object,
    ) -> None:
        """Persist a receipt only from the non-public adapter integration boundary."""
        if capability is not _ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY:
            raise TypeError("adapter receipts require the authenticated adapter boundary")
        if type(receipt) is not AdapterQueryReceipt:
            raise TypeError("adapter receipt must be an exact AdapterQueryReceipt")
        if receipt.account_id != self._account_id:
            raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_ACCOUNT_MISMATCH")
        if receipt.status is AdapterQueryReceiptStatus.STARTED:
            self._begin_adapter_query_receipt_from_authenticated_adapter(receipt)
            return
        if receipt.status is AdapterQueryReceiptStatus.COMPLETED:
            self._complete_adapter_query_receipt_from_authenticated_adapter(receipt)
            return
        if receipt.status is AdapterQueryReceiptStatus.FAILED:
            self._fail_adapter_query_receipt_from_authenticated_adapter(receipt)
            return
        raise ValueError("adapter query receipt status is unsupported")

    def _begin_adapter_query_receipt_from_authenticated_adapter(
        self,
        receipt: AdapterQueryReceipt,
    ) -> None:
        """Durably record the pre-query receipt before observations exist."""
        with self._local_account_lock(self._account_id):
            with self._session_factory.begin() as session:
                self._account_safety_state_for_update(session, self._account_id)
                existing = session.scalar(
                    select(DurableAdapterQueryReceipt).where(
                        DurableAdapterQueryReceipt.receipt_id == receipt.receipt_id
                    )
                )
                if existing is not None:
                    if not self._adapter_receipt_identity_matches(existing, receipt):
                        raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_SEMANTIC_CONFLICT")
                    if existing.status not in {
                        AdapterQueryReceiptStatus.STARTED.value,
                        AdapterQueryReceiptStatus.COMPLETED.value,
                    }:
                        raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_STATUS_INVALID")
                    return
                session.add(
                    DurableAdapterQueryReceipt(
                        receipt_id=receipt.receipt_id,
                        account_id=receipt.account_id,
                        adapter_instance_id=receipt.adapter_instance_id,
                        query_id=receipt.query_id,
                        correlation_id=receipt.correlation_id,
                        query_epoch=receipt.query_epoch,
                        requested_at=receipt.requested_at,
                        completed_at=None,
                        server_time=None,
                        query_type=receipt.query_type,
                        response_fingerprint=None,
                        status=AdapterQueryReceiptStatus.STARTED.value,
                        receipt_fingerprint=receipt.receipt_fingerprint,
                    )
                )

    def _complete_adapter_query_receipt_from_authenticated_adapter(
        self,
        receipt: AdapterQueryReceipt,
    ) -> None:
        """Complete a pre-existing receipt with the adapter response fingerprint."""
        receipt.require_completed()
        with self._local_account_lock(self._account_id):
            with self._session_factory.begin() as session:
                self._account_safety_state_for_update(session, self._account_id)
                row = session.scalar(
                    select(DurableAdapterQueryReceipt).where(
                        DurableAdapterQueryReceipt.receipt_id == receipt.receipt_id
                    )
                )
                if row is None:
                    raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_START_MISSING")
                if not self._adapter_receipt_identity_matches(row, receipt):
                    raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_SEMANTIC_CONFLICT")
                if row.status == AdapterQueryReceiptStatus.COMPLETED.value:
                    if not self._durable_receipt_matches(row, receipt):
                        raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_SEMANTIC_CONFLICT")
                    return
                if row.status != AdapterQueryReceiptStatus.STARTED.value:
                    raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_STATUS_INVALID")
                row.completed_at = receipt.completed_at
                row.server_time = receipt.server_time
                row.response_fingerprint = receipt.response_fingerprint
                row.status = receipt.status.value
                row.receipt_fingerprint = receipt.receipt_fingerprint

    def _fail_adapter_query_receipt_from_authenticated_adapter(
        self,
        receipt: AdapterQueryReceipt,
    ) -> None:
        """Retain a failed query lifecycle without allowing it to become evidence."""
        with self._local_account_lock(self._account_id):
            with self._session_factory.begin() as session:
                self._account_safety_state_for_update(session, self._account_id)
                row = session.scalar(
                    select(DurableAdapterQueryReceipt).where(
                        DurableAdapterQueryReceipt.receipt_id == receipt.receipt_id
                    )
                )
                if row is None:
                    raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_START_MISSING")
                if not self._adapter_receipt_identity_matches(row, receipt):
                    raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_SEMANTIC_CONFLICT")
                if row.status == AdapterQueryReceiptStatus.FAILED.value:
                    if row.receipt_fingerprint != receipt.receipt_fingerprint:
                        raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_SEMANTIC_CONFLICT")
                    return
                if row.status != AdapterQueryReceiptStatus.STARTED.value:
                    raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_STATUS_INVALID")
                row.status = receipt.status.value
                row.receipt_fingerprint = receipt.receipt_fingerprint

    @staticmethod
    def _adapter_receipt_identity_matches(
        row: DurableAdapterQueryReceipt,
        receipt: AdapterQueryReceipt,
    ) -> bool:
        return (
            row.account_id == receipt.account_id
            and row.adapter_instance_id == receipt.adapter_instance_id
            and row.query_id == receipt.query_id
            and row.correlation_id == receipt.correlation_id
            and row.query_epoch == receipt.query_epoch
            and DurableIntentLedger._as_utc(row.requested_at) == receipt.requested_at
            and row.query_type == receipt.query_type
        )

    @classmethod
    def _durable_receipt_matches(
        cls,
        row: DurableAdapterQueryReceipt,
        receipt: AdapterQueryReceipt,
    ) -> bool:
        return (
            cls._adapter_receipt_identity_matches(row, receipt)
            and row.status == AdapterQueryReceiptStatus.COMPLETED.value
            and row.completed_at is not None
            and row.server_time is not None
            and cls._as_utc(row.completed_at) == receipt.completed_at
            and cls._as_utc(row.server_time) == receipt.server_time
            and row.response_fingerprint == receipt.response_fingerprint
            and row.receipt_fingerprint == receipt.receipt_fingerprint
        )

    def validate_reconciliation_observation(
        self,
        batch: ExchangeReconciliationObservationBatch,
    ) -> None:
        if type(batch) is not ExchangeReconciliationObservationBatch:
            raise TypeError("reconciliation requires an exact observation batch")
        batch.require_fresh()
        with self._session_factory() as session:
            self._validate_durable_adapter_query_receipt(session, batch)

    @classmethod
    def _validate_durable_adapter_query_receipt(
        cls,
        session: Session,
        batch: ExchangeReconciliationObservationBatch,
    ) -> DurableAdapterQueryReceipt:
        receipt = batch.query_receipt
        if type(receipt) is not AdapterQueryReceipt:
            raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_MISSING")
        try:
            receipt.require_completed()
        except ValueError as error:
            raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_INCOMPLETE") from error
        if (
            receipt.account_id != batch.account_id
            or receipt.query_epoch != batch.query_epoch
            or receipt.correlation_id != batch.correlation_id
            or receipt.completed_at != batch.fetched_at
            or receipt.server_time != batch.server_time
            or receipt.response_fingerprint != batch.response_fingerprint
            or receipt.query_type != "reconciliation"
        ):
            raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_BATCH_MISMATCH")
        row = session.scalar(
            select(DurableAdapterQueryReceipt).where(
                DurableAdapterQueryReceipt.receipt_id == receipt.receipt_id,
                DurableAdapterQueryReceipt.account_id == batch.account_id,
            )
        )
        if row is None:
            raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_MISSING")
        if not cls._durable_receipt_matches(row, receipt):
            raise PersistenceUnavailable("ADAPTER_QUERY_RECEIPT_TAMPERED")
        return row

    def reopen_after_restart(self) -> "DurableIntentLedger":
        """Return a fresh local ledger instance over the same durable store."""
        reopened = DurableIntentLedger(
            self._session_factory,
            persistence_breaker=PersistenceCircuitBreaker(),
            account_id=self._account_id,
        )
        with self._local_account_lock(self._account_id):
            with self._session_factory.begin() as session:
                self._account_safety_state_for_update(session, self._account_id)
                self._fence_filled_active_intents(session, self._account_id)
        reopened._reapply_unapplied_fill_facts()
        return reopened

    def _reapply_unapplied_fill_facts(self) -> None:
        """Startup recovery retries durable facts without trusting process memory."""
        with self._session_factory() as session:
            facts = tuple(
                session.scalars(
                    select(ExchangeFillFactJournal)
                    .where(
                        ExchangeFillFactJournal.account_id == self._account_id,
                        ExchangeFillFactJournal.apply_status != FillFactApplyStatus.APPLIED.value,
                    )
                    .order_by(ExchangeFillFactJournal.id.asc())
                )
            )
        for fact in facts:
            try:
                self.record_fill(
                    self._fill_event_from_fact(fact),
                    materialize_simulated_protection=fact.materialize_simulated_protection,
                )
            except Exception:
                # The fact remains RECOVERY_REQUIRED and the durable entry gate
                # stays closed; later recovery must not lose it by skipping it.
                continue

    @staticmethod
    def _local_account_lock(account_id: str) -> threading.RLock:
        with _ACCOUNT_LOCKS_GUARD:
            lock = _ACCOUNT_LOCKS.get(account_id)
            if lock is None:
                lock = threading.RLock()
                _ACCOUNT_LOCKS[account_id] = lock
            return lock

    def _require_v1_account(self, account_id: str) -> None:
        if account_id != self._account_id or account_id != V1_DEFAULT_ACCOUNT_ID:
            raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")

    @classmethod
    def _account_safety_state_for_update(
        cls,
        session: Session,
        account_id: str,
    ) -> DurableAccountSafetyState:
        if account_id != V1_DEFAULT_ACCOUNT_ID:
            raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
        statement = select(DurableAccountSafetyState).where(
            DurableAccountSafetyState.account_id == account_id
        )
        if session.get_bind().dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:account_id))"),
                {"account_id": account_id},
            )
            statement = statement.with_for_update()
        state = session.scalar(statement)
        if state is None:
            session.add(
                DurableAccountSafetyState(
                    account_id=account_id,
                    failure_epoch=0,
                    recovery_epoch=0,
                    envelope_version=0,
                    envelope_fingerprint=None,
                    grant_generation=0,
                    recovery_required=True,
                )
            )
            session.add(DurableAccountScopeLock(account_id=account_id, lock_generation=0))
            session.flush()
            state = session.get(DurableAccountSafetyState, account_id)
        if state is None:
            raise PersistenceUnavailable("ACCOUNT_SAFETY_STATE_MISSING")
        lock_statement = select(DurableAccountScopeLock).where(
            DurableAccountScopeLock.account_id == account_id
        )
        if session.get_bind().dialect.name == "postgresql":
            lock_statement = lock_statement.with_for_update()
        lock_row = session.scalar(lock_statement)
        if lock_row is None:
            session.add(DurableAccountScopeLock(account_id=account_id, lock_generation=0))
            session.flush()
        return state

    @staticmethod
    def _lock_account_risk_rows(session: Session, account_id: str) -> None:
        """Lock every account-wide risk input before a fill is projected and committed."""
        queries = (
            select(DurablePortfolioEnvelopeHead).where(
                DurablePortfolioEnvelopeHead.account_id == account_id
            ),
            select(DurableActualRiskPolicy).where(DurableActualRiskPolicy.account_id == account_id),
            select(DurableActualRiskState).where(DurableActualRiskState.account_id == account_id),
            select(DurableSimulatedProtection).where(
                DurableSimulatedProtection.account_id == account_id
            ),
            select(DurableRiskReductionRequirement).where(
                DurableRiskReductionRequirement.account_id == account_id
            ),
            select(DurableOrderIntent).where(DurableOrderIntent.account_id == account_id),
            select(DurableIntentFill).where(DurableIntentFill.account_id == account_id),
            select(ExchangeFillFactJournal).where(ExchangeFillFactJournal.account_id == account_id),
        )
        for statement in queries:
            if session.get_bind().dialect.name == "postgresql":
                statement = statement.with_for_update()
            tuple(session.scalars(statement))

    @classmethod
    def _unresolved_quarantine_count(
        cls,
        session: Session,
        *,
        account_id: str,
        economic_key: str | None = None,
    ) -> int:
        statement = (
            select(DurableEvidenceQuarantine.id)
            .outerjoin(
                DurableEvidenceQuarantineSource,
                DurableEvidenceQuarantineSource.quarantine_id
                == DurableEvidenceQuarantine.quarantine_id,
            )
            .outerjoin(
                DurableEvidenceQuarantineSourceResolution,
                DurableEvidenceQuarantineSourceResolution.source_id
                == DurableEvidenceQuarantineSource.id,
            )
            .where(
                DurableEvidenceQuarantine.account_id == account_id,
                (
                    DurableEvidenceQuarantineSource.id.is_(None)
                    | DurableEvidenceQuarantineSourceResolution.id.is_(None)
                ),
            )
            .distinct()
        )
        if economic_key is not None:
            statement = statement.where(DurableEvidenceQuarantine.economic_key == economic_key)
        durable_count = len(tuple(session.scalars(statement)))
        if economic_key is not None:
            return durable_count
        migrated_source_row_ids = frozenset(
            session.scalars(
                select(DurableEvidenceQuarantineSource.source_row_id).where(
                    DurableEvidenceQuarantineSource.source_kind == "migration_quarantine"
                )
            )
        )
        migration_count = sum(
            str(row_id) not in migrated_source_row_ids
            for row_id in session.scalars(
                select(MigrationQuarantineRecord.id).where(
                    MigrationQuarantineRecord.reconciliation_required.is_(True)
                )
            )
        )
        return durable_count + migration_count

    @classmethod
    def _require_no_unresolved_quarantine(
        cls,
        session: Session,
        *,
        account_id: str,
        economic_key: str | None = None,
    ) -> None:
        if cls._unresolved_quarantine_count(
            session,
            account_id=account_id,
            economic_key=economic_key,
        ):
            raise PersistenceUnavailable("UNRESOLVED_QUARANTINE")

    def record_evidence_quarantine(
        self,
        *,
        account_id: str,
        economic_key: str,
        client_order_id: str,
        query_reference: str,
        provenance_fingerprint: str,
        reason: str,
        attempt_id: str = "attempt-1",
        evidence: dict[str, object] | None = None,
    ) -> str:
        """Persist an immutable deny record without altering original provenance."""
        self._require_v1_account(account_id)
        if (
            not economic_key
            or not client_order_id
            or not query_reference
            or not reason
            or not attempt_id
            or len(provenance_fingerprint) != 64
        ):
            raise ValueError("quarantine requires complete original economic provenance")
        with self._local_account_lock(account_id):
            with self._session_factory.begin() as session:
                safety_state = self._account_safety_state_for_update(session, account_id)
                existing = session.scalar(
                    select(DurableEvidenceQuarantine).where(
                        DurableEvidenceQuarantine.account_id == account_id,
                        DurableEvidenceQuarantine.economic_key == economic_key,
                        DurableEvidenceQuarantine.provenance_fingerprint == provenance_fingerprint,
                    )
                )
                source_row_id = self._quarantine_source_row_id(
                    account_id=account_id,
                    economic_key=economic_key,
                    client_order_id=client_order_id,
                    attempt_id=attempt_id,
                    query_reference=query_reference,
                    provenance_fingerprint=provenance_fingerprint,
                    reason=reason,
                    evidence=evidence or {},
                )
                if existing is not None:
                    source_exists = session.scalar(
                        select(DurableEvidenceQuarantineSource.id).where(
                            DurableEvidenceQuarantineSource.source_kind == "runtime_quarantine",
                            DurableEvidenceQuarantineSource.source_table == "runtime_evidence",
                            DurableEvidenceQuarantineSource.source_row_id == source_row_id,
                        )
                    )
                    if source_exists is None:
                        session.add(
                            DurableEvidenceQuarantineSource(
                                quarantine_id=existing.quarantine_id,
                                account_id=account_id,
                                source_kind="runtime_quarantine",
                                source_table="runtime_evidence",
                                source_row_id=source_row_id,
                                source_identity=client_order_id,
                                economic_key=economic_key,
                                client_order_id=client_order_id,
                                attempt_id=attempt_id,
                                query_reference=query_reference,
                                provenance_fingerprint=provenance_fingerprint,
                                evidence=evidence or {},
                            )
                        )
                        safety_state.grant_generation += 1
                        safety_state.recovery_required = True
                        safety_state.updated_at = datetime.now(UTC)
                    return existing.quarantine_id
                quarantine_id = f"quarantine-{uuid4().hex}"
                session.add(
                    DurableEvidenceQuarantine(
                        quarantine_id=quarantine_id,
                        account_id=account_id,
                        economic_key=economic_key,
                        client_order_id=client_order_id,
                        query_reference=query_reference,
                        provenance_fingerprint=provenance_fingerprint,
                        reason=reason,
                        evidence=evidence or {},
                    )
                )
                session.add(
                    DurableEvidenceQuarantineSource(
                        quarantine_id=quarantine_id,
                        account_id=account_id,
                        source_kind="runtime_quarantine",
                        source_table="runtime_evidence",
                        source_row_id=source_row_id,
                        source_identity=client_order_id,
                        economic_key=economic_key,
                        client_order_id=client_order_id,
                        attempt_id=attempt_id,
                        query_reference=query_reference,
                        provenance_fingerprint=provenance_fingerprint,
                        evidence=evidence or {},
                    )
                )
                safety_state.grant_generation += 1
                safety_state.recovery_required = True
                safety_state.updated_at = datetime.now(UTC)
                return quarantine_id

    @staticmethod
    def _quarantine_source_row_id(
        *,
        account_id: str,
        economic_key: str,
        client_order_id: str,
        attempt_id: str,
        query_reference: str,
        provenance_fingerprint: str,
        reason: str,
        evidence: dict[str, object],
    ) -> str:
        canonical = json.dumps(
            {
                "account_id": account_id,
                "attempt_id": attempt_id,
                "client_order_id": client_order_id,
                "economic_key": economic_key,
                "evidence": evidence,
                "provenance_fingerprint": provenance_fingerprint,
                "query_reference": query_reference,
                "reason": reason,
            },
            default=str,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def resolve_evidence_quarantine(
        self,
        quarantine_id: str,
        *,
        operator_id: str,
        verified_evidence: VerifiedQuarantineResolutionEvidence | None = None,
        verified_evidence_fingerprint: str | None = None,
    ) -> str:
        if not quarantine_id or not operator_id:
            raise ValueError("quarantine resolution requires an operator identity")
        if verified_evidence is None:
            raise ValueError("quarantine resolution requires typed new verified evidence")
        if type(verified_evidence) is not VerifiedQuarantineResolutionEvidence:
            raise TypeError("quarantine resolution requires exact verified evidence")
        if verified_evidence_fingerprint is not None:
            raise ValueError("quarantine resolution does not accept caller-supplied fingerprints")
        with self._local_account_lock(self._account_id):
            with self._session_factory.begin() as session:
                quarantine = session.scalar(
                    select(DurableEvidenceQuarantine).where(
                        DurableEvidenceQuarantine.quarantine_id == quarantine_id
                    )
                )
                if quarantine is None:
                    raise KeyError(f"unknown quarantine {quarantine_id}")
                self._require_v1_account(quarantine.account_id)
                if (
                    verified_evidence.account_id != quarantine.account_id
                    or verified_evidence.economic_key != quarantine.economic_key
                ):
                    raise ValueError(
                        "quarantine resolution requires new verified evidence "
                        "for the original identity"
                    )
                source_statement = select(DurableEvidenceQuarantineSource).where(
                    DurableEvidenceQuarantineSource.quarantine_id == quarantine_id,
                    DurableEvidenceQuarantineSource.account_id == quarantine.account_id,
                    DurableEvidenceQuarantineSource.economic_key == verified_evidence.economic_key,
                    DurableEvidenceQuarantineSource.client_order_id
                    == verified_evidence.client_order_id,
                    DurableEvidenceQuarantineSource.attempt_id == verified_evidence.attempt_id,
                )
                if verified_evidence.source_row_id is not None:
                    source_statement = source_statement.where(
                        DurableEvidenceQuarantineSource.source_row_id
                        == verified_evidence.source_row_id
                    )
                sources = tuple(session.scalars(source_statement))
                if not sources:
                    raise ValueError("quarantine resolution source identity does not match")
                if len(sources) != 1:
                    raise ValueError("quarantine resolution requires one exact source row identity")
                source = sources[0]
                if verified_evidence.fingerprint == source.provenance_fingerprint or (
                    source.query_reference is not None
                    and verified_evidence.query_reference == source.query_reference
                ):
                    raise ValueError(
                        "quarantine resolution requires new verified evidence "
                        "for the original source"
                    )
                safety_state = self._account_safety_state_for_update(session, quarantine.account_id)
                existing = session.scalar(
                    select(DurableEvidenceQuarantineSourceResolution).where(
                        DurableEvidenceQuarantineSourceResolution.source_id == source.id
                    )
                )
                if existing is not None:
                    return existing.resolution_id
                resolution_id = f"quarantine-resolution-{uuid4().hex}"
                session.add(
                    DurableEvidenceQuarantineSourceResolution(
                        resolution_id=resolution_id,
                        source_id=source.id,
                        quarantine_id=quarantine_id,
                        account_id=quarantine.account_id,
                        operator_id=operator_id,
                        verified_evidence_source=verified_evidence.source,
                        verified_query_reference=verified_evidence.query_reference,
                        verified_evidence_fingerprint=verified_evidence.fingerprint,
                        verified_at=verified_evidence.observed_at.astimezone(UTC),
                    )
                )
                safety_state.grant_generation += 1
                safety_state.recovery_required = True
                safety_state.updated_at = datetime.now(UTC)
                return resolution_id

    def entry_authorization_capability(self) -> EntryAuthorizationCapability:
        """Expose a re-derived capability only for gate verification tests and callers."""
        return self._derive_entry_authorization_capability()

    def publish_portfolio_envelope_head(
        self,
        envelope: AccountPortfolioEnvelope,
    ) -> PortfolioEnvelopeHead:
        if type(envelope) is not AccountPortfolioEnvelope:
            raise TypeError("portfolio envelope head requires an exact envelope")
        self._require_v1_account(envelope.account_id)
        with self._local_account_lock(envelope.account_id):
            with self._session_factory.begin() as session:
                self._account_safety_state_for_update(session, envelope.account_id)
                self._register_portfolio_envelope(session, envelope)
                row = session.scalar(
                    select(DurablePortfolioEnvelopeHead)
                    .where(
                        DurablePortfolioEnvelopeHead.account_id == envelope.account_id,
                        DurablePortfolioEnvelopeHead.version == envelope.version,
                    )
                    .limit(1)
                )
                if row is None:
                    raise DurableRiskPolicyError("portfolio envelope head was not persisted")
                return PortfolioEnvelopeHead(
                    account_id=row.account_id,
                    version=row.version,
                    fingerprint=row.envelope_fingerprint,
                    effective_from=self._as_utc(row.effective_from),
                    superseded_by=row.superseded_by,
                )

    def issue_entry_authorization_capability(
        self,
        *,
        evidence: PersistenceRecoveryEvidence,
        reconciliation_snapshot: ExchangeReconciliationObservationBatch,
    ) -> EntryAuthorizationCapability:
        """Persist a time-bounded grant only after rechecking repository facts."""
        if type(evidence) is not PersistenceRecoveryEvidence:
            raise TypeError("entry authorization requires typed recovery evidence")
        if type(reconciliation_snapshot) is not ExchangeReconciliationObservationBatch:
            raise TypeError("entry authorization requires an exchange observation batch")
        self.validate_reconciliation_observation(reconciliation_snapshot)
        reconciliation_snapshot.require_fresh()
        if reconciliation_snapshot.account_id != self._account_id:
            raise PersistenceUnavailable("V1_SECOND_ACCOUNT_UNSUPPORTED")
        if not evidence.is_complete:
            raise PersistenceUnavailable("RECOVERY_EVIDENCE_INCOMPLETE")

        issued_at = datetime.now(UTC)
        expires_at = issued_at + _ENTRY_CAPABILITY_TTL
        grant_id = f"entry-capability-{uuid4().hex}"
        try:
            with self._session_factory.begin() as session:
                safety_state = self._account_safety_state_for_update(session, self._account_id)
                receipt = self._validate_durable_adapter_query_receipt(
                    session,
                    reconciliation_snapshot,
                )
                self._require_no_unresolved_quarantine(
                    session,
                    account_id=self._account_id,
                )
                events, head, replay_valid, projection_matches = self._audit_replay_state(session)
                if (
                    not replay_valid
                    or not projection_matches
                    or head.event_count != evidence.audit_event_count
                    or head.last_sequence != evidence.audit_last_sequence
                    or head.last_record_hash != evidence.audit_last_record_hash
                ):
                    raise PersistenceUnavailable("RECOVERY_AUDIT_STATE_CHANGED")
                probe = next(
                    (
                        event
                        for event in events
                        if event.event_id == evidence.write_probe_event_id
                        and event.source == "persistence"
                        and event.event_type == "persistence_recovery_probe"
                    ),
                    None,
                )
                if probe is None:
                    raise PersistenceUnavailable("RECOVERY_WRITE_PROBE_MISSING")
                prior_grant = session.scalar(
                    select(DurableEntryAuthorizationGrant)
                    .where(DurableEntryAuthorizationGrant.account_id == self._account_id)
                    .order_by(DurableEntryAuthorizationGrant.id.desc())
                    .limit(1)
                )
                if prior_grant is not None:
                    prior_probe_sequence = session.scalar(
                        select(AuditEvent.chain_sequence).where(
                            AuditEvent.event_id == prior_grant.recovery_probe_event_id
                        )
                    )
                    if prior_probe_sequence is None or probe.chain_sequence <= prior_probe_sequence:
                        raise PersistenceUnavailable("RECOVERY_WRITE_PROBE_NOT_FRESH")

                facts = self._reconciliation_facts_from_session(session)
                if facts.unresolved_unknown_intent_ids:
                    raise PersistenceUnavailable("RECOVERY_UNRESOLVED_INTENTS_PRESENT")
                local = self._local_reconciliation_state(
                    facts,
                    audit_chain_valid=True,
                    replay_valid=replay_valid,
                )
                outcome = reconcile_local_state(
                    local=local,
                    snapshot=reconciliation_snapshot.snapshot,
                )
                if not outcome.is_clean:
                    raise PersistenceUnavailable("RECOVERY_RECONCILIATION_NOT_CLEAN")
                reconciliation_fingerprint = self._reconciliation_fingerprint(outcome)
                if reconciliation_fingerprint != self._reconciliation_fingerprint(
                    evidence.reconciliation_outcome
                ):
                    raise PersistenceUnavailable("RECOVERY_RECONCILIATION_CHANGED")

                safety_state.recovery_epoch += 1
                safety_state.grant_generation += 1
                safety_state.recovery_required = False
                safety_state.updated_at = issued_at

                reconciliation_snapshot.require_fresh(issued_at)
                snapshot_payload = self._snapshot_payload(reconciliation_snapshot)
                snapshot_fingerprint = self._canonical_fingerprint(snapshot_payload)
                local_state_payload = self._local_state_payload(facts)
                local_state_fingerprint = self._canonical_fingerprint(local_state_payload)
                grant_payload = {
                    "account_id": self._account_id,
                    "audit_event_count": head.event_count,
                    "audit_last_record_hash": head.last_record_hash,
                    "audit_last_sequence": head.last_sequence,
                    "envelope_version": safety_state.envelope_version,
                    "exchange_snapshot_fingerprint": snapshot_fingerprint,
                    "failure_epoch": safety_state.failure_epoch,
                    "grant_generation": safety_state.grant_generation,
                    "query_correlation_id": reconciliation_snapshot.correlation_id,
                    "query_epoch": reconciliation_snapshot.query_epoch,
                    "query_receipt_id": receipt.receipt_id,
                    "query_response_fingerprint": reconciliation_snapshot.response_fingerprint,
                    "expires_at": expires_at.isoformat(),
                    "grant_id": grant_id,
                    "issued_at": issued_at.isoformat(),
                    "local_state_fingerprint": local_state_fingerprint,
                    "projection_matches_replay": projection_matches,
                    "reconciliation_clean": outcome.is_clean,
                    "reconciliation_fingerprint": reconciliation_fingerprint,
                    "recovery_epoch": safety_state.recovery_epoch,
                    "recovery_probe_event_id": evidence.write_probe_event_id,
                    "recovery_status": "VERIFIED",
                    "replay_valid": replay_valid,
                    "unresolved_intent_count": len(facts.unresolved_unknown_intent_ids),
                }
                capability_fingerprint = self._canonical_fingerprint(grant_payload)
                session.add(
                    DurableEntryAuthorizationGrant(
                        account_id=self._account_id,
                        failure_epoch=safety_state.failure_epoch,
                        recovery_epoch=safety_state.recovery_epoch,
                        envelope_version=safety_state.envelope_version,
                        grant_generation=safety_state.grant_generation,
                        grant_id=grant_id,
                        recovery_probe_event_id=evidence.write_probe_event_id,
                        recovery_status="VERIFIED",
                        issued_at=issued_at,
                        expires_at=expires_at,
                        audit_event_count=head.event_count,
                        audit_last_sequence=head.last_sequence,
                        audit_last_record_hash=head.last_record_hash,
                        replay_valid=replay_valid,
                        projection_matches_replay=projection_matches,
                        unresolved_intent_count=len(facts.unresolved_unknown_intent_ids),
                        reconciliation_clean=outcome.is_clean,
                        reconciliation_fingerprint=reconciliation_fingerprint,
                        exchange_snapshot=snapshot_payload,
                        exchange_snapshot_fingerprint=snapshot_fingerprint,
                        query_receipt_id=receipt.receipt_id,
                        query_epoch=reconciliation_snapshot.query_epoch,
                        query_correlation_id=reconciliation_snapshot.correlation_id,
                        query_response_fingerprint=reconciliation_snapshot.response_fingerprint,
                        local_state_snapshot=local_state_payload,
                        local_state_fingerprint=local_state_fingerprint,
                        capability_fingerprint=capability_fingerprint,
                    )
                )
                session.flush()
        except PersistenceUnavailable:
            raise
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise
        return self._derive_entry_authorization_capability()

    def _derive_entry_authorization_capability(self) -> EntryAuthorizationCapability:
        """Revalidate every durable authority dimension for the current entry check."""
        with self._session_factory() as session:
            grant = session.scalar(
                select(DurableEntryAuthorizationGrant)
                .where(DurableEntryAuthorizationGrant.account_id == self._account_id)
                .order_by(DurableEntryAuthorizationGrant.id.desc())
                .limit(1)
            )
            if grant is None:
                raise PersistenceUnavailable("DURABLE_ENTRY_AUTHORIZATION_MISSING")
            revoked = session.scalar(
                select(DurableEntryAuthorizationRevocation.id)
                .where(DurableEntryAuthorizationRevocation.grant_id == grant.grant_id)
                .limit(1)
            )
            if revoked is not None:
                raise PersistenceUnavailable("DURABLE_ENTRY_AUTHORIZATION_REVOKED")
            safety_state = session.get(DurableAccountSafetyState, self._account_id)
            if safety_state is None:
                raise PersistenceUnavailable("ACCOUNT_SAFETY_STATE_MISSING")
            self._require_no_unresolved_quarantine(session, account_id=self._account_id)
            if (
                safety_state.recovery_required
                or grant.account_id != self._account_id
                or grant.failure_epoch != safety_state.failure_epoch
                or grant.recovery_epoch != safety_state.recovery_epoch
                or grant.grant_generation != safety_state.grant_generation
            ):
                raise PersistenceUnavailable("DURABLE_ENTRY_AUTHORIZATION_EPOCH_STALE")
            bootstrap_envelope_grant = (
                grant.envelope_version == 0 and safety_state.envelope_version == 1
            )
            if (
                not bootstrap_envelope_grant
                and grant.envelope_version != safety_state.envelope_version
            ):
                raise PersistenceUnavailable("DURABLE_ENTRY_AUTHORIZATION_EPOCH_STALE")
            if (
                grant.recovery_status != "VERIFIED"
                or not grant.replay_valid
                or not grant.projection_matches_replay
                or not grant.reconciliation_clean
                or grant.unresolved_intent_count != 0
                or grant.query_receipt_id is None
                or grant.query_epoch is None
                or grant.query_correlation_id is None
                or grant.query_response_fingerprint is None
            ):
                raise PersistenceUnavailable("DURABLE_RECOVERY_STATUS_INVALID")

            issued_at = self._as_utc(grant.issued_at)
            expires_at = self._as_utc(grant.expires_at)
            if datetime.now(UTC) >= expires_at:
                raise PersistenceUnavailable("ENTRY_AUTHORIZATION_CAPABILITY_EXPIRED")
            expected_grant_fingerprint = self._canonical_fingerprint(
                {
                    "account_id": grant.account_id,
                    "audit_event_count": grant.audit_event_count,
                    "audit_last_record_hash": grant.audit_last_record_hash,
                    "audit_last_sequence": grant.audit_last_sequence,
                    "envelope_version": grant.envelope_version,
                    "exchange_snapshot_fingerprint": (grant.exchange_snapshot_fingerprint),
                    "failure_epoch": grant.failure_epoch,
                    "grant_generation": grant.grant_generation,
                    "query_correlation_id": grant.query_correlation_id,
                    "query_epoch": grant.query_epoch,
                    "query_receipt_id": grant.query_receipt_id,
                    "query_response_fingerprint": grant.query_response_fingerprint,
                    "expires_at": expires_at.isoformat(),
                    "grant_id": grant.grant_id,
                    "issued_at": issued_at.isoformat(),
                    "local_state_fingerprint": grant.local_state_fingerprint,
                    "projection_matches_replay": grant.projection_matches_replay,
                    "reconciliation_clean": grant.reconciliation_clean,
                    "reconciliation_fingerprint": (grant.reconciliation_fingerprint),
                    "recovery_epoch": grant.recovery_epoch,
                    "recovery_probe_event_id": grant.recovery_probe_event_id,
                    "recovery_status": grant.recovery_status,
                    "replay_valid": grant.replay_valid,
                    "unresolved_intent_count": grant.unresolved_intent_count,
                }
            )
            if expected_grant_fingerprint != grant.capability_fingerprint:
                raise PersistenceUnavailable("DURABLE_ENTRY_AUTHORIZATION_TAMPERED")

            _, head, replay_valid, projection_matches = self._audit_replay_state(session)
            if (
                not replay_valid
                or not projection_matches
                or head.event_count != grant.audit_event_count
                or head.last_sequence != grant.audit_last_sequence
                or head.last_record_hash != grant.audit_last_record_hash
            ):
                raise PersistenceUnavailable("DURABLE_AUDIT_REPLAY_CHANGED")

            facts = self._reconciliation_facts_from_session(session)
            if facts.unresolved_unknown_intent_ids:
                raise PersistenceUnavailable("DURABLE_UNRESOLVED_INTENT_PRESENT")
            snapshot_payload = dict(grant.exchange_snapshot)
            if self._canonical_fingerprint(snapshot_payload) != grant.exchange_snapshot_fingerprint:
                raise PersistenceUnavailable("EXCHANGE_SNAPSHOT_EVIDENCE_TAMPERED")
            observation_batch = self._snapshot_from_payload(snapshot_payload)
            observation_batch.require_fresh()
            receipt = self._validate_durable_adapter_query_receipt(session, observation_batch)
            if (
                observation_batch.query_receipt is None
                or receipt.receipt_id != grant.query_receipt_id
                or observation_batch.query_epoch != grant.query_epoch
                or observation_batch.correlation_id != grant.query_correlation_id
                or observation_batch.response_fingerprint != grant.query_response_fingerprint
            ):
                raise PersistenceUnavailable("DURABLE_QUERY_RECEIPT_CHANGED")
            local_state_payload = dict(grant.local_state_snapshot)
            if self._canonical_fingerprint(local_state_payload) != grant.local_state_fingerprint:
                raise PersistenceUnavailable("LOCAL_RECONCILIATION_EVIDENCE_TAMPERED")
            baseline_local = self._local_state_from_payload(
                local_state_payload,
                audit_chain_valid=replay_valid,
                replay_valid=replay_valid,
            )
            outcome = reconcile_local_state(
                local=baseline_local,
                snapshot=observation_batch.snapshot,
            )
            if (
                not outcome.is_clean
                or self._reconciliation_fingerprint(outcome) != grant.reconciliation_fingerprint
            ):
                raise PersistenceUnavailable("DURABLE_RECONCILIATION_NOT_CLEAN")

            envelope_fingerprints = self._current_envelope_fingerprints(session)
            capability_fingerprint = self._canonical_fingerprint(
                {
                    "account_id": self._account_id,
                    "account_envelope_fingerprints": envelope_fingerprints,
                    "current_local_state_fingerprint": self._local_state_fingerprint(facts),
                    "current_unresolved_intent_count": len(facts.unresolved_unknown_intent_ids),
                    "durable_grant_fingerprint": grant.capability_fingerprint,
                    "envelope_version": safety_state.envelope_version,
                    "failure_epoch": safety_state.failure_epoch,
                    "grant_generation": safety_state.grant_generation,
                    "recovery_epoch": safety_state.recovery_epoch,
                }
            )
            return EntryAuthorizationCapability(
                grant_id=grant.grant_id,
                issued_at=issued_at,
                expires_at=expires_at,
                audit_last_record_hash=grant.audit_last_record_hash,
                reconciliation_fingerprint=grant.reconciliation_fingerprint,
                exchange_snapshot_fingerprint=grant.exchange_snapshot_fingerprint,
                account_envelope_fingerprints=envelope_fingerprints,
                fingerprint=capability_fingerprint,
                account_id=self._account_id,
                failure_epoch=safety_state.failure_epoch,
                recovery_epoch=safety_state.recovery_epoch,
                envelope_version=safety_state.envelope_version,
                grant_generation=safety_state.grant_generation,
            )

    def _record_entry_authorization_revocation(self, reason: str) -> None:
        """Fence grants before attempting the append-only revocation record.

        The state transaction is deliberately separate: a later revocation insert
        outage cannot roll back the failure epoch that makes every old grant stale.
        """
        with self._local_account_lock(self._account_id):
            with self._session_factory.begin() as session:
                safety_state = self._account_safety_state_for_update(session, self._account_id)
                safety_state.failure_epoch += 1
                safety_state.grant_generation += 1
                safety_state.recovery_required = True
                safety_state.updated_at = datetime.now(UTC)
            with self._session_factory.begin() as session:
                grant = session.scalar(
                    select(DurableEntryAuthorizationGrant)
                    .where(DurableEntryAuthorizationGrant.account_id == self._account_id)
                    .order_by(DurableEntryAuthorizationGrant.id.desc())
                    .limit(1)
                )
                if grant is None:
                    return
                existing = session.scalar(
                    select(DurableEntryAuthorizationRevocation.id)
                    .where(DurableEntryAuthorizationRevocation.grant_id == grant.grant_id)
                    .limit(1)
                )
                if existing is not None:
                    return
                self._entry_revocation_checkpoint("before_revocation_append")
                session.add(
                    DurableEntryAuthorizationRevocation(
                        revocation_id=f"entry-revocation-{uuid4().hex}",
                        account_id=self._account_id,
                        grant_id=grant.grant_id,
                        reason=reason[:128],
                    )
                )

    @staticmethod
    def _entry_revocation_checkpoint(_phase: str) -> None:
        """Fault-injection seam proving epoch fencing precedes the revocation append."""

    @staticmethod
    def _canonical_fingerprint(payload: object) -> str:
        canonical = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @classmethod
    def _snapshot_payload(
        cls,
        snapshot: ExchangeReconciliationObservationBatch,
    ) -> dict[str, object]:
        del cls
        snapshot.require_fresh()
        return snapshot.canonical_record()

    @staticmethod
    def _snapshot_from_payload(
        payload: dict[str, object],
    ) -> ExchangeReconciliationObservationBatch:
        raw_positions = payload.get("positions_by_symbol")
        raw_orders = payload.get("algo_orders")
        account_id = payload.get("account_id")
        if (
            not isinstance(raw_positions, dict)
            or not isinstance(raw_orders, list)
            or not isinstance(account_id, str)
            or not account_id
        ):
            raise PersistenceUnavailable("EXCHANGE_SNAPSHOT_EVIDENCE_INVALID")
        try:
            source = str(payload["source"])
            query_epoch = int(str(payload["query_epoch"]))
            correlation_id = str(payload["correlation_id"])
            requested_at = datetime.fromisoformat(str(payload["requested_at"]))
            fetched_at = datetime.fromisoformat(str(payload["fetched_at"]))
            server_time = datetime.fromisoformat(str(payload["server_time"]))
            max_age = timedelta(milliseconds=int(str(payload["max_age_ms"])))
            max_clock_skew = timedelta(milliseconds=int(str(payload["max_clock_skew_ms"])))
            positions = {
                str(symbol): Decimal(str(quantity)) for symbol, quantity in raw_positions.items()
            }
            orders = tuple(
                ExchangeAlgoOrderObservation(
                    source=source,
                    account_id=account_id,
                    fetched_at=fetched_at,
                    server_time=server_time,
                    freshness_window=max_age,
                    correlation_id=correlation_id,
                    query_epoch=query_epoch,
                    client_algo_id=str(item["client_algo_id"]),
                    symbol=str(item["symbol"]),
                    direction=Direction(str(item["direction"])),
                    algo_type=AlgoOrderType(str(item["algo_type"])),
                    trigger_price=Decimal(str(item["trigger_price"])),
                    close_position=bool(item["close_position"]),
                    quantity=(
                        None if item.get("quantity") is None else Decimal(str(item["quantity"]))
                    ),
                    working_type=StopWorkingType(str(item["working_type"])),
                    status=AlgoOrderStatus(str(item["status"])),
                    plan_id=(None if item.get("plan_id") is None else str(item["plan_id"])),
                    policy_version=(
                        None if item.get("policy_version") is None else int(item["policy_version"])
                    ),
                    account_envelope_version=(
                        None
                        if item.get("account_envelope_version") is None
                        else int(item["account_envelope_version"])
                    ),
                    policy_fingerprint=(
                        None
                        if item.get("policy_fingerprint") is None
                        else str(item["policy_fingerprint"])
                    ),
                    account_envelope_fingerprint=(
                        None
                        if item.get("account_envelope_fingerprint") is None
                        else str(item["account_envelope_fingerprint"])
                    ),
                    stop_contract_fingerprint=(
                        None
                        if item.get("stop_contract_fingerprint") is None
                        else str(item["stop_contract_fingerprint"])
                    ),
                )
                for raw_item in raw_orders
                if isinstance(raw_item, dict)
                for item in (raw_item,)
            )
            if len(orders) != len(raw_orders):
                raise ValueError("all Algo observations must be structured")
            provisional = ExchangeReconciliationObservationBatch(
                source=source,
                account_id=account_id,
                query_epoch=query_epoch,
                correlation_id=correlation_id,
                requested_at=requested_at,
                fetched_at=fetched_at,
                server_time=server_time,
                max_age=max_age,
                max_clock_skew=max_clock_skew,
                positions_by_symbol=positions,
                normal_order_client_ids=DurableIntentLedger._string_set_from_payload(
                    payload,
                    "normal_order_client_ids",
                    "EXCHANGE_SNAPSHOT_EVIDENCE_INVALID",
                ),
                algo_order_client_ids=DurableIntentLedger._string_set_from_payload(
                    payload,
                    "algo_order_client_ids",
                    "EXCHANGE_SNAPSHOT_EVIDENCE_INVALID",
                ),
                algo_orders=orders,
            )
            raw_receipt = payload.get("query_receipt")
            if not isinstance(raw_receipt, dict):
                raise ValueError("exchange snapshot has no durable query receipt identity")
            receipt = AdapterQueryReceipt(
                receipt_id=str(raw_receipt["receipt_id"]),
                account_id=account_id,
                adapter_instance_id=str(raw_receipt["adapter_instance_id"]),
                query_id=str(raw_receipt["query_id"]),
                correlation_id=correlation_id,
                query_epoch=query_epoch,
                requested_at=requested_at,
                completed_at=fetched_at,
                server_time=server_time,
                query_type="reconciliation",
                response_fingerprint=provisional.response_fingerprint,
                status=AdapterQueryReceiptStatus(str(raw_receipt["status"])),
                receipt_fingerprint=str(raw_receipt["receipt_fingerprint"]),
            )
            return replace(provisional, query_receipt=receipt)
        except (KeyError, TypeError, ValueError) as error:
            raise PersistenceUnavailable("EXCHANGE_SNAPSHOT_EVIDENCE_INVALID") from error

    @classmethod
    def _local_state_fingerprint(cls, facts: DurableReconciliationFacts) -> str:
        return cls._canonical_fingerprint(cls._local_state_payload(facts))

    @staticmethod
    def _local_state_payload(facts: DurableReconciliationFacts) -> dict[str, object]:
        return {
            "algo_order_client_ids": sorted(facts.algo_order_client_ids),
            "expected_stop_contracts": [
                {
                    "account_id": contract.account_id,
                    "account_envelope_fingerprint": (contract.account_envelope_fingerprint),
                    "account_envelope_version": contract.account_envelope_version,
                    "active_status": contract.active_status.value,
                    "algo_type": contract.algo_type.value,
                    "client_algo_id": contract.client_algo_id,
                    "close_position": contract.close_position,
                    "expected_order_side": contract.expected_order_side.value,
                    "fingerprint": contract.fingerprint,
                    "plan_id": contract.plan_id,
                    "policy_fingerprint": contract.policy_fingerprint,
                    "policy_version": contract.policy_version,
                    "position_side": contract.position_side.value,
                    "quantity_semantics": contract.quantity_semantics.value,
                    "symbol": contract.symbol,
                    "trigger_price": format(contract.trigger_price, "f"),
                    "working_type": contract.working_type.value,
                }
                for contract in sorted(
                    facts.expected_stop_contracts,
                    key=lambda item: item.client_algo_id,
                )
            ],
            "normal_order_client_ids": sorted(facts.normal_order_client_ids),
            "positions_by_symbol": {
                symbol: format(quantity, "f")
                for symbol, quantity in sorted(facts.positions_by_symbol.items())
            },
            "required_stop_symbols": sorted(facts.required_stop_symbols),
            "unresolved_unknown_intent_ids": sorted(facts.unresolved_unknown_intent_ids),
        }

    @staticmethod
    def _local_state_from_payload(
        payload: dict[str, object],
        *,
        audit_chain_valid: bool,
        replay_valid: bool,
    ) -> LocalReconciliationState:
        raw_positions = payload.get("positions_by_symbol")
        raw_contracts = payload.get("expected_stop_contracts")
        if not isinstance(raw_positions, dict) or not isinstance(raw_contracts, list):
            raise PersistenceUnavailable("LOCAL_RECONCILIATION_EVIDENCE_INVALID")
        try:
            contracts = tuple(
                ExpectedStopContract(
                    account_id=str(item["account_id"]),
                    plan_id=str(item["plan_id"]),
                    symbol=str(item["symbol"]),
                    position_side=Direction(str(item["position_side"])),
                    expected_order_side=OrderSide(str(item["expected_order_side"])),
                    client_algo_id=str(item["client_algo_id"]),
                    algo_type=AlgoOrderType(str(item["algo_type"])),
                    trigger_price=Decimal(str(item["trigger_price"])),
                    working_type=StopWorkingType(str(item["working_type"])),
                    close_position=bool(item["close_position"]),
                    quantity_semantics=StopQuantitySemantics(str(item["quantity_semantics"])),
                    active_status=AlgoOrderStatus(str(item["active_status"])),
                    policy_version=int(item["policy_version"]),
                    account_envelope_version=int(item["account_envelope_version"]),
                    policy_fingerprint=str(item["policy_fingerprint"]),
                    account_envelope_fingerprint=str(item["account_envelope_fingerprint"]),
                    fingerprint=str(item["fingerprint"]),
                )
                for raw_item in raw_contracts
                if isinstance(raw_item, dict)
                for item in (raw_item,)
            )
            if len(contracts) != len(raw_contracts):
                raise ValueError("all local stop contracts must be structured")
            return LocalReconciliationState(
                positions_by_symbol={
                    str(symbol): Decimal(str(quantity))
                    for symbol, quantity in raw_positions.items()
                },
                normal_order_client_ids=DurableIntentLedger._string_set_from_payload(
                    payload,
                    "normal_order_client_ids",
                    "LOCAL_RECONCILIATION_EVIDENCE_INVALID",
                ),
                algo_order_client_ids=DurableIntentLedger._string_set_from_payload(
                    payload,
                    "algo_order_client_ids",
                    "LOCAL_RECONCILIATION_EVIDENCE_INVALID",
                ),
                required_stop_symbols=DurableIntentLedger._string_set_from_payload(
                    payload,
                    "required_stop_symbols",
                    "LOCAL_RECONCILIATION_EVIDENCE_INVALID",
                ),
                unresolved_unknown_intent_ids=DurableIntentLedger._string_set_from_payload(
                    payload,
                    "unresolved_unknown_intent_ids",
                    "LOCAL_RECONCILIATION_EVIDENCE_INVALID",
                ),
                audit_chain_valid=audit_chain_valid,
                replay_valid=replay_valid,
                expected_stop_contracts=contracts,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PersistenceUnavailable("LOCAL_RECONCILIATION_EVIDENCE_INVALID") from error

    @staticmethod
    def _string_set_from_payload(
        payload: dict[str, object],
        key: str,
        error_code: str,
    ) -> frozenset[str]:
        values = payload.get(key)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise PersistenceUnavailable(error_code)
        return frozenset(values)

    @classmethod
    def _reconciliation_fingerprint(cls, outcome: ReconciliationOutcome) -> str:
        return cls._canonical_fingerprint(
            {
                "audit_chain_valid": outcome.audit_chain_valid,
                "invalid_stop_contract_ids": list(outcome.invalid_stop_contract_ids),
                "is_clean": outcome.is_clean,
                "missing_algo_order_ids": list(outcome.missing_algo_order_ids),
                "missing_expected_positions": [
                    [item.symbol, format(item.quantity, "f")]
                    for item in outcome.missing_expected_positions
                ],
                "missing_normal_order_ids": list(outcome.missing_normal_order_ids),
                "missing_stop_symbols": list(outcome.missing_stop_symbols),
                "position_quantity_mismatches": [
                    [
                        item.symbol,
                        format(item.expected_quantity, "f"),
                        format(item.exchange_quantity, "f"),
                    ]
                    for item in outcome.position_quantity_mismatches
                ],
                "replay_valid": outcome.replay_valid,
                "unexpected_algo_order_ids": list(outcome.unexpected_algo_order_ids),
                "unexpected_exchange_positions": [
                    [item.symbol, format(item.quantity, "f")]
                    for item in outcome.unexpected_exchange_positions
                ],
                "unexpected_normal_order_ids": list(outcome.unexpected_normal_order_ids),
                "unresolved_unknown_intent_ids": list(outcome.unresolved_unknown_intent_ids),
            }
        )

    @staticmethod
    def _local_reconciliation_state(
        facts: DurableReconciliationFacts,
        *,
        audit_chain_valid: bool,
        replay_valid: bool,
    ) -> LocalReconciliationState:
        return LocalReconciliationState(
            positions_by_symbol=facts.positions_by_symbol,
            normal_order_client_ids=facts.normal_order_client_ids,
            algo_order_client_ids=facts.algo_order_client_ids,
            required_stop_symbols=facts.required_stop_symbols,
            unresolved_unknown_intent_ids=facts.unresolved_unknown_intent_ids,
            audit_chain_valid=audit_chain_valid,
            replay_valid=replay_valid,
            expected_stop_contracts=facts.expected_stop_contracts,
        )

    @staticmethod
    def _audit_replay_state(
        session: Session,
    ) -> tuple[tuple[AuditEvent, ...], AuditChainHead, bool, bool]:
        events = tuple(
            session.scalars(select(AuditEvent).order_by(AuditEvent.chain_sequence.asc()))
        )
        head = session.get(AuditChainHead, 1)
        if head is None:
            raise PersistenceUnavailable("DURABLE_AUDIT_HEAD_MISSING")
        chain_valid = verify_hash_chain(
            events,
            expected_count=head.event_count,
            expected_last_sequence=head.last_sequence,
            expected_last_record_hash=head.last_record_hash,
        )
        replay = ReplayRunner().replay(
            events,
            AuditChainHeadSnapshot(
                event_count=head.event_count,
                last_sequence=head.last_sequence,
                last_record_hash=head.last_record_hash,
            ),
        )
        projected_states = {
            row.plan_id: row.state for row in session.scalars(select(TradePlanProjection))
        }
        replayed_states = {plan_id: state.value for plan_id, state in replay.plan_states.items()}
        return (
            events,
            head,
            chain_valid and replay.is_valid,
            replay.is_valid and projected_states == replayed_states,
        )

    @classmethod
    def _current_envelope_fingerprints(cls, session: Session) -> tuple[str, ...]:
        heads = tuple(
            session.scalars(
                select(DurablePortfolioEnvelopeHead)
                .where(DurablePortfolioEnvelopeHead.account_id == V1_DEFAULT_ACCOUNT_ID)
                .order_by(DurablePortfolioEnvelopeHead.version.asc())
            )
        )
        if not heads:
            return ()
        current = heads[-1]
        safety_state = session.get(DurableAccountSafetyState, V1_DEFAULT_ACCOUNT_ID)
        if (
            safety_state is None
            or safety_state.envelope_version != current.version
            or safety_state.envelope_fingerprint != current.envelope_fingerprint
        ):
            raise PersistenceUnavailable("ACCOUNT_ENVELOPE_HEAD_STATE_INVALID")
        return (current.envelope_fingerprint,)

    def register_actual_risk_policy(self, policy: ActualRiskPolicy) -> ActualRiskPolicy:
        """Persist an immutable entry policy before it can authorize simulator fills."""
        if not isinstance(policy, ActualRiskPolicy):
            raise TypeError("actual-risk policy must be typed")
        self._require_v1_account(policy.portfolio_envelope.account_id)
        fingerprint = self._policy_fingerprint(policy)
        try:
            with self._session_factory.begin() as session:
                self._account_safety_state_for_update(session, self._account_id)
                self._register_portfolio_envelope(session, policy.portfolio_envelope)
                self._assert_account_envelope_consistency(
                    session,
                    policy.portfolio_envelope,
                    excluding_plan_id=policy.plan_id,
                )
                existing = session.get(DurableActualRiskPolicy, policy.plan_id)
                if existing is not None:
                    if existing.policy_fingerprint != fingerprint:
                        raise DurableRiskPolicyError(
                            "plan ID already has a different durable actual-risk policy"
                        )
                    return self._policy_from_row(session, existing)
                session.add(
                    DurableActualRiskPolicy(
                        plan_id=policy.plan_id,
                        account_id=self._account_id,
                        symbol=policy.symbol,
                        direction=policy.direction.value,
                        worst_stop_exit_price=format(policy.worst_stop_exit_price, "f"),
                        exit_fee_rate=format(policy.exit_fee_rate, "f"),
                        funding_buffer_rate=format(policy.funding_buffer_rate, "f"),
                        funding_interval_count=policy.funding_interval_count,
                        risk_budget=format(policy.risk_budget, "f"),
                        max_symbol_exposure_usdt=format(policy.max_symbol_exposure_usdt, "f"),
                        max_total_exposure_usdt=format(policy.max_total_exposure_usdt, "f"),
                        existing_symbol_exposure_usdt=format(
                            policy.existing_symbol_exposure_usdt, "f"
                        ),
                        existing_total_exposure_usdt=format(
                            policy.existing_total_exposure_usdt, "f"
                        ),
                        effective_leverage=policy.effective_leverage,
                        required_reserve_usdt=format(policy.required_reserve_usdt, "f"),
                        effective_equity_usdt=format(policy.effective_equity_usdt, "f"),
                        protective_stop_reference=policy.protective_stop_reference,
                        reduce_only_exit_reference=policy.reduce_only_exit_reference,
                        account_envelope_scope=policy.portfolio_envelope.account_scope,
                        account_envelope_version=policy.portfolio_envelope.version,
                        account_envelope_fingerprint=policy.portfolio_envelope.fingerprint,
                        policy_fingerprint=fingerprint,
                    )
                )
                session.add(
                    DurableSimulatedProtection(
                        plan_id=policy.plan_id,
                        account_id=self._account_id,
                        stop_intent_reference=policy.protective_stop_reference,
                        reduce_only_exit_intent_reference=policy.reduce_only_exit_reference,
                        protected_position_quantity=format(ZERO, "f"),
                        stop_intent_ready=False,
                        reduce_only_exit_intent_ready=False,
                    )
                )
                session.add(
                    DurableActualRiskState(
                        plan_id=policy.plan_id,
                        account_id=self._account_id,
                        position_quantity=format(ZERO, "f"),
                        average_entry_price=None,
                        actual_notional_usdt=format(ZERO, "f"),
                        actual_required_margin_usdt=format(ZERO, "f"),
                        actual_stop_risk=format(ZERO, "f"),
                        pending_entries_blocked=False,
                        hard_halted=False,
                        reason=None,
                    )
                )
                return policy
        except DurableRiskPolicyError:
            raise
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    def actual_risk_policy(self, plan_id: str) -> ActualRiskPolicy | None:
        with self._session_factory() as session:
            base = session.get(DurableActualRiskPolicy, plan_id)
            if base is None:
                return None
            if base.account_id != self._account_id:
                raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
            return self._validated_policy_lineage(session, base)

    def version_actual_risk_policy(self, policy: ActualRiskPolicy) -> int:
        """Append a validated policy revision without mutating earlier evidence."""
        if not isinstance(policy, ActualRiskPolicy):
            raise TypeError("actual-risk policy must be typed")
        self._require_v1_account(policy.portfolio_envelope.account_id)
        fingerprint = self._policy_fingerprint(policy)
        try:
            with self._session_factory.begin() as session:
                self._account_safety_state_for_update(session, self._account_id)
                base = session.get(DurableActualRiskPolicy, policy.plan_id)
                if base is None:
                    raise DurableRiskPolicyError("base actual-risk policy must exist first")
                if base.account_id != self._account_id:
                    raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
                base_policy = self._policy_from_row(session, base)
                self._validated_policy_lineage(session, base)
                if (
                    policy.symbol != base_policy.symbol
                    or policy.direction is not base_policy.direction
                    or policy.protective_stop_reference != base_policy.protective_stop_reference
                    or policy.reduce_only_exit_reference != base_policy.reduce_only_exit_reference
                    or policy.portfolio_envelope.fingerprint
                    != base_policy.portfolio_envelope.fingerprint
                ):
                    raise DurableRiskPolicyError(
                        "policy revisions must preserve plan identity and protection references"
                    )
                latest = session.scalar(
                    select(DurableActualRiskPolicyVersion)
                    .where(DurableActualRiskPolicyVersion.plan_id == policy.plan_id)
                    .order_by(DurableActualRiskPolicyVersion.version.desc())
                    .limit(1)
                )
                version = 2 if latest is None else latest.version + 1
                session.add(
                    DurableActualRiskPolicyVersion(
                        plan_id=policy.plan_id,
                        version=version,
                        **self._policy_row_values(policy, fingerprint=fingerprint),
                    )
                )
                session.flush()
                return version
        except DurableRiskPolicyError:
            raise
        except IntegrityError as error:
            raise DurableRiskPolicyError(
                "policy revision must be unique and append-only"
            ) from error
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    def simulated_protection_evidence(self, plan_id: str) -> SimulatedProtectionEvidence:
        with self._session_factory() as session:
            row = session.get(DurableSimulatedProtection, plan_id)
            if row is None:
                raise KeyError(f"no durable simulated protection evidence for {plan_id}")
            return self._to_simulated_protection_evidence(row)

    def record_simulated_protection(
        self,
        plan_id: str,
        *,
        protected_position_quantity: Decimal,
    ) -> SimulatedProtectionEvidence:
        """Record rehearsal stop/reduce intent coverage without exchange confirmation."""
        if (
            not isinstance(protected_position_quantity, Decimal)
            or not protected_position_quantity.is_finite()
            or protected_position_quantity < ZERO
        ):
            raise DurableRiskPolicyError("protected position quantity must be non-negative Decimal")
        try:
            with self._session_factory.begin() as session:
                policy = session.get(DurableActualRiskPolicy, plan_id)
                protection = session.get(DurableSimulatedProtection, plan_id)
                if policy is None or protection is None:
                    raise DurableRiskPolicyError(
                        "durable policy and protection evidence are required"
                    )
                if (
                    policy.account_id != self._account_id
                    or protection.account_id != self._account_id
                ):
                    raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
                self._account_safety_state_for_update(session, self._account_id)
                fills = self._fill_ledger_for_plan(session, plan_id)
                if fills.filled_quantity != protected_position_quantity:
                    raise DurableRiskPolicyError(
                        "simulated protection quantity must match durable rehearsal entry fills"
                    )
                protection.protected_position_quantity = format(protected_position_quantity, "f")
                protection.stop_intent_ready = True
                protection.reduce_only_exit_intent_ready = True
                protection.updated_at = datetime.now(UTC)
                self._recalculate_actual_risk(session, policy)
                session.flush()
                return self._to_simulated_protection_evidence(protection)
        except DurableRiskPolicyError:
            raise
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    def actual_risk_state(self, plan_id: str) -> PositionRiskAssessment:
        try:
            with self._session_factory.begin() as session:
                policy = session.get(DurableActualRiskPolicy, plan_id)
                if policy is None:
                    raise DurableRiskPolicyError("no durable actual-risk policy for this plan")
                if policy.account_id != self._account_id:
                    raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
                self._account_safety_state_for_update(session, self._account_id)
                result = self._recalculate_actual_risk(session, policy)
                session.flush()
                return result
        except DurableRiskPolicyError:
            raise
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    def risk_reduction_requirement(self, plan_id: str) -> RiskReductionRequirement:
        with self._session_factory() as session:
            row = session.get(DurableRiskReductionRequirement, plan_id)
            if row is None:
                raise KeyError(f"no durable risk-reduction requirement for {plan_id}")
            return self._to_risk_reduction_requirement(row)

    def projected_entry_risk(self, intent: SimulatedOrderIntent) -> PortfolioRiskAssessment:
        """Recompute pending, position, aggregate, margin, reserve, and stop risk."""
        if intent.role is not OrderRole.ENTRY:
            raise DurableRiskPolicyError("projected entry risk requires an ENTRY intent")
        self._require_v1_account(intent.account_id)
        with self._session_factory() as session:
            self._require_no_unresolved_quarantine(
                session,
                account_id=self._account_id,
                economic_key=intent.economic_key,
            )
            policy_row = session.get(DurableActualRiskPolicy, intent.plan_id)
            if policy_row is None:
                raise DurableRiskPolicyError("RISK_POLICY_MISSING")
            if policy_row.account_id != self._account_id:
                raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
            policy = self._latest_policy_from_session(session, intent.plan_id)
            if policy.symbol != intent.symbol or policy.direction is not intent.direction:
                raise DurableRiskPolicyError("RISK_POLICY_PLAN_MISMATCH")
            self._assert_account_envelope_consistency(session, policy.portfolio_envelope)
            open_requirement = session.scalar(
                select(DurableRiskReductionRequirement)
                .where(
                    DurableRiskReductionRequirement.account_id == self._account_id,
                    DurableRiskReductionRequirement.status == RiskReductionStatus.OPEN.value,
                )
                .limit(1)
            )
            if open_requirement is not None:
                return PortfolioRiskAssessment(
                    pending_order_exposure_usdt=ZERO,
                    position_exposure_usdt=ZERO,
                    aggregate_symbol_exposure_usdt=ZERO,
                    aggregate_total_exposure_usdt=ZERO,
                    required_margin_usdt=ZERO,
                    reserve_usdt=policy.required_reserve_usdt,
                    projected_plan_stop_risk=ZERO,
                    blocked=True,
                    reason="RISK_REDUCTION_REQUIRED",
                )
            return self._projected_portfolio_risk(session, policy, proposed_intent=intent)

    def admit_entry(self, intent: SimulatedOrderIntent) -> EntryAdmissionDecision:
        """Create the sole durable admission authority for one entry intent."""
        if type(intent) is not SimulatedOrderIntent or intent.role is not OrderRole.ENTRY:
            raise TypeError("entry admission requires an exact ENTRY simulated intent")
        self._require_v1_account(intent.account_id)
        issued_at = datetime.now(UTC)
        try:
            with self._local_account_lock(self._account_id):
                with self._session_factory.begin() as session:
                    safety_state = self._account_safety_state_for_update(session, self._account_id)
                    self._lock_account_risk_rows(session, self._account_id)
                    self._require_no_unresolved_quarantine(
                        session,
                        account_id=self._account_id,
                        economic_key=intent.economic_key,
                    )
                    latest = self._latest_for_economic_key(session, intent.economic_key)
                    if (
                        latest is not None
                        and DurableIntentStatus(latest.status) is not DurableIntentStatus.ABSENT
                    ):
                        raise UnresolvedEconomicAction(
                            "an existing economic action must be reconciled before admission"
                        )
                    duplicate_client = session.scalar(
                        select(DurableOrderIntent).where(
                            DurableOrderIntent.client_order_id == intent.client_order_id
                        )
                    )
                    if duplicate_client is not None:
                        raise UnresolvedEconomicAction(
                            "client order ID already has a durable intent"
                        )
                    capability = self._entry_gate.authorize_new_entry_intent()
                    grant = session.scalar(
                        select(DurableEntryAuthorizationGrant).where(
                            DurableEntryAuthorizationGrant.grant_id == capability.grant_id,
                            DurableEntryAuthorizationGrant.account_id == self._account_id,
                        )
                    )
                    if (
                        grant is None
                        or grant.query_epoch is None
                        or grant.query_response_fingerprint is None
                        or grant.query_receipt_id is None
                    ):
                        raise PersistenceUnavailable("ENTRY_ADMISSION_QUERY_EVIDENCE_MISSING")
                    policy, policy_version, policy_fingerprint = self._current_policy_version(
                        session,
                        intent.plan_id,
                    )
                    if policy.symbol != intent.symbol or policy.direction is not intent.direction:
                        raise PersistenceUnavailable("ENTRY_ADMISSION_POLICY_PLAN_MISMATCH")
                    if (
                        safety_state.recovery_required
                        or safety_state.failure_epoch != capability.failure_epoch
                        or safety_state.recovery_epoch != capability.recovery_epoch
                        or safety_state.grant_generation != capability.grant_generation
                        or safety_state.envelope_version != capability.envelope_version
                        or not safety_state.envelope_fingerprint
                    ):
                        raise PersistenceUnavailable("ENTRY_ADMISSION_EPOCH_STALE")
                    envelope_head = session.scalar(
                        select(DurablePortfolioEnvelopeHead)
                        .where(DurablePortfolioEnvelopeHead.account_id == self._account_id)
                        .order_by(DurablePortfolioEnvelopeHead.version.desc())
                        .limit(1)
                    )
                    if (
                        envelope_head is None
                        or envelope_head.version != safety_state.envelope_version
                        or envelope_head.envelope_fingerprint != safety_state.envelope_fingerprint
                    ):
                        raise PersistenceUnavailable("ENTRY_ADMISSION_ENVELOPE_STALE")
                    self._require_no_durable_entry_risk_block(session, self._account_id)
                    projected = self._projected_portfolio_risk(
                        session,
                        policy,
                        proposed_intent=intent,
                    )
                    if projected.blocked:
                        raise PersistenceUnavailable(
                            f"ENTRY_ADMISSION_RISK_BLOCKED:{projected.reason or 'UNKNOWN'}"
                        )
                    decision = EntryAdmissionDecision(
                        decision_id=f"entry-admission-{uuid4().hex}",
                        account_id=self._account_id,
                        client_order_id=intent.client_order_id,
                        plan_id=intent.plan_id,
                        risk_policy_id=intent.plan_id,
                        risk_policy_version=policy_version,
                        risk_policy_fingerprint=policy_fingerprint,
                        envelope_version=safety_state.envelope_version,
                        envelope_fingerprint=safety_state.envelope_fingerprint,
                        symbol=intent.symbol,
                        side=intent.direction,
                        max_quantity=intent.quantity,
                        max_notional_usdt=intent.quantity * intent.price,
                        leverage=policy.effective_leverage,
                        expires_at=min(capability.expires_at, issued_at + timedelta(seconds=30)),
                        grant_generation=safety_state.grant_generation,
                        failure_epoch=safety_state.failure_epoch,
                        recovery_epoch=safety_state.recovery_epoch,
                        query_epoch=grant.query_epoch,
                        grant_id=capability.grant_id,
                    )
                    session.add(
                        DurableEntryAdmissionDecision(
                            decision_id=decision.decision_id,
                            account_id=decision.account_id,
                            client_order_id=decision.client_order_id,
                            plan_id=decision.plan_id,
                            risk_policy_id=decision.risk_policy_id,
                            risk_policy_version=decision.risk_policy_version,
                            risk_policy_fingerprint=decision.risk_policy_fingerprint,
                            envelope_version=decision.envelope_version,
                            envelope_fingerprint=decision.envelope_fingerprint,
                            symbol=decision.symbol,
                            side=decision.side.value,
                            max_quantity=format(decision.max_quantity, "f"),
                            max_notional_usdt=format(decision.max_notional_usdt, "f"),
                            leverage=decision.leverage,
                            expires_at=decision.expires_at,
                            grant_generation=decision.grant_generation,
                            failure_epoch=decision.failure_epoch,
                            recovery_epoch=decision.recovery_epoch,
                            query_epoch=decision.query_epoch,
                            grant_id=decision.grant_id,
                            decision_fingerprint=decision.decision_fingerprint,
                        )
                    )
                    return decision
        except PersistenceUnavailable:
            raise
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    @classmethod
    def _current_policy_version(
        cls,
        session: Session,
        plan_id: str,
    ) -> tuple[ActualRiskPolicy, int, str]:
        base = session.get(DurableActualRiskPolicy, plan_id)
        if base is None:
            raise PersistenceUnavailable("ENTRY_ADMISSION_POLICY_MISSING")
        policy = cls._validated_policy_lineage(session, base)
        latest = session.scalar(
            select(DurableActualRiskPolicyVersion)
            .where(
                DurableActualRiskPolicyVersion.plan_id == plan_id,
                DurableActualRiskPolicyVersion.account_id == cls._account_id_from_policy_row(base),
            )
            .order_by(DurableActualRiskPolicyVersion.version.desc())
            .limit(1)
        )
        return (
            policy,
            (1 if latest is None else latest.version),
            (base.policy_fingerprint if latest is None else latest.policy_fingerprint),
        )

    @staticmethod
    def _account_id_from_policy_row(row: DurableActualRiskPolicy) -> str:
        if row.account_id != V1_DEFAULT_ACCOUNT_ID:
            raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
        return row.account_id

    def prepare(
        self,
        intent: SimulatedOrderIntent,
        *,
        admission_decision: EntryAdmissionDecision | None = None,
    ) -> DurableIntentRecord:
        self._require_v1_account(intent.account_id)
        try:
            with self._session_factory.begin() as session:
                safety_state = self._account_safety_state_for_update(session, self._account_id)
                self._require_no_unresolved_quarantine(
                    session,
                    account_id=self._account_id,
                    economic_key=intent.economic_key,
                )
                latest = self._latest_for_economic_key(session, intent.economic_key)
                if (
                    latest is not None
                    and DurableIntentStatus(latest.status) is not DurableIntentStatus.ABSENT
                ):
                    raise UnresolvedEconomicAction(
                        "an existing economic action must be reconciled before another attempt"
                    )
                attempt_number = 1 if latest is None else latest.attempt_number + 1
                duplicate_client = session.scalar(
                    select(DurableOrderIntent).where(
                        DurableOrderIntent.client_order_id == intent.client_order_id
                    )
                )
                if duplicate_client is not None:
                    raise UnresolvedEconomicAction("client order ID already has a durable intent")
                if intent.role is OrderRole.ENTRY:
                    if admission_decision is None:
                        raise PersistenceUnavailable("ENTRY_ADMISSION_DECISION_REQUIRED")
                    self._validate_entry_admission_decision(
                        session,
                        intent=intent,
                        decision=admission_decision,
                        safety_state=safety_state,
                    )
                record = DurableOrderIntent(
                    account_id=self._account_id,
                    economic_key=intent.economic_key,
                    attempt_number=attempt_number,
                    client_order_id=intent.client_order_id,
                    plan_id=intent.plan_id,
                    symbol=intent.symbol,
                    direction=intent.direction.value,
                    role=intent.role.value,
                    stage_index=intent.stage_index,
                    quantity=format(intent.quantity, "f"),
                    price=format(intent.price, "f"),
                    filled_quantity=format(ZERO, "f"),
                    status=DurableIntentStatus.PREPARED.value,
                )
                session.add(record)
                session.flush()
                return self._to_record(record)
        except UnresolvedEconomicAction:
            raise
        except PersistenceUnavailable:
            raise
        except IntegrityError as error:
            raise UnresolvedEconomicAction(
                "concurrent economic action already created a durable intent"
            ) from error
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    def _validate_entry_admission_decision(
        self,
        session: Session,
        *,
        intent: SimulatedOrderIntent,
        decision: EntryAdmissionDecision,
        safety_state: DurableAccountSafetyState,
    ) -> None:
        if type(decision) is not EntryAdmissionDecision:
            raise PersistenceUnavailable("ENTRY_ADMISSION_DECISION_INVALID")
        decision.require_current()
        self._entry_gate.authorize_new_entry_intent()
        row = session.scalar(
            select(DurableEntryAdmissionDecision).where(
                DurableEntryAdmissionDecision.decision_id == decision.decision_id,
                DurableEntryAdmissionDecision.account_id == self._account_id,
            )
        )
        if row is None or row.decision_fingerprint != decision.decision_fingerprint:
            raise PersistenceUnavailable("ENTRY_ADMISSION_DECISION_UNVERIFIED")
        if (
            row.client_order_id != intent.client_order_id
            or row.plan_id != intent.plan_id
            or row.symbol != intent.symbol
            or row.side != intent.direction.value
            or row.risk_policy_id != decision.risk_policy_id
            or row.risk_policy_version != decision.risk_policy_version
            or row.risk_policy_fingerprint != decision.risk_policy_fingerprint
            or row.envelope_version != decision.envelope_version
            or row.envelope_fingerprint != decision.envelope_fingerprint
            or Decimal(row.max_quantity) != decision.max_quantity
            or Decimal(row.max_notional_usdt) != decision.max_notional_usdt
            or row.leverage != decision.leverage
            or self._as_utc(row.expires_at) != decision.expires_at
            or row.grant_generation != decision.grant_generation
            or row.failure_epoch != decision.failure_epoch
            or row.recovery_epoch != decision.recovery_epoch
            or row.query_epoch != decision.query_epoch
            or row.grant_id != decision.grant_id
        ):
            raise PersistenceUnavailable("ENTRY_ADMISSION_DECISION_TAMPERED")
        policy, version, fingerprint = self._current_policy_version(session, intent.plan_id)
        if (
            policy.symbol != intent.symbol
            or policy.direction is not intent.direction
            or version != decision.risk_policy_version
            or fingerprint != decision.risk_policy_fingerprint
            or policy.effective_leverage != decision.leverage
        ):
            raise PersistenceUnavailable("ENTRY_ADMISSION_POLICY_STALE")
        if (
            safety_state.recovery_required
            or safety_state.failure_epoch != decision.failure_epoch
            or safety_state.recovery_epoch != decision.recovery_epoch
            or safety_state.grant_generation != decision.grant_generation
            or safety_state.envelope_version != decision.envelope_version
            or safety_state.envelope_fingerprint != decision.envelope_fingerprint
        ):
            raise PersistenceUnavailable("ENTRY_ADMISSION_EPOCH_STALE")
        if (
            intent.quantity > decision.max_quantity
            or intent.quantity * intent.price > decision.max_notional_usdt
        ):
            raise PersistenceUnavailable("ENTRY_ADMISSION_LIMIT_EXCEEDED")
        self._require_no_durable_entry_risk_block(session, self._account_id)
        projected = self._projected_portfolio_risk(session, policy, proposed_intent=intent)
        if projected.blocked:
            raise PersistenceUnavailable(
                f"ENTRY_ADMISSION_RISK_BLOCKED:{projected.reason or 'UNKNOWN'}"
            )

    @classmethod
    def _require_no_durable_entry_risk_block(cls, session: Session, account_id: str) -> None:
        """Make durable risk blocks account-wide admission facts, including after restart."""
        blocked_state = session.scalar(
            select(DurableActualRiskState)
            .where(
                DurableActualRiskState.account_id == account_id,
                or_(
                    DurableActualRiskState.pending_entries_blocked.is_(True),
                    DurableActualRiskState.hard_halted.is_(True),
                ),
            )
            .order_by(DurableActualRiskState.updated_at.desc())
            .limit(1)
        )
        if blocked_state is not None:
            raise PersistenceUnavailable(
                "ENTRY_ADMISSION_RISK_BLOCKED:"
                f"{blocked_state.reason or 'DURABLE_RISK_STATE_BLOCKED'}"
            )
        open_requirement = session.scalar(
            select(DurableRiskReductionRequirement)
            .where(
                DurableRiskReductionRequirement.account_id == account_id,
                DurableRiskReductionRequirement.status == RiskReductionStatus.OPEN.value,
            )
            .limit(1)
        )
        if open_requirement is not None:
            raise PersistenceUnavailable("ENTRY_ADMISSION_RISK_BLOCKED:RISK_REDUCTION_REQUIRED")

    def mark_submitting(
        self,
        client_order_id: str,
        *,
        submitted_at_ms: int | None = None,
    ) -> DurableIntentRecord:
        return self._transition_status(
            client_order_id,
            expected_statuses=frozenset({DurableIntentStatus.PREPARED}),
            target_status=DurableIntentStatus.SUBMITTING,
            timestamp_field="submitted_at_ms",
            timestamp_value=submitted_at_ms,
        )

    def mark_unknown(
        self,
        client_order_id: str,
        *,
        unknown_at_ms: int | None = None,
    ) -> DurableIntentRecord:
        return self._transition_status(
            client_order_id,
            expected_statuses=frozenset({DurableIntentStatus.SUBMITTING}),
            target_status=DurableIntentStatus.UNKNOWN,
            timestamp_field="unknown_at_ms",
            timestamp_value=unknown_at_ms,
        )

    def record_fill(
        self,
        event: FillEvent,
        *,
        materialize_simulated_protection: bool = False,
    ) -> FillLedgerReceipt:
        """Ingest an exchange fact before attempting its idempotent application."""
        with self._local_account_lock(self._account_id):
            fact = self._ingest_exchange_fill_fact(
                event,
                materialize_simulated_protection=materialize_simulated_protection,
            )
            if fact.last_apply_error == "SEMANTIC_CONFLICT":
                raise FillLedgerError(
                    "exchange fill trade ID was redelivered with a semantic conflict"
                )
            if fact.apply_status == FillFactApplyStatus.APPLIED.value:
                return self._receipt_for_applied_fill_fact(fact, duplicate=True)
            try:
                self._claim_fill_fact_application(fact.id)
                receipt = self._record_fill_locked(
                    event,
                    materialize_simulated_protection=materialize_simulated_protection,
                )
                self._mark_fill_fact_applied(fact.id)
            except Exception as error:
                self._mark_fill_fact_recovery_required(fact.id, error)
                self._persistence_breaker.record_write_failure(error)
                raise
            return receipt

    def _ingest_exchange_fill_fact(
        self,
        event: FillEvent,
        *,
        materialize_simulated_protection: bool,
    ) -> ExchangeFillFactJournal:
        """Stage A: commit immutable exchange data before any risk-side effects."""
        try:
            self._require_v1_account(event.account_id)
        except DurableRiskPolicyError as error:
            raise FillLedgerError(
                "exchange fill account does not match the durable intent scope"
            ) from error
        fingerprint = self._fill_fingerprint(event)
        provenance_fingerprint = self._fill_provenance_fingerprint(event)
        try:
            with self._session_factory.begin() as session:
                safety_state = self._account_safety_state_for_update(session, self._account_id)
                existing_intent = session.scalar(
                    select(DurableOrderIntent).where(
                        DurableOrderIntent.client_order_id == event.client_order_id
                    )
                )
                # A known local intent lets us reject a fabricated fact before it
                # reaches the durable journal. An unknown intent remains a fact
                # that must be held for recovery rather than silently discarded.
                if existing_intent is not None:
                    self._validate_fill_identity(event, existing_intent)
                existing = session.scalar(
                    select(ExchangeFillFactJournal).where(
                        ExchangeFillFactJournal.account_id == self._account_id,
                        ExchangeFillFactJournal.exchange_trade_id == event.trade_id,
                    )
                )
                if existing is not None:
                    if existing.semantic_fingerprint != fingerprint:
                        safety_state.failure_epoch += 1
                        safety_state.grant_generation += 1
                        safety_state.recovery_required = True
                        safety_state.updated_at = datetime.now(UTC)
                        self._cancel_known_active_entry_intents(session, self._account_id)
                        existing.apply_status = FillFactApplyStatus.RECOVERY_REQUIRED.value
                        existing.last_apply_error = "SEMANTIC_CONFLICT"
                        return existing
                    return existing
                fact = ExchangeFillFactJournal(
                    account_id=self._account_id,
                    exchange_trade_id=event.trade_id,
                    client_order_id=event.client_order_id,
                    intent_id=(
                        None if existing_intent is None else existing_intent.client_order_id
                    ),
                    symbol=event.symbol,
                    side=event.side.value,
                    quantity=format(event.last_quantity, "f"),
                    cumulative_quantity=format(event.cumulative_quantity, "f"),
                    price=format(event.fill_price, "f"),
                    fee=format(event.fee, "f"),
                    fee_asset=event.fee_asset,
                    exchange_timestamp=event.occurred_at.astimezone(UTC),
                    observation_source=event.observation_source.value,
                    observation_reference=event.observation_reference,
                    observation_correlation=event.observation_correlation,
                    provenance_fingerprint=provenance_fingerprint,
                    semantic_fingerprint=fingerprint,
                    materialize_simulated_protection=materialize_simulated_protection,
                    apply_status=FillFactApplyStatus.PENDING.value,
                    apply_attempt_count=0,
                    last_apply_error=None,
                )
                session.add(fact)
                session.flush()
                return fact
        except (DurableRiskPolicyError, FillLedgerError):
            raise
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    def _claim_fill_fact_application(self, fact_id: int) -> None:
        with self._session_factory.begin() as session:
            self._account_safety_state_for_update(session, self._account_id)
            fact = session.get(ExchangeFillFactJournal, fact_id)
            if fact is None or fact.account_id != self._account_id:
                raise PersistenceUnavailable("EXCHANGE_FILL_FACT_MISSING")
            if fact.apply_status == FillFactApplyStatus.APPLIED.value:
                return
            fact.apply_status = FillFactApplyStatus.PENDING.value
            fact.apply_attempt_count += 1
            fact.last_apply_error = None

    def _mark_fill_fact_applied(self, fact_id: int) -> None:
        with self._session_factory.begin() as session:
            fact = session.get(ExchangeFillFactJournal, fact_id)
            if fact is None or fact.account_id != self._account_id:
                raise PersistenceUnavailable("EXCHANGE_FILL_FACT_MISSING")
            fact.apply_status = FillFactApplyStatus.APPLIED.value
            fact.last_apply_error = None
            fact.applied_at = datetime.now(UTC)

    def _mark_fill_fact_recovery_required(self, fact_id: int, error: Exception) -> None:
        """Keep the fact and durably close every new-entry route after failure."""
        try:
            with self._session_factory.begin() as session:
                safety_state = self._account_safety_state_for_update(session, self._account_id)
                self._lock_account_risk_rows(session, self._account_id)
                fact = session.get(ExchangeFillFactJournal, fact_id)
                if fact is None or fact.account_id != self._account_id:
                    return
                if fact.apply_status == FillFactApplyStatus.APPLIED.value:
                    return
                fact.apply_status = FillFactApplyStatus.RECOVERY_REQUIRED.value
                fact.last_apply_error = f"{type(error).__name__}: {error}"[:4096]
                safety_state.failure_epoch += 1
                safety_state.grant_generation += 1
                safety_state.recovery_required = True
                safety_state.updated_at = datetime.now(UTC)
                intent = session.scalar(
                    select(DurableOrderIntent).where(
                        DurableOrderIntent.client_order_id == fact.client_order_id
                    )
                )
                if intent is not None and DurableIntentStatus(intent.status) in {
                    DurableIntentStatus.PREPARED,
                    DurableIntentStatus.SUBMITTING,
                    DurableIntentStatus.UNKNOWN,
                    DurableIntentStatus.NEW,
                    DurableIntentStatus.PARTIALLY_FILLED,
                }:
                    intent.status = DurableIntentStatus.CANCEL_REQUIRED.value
                    intent.updated_at = datetime.now(UTC)
                self._cancel_known_active_entry_intents(session, self._account_id)
        except Exception as mark_error:
            self._persistence_breaker.record_write_failure(mark_error)

    def _receipt_for_applied_fill_fact(
        self,
        fact: ExchangeFillFactJournal,
        *,
        duplicate: bool,
    ) -> FillLedgerReceipt:
        with self._session_factory() as session:
            ledger = self._intent_fill_ledger(session, fact.client_order_id)
            if ledger.filled_quantity <= ZERO:
                raise PersistenceUnavailable("EXCHANGE_FILL_FACT_APPLY_PROJECTION_MISSING")
            return FillLedgerReceipt(
                is_duplicate=duplicate,
                filled_quantity=ledger.filled_quantity,
                average_fill_price=ledger.average_fill_price,
                total_fee=ledger.total_fee,
            )

    @staticmethod
    def _fill_provenance_fingerprint(event: FillEvent) -> str:
        if event.provenance_fingerprint is not None:
            return event.provenance_fingerprint
        return DurableIntentLedger._canonical_fingerprint(
            {
                "account_id": event.account_id,
                "correlation": event.observation_correlation,
                "observation_reference": event.observation_reference,
                "observation_source": event.observation_source.value,
                "trade_id": event.trade_id,
            }
        )

    def _record_fill_locked(
        self,
        event: FillEvent,
        *,
        materialize_simulated_protection: bool,
    ) -> FillLedgerReceipt:
        """Atomically persist a fill and every resulting rehearsal safety decision."""
        try:
            with self._session_factory.begin() as session:
                safety_state = self._account_safety_state_for_update(session, self._account_id)
                self._lock_account_risk_rows(session, self._account_id)
                intent = self._record_for_update(session, event.client_order_id)
                if intent.account_id != self._account_id:
                    raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
                self._validate_fill_identity(event, intent)
                status = DurableIntentStatus(intent.status)
                existing_rows = tuple(
                    session.scalars(
                        select(DurableIntentFill).where(
                            DurableIntentFill.client_order_id == event.client_order_id,
                            DurableIntentFill.account_id == self._account_id,
                        )
                    )
                )
                fill_ledger = FillLedger.from_events(
                    self._fill_event_from_row(row) for row in existing_rows
                )
                fingerprint = self._fill_fingerprint(event)
                existing = session.scalar(
                    select(DurableIntentFill).where(
                        DurableIntentFill.client_order_id == event.client_order_id,
                        DurableIntentFill.trade_id == event.trade_id,
                        DurableIntentFill.account_id == self._account_id,
                    )
                )
                if existing is not None:
                    if existing.semantic_fingerprint != fingerprint:
                        raise FillLedgerError(
                            "durable trade ID was redelivered with a semantic conflict"
                        )
                    return FillLedgerReceipt(
                        is_duplicate=True,
                        filled_quantity=fill_ledger.filled_quantity,
                        average_fill_price=fill_ledger.average_fill_price,
                        total_fee=fill_ledger.total_fee,
                    )
                receipt = fill_ledger.record(event)
                planned_quantity = Decimal(intent.quantity)
                if receipt.filled_quantity > planned_quantity:
                    raise IntentLifecycleError("durable fills exceed the planned quantity")
                session.add(
                    DurableIntentFill(
                        account_id=self._account_id,
                        client_order_id=event.client_order_id,
                        trade_id=event.trade_id,
                        symbol=event.symbol,
                        side=event.side.value,
                        observation_source=event.observation_source.value,
                        observation_reference=event.observation_reference,
                        semantic_fingerprint=fingerprint,
                        last_quantity=format(event.last_quantity, "f"),
                        cumulative_quantity=format(event.cumulative_quantity, "f"),
                        fill_price=format(event.fill_price, "f"),
                        fee=format(event.fee, "f"),
                        fee_asset=event.fee_asset,
                        occurred_at=event.occurred_at.astimezone(UTC),
                    )
                )
                if status not in {
                    DurableIntentStatus.CANCELLED,
                    DurableIntentStatus.CANCEL_REQUIRED,
                }:
                    target_status = (
                        DurableIntentStatus.FILLED
                        if receipt.filled_quantity == planned_quantity
                        else DurableIntentStatus.PARTIALLY_FILLED
                    )
                    if status not in {
                        DurableIntentStatus.ABSENT,
                        DurableIntentStatus.REJECTED,
                    }:
                        self._assert_known_outcome_transition(
                            current=status,
                            target=target_status,
                            prior_filled_quantity=Decimal(intent.filled_quantity),
                            filled_quantity=receipt.filled_quantity,
                        )
                    intent.status = target_status.value
                elif (
                    status is DurableIntentStatus.CANCEL_REQUIRED
                    and receipt.filled_quantity == planned_quantity
                ):
                    intent.status = DurableIntentStatus.FILLED.value
                intent.filled_quantity = format(receipt.filled_quantity, "f")
                intent.updated_at = datetime.now(UTC)
                session.flush()
                self._fill_uow_checkpoint("after_fill_persisted")
                if intent.role == OrderRole.ENTRY.value:
                    self._cancel_unfilled_superseding_attempts(session, intent)
                    policy = session.get(DurableActualRiskPolicy, intent.plan_id)
                    if policy is None:
                        safety_state.grant_generation += 1
                        safety_state.recovery_required = True
                        self._cancel_known_active_entry_intents(session, self._account_id)
                        session.flush()
                        return receipt
                    try:
                        if policy.account_id != self._account_id:
                            raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
                        self._fill_uow_checkpoint("before_protection_evaluation")
                        if materialize_simulated_protection:
                            self._materialize_simulated_protection_in_session(
                                session,
                                policy,
                            )
                        self._fill_uow_checkpoint("after_protection_evaluation")
                        results = tuple(
                            self._recalculate_actual_risk(session, account_policy)
                            for account_policy in session.scalars(
                                select(DurableActualRiskPolicy).where(
                                    DurableActualRiskPolicy.account_id == self._account_id
                                )
                            )
                        )
                        self._fill_uow_checkpoint("after_actual_risk")
                        if any(result.pending_entries_blocked for result in results):
                            self._fill_uow_checkpoint("before_pending_cancel")
                            self._cancel_known_active_entry_intents(session, self._account_id)
                            self._fill_uow_checkpoint("after_pending_cancel")
                    except (
                        DurableRiskPolicyError,
                        FillLedgerError,
                        ValueError,
                        ArithmeticError,
                    ) as error:
                        self._hard_block_after_fill_risk_failure(
                            session,
                            safety_state=safety_state,
                            policy_row=policy,
                            receipt=receipt,
                            error=error,
                        )
                    session.flush()
                return receipt
        except (DurableRiskPolicyError, FillLedgerError, IntentLifecycleError):
            raise
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    def record_exchange_outcome(
        self,
        client_order_id: str,
        *,
        status: DurableIntentStatus,
        filled_quantity: Decimal,
    ) -> DurableIntentRecord:
        if status not in _KNOWN_OUTCOME_STATUSES:
            raise ValueError("only a known exchange outcome can resolve a durable intent")
        if not isinstance(filled_quantity, Decimal) or not filled_quantity.is_finite():
            raise ValueError("filled quantity must be a finite Decimal")
        try:
            with self._session_factory.begin() as session:
                record = self._record_for_update(session, client_order_id)
                durable_fills = self._intent_fill_ledger(session, client_order_id)
                if (
                    status
                    in {
                        DurableIntentStatus.PARTIALLY_FILLED,
                        DurableIntentStatus.FILLED,
                    }
                    and durable_fills.filled_quantity <= ZERO
                ):
                    raise IntentLifecycleError("filled outcomes require durable fill facts")
                if filled_quantity != durable_fills.filled_quantity:
                    raise IntentLifecycleError(
                        "exchange outcome quantity must equal durable fill evidence"
                    )
                self._validate_filled_quantity(
                    quantity=Decimal(record.quantity),
                    filled_quantity=filled_quantity,
                    status=status,
                )
                self._assert_known_outcome_transition(
                    current=DurableIntentStatus(record.status),
                    target=status,
                    prior_filled_quantity=Decimal(record.filled_quantity),
                    filled_quantity=filled_quantity,
                )
                record.status = status.value
                record.filled_quantity = format(filled_quantity, "f")
                record.updated_at = datetime.now(UTC)
                session.flush()
                return self._to_record(record)
        except (BoundedAbsenceEvidenceError, IntentLifecycleError):
            raise
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    def _record_simulated_query_observation(
        self,
        client_order_id: str,
        *,
        source: AbsenceEvidenceSource,
        found: bool,
        observed_at_ms: int,
        query_reference: str,
        capability: object,
    ) -> DurableAbsenceObservation:
        """Persist a query fact that was derived by the concrete simulator query service."""
        if capability is not _SIMULATED_QUERY_WRITE_CAPABILITY:
            raise TypeError("absence observations require the simulator query service")
        if not isinstance(source, AbsenceEvidenceSource):
            raise TypeError("absence query source must be typed")
        if not isinstance(found, bool):
            raise TypeError("absence query result must be boolean")
        if (
            not isinstance(observed_at_ms, int)
            or isinstance(observed_at_ms, bool)
            or observed_at_ms < 0
        ):
            raise ValueError("absence query time must be non-negative")
        if not query_reference:
            raise ValueError("absence query reference is required")
        try:
            with self._session_factory.begin() as session:
                record = self._record_for_update(session, client_order_id)
                if DurableIntentStatus(record.status) is not DurableIntentStatus.UNKNOWN:
                    raise IntentLifecycleError(
                        "absence observations require an UNKNOWN durable intent"
                    )
                if record.submitted_at_ms is None or record.unknown_at_ms is None:
                    raise BoundedAbsenceEvidenceError(
                        "durable submission and UNKNOWN timestamps are required"
                    )
                observation = UnknownIntentObservation(
                    source=source,
                    observed_at_ms=observed_at_ms,
                    stream_watermark_ms=observed_at_ms,
                    found=found,
                    query_reference=query_reference,
                    query_client_order_id=record.client_order_id,
                    query_economic_key=record.economic_key,
                    query_started_at_ms=observed_at_ms,
                    attempt_number=record.attempt_number,
                    client_order_namespace=ClientOrderNamespace.NORMAL,
                )
                if (
                    observation.query_client_order_id != record.client_order_id
                    or observation.query_economic_key != record.economic_key
                    or observation.attempt_number != record.attempt_number
                    or observation.client_order_namespace is not ClientOrderNamespace.NORMAL
                ):
                    raise BoundedAbsenceEvidenceError(
                        "absence query identity does not match the durable intent"
                    )
                if (
                    observation.query_started_at_ms < record.unknown_at_ms
                    or observation.observed_at_ms < record.unknown_at_ms
                    or observation.stream_watermark_ms < record.unknown_at_ms
                ):
                    raise BoundedAbsenceEvidenceError(
                        "absence observation predates durable UNKNOWN submission evidence"
                    )
                if (
                    observation.observed_at_ms > observed_at_ms
                    or observation.stream_watermark_ms > observed_at_ms
                    or observation.query_started_at_ms > observed_at_ms
                ):
                    raise BoundedAbsenceEvidenceError(
                        "absence observation cannot come from the future"
                    )
                if (
                    observation.provenance_fingerprint
                    != observation.expected_provenance_fingerprint()
                ):
                    raise BoundedAbsenceEvidenceError(
                        "absence query provenance fingerprint is invalid"
                    )
                row = DurableIntentAbsenceObservation(
                    account_id=record.account_id,
                    client_order_id=record.client_order_id,
                    economic_key=record.economic_key,
                    source=observation.source.value,
                    query_reference=observation.query_reference,
                    query_client_order_id=observation.query_client_order_id,
                    query_economic_key=observation.query_economic_key,
                    query_started_at_ms=observation.query_started_at_ms,
                    attempt_number=observation.attempt_number,
                    client_order_namespace=observation.client_order_namespace.value,
                    provenance_fingerprint=observation.provenance_fingerprint,
                    observed_at_ms=observation.observed_at_ms,
                    stream_watermark_ms=observation.stream_watermark_ms,
                    found=observation.found,
                )
                session.add(row)
                session.flush()
                return self._to_absence_observation(row)
        except (BoundedAbsenceEvidenceError, IntentLifecycleError):
            raise
        except IntegrityError as error:
            raise BoundedAbsenceEvidenceError(
                "duplicate durable absence observation is not additional evidence"
            ) from error
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    def bounded_absence_evidence(self, client_order_id: str) -> BoundedAbsenceEvidence:
        with self._session_factory() as session:
            record = self._record_for_update(session, client_order_id)
            if DurableIntentStatus(record.status) is not DurableIntentStatus.UNKNOWN:
                raise IntentLifecycleError("only UNKNOWN intents can have absence evidence")
            return self._derive_bounded_absence_evidence(session, record)

    def resolve_unknown_as_absent(
        self,
        client_order_id: str,
        evidence: BoundedAbsenceEvidence | None = None,
    ) -> DurableIntentRecord:
        """Resolve only from queryable, persisted multi-source evidence; caller data is rejected."""
        if evidence is not None:
            raise BoundedAbsenceEvidenceError(
                "caller-supplied absence summaries cannot authorize a retry"
            )
        try:
            with self._session_factory.begin() as session:
                record = self._record_for_update(session, client_order_id)
                if DurableIntentStatus(record.status) is not DurableIntentStatus.UNKNOWN:
                    raise IntentLifecycleError("only UNKNOWN intents can resolve as absent")
                self._require_no_unresolved_quarantine(
                    session,
                    account_id=record.account_id,
                    economic_key=record.economic_key,
                )
                self._derive_bounded_absence_evidence(session, record)
                record.status = DurableIntentStatus.ABSENT.value
                record.updated_at = datetime.now(UTC)
                session.flush()
                return self._to_record(record)
        except (BoundedAbsenceEvidenceError, IntentLifecycleError):
            raise
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    def intent(self, client_order_id: str) -> DurableIntentRecord:
        with self._session_factory() as session:
            record = session.scalar(
                select(DurableOrderIntent).where(
                    DurableOrderIntent.client_order_id == client_order_id,
                    DurableOrderIntent.account_id == self._account_id,
                )
            )
            if record is None:
                raise KeyError(f"no durable intent for {client_order_id}")
            return self._to_record(record)

    def list_intents(self) -> tuple[DurableIntentRecord, ...]:
        with self._session_factory() as session:
            records = session.scalars(
                select(DurableOrderIntent)
                .where(DurableOrderIntent.account_id == self._account_id)
                .order_by(DurableOrderIntent.id.asc())
            )
            return tuple(self._to_record(record) for record in records)

    def list_absence_observations(
        self,
        client_order_id: str,
    ) -> tuple[DurableAbsenceObservation, ...]:
        with self._session_factory() as session:
            record = session.scalar(
                select(DurableOrderIntent).where(
                    DurableOrderIntent.client_order_id == client_order_id,
                    DurableOrderIntent.account_id == self._account_id,
                )
            )
            if record is None:
                raise KeyError(f"no durable intent for {client_order_id}")
            self._validate_lifecycle_timeline(record)
            rows = session.scalars(
                select(DurableIntentAbsenceObservation)
                .where(
                    DurableIntentAbsenceObservation.client_order_id == client_order_id,
                    DurableIntentAbsenceObservation.account_id == self._account_id,
                )
                .order_by(
                    DurableIntentAbsenceObservation.observed_at_ms.asc(),
                    DurableIntentAbsenceObservation.source.asc(),
                )
            )
            return tuple(self._validated_absence_observation(row, record) for row in rows)

    def fill_ledger_for_intent(self, client_order_id: str) -> FillLedger:
        with self._session_factory() as session:
            return self._intent_fill_ledger(session, client_order_id)

    def fill_ledger_for_plan(self, plan_id: str) -> FillLedger:
        with self._session_factory() as session:
            return self._fill_ledger_for_plan(session, plan_id)

    def unresolved_client_order_ids(self) -> tuple[str, ...]:
        return tuple(
            record.client_order_id
            for record in self.list_intents()
            if record.status in _UNRESOLVED_STATUSES
        )

    def unresolved_counts(self) -> dict[DurableIntentStatus, int]:
        records = self.list_intents()
        return {
            status: sum(record.status is status for record in records)
            for status in (
                DurableIntentStatus.PREPARED,
                DurableIntentStatus.SUBMITTING,
                DurableIntentStatus.UNKNOWN,
                DurableIntentStatus.CANCEL_REQUIRED,
            )
            if any(record.status is status for record in records)
        }

    def reconciliation_facts(self) -> DurableReconciliationFacts:
        """Derive local position and protection expectations from durable records only."""
        with self._session_factory() as session:
            return self._reconciliation_facts_from_session(session)

    @classmethod
    def _reconciliation_facts_from_session(
        cls,
        session: Session,
    ) -> DurableReconciliationFacts:
        positions_by_symbol: dict[str, Decimal] = {}
        required_stop_symbols: set[str] = set()
        algo_order_client_ids: set[str] = set()
        expected_stop_contracts: list[ExpectedStopContract] = []
        for state in session.scalars(
            select(DurableActualRiskState).where(
                DurableActualRiskState.account_id == V1_DEFAULT_ACCOUNT_ID
            )
        ):
            policy_row = session.get(DurableActualRiskPolicy, state.plan_id)
            protection = session.get(DurableSimulatedProtection, state.plan_id)
            if policy_row is None or protection is None:
                raise DurableRiskPolicyError(
                    "durable reconciliation facts require complete risk and protection records"
                )
            if (
                policy_row.account_id != V1_DEFAULT_ACCOUNT_ID
                or protection.account_id != V1_DEFAULT_ACCOUNT_ID
                or state.account_id != V1_DEFAULT_ACCOUNT_ID
            ):
                raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
            version_row = session.scalar(
                select(DurableActualRiskPolicyVersion)
                .where(DurableActualRiskPolicyVersion.plan_id == state.plan_id)
                .order_by(DurableActualRiskPolicyVersion.version.desc())
                .limit(1)
            )
            active_row = policy_row if version_row is None else version_row
            policy_version = 1 if version_row is None else version_row.version
            policy = cls._policy_from_row(session, active_row)
            quantity = Decimal(state.position_quantity)
            if quantity <= ZERO:
                continue
            signed_quantity = quantity if policy.direction is Direction.LONG else -quantity
            positions_by_symbol[policy.symbol] = (
                positions_by_symbol.get(policy.symbol, ZERO) + signed_quantity
            )
            required_stop_symbols.add(policy.symbol)
            if protection.stop_intent_reference != policy.protective_stop_reference:
                raise DurableRiskPolicyError("durable expected Algo stop does not match its policy")
            algo_order_client_ids.add(policy.protective_stop_reference)
            expected_stop_contracts.append(
                ExpectedStopContract(
                    account_id=policy.portfolio_envelope.account_id,
                    plan_id=policy.plan_id,
                    symbol=policy.symbol,
                    position_side=policy.direction,
                    expected_order_side=(
                        OrderSide.SELL if policy.direction is Direction.LONG else OrderSide.BUY
                    ),
                    client_algo_id=policy.protective_stop_reference,
                    algo_type=AlgoOrderType.STOP_MARKET,
                    trigger_price=policy.worst_stop_exit_price,
                    working_type=StopWorkingType.MARK_PRICE,
                    close_position=True,
                    quantity_semantics=StopQuantitySemantics.CLOSE_POSITION_FULL,
                    active_status=AlgoOrderStatus.NEW,
                    policy_version=policy_version,
                    account_envelope_version=policy.portfolio_envelope.version,
                    policy_fingerprint=active_row.policy_fingerprint,
                    account_envelope_fingerprint=(policy.portfolio_envelope.fingerprint),
                )
            )

        active_normal_statuses = {
            DurableIntentStatus.NEW.value,
            DurableIntentStatus.PARTIALLY_FILLED.value,
        }
        normal_order_client_ids = frozenset(
            row.client_order_id
            for row in session.scalars(
                select(DurableOrderIntent).where(
                    DurableOrderIntent.account_id == V1_DEFAULT_ACCOUNT_ID
                )
            )
            if row.status in active_normal_statuses and row.role != OrderRole.STOP.value
        )
        unresolved_unknown_intent_ids = frozenset(
            row.client_order_id
            for row in session.scalars(
                select(DurableOrderIntent).where(
                    DurableOrderIntent.account_id == V1_DEFAULT_ACCOUNT_ID
                )
            )
            if row.status in {status.value for status in _UNRESOLVED_STATUSES}
        )
        return DurableReconciliationFacts(
            account_id=V1_DEFAULT_ACCOUNT_ID,
            positions_by_symbol=positions_by_symbol,
            normal_order_client_ids=normal_order_client_ids,
            algo_order_client_ids=frozenset(algo_order_client_ids),
            required_stop_symbols=frozenset(required_stop_symbols),
            unresolved_unknown_intent_ids=unresolved_unknown_intent_ids,
            expected_stop_contracts=tuple(expected_stop_contracts),
        )

    def _transition_status(
        self,
        client_order_id: str,
        *,
        expected_statuses: frozenset[DurableIntentStatus],
        target_status: DurableIntentStatus,
        timestamp_field: str | None = None,
        timestamp_value: int | None = None,
    ) -> DurableIntentRecord:
        if timestamp_value is not None and (
            not isinstance(timestamp_value, int)
            or isinstance(timestamp_value, bool)
            or timestamp_value < 0
        ):
            raise ValueError("durable lifecycle timestamps must be non-negative integers")
        if timestamp_field not in {None, "submitted_at_ms", "unknown_at_ms"}:
            raise ValueError("durable lifecycle timestamp field is invalid")
        try:
            with self._session_factory.begin() as session:
                record = self._record_for_update(session, client_order_id)
                if DurableIntentStatus(record.status) not in expected_statuses:
                    raise IntentLifecycleError(
                        "durable intent is not in the required lifecycle state"
                    )
                if timestamp_field is not None:
                    setattr(record, timestamp_field, timestamp_value)
                record.status = target_status.value
                self._validate_lifecycle_timeline(record)
                record.updated_at = datetime.now(UTC)
                session.flush()
                return self._to_record(record)
        except IntentLifecycleError:
            raise
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

    @staticmethod
    def _validate_filled_quantity(
        *,
        quantity: Decimal,
        filled_quantity: Decimal,
        status: DurableIntentStatus,
    ) -> None:
        if filled_quantity < ZERO or filled_quantity > quantity:
            raise IntentLifecycleError("filled quantity must remain within planned quantity")
        if status is DurableIntentStatus.PARTIALLY_FILLED:
            if filled_quantity <= ZERO or filled_quantity >= quantity:
                raise IntentLifecycleError("PARTIALLY_FILLED requires a strict partial quantity")
        elif status is DurableIntentStatus.FILLED and filled_quantity != quantity:
            raise IntentLifecycleError("FILLED requires the full planned quantity")
        elif (
            status in {DurableIntentStatus.NEW, DurableIntentStatus.REJECTED}
            and filled_quantity != ZERO
        ):
            raise IntentLifecycleError(f"{status.value} must not carry a filled quantity")

    @staticmethod
    def _assert_known_outcome_transition(
        *,
        current: DurableIntentStatus,
        target: DurableIntentStatus,
        prior_filled_quantity: Decimal,
        filled_quantity: Decimal,
    ) -> None:
        if filled_quantity < prior_filled_quantity:
            raise IntentLifecycleError("known outcomes cannot reduce durable filled quantity")
        if current in _TERMINAL_STATUSES:
            if current is DurableIntentStatus.CANCELLED and target is DurableIntentStatus.CANCELLED:
                return
            if current is target and prior_filled_quantity == filled_quantity:
                return
            raise IntentLifecycleError("terminal durable intent cannot change outcome")
        if current is DurableIntentStatus.PREPARED:
            raise IntentLifecycleError(
                "known outcomes require SUBMITTING or a reconciled active state"
            )
        if current in {DurableIntentStatus.SUBMITTING, DurableIntentStatus.UNKNOWN}:
            return
        if current is DurableIntentStatus.NEW:
            if target in {
                DurableIntentStatus.NEW,
                DurableIntentStatus.PARTIALLY_FILLED,
                DurableIntentStatus.FILLED,
                DurableIntentStatus.CANCELLED,
            }:
                return
        if current is DurableIntentStatus.PARTIALLY_FILLED:
            if target in {
                DurableIntentStatus.PARTIALLY_FILLED,
                DurableIntentStatus.FILLED,
                DurableIntentStatus.CANCELLED,
            }:
                return
        if current is DurableIntentStatus.CANCEL_REQUIRED:
            if target in {
                DurableIntentStatus.CANCELLED,
                DurableIntentStatus.PARTIALLY_FILLED,
                DurableIntentStatus.FILLED,
            }:
                return
        raise IntentLifecycleError("known outcome violates the durable intent lifecycle")

    @staticmethod
    def _fill_uow_checkpoint(_phase: str) -> None:
        """Crash-injection seam used to prove transaction boundaries in tests."""

    @classmethod
    def _to_record(cls, record: DurableOrderIntent) -> DurableIntentRecord:
        cls._validate_lifecycle_timeline(record)
        return DurableIntentRecord(
            account_id=record.account_id,
            client_order_id=record.client_order_id,
            economic_key=record.economic_key,
            attempt_number=record.attempt_number,
            plan_id=record.plan_id,
            symbol=record.symbol,
            direction=Direction(record.direction),
            role=OrderRole(record.role),
            stage_index=record.stage_index,
            quantity=Decimal(record.quantity),
            price=Decimal(record.price),
            filled_quantity=Decimal(record.filled_quantity),
            status=DurableIntentStatus(record.status),
            submitted_at_ms=record.submitted_at_ms,
            unknown_at_ms=record.unknown_at_ms,
        )

    @staticmethod
    def _to_absence_observation(
        row: DurableIntentAbsenceObservation,
    ) -> DurableAbsenceObservation:
        if row.query_reference is None:
            raise BoundedAbsenceEvidenceError("durable absence observation has no query provenance")
        return DurableAbsenceObservation(
            account_id=row.account_id,
            client_order_id=row.client_order_id,
            economic_key=row.economic_key,
            source=AbsenceEvidenceSource(row.source),
            query_reference=row.query_reference,
            query_client_order_id=row.query_client_order_id,
            query_economic_key=row.query_economic_key,
            query_started_at_ms=row.query_started_at_ms,
            attempt_number=row.attempt_number,
            client_order_namespace=ClientOrderNamespace(row.client_order_namespace),
            provenance_fingerprint=row.provenance_fingerprint,
            observed_at_ms=row.observed_at_ms,
            stream_watermark_ms=row.stream_watermark_ms,
            found=row.found,
        )

    @classmethod
    def _validated_absence_observation(
        cls,
        row: DurableIntentAbsenceObservation,
        record: DurableOrderIntent,
    ) -> DurableAbsenceObservation:
        cls._validate_lifecycle_timeline(record)
        if row.query_reference is None:
            raise BoundedAbsenceEvidenceError("durable absence observation has no query provenance")
        try:
            observation = UnknownIntentObservation(
                source=AbsenceEvidenceSource(row.source),
                observed_at_ms=row.observed_at_ms,
                stream_watermark_ms=row.stream_watermark_ms,
                found=row.found,
                query_reference=row.query_reference,
                query_client_order_id=row.query_client_order_id,
                query_economic_key=row.query_economic_key,
                query_started_at_ms=row.query_started_at_ms,
                attempt_number=row.attempt_number,
                client_order_namespace=ClientOrderNamespace(row.client_order_namespace),
                provenance_fingerprint=row.provenance_fingerprint,
            )
        except (TypeError, ValueError) as error:
            raise BoundedAbsenceEvidenceError(
                "durable absence observation has invalid time or provenance"
            ) from error
        if (
            record.unknown_at_ms is None
            or observation.query_started_at_ms < record.unknown_at_ms
            or observation.observed_at_ms < record.unknown_at_ms
            or observation.stream_watermark_ms < record.unknown_at_ms
        ):
            raise BoundedAbsenceEvidenceError("durable absence observation predates UNKNOWN time")
        if (
            observation.query_client_order_id != record.client_order_id
            or observation.query_economic_key != record.economic_key
            or observation.attempt_number != record.attempt_number
            or observation.client_order_namespace is not ClientOrderNamespace.NORMAL
        ):
            raise BoundedAbsenceEvidenceError(
                "durable absence observation identity does not match its intent"
            )
        return cls._to_absence_observation(row)

    @staticmethod
    def _validate_lifecycle_timeline(record: DurableOrderIntent) -> None:
        for timestamp in (record.submitted_at_ms, record.unknown_at_ms):
            if timestamp is not None and (
                not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0
            ):
                raise IntentLifecycleError(
                    "durable lifecycle timestamp must be a non-negative integer"
                )
        status = DurableIntentStatus(record.status)
        if status is DurableIntentStatus.UNKNOWN and (
            record.submitted_at_ms is None or record.unknown_at_ms is None
        ):
            raise IntentLifecycleError("UNKNOWN requires durable submission and UNKNOWN timestamps")
        if record.unknown_at_ms is not None and (
            record.submitted_at_ms is None or record.unknown_at_ms < record.submitted_at_ms
        ):
            raise IntentLifecycleError(
                "UNKNOWN timestamp cannot precede durable submission timestamp"
            )

    @staticmethod
    def _fill_event_from_row(row: DurableIntentFill) -> FillEvent:
        occurred_at = row.occurred_at
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)
        else:
            occurred_at = occurred_at.astimezone(UTC)
        return FillEvent(
            account_id=row.account_id,
            trade_id=row.trade_id,
            client_order_id=row.client_order_id,
            symbol=row.symbol,
            side=FillSide(row.side),
            last_quantity=Decimal(row.last_quantity),
            cumulative_quantity=Decimal(row.cumulative_quantity),
            fill_price=Decimal(row.fill_price),
            fee=Decimal(row.fee),
            fee_asset=row.fee_asset,
            occurred_at=occurred_at,
            observation_source=FillObservationSource(row.observation_source),
            observation_reference=row.observation_reference,
        )

    @staticmethod
    def _fill_event_from_fact(row: ExchangeFillFactJournal) -> FillEvent:
        occurred_at = row.exchange_timestamp
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)
        else:
            occurred_at = occurred_at.astimezone(UTC)
        return FillEvent(
            account_id=row.account_id,
            trade_id=row.exchange_trade_id,
            client_order_id=row.client_order_id,
            symbol=row.symbol,
            side=FillSide(row.side),
            last_quantity=Decimal(row.quantity),
            cumulative_quantity=Decimal(row.cumulative_quantity),
            fill_price=Decimal(row.price),
            fee=Decimal(row.fee),
            fee_asset=row.fee_asset,
            occurred_at=occurred_at,
            observation_source=FillObservationSource(row.observation_source),
            observation_reference=row.observation_reference,
            observation_correlation=row.observation_correlation,
            provenance_fingerprint=row.provenance_fingerprint,
        )

    @staticmethod
    def _validate_fill_identity(
        event: FillEvent,
        intent: DurableOrderIntent,
    ) -> None:
        if event.account_id != intent.account_id:
            raise FillLedgerError("fill account does not match its durable intent")
        if event.symbol != intent.symbol:
            raise FillLedgerError("fill symbol does not match its durable intent")
        if event.observation_source is FillObservationSource.LEGACY_MIGRATION:
            raise FillLedgerError("legacy migration provenance cannot submit a new fill")
        direction = Direction(intent.direction)
        role = OrderRole(intent.role)
        expected_side = (
            FillSide.BUY
            if (role is OrderRole.ENTRY and direction is Direction.LONG)
            or (role is not OrderRole.ENTRY and direction is Direction.SHORT)
            else FillSide.SELL
        )
        if event.side is not expected_side:
            raise FillLedgerError("fill side does not match its durable intent")

    @staticmethod
    def _fill_fingerprint(event: FillEvent) -> str:
        canonical = json.dumps(
            {
                "account_id": event.account_id,
                "client_order_id": event.client_order_id,
                "cumulative_quantity": format(event.cumulative_quantity, "f"),
                "fee": format(event.fee, "f"),
                "fee_asset": event.fee_asset,
                "fill_price": format(event.fill_price, "f"),
                "last_quantity": format(event.last_quantity, "f"),
                "observation_correlation": event.observation_correlation,
                "observation_reference": event.observation_reference,
                "observation_source": event.observation_source.value,
                "occurred_at": event.occurred_at.astimezone(UTC).isoformat(),
                "side": event.side.value,
                "symbol": event.symbol,
                "trade_id": event.trade_id,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _policy_fingerprint(policy: ActualRiskPolicy) -> str:
        canonical = json.dumps(
            {
                "account_id": policy.portfolio_envelope.account_id,
                "direction": policy.direction.value,
                "effective_leverage": policy.effective_leverage,
                "exit_fee_rate": format(policy.exit_fee_rate, "f"),
                "funding_buffer_rate": format(policy.funding_buffer_rate, "f"),
                "funding_interval_count": policy.funding_interval_count,
                "account_envelope_fingerprint": policy.portfolio_envelope.fingerprint,
                "account_envelope_scope": policy.portfolio_envelope.account_scope,
                "account_envelope_version": policy.portfolio_envelope.version,
                "plan_id": policy.plan_id,
                "protective_stop_reference": policy.protective_stop_reference,
                "reduce_only_exit_reference": policy.reduce_only_exit_reference,
                "risk_budget": format(policy.risk_budget, "f"),
                "symbol": policy.symbol,
                "worst_stop_exit_price": format(policy.worst_stop_exit_price, "f"),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _legacy_policy_fingerprint(policy: ActualRiskPolicy) -> str:
        canonical = json.dumps(
            {
                "direction": policy.direction.value,
                "effective_leverage": policy.effective_leverage,
                "exit_fee_rate": format(policy.exit_fee_rate, "f"),
                "funding_buffer_rate": format(policy.funding_buffer_rate, "f"),
                "funding_interval_count": policy.funding_interval_count,
                "account_envelope_fingerprint": policy.portfolio_envelope.fingerprint,
                "account_envelope_scope": policy.portfolio_envelope.account_scope,
                "account_envelope_version": policy.portfolio_envelope.version,
                "plan_id": policy.plan_id,
                "protective_stop_reference": policy.protective_stop_reference,
                "reduce_only_exit_reference": policy.reduce_only_exit_reference,
                "risk_budget": format(policy.risk_budget, "f"),
                "symbol": policy.symbol,
                "worst_stop_exit_price": format(policy.worst_stop_exit_price, "f"),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @classmethod
    def _policy_from_row(
        cls,
        session: Session,
        row: DurableActualRiskPolicy | DurableActualRiskPolicyVersion,
    ) -> ActualRiskPolicy:
        account_id = getattr(row, "account_id", V1_DEFAULT_ACCOUNT_ID)
        if account_id != V1_DEFAULT_ACCOUNT_ID:
            raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
        head_row = session.scalar(
            select(DurablePortfolioEnvelopeHead).where(
                DurablePortfolioEnvelopeHead.account_id == account_id,
                DurablePortfolioEnvelopeHead.version == row.account_envelope_version,
            )
        )
        if head_row is not None:
            envelope = cls._envelope_head_from_row(head_row)
        else:
            envelope_row = session.scalar(
                select(DurableAccountPortfolioEnvelope).where(
                    DurableAccountPortfolioEnvelope.account_scope == row.account_envelope_scope,
                    DurableAccountPortfolioEnvelope.version == row.account_envelope_version,
                )
            )
            if envelope_row is None:
                raise DurableRiskPolicyError("durable account portfolio envelope is missing")
            envelope = cls._envelope_from_row(envelope_row)
        if envelope.fingerprint != row.account_envelope_fingerprint:
            raise DurableRiskPolicyError("durable policy envelope fingerprint is invalid")
        policy = ActualRiskPolicy(
            plan_id=row.plan_id,
            symbol=row.symbol,
            direction=Direction(row.direction),
            worst_stop_exit_price=Decimal(row.worst_stop_exit_price),
            exit_fee_rate=Decimal(row.exit_fee_rate),
            funding_buffer_rate=Decimal(row.funding_buffer_rate),
            funding_interval_count=row.funding_interval_count,
            risk_budget=Decimal(row.risk_budget),
            effective_leverage=row.effective_leverage,
            protective_stop_reference=row.protective_stop_reference,
            reduce_only_exit_reference=row.reduce_only_exit_reference,
            portfolio_envelope=envelope,
        )
        if row.policy_fingerprint not in {
            cls._policy_fingerprint(policy),
            cls._legacy_policy_fingerprint(policy),
        }:
            raise DurableRiskPolicyError("durable actual-risk policy fingerprint is invalid")
        return policy

    @staticmethod
    def _to_simulated_protection_evidence(
        row: DurableSimulatedProtection,
    ) -> SimulatedProtectionEvidence:
        return SimulatedProtectionEvidence(
            account_id=row.account_id,
            plan_id=row.plan_id,
            stop_intent_reference=row.stop_intent_reference,
            reduce_only_exit_intent_reference=row.reduce_only_exit_intent_reference,
            protected_position_quantity=Decimal(row.protected_position_quantity),
            stop_intent_ready=row.stop_intent_ready,
            reduce_only_exit_intent_ready=row.reduce_only_exit_intent_ready,
        )

    @staticmethod
    def _to_risk_reduction_requirement(
        row: DurableRiskReductionRequirement,
    ) -> RiskReductionRequirement:
        return RiskReductionRequirement(
            plan_id=row.plan_id,
            symbol=row.symbol,
            direction=Direction(row.direction),
            reason=row.reason,
            required_reduction_quantity=Decimal(row.required_reduction_quantity),
            status=RiskReductionStatus(row.status),
        )

    @classmethod
    def _latest_policy_from_session(
        cls,
        session: Session,
        plan_id: str,
    ) -> ActualRiskPolicy:
        base = session.get(DurableActualRiskPolicy, plan_id)
        if base is None:
            raise DurableRiskPolicyError("RISK_POLICY_MISSING")
        if base.account_id != V1_DEFAULT_ACCOUNT_ID:
            raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
        return cls._validated_policy_lineage(session, base)

    @classmethod
    def _validated_policy_lineage(
        cls,
        session: Session,
        base: DurableActualRiskPolicy,
    ) -> ActualRiskPolicy:
        base_policy = cls._policy_from_row(session, base)
        latest = base_policy
        expected_version = 2
        versions = session.scalars(
            select(DurableActualRiskPolicyVersion)
            .where(
                DurableActualRiskPolicyVersion.plan_id == base.plan_id,
                DurableActualRiskPolicyVersion.account_id == base.account_id,
            )
            .order_by(DurableActualRiskPolicyVersion.version.asc())
        )
        for row in versions:
            if row.version != expected_version:
                raise DurableRiskPolicyError(
                    "durable actual-risk policy version lineage is not contiguous"
                )
            policy = cls._policy_from_row(session, row)
            if (
                policy.plan_id != base_policy.plan_id
                or policy.symbol != base_policy.symbol
                or policy.direction is not base_policy.direction
                or policy.protective_stop_reference != base_policy.protective_stop_reference
                or policy.reduce_only_exit_reference != base_policy.reduce_only_exit_reference
                or policy.portfolio_envelope.fingerprint
                != base_policy.portfolio_envelope.fingerprint
            ):
                raise DurableRiskPolicyError(
                    "durable policy lineage changed plan, side, symbol, or protection identity"
                )
            latest = policy
            expected_version += 1
        return latest

    @staticmethod
    def _policy_row_values(
        policy: ActualRiskPolicy,
        *,
        fingerprint: str,
    ) -> dict[str, object]:
        return {
            "account_id": policy.portfolio_envelope.account_id,
            "symbol": policy.symbol,
            "direction": policy.direction.value,
            "worst_stop_exit_price": format(policy.worst_stop_exit_price, "f"),
            "exit_fee_rate": format(policy.exit_fee_rate, "f"),
            "funding_buffer_rate": format(policy.funding_buffer_rate, "f"),
            "funding_interval_count": policy.funding_interval_count,
            "risk_budget": format(policy.risk_budget, "f"),
            "max_symbol_exposure_usdt": format(policy.max_symbol_exposure_usdt, "f"),
            "max_total_exposure_usdt": format(policy.max_total_exposure_usdt, "f"),
            "existing_symbol_exposure_usdt": format(policy.existing_symbol_exposure_usdt, "f"),
            "existing_total_exposure_usdt": format(policy.existing_total_exposure_usdt, "f"),
            "effective_leverage": policy.effective_leverage,
            "required_reserve_usdt": format(policy.required_reserve_usdt, "f"),
            "effective_equity_usdt": format(policy.effective_equity_usdt, "f"),
            "protective_stop_reference": policy.protective_stop_reference,
            "reduce_only_exit_reference": policy.reduce_only_exit_reference,
            "account_envelope_scope": policy.portfolio_envelope.account_scope,
            "account_envelope_version": policy.portfolio_envelope.version,
            "account_envelope_fingerprint": policy.portfolio_envelope.fingerprint,
            "policy_fingerprint": fingerprint,
        }

    @staticmethod
    def _envelope_from_row(row: DurableAccountPortfolioEnvelope) -> AccountPortfolioEnvelope:
        try:
            raw_slices = row.exposure_slices
            if not isinstance(raw_slices, list):
                raise TypeError("durable exposure slices must be a list")
            slices = tuple(
                PortfolioExposureSlice(
                    account_id=str(item.get("account_id", row.account_id)),
                    slice_id=str(item["slice_id"]),
                    plan_id=str(item["plan_id"]),
                    symbol=str(item["symbol"]),
                    direction=Direction(str(item["direction"])),
                    notional_usdt=Decimal(str(item["notional_usdt"])),
                    leverage=int(str(item["leverage"])),
                    required_margin_usdt=Decimal(str(item["required_margin_usdt"])),
                    source_state=ExposureSourceState(str(item["source_state"])),
                )
                for raw_item in raw_slices
                if isinstance(raw_item, dict)
                for item in (raw_item,)
            )
            if len(slices) != len(raw_slices):
                raise TypeError("durable exposure slices must be structured")
            envelope = AccountPortfolioEnvelope(
                account_scope=row.account_scope,
                account_id=row.account_id,
                version=row.version,
                verified_account_equity_usdt=Decimal(row.verified_account_equity_usdt),
                bot_equity_cap_usdt=Decimal(row.bot_equity_cap_usdt),
                required_reserve_usdt=Decimal(row.required_reserve_usdt),
                max_total_exposure_usdt=Decimal(row.max_total_exposure_usdt),
                max_symbol_exposure_usdt=Decimal(row.max_symbol_exposure_usdt),
                max_required_margin_usdt=Decimal(row.max_required_margin_usdt),
                daily_remaining_risk_usdt=Decimal(row.daily_remaining_risk_usdt),
                weekly_remaining_risk_usdt=Decimal(row.weekly_remaining_risk_usdt),
                open_position_count=row.open_position_count,
                pending_order_count=row.pending_order_count,
                exposure_slices=slices,
                reconciliation_required=row.reconciliation_required,
                fingerprint=row.envelope_fingerprint,
            )
        except (KeyError, TypeError, ValueError, FillLedgerError) as error:
            raise DurableRiskPolicyError("durable account envelope is invalid") from error
        return envelope

    @staticmethod
    def _envelope_head_from_row(row: DurablePortfolioEnvelopeHead) -> AccountPortfolioEnvelope:
        try:
            raw_slices = row.exposure_slices
            raw_caps = row.symbol_exposure_caps_usdt
            if not isinstance(raw_slices, list) or not isinstance(raw_caps, dict):
                raise TypeError("durable account envelope head must be structured")
            slices = tuple(
                PortfolioExposureSlice(
                    account_id=str(item.get("account_id", row.account_id)),
                    slice_id=str(item["slice_id"]),
                    plan_id=str(item["plan_id"]),
                    symbol=str(item["symbol"]),
                    direction=Direction(str(item["direction"])),
                    notional_usdt=Decimal(str(item["notional_usdt"])),
                    leverage=int(str(item["leverage"])),
                    required_margin_usdt=Decimal(str(item["required_margin_usdt"])),
                    source_state=ExposureSourceState(str(item["source_state"])),
                )
                for raw_item in raw_slices
                if isinstance(raw_item, dict)
                for item in (raw_item,)
            )
            if len(slices) != len(raw_slices):
                raise TypeError("durable envelope head slices must be structured")
            return AccountPortfolioEnvelope(
                account_scope=row.account_scope,
                account_id=row.account_id,
                version=row.version,
                verified_account_equity_usdt=Decimal(row.verified_account_equity_usdt),
                bot_equity_cap_usdt=Decimal(row.bot_equity_cap_usdt),
                required_reserve_usdt=Decimal(row.required_reserve_usdt),
                max_total_exposure_usdt=Decimal(row.max_total_exposure_usdt),
                max_symbol_exposure_usdt=max(Decimal(str(value)) for value in raw_caps.values()),
                symbol_exposure_caps_usdt={
                    str(symbol): Decimal(str(cap)) for symbol, cap in raw_caps.items()
                },
                max_required_margin_usdt=Decimal(row.max_required_margin_usdt),
                daily_remaining_risk_usdt=Decimal(row.daily_remaining_risk_usdt),
                weekly_remaining_risk_usdt=Decimal(row.weekly_remaining_risk_usdt),
                open_position_count=row.open_position_count,
                pending_order_count=row.pending_order_count,
                exposure_slices=slices,
                reconciliation_required=row.reconciliation_required,
                fingerprint=row.envelope_fingerprint,
                # Historical heads have NULL provenance; recent heads preserve the
                # operator-supplied timestamp that was part of their fingerprint.
                effective_from=(
                    None
                    if row.fingerprint_effective_from is None
                    else DurableIntentLedger._as_utc(row.fingerprint_effective_from)
                ),
                superseded_by=row.superseded_by,
            )
        except (KeyError, TypeError, ValueError, FillLedgerError) as error:
            raise DurableRiskPolicyError("durable account envelope head is invalid") from error

    @classmethod
    def _register_portfolio_envelope(
        cls,
        session: Session,
        envelope: AccountPortfolioEnvelope,
    ) -> None:
        if envelope.account_id != V1_DEFAULT_ACCOUNT_ID:
            raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
        if envelope.superseded_by is not None:
            raise DurableRiskPolicyError(
                "portfolio envelope supersession is derived from append-only head history"
            )
        safety_state = cls._account_safety_state_for_update(session, envelope.account_id)
        current_head = session.scalar(
            select(DurablePortfolioEnvelopeHead)
            .where(DurablePortfolioEnvelopeHead.account_id == envelope.account_id)
            .order_by(DurablePortfolioEnvelopeHead.version.desc())
            .limit(1)
        )
        head_advanced = False
        if current_head is not None:
            if current_head.version == envelope.version:
                if current_head.envelope_fingerprint != envelope.fingerprint:
                    raise DurableRiskPolicyError(
                        "account envelope version already has conflicting durable facts"
                    )
            elif envelope.version <= current_head.version:
                raise DurableRiskPolicyError("ACCOUNT_ENVELOPE_STALE")
            elif envelope.version != current_head.version + 1:
                raise DurableRiskPolicyError("account envelope versions must be contiguous")
            else:
                session.add(
                    DurablePortfolioEnvelopeSupersession(
                        account_id=envelope.account_id,
                        superseded_version=current_head.version,
                        superseded_by=envelope.version,
                    )
                )
                session.add(
                    DurablePortfolioEnvelopeHead(
                        account_id=envelope.account_id,
                        account_scope=envelope.account_scope,
                        version=envelope.version,
                        verified_account_equity_usdt=format(
                            envelope.verified_account_equity_usdt, "f"
                        ),
                        bot_equity_cap_usdt=format(envelope.bot_equity_cap_usdt, "f"),
                        required_reserve_usdt=format(envelope.required_reserve_usdt, "f"),
                        max_total_exposure_usdt=format(envelope.max_total_exposure_usdt, "f"),
                        symbol_exposure_caps_usdt={
                            symbol: format(cap, "f")
                            for symbol, cap in (envelope.symbol_exposure_caps_usdt or {}).items()
                        },
                        max_required_margin_usdt=format(envelope.max_required_margin_usdt, "f"),
                        daily_remaining_risk_usdt=format(envelope.daily_remaining_risk_usdt, "f"),
                        weekly_remaining_risk_usdt=format(envelope.weekly_remaining_risk_usdt, "f"),
                        open_position_count=envelope.open_position_count,
                        pending_order_count=envelope.pending_order_count,
                        reconciliation_required=envelope.reconciliation_required,
                        exposure_slices=[
                            item.canonical_record() for item in envelope.exposure_slices
                        ],
                        envelope_fingerprint=envelope.fingerprint,
                        fingerprint_effective_from=envelope.effective_from,
                        effective_from=envelope.effective_from or datetime.now(UTC),
                        superseded_by=envelope.superseded_by,
                    )
                )
                safety_state.envelope_version = envelope.version
                safety_state.envelope_fingerprint = envelope.fingerprint
                safety_state.grant_generation += 1
                safety_state.recovery_required = True
                cls._cancel_known_active_entry_intents(session, envelope.account_id)
                head_advanced = True
        else:
            session.add(
                DurablePortfolioEnvelopeHead(
                    account_id=envelope.account_id,
                    account_scope=envelope.account_scope,
                    version=envelope.version,
                    verified_account_equity_usdt=format(envelope.verified_account_equity_usdt, "f"),
                    bot_equity_cap_usdt=format(envelope.bot_equity_cap_usdt, "f"),
                    required_reserve_usdt=format(envelope.required_reserve_usdt, "f"),
                    max_total_exposure_usdt=format(envelope.max_total_exposure_usdt, "f"),
                    symbol_exposure_caps_usdt={
                        symbol: format(cap, "f")
                        for symbol, cap in (envelope.symbol_exposure_caps_usdt or {}).items()
                    },
                    max_required_margin_usdt=format(envelope.max_required_margin_usdt, "f"),
                    daily_remaining_risk_usdt=format(envelope.daily_remaining_risk_usdt, "f"),
                    weekly_remaining_risk_usdt=format(envelope.weekly_remaining_risk_usdt, "f"),
                    open_position_count=envelope.open_position_count,
                    pending_order_count=envelope.pending_order_count,
                    reconciliation_required=envelope.reconciliation_required,
                    exposure_slices=[item.canonical_record() for item in envelope.exposure_slices],
                    envelope_fingerprint=envelope.fingerprint,
                    fingerprint_effective_from=envelope.effective_from,
                    effective_from=envelope.effective_from or datetime.now(UTC),
                    superseded_by=envelope.superseded_by,
                )
            )
            safety_state.envelope_version = envelope.version
            safety_state.envelope_fingerprint = envelope.fingerprint
        existing = session.scalar(
            select(DurableAccountPortfolioEnvelope).where(
                DurableAccountPortfolioEnvelope.account_scope == envelope.account_scope,
                DurableAccountPortfolioEnvelope.version == envelope.version,
            )
        )
        if existing is not None:
            if (
                existing.account_id != envelope.account_id
                or existing.envelope_fingerprint != envelope.fingerprint
            ):
                raise DurableRiskPolicyError(
                    "account envelope version already has conflicting durable facts"
                )
        else:
            session.add(
                DurableAccountPortfolioEnvelope(
                    account_id=envelope.account_id,
                    account_scope=envelope.account_scope,
                    version=envelope.version,
                    verified_account_equity_usdt=format(envelope.verified_account_equity_usdt, "f"),
                    bot_equity_cap_usdt=format(envelope.bot_equity_cap_usdt, "f"),
                    required_reserve_usdt=format(envelope.required_reserve_usdt, "f"),
                    max_total_exposure_usdt=format(envelope.max_total_exposure_usdt, "f"),
                    max_symbol_exposure_usdt=format(envelope.max_symbol_exposure_usdt, "f"),
                    max_required_margin_usdt=format(envelope.max_required_margin_usdt, "f"),
                    daily_remaining_risk_usdt=format(envelope.daily_remaining_risk_usdt, "f"),
                    weekly_remaining_risk_usdt=format(envelope.weekly_remaining_risk_usdt, "f"),
                    open_position_count=envelope.open_position_count,
                    pending_order_count=envelope.pending_order_count,
                    exposure_slices=[item.canonical_record() for item in envelope.exposure_slices],
                    reconciliation_required=envelope.reconciliation_required,
                    envelope_fingerprint=envelope.fingerprint,
                )
            )
        if head_advanced:
            session.flush()
            cls._recalculate_account_after_envelope_change(
                session,
                safety_state=safety_state,
                account_id=envelope.account_id,
            )

    @classmethod
    def _assert_account_envelope_consistency(
        cls,
        session: Session,
        envelope: AccountPortfolioEnvelope,
        *,
        excluding_plan_id: str | None = None,
    ) -> None:
        if envelope.account_id != V1_DEFAULT_ACCOUNT_ID:
            raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
        if envelope.reconciliation_required:
            raise DurableRiskPolicyError("account portfolio envelope requires reconciliation")
        current_head = session.scalar(
            select(DurablePortfolioEnvelopeHead)
            .where(DurablePortfolioEnvelopeHead.account_id == envelope.account_id)
            .order_by(DurablePortfolioEnvelopeHead.version.desc())
            .limit(1)
        )
        if (
            current_head is None
            or current_head.version != envelope.version
            or current_head.envelope_fingerprint != envelope.fingerprint
        ):
            raise DurableRiskPolicyError("ACCOUNT_ENVELOPE_STALE")

    @classmethod
    def _projected_portfolio_risk(
        cls,
        session: Session,
        policy: ActualRiskPolicy,
        *,
        proposed_intent: SimulatedOrderIntent | None,
        current_plan_result: PositionRiskAssessment | None = None,
    ) -> PortfolioRiskAssessment:
        current_head = session.scalar(
            select(DurablePortfolioEnvelopeHead)
            .where(DurablePortfolioEnvelopeHead.account_id == policy.portfolio_envelope.account_id)
            .order_by(DurablePortfolioEnvelopeHead.version.desc())
            .limit(1)
        )
        if current_head is None:
            raise DurableRiskPolicyError("ACCOUNT_ENVELOPE_HEAD_MISSING")
        envelope = cls._envelope_head_from_row(current_head)
        cls._assert_account_envelope_consistency(session, envelope)
        slices_by_id = {item.slice_id: item for item in envelope.exposure_slices}

        def add_slice(item: PortfolioExposureSlice) -> None:
            existing = slices_by_id.get(item.slice_id)
            if existing is not None and existing != item:
                raise DurableRiskPolicyError("portfolio exposure slice identity is conflicting")
            slices_by_id[item.slice_id] = item

        position_exposure = ZERO
        projected_plan_stop_risk = ZERO
        aggregate_stop_risk = ZERO
        for state in session.scalars(
            select(DurableActualRiskState).where(
                DurableActualRiskState.account_id == envelope.account_id
            )
        ):
            state_policy = cls._latest_policy_from_session(session, state.plan_id)
            if state_policy.portfolio_envelope.account_id != envelope.account_id:
                raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
            notional = Decimal(state.actual_notional_usdt)
            stop_risk = Decimal(state.actual_stop_risk)
            if current_plan_result is not None and state.plan_id == policy.plan_id:
                notional = current_plan_result.actual_notional_usdt
                stop_risk = current_plan_result.actual_stop_risk
            position_exposure += notional
            aggregate_stop_risk += stop_risk
            if notional > ZERO:
                add_slice(
                    PortfolioExposureSlice(
                        account_id=envelope.account_id,
                        slice_id=f"position:{state.plan_id}",
                        plan_id=state.plan_id,
                        symbol=state_policy.symbol,
                        direction=state_policy.direction,
                        notional_usdt=notional,
                        leverage=state_policy.effective_leverage,
                        required_margin_usdt=(notional / Decimal(state_policy.effective_leverage)),
                        source_state=ExposureSourceState.CONFIRMED,
                    )
                )
            if state.plan_id == policy.plan_id:
                projected_plan_stop_risk += stop_risk

        active_statuses = {
            DurableIntentStatus.PREPARED.value,
            DurableIntentStatus.SUBMITTING.value,
            DurableIntentStatus.UNKNOWN.value,
            DurableIntentStatus.NEW.value,
            DurableIntentStatus.PARTIALLY_FILLED.value,
        }
        pending_exposure = ZERO
        for row in session.scalars(
            select(DurableOrderIntent).where(DurableOrderIntent.account_id == envelope.account_id)
        ):
            if row.role != OrderRole.ENTRY.value or row.status not in active_statuses:
                continue
            remaining_quantity = max(Decimal(row.quantity) - Decimal(row.filled_quantity), ZERO)
            pending_notional = remaining_quantity * Decimal(row.price)
            row_policy = cls._latest_policy_from_session(session, row.plan_id)
            if row_policy.portfolio_envelope.account_id != envelope.account_id:
                raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
            pending_exposure += pending_notional
            if pending_notional > ZERO:
                add_slice(
                    PortfolioExposureSlice(
                        account_id=envelope.account_id,
                        slice_id=f"pending:{row.client_order_id}",
                        plan_id=row.plan_id,
                        symbol=row.symbol,
                        direction=Direction(row.direction),
                        notional_usdt=pending_notional,
                        leverage=row_policy.effective_leverage,
                        required_margin_usdt=(
                            pending_notional / Decimal(row_policy.effective_leverage)
                        ),
                        source_state=(
                            ExposureSourceState.PARTIALLY_FILLED
                            if row.status == DurableIntentStatus.PARTIALLY_FILLED.value
                            else ExposureSourceState.PENDING
                        ),
                    )
                )
            pending_stop_risk = cls._projected_pending_stop_risk(
                row_policy,
                quantity=remaining_quantity,
                entry_price=Decimal(row.price),
            )
            aggregate_stop_risk += pending_stop_risk
            if row.plan_id == policy.plan_id:
                projected_plan_stop_risk += pending_stop_risk

        if proposed_intent is not None:
            if proposed_intent.account_id != envelope.account_id:
                raise DurableRiskPolicyError("V1_SECOND_ACCOUNT_UNSUPPORTED")
            proposed_notional = proposed_intent.quantity * proposed_intent.price
            pending_exposure += proposed_notional
            add_slice(
                PortfolioExposureSlice(
                    account_id=envelope.account_id,
                    slice_id=f"proposed:{proposed_intent.client_order_id}",
                    plan_id=policy.plan_id,
                    symbol=proposed_intent.symbol,
                    direction=proposed_intent.direction,
                    notional_usdt=proposed_notional,
                    leverage=policy.effective_leverage,
                    required_margin_usdt=(proposed_notional / Decimal(policy.effective_leverage)),
                    source_state=ExposureSourceState.PENDING,
                )
            )
            proposed_stop_risk = cls._projected_pending_stop_risk(
                policy,
                quantity=proposed_intent.quantity,
                entry_price=proposed_intent.price,
            )
            projected_plan_stop_risk += proposed_stop_risk
            aggregate_stop_risk += proposed_stop_risk

        exposure_slices = tuple(sorted(slices_by_id.values(), key=lambda item: item.slice_id))
        exposures_by_symbol: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for item in exposure_slices:
            exposures_by_symbol[item.symbol] += item.notional_usdt
        aggregate_symbol = exposures_by_symbol.get(policy.symbol, ZERO)
        aggregate_total = sum((item.notional_usdt for item in exposure_slices), ZERO)
        aggregate_reserve = envelope.required_reserve_usdt
        required_margin = (
            sum((item.required_margin_usdt for item in exposure_slices), ZERO) + aggregate_reserve
        )
        reason: str | None = None
        for symbol, exposure in sorted(exposures_by_symbol.items()):
            try:
                symbol_cap = envelope.symbol_exposure_cap_usdt(symbol)
            except FillLedgerError as error:
                raise DurableRiskPolicyError("ACCOUNT_SYMBOL_CAP_MISSING") from error
            if exposure > symbol_cap:
                reason = f"ACCOUNT_SYMBOL_EXPOSURE_LIMIT_BREACH:{symbol}"
                break
        if reason is not None:
            pass
        elif aggregate_total > envelope.max_total_exposure_usdt:
            reason = "PROJECTED_TOTAL_EXPOSURE_LIMIT_BREACH"
        elif required_margin > min(
            envelope.max_required_margin_usdt,
            envelope.effective_equity_usdt,
        ):
            reason = "PROJECTED_MARGIN_REQUIREMENT_BREACH"
        elif projected_plan_stop_risk > policy.risk_budget:
            reason = "PROJECTED_STOP_RISK_BREACH"
        elif aggregate_stop_risk > envelope.daily_remaining_risk_usdt:
            reason = "PROJECTED_DAILY_RISK_LIMIT_BREACH"
        elif aggregate_stop_risk > envelope.weekly_remaining_risk_usdt:
            reason = "PROJECTED_WEEKLY_RISK_LIMIT_BREACH"
        return PortfolioRiskAssessment(
            pending_order_exposure_usdt=pending_exposure,
            position_exposure_usdt=position_exposure,
            aggregate_symbol_exposure_usdt=aggregate_symbol,
            aggregate_total_exposure_usdt=aggregate_total,
            required_margin_usdt=required_margin,
            reserve_usdt=aggregate_reserve,
            projected_plan_stop_risk=projected_plan_stop_risk,
            blocked=reason is not None,
            reason=reason,
            exposure_slices=exposure_slices,
        )

    @staticmethod
    def _projected_pending_stop_risk(
        policy: ActualRiskPolicy,
        *,
        quantity: Decimal,
        entry_price: Decimal,
    ) -> Decimal:
        price_loss = (
            entry_price - policy.worst_stop_exit_price
            if policy.direction is Direction.LONG
            else policy.worst_stop_exit_price - entry_price
        )
        if price_loss < ZERO:
            raise DurableRiskPolicyError("projected stop is on the non-loss side")
        return (
            quantity * price_loss
            + quantity * policy.worst_stop_exit_price * policy.exit_fee_rate
            + quantity * entry_price * policy.funding_buffer_rate * policy.funding_interval_count
        )

    @classmethod
    def _apply_aggregate_risk(
        cls,
        session: Session,
        policy: ActualRiskPolicy,
        result: PositionRiskAssessment,
    ) -> PositionRiskAssessment:
        portfolio = cls._projected_portfolio_risk(
            session,
            policy,
            proposed_intent=None,
            current_plan_result=result,
        )
        transient_protection_gap = result.reason == "SIMULATED_PROTECTION_MISSING"
        if result.pending_entries_blocked and not transient_protection_gap or not portfolio.blocked:
            return PositionRiskAssessment(
                position_quantity=result.position_quantity,
                average_entry_price=result.average_entry_price,
                actual_notional_usdt=result.actual_notional_usdt,
                actual_required_margin_usdt=portfolio.required_margin_usdt,
                actual_stop_risk=result.actual_stop_risk,
                pending_entries_blocked=result.pending_entries_blocked,
                hard_halted=result.hard_halted,
                reason=result.reason,
            )
        return PositionRiskAssessment(
            position_quantity=result.position_quantity,
            average_entry_price=result.average_entry_price,
            actual_notional_usdt=result.actual_notional_usdt,
            actual_required_margin_usdt=portfolio.required_margin_usdt,
            actual_stop_risk=result.actual_stop_risk,
            pending_entries_blocked=True,
            hard_halted=result.hard_halted,
            reason=portfolio.reason,
        )

    @staticmethod
    def _upsert_risk_reduction_requirement(
        session: Session,
        policy: ActualRiskPolicy,
        result: PositionRiskAssessment,
    ) -> None:
        reduction_reasons = {
            "ACTUAL_EXPOSURE_LIMIT_BREACH",
            "ACTUAL_MARGIN_REQUIREMENT_BREACH",
            "ACTUAL_STOP_RISK_BREACH",
            "PROJECTED_SYMBOL_EXPOSURE_LIMIT_BREACH",
            "PROJECTED_TOTAL_EXPOSURE_LIMIT_BREACH",
            "PROJECTED_MARGIN_REQUIREMENT_BREACH",
            "PROJECTED_STOP_RISK_BREACH",
            "PROJECTED_DAILY_RISK_LIMIT_BREACH",
            "PROJECTED_WEEKLY_RISK_LIMIT_BREACH",
            "SIMULATED_PROTECTION_MISSING",
            "SIMULATED_PROTECTION_QUANTITY_MISMATCH",
        }
        if result.reason not in reduction_reasons and not (
            result.reason is not None
            and result.reason.startswith("ACCOUNT_SYMBOL_EXPOSURE_LIMIT_BREACH:")
        ):
            return
        requirement = session.get(DurableRiskReductionRequirement, policy.plan_id)
        if requirement is None:
            session.add(
                DurableRiskReductionRequirement(
                    plan_id=policy.plan_id,
                    account_id=policy.portfolio_envelope.account_id,
                    symbol=policy.symbol,
                    direction=policy.direction.value,
                    reason=result.reason,
                    required_reduction_quantity=format(result.position_quantity, "f"),
                    status=RiskReductionStatus.OPEN.value,
                )
            )
            return
        requirement.reason = result.reason
        requirement.required_reduction_quantity = format(result.position_quantity, "f")
        requirement.status = RiskReductionStatus.OPEN.value
        requirement.updated_at = datetime.now(UTC)

    @staticmethod
    def _cancel_unfilled_superseding_attempts(
        session: Session,
        authoritative_intent: DurableOrderIntent,
    ) -> None:
        later_attempts = session.scalars(
            select(DurableOrderIntent).where(
                DurableOrderIntent.account_id == authoritative_intent.account_id,
                DurableOrderIntent.economic_key == authoritative_intent.economic_key,
                DurableOrderIntent.attempt_number > authoritative_intent.attempt_number,
            )
        )
        for later in later_attempts:
            if Decimal(later.filled_quantity) != ZERO:
                continue
            if DurableIntentStatus(later.status) is DurableIntentStatus.FILLED:
                continue
            later.status = DurableIntentStatus.CANCEL_REQUIRED.value
            later.updated_at = datetime.now(UTC)

    @staticmethod
    def _cancel_known_active_entry_intents(session: Session, account_id: str) -> None:
        active_statuses = {
            DurableIntentStatus.PREPARED.value,
            DurableIntentStatus.SUBMITTING.value,
            DurableIntentStatus.UNKNOWN.value,
            DurableIntentStatus.NEW.value,
            DurableIntentStatus.PARTIALLY_FILLED.value,
        }
        for row in session.scalars(
            select(DurableOrderIntent).where(
                DurableOrderIntent.account_id == account_id,
                DurableOrderIntent.role == OrderRole.ENTRY.value,
                DurableOrderIntent.status.in_(active_statuses),
            )
        ):
            row.status = DurableIntentStatus.CANCEL_REQUIRED.value
            row.updated_at = datetime.now(UTC)

    @staticmethod
    def _fence_filled_active_intents(session: Session, account_id: str) -> None:
        active_statuses = {
            DurableIntentStatus.PREPARED.value,
            DurableIntentStatus.SUBMITTING.value,
            DurableIntentStatus.UNKNOWN.value,
            DurableIntentStatus.NEW.value,
            DurableIntentStatus.PARTIALLY_FILLED.value,
        }
        for row in session.scalars(
            select(DurableOrderIntent).where(
                DurableOrderIntent.account_id == account_id,
                DurableOrderIntent.status.in_(active_statuses),
            )
        ):
            filled_quantity = Decimal(row.filled_quantity)
            if filled_quantity <= ZERO:
                continue
            row.status = (
                DurableIntentStatus.FILLED.value
                if filled_quantity >= Decimal(row.quantity)
                else DurableIntentStatus.CANCEL_REQUIRED.value
            )
            row.updated_at = datetime.now(UTC)

    @classmethod
    def _hard_block_after_fill_risk_failure(
        cls,
        session: Session,
        *,
        safety_state: DurableAccountSafetyState,
        policy_row: DurableActualRiskPolicy,
        receipt: FillLedgerReceipt,
        error: Exception,
    ) -> None:
        """Keep the exchange fact and persist a conservative recovery boundary."""
        state = session.get(DurableActualRiskState, policy_row.plan_id)
        quantity = receipt.filled_quantity
        average_price = receipt.average_fill_price
        try:
            fills = cls._fill_ledger_for_plan(session, policy_row.plan_id)
            quantity = fills.filled_quantity
            average_price = fills.average_fill_price
        except FillLedgerError:
            if state is not None:
                quantity = max(quantity, Decimal(state.position_quantity))
        cls._hard_block_policy_after_risk_failure(
            session,
            policy_row=policy_row,
            quantity=quantity,
            average_price=average_price,
            error=error,
        )
        safety_state.failure_epoch += 1
        safety_state.grant_generation += 1
        safety_state.recovery_required = True
        safety_state.updated_at = datetime.now(UTC)
        cls._cancel_known_active_entry_intents(session, policy_row.account_id)

    @classmethod
    def _hard_block_policy_after_risk_failure(
        cls,
        session: Session,
        *,
        policy_row: DurableActualRiskPolicy,
        quantity: Decimal,
        average_price: Decimal | None,
        error: Exception,
    ) -> None:
        """Persist a plan-level hard block without discarding its durable facts."""
        reason = "RISK_EVALUATION_FAILED"
        state = session.get(DurableActualRiskState, policy_row.plan_id)
        if state is None:
            state = DurableActualRiskState(
                plan_id=policy_row.plan_id,
                account_id=policy_row.account_id,
            )
            session.add(state)
        state.position_quantity = format(quantity, "f")
        state.average_entry_price = None if average_price is None else format(average_price, "f")
        if average_price is not None:
            notional = quantity * average_price
            state.actual_notional_usdt = format(notional, "f")
            if policy_row.effective_leverage > 0:
                state.actual_required_margin_usdt = format(
                    notional / Decimal(policy_row.effective_leverage),
                    "f",
                )
        state.pending_entries_blocked = True
        state.hard_halted = True
        state.reason = reason
        state.updated_at = datetime.now(UTC)
        if quantity > ZERO:
            requirement = session.get(DurableRiskReductionRequirement, policy_row.plan_id)
            if requirement is None:
                session.add(
                    DurableRiskReductionRequirement(
                        plan_id=policy_row.plan_id,
                        account_id=policy_row.account_id,
                        symbol=policy_row.symbol,
                        direction=policy_row.direction,
                        reason=reason,
                        required_reduction_quantity=format(quantity, "f"),
                        status=RiskReductionStatus.OPEN.value,
                    )
                )
            else:
                requirement.reason = reason
                requirement.required_reduction_quantity = format(quantity, "f")
                requirement.status = RiskReductionStatus.OPEN.value
                requirement.updated_at = datetime.now(UTC)
        # Retain only the stable error class in durable state; exception text may contain data.
        _ = type(error).__name__

    @classmethod
    def _recalculate_account_after_envelope_change(
        cls,
        session: Session,
        *,
        safety_state: DurableAccountSafetyState,
        account_id: str,
    ) -> None:
        """Apply a new account risk authority to all existing positions atomically."""
        policies = tuple(
            session.scalars(
                select(DurableActualRiskPolicy).where(
                    DurableActualRiskPolicy.account_id == account_id
                )
            )
        )
        results: list[PositionRiskAssessment] = []
        risk_evaluation_failed = False
        for policy_row in policies:
            try:
                results.append(cls._recalculate_actual_risk(session, policy_row))
            except (
                DurableRiskPolicyError,
                FillLedgerError,
                ValueError,
                ArithmeticError,
            ) as error:
                risk_evaluation_failed = True
                state = session.get(DurableActualRiskState, policy_row.plan_id)
                quantity = ZERO if state is None else Decimal(state.position_quantity)
                average_price = (
                    None
                    if state is None or state.average_entry_price is None
                    else Decimal(state.average_entry_price)
                )
                try:
                    fills = cls._fill_ledger_for_plan(session, policy_row.plan_id)
                    quantity = fills.filled_quantity
                    average_price = fills.average_fill_price
                except (FillLedgerError, ValueError, ArithmeticError):
                    pass
                cls._hard_block_policy_after_risk_failure(
                    session,
                    policy_row=policy_row,
                    quantity=quantity,
                    average_price=average_price,
                    error=error,
                )
        if risk_evaluation_failed:
            safety_state.failure_epoch += 1
            safety_state.grant_generation += 1
            safety_state.recovery_required = True
            safety_state.updated_at = datetime.now(UTC)
        if risk_evaluation_failed or any(result.pending_entries_blocked for result in results):
            cls._cancel_known_active_entry_intents(session, account_id)

    @classmethod
    def _materialize_simulated_protection_in_session(
        cls,
        session: Session,
        policy_row: DurableActualRiskPolicy,
    ) -> None:
        protection = session.get(DurableSimulatedProtection, policy_row.plan_id)
        if protection is None:
            raise DurableRiskPolicyError("durable simulated protection evidence is missing")
        policy = cls._latest_policy_from_session(session, policy_row.plan_id)
        if (
            protection.stop_intent_reference != policy.protective_stop_reference
            or protection.reduce_only_exit_intent_reference != policy.reduce_only_exit_reference
        ):
            raise DurableRiskPolicyError("durable protection references do not match the policy")
        fills = cls._fill_ledger_for_plan(session, policy.plan_id)
        protection.protected_position_quantity = format(fills.filled_quantity, "f")
        protection.stop_intent_ready = fills.filled_quantity > ZERO
        protection.reduce_only_exit_intent_ready = fills.filled_quantity > ZERO
        protection.updated_at = datetime.now(UTC)

    @classmethod
    def _recalculate_actual_risk(
        cls,
        session: Session,
        policy_row: DurableActualRiskPolicy,
    ) -> PositionRiskAssessment:
        policy = cls._latest_policy_from_session(session, policy_row.plan_id)
        protection = session.get(DurableSimulatedProtection, policy.plan_id)
        state = session.get(DurableActualRiskState, policy.plan_id)
        if protection is None or state is None:
            raise DurableRiskPolicyError("durable policy state or protection evidence is missing")
        if (
            protection.stop_intent_reference != policy.protective_stop_reference
            or protection.reduce_only_exit_intent_reference != policy.reduce_only_exit_reference
        ):
            raise DurableRiskPolicyError("durable protection references do not match the policy")
        fills = cls._fill_ledger_for_plan(session, policy.plan_id)
        signed_simulated_position_quantity = (
            fills.filled_quantity if policy.direction is Direction.LONG else -fills.filled_quantity
        )
        protected_quantity = Decimal(protection.protected_position_quantity)
        protection_quantity_matches = protected_quantity == fills.filled_quantity
        protection_evidence = cls._to_simulated_protection_evidence(protection)
        protection_ready = protection_evidence.is_ready
        result = evaluate_simulated_position_risk(
            direction=policy.direction,
            signed_simulated_position_quantity=signed_simulated_position_quantity,
            fills=fills,
            worst_stop_exit_price=policy.worst_stop_exit_price,
            exit_fee_rate=policy.exit_fee_rate,
            funding_buffer_rate=policy.funding_buffer_rate,
            funding_interval_count=policy.funding_interval_count,
            risk_budget=policy.risk_budget,
            simulated_protection_ready=protection_ready and protection_quantity_matches,
            unprotected_reason=(
                "SIMULATED_PROTECTION_QUANTITY_MISMATCH"
                if protection_ready and not protection_quantity_matches
                else "SIMULATED_PROTECTION_MISSING"
            ),
            max_symbol_exposure_usdt=policy.max_symbol_exposure_usdt,
            max_total_exposure_usdt=policy.max_total_exposure_usdt,
            existing_symbol_exposure_usdt=ZERO,
            existing_total_exposure_usdt=ZERO,
            effective_leverage=policy.effective_leverage,
            required_reserve_usdt=ZERO,
            effective_equity_usdt=policy.effective_equity_usdt,
        )
        result = cls._apply_aggregate_risk(session, policy, result)
        requirement = session.get(DurableRiskReductionRequirement, policy.plan_id)
        if result.pending_entries_blocked and result.position_quantity > ZERO:
            cls._upsert_risk_reduction_requirement(session, policy, result)
        elif (
            requirement is not None
            and RiskReductionStatus(requirement.status) is RiskReductionStatus.OPEN
        ):
            result = PositionRiskAssessment(
                position_quantity=result.position_quantity,
                average_entry_price=result.average_entry_price,
                actual_notional_usdt=result.actual_notional_usdt,
                actual_required_margin_usdt=result.actual_required_margin_usdt,
                actual_stop_risk=result.actual_stop_risk,
                pending_entries_blocked=True,
                hard_halted=result.hard_halted,
                reason="RISK_REDUCTION_REQUIRED",
            )
        state.position_quantity = format(result.position_quantity, "f")
        state.average_entry_price = (
            None if result.average_entry_price is None else format(result.average_entry_price, "f")
        )
        state.actual_notional_usdt = format(result.actual_notional_usdt, "f")
        state.actual_required_margin_usdt = format(result.actual_required_margin_usdt, "f")
        state.actual_stop_risk = format(result.actual_stop_risk, "f")
        state.pending_entries_blocked = result.pending_entries_blocked
        state.hard_halted = result.hard_halted
        state.reason = result.reason
        state.updated_at = datetime.now(UTC)
        return result

    @staticmethod
    def _fill_ledger_for_plan(session: Session, plan_id: str) -> FillLedger:
        rows = tuple(
            session.scalars(
                select(DurableIntentFill)
                .join(
                    DurableOrderIntent,
                    DurableOrderIntent.client_order_id == DurableIntentFill.client_order_id,
                )
                .where(
                    DurableOrderIntent.plan_id == plan_id,
                    DurableOrderIntent.account_id == V1_DEFAULT_ACCOUNT_ID,
                    DurableIntentFill.account_id == V1_DEFAULT_ACCOUNT_ID,
                    DurableOrderIntent.role == OrderRole.ENTRY.value,
                )
                .order_by(DurableIntentFill.id.asc())
            )
        )
        return FillLedger.from_events(DurableIntentLedger._fill_event_from_row(row) for row in rows)

    @staticmethod
    def _record_for_update(session: Session, client_order_id: str) -> DurableOrderIntent:
        statement = select(DurableOrderIntent).where(
            DurableOrderIntent.client_order_id == client_order_id,
            DurableOrderIntent.account_id == V1_DEFAULT_ACCOUNT_ID,
        )
        if session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
        record = session.scalar(statement)
        if record is None:
            raise IntentLifecycleError("durable intent was not found")
        return record

    @staticmethod
    def _latest_for_economic_key(session: Session, economic_key: str) -> DurableOrderIntent | None:
        statement = (
            select(DurableOrderIntent)
            .where(
                DurableOrderIntent.economic_key == economic_key,
                DurableOrderIntent.account_id == V1_DEFAULT_ACCOUNT_ID,
            )
            .order_by(DurableOrderIntent.attempt_number.desc())
            .limit(1)
        )
        if session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
        return session.scalar(statement)

    @staticmethod
    def _intent_fill_ledger(session: Session, client_order_id: str) -> FillLedger:
        rows = tuple(
            session.scalars(
                select(DurableIntentFill)
                .where(
                    DurableIntentFill.client_order_id == client_order_id,
                    DurableIntentFill.account_id == V1_DEFAULT_ACCOUNT_ID,
                )
                .order_by(DurableIntentFill.id.asc())
            )
        )
        return FillLedger.from_events(DurableIntentLedger._fill_event_from_row(row) for row in rows)

    @staticmethod
    def _derive_bounded_absence_evidence(
        session: Session,
        record: DurableOrderIntent,
    ) -> BoundedAbsenceEvidence:
        DurableIntentLedger._validate_lifecycle_timeline(record)
        if record.submitted_at_ms is None or record.unknown_at_ms is None:
            raise BoundedAbsenceEvidenceError(
                "durable submission and UNKNOWN timestamps are required"
            )
        rows = tuple(
            session.scalars(
                select(DurableIntentAbsenceObservation)
                .where(
                    DurableIntentAbsenceObservation.client_order_id == record.client_order_id,
                    DurableIntentAbsenceObservation.account_id == record.account_id,
                )
                .order_by(DurableIntentAbsenceObservation.observed_at_ms.asc())
            )
        )
        if not rows:
            raise BoundedAbsenceEvidenceError("no durable absence observations exist")
        if any(row.economic_key != record.economic_key for row in rows):
            raise BoundedAbsenceEvidenceError(
                "absence evidence does not match durable economic identity"
            )
        if any(
            row.query_client_order_id != record.client_order_id
            or row.query_economic_key != record.economic_key
            or row.attempt_number != record.attempt_number
            or row.client_order_namespace != ClientOrderNamespace.NORMAL.value
            for row in rows
        ):
            raise BoundedAbsenceEvidenceError(
                "absence query identity does not match durable intent evidence"
            )
        if any(
            not row.query_reference
            or row.query_started_at_ms < record.unknown_at_ms
            or row.observed_at_ms < record.unknown_at_ms
            or row.stream_watermark_ms < record.unknown_at_ms
            for row in rows
        ):
            raise BoundedAbsenceEvidenceError(
                "absence evidence is not causally after durable UNKNOWN submission"
            )
        if any(row.found for row in rows):
            raise BoundedAbsenceEvidenceError("presence evidence prevents an absence resolution")
        observations_by_source: defaultdict[
            AbsenceEvidenceSource, list[DurableIntentAbsenceObservation]
        ] = defaultdict(list)
        for row in rows:
            if row.query_reference is None:
                raise BoundedAbsenceEvidenceError(
                    "durable absence observation has no query provenance"
                )
            try:
                source = AbsenceEvidenceSource(row.source)
            except ValueError as error:
                raise BoundedAbsenceEvidenceError(
                    "absence observation source is invalid"
                ) from error
            if row.stream_watermark_ms < row.observed_at_ms:
                raise BoundedAbsenceEvidenceError("durable stream watermark is invalid")
            observation = UnknownIntentObservation(
                source=source,
                observed_at_ms=row.observed_at_ms,
                stream_watermark_ms=row.stream_watermark_ms,
                found=row.found,
                query_reference=row.query_reference,
                query_client_order_id=row.query_client_order_id,
                query_economic_key=row.query_economic_key,
                query_started_at_ms=row.query_started_at_ms,
                attempt_number=row.attempt_number,
                client_order_namespace=ClientOrderNamespace(row.client_order_namespace),
                provenance_fingerprint=row.provenance_fingerprint,
            )
            if observation.provenance_fingerprint != row.provenance_fingerprint:
                raise BoundedAbsenceEvidenceError("durable query provenance is invalid")
            observations_by_source[source].append(row)
        missing_sources = _REQUIRED_ABSENCE_SOURCES - set(observations_by_source)
        if missing_sources:
            raise BoundedAbsenceEvidenceError(
                "all bounded-absence observation sources are required"
            )
        for source in _REQUIRED_ABSENCE_SOURCES:
            source_rows = observations_by_source[source]
            timestamps = [row.observed_at_ms for row in source_rows]
            if len(timestamps) < BoundedAbsenceEvidence.minimum_observation_count:
                raise BoundedAbsenceEvidenceError("each absence source needs repeated observations")
            if (
                max(timestamps) - min(timestamps)
                < BoundedAbsenceEvidence.minimum_observation_window_ms
            ):
                raise BoundedAbsenceEvidenceError("absence source observation window is too short")
        timestamps = [row.observed_at_ms for row in rows]
        return BoundedAbsenceEvidence(
            client_order_id=record.client_order_id,
            economic_key=record.economic_key,
            first_not_found_at_ms=min(timestamps),
            last_not_found_at_ms=max(timestamps),
            not_found_observation_count=len(rows),
            attempt_number=record.attempt_number,
            client_order_namespace=ClientOrderNamespace.NORMAL,
        )
