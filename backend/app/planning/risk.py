"""Fail-closed all-fill planning with a backend-authoritative risk envelope."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.domain.decimal_math import ZERO
from app.domain.filters import FilterViolation, SymbolFilters
from app.domain.risk import DEFAULT_HARD_CAPS, PilotHardCaps, RiskSettings, apply_hard_caps
from app.domain.types import Direction
from app.planning.ladder import StageBlueprint

_BPS_DENOMINATOR = Decimal("10000")


class RiskPlanningError(ValueError):
    """Raised when a supplied stop, budget, or plan shape is intrinsically invalid."""


@dataclass(frozen=True, slots=True)
class CostAssumptions:
    entry_fee_rate: Decimal
    exit_fee_rate: Decimal
    entry_slippage_bps: Decimal
    stop_slippage_bps: Decimal
    funding_buffer_rate: Decimal
    funding_interval_count: int

    def __post_init__(self) -> None:
        decimal_values = (
            self.entry_fee_rate,
            self.exit_fee_rate,
            self.entry_slippage_bps,
            self.stop_slippage_bps,
            self.funding_buffer_rate,
        )
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in decimal_values):
            raise RiskPlanningError("cost assumptions must be finite Decimal values")
        if min(decimal_values) < ZERO:
            raise RiskPlanningError("cost assumptions must not be negative")
        if (
            not isinstance(self.funding_interval_count, int)
            or isinstance(self.funding_interval_count, bool)
            or self.funding_interval_count < 0
        ):
            raise RiskPlanningError("funding_interval_count must be a non-negative integer")


class RiskEnvelopeState(StrEnum):
    READY = "READY"
    SKIP = "SKIP"
    HALT = "HALT"


@dataclass(frozen=True, slots=True)
class PlanningContext:
    """All verified live-risk facts required before a plan may be constructed."""

    symbol: str
    requested_settings: RiskSettings
    verified_available_equity_usdt: Decimal | None
    verified_effective_leverage: int | None
    leverage_bracket_notional_cap_usdt: Decimal | None
    required_reserve_usdt: Decimal | None
    existing_symbol_exposure_usdt: Decimal | None
    existing_total_exposure_usdt: Decimal | None
    symbol_exposure_cap_usdt: Decimal | None
    total_exposure_cap_usdt: Decimal | None
    existing_open_position_count: int | None
    active_strategy_count: int | None
    remaining_daily_loss_usdt: Decimal | None
    remaining_weekly_drawdown_usdt: Decimal | None
    consecutive_loss_count: int | None
    isolated_margin_verified: bool | None
    one_way_mode_verified: bool | None
    server_stop_capable: bool | None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("planning context symbol is required")
        decimal_fields = (
            self.verified_available_equity_usdt,
            self.leverage_bracket_notional_cap_usdt,
            self.required_reserve_usdt,
            self.existing_symbol_exposure_usdt,
            self.existing_total_exposure_usdt,
            self.symbol_exposure_cap_usdt,
            self.total_exposure_cap_usdt,
            self.remaining_daily_loss_usdt,
            self.remaining_weekly_drawdown_usdt,
        )
        for decimal_value in decimal_fields:
            if decimal_value is not None and (
                not isinstance(decimal_value, Decimal)
                or not decimal_value.is_finite()
                or decimal_value < ZERO
            ):
                raise ValueError("verified Decimal planning facts must be finite and non-negative")
        integer_fields = (
            self.verified_effective_leverage,
            self.existing_open_position_count,
            self.active_strategy_count,
            self.consecutive_loss_count,
        )
        for integer_value in integer_fields:
            if integer_value is not None and (
                not isinstance(integer_value, int)
                or isinstance(integer_value, bool)
                or integer_value < 0
            ):
                raise ValueError("verified integer planning facts must be non-negative integers")
        if self.verified_effective_leverage == 0:
            raise ValueError("verified effective leverage must be positive when supplied")
        boolean_fields = (
            self.isolated_margin_verified,
            self.one_way_mode_verified,
            self.server_stop_capable,
        )
        if any(value is not None and not isinstance(value, bool) for value in boolean_fields):
            raise TypeError("verified planning flags must be boolean or unavailable")


@dataclass(frozen=True, slots=True)
class RiskEnvelope:
    state: RiskEnvelopeState
    reason: str | None
    settings: RiskSettings
    effective_equity_usdt: Decimal
    effective_leverage: int
    risk_budget_usdt: Decimal
    remaining_symbol_exposure_usdt: Decimal
    remaining_total_exposure_usdt: Decimal
    required_reserve_usdt: Decimal

    @property
    def is_ready(self) -> bool:
        return self.state is RiskEnvelopeState.READY


@dataclass(frozen=True, slots=True)
class WorstCasePriceProjection:
    entry_price: Decimal
    stop_price: Decimal
    worst_entry_fill_price: Decimal
    worst_stop_exit_price: Decimal


@dataclass(frozen=True, slots=True)
class PlannedStage:
    index: int
    entry_price: Decimal
    worst_entry_fill_price: Decimal
    worst_stop_exit_price: Decimal
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
    planned_notional_usdt: Decimal
    required_margin_usdt: Decimal
    risk_envelope: RiskEnvelope
    stages: tuple[PlannedStage, ...]
    skip_reason: str | None = None

    @property
    def is_skipped(self) -> bool:
        return self.skip_reason is not None


def build_risk_envelope(
    planning_context: PlanningContext,
    caps: PilotHardCaps = DEFAULT_HARD_CAPS,
) -> RiskEnvelope:
    """Reject cap violations and skip whenever verified account facts are incomplete."""
    settings = apply_hard_caps(planning_context.requested_settings, caps)
    missing_fields = tuple(
        field_name
        for field_name, value in (
            ("verified_available_equity_usdt", planning_context.verified_available_equity_usdt),
            ("verified_effective_leverage", planning_context.verified_effective_leverage),
            (
                "leverage_bracket_notional_cap_usdt",
                planning_context.leverage_bracket_notional_cap_usdt,
            ),
            ("required_reserve_usdt", planning_context.required_reserve_usdt),
            ("existing_symbol_exposure_usdt", planning_context.existing_symbol_exposure_usdt),
            ("existing_total_exposure_usdt", planning_context.existing_total_exposure_usdt),
            ("symbol_exposure_cap_usdt", planning_context.symbol_exposure_cap_usdt),
            ("total_exposure_cap_usdt", planning_context.total_exposure_cap_usdt),
            ("existing_open_position_count", planning_context.existing_open_position_count),
            ("active_strategy_count", planning_context.active_strategy_count),
            ("remaining_daily_loss_usdt", planning_context.remaining_daily_loss_usdt),
            ("remaining_weekly_drawdown_usdt", planning_context.remaining_weekly_drawdown_usdt),
            ("consecutive_loss_count", planning_context.consecutive_loss_count),
            ("isolated_margin_verified", planning_context.isolated_margin_verified),
            ("one_way_mode_verified", planning_context.one_way_mode_verified),
            ("server_stop_capable", planning_context.server_stop_capable),
        )
        if value is None
    )
    if missing_fields:
        return _blocked_envelope(settings, "MISSING_VERIFIED_DATA")

    # The explicit missing-data branch makes all values below concrete for runtime and typing.
    assert planning_context.verified_available_equity_usdt is not None
    assert planning_context.verified_effective_leverage is not None
    assert planning_context.leverage_bracket_notional_cap_usdt is not None
    assert planning_context.required_reserve_usdt is not None
    assert planning_context.existing_symbol_exposure_usdt is not None
    assert planning_context.existing_total_exposure_usdt is not None
    assert planning_context.symbol_exposure_cap_usdt is not None
    assert planning_context.total_exposure_cap_usdt is not None
    assert planning_context.existing_open_position_count is not None
    assert planning_context.active_strategy_count is not None
    assert planning_context.remaining_daily_loss_usdt is not None
    assert planning_context.remaining_weekly_drawdown_usdt is not None
    assert planning_context.consecutive_loss_count is not None
    assert planning_context.isolated_margin_verified is not None
    assert planning_context.one_way_mode_verified is not None
    assert planning_context.server_stop_capable is not None

    if not planning_context.isolated_margin_verified:
        return _blocked_envelope(settings, "ISOLATED_MARGIN_UNVERIFIED")
    if not planning_context.one_way_mode_verified:
        return _blocked_envelope(settings, "ONE_WAY_MODE_UNVERIFIED")
    if not planning_context.server_stop_capable:
        return _blocked_envelope(settings, "SERVER_STOP_UNAVAILABLE")
    if planning_context.verified_effective_leverage > settings.max_leverage:
        return _blocked_envelope(settings, "VERIFIED_LEVERAGE_EXCEEDS_CAP")
    if planning_context.consecutive_loss_count >= settings.consecutive_loss_limit:
        return _blocked_envelope(
            settings,
            "CONSECUTIVE_LOSS_LIMIT_REACHED",
            state=RiskEnvelopeState.HALT,
        )
    if planning_context.existing_open_position_count >= settings.max_concurrent_positions:
        return _blocked_envelope(settings, "POSITION_LIMIT_REACHED")
    if planning_context.active_strategy_count >= settings.max_active_strategy_count:
        return _blocked_envelope(settings, "ACTIVE_STRATEGY_LIMIT_REACHED")

    effective_equity = min(
        planning_context.verified_available_equity_usdt,
        settings.pilot_equity_cap_usdt,
    )
    if effective_equity <= ZERO:
        return _blocked_envelope(settings, "NO_EFFECTIVE_EQUITY")
    if planning_context.required_reserve_usdt >= effective_equity:
        return _blocked_envelope(settings, "RESERVE_EXHAUSTS_EQUITY")
    if planning_context.remaining_daily_loss_usdt <= ZERO:
        return _blocked_envelope(settings, "DAILY_LOSS_LIMIT_REACHED")
    if planning_context.remaining_weekly_drawdown_usdt <= ZERO:
        return _blocked_envelope(settings, "WEEKLY_DRAWDOWN_LIMIT_REACHED")

    risk_budget = min(
        settings.risk_per_trade_usdt,
        planning_context.remaining_daily_loss_usdt,
        planning_context.remaining_weekly_drawdown_usdt,
    )
    symbol_cap = min(
        planning_context.symbol_exposure_cap_usdt,
        planning_context.leverage_bracket_notional_cap_usdt,
        effective_equity * planning_context.verified_effective_leverage,
    )
    total_cap = min(
        planning_context.total_exposure_cap_usdt,
        effective_equity * planning_context.verified_effective_leverage,
    )
    remaining_symbol = symbol_cap - planning_context.existing_symbol_exposure_usdt
    remaining_total = total_cap - planning_context.existing_total_exposure_usdt
    if remaining_symbol <= ZERO or remaining_total <= ZERO:
        return _blocked_envelope(settings, "EXPOSURE_LIMIT_REACHED")
    return RiskEnvelope(
        state=RiskEnvelopeState.READY,
        reason=None,
        settings=settings,
        effective_equity_usdt=effective_equity,
        effective_leverage=planning_context.verified_effective_leverage,
        risk_budget_usdt=risk_budget,
        remaining_symbol_exposure_usdt=remaining_symbol,
        remaining_total_exposure_usdt=remaining_total,
        required_reserve_usdt=planning_context.required_reserve_usdt,
    )


def worst_case_price_projection(
    *,
    direction: Direction,
    entry_target: Decimal,
    stop_target: Decimal,
    filters: SymbolFilters,
    costs: CostAssumptions,
) -> WorstCasePriceProjection:
    """Round requested and adverse fill prices in the direction that maximizes loss."""
    entry_price = filters.round_entry_price(direction, entry_target)
    stop_price = filters.round_stop_price(direction, stop_target)
    _validate_stop_relation(direction=direction, entry_price=entry_price, stop_price=stop_price)

    entry_slippage_rate = costs.entry_slippage_bps / _BPS_DENOMINATOR
    stop_slippage_rate = costs.stop_slippage_bps / _BPS_DENOMINATOR
    if direction is Direction.LONG:
        worst_entry_fill = filters.round_price_up(
            entry_price * (Decimal("1") + entry_slippage_rate)
        )
        worst_stop_exit = filters.round_price_down(stop_price * (Decimal("1") - stop_slippage_rate))
    else:
        worst_entry_fill = filters.round_price_down(
            entry_price * (Decimal("1") - entry_slippage_rate)
        )
        worst_stop_exit = filters.round_price_up(stop_price * (Decimal("1") + stop_slippage_rate))
    _validate_stop_relation(
        direction=direction,
        entry_price=worst_entry_fill,
        stop_price=worst_stop_exit,
    )
    filters.validate_price(entry_price)
    filters.validate_price(stop_price)
    filters.validate_price(worst_entry_fill)
    filters.validate_price(worst_stop_exit)
    return WorstCasePriceProjection(
        entry_price=entry_price,
        stop_price=stop_price,
        worst_entry_fill_price=worst_entry_fill,
        worst_stop_exit_price=worst_stop_exit,
    )


def risk_per_unit(
    *,
    direction: Direction,
    entry_price: Decimal,
    stop_price: Decimal,
    filters: SymbolFilters,
    costs: CostAssumptions,
) -> Decimal:
    projection = worst_case_price_projection(
        direction=direction,
        entry_target=entry_price,
        stop_target=stop_price,
        filters=filters,
        costs=costs,
    )
    return _risk_per_unit_from_projection(direction=direction, projection=projection, costs=costs)


def solve_ladder(
    *,
    direction: Direction,
    blueprints: tuple[StageBlueprint, ...],
    stop_price: Decimal,
    filters: SymbolFilters,
    costs: CostAssumptions,
    planning_context: PlanningContext,
) -> LadderPlan:
    envelope = build_risk_envelope(planning_context)
    if not envelope.is_ready:
        return _skip_plan(
            direction,
            stop_price,
            envelope,
            envelope.reason or "RISK_ENVELOPE_BLOCKED",
        )
    assert planning_context.existing_total_exposure_usdt is not None
    if planning_context.symbol != filters.symbol:
        return _skip_plan(direction, stop_price, envelope, "SYMBOL_CONTEXT_MISMATCH")
    if not blueprints:
        return _skip_plan(direction, stop_price, envelope, "NO_STAGES")
    if len(blueprints) > envelope.settings.max_stages:
        return _skip_plan(direction, stop_price, envelope, "HARD_STAGE_LIMIT_EXCEEDED")

    rounded_stop_price = filters.round_stop_price(direction, stop_price)
    stages: list[PlannedStage] = []
    stage_indexes: set[int] = set()
    rounded_stage_keys: set[tuple[Decimal, int | None]] = set()
    for blueprint in blueprints:
        if blueprint.index in stage_indexes:
            return _skip_plan(direction, rounded_stop_price, envelope, "DUPLICATE_STAGE_INDEX")
        stage_indexes.add(blueprint.index)
        try:
            projection = worst_case_price_projection(
                direction=direction,
                entry_target=blueprint.entry_price,
                stop_target=rounded_stop_price,
                filters=filters,
                costs=costs,
            )
        except (FilterViolation, RiskPlanningError):
            return _skip_plan(direction, rounded_stop_price, envelope, "INVALID_STOP_RELATION")
        rounded_stage_key = (projection.entry_price, blueprint.scheduled_at_ms)
        if rounded_stage_key in rounded_stage_keys:
            return _skip_plan(
                direction,
                rounded_stop_price,
                envelope,
                "ROUNDED_STAGE_PRICE_COLLISION",
            )
        rounded_stage_keys.add(rounded_stage_key)
        per_unit = _risk_per_unit_from_projection(
            direction=direction,
            projection=projection,
            costs=costs,
        )
        raw_quantity = envelope.risk_budget_usdt * blueprint.weight / per_unit
        quantity = filters.round_quantity_down(raw_quantity)
        try:
            filters.validate_entry(price=projection.entry_price, quantity=quantity)
        except FilterViolation:
            return _skip_plan(
                direction,
                rounded_stop_price,
                envelope,
                "MIN_NOTIONAL_OR_FILTER_FAILURE",
            )
        stages.append(
            PlannedStage(
                index=blueprint.index,
                entry_price=projection.entry_price,
                worst_entry_fill_price=projection.worst_entry_fill_price,
                worst_stop_exit_price=projection.worst_stop_exit_price,
                quantity=quantity,
                risk_per_unit=per_unit,
                risk_contribution=quantity * per_unit,
                scheduled_at_ms=blueprint.scheduled_at_ms,
            )
        )

    projected_total_loss = sum((stage.risk_contribution for stage in stages), ZERO)
    planned_notional = sum(
        (stage.quantity * stage.worst_entry_fill_price for stage in stages),
        ZERO,
    )
    required_margin = (
        planning_context.existing_total_exposure_usdt + planned_notional
    ) / envelope.effective_leverage + envelope.required_reserve_usdt
    if projected_total_loss > envelope.risk_budget_usdt:
        return _skip_plan(direction, rounded_stop_price, envelope, "ROUNDING_RISK_EXCEEDED")
    if (
        planned_notional > envelope.remaining_symbol_exposure_usdt
        or planned_notional > envelope.remaining_total_exposure_usdt
    ):
        return _skip_plan(direction, rounded_stop_price, envelope, "EXPOSURE_LIMIT_EXCEEDED")
    if required_margin > envelope.effective_equity_usdt:
        return _skip_plan(direction, rounded_stop_price, envelope, "MARGIN_REQUIREMENT_EXCEEDED")
    return LadderPlan(
        direction=direction,
        stop_price=rounded_stop_price,
        risk_budget=envelope.risk_budget_usdt,
        projected_total_loss=projected_total_loss,
        planned_notional_usdt=planned_notional,
        required_margin_usdt=required_margin,
        risk_envelope=envelope,
        stages=tuple(stages),
    )


def _risk_per_unit_from_projection(
    *,
    direction: Direction,
    projection: WorstCasePriceProjection,
    costs: CostAssumptions,
) -> Decimal:
    price_loss = (
        projection.worst_entry_fill_price - projection.worst_stop_exit_price
        if direction is Direction.LONG
        else projection.worst_stop_exit_price - projection.worst_entry_fill_price
    )
    entry_fee = projection.worst_entry_fill_price * costs.entry_fee_rate
    exit_fee = projection.worst_stop_exit_price * costs.exit_fee_rate
    funding_loss = (
        projection.worst_entry_fill_price * costs.funding_buffer_rate * costs.funding_interval_count
    )
    return price_loss + entry_fee + exit_fee + funding_loss


def _validate_stop_relation(
    *,
    direction: Direction,
    entry_price: Decimal,
    stop_price: Decimal,
) -> None:
    if entry_price <= ZERO or stop_price <= ZERO:
        raise RiskPlanningError("entry and stop prices must be positive")
    if direction is Direction.LONG and stop_price >= entry_price:
        raise RiskPlanningError("long stop must be below entry")
    if direction is Direction.SHORT and stop_price <= entry_price:
        raise RiskPlanningError("short stop must be above entry")


def _blocked_envelope(
    settings: RiskSettings,
    reason: str,
    *,
    state: RiskEnvelopeState = RiskEnvelopeState.SKIP,
) -> RiskEnvelope:
    return RiskEnvelope(
        state=state,
        reason=reason,
        settings=settings,
        effective_equity_usdt=ZERO,
        effective_leverage=0,
        risk_budget_usdt=ZERO,
        remaining_symbol_exposure_usdt=ZERO,
        remaining_total_exposure_usdt=ZERO,
        required_reserve_usdt=ZERO,
    )


def _skip_plan(
    direction: Direction,
    stop_price: Decimal,
    envelope: RiskEnvelope,
    reason: str,
) -> LadderPlan:
    return LadderPlan(
        direction=direction,
        stop_price=stop_price,
        risk_budget=envelope.risk_budget_usdt,
        projected_total_loss=ZERO,
        planned_notional_usdt=ZERO,
        required_margin_usdt=ZERO,
        risk_envelope=envelope,
        stages=(),
        skip_reason=reason,
    )
