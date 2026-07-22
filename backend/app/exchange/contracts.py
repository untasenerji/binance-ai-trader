"""Typed normal/algo order and fail-closed reconciliation contracts."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, cast, final

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


class AdapterQueryReceiptStatus(StrEnum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


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
class ExchangeAlgoOrderObservation:
    """Fresh adapter-attested Algo state; local order intents cannot impersonate it.

    This is a data-only Phase-13 contract. No transport, signer, credential, or
    exchange request implementation is supplied by this type.
    """

    source: str
    account_id: str
    fetched_at: datetime
    server_time: datetime
    freshness_window: timedelta
    correlation_id: str
    query_epoch: int
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
        if self.source != "authenticated_exchange_adapter":
            raise ValueError("exchange observations require authenticated adapter provenance")
        if not self.account_id or not self.correlation_id:
            raise ValueError("exchange observations require account and query correlation IDs")
        if (
            not isinstance(self.query_epoch, int)
            or isinstance(self.query_epoch, bool)
            or self.query_epoch < 1
        ):
            raise ValueError("exchange observations require a positive query epoch")
        if self.fetched_at.tzinfo is None or self.server_time.tzinfo is None:
            raise ValueError("exchange observation timestamps must be timezone-aware")
        if (
            not isinstance(self.freshness_window, timedelta)
            or self.freshness_window <= timedelta(0)
            or self.freshness_window > timedelta(minutes=10)
        ):
            raise ValueError("exchange observation freshness window is invalid")
        fetched_at = self.fetched_at.astimezone(UTC)
        server_time = self.server_time.astimezone(UTC)
        now = datetime.now(UTC)
        if fetched_at > now:
            raise ValueError("exchange observation fetch time cannot be in the future")
        if server_time > fetched_at + timedelta(seconds=5):
            raise ValueError("exchange observation server time cannot follow fetch time")
        if server_time < fetched_at - timedelta(seconds=5):
            raise ValueError("exchange observation server time is too old")
        if not self.is_fresh():
            raise ValueError("exchange observation is not fresh")
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
            raise ValueError("exchange-observed Algo fields are invalid")
        if self.quantity is not None and (
            not isinstance(self.quantity, Decimal)
            or not self.quantity.is_finite()
            or self.quantity <= ZERO
        ):
            raise ValueError("exchange-observed Algo quantity must be a positive Decimal")
        for version in (self.policy_version, self.account_envelope_version):
            if version is not None and (
                not isinstance(version, int) or isinstance(version, bool) or version < 1
            ):
                raise ValueError("exchange-observed Algo policy versions are invalid")
        for fingerprint in (
            self.policy_fingerprint,
            self.account_envelope_fingerprint,
            self.stop_contract_fingerprint,
        ):
            if fingerprint is not None and (
                not isinstance(fingerprint, str) or len(fingerprint) != 64
            ):
                raise ValueError("exchange-observed Algo fingerprints are invalid")

    @property
    def side(self) -> OrderSide:
        return OrderSide.SELL if self.direction is Direction.LONG else OrderSide.BUY

    def is_fresh(self, now: datetime | None = None) -> bool:
        """Recheck the bounded observation lifetime at every evidence boundary."""
        checked_at = (now or datetime.now(UTC)).astimezone(UTC)
        fetched_at = self.fetched_at.astimezone(UTC)
        return fetched_at <= checked_at and checked_at - fetched_at <= self.freshness_window

    def canonical_record(self) -> dict[str, object]:
        return {
            "source": self.source,
            "account_id": self.account_id,
            "fetched_at": self.fetched_at,
            "server_time": self.server_time,
            "freshness_window": self.freshness_window,
            "correlation_id": self.correlation_id,
            "query_epoch": self.query_epoch,
            "client_algo_id": self.client_algo_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "algo_type": self.algo_type,
            "trigger_price": self.trigger_price,
            "close_position": self.close_position,
            "quantity": self.quantity,
            "working_type": self.working_type,
            "status": self.status,
            "plan_id": self.plan_id,
            "policy_version": self.policy_version,
            "account_envelope_version": self.account_envelope_version,
            "policy_fingerprint": self.policy_fingerprint,
            "account_envelope_fingerprint": self.account_envelope_fingerprint,
            "stop_contract_fingerprint": self.stop_contract_fingerprint,
        }


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
    account_id: str = "v1-primary"

    def __post_init__(self) -> None:
        if not self.account_id or not self.plan_id or not self.symbol or not self.client_algo_id:
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
                "account_id": self.account_id,
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
            type(observed) is ExchangeAlgoOrderObservation
            and observed.source == "authenticated_exchange_adapter"
            and observed.is_fresh()
            and observed.account_id == self.account_id
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
    stop_order: ExchangeAlgoOrderObservation
    reduce_only_exit_reference: str
    position_observed_at_ms: int
    protection_observed_at_ms: int
    expected_stop_contract: ExpectedStopContract | None = None

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
        if type(self.stop_order) is not ExchangeAlgoOrderObservation:
            raise TypeError("exchange protection requires an exchange Algo observation")
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
        if type(self.expected_stop_contract) is not ExpectedStopContract:
            raise TypeError("exchange protection requires a durable expected stop contract")
        if (
            self.expected_stop_contract.plan_id != self.plan_id
            or self.expected_stop_contract.symbol != self.symbol
            or self.expected_stop_contract.position_side is not self.direction
            or not self.expected_stop_contract.matches(self.stop_order)
        ):
            raise ValueError("exchange protection does not match its durable expected stop")
        if self.protection_observed_at_ms < self.position_observed_at_ms:
            raise ValueError("exchange protection must not predate the observed position")


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
    algo_orders: tuple[ExchangeAlgoOrderObservation, ...] = ()
    stop_protected_symbols: frozenset[str] = frozenset()
    account_id: str = "v1-primary"

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
        if not self.account_id:
            raise ValueError("reconciliation snapshots require an account ID")
        if any(type(order) is not ExchangeAlgoOrderObservation for order in self.algo_orders):
            raise TypeError("algo snapshot records must be ExchangeAlgoOrderObservation values")
        if any(order.account_id != self.account_id for order in self.algo_orders):
            raise ValueError("exchange observations must match the snapshot account")
        correlations = {order.correlation_id for order in self.algo_orders}
        if len(correlations) > 1:
            raise ValueError("algo observations must share one query correlation")
        query_epochs = {order.query_epoch for order in self.algo_orders}
        if len(query_epochs) > 1:
            raise ValueError("algo observations must share one query epoch")
        observed_algo_ids = frozenset(order.client_algo_id for order in self.algo_orders)
        if len(observed_algo_ids) != len(self.algo_orders):
            raise ValueError("algo snapshot records must not duplicate client IDs")
        if supplied_algo_ids and supplied_algo_ids != observed_algo_ids:
            raise ValueError("algo order IDs must match concrete algo snapshot records")
        object.__setattr__(self, "algo_order_client_ids", observed_algo_ids)
        object.__setattr__(self, "algo_orders", tuple(self.algo_orders))
        normalized_protected_symbols = _normalize_identifiers(
            self.stop_protected_symbols,
            field_name="stop-protected symbols",
        )
        active_stop_symbols = {
            order.symbol
            for order in self.algo_orders
            if order.algo_type is AlgoOrderType.STOP_MARKET
            and order.close_position
            and order.status is AlgoOrderStatus.NEW
        }
        if not normalized_protected_symbols <= active_stop_symbols:
            raise ValueError("stop-protected symbols require fresh exchange stop observations")
        object.__setattr__(
            self,
            "stop_protected_symbols",
            normalized_protected_symbols,
        )


@final
@dataclass(frozen=True, slots=True)
class AdapterQueryReceipt:
    """Typed adapter query evidence that must also exist in durable storage."""

    receipt_id: str
    account_id: str
    adapter_instance_id: str
    query_id: str
    correlation_id: str
    query_epoch: int
    requested_at: datetime
    completed_at: datetime | None
    server_time: datetime | None
    query_type: str
    response_fingerprint: str | None
    status: AdapterQueryReceiptStatus
    receipt_fingerprint: str = ""

    def __post_init__(self) -> None:
        if not all(
            (
                self.receipt_id,
                self.account_id,
                self.adapter_instance_id,
                self.query_id,
                self.correlation_id,
                self.query_type,
            )
        ):
            raise ValueError("adapter query receipt identity is incomplete")
        if (
            not isinstance(self.query_epoch, int)
            or isinstance(self.query_epoch, bool)
            or self.query_epoch < 1
        ):
            raise ValueError("adapter query receipt query epoch is invalid")
        if self.requested_at.tzinfo is None:
            raise ValueError("adapter query receipt request time must be timezone-aware")
        requested_at = self.requested_at.astimezone(UTC)
        completed_at = None if self.completed_at is None else self.completed_at.astimezone(UTC)
        server_time = None if self.server_time is None else self.server_time.astimezone(UTC)
        if self.status is AdapterQueryReceiptStatus.COMPLETED:
            if completed_at is None or server_time is None or not self.response_fingerprint:
                raise ValueError("completed adapter query receipt is incomplete")
            if completed_at < requested_at:
                raise ValueError("adapter query receipt completion predates request")
        elif (
            completed_at is not None
            or server_time is not None
            or self.response_fingerprint is not None
        ):
            raise ValueError("non-completed adapter query receipt carries response evidence")
        if self.response_fingerprint is not None and (
            len(self.response_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in self.response_fingerprint.lower())
        ):
            raise ValueError("adapter query response fingerprint is invalid")
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "server_time", server_time)
        expected = self.expected_fingerprint()
        if self.receipt_fingerprint and self.receipt_fingerprint != expected:
            raise ValueError("adapter query receipt fingerprint is invalid")
        object.__setattr__(self, "receipt_fingerprint", expected)

    def expected_fingerprint(self) -> str:
        canonical = json.dumps(
            {
                "account_id": self.account_id,
                "adapter_instance_id": self.adapter_instance_id,
                "completed_at": None
                if self.completed_at is None
                else self.completed_at.isoformat(),
                "correlation_id": self.correlation_id,
                "query_epoch": self.query_epoch,
                "query_id": self.query_id,
                "query_type": self.query_type,
                "receipt_id": self.receipt_id,
                "requested_at": self.requested_at.isoformat(),
                "response_fingerprint": self.response_fingerprint,
                "server_time": None if self.server_time is None else self.server_time.isoformat(),
                "status": self.status.value,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def require_completed(self) -> None:
        if self.status is not AdapterQueryReceiptStatus.COMPLETED:
            raise ValueError("adapter query receipt is not complete")

    def started_record(self) -> "AdapterQueryReceipt":
        """Return the durable pre-query receipt for this completed adapter result."""
        return AdapterQueryReceipt(
            receipt_id=self.receipt_id,
            account_id=self.account_id,
            adapter_instance_id=self.adapter_instance_id,
            query_id=self.query_id,
            correlation_id=self.correlation_id,
            query_epoch=self.query_epoch,
            requested_at=self.requested_at,
            completed_at=None,
            server_time=None,
            query_type=self.query_type,
            response_fingerprint=None,
            status=AdapterQueryReceiptStatus.STARTED,
        )


@final
@dataclass(frozen=True, slots=True)
class ExchangeReconciliationObservationBatch:
    """One bounded, adapter-attested exchange reconciliation query result.

    The type is deliberately data-only. Exact-type checks at authorization
    boundaries prevent local snapshots and simulator objects from being used as
    exchange evidence.
    """

    source: str
    account_id: str
    query_epoch: int
    correlation_id: str
    requested_at: datetime
    fetched_at: datetime
    server_time: datetime
    max_age: timedelta
    max_clock_skew: timedelta
    positions_by_symbol: Mapping[str, Decimal]
    normal_order_client_ids: frozenset[str]
    algo_order_client_ids: frozenset[str]
    algo_orders: tuple[ExchangeAlgoOrderObservation, ...] = ()
    query_receipt: AdapterQueryReceipt | None = None
    response_fingerprint: str = field(init=False)
    observation_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if self.source != "authenticated_exchange_adapter":
            raise ValueError("reconciliation batches require authenticated adapter provenance")
        if not self.account_id or not self.correlation_id:
            raise ValueError("reconciliation batches require account and correlation IDs")
        if (
            not isinstance(self.query_epoch, int)
            or isinstance(self.query_epoch, bool)
            or self.query_epoch < 1
        ):
            raise ValueError("reconciliation batches require a positive query epoch")
        timestamps = (self.requested_at, self.fetched_at, self.server_time)
        if any(value.tzinfo is None for value in timestamps):
            raise ValueError("reconciliation batch timestamps must be timezone-aware")
        if (
            not isinstance(self.max_age, timedelta)
            or self.max_age <= timedelta(0)
            or self.max_age > timedelta(minutes=10)
        ):
            raise ValueError("reconciliation batch max age is invalid")
        if (
            not isinstance(self.max_clock_skew, timedelta)
            or self.max_clock_skew <= timedelta(0)
            or self.max_clock_skew > timedelta(minutes=1)
        ):
            raise ValueError("reconciliation batch clock skew is invalid")
        requested_at = self.requested_at.astimezone(UTC)
        fetched_at = self.fetched_at.astimezone(UTC)
        server_time = self.server_time.astimezone(UTC)
        if requested_at > fetched_at:
            raise ValueError("reconciliation request time cannot follow fetch time")
        if fetched_at - requested_at > self.max_age:
            raise ValueError("reconciliation query duration exceeds its bounded age")
        if abs(fetched_at - server_time) > self.max_clock_skew:
            raise ValueError("reconciliation server time exceeds the allowed clock skew")

        normalized_positions = MappingProxyType(_normalize_positions(self.positions_by_symbol))
        normal_ids = _normalize_identifiers(
            self.normal_order_client_ids,
            field_name="normal order IDs",
        )
        supplied_algo_ids = _normalize_identifiers(
            self.algo_order_client_ids,
            field_name="algo order IDs",
        )
        if any(type(order) is not ExchangeAlgoOrderObservation for order in self.algo_orders):
            raise TypeError("reconciliation Algo records require exact exchange observations")
        orders = tuple(sorted(self.algo_orders, key=lambda item: item.client_algo_id))
        observed_algo_ids = frozenset(order.client_algo_id for order in orders)
        if len(observed_algo_ids) != len(orders):
            raise ValueError("reconciliation Algo records must not duplicate client IDs")
        if supplied_algo_ids != observed_algo_ids:
            raise ValueError("algo order IDs must match concrete batch observations")
        for order in orders:
            if order.account_id != self.account_id:
                raise ValueError("reconciliation observation account mismatch")
            if order.source != self.source:
                raise ValueError("reconciliation observation source mismatch")
            if order.query_epoch != self.query_epoch:
                raise ValueError("reconciliation observation query epoch mismatch")
            if order.correlation_id != self.correlation_id:
                raise ValueError("reconciliation observation correlation mismatch")
            if order.fetched_at.astimezone(UTC) != fetched_at:
                raise ValueError("reconciliation observation fetch time mismatch")
            if order.server_time.astimezone(UTC) != server_time:
                raise ValueError("reconciliation observation server time mismatch")
            if order.freshness_window != self.max_age:
                raise ValueError("reconciliation observation age bound mismatch")

        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "fetched_at", fetched_at)
        object.__setattr__(self, "server_time", server_time)
        object.__setattr__(self, "positions_by_symbol", normalized_positions)
        object.__setattr__(self, "normal_order_client_ids", normal_ids)
        object.__setattr__(self, "algo_order_client_ids", observed_algo_ids)
        object.__setattr__(self, "algo_orders", orders)
        self.require_fresh()
        response_fingerprint = hashlib.sha256(self._response_canonical_json().encode()).hexdigest()
        receipt = self.query_receipt
        if receipt is not None:
            if type(receipt) is not AdapterQueryReceipt:
                raise TypeError("reconciliation batch receipt must be an exact adapter receipt")
            receipt.require_completed()
            if (
                receipt.account_id != self.account_id
                or receipt.correlation_id != self.correlation_id
                or receipt.query_epoch != self.query_epoch
                or receipt.requested_at != requested_at
                or receipt.completed_at != fetched_at
                or receipt.server_time != server_time
                or receipt.response_fingerprint != response_fingerprint
            ):
                raise ValueError("reconciliation batch does not match its adapter query receipt")
        object.__setattr__(self, "response_fingerprint", response_fingerprint)
        object.__setattr__(
            self,
            "observation_fingerprint",
            hashlib.sha256(self._canonical_json().encode()).hexdigest(),
        )

    @property
    def snapshot(self) -> ReconciliationSnapshot:
        active_stop_symbols = frozenset(
            order.symbol
            for order in self.algo_orders
            if order.algo_type is AlgoOrderType.STOP_MARKET
            and order.close_position
            and order.status is AlgoOrderStatus.NEW
        )
        return ReconciliationSnapshot(
            account_id=self.account_id,
            positions_by_symbol=self.positions_by_symbol,
            normal_order_client_ids=self.normal_order_client_ids,
            algo_order_client_ids=self.algo_order_client_ids,
            algo_orders=self.algo_orders,
            stop_protected_symbols=active_stop_symbols,
        )

    def require_fresh(self, now: datetime | None = None) -> None:
        checked_at = (now or datetime.now(UTC)).astimezone(UTC)
        if self.fetched_at > checked_at:
            raise ValueError("reconciliation batch fetch time cannot be in the future")
        if checked_at - self.fetched_at > self.max_age:
            raise ValueError("reconciliation batch is stale")
        if abs(self.fetched_at - self.server_time) > self.max_clock_skew:
            raise ValueError("reconciliation server time exceeds the allowed clock skew")
        if any(not order.is_fresh(checked_at) for order in self.algo_orders):
            raise ValueError("reconciliation batch contains a stale Algo observation")

    def canonical_record(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self._canonical_json()))

    def _canonical_json(self) -> str:
        payload = self._response_payload()
        receipt = self.query_receipt
        payload["query_receipt"] = (
            None
            if receipt is None
            else {
                "adapter_instance_id": receipt.adapter_instance_id,
                "query_id": receipt.query_id,
                "receipt_fingerprint": receipt.receipt_fingerprint,
                "receipt_id": receipt.receipt_id,
                "status": receipt.status.value,
            }
        )
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)

    def _response_canonical_json(self) -> str:
        return json.dumps(
            self._response_payload(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    def _response_payload(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "algo_order_client_ids": sorted(self.algo_order_client_ids),
            "algo_orders": [
                {
                    "account_envelope_fingerprint": order.account_envelope_fingerprint,
                    "account_envelope_version": order.account_envelope_version,
                    "algo_type": order.algo_type.value,
                    "client_algo_id": order.client_algo_id,
                    "close_position": order.close_position,
                    "direction": order.direction.value,
                    "plan_id": order.plan_id,
                    "policy_fingerprint": order.policy_fingerprint,
                    "policy_version": order.policy_version,
                    "quantity": (None if order.quantity is None else format(order.quantity, "f")),
                    "status": order.status.value,
                    "stop_contract_fingerprint": order.stop_contract_fingerprint,
                    "symbol": order.symbol,
                    "trigger_price": format(order.trigger_price, "f"),
                    "working_type": order.working_type.value,
                }
                for order in self.algo_orders
            ],
            "correlation_id": self.correlation_id,
            "fetched_at": self.fetched_at.isoformat(),
            "max_age_ms": int(self.max_age.total_seconds() * 1000),
            "max_clock_skew_ms": int(self.max_clock_skew.total_seconds() * 1000),
            "normal_order_client_ids": sorted(self.normal_order_client_ids),
            "positions_by_symbol": {
                symbol: format(quantity, "f")
                for symbol, quantity in sorted(self.positions_by_symbol.items())
            },
            "query_epoch": self.query_epoch,
            "requested_at": self.requested_at.isoformat(),
            "server_time": self.server_time.isoformat(),
            "source": self.source,
        }


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
