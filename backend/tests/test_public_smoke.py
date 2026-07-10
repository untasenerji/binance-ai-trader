import pytest

from app.market.client import PublicMarketClient


@pytest.mark.anyio
@pytest.mark.live_public
async def test_binance_public_server_time_smoke() -> None:
    async with PublicMarketClient(timeout_seconds=10) as client:
        server_time = await client.get_server_time()

    assert server_time > 0
