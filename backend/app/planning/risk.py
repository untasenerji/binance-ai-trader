"""All-fill risk projection and quantity solver using Decimal-only arithmetic."""

from dataclasses import dataclass
from decimal import Decimal

from app.domain.decimal_math import ZERO
from app.domain.filters import FilterViolation, SymbolFilters
from app.domain.types import Direction
from app.planning.ladder import StageBlueprint

_BPS_DENOMINATOR = Decimal("10000")


class RiskPlanningError(ValueError):
    """Raised when a supplied stop, budget, or plan shape is intrinsically invalid."""


@dataclass(frozen=True, slots=True)
class CostAssumptions:
    entry_fee_rate: Decimal
    exit_fee_rate: Decimal
    slippage_bps: Decimal
    funding_buffer_rate: Decimal

    def __post_init__(self) -> None:
        if (
            min(
                self.entry_fee_rate,
                self.exit_fee_rate,
                self.slippage_bps,
                self.funding_buffer_rate,
            )
            < ZERO
        ):
            raise RiskPlanningError("cost assumptions must not be negative")


@dataclass(frozen=True, slots=True)
class PlannedStage:
    index: int
    entry_price: Decimal
    quantity: Decimal
    risk_per_unit: Decimal
    risk_contribution: Decimal
    scheduled_at_ms: int | None


@dataclass(frozen=True, slots=True)
class LadderPlan:
    direction: Direction
    stop_price: Decimal
    risk_budget: Decimal
    projected_total_loss: Decimal
    stages: tuple[PlannedStage, ...]
    skip_reason: str | None = None

    @property
    def is_skipped(self) -> bool:
        return self.skip_reason is not None


def risk_per_unit(
    *,
    direction: Direction,
    entry_price: Decimal,
    stop_price: Decimal,
    costs: CostAssumptions,
) -> Decimal:
    if entry_price <= ZERO or stop_price <= ZERO:
        raise RiskPlanningError("entry and stop prices must be positive")
    if direction is Direction.LONG and stop_price >= entry_price:
        raise RiskPlanningError("long stop must be below entry")
    if direction is Direction.SHORT and stop_price <= entry_price:
        raise RiskPlanningError("short stop must be above entry")

    price_loss = (
        entry_price - stop_price if direction is Direction.LONG else stop_price - entry_price
    )
    fee_loss = entry_price * costs.entry_fee_rate + stop_price * costs.exit_fee_rate
    slippage_loss = (entry_price + stop_price) * costs.slippage_bps / _BPS_DENOMINATOR
    funding_loss = entry_price * costs.funding_buffer_rate
    return price_loss + fee_loss + slippage_loss + funding_loss


def solve_ladder(
    *,
    direction: Direction,
    blueprints: tuple[StageBlueprint, ...],
    stop_price: Decimal,
    risk_budget: Decimal,
    filters: SymbolFilters,
    costs: CostAssumptions,
    max_stages: int,
) -> LadderPlan:
    if risk_budget <= ZERO or max_stages < 1:
        raise RiskPlanningError("risk_budget and max_stages must be positive")
    if not blueprints:
        return _skip_plan(direction, stop_price, risk_budget, "NO_STAGES")
    if len(blueprints) > max_stages:
        return _skip_plan(direction, stop_price, risk_budget, "HARD_STAGE_LIMIT_EXCEEDED")

    stages: list[PlannedStage] = []
    for blueprint in blueprints:
        entry_price = filters.round_price_down(blueprint.entry_price)
        try:
            per_unit = risk_per_unit(
                direction=direction,
                entry_price=entry_price,
                stop_price=stop_price,
                costs=costs,
            )
        except RiskPlanningError:
            return _skip_plan(direction, stop_price, risk_budget, "INVALID_STOP_RELATION")
        raw_quantity = risk_budget * blueprint.weight / per_unit
        quantity = filters.round_quantity_down(raw_quantity)
        try:
            filters.validate_entry(price=entry_price, quantity=quantity)
        except FilterViolation:
            return _skip_plan(direction, stop_price, risk_budget, "MIN_NOTIONAL_OR_FILTER_FAILURE")
        contribution = quantity * per_unit
        stages.append(
            PlannedStage(
                index=blueprint.index,
                entry_price=entry_price,
                quantity=quantity,
                risk_per_unit=per_unit,
                risk_contribution=contribution,
                scheduled_at_ms=blueprint.scheduled_at_ms,
            )
        )

    projected_total_loss = sum((stage.risk_contribution for stage in stages), ZERO)
    if projected_total_loss > risk_budget:
        return _skip_plan(direction, stop_price, risk_budget, "ROUNDING_RISK_EXCEEDED")
    return LadderPlan(
        direction=direction,
        stop_price=stop_price,
        risk_budget=risk_budget,
        projected_total_loss=projected_total_loss,
        stages=tuple(stages),
    )


def _skip_plan(
    direction: Direction,
    stop_price: Decimal,
    risk_budget: Decimal,
    reason: str,
) -> LadderPlan:
    return LadderPlan(
        direction=direction,
        stop_price=stop_price,
        risk_budget=risk_budget,
        projected_total_loss=ZERO,
        stages=(),
        skip_reason=reason,
    )
