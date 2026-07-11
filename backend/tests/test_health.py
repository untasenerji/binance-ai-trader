import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_health_endpoint_is_safe_and_available() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "binance-ai-trader",
        "phase": 13,
        "live_trading_enabled": False,
    }


def test_control_plane_stream_only_exposes_locked_local_status() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/api/ws/control-plane") as websocket:
            snapshot = websocket.receive_json()

    assert snapshot["phase"] == 13
    assert snapshot["live_trading_enabled"] is False
    assert snapshot["execution_status"] == "locked"
    assert snapshot["market_data_status"] == "shadow_only"
    assert snapshot["user_stream_status"] == "locked"
    assert snapshot["ai_status"] == "model_unavailable"


@pytest.mark.anyio
async def test_metrics_endpoint_contains_only_safe_local_metrics() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/metrics")

    assert response.status_code == 200
    assert "uta_live_trading_enabled 0" in response.text
    assert "uta_control_plane_phase 13" in response.text


@pytest.mark.anyio
async def test_risk_config_preview_is_read_only_and_uses_backend_hard_caps() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/risk-config-preview")

    assert response.status_code == 200
    assert response.json() == {
        "phase": 13,
        "profile": "live_pilot_20_usdt",
        "authority": "backend_hard_cap",
        "live_trading_enabled": False,
        "pilot_equity_cap_usdt": "20",
        "margin_type": "ISOLATED",
        "position_mode": "ONE_WAY",
        "max_leverage": 2,
        "max_concurrent_positions": 1,
        "max_active_strategy_count": 1,
        "max_stages": 2,
        "risk_per_trade_usdt": "0.1",
        "daily_loss_limit_usdt": "0.3",
        "weekly_drawdown_limit_usdt": "0.8",
        "consecutive_loss_limit": 3,
        "allow_market_entry": False,
        "allow_back_loaded_ladder": False,
        "require_server_side_stop": True,
        "openai_mode": "advisory",
        "openai_daily_budget_usd": "0.02",
    }
