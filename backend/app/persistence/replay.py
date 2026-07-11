"""Replay and exchange-truth comparison skeleton without an exchange client."""

from dataclasses import dataclass

from app.domain.types import TradePlanState
from app.persistence.models import AuditEvent


@dataclass(frozen=True, slots=True)
class ReplayResult:
    plan_states: dict[str, TradePlanState]
    duplicate_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    is_clean: bool
    reason: str | None


class ReplayRunner:
    """Reduces only the first delivery of each event ID into a local projection."""

    def replay(self, events: tuple[AuditEvent, ...]) -> ReplayResult:
        seen_event_ids: set[str] = set()
        duplicate_event_ids: list[str] = []
        plan_states: dict[str, TradePlanState] = {}

        for event in events:
            if event.event_id in seen_event_ids:
                duplicate_event_ids.append(event.event_id)
                continue
            seen_event_ids.add(event.event_id)

            plan_id = event.payload.get("plan_id")
            target_state = event.payload.get("to_state")
            if not isinstance(plan_id, str) or not isinstance(target_state, str):
                continue
            try:
                plan_states[plan_id] = TradePlanState(target_state)
            except ValueError:
                continue

        return ReplayResult(plan_states=plan_states, duplicate_event_ids=tuple(duplicate_event_ids))


def reconcile_projection(
    *,
    local_state: TradePlanState | None,
    exchange_state: TradePlanState | None,
) -> ReconciliationResult:
    """Exchange truth wins; any mismatch remains a halt condition until resolved."""
    if local_state == exchange_state:
        return ReconciliationResult(is_clean=True, reason=None)
    return ReconciliationResult(is_clean=False, reason="EXCHANGE_LOCAL_STATE_MISMATCH")
