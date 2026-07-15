"""Durable local intent/outbox evidence for simulator-only submission scenarios."""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.decimal_math import ZERO
from app.domain.types import Direction
from app.persistence.circuit_breaker import EntryIntentAuthorizationGate, PersistenceCircuitBreaker
from app.persistence.models import (
    DurableActualRiskPolicy,
    DurableActualRiskPolicyVersion,
    DurableActualRiskState,
    DurableIntentAbsenceObservation,
    DurableIntentFill,
    DurableOrderIntent,
    DurableRiskReductionRequirement,
    DurableSimulatedProtection,
)
from app.planning.fills import (
    ActualRiskPolicy,
    FillEvent,
    FillLedger,
    FillLedgerError,
    FillLedgerReceipt,
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
    positions_by_symbol: dict[str, Decimal]
    normal_order_client_ids: frozenset[str]
    algo_order_client_ids: frozenset[str]
    required_stop_symbols: frozenset[str]
    unresolved_unknown_intent_ids: frozenset[str]


class DurableIntentLedger:
    """Durably gates simulator submissions and retains all uncertainty/fill evidence."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        persistence_breaker: PersistenceCircuitBreaker | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._persistence_breaker = (
            persistence_breaker if persistence_breaker is not None else PersistenceCircuitBreaker()
        )
        self._entry_gate = EntryIntentAuthorizationGate(self._persistence_breaker)

    @property
    def persistence_breaker(self) -> PersistenceCircuitBreaker:
        return self._persistence_breaker

    def reopen_after_restart(self) -> "DurableIntentLedger":
        """Return a fresh local ledger instance over the same durable store."""
        return DurableIntentLedger(
            self._session_factory, persistence_breaker=self._persistence_breaker
        )

    def register_actual_risk_policy(self, policy: ActualRiskPolicy) -> ActualRiskPolicy:
        """Persist an immutable entry policy before it can authorize simulator fills."""
        if not isinstance(policy, ActualRiskPolicy):
            raise TypeError("actual-risk policy must be typed")
        fingerprint = self._policy_fingerprint(policy)
        try:
            with self._session_factory.begin() as session:
                existing = session.get(DurableActualRiskPolicy, policy.plan_id)
                if existing is not None:
                    if existing.policy_fingerprint != fingerprint:
                        raise DurableRiskPolicyError(
                            "plan ID already has a different durable actual-risk policy"
                        )
                    return self._policy_from_row(existing)
                session.add(
                    DurableActualRiskPolicy(
                        plan_id=policy.plan_id,
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
                        policy_fingerprint=fingerprint,
                    )
                )
                session.add(
                    DurableSimulatedProtection(
                        plan_id=policy.plan_id,
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
            version = session.scalar(
                select(DurableActualRiskPolicyVersion)
                .where(DurableActualRiskPolicyVersion.plan_id == plan_id)
                .order_by(DurableActualRiskPolicyVersion.version.desc())
                .limit(1)
            )
            row = version if version is not None else session.get(DurableActualRiskPolicy, plan_id)
            return None if row is None else self._policy_from_row(row)

    def version_actual_risk_policy(self, policy: ActualRiskPolicy) -> int:
        """Append a validated policy revision without mutating earlier evidence."""
        if not isinstance(policy, ActualRiskPolicy):
            raise TypeError("actual-risk policy must be typed")
        fingerprint = self._policy_fingerprint(policy)
        try:
            with self._session_factory.begin() as session:
                base = session.get(DurableActualRiskPolicy, policy.plan_id)
                if base is None:
                    raise DurableRiskPolicyError("base actual-risk policy must exist first")
                base_policy = self._policy_from_row(base)
                if (
                    policy.symbol != base_policy.symbol
                    or policy.direction is not base_policy.direction
                    or policy.protective_stop_reference != base_policy.protective_stop_reference
                    or policy.reduce_only_exit_reference != base_policy.reduce_only_exit_reference
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
        with self._session_factory() as session:
            policy_row = session.get(DurableActualRiskPolicy, intent.plan_id)
            if policy_row is None:
                raise DurableRiskPolicyError("RISK_POLICY_MISSING")
            policy = self._latest_policy_from_session(session, intent.plan_id)
            if policy.symbol != intent.symbol or policy.direction is not intent.direction:
                raise DurableRiskPolicyError("RISK_POLICY_PLAN_MISMATCH")
            open_requirement = session.scalar(
                select(DurableRiskReductionRequirement)
                .where(DurableRiskReductionRequirement.status == RiskReductionStatus.OPEN.value)
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

    def prepare(self, intent: SimulatedOrderIntent) -> DurableIntentRecord:
        if intent.role is OrderRole.ENTRY:
            self._entry_gate.authorize_new_entry_intent()
        try:
            with self._session_factory.begin() as session:
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
                record = DurableOrderIntent(
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
        except IntegrityError as error:
            raise UnresolvedEconomicAction(
                "concurrent economic action already created a durable intent"
            ) from error
        except Exception as error:
            self._persistence_breaker.record_write_failure(error)
            raise

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

    def record_fill(self, event: FillEvent) -> FillLedgerReceipt:
        """Persist one immutable trade fact before it can influence an outcome or risk gate."""
        try:
            with self._session_factory.begin() as session:
                intent = self._record_for_update(session, event.client_order_id)
                status = DurableIntentStatus(intent.status)
                existing_rows = tuple(
                    session.scalars(
                        select(DurableIntentFill).where(
                            DurableIntentFill.client_order_id == event.client_order_id
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
                if status is DurableIntentStatus.FILLED:
                    raise IntentLifecycleError("terminal durable intent cannot receive a new fill")
                receipt = fill_ledger.record(event)
                planned_quantity = Decimal(intent.quantity)
                if receipt.filled_quantity > planned_quantity:
                    raise IntentLifecycleError("durable fills exceed the planned quantity")
                session.add(
                    DurableIntentFill(
                        client_order_id=event.client_order_id,
                        trade_id=event.trade_id,
                        semantic_fingerprint=fingerprint,
                        last_quantity=format(event.last_quantity, "f"),
                        cumulative_quantity=format(event.cumulative_quantity, "f"),
                        fill_price=format(event.fill_price, "f"),
                        fee=format(event.fee, "f"),
                        fee_asset=event.fee_asset,
                        occurred_at=event.occurred_at.astimezone(UTC),
                    )
                )
                if status is not DurableIntentStatus.CANCELLED:
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
                intent.filled_quantity = format(receipt.filled_quantity, "f")
                intent.updated_at = datetime.now(UTC)
                session.flush()
                if intent.role == OrderRole.ENTRY.value:
                    self._cancel_unfilled_superseding_attempts(session, intent)
                    policy = session.get(DurableActualRiskPolicy, intent.plan_id)
                    if policy is not None:
                        self._recalculate_actual_risk(session, policy)
                        session.flush()
                return receipt
        except (FillLedgerError, IntentLifecycleError):
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
                    DurableOrderIntent.client_order_id == client_order_id
                )
            )
            if record is None:
                raise KeyError(f"no durable intent for {client_order_id}")
            return self._to_record(record)

    def list_intents(self) -> tuple[DurableIntentRecord, ...]:
        with self._session_factory() as session:
            records = session.scalars(
                select(DurableOrderIntent).order_by(DurableOrderIntent.id.asc())
            )
            return tuple(self._to_record(record) for record in records)

    def list_absence_observations(
        self,
        client_order_id: str,
    ) -> tuple[DurableAbsenceObservation, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(DurableIntentAbsenceObservation)
                .where(DurableIntentAbsenceObservation.client_order_id == client_order_id)
                .order_by(
                    DurableIntentAbsenceObservation.observed_at_ms.asc(),
                    DurableIntentAbsenceObservation.source.asc(),
                )
            )
            return tuple(self._to_absence_observation(row) for row in rows)

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
            )
            if any(record.status is status for record in records)
        }

    def reconciliation_facts(self) -> DurableReconciliationFacts:
        """Derive local position and protection expectations from durable records only."""
        with self._session_factory() as session:
            positions_by_symbol: dict[str, Decimal] = {}
            required_stop_symbols: set[str] = set()
            algo_order_client_ids: set[str] = set()
            for state in session.scalars(select(DurableActualRiskState)):
                policy = session.get(DurableActualRiskPolicy, state.plan_id)
                protection = session.get(DurableSimulatedProtection, state.plan_id)
                if policy is None or protection is None:
                    raise DurableRiskPolicyError(
                        "durable reconciliation facts require complete risk and protection records"
                    )
                quantity = Decimal(state.position_quantity)
                if quantity <= ZERO:
                    continue
                signed_quantity = (
                    quantity if Direction(policy.direction) is Direction.LONG else -quantity
                )
                positions_by_symbol[policy.symbol] = (
                    positions_by_symbol.get(policy.symbol, ZERO) + signed_quantity
                )
                required_stop_symbols.add(policy.symbol)
                # Rehearsal intent references are never exchange algo-order evidence.

            active_normal_statuses = {
                DurableIntentStatus.NEW.value,
                DurableIntentStatus.PARTIALLY_FILLED.value,
            }
            normal_order_client_ids = frozenset(
                row.client_order_id
                for row in session.scalars(select(DurableOrderIntent))
                if row.status in active_normal_statuses and row.role != OrderRole.STOP.value
            )
            unresolved_unknown_intent_ids = frozenset(
                row.client_order_id
                for row in session.scalars(select(DurableOrderIntent))
                if row.status in {status.value for status in _UNRESOLVED_STATUSES}
            )
            return DurableReconciliationFacts(
                positions_by_symbol=positions_by_symbol,
                normal_order_client_ids=normal_order_client_ids,
                algo_order_client_ids=frozenset(algo_order_client_ids),
                required_stop_symbols=frozenset(required_stop_symbols),
                unresolved_unknown_intent_ids=unresolved_unknown_intent_ids,
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
                record.status = target_status.value
                if timestamp_field is not None:
                    setattr(record, timestamp_field, timestamp_value)
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
        raise IntentLifecycleError("known outcome violates the durable intent lifecycle")

    @staticmethod
    def _to_record(record: DurableOrderIntent) -> DurableIntentRecord:
        return DurableIntentRecord(
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
        return DurableAbsenceObservation(
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

    @staticmethod
    def _fill_event_from_row(row: DurableIntentFill) -> FillEvent:
        occurred_at = row.occurred_at
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)
        else:
            occurred_at = occurred_at.astimezone(UTC)
        return FillEvent(
            trade_id=row.trade_id,
            client_order_id=row.client_order_id,
            last_quantity=Decimal(row.last_quantity),
            cumulative_quantity=Decimal(row.cumulative_quantity),
            fill_price=Decimal(row.fill_price),
            fee=Decimal(row.fee),
            fee_asset=row.fee_asset,
            occurred_at=occurred_at,
        )

    @staticmethod
    def _fill_fingerprint(event: FillEvent) -> str:
        canonical = json.dumps(
            {
                "client_order_id": event.client_order_id,
                "cumulative_quantity": format(event.cumulative_quantity, "f"),
                "fee": format(event.fee, "f"),
                "fee_asset": event.fee_asset,
                "fill_price": format(event.fill_price, "f"),
                "last_quantity": format(event.last_quantity, "f"),
                "occurred_at": event.occurred_at.astimezone(UTC).isoformat(),
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
                "direction": policy.direction.value,
                "effective_equity_usdt": format(policy.effective_equity_usdt, "f"),
                "effective_leverage": policy.effective_leverage,
                "existing_symbol_exposure_usdt": format(policy.existing_symbol_exposure_usdt, "f"),
                "existing_total_exposure_usdt": format(policy.existing_total_exposure_usdt, "f"),
                "exit_fee_rate": format(policy.exit_fee_rate, "f"),
                "funding_buffer_rate": format(policy.funding_buffer_rate, "f"),
                "funding_interval_count": policy.funding_interval_count,
                "max_symbol_exposure_usdt": format(policy.max_symbol_exposure_usdt, "f"),
                "max_total_exposure_usdt": format(policy.max_total_exposure_usdt, "f"),
                "plan_id": policy.plan_id,
                "protective_stop_reference": policy.protective_stop_reference,
                "reduce_only_exit_reference": policy.reduce_only_exit_reference,
                "required_reserve_usdt": format(policy.required_reserve_usdt, "f"),
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
        row: DurableActualRiskPolicy | DurableActualRiskPolicyVersion,
    ) -> ActualRiskPolicy:
        policy = ActualRiskPolicy(
            plan_id=row.plan_id,
            symbol=row.symbol,
            direction=Direction(row.direction),
            worst_stop_exit_price=Decimal(row.worst_stop_exit_price),
            exit_fee_rate=Decimal(row.exit_fee_rate),
            funding_buffer_rate=Decimal(row.funding_buffer_rate),
            funding_interval_count=row.funding_interval_count,
            risk_budget=Decimal(row.risk_budget),
            max_symbol_exposure_usdt=Decimal(row.max_symbol_exposure_usdt),
            max_total_exposure_usdt=Decimal(row.max_total_exposure_usdt),
            existing_symbol_exposure_usdt=Decimal(row.existing_symbol_exposure_usdt),
            existing_total_exposure_usdt=Decimal(row.existing_total_exposure_usdt),
            effective_leverage=row.effective_leverage,
            required_reserve_usdt=Decimal(row.required_reserve_usdt),
            effective_equity_usdt=Decimal(row.effective_equity_usdt),
            protective_stop_reference=row.protective_stop_reference,
            reduce_only_exit_reference=row.reduce_only_exit_reference,
        )
        if row.policy_fingerprint != cls._policy_fingerprint(policy):
            raise DurableRiskPolicyError("durable actual-risk policy fingerprint is invalid")
        return policy

    @staticmethod
    def _to_simulated_protection_evidence(
        row: DurableSimulatedProtection,
    ) -> SimulatedProtectionEvidence:
        return SimulatedProtectionEvidence(
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
        version = session.scalar(
            select(DurableActualRiskPolicyVersion)
            .where(DurableActualRiskPolicyVersion.plan_id == plan_id)
            .order_by(DurableActualRiskPolicyVersion.version.desc())
            .limit(1)
        )
        row = version if version is not None else session.get(DurableActualRiskPolicy, plan_id)
        if row is None:
            raise DurableRiskPolicyError("RISK_POLICY_MISSING")
        return cls._policy_from_row(row)

    @staticmethod
    def _policy_row_values(
        policy: ActualRiskPolicy,
        *,
        fingerprint: str,
    ) -> dict[str, object]:
        return {
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
            "policy_fingerprint": fingerprint,
        }

    @classmethod
    def _projected_portfolio_risk(
        cls,
        session: Session,
        policy: ActualRiskPolicy,
        *,
        proposed_intent: SimulatedOrderIntent | None,
        current_plan_result: PositionRiskAssessment | None = None,
    ) -> PortfolioRiskAssessment:
        position_exposure = ZERO
        symbol_position_exposure = ZERO
        projected_plan_stop_risk = ZERO
        for state in session.scalars(select(DurableActualRiskState)):
            state_policy = cls._latest_policy_from_session(session, state.plan_id)
            notional = Decimal(state.actual_notional_usdt)
            stop_risk = Decimal(state.actual_stop_risk)
            if current_plan_result is not None and state.plan_id == policy.plan_id:
                notional = current_plan_result.actual_notional_usdt
                stop_risk = current_plan_result.actual_stop_risk
            position_exposure += notional
            if state_policy.symbol == policy.symbol:
                symbol_position_exposure += notional
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
        symbol_pending_exposure = ZERO
        for row in session.scalars(select(DurableOrderIntent)):
            if row.role != OrderRole.ENTRY.value or row.status not in active_statuses:
                continue
            remaining_quantity = max(Decimal(row.quantity) - Decimal(row.filled_quantity), ZERO)
            pending_notional = remaining_quantity * Decimal(row.price)
            pending_exposure += pending_notional
            if row.symbol == policy.symbol:
                symbol_pending_exposure += pending_notional
            if row.plan_id == policy.plan_id:
                projected_plan_stop_risk += cls._projected_pending_stop_risk(
                    policy,
                    quantity=remaining_quantity,
                    entry_price=Decimal(row.price),
                )

        if proposed_intent is not None:
            proposed_notional = proposed_intent.quantity * proposed_intent.price
            pending_exposure += proposed_notional
            symbol_pending_exposure += proposed_notional
            projected_plan_stop_risk += cls._projected_pending_stop_risk(
                policy,
                quantity=proposed_intent.quantity,
                entry_price=proposed_intent.price,
            )

        aggregate_symbol = (
            policy.existing_symbol_exposure_usdt
            + symbol_position_exposure
            + symbol_pending_exposure
        )
        aggregate_total = policy.existing_total_exposure_usdt + position_exposure + pending_exposure
        required_margin = (
            aggregate_total / Decimal(policy.effective_leverage) + policy.required_reserve_usdt
        )
        reason: str | None = None
        if aggregate_symbol > policy.max_symbol_exposure_usdt:
            reason = "PROJECTED_SYMBOL_EXPOSURE_LIMIT_BREACH"
        elif aggregate_total > policy.max_total_exposure_usdt:
            reason = "PROJECTED_TOTAL_EXPOSURE_LIMIT_BREACH"
        elif required_margin > policy.effective_equity_usdt:
            reason = "PROJECTED_MARGIN_REQUIREMENT_BREACH"
        elif projected_plan_stop_risk > policy.risk_budget:
            reason = "PROJECTED_STOP_RISK_BREACH"
        return PortfolioRiskAssessment(
            pending_order_exposure_usdt=pending_exposure,
            position_exposure_usdt=position_exposure,
            aggregate_symbol_exposure_usdt=aggregate_symbol,
            aggregate_total_exposure_usdt=aggregate_total,
            required_margin_usdt=required_margin,
            reserve_usdt=policy.required_reserve_usdt,
            projected_plan_stop_risk=projected_plan_stop_risk,
            blocked=reason is not None,
            reason=reason,
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
        if result.pending_entries_blocked or not portfolio.blocked:
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
            "SIMULATED_PROTECTION_QUANTITY_MISMATCH",
        }
        if result.reason not in reduction_reasons:
            return
        requirement = session.get(DurableRiskReductionRequirement, policy.plan_id)
        if requirement is None:
            session.add(
                DurableRiskReductionRequirement(
                    plan_id=policy.plan_id,
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
                DurableOrderIntent.economic_key == authoritative_intent.economic_key,
                DurableOrderIntent.attempt_number > authoritative_intent.attempt_number,
            )
        )
        for later in later_attempts:
            if Decimal(later.filled_quantity) != ZERO:
                continue
            if DurableIntentStatus(later.status) is DurableIntentStatus.FILLED:
                continue
            later.status = DurableIntentStatus.CANCELLED.value
            later.updated_at = datetime.now(UTC)

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
            existing_symbol_exposure_usdt=policy.existing_symbol_exposure_usdt,
            existing_total_exposure_usdt=policy.existing_total_exposure_usdt,
            effective_leverage=policy.effective_leverage,
            required_reserve_usdt=policy.required_reserve_usdt,
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
                    DurableOrderIntent.role == OrderRole.ENTRY.value,
                )
                .order_by(DurableIntentFill.id.asc())
            )
        )
        return FillLedger.from_events(DurableIntentLedger._fill_event_from_row(row) for row in rows)

    @staticmethod
    def _record_for_update(session: Session, client_order_id: str) -> DurableOrderIntent:
        statement = select(DurableOrderIntent).where(
            DurableOrderIntent.client_order_id == client_order_id
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
            .where(DurableOrderIntent.economic_key == economic_key)
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
                .where(DurableIntentFill.client_order_id == client_order_id)
                .order_by(DurableIntentFill.id.asc())
            )
        )
        return FillLedger.from_events(DurableIntentLedger._fill_event_from_row(row) for row in rows)

    @staticmethod
    def _derive_bounded_absence_evidence(
        session: Session,
        record: DurableOrderIntent,
    ) -> BoundedAbsenceEvidence:
        if record.submitted_at_ms is None or record.unknown_at_ms is None:
            raise BoundedAbsenceEvidenceError(
                "durable submission and UNKNOWN timestamps are required"
            )
        rows = tuple(
            session.scalars(
                select(DurableIntentAbsenceObservation)
                .where(DurableIntentAbsenceObservation.client_order_id == record.client_order_id)
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
