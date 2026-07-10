from decimal import Decimal

import httpx
import pytest

from app.market.client import PublicMarketClient


@pytest.mark.anyio
async def test_public_client_parses_documented_market_payloads_without_auth_headers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1_700_000_000_000})
        if request.url.path == "/fapi/v1/klines":
            return httpx.Response(
                200,
                json=[
                    [
                        1_700_000_000_000,
                        "100.00",
                        "101.00",
                        "99.00",
                        "100.50",
                        "12.50",
                        1_700_000_059_999,
                        "1256.25",
                        12,
                        "6.00",
                        "603.00",
                        "0",
                    ]
                ],
            )
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "markPrice": "100.50",
                    "indexPrice": "100.40",
                    "lastFundingRate": "0.00010000",
                    "nextFundingTime": 1_700_028_800_000,
                    "time": 1_700_000_000_010,
                },
            )
        if request.url.path == "/fapi/v1/ticker/bookTicker":
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "bidPrice": "100.40",
                    "bidQty": "5.0",
                    "askPrice": "100.50",
                    "askQty": "4.0",
                    "time": 1_700_000_000_020,
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    async with httpx.AsyncClient(
        base_url="https://fapi.binance.com",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = PublicMarketClient(http_client)
        assert await client.get_server_time() == 1_700_000_000_000
        klines = await client.get_klines(symbol="BTCUSDT", interval="1m", limit=1)
        mark_price = await client.get_mark_price(symbol="BTCUSDT")
        book_ticker = await client.get_book_ticker(symbol="BTCUSDT")

    assert klines[0].close_price == Decimal("100.50")
    assert mark_price.funding_rate == Decimal("0.00010000")
    assert book_ticker.ask_price == Decimal("100.50")
    assert all("X-MBX-APIKEY" not in request.headers for request in requests)
    assert all("signature" not in request.url.query.decode("utf-8") for request in requests)


@pytest.mark.anyio
async def test_public_client_rejects_invalid_kline_shape() -> None:
    async with httpx.AsyncClient(
        base_url="https://fapi.binance.com",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[[1, "only"]])),
    ) as http_client:
        client = PublicMarketClient(http_client)
        with pytest.raises(ValueError, match="12 fields"):
            await client.get_klines(symbol="BTCUSDT", interval="1m", limit=1)
