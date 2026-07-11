"""Typed normal/algo order and reconciliation contracts with no secret fields."""

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


@dataclass(frozen=True, slots=True)
class NormalOrderIntent:
    client_order_id: str
    symbol: str
    direction: Direction
    order_type: NormalOrderType
    quantity: Decimal
    price: Decimal
    reduce_only: bool = False

    def __post_init__(self) -> None:
        if not self.client_order_id or not self.symbol:
            raise ValueError("client_order_id and symbol are required")
        if self.quantity <= ZERO or self.price <= ZERO:
            raise ValueError("normal order quantity and price must be positive")


@dataclass(frozen=True, slots=True)
class AlgoOrderIntent:
    client_algo_id: str
    symbol: str
    direction: Direction
    algo_type: AlgoOrderType
    trigger_price: Decimal
    close_position: bool

    def __post_init__(self) -> None:
        if not self.client_algo_id or not self.symbol or self.trigger_price <= ZERO:
            raise ValueError("algo order fields are invalid")
        if self.algo_type is AlgoOrderType.STOP_MARKET and not self.close_position:
            raise ValueError("protective STOP_MARKET must close the full position")


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


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    positions_by_symbol: Mapping[str, Decimal]
    normal_order_client_ids: frozenset[str]
    algo_order_client_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    is_clean: bool
    missing_local_order_ids: tuple[str, ...]
    unexpected_exchange_order_ids: tuple[str, ...]


def reconcile_known_order_ids(
    *,
    local_normal_order_ids: frozenset[str],
    local_algo_order_ids: frozenset[str],
    snapshot: ReconciliationSnapshot,
) -> ReconciliationOutcome:
    local_ids = local_normal_order_ids | local_algo_order_ids
    exchange_ids = snapshot.normal_order_client_ids | snapshot.algo_order_client_ids
    missing_local = tuple(sorted(local_ids - exchange_ids))
    unexpected_exchange = tuple(sorted(exchange_ids - local_ids))
    return ReconciliationOutcome(
        is_clean=not missing_local and not unexpected_exchange,
        missing_local_order_ids=missing_local,
        unexpected_exchange_order_ids=unexpected_exchange,
    )
