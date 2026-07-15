"""Typed normal/algo order and fail-closed reconciliation contracts."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.domain.decimal_math import ZERO
from app.domain.types import Direction


class NormalOrderType(StrEnum):
    LIMIT = "LIMIT"
    REDUCE_ONLY_LIMIT = "REDUCE_ONLY_LIMIT"
    EMERGENCY_REDUCE = "EMERGENCY_REDUCE"


class AlgoOrderType(StrEnum):
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class NormalOrderRole(StrEnum):
    ENTRY = "ENTRY"
    TAKE_PROFIT = "TAKE_PROFIT"
    EMERGENCY_REDUCE = "EMERGENCY_REDUCE"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class StopWorkingType(StrEnum):
    MARK_PRICE = "MARK_PRICE"
    CONTRACT_PRICE = "CONTRACT_PRICE"


class AlgoOrderStatus(StrEnum):
    NEW = "NEW"
    TRIGGERED = "TRIGGERED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class StopQuantitySemantics(StrEnum):
    CLOSE_POSITION_FULL = "CLOSE_POSITION_FULL"


@dataclass(frozen=True, slots=True)
class NormalOrderIntent:
    client_order_id: str
    symbol: str
    direction: Direction
    order_type: NormalOrderType
    quantity: Decimal
    price: Decimal
    role: NormalOrderRole = NormalOrderRole.ENTRY
    reduce_only: bool = False

    def __post_init__(self) -> None:
        if not self.client_order_id or not self.symbol:
            raise ValueError("client_order_id and symbol are required")
        if self.quantity <= ZERO or self.price <= ZERO:
            raise ValueError("normal order quantity and price must be positive")
        if self.role is NormalOrderRole.ENTRY:
            if self.reduce_only or self.order_type is not NormalOrderType.LIMIT:
                raise ValueError("entry orders must be non-reduce-only LIMIT orders")
        elif self.role is NormalOrderRole.TAKE_PROFIT:
            if not self.reduce_only or self.order_type is not NormalOrderType.REDUCE_ONLY_LIMIT:
                raise ValueError("take-profit orders must be reduce-only limits")
        elif self.role is NormalOrderRole.EMERGENCY_REDUCE and (
            not self.reduce_only or self.order_type is not NormalOrderType.EMERGENCY_REDUCE
        ):
            raise ValueError("emergency reductions must be reduce-only emergency orders")

    @property
    def side(self) -> OrderSide:
        if self.role is NormalOrderRole.ENTRY:
            return OrderSide.BUY if self.direction is Direction.LONG else OrderSide.SELL
        return OrderSide.SELL if self.direction is Direction.LONG else OrderSide.BUY


@dataclass(frozen=True, slots=True)
class AlgoOrderIntent:
    client_algo_id: str
    symbol: str
    direction: Direction
    algo_type: AlgoOrderType
    trigger_price: Decimal
    close_position: bool
    quantity: Decimal | None = None
    working_type: StopWorkingType = StopWorkingType.MARK_PRICE
    status: AlgoOrderStatus = AlgoOrderStatus.NEW
    plan_id: str | None = None
    policy_version: int | None = None
    account_envelope_version: int | None = None
    policy_fingerprint: str | None = None
    account_envelope_fingerprint: str | None = None
    stop_contract_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not self.client_algo_id or not self.symbol or self.trigger_price <= ZERO:
            raise ValueError("algo order fields are invalid")
        if self.quantity is not None and (
            not isinstance(self.quantity, Decimal)
            or not self.quantity.is_finite()
            or self.quantity <= ZERO
        ):
            raise ValueError("algo order quantity must be a positive finite Decimal")
        if not isinstance(self.working_type, StopWorkingType):
            raise TypeError("algo order working type must be typed")
        if not isinstance(self.status, AlgoOrderStatus):
            raise TypeError("algo order status must be typed")
        for version in (self.policy_version, self.account_envelope_version):
            if version is not None and (
                not isinstance(version, int) or isinstance(version, bool) or version < 1
            ):
                raise ValueError("algo order policy versions must be positive integers")
        for fingerprint in (
            self.policy_fingerprint,
            self.account_envelope_fingerprint,
            self.stop_contract_fingerprint,
        ):
            if fingerprint is not None and (
                not isinstance(fingerprint, str) or len(fingerprint) != 64
            ):
                raise ValueError("algo order fingerprints must be 64-character strings")
        if self.algo_type is AlgoOrderType.STOP_MARKET:
            if not self.close_position:
                raise ValueError("protective STOP_MARKET must use closePosition")
            if self.quantity is not None:
                raise ValueError("closePosition STOP_MARKET must not carry a quantity")

    @property
    def side(self) -> OrderSide:
        return OrderSide.SELL if self.direction is Direction.LONG else OrderSide.BUY


@dataclass(frozen=True, slots=True)
class AlgoOrderObservation:
    """Exchange-observed Algo fields, including values invalid for a local intent."""

    client_algo_id: str
    symbol: str
    direction: Direction
    algo_type: AlgoOrderType
    trigger_price: Decimal
    close_position: bool
    quantity: Decimal | None = None
    working_type: StopWorkingType = StopWorkingType.MARK_PRICE
    status: AlgoOrderStatus = AlgoOrderStatus.NEW
    plan_id: str | None = None
    policy_version: int | None = None
    account_envelope_version: int | None = None
    policy_fingerprint: str | None = None
    account_envelope_fingerprint: str | None = None
    stop_contract_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.client_algo_id
            or not self.symbol
            or not isinstance(self.direction, Direction)
            or not isinstance(self.algo_type, AlgoOrderType)
            or not isinstance(self.trigger_price, Decimal)
            or not self.trigger_price.is_finite()
            or self.trigger_price <= ZERO
            or not isinstance(self.close_position, bool)
            or not isinstance(self.working_type, StopWorkingType)
            or not isinstance(self.status, AlgoOrderStatus)
        ):
            raise ValueError("observed Algo order fields are invalid")
        if self.quantity is not None and (
            not isinstance(self.quantity, Decimal)
            or not self.quantity.is_finite()
            or self.quantity <= ZERO
        ):
            raise ValueError("observed Algo quantity must be a positive Decimal")
        for version in (self.policy_version, self.account_envelope_version):
            if version is not None and (
                not isinstance(version, int) or isinstance(version, bool) or version < 1
            ):
                raise ValueError("observed Algo policy versions are invalid")
        for fingerprint in (
            self.policy_fingerprint,
            self.account_envelope_fingerprint,
            self.stop_contract_fingerprint,
        ):
            if fingerprint is not None and (
                not isinstance(fingerprint, str) or len(fingerprint) != 64
            ):
                raise ValueError("observed Algo fingerprints are invalid")

    @property
    def side(self) -> OrderSide:
        return OrderSide.SELL if self.direction is Direction.LONG else OrderSide.BUY


@dataclass(frozen=True, slots=True)
class ExpectedStopContract:
    """Complete local expectation that an exchange-observed stop must match."""

    plan_id: str
    symbol: str
    position_side: Direction
    expected_order_side: OrderSide
    client_algo_id: str
    algo_type: AlgoOrderType
    trigger_price: Decimal
    working_type: StopWorkingType
    close_position: bool
    quantity_semantics: StopQuantitySemantics
    active_status: AlgoOrderStatus
    policy_version: int
    account_envelope_version: int
    policy_fingerprint: str
    account_envelope_fingerprint: str
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.plan_id or not self.symbol or not self.client_algo_id:
            raise ValueError("expected stop contract needs durable identity")
        if not isinstance(self.position_side, Direction):
            raise TypeError("expected stop position side must be typed")
        inverse = OrderSide.SELL if self.position_side is Direction.LONG else OrderSide.BUY
        if self.expected_order_side is not inverse:
            raise ValueError("expected stop side must be inverse to the position")
        if self.algo_type is not AlgoOrderType.STOP_MARKET:
            raise ValueError("expected protection must be STOP_MARKET")
        if (
            not isinstance(self.trigger_price, Decimal)
            or not self.trigger_price.is_finite()
            or self.trigger_price <= ZERO
        ):
            raise ValueError("expected stop trigger must be a positive Decimal")
        if self.working_type not in {StopWorkingType.MARK_PRICE, StopWorkingType.CONTRACT_PRICE}:
            raise TypeError("expected stop working type must be typed")
        if not self.close_position:
            raise ValueError("expected stop must close the full position")
        if self.quantity_semantics is not StopQuantitySemantics.CLOSE_POSITION_FULL:
            raise ValueError("expected stop must omit quantity under closePosition")
        if self.active_status is not AlgoOrderStatus.NEW:
            raise ValueError("expected stop must be exchange-active")
        for version in (self.policy_version, self.account_envelope_version):
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                raise ValueError("expected stop versions must be positive integers")
        for fingerprint in (
            self.policy_fingerprint,
            self.account_envelope_fingerprint,
        ):
            if not isinstance(fingerprint, str) or len(fingerprint) != 64:
                raise ValueError("expected stop policy fingerprints are invalid")
        expected = self.expected_fingerprint()
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("expected stop contract fingerprint is invalid")
        object.__setattr__(self, "fingerprint", expected)

    def expected_fingerprint(self) -> str:
        canonical = json.dumps(
            {
                "account_envelope_fingerprint": self.account_envelope_fingerprint,
                "account_envelope_version": self.account_envelope_version,
                "active_status": self.active_status.value,
                "algo_type": self.algo_type.value,
                "client_algo_id": self.client_algo_id,
                "close_position": self.close_position,
                "expected_order_side": self.expected_order_side.value,
                "plan_id": self.plan_id,
                "policy_fingerprint": self.policy_fingerprint,
                "policy_version": self.policy_version,
                "position_side": self.position_side.value,
                "quantity_semantics": self.quantity_semantics.value,
                "symbol": self.symbol,
                "trigger_price": format(self.trigger_price, "f"),
                "working_type": self.working_type.value,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def matches(self, observed: object) -> bool:
        return (
            isinstance(observed, (AlgoOrderIntent, AlgoOrderObservation))
            and observed.client_algo_id == self.client_algo_id
            and observed.plan_id == self.plan_id
            and observed.symbol == self.symbol
            and observed.direction is self.position_side
            and observed.side is self.expected_order_side
            and observed.algo_type is self.algo_type
            and observed.trigger_price == self.trigger_price
            and observed.working_type is self.working_type
            and observed.close_position is self.close_position
            and observed.quantity is None
            and observed.status is self.active_status
            and observed.policy_version == self.policy_version
            and observed.account_envelope_version == self.account_envelope_version
            and observed.policy_fingerprint == self.policy_fingerprint
            and observed.account_envelope_fingerprint == self.account_envelope_fingerprint
            and observed.stop_contract_fingerprint == self.fingerprint
        )


@dataclass(frozen=True, slots=True)
class ExchangeProtectionEvidence:
    """Exchange-observed position and protective orders for a future live gate."""

    plan_id: str
    symbol: str
    direction: Direction
    position_quantity: Decimal
    stop_order: AlgoOrderIntent
    reduce_only_exit_reference: str
    position_observed_at_ms: int
    protection_observed_at_ms: int

    def __post_init__(self) -> None:
        if not self.plan_id or not self.symbol or not self.reduce_only_exit_reference:
            raise ValueError("exchange protection evidence needs durable identifiers")
        if (
            not isinstance(self.position_quantity, Decimal)
            or not self.position_quantity.is_finite()
            or self.position_quantity <= ZERO
        ):
            raise ValueError("exchange position quantity must be a positive Decimal")
        if not isinstance(self.direction, Direction):
            raise TypeError("exchange protection direction must be typed")
        if type(self.stop_order) is not AlgoOrderIntent:
            raise TypeError("exchange protection requires a concrete algo stop record")
        if (
            self.stop_order.symbol != self.symbol
            or self.stop_order.direction is not self.direction
            or self.stop_order.algo_type is not AlgoOrderType.STOP_MARKET
            or not self.stop_order.close_position
            or self.stop_order.quantity is not None
        ):
            raise ValueError("exchange stop evidence does not protect the observed position")
        timestamps = (self.position_observed_at_ms, self.protection_observed_at_ms)
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in timestamps
        ):
            raise ValueError("exchange protection timestamps must be non-negative integers")


@dataclass(frozen=True, slots=True)
class LiveProtectionEvidenceGate:
    """Pure Phase-14 prerequisite; it cannot activate or submit anything."""

    def require(self, evidence: object) -> ExchangeProtectionEvidence:
        if type(evidence) is not ExchangeProtectionEvidence:
            raise TypeError("live activation requires exchange protection evidence")
        return evidence


@dataclass(frozen=True, slots=True)
class UnsignedRequest:
    method: str
    path: str
    parameters: Mapping[str, str]


class RequestSigner(Protocol):
    """Future Phase 14 boundary; no implementation is included in Phase 8."""

    def sign(self, request: UnsignedRequest) -> str: ...


class ExchangeTransport(Protocol):
    """Future transport boundary; Phase 8 does not invoke it."""

    async def send(self, request: UnsignedRequest) -> Mapping[str, object]: ...


class ReconciliationReasonCode(StrEnum):
    MISSING_NORMAL_ORDER = "MISSING_NORMAL_ORDER"
    UNEXPECTED_NORMAL_ORDER = "UNEXPECTED_NORMAL_ORDER"
    MISSING_ALGO_ORDER = "MISSING_ALGO_ORDER"
    UNEXPECTED_ALGO_ORDER = "UNEXPECTED_ALGO_ORDER"
    POSITION_QUANTITY_MISMATCH = "POSITION_QUANTITY_MISMATCH"
    MISSING_EXPECTED_POSITION = "MISSING_EXPECTED_POSITION"
    UNEXPECTED_EXCHANGE_POSITION = "UNEXPECTED_EXCHANGE_POSITION"
    MISSING_STOP_PROTECTION = "MISSING_STOP_PROTECTION"
    UNRESOLVED_UNKNOWN_INTENT = "UNRESOLVED_UNKNOWN_INTENT"
    AUDIT_CHAIN_INVALID = "AUDIT_CHAIN_INVALID"
    REPLAY_INVALID = "REPLAY_INVALID"


@dataclass(frozen=True, slots=True)
class PositionAmount:
    symbol: str
    quantity: Decimal

    def __post_init__(self) -> None:
        _validate_position(self.symbol, self.quantity)


@dataclass(frozen=True, slots=True)
class PositionQuantityMismatch:
    symbol: str
    expected_quantity: Decimal
    exchange_quantity: Decimal

    def __post_init__(self) -> None:
        _validate_position(self.symbol, self.expected_quantity)
        _validate_position(self.symbol, self.exchange_quantity)
        if self.expected_quantity == self.exchange_quantity:
            raise ValueError("position mismatch quantities must differ")


def _validate_position(symbol: str, quantity: Decimal) -> None:
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("position symbol is required")
    if not isinstance(quantity, Decimal) or not quantity.is_finite():
        raise ValueError("position quantity must be a finite Decimal")


def _normalize_positions(positions_by_symbol: Mapping[str, Decimal]) -> dict[str, Decimal]:
    normalized: dict[str, Decimal] = {}
    for symbol, quantity in positions_by_symbol.items():
        _validate_position(symbol, quantity)
        normalized[symbol] = quantity
    return normalized


def _normalize_identifiers(values: frozenset[str], *, field_name: str) -> frozenset[str]:
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"{field_name} must contain non-empty string identifiers")
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    """Exchange-observed facts; this model has no transport or credential behavior."""

    positions_by_symbol: Mapping[str, Decimal]
    normal_order_client_ids: frozenset[str]
    algo_order_client_ids: frozenset[str]
    algo_orders: tuple[AlgoOrderIntent | AlgoOrderObservation, ...] = ()
    stop_protected_symbols: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "positions_by_symbol",
            _normalize_positions(self.positions_by_symbol),
        )
        object.__setattr__(
            self,
            "normal_order_client_ids",
            _normalize_identifiers(self.normal_order_client_ids, field_name="normal order IDs"),
        )
        supplied_algo_ids = _normalize_identifiers(
            self.algo_order_client_ids, field_name="algo order IDs"
        )
        if any(
            type(order) not in {AlgoOrderIntent, AlgoOrderObservation} for order in self.algo_orders
        ):
            raise TypeError("algo snapshot records must be typed Algo observations")
        observed_algo_ids = frozenset(order.client_algo_id for order in self.algo_orders)
        if len(observed_algo_ids) != len(self.algo_orders):
            raise ValueError("algo snapshot records must not duplicate client IDs")
        if supplied_algo_ids and supplied_algo_ids != observed_algo_ids:
            raise ValueError("algo order IDs must match concrete algo snapshot records")
        object.__setattr__(self, "algo_order_client_ids", observed_algo_ids)
        object.__setattr__(self, "algo_orders", tuple(self.algo_orders))
        object.__setattr__(
            self,
            "stop_protected_symbols",
            _normalize_identifiers(
                self.stop_protected_symbols,
                field_name="stop-protected symbols",
            ),
        )


@dataclass(frozen=True, slots=True)
class LocalReconciliationState:
    """Locally durable expectations that must agree with an exchange snapshot."""

    positions_by_symbol: Mapping[str, Decimal]
    normal_order_client_ids: frozenset[str]
    algo_order_client_ids: frozenset[str]
    required_stop_symbols: frozenset[str]
    unresolved_unknown_intent_ids: frozenset[str]
    audit_chain_valid: bool
    replay_valid: bool
    expected_stop_contracts: tuple[ExpectedStopContract, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.audit_chain_valid, bool) or not isinstance(self.replay_valid, bool):
            raise TypeError("audit and replay health must be boolean")
        normalized_positions = _normalize_positions(self.positions_by_symbol)
        required_stops = _normalize_identifiers(
            self.required_stop_symbols, field_name="required stop symbols"
        )
        nonzero_position_symbols = {
            symbol for symbol, quantity in normalized_positions.items() if quantity != ZERO
        }
        if not required_stops.issubset(nonzero_position_symbols):
            raise ValueError("required stops must correspond to nonzero expected positions")
        if any(
            type(contract) is not ExpectedStopContract for contract in self.expected_stop_contracts
        ):
            raise TypeError("expected stops must use complete typed contracts")
        contract_ids = [contract.client_algo_id for contract in self.expected_stop_contracts]
        if len(contract_ids) != len(set(contract_ids)):
            raise ValueError("expected stop contract IDs must be unique")
        contract_symbols = {contract.symbol for contract in self.expected_stop_contracts}
        if not contract_symbols.issubset(required_stops):
            raise ValueError("expected stop contracts must correspond to required symbols")
        if not set(contract_ids).issubset(self.algo_order_client_ids):
            raise ValueError("expected stop contract IDs must be durable expected algo IDs")
        object.__setattr__(self, "positions_by_symbol", normalized_positions)
        object.__setattr__(
            self,
            "normal_order_client_ids",
            _normalize_identifiers(self.normal_order_client_ids, field_name="normal order IDs"),
        )
        object.__setattr__(
            self,
            "algo_order_client_ids",
            _normalize_identifiers(self.algo_order_client_ids, field_name="algo order IDs"),
        )
        object.__setattr__(self, "required_stop_symbols", required_stops)
        object.__setattr__(
            self,
            "expected_stop_contracts",
            tuple(sorted(self.expected_stop_contracts, key=lambda item: item.client_algo_id)),
        )
        object.__setattr__(
            self,
            "unresolved_unknown_intent_ids",
            _normalize_identifiers(
                self.unresolved_unknown_intent_ids, field_name="unresolved UNKNOWN intent IDs"
            ),
        )


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    missing_normal_order_ids: tuple[str, ...]
    unexpected_normal_order_ids: tuple[str, ...]
    missing_algo_order_ids: tuple[str, ...]
    unexpected_algo_order_ids: tuple[str, ...]
    position_quantity_mismatches: tuple[PositionQuantityMismatch, ...]
    missing_expected_positions: tuple[PositionAmount, ...]
    unexpected_exchange_positions: tuple[PositionAmount, ...]
    missing_stop_symbols: tuple[str, ...]
    unresolved_unknown_intent_ids: tuple[str, ...]
    audit_chain_valid: bool
    replay_valid: bool
    invalid_stop_contract_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.audit_chain_valid, bool) or not isinstance(self.replay_valid, bool):
            raise TypeError("reconciliation audit and replay health must be boolean")

    @property
    def reason_codes(self) -> tuple[ReconciliationReasonCode, ...]:
        reasons: list[ReconciliationReasonCode] = []
        typed_fields = (
            (self.missing_normal_order_ids, ReconciliationReasonCode.MISSING_NORMAL_ORDER),
            (
                self.unexpected_normal_order_ids,
                ReconciliationReasonCode.UNEXPECTED_NORMAL_ORDER,
            ),
            (self.missing_algo_order_ids, ReconciliationReasonCode.MISSING_ALGO_ORDER),
            (self.unexpected_algo_order_ids, ReconciliationReasonCode.UNEXPECTED_ALGO_ORDER),
            (
                self.position_quantity_mismatches,
                ReconciliationReasonCode.POSITION_QUANTITY_MISMATCH,
            ),
            (
                self.missing_expected_positions,
                ReconciliationReasonCode.MISSING_EXPECTED_POSITION,
            ),
            (
                self.unexpected_exchange_positions,
                ReconciliationReasonCode.UNEXPECTED_EXCHANGE_POSITION,
            ),
            (self.missing_stop_symbols, ReconciliationReasonCode.MISSING_STOP_PROTECTION),
            (
                self.invalid_stop_contract_ids,
                ReconciliationReasonCode.MISSING_STOP_PROTECTION,
            ),
            (
                self.unresolved_unknown_intent_ids,
                ReconciliationReasonCode.UNRESOLVED_UNKNOWN_INTENT,
            ),
        )
        reasons.extend(reason for values, reason in typed_fields if values)
        if not self.audit_chain_valid:
            reasons.append(ReconciliationReasonCode.AUDIT_CHAIN_INVALID)
        if not self.replay_valid:
            reasons.append(ReconciliationReasonCode.REPLAY_INVALID)
        return tuple(reasons)

    @property
    def is_clean(self) -> bool:
        return not any(
            (
                self.missing_normal_order_ids,
                self.unexpected_normal_order_ids,
                self.missing_algo_order_ids,
                self.unexpected_algo_order_ids,
                self.position_quantity_mismatches,
                self.missing_expected_positions,
                self.unexpected_exchange_positions,
                self.missing_stop_symbols,
                self.invalid_stop_contract_ids,
                self.unresolved_unknown_intent_ids,
                not self.audit_chain_valid,
                not self.replay_valid,
            )
        )

    @property
    def missing_local_order_ids(self) -> tuple[str, ...]:
        """Compatibility projection; reconciliation itself never merges namespaces."""
        return tuple(sorted((*self.missing_normal_order_ids, *self.missing_algo_order_ids)))

    @property
    def unexpected_exchange_order_ids(self) -> tuple[str, ...]:
        """Compatibility projection; reconciliation itself never merges namespaces."""
        return tuple(sorted((*self.unexpected_normal_order_ids, *self.unexpected_algo_order_ids)))


def reconcile_local_state(
    *,
    local: LocalReconciliationState,
    snapshot: ReconciliationSnapshot,
) -> ReconciliationOutcome:
    """Compare every safety dimension independently; one mismatch makes the result dirty."""
    missing_normal = tuple(sorted(local.normal_order_client_ids - snapshot.normal_order_client_ids))
    unexpected_normal = tuple(
        sorted(snapshot.normal_order_client_ids - local.normal_order_client_ids)
    )
    missing_algo = tuple(sorted(local.algo_order_client_ids - snapshot.algo_order_client_ids))
    unexpected_algo = tuple(sorted(snapshot.algo_order_client_ids - local.algo_order_client_ids))

    mismatches: list[PositionQuantityMismatch] = []
    missing_positions: list[PositionAmount] = []
    unexpected_positions: list[PositionAmount] = []
    position_symbols = set(local.positions_by_symbol)
    position_symbols.update(snapshot.positions_by_symbol)
    for symbol in sorted(position_symbols):
        expected_quantity = local.positions_by_symbol.get(symbol, ZERO)
        exchange_quantity = snapshot.positions_by_symbol.get(symbol, ZERO)
        if expected_quantity == exchange_quantity:
            continue
        if expected_quantity == ZERO:
            unexpected_positions.append(PositionAmount(symbol=symbol, quantity=exchange_quantity))
        elif exchange_quantity == ZERO:
            missing_positions.append(PositionAmount(symbol=symbol, quantity=expected_quantity))
        else:
            mismatches.append(
                PositionQuantityMismatch(
                    symbol=symbol,
                    expected_quantity=expected_quantity,
                    exchange_quantity=exchange_quantity,
                )
            )

    observed_by_id = {order.client_algo_id: order for order in snapshot.algo_orders}
    invalid_stop_contract_ids = tuple(
        sorted(
            contract.client_algo_id
            for contract in local.expected_stop_contracts
            if not contract.matches(observed_by_id.get(contract.client_algo_id))
        )
    )
    invalid_stop_ids = set(invalid_stop_contract_ids)
    contracted_symbols = {contract.symbol for contract in local.expected_stop_contracts}
    missing_stops = tuple(
        sorted(
            (local.required_stop_symbols - contracted_symbols)
            | {
                contract.symbol
                for contract in local.expected_stop_contracts
                if contract.client_algo_id in invalid_stop_ids
            }
        )
    )
    unresolved_unknowns = tuple(sorted(local.unresolved_unknown_intent_ids))
    return ReconciliationOutcome(
        missing_normal_order_ids=missing_normal,
        unexpected_normal_order_ids=unexpected_normal,
        missing_algo_order_ids=missing_algo,
        unexpected_algo_order_ids=unexpected_algo,
        position_quantity_mismatches=tuple(mismatches),
        missing_expected_positions=tuple(missing_positions),
        unexpected_exchange_positions=tuple(unexpected_positions),
        missing_stop_symbols=missing_stops,
        unresolved_unknown_intent_ids=unresolved_unknowns,
        audit_chain_valid=local.audit_chain_valid,
        replay_valid=local.replay_valid,
        invalid_stop_contract_ids=invalid_stop_contract_ids,
    )


def reconcile_known_order_ids(
    *,
    local_normal_order_ids: frozenset[str],
    local_algo_order_ids: frozenset[str],
    snapshot: ReconciliationSnapshot,
) -> ReconciliationOutcome:
    """Legacy order-only entry point routed through the complete reconciliation contract."""
    return reconcile_local_state(
        local=LocalReconciliationState(
            positions_by_symbol={},
            normal_order_client_ids=local_normal_order_ids,
            algo_order_client_ids=local_algo_order_ids,
            required_stop_symbols=frozenset(),
            unresolved_unknown_intent_ids=frozenset(),
            audit_chain_valid=True,
            replay_valid=True,
        ),
        snapshot=snapshot,
    )
