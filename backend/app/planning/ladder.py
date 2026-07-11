"""Price-only ladder variants. They produce plans, not exchange instructions."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.domain.decimal_math import ZERO
from app.domain.types import Direction


class LadderVariant(StrEnum):
    EQUAL_PERCENT = "EQUAL_PERCENT"
    ATR = "ATR"
    TECHNICAL_LEVEL = "TECHNICAL_LEVEL"
    ORDER_BOOK_LIQUIDITY = "ORDER_BOOK_LIQUIDITY"
    TIME_SLICED = "TIME_SLICED"


class LadderPlanningError(ValueError):
    """Raised for an invalid, unsafe, or deliberately disabled ladder variant."""


@dataclass(frozen=True, slots=True)
class StageBlueprint:
    index: int
    entry_price: Decimal
    weight: Decimal
    scheduled_at_ms: int | None = None

    def __post_init__(self) -> None:
        if self.index < 1 or self.entry_price <= ZERO or self.weight <= ZERO:
            raise LadderPlanningError("stage blueprint values must be positive")
        if self.scheduled_at_ms is not None and self.scheduled_at_ms < 0:
            raise LadderPlanningError("scheduled_at_ms must not be negative")


def equal_percent_ladder(
    *,
    direction: Direction,
    reference_price: Decimal,
    stage_count: int,
    spacing_percent: Decimal,
    weights: Sequence[Decimal],
) -> tuple[StageBlueprint, ...]:
    if reference_price <= ZERO or spacing_percent < ZERO:
        raise LadderPlanningError("reference price and spacing are invalid")
    _validate_weights(stage_count, weights)
    levels: list[StageBlueprint] = []
    for index, weight in enumerate(weights, start=1):
        distance = spacing_percent * Decimal(index - 1)
        price = (
            reference_price * (Decimal("1") - distance)
            if direction is Direction.LONG
            else reference_price * (Decimal("1") + distance)
        )
        levels.append(StageBlueprint(index=index, entry_price=price, weight=weight))
    return tuple(levels)


def atr_ladder(
    *,
    direction: Direction,
    reference_price: Decimal,
    atr: Decimal,
    stage_count: int,
    atr_multiple: Decimal,
    weights: Sequence[Decimal],
) -> tuple[StageBlueprint, ...]:
    if reference_price <= ZERO or atr <= ZERO or atr_multiple < ZERO:
        raise LadderPlanningError("ATR ladder inputs are invalid")
    _validate_weights(stage_count, weights)
    levels: list[StageBlueprint] = []
    for index, weight in enumerate(weights, start=1):
        distance = atr * atr_multiple * Decimal(index - 1)
        price = (
            reference_price - distance
            if direction is Direction.LONG
            else reference_price + distance
        )
        levels.append(StageBlueprint(index=index, entry_price=price, weight=weight))
    return tuple(levels)


def technical_level_ladder(
    *,
    direction: Direction,
    levels: Sequence[Decimal],
    stop_price: Decimal,
    weights: Sequence[Decimal],
) -> tuple[StageBlueprint, ...]:
    _validate_weights(len(levels), weights)
    if stop_price <= ZERO:
        raise LadderPlanningError("stop_price must be positive")
    if any(level <= ZERO for level in levels):
        raise LadderPlanningError("technical levels must be positive")
    if direction is Direction.LONG and any(level <= stop_price for level in levels):
        raise LadderPlanningError("long technical levels must remain above the stop")
    if direction is Direction.SHORT and any(level >= stop_price for level in levels):
        raise LadderPlanningError("short technical levels must remain below the stop")
    return tuple(
        StageBlueprint(index=index, entry_price=level, weight=weight)
        for index, (level, weight) in enumerate(zip(levels, weights, strict=True), start=1)
    )


def order_book_liquidity_ladder(
    *,
    direction: Direction,
    levels: Sequence[tuple[Decimal, Decimal]],
    stop_price: Decimal,
    weights: Sequence[Decimal],
    enabled: bool = False,
) -> tuple[StageBlueprint, ...]:
    if not enabled:
        raise LadderPlanningError(
            "order-book liquidity ladder is disabled without shadow-data approval"
        )
    _validate_weights(len(weights), weights)
    eligible_levels = [
        level
        for level in levels
        if level[0] > ZERO
        and level[1] > ZERO
        and (
            (direction is Direction.LONG and level[0] > stop_price)
            or (direction is Direction.SHORT and level[0] < stop_price)
        )
    ]
    if len(eligible_levels) < len(weights):
        raise LadderPlanningError("insufficient safe order-book liquidity levels")
    ordered = sorted(eligible_levels, key=lambda level: level[1], reverse=True)[: len(weights)]
    return tuple(
        StageBlueprint(index=index, entry_price=level[0], weight=weight)
        for index, (level, weight) in enumerate(zip(ordered, weights, strict=True), start=1)
    )


def time_sliced_ladder(
    *,
    reference_price: Decimal,
    stage_count: int,
    start_at_ms: int,
    interval_ms: int,
    weights: Sequence[Decimal],
) -> tuple[StageBlueprint, ...]:
    if reference_price <= ZERO or start_at_ms < 0 or interval_ms <= 0:
        raise LadderPlanningError("time-sliced ladder inputs are invalid")
    _validate_weights(stage_count, weights)
    return tuple(
        StageBlueprint(
            index=index,
            entry_price=reference_price,
            weight=weight,
            scheduled_at_ms=start_at_ms + interval_ms * (index - 1),
        )
        for index, weight in enumerate(weights, start=1)
    )


def _validate_weights(stage_count: int, weights: Sequence[Decimal]) -> None:
    if stage_count < 1 or len(weights) != stage_count:
        raise LadderPlanningError("stage_count and weights must match")
    if any(weight <= ZERO for weight in weights) or sum(weights, ZERO) != Decimal("1"):
        raise LadderPlanningError("stage weights must be positive and sum to exactly one")
