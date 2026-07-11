"""Local control-plane status stream with no exchange or credential dependency."""

import asyncio
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict

router = APIRouter(tags=["control-plane"])


class ControlPlaneSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: Literal[13]
    generated_at_utc: datetime
    live_trading_enabled: Literal[False]
    execution_status: Literal["locked"]
    market_data_status: Literal["shadow_only"]
    user_stream_status: Literal["locked"]
    database_status: Literal["local_ready"]
    ai_status: Literal["model_unavailable"]


def control_plane_snapshot() -> ControlPlaneSnapshot:
    return ControlPlaneSnapshot(
        phase=13,
        generated_at_utc=datetime.now(UTC),
        live_trading_enabled=False,
        execution_status="locked",
        market_data_status="shadow_only",
        user_stream_status="locked",
        database_status="local_ready",
        ai_status="model_unavailable",
    )


@router.websocket("/ws/control-plane")
async def stream_control_plane(websocket: WebSocket) -> None:
    """Push local status only; this is not a Binance stream or execution channel."""
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(control_plane_snapshot().model_dump(mode="json"))
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        return
