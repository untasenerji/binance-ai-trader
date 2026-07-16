"""Exact fill accounting and confirmed-position stop-risk evaluation."""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from app.domain.decimal_math import ZERO
from app.domain.types import Direction

V1_DEFAULT_ACCOUNT_ID = "v1-primary"


class FillLedgerError(ValueError):
    pass


class PositionQuantityMismatch(FillLedgerError):
    pass


class FillSemanticConflict(FillLedgerError):
    pass


@dataclass(frozen=True, slots=True)
class FillEvent:
    trade_id: str
    client_order_id: str
    last_quantity: Decimal
    cumulative_quantity: Decimal
    fill_price: Decimal
    fee: Decimal
    fee_asset: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not self.trade_id or not self.client_order_id or not self.fee_asset:
            raise FillLedgerError("fill events need trade, client order, and fee asset identifiers")
        decimal_values = (
            self.last_quantity,
            self.cumulative_quantity,
            self.fill_price,
            self.fee,
        )
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in decimal_values):
            raise FillLedgerError("fill values must be finite Decimal values")
        if self.last_quantity <= ZERO or self.cumulative_quantity < self.last_quantity:
            raise FillLedgerError("fill quantities are inconsistent")
        if self.fill_price <= ZERO or self.fee < ZERO:
            raise FillLedgerError("fill price and fee are invalid")
        if self.occurred_at.tzinfo is None:
            raise FillLedgerError("fill timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class FillLedgerReceipt:
    is_duplicate: bool
    filled_quantity: Decimal
    average_fill_price: Decimal
    total_fee: Decimal


@dataclass(frozen=True, slots=True)
class PositionRiskAssessment:
    position_quantity: Decimal
    average_entry_price: Decimal | None
    actual_notional_usdt: Decimal
    actual_required_margin_usdt: Decimal
    actual_stop_risk: Decimal
    pending_entries_blocked: bool
    hard_halted: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class PortfolioRiskAssessment:
    pending_order_exposure_usdt: Decimal
    position_exposure_usdt: Decimal
    aggregate_symbol_exposure_usdt: Decimal
    aggregate_total_exposure_usdt: Decimal
    required_margin_usdt: Decimal
    reserve_usdt: Decimal
    projected_plan_stop_risk: Decimal
    blocked: bool
    reason: str | None
    exposure_slices: tuple["PortfolioExposureSlice", ...] = ()


class RiskReductionStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


@dataclass(frozen=True, slots=True)
class RiskReductionRequirement:
    plan_id: str
    symbol: str
    direction: Direction
    reason: str
    required_reduction_quantity: Decimal
    status: RiskReductionStatus

    @property
    def is_open(self) -> bool:
        return self.status is RiskReductionStatus.OPEN


class ExposureSourceState(StrEnum):
    EXTERNAL_CONFIRMED = "EXTERNAL_CONFIRMED"
    EXTERNAL_PENDING = "EXTERNAL_PENDING"
    CONFIRMED = "CONFIRMED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PENDING = "PENDING"


@dataclass(frozen=True, slots=True)
class PortfolioExposureSlice:
    """One independently margined account exposure fact."""

    slice_id: str
    plan_id: str
    symbol: str
    direction: Direction
    notional_usdt: Decimal
    leverage: int
    required_margin_usdt: Decimal
    source_state: ExposureSourceState
    account_id: str = V1_DEFAULT_ACCOUNT_ID

    def __post_init__(self) -> None:
        if not self.slice_id or not self.plan_id or not self.symbol or not self.account_id:
            raise FillLedgerError("portfolio exposure slices need durable identity")
        if not isinstance(self.direction, Direction):
            raise TypeError("portfolio exposure direction must be typed")
        if not isinstance(self.source_state, ExposureSourceState):
            raise TypeError("portfolio exposure source state must be typed")
        if (
            not isinstance(self.leverage, int)
            or isinstance(self.leverage, bool)
            or self.leverage < 1
        ):
            raise FillLedgerError("portfolio exposure leverage must be a positive integer")
        for value in (self.notional_usdt, self.required_margin_usdt):
            if not isinstance(value, Decimal) or not value.is_finite() or value < ZERO:
                raise FillLedgerError("portfolio exposure values must be non-negative Decimals")
        if self.required_margin_usdt != self.notional_usdt / Decimal(self.leverage):
            raise FillLedgerError(
                "portfolio exposure margin must equal notional divided by leverage"
            )

    def canonical_record(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "direction": self.direction.value,
            "leverage": self.leverage,
            "notional_usdt": format(self.notional_usdt, "f"),
            "plan_id": self.plan_id,
            "required_margin_usdt": format(self.required_margin_usdt, "f"),
            "slice_id": self.slice_id,
            "source_state": self.source_state.value,
            "symbol": self.symbol,
        }


@dataclass(frozen=True, slots=True)
class AccountPortfolioEnvelope:
    """Immutable account-level risk authority shared by every plan."""

    account_scope: str
    version: int
    verified_account_equity_usdt: Decimal
    bot_equity_cap_usdt: Decimal
    required_reserve_usdt: Decimal
    max_total_exposure_usdt: Decimal
    max_symbol_exposure_usdt: Decimal
    max_required_margin_usdt: Decimal
    daily_remaining_risk_usdt: Decimal
    weekly_remaining_risk_usdt: Decimal
    open_position_count: int
    pending_order_count: int
    exposure_slices: tuple[PortfolioExposureSlice, ...] = ()
    reconciliation_required: bool = False
    fingerprint: str = ""
    # V1 supports exactly one configured account. account_scope remains a legacy
    # label so existing durable rows can be replayed without treating it as tenancy.
    account_id: str = V1_DEFAULT_ACCOUNT_ID
    symbol_exposure_caps_usdt: Mapping[str, Decimal] | None = None
    effective_from: datetime | None = None
    superseded_by: int | None = None

    def __post_init__(self) -> None:
        if not self.account_scope or not self.account_id:
            raise FillLedgerError("portfolio envelope needs an account scope")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise FillLedgerError("portfolio envelope version must be positive")
        decimal_values = (
            self.verified_account_equity_usdt,
            self.bot_equity_cap_usdt,
            self.required_reserve_usdt,
            self.max_total_exposure_usdt,
            self.max_symbol_exposure_usdt,
            self.max_required_margin_usdt,
            self.daily_remaining_risk_usdt,
            self.weekly_remaining_risk_usdt,
        )
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value < ZERO
            for value in decimal_values
        ):
            raise FillLedgerError("portfolio envelope values must be non-negative Decimals")
        if (
            min(
                self.verified_account_equity_usdt,
                self.bot_equity_cap_usdt,
                self.max_total_exposure_usdt,
                self.max_symbol_exposure_usdt,
                self.max_required_margin_usdt,
            )
            <= ZERO
        ):
            raise FillLedgerError("portfolio envelope financial caps must be positive")
        if self.required_reserve_usdt > self.max_required_margin_usdt:
            raise FillLedgerError("portfolio reserve cannot exceed maximum required margin")
        raw_symbol_caps = self.symbol_exposure_caps_usdt
        if raw_symbol_caps is None:
            raw_symbol_caps = {"*": self.max_symbol_exposure_usdt}
        if not isinstance(raw_symbol_caps, Mapping) or not raw_symbol_caps:
            raise FillLedgerError("portfolio envelope needs a non-empty symbol cap map")
        normalized_symbol_caps: dict[str, Decimal] = {}
        for symbol, cap in raw_symbol_caps.items():
            if not isinstance(symbol, str) or not symbol:
                raise FillLedgerError("portfolio envelope symbol cap keys must be non-empty")
            if not isinstance(cap, Decimal) or not cap.is_finite() or cap <= ZERO:
                raise FillLedgerError("portfolio envelope symbol caps must be positive Decimals")
            normalized_symbol_caps[symbol] = cap
        object.__setattr__(
            self,
            "symbol_exposure_caps_usdt",
            MappingProxyType(dict(sorted(normalized_symbol_caps.items()))),
        )
        if self.effective_from is not None and self.effective_from.tzinfo is None:
            raise FillLedgerError("portfolio envelope effective_from must be timezone-aware")
        if self.superseded_by is not None and (
            not isinstance(self.superseded_by, int)
            or isinstance(self.superseded_by, bool)
            or self.superseded_by <= self.version
        ):
            raise FillLedgerError("portfolio envelope superseded_by must follow its version")
        for count in (self.open_position_count, self.pending_order_count):
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise FillLedgerError("portfolio envelope counts must be non-negative integers")
        if not isinstance(self.reconciliation_required, bool):
            raise TypeError("portfolio envelope reconciliation flag must be boolean")
        if any(type(item) is not PortfolioExposureSlice for item in self.exposure_slices):
            raise TypeError("portfolio envelope exposure slices must be exact typed values")
        if any(item.account_id != self.account_id for item in self.exposure_slices):
            raise FillLedgerError("portfolio exposure slices must match their account envelope")
        slice_ids = [item.slice_id for item in self.exposure_slices]
        if len(slice_ids) != len(set(slice_ids)):
            raise FillLedgerError("portfolio envelope exposure slice IDs must be unique")
        ordered_slices = tuple(sorted(self.exposure_slices, key=lambda item: item.slice_id))
        object.__setattr__(self, "exposure_slices", ordered_slices)
        expected = self.expected_fingerprint()
        legacy_expected = self._legacy_expected_fingerprint()
        if self.fingerprint and self.fingerprint not in {expected, legacy_expected}:
            raise FillLedgerError("portfolio envelope fingerprint is invalid")
        object.__setattr__(self, "fingerprint", self.fingerprint or expected)

    @property
    def effective_equity_usdt(self) -> Decimal:
        return min(self.verified_account_equity_usdt, self.bot_equity_cap_usdt)

    @property
    def existing_total_exposure_usdt(self) -> Decimal:
        return sum((item.notional_usdt for item in self.exposure_slices), ZERO)

    def existing_symbol_exposure_usdt(self, symbol: str) -> Decimal:
        return sum(
            (item.notional_usdt for item in self.exposure_slices if item.symbol == symbol),
            ZERO,
        )

    def symbol_exposure_cap_usdt(self, symbol: str) -> Decimal:
        caps = self.symbol_exposure_caps_usdt
        assert caps is not None
        cap = caps.get(symbol, caps.get("*"))
        if cap is None:
            raise FillLedgerError(f"portfolio envelope has no symbol cap for {symbol}")
        return cap

    @property
    def existing_required_margin_usdt(self) -> Decimal:
        return sum((item.required_margin_usdt for item in self.exposure_slices), ZERO)

    def expected_fingerprint(self) -> str:
        canonical = json.dumps(
            {
                "account_id": self.account_id,
                "account_scope": self.account_scope,
                "bot_equity_cap_usdt": format(self.bot_equity_cap_usdt, "f"),
                "daily_remaining_risk_usdt": format(self.daily_remaining_risk_usdt, "f"),
                "exposure_slices": [item.canonical_record() for item in self.exposure_slices],
                "max_required_margin_usdt": format(self.max_required_margin_usdt, "f"),
                "max_symbol_exposure_usdt": format(self.max_symbol_exposure_usdt, "f"),
                "max_total_exposure_usdt": format(self.max_total_exposure_usdt, "f"),
                "open_position_count": self.open_position_count,
                "pending_order_count": self.pending_order_count,
                "reconciliation_required": self.reconciliation_required,
                "required_reserve_usdt": format(self.required_reserve_usdt, "f"),
                "effective_from": (
                    None
                    if self.effective_from is None
                    else self.effective_from.astimezone(UTC).isoformat()
                ),
                "superseded_by": self.superseded_by,
                "symbol_exposure_caps_usdt": {
                    symbol: format(cap, "f")
                    for symbol, cap in sorted((self.symbol_exposure_caps_usdt or {}).items())
                },
                "verified_account_equity_usdt": format(self.verified_account_equity_usdt, "f"),
                "version": self.version,
                "weekly_remaining_risk_usdt": format(self.weekly_remaining_risk_usdt, "f"),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _legacy_expected_fingerprint(self) -> str:
        """Accept only historical rows whose old immutable fingerprint still verifies."""
        canonical = json.dumps(
            {
                "account_scope": self.account_scope,
                "bot_equity_cap_usdt": format(self.bot_equity_cap_usdt, "f"),
                "daily_remaining_risk_usdt": format(self.daily_remaining_risk_usdt, "f"),
                "exposure_slices": [
                    {
                        key: value
                        for key, value in item.canonical_record().items()
                        if key != "account_id"
                    }
                    for item in self.exposure_slices
                ],
                "max_required_margin_usdt": format(self.max_required_margin_usdt, "f"),
                "max_symbol_exposure_usdt": format(self.max_symbol_exposure_usdt, "f"),
                "max_total_exposure_usdt": format(self.max_total_exposure_usdt, "f"),
                "open_position_count": self.open_position_count,
                "pending_order_count": self.pending_order_count,
                "reconciliation_required": self.reconciliation_required,
                "required_reserve_usdt": format(self.required_reserve_usdt, "f"),
                "verified_account_equity_usdt": format(self.verified_account_equity_usdt, "f"),
                "version": self.version,
                "weekly_remaining_risk_usdt": format(self.weekly_remaining_risk_usdt, "f"),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ActualRiskPolicy:
    """Immutable local policy used to gate entry stages after durable simulated fills."""

    plan_id: str
    symbol: str
    direction: Direction
    worst_stop_exit_price: Decimal
    exit_fee_rate: Decimal
    funding_buffer_rate: Decimal
    funding_interval_count: int
    risk_budget: Decimal
    effective_leverage: int
    protective_stop_reference: str
    reduce_only_exit_reference: str
    portfolio_envelope: AccountPortfolioEnvelope

    def __post_init__(self) -> None:
        if not self.plan_id or not self.symbol:
            raise FillLedgerError("actual-risk policy needs plan and symbol identifiers")
        if not self.protective_stop_reference or not self.reduce_only_exit_reference:
            raise FillLedgerError("actual-risk policy needs durable protection references")
        if not isinstance(self.direction, Direction):
            raise TypeError("actual-risk policy direction must be typed")
        if type(self.portfolio_envelope) is not AccountPortfolioEnvelope:
            raise TypeError("actual-risk policy requires an exact account portfolio envelope")
        # Keep policy validation aligned with the financial evaluator so a malformed
        # policy cannot silently skip the post-fill entry gate.
        evaluate_simulated_position_risk(
            direction=self.direction,
            signed_simulated_position_quantity=ZERO,
            fills=FillLedger(),
            worst_stop_exit_price=self.worst_stop_exit_price,
            exit_fee_rate=self.exit_fee_rate,
            funding_buffer_rate=self.funding_buffer_rate,
            funding_interval_count=self.funding_interval_count,
            risk_budget=self.risk_budget,
            simulated_protection_ready=False,
            max_symbol_exposure_usdt=self.max_symbol_exposure_usdt,
            max_total_exposure_usdt=self.max_total_exposure_usdt,
            existing_symbol_exposure_usdt=self.existing_symbol_exposure_usdt,
            existing_total_exposure_usdt=self.existing_total_exposure_usdt,
            effective_leverage=self.effective_leverage,
            required_reserve_usdt=self.required_reserve_usdt,
            effective_equity_usdt=self.effective_equity_usdt,
        )

    @property
    def max_symbol_exposure_usdt(self) -> Decimal:
        return self.portfolio_envelope.symbol_exposure_cap_usdt(self.symbol)

    @property
    def max_total_exposure_usdt(self) -> Decimal:
        return self.portfolio_envelope.max_total_exposure_usdt

    @property
    def existing_symbol_exposure_usdt(self) -> Decimal:
        return self.portfolio_envelope.existing_symbol_exposure_usdt(self.symbol)

    @property
    def existing_total_exposure_usdt(self) -> Decimal:
        return self.portfolio_envelope.existing_total_exposure_usdt

    @property
    def required_reserve_usdt(self) -> Decimal:
        return self.portfolio_envelope.required_reserve_usdt

    @property
    def effective_equity_usdt(self) -> Decimal:
        return self.portfolio_envelope.effective_equity_usdt


@dataclass(slots=True)
class FillLedger:
    """Deduplicates authoritative fill facts across entry stages without financial gaps."""

    _events_by_trade_id: dict[str, FillEvent] = field(default_factory=dict)

    @classmethod
    def from_events(cls, events: Iterable[FillEvent]) -> "FillLedger":
        ledger = cls()
        for event in events:
            ledger.record(event)
        return ledger

    def record(self, event: FillEvent) -> FillLedgerReceipt:
        existing = self._events_by_trade_id.get(event.trade_id)
        if existing is not None:
            if existing != event:
                raise FillSemanticConflict("trade ID was redelivered with a semantic conflict")
            return self._receipt(is_duplicate=True)
        self._assert_contiguous_client_fills(
            (*self._events_for_client(event.client_order_id), event)
        )
        self._events_by_trade_id[event.trade_id] = event
        return self._receipt(is_duplicate=False)

    @property
    def filled_quantity(self) -> Decimal:
        return sum((event.last_quantity for event in self._events_by_trade_id.values()), ZERO)

    @property
    def average_fill_price(self) -> Decimal:
        if self.filled_quantity <= ZERO:
            raise FillLedgerError("average fill price requires at least one fill")
        filled_notional = sum(
            (event.last_quantity * event.fill_price for event in self._events_by_trade_id.values()),
            ZERO,
        )
        return filled_notional / self.filled_quantity

    @property
    def total_fee(self) -> Decimal:
        return sum((event.fee for event in self._events_by_trade_id.values()), ZERO)

    @property
    def observed_cumulative_quantity(self) -> Decimal:
        return sum(
            (
                max(event.cumulative_quantity for event in self._events_for_client(client_order_id))
                for client_order_id in self.client_order_ids
            ),
            ZERO,
        )

    @property
    def client_order_ids(self) -> frozenset[str]:
        return frozenset(event.client_order_id for event in self._events_by_trade_id.values())

    @property
    def fee_assets(self) -> frozenset[str]:
        return frozenset(event.fee_asset for event in self._events_by_trade_id.values())

    @property
    def events(self) -> tuple[FillEvent, ...]:
        return tuple(
            sorted(
                self._events_by_trade_id.values(),
                key=lambda event: (
                    event.client_order_id,
                    event.cumulative_quantity,
                    event.trade_id,
                ),
            )
        )

    def filled_quantity_for(self, client_order_id: str) -> Decimal:
        return sum(
            (event.last_quantity for event in self._events_for_client(client_order_id)),
            ZERO,
        )

    def _events_for_client(self, client_order_id: str) -> tuple[FillEvent, ...]:
        return tuple(
            event
            for event in self._events_by_trade_id.values()
            if event.client_order_id == client_order_id
        )

    @staticmethod
    def _assert_contiguous_client_fills(events: Iterable[FillEvent]) -> None:
        expected_cumulative = ZERO
        for event in sorted(
            events, key=lambda candidate: (candidate.cumulative_quantity, candidate.trade_id)
        ):
            if event.cumulative_quantity - event.last_quantity != expected_cumulative:
                raise FillLedgerError(
                    "fill cumulative quantities must be contiguous per client order"
                )
            expected_cumulative = event.cumulative_quantity

    def _receipt(self, *, is_duplicate: bool) -> FillLedgerReceipt:
        return FillLedgerReceipt(
            is_duplicate=is_duplicate,
            filled_quantity=self.filled_quantity,
            average_fill_price=self.average_fill_price,
            total_fee=self.total_fee,
        )


def _evaluate_position_risk(
    *,
    direction: Direction,
    signed_position_quantity: Decimal,
    fills: FillLedger,
    worst_stop_exit_price: Decimal,
    exit_fee_rate: Decimal,
    funding_buffer_rate: Decimal,
    funding_interval_count: int,
    risk_budget: Decimal,
    protection_ready: bool,
    unprotected_reason: str,
    max_symbol_exposure_usdt: Decimal | None = None,
    max_total_exposure_usdt: Decimal | None = None,
    existing_symbol_exposure_usdt: Decimal = ZERO,
    existing_total_exposure_usdt: Decimal = ZERO,
    effective_leverage: int | None = None,
    required_reserve_usdt: Decimal = ZERO,
    effective_equity_usdt: Decimal | None = None,
) -> PositionRiskAssessment:
    decimal_values = (
        signed_position_quantity,
        worst_stop_exit_price,
        exit_fee_rate,
        funding_buffer_rate,
        risk_budget,
        existing_symbol_exposure_usdt,
        existing_total_exposure_usdt,
        required_reserve_usdt,
    )
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in decimal_values):
        raise FillLedgerError("position-risk values must be finite Decimal values")
    if worst_stop_exit_price <= ZERO or min(exit_fee_rate, funding_buffer_rate, risk_budget) < ZERO:
        raise FillLedgerError("position-risk inputs are invalid")
    if (
        not isinstance(funding_interval_count, int)
        or isinstance(funding_interval_count, bool)
        or funding_interval_count < 0
    ):
        raise FillLedgerError("funding interval count is invalid")
    cap_values = (max_symbol_exposure_usdt, max_total_exposure_usdt, effective_equity_usdt)
    if any(
        value is not None and (not isinstance(value, Decimal) or not value.is_finite())
        for value in cap_values
    ):
        raise FillLedgerError("exposure and margin caps must be finite Decimal values")
    if (
        min(
            existing_symbol_exposure_usdt,
            existing_total_exposure_usdt,
            required_reserve_usdt,
        )
        < ZERO
    ):
        raise FillLedgerError("existing exposure and reserve values must be non-negative")
    has_cap_policy = any(value is not None for value in cap_values)
    if has_cap_policy:
        if any(value is None for value in cap_values):
            raise FillLedgerError("exposure and margin caps must be supplied together")
        assert max_symbol_exposure_usdt is not None
        assert max_total_exposure_usdt is not None
        assert effective_equity_usdt is not None
        if min(max_symbol_exposure_usdt, max_total_exposure_usdt, effective_equity_usdt) < ZERO:
            raise FillLedgerError("exposure and equity caps must be non-negative")
        if (
            not isinstance(effective_leverage, int)
            or isinstance(effective_leverage, bool)
            or effective_leverage < 1
        ):
            raise FillLedgerError("effective leverage must be a positive integer")
    elif effective_leverage is not None:
        raise FillLedgerError("effective leverage requires complete exposure and margin caps")
    if signed_position_quantity == ZERO:
        return PositionRiskAssessment(
            position_quantity=ZERO,
            average_entry_price=None,
            actual_notional_usdt=ZERO,
            actual_required_margin_usdt=ZERO,
            actual_stop_risk=ZERO,
            pending_entries_blocked=False,
            hard_halted=False,
            reason=None,
        )
    if direction is Direction.LONG and signed_position_quantity < ZERO:
        raise PositionQuantityMismatch("long position quantity must be positive")
    if direction is Direction.SHORT and signed_position_quantity > ZERO:
        raise PositionQuantityMismatch("short position quantity must be negative")
    position_quantity = abs(signed_position_quantity)
    if fills.filled_quantity <= ZERO or position_quantity > fills.filled_quantity:
        raise PositionQuantityMismatch("position quantity exceeds durable entry fills")
    if fills.fee_assets != frozenset({"USDT"}):
        raise FillLedgerError("non-USDT fees need a verified conversion before risk evaluation")

    average_entry = fills.average_fill_price
    price_loss_per_unit = (
        average_entry - worst_stop_exit_price
        if direction is Direction.LONG
        else worst_stop_exit_price - average_entry
    )
    if price_loss_per_unit < ZERO:
        raise FillLedgerError("stop is on the non-loss side of the confirmed entry")
    allocated_entry_fee = fills.total_fee * position_quantity / fills.filled_quantity
    projected_exit_fee = position_quantity * worst_stop_exit_price * exit_fee_rate
    funding_buffer = (
        position_quantity * average_entry * funding_buffer_rate * funding_interval_count
    )
    actual_risk = (
        position_quantity * price_loss_per_unit
        + allocated_entry_fee
        + projected_exit_fee
        + funding_buffer
    )
    actual_notional = position_quantity * average_entry
    actual_required_margin = (
        (existing_total_exposure_usdt + actual_notional) / Decimal(effective_leverage)
        + required_reserve_usdt
        if effective_leverage is not None
        else ZERO
    )
    if not protection_ready:
        return PositionRiskAssessment(
            position_quantity=position_quantity,
            average_entry_price=average_entry,
            actual_notional_usdt=actual_notional,
            actual_required_margin_usdt=actual_required_margin,
            actual_stop_risk=actual_risk,
            pending_entries_blocked=True,
            hard_halted=True,
            reason=unprotected_reason,
        )
    if has_cap_policy:
        assert max_symbol_exposure_usdt is not None
        assert max_total_exposure_usdt is not None
        assert effective_equity_usdt is not None
        if (
            existing_symbol_exposure_usdt + actual_notional > max_symbol_exposure_usdt
            or existing_total_exposure_usdt + actual_notional > max_total_exposure_usdt
        ):
            return PositionRiskAssessment(
                position_quantity=position_quantity,
                average_entry_price=average_entry,
                actual_notional_usdt=actual_notional,
                actual_required_margin_usdt=actual_required_margin,
                actual_stop_risk=actual_risk,
                pending_entries_blocked=True,
                hard_halted=False,
                reason="ACTUAL_EXPOSURE_LIMIT_BREACH",
            )
        if actual_required_margin > effective_equity_usdt:
            return PositionRiskAssessment(
                position_quantity=position_quantity,
                average_entry_price=average_entry,
                actual_notional_usdt=actual_notional,
                actual_required_margin_usdt=actual_required_margin,
                actual_stop_risk=actual_risk,
                pending_entries_blocked=True,
                hard_halted=False,
                reason="ACTUAL_MARGIN_REQUIREMENT_BREACH",
            )
    if actual_risk > risk_budget:
        return PositionRiskAssessment(
            position_quantity=position_quantity,
            average_entry_price=average_entry,
            actual_notional_usdt=actual_notional,
            actual_required_margin_usdt=actual_required_margin,
            actual_stop_risk=actual_risk,
            pending_entries_blocked=True,
            hard_halted=False,
            reason="ACTUAL_STOP_RISK_BREACH",
        )
    return PositionRiskAssessment(
        position_quantity=position_quantity,
        average_entry_price=average_entry,
        actual_notional_usdt=actual_notional,
        actual_required_margin_usdt=actual_required_margin,
        actual_stop_risk=actual_risk,
        pending_entries_blocked=False,
        hard_halted=False,
        reason=None,
    )


def evaluate_confirmed_position_risk(
    *,
    direction: Direction,
    signed_confirmed_position_quantity: Decimal,
    fills: FillLedger,
    worst_stop_exit_price: Decimal,
    exit_fee_rate: Decimal,
    funding_buffer_rate: Decimal,
    funding_interval_count: int,
    risk_budget: Decimal,
    stop_confirmed: bool,
    max_symbol_exposure_usdt: Decimal | None = None,
    max_total_exposure_usdt: Decimal | None = None,
    existing_symbol_exposure_usdt: Decimal = ZERO,
    existing_total_exposure_usdt: Decimal = ZERO,
    effective_leverage: int | None = None,
    required_reserve_usdt: Decimal = ZERO,
    effective_equity_usdt: Decimal | None = None,
) -> PositionRiskAssessment:
    """Evaluate exchange-confirmed quantity against durable fill and stop evidence."""
    return _evaluate_position_risk(
        direction=direction,
        signed_position_quantity=signed_confirmed_position_quantity,
        fills=fills,
        worst_stop_exit_price=worst_stop_exit_price,
        exit_fee_rate=exit_fee_rate,
        funding_buffer_rate=funding_buffer_rate,
        funding_interval_count=funding_interval_count,
        risk_budget=risk_budget,
        protection_ready=stop_confirmed,
        unprotected_reason="STOP_UNCONFIRMED",
        max_symbol_exposure_usdt=max_symbol_exposure_usdt,
        max_total_exposure_usdt=max_total_exposure_usdt,
        existing_symbol_exposure_usdt=existing_symbol_exposure_usdt,
        existing_total_exposure_usdt=existing_total_exposure_usdt,
        effective_leverage=effective_leverage,
        required_reserve_usdt=required_reserve_usdt,
        effective_equity_usdt=effective_equity_usdt,
    )


def evaluate_simulated_position_risk(
    *,
    direction: Direction,
    signed_simulated_position_quantity: Decimal,
    fills: FillLedger,
    worst_stop_exit_price: Decimal,
    exit_fee_rate: Decimal,
    funding_buffer_rate: Decimal,
    funding_interval_count: int,
    risk_budget: Decimal,
    simulated_protection_ready: bool,
    unprotected_reason: str = "SIMULATED_PROTECTION_MISSING",
    max_symbol_exposure_usdt: Decimal | None = None,
    max_total_exposure_usdt: Decimal | None = None,
    existing_symbol_exposure_usdt: Decimal = ZERO,
    existing_total_exposure_usdt: Decimal = ZERO,
    effective_leverage: int | None = None,
    required_reserve_usdt: Decimal = ZERO,
    effective_equity_usdt: Decimal | None = None,
) -> PositionRiskAssessment:
    """Evaluate rehearsal-only position and protection facts without exchange claims."""
    return _evaluate_position_risk(
        direction=direction,
        signed_position_quantity=signed_simulated_position_quantity,
        fills=fills,
        worst_stop_exit_price=worst_stop_exit_price,
        exit_fee_rate=exit_fee_rate,
        funding_buffer_rate=funding_buffer_rate,
        funding_interval_count=funding_interval_count,
        risk_budget=risk_budget,
        protection_ready=simulated_protection_ready,
        unprotected_reason=unprotected_reason,
        max_symbol_exposure_usdt=max_symbol_exposure_usdt,
        max_total_exposure_usdt=max_total_exposure_usdt,
        existing_symbol_exposure_usdt=existing_symbol_exposure_usdt,
        existing_total_exposure_usdt=existing_total_exposure_usdt,
        effective_leverage=effective_leverage,
        required_reserve_usdt=required_reserve_usdt,
        effective_equity_usdt=effective_equity_usdt,
    )
