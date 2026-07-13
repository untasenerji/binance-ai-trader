"""Pure state-transition validation shared by online projection and replay."""

from collections.abc import Mapping
from dataclasses import dataclass

from app.domain.state_machine import TransitionContext, TransitionRejected, transition
from app.domain.types import TradePlanState


class TransitionEventSchemaError(ValueError):
    """Raised before dedupe when a state-transition event lacks durable evidence."""


class TransitionSequenceError(ValueError):
    """Raised when a non-stale event cannot safely follow the local projection."""


@dataclass(frozen=True, slots=True)
class TransitionEvidence:
    risk_permits_entry: bool = False
    stop_confirmed: bool = False
    reduction_only: bool = False

    def to_context(self) -> TransitionContext:
        return TransitionContext(
            risk_permits_entry=self.risk_permits_entry,
            stop_confirmed=self.stop_confirmed,
            reduction_only=self.reduction_only,
        )


@dataclass(frozen=True, slots=True)
class StateTransitionEvent:
    plan_id: str
    from_state: TradePlanState
    to_state: TradePlanState
    plan_version: int
    source_sequence: int
    evidence: TransitionEvidence


@dataclass(frozen=True, slots=True)
class ProjectionState:
    plan_id: str
    state: TradePlanState
    plan_version: int
    source_sequence: int


@dataclass(frozen=True, slots=True)
class TransitionReduction:
    projection: ProjectionState
    mutated: bool
    stale: bool


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise TransitionEventSchemaError(f"state transition requires {field}")
    return value


def _required_positive_int(payload: Mapping[str, object], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TransitionEventSchemaError(f"state transition requires positive integer {field}")
    return value


def _parse_state(value: str, field: str) -> TradePlanState:
    try:
        return TradePlanState(value)
    except ValueError as error:
        raise TransitionEventSchemaError(f"state transition has invalid {field}") from error


def projection_state_from_value(value: str) -> TradePlanState:
    """Parse a durable projection state and fail closed if persistence is malformed."""
    try:
        return TradePlanState(value)
    except ValueError as error:
        raise TransitionSequenceError("projection has an invalid state") from error


def _parse_evidence(payload: Mapping[str, object]) -> TransitionEvidence:
    raw_evidence = payload.get("transition_evidence")
    if not isinstance(raw_evidence, Mapping):
        raise TransitionEventSchemaError("state transition requires transition_evidence mapping")
    fields = ("risk_permits_entry", "stop_confirmed", "reduction_only")
    values: dict[str, bool] = {}
    for field in fields:
        value = raw_evidence.get(field)
        if not isinstance(value, bool):
            raise TransitionEventSchemaError(f"transition_evidence requires boolean {field}")
        values[field] = value
    return TransitionEvidence(**values)


def parse_state_transition_payload(payload: Mapping[str, object]) -> StateTransitionEvent:
    """Parse durable state evidence before it can consume a dedupe claim."""
    return StateTransitionEvent(
        plan_id=_required_string(payload, "plan_id"),
        from_state=_parse_state(_required_string(payload, "from_state"), "from_state"),
        to_state=_parse_state(_required_string(payload, "to_state"), "to_state"),
        plan_version=_required_positive_int(payload, "plan_version"),
        source_sequence=_required_positive_int(payload, "source_sequence"),
        evidence=_parse_evidence(payload),
    )


def reduce_state_transition(
    current: ProjectionState | None,
    event: StateTransitionEvent,
) -> TransitionReduction:
    """Return a new projection or a state-neutral stale result without side effects."""
    if current is None:
        if event.from_state is not TradePlanState.DRAFT:
            raise TransitionSequenceError("first state transition must originate at DRAFT")
        if event.plan_version != 1:
            raise TransitionSequenceError("first state transition must have plan version 1")
        next_state = transition(
            TradePlanState.DRAFT,
            event.to_state,
            context=event.evidence.to_context(),
        )
        return TransitionReduction(
            projection=ProjectionState(
                plan_id=event.plan_id,
                state=next_state,
                plan_version=event.plan_version,
                source_sequence=event.source_sequence,
            ),
            mutated=True,
            stale=False,
        )

    if event.plan_id != current.plan_id:
        raise TransitionSequenceError("state transition plan ID does not match projection")
    if (
        event.plan_version <= current.plan_version
        and event.source_sequence <= current.source_sequence
    ):
        return TransitionReduction(projection=current, mutated=False, stale=True)
    if (
        event.plan_version <= current.plan_version
        or event.source_sequence <= current.source_sequence
    ):
        raise TransitionSequenceError("state transition has inconsistent version ordering")
    if event.plan_version != current.plan_version + 1:
        raise TransitionSequenceError("state transition skipped a plan version")
    if event.from_state is not current.state:
        raise TransitionSequenceError(
            "state transition does not match the current projection state"
        )
    try:
        next_state = transition(current.state, event.to_state, context=event.evidence.to_context())
    except TransitionRejected as error:
        raise TransitionSequenceError("state transition is not allowed") from error
    return TransitionReduction(
        projection=ProjectionState(
            plan_id=event.plan_id,
            state=next_state,
            plan_version=event.plan_version,
            source_sequence=event.source_sequence,
        ),
        mutated=True,
        stale=False,
    )
