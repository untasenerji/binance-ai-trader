"""Fail-closed replay over the same reducer used for online projections."""

from dataclasses import dataclass

from app.domain.types import TradePlanState
from app.persistence.audit import AuditChainHeadSnapshot, AuditDeliveryStatus, verify_hash_chain
from app.persistence.models import AuditEvent
from app.persistence.reducer import (
    ProjectionState,
    TransitionEventSchemaError,
    TransitionSequenceError,
    parse_state_transition_payload,
    reduce_state_transition,
)


@dataclass(frozen=True, slots=True)
class ReplayResult:
    is_valid: bool
    plan_states: dict[str, TradePlanState]
    duplicate_event_ids: tuple[str, ...]
    reason: str | None


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    is_clean: bool
    reason: str | None


class ReplayRunner:
    """Reconstructs local projections only after chain and head evidence validate."""

    def replay(
        self,
        events: tuple[AuditEvent, ...],
        chain_head: AuditChainHeadSnapshot,
    ) -> ReplayResult:
        if not verify_hash_chain(
            events,
            expected_count=chain_head.event_count,
            expected_last_record_hash=chain_head.last_record_hash,
        ):
            return self._invalid("AUDIT_CHAIN_OR_HEAD_INVALID")

        projections: dict[str, ProjectionState] = {}
        duplicate_event_ids: list[str] = []
        for event in events:
            try:
                status = AuditDeliveryStatus(event.delivery_status)
            except ValueError:
                return self._invalid("AUDIT_DELIVERY_STATUS_INVALID")
            if status is AuditDeliveryStatus.EXACT_DUPLICATE:
                duplicate_event_ids.append(event.event_id)
                continue
            if status is AuditDeliveryStatus.SEMANTIC_CONFLICT:
                return self._invalid("AUDIT_SEMANTIC_CONFLICT")
            if event.event_type != "state_transition":
                continue

            try:
                transition_event = parse_state_transition_payload(event.payload)
                reduction = reduce_state_transition(
                    projections.get(transition_event.plan_id), transition_event
                )
            except (TransitionEventSchemaError, TransitionSequenceError):
                return self._invalid("STATE_TRANSITION_INVALID")
            if reduction.mutated:
                projections[transition_event.plan_id] = reduction.projection

        return ReplayResult(
            is_valid=True,
            plan_states={plan_id: projection.state for plan_id, projection in projections.items()},
            duplicate_event_ids=tuple(duplicate_event_ids),
            reason=None,
        )

    @staticmethod
    def _invalid(reason: str) -> ReplayResult:
        return ReplayResult(is_valid=False, plan_states={}, duplicate_event_ids=(), reason=reason)


def reconcile_projection(
    *,
    local_state: TradePlanState | None,
    exchange_state: TradePlanState | None,
) -> ReconciliationResult:
    """Exchange truth wins; any mismatch remains a halt condition until resolved."""
    if local_state == exchange_state:
        return ReconciliationResult(is_clean=True, reason=None)
    return ReconciliationResult(is_clean=False, reason="EXCHANGE_LOCAL_STATE_MISMATCH")
