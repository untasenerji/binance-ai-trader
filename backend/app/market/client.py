"""Credential-free Binance USD-M Futures REST market-data client."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.domain.decimal_math import to_decimal

BINANCE_FUTURES_REST_URL = "https://fapi.binance.com"


class MarketPayloadError(ValueError):
    """Raised when a public endpoint does not match its documented payload shape."""


@dataclass(frozen=True, slots=True)
class Kline:
    symbol: str
    interval: str
    open_time_ms: int
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal
    close_time_ms: int
    quote_volume: Decimal
    trade_count: int


@dataclass(frozen=True, slots=True)
class MarkPrice:
    symbol: str
    mark_price: Decimal
    index_price: Decimal
    funding_rate: Decimal
    next_funding_time_ms: int
    observed_at_ms: int


@dataclass(frozen=True, slots=True)
class BookTicker:
    symbol: str
    bid_price: Decimal
    bid_quantity: Decimal
    ask_price: Decimal
    ask_quantity: Decimal
    observed_at_ms: int


class PublicMarketClient:
    """A deliberately narrow, GET-only client with no authentication surface."""

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        *,
        timeout_seconds: int = 10,
    ) -> None:
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=BINANCE_FUTURES_REST_URL,
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Accept": "application/json"},
        )

    async def __aenter__(self) -> "PublicMarketClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_server_time(self) -> int:
        payload = await self._get_object("/fapi/v1/time")
        return _require_int(payload, "serverTime")

    async def get_exchange_info(self) -> Mapping[str, object]:
        return await self._get_object("/fapi/v1/exchangeInfo")

    async def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int = 500,
    ) -> tuple[Kline, ...]:
        if not 1 <= limit <= 1500:
            raise ValueError("limit must be between 1 and 1500")

        payload = await self._get_array(
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": str(limit)},
        )
        return tuple(_parse_kline(item, symbol=symbol, interval=interval) for item in payload)

    async def get_mark_price(self, *, symbol: str) -> MarkPrice:
        payload = await self._get_object("/fapi/v1/premiumIndex", params={"symbol": symbol})
        return MarkPrice(
            symbol=_require_string(payload, "symbol"),
            mark_price=to_decimal(_require_string(payload, "markPrice"), field="markPrice"),
            index_price=to_decimal(_require_string(payload, "indexPrice"), field="indexPrice"),
            funding_rate=to_decimal(
                _require_string(payload, "lastFundingRate"), field="lastFundingRate"
            ),
            next_funding_time_ms=_require_int(payload, "nextFundingTime"),
            observed_at_ms=_require_int(payload, "time"),
        )

    async def get_book_ticker(self, *, symbol: str) -> BookTicker:
        payload = await self._get_object("/fapi/v1/ticker/bookTicker", params={"symbol": symbol})
        return BookTicker(
            symbol=_require_string(payload, "symbol"),
            bid_price=to_decimal(_require_string(payload, "bidPrice"), field="bidPrice"),
            bid_quantity=to_decimal(_require_string(payload, "bidQty"), field="bidQty"),
            ask_price=to_decimal(_require_string(payload, "askPrice"), field="askPrice"),
            ask_quantity=to_decimal(_require_string(payload, "askQty"), field="askQty"),
            observed_at_ms=_require_int(payload, "time"),
        )

    async def _get_object(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> Mapping[str, object]:
        return _require_object(await self._get(path, params=params))

    async def _get_array(
        self,
        path: str,
        *,
        params: Mapping[str, str],
    ) -> Sequence[object]:
        payload = await self._get(path, params=params)
        if not isinstance(payload, list):
            raise MarketPayloadError("expected an array payload")
        return payload

    async def _get(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> object:
        response = await self._client.get(path, params=params)
        response.raise_for_status()
        return response.json()


def _parse_kline(value: object, *, symbol: str, interval: str) -> Kline:
    if not isinstance(value, list) or len(value) != 12:
        raise MarketPayloadError("kline payload must contain exactly 12 fields")

    return Kline(
        symbol=symbol,
        interval=interval,
        open_time_ms=_require_kline_int(value, 0),
        open_price=to_decimal(_require_kline_string(value, 1), field="openPrice"),
        high_price=to_decimal(_require_kline_string(value, 2), field="highPrice"),
        low_price=to_decimal(_require_kline_string(value, 3), field="lowPrice"),
        close_price=to_decimal(_require_kline_string(value, 4), field="closePrice"),
        volume=to_decimal(_require_kline_string(value, 5), field="volume"),
        close_time_ms=_require_kline_int(value, 6),
        quote_volume=to_decimal(_require_kline_string(value, 7), field="quoteVolume"),
        trade_count=_require_kline_int(value, 8),
    )


def _require_object(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise MarketPayloadError("expected an object payload")
    return value


def _require_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise MarketPayloadError(f"{field} must be a string")
    return value


def _require_int(payload: Mapping[str, object], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise MarketPayloadError(f"{field} must be an integer")
    return value


def _require_kline_string(payload: Sequence[object], index: int) -> str:
    value = payload[index]
    if not isinstance(value, str):
        raise MarketPayloadError(f"kline field {index} must be a string")
    return value


def _require_kline_int(payload: Sequence[object], index: int) -> int:
    value = payload[index]
    if not isinstance(value, int) or isinstance(value, bool):
        raise MarketPayloadError(f"kline field {index} must be an integer")
    return value
