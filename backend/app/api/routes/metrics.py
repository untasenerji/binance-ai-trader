"""Local safe metrics endpoint with no account or credential labels."""

from decimal import Decimal

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.observability.metrics import runtime_metrics

router = APIRouter(tags=["observability"])


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
def get_metrics() -> PlainTextResponse:
    runtime_metrics.set_gauge("uta_live_trading_enabled", Decimal("0"))
    runtime_metrics.set_gauge("uta_control_plane_phase", Decimal("13"))
    return PlainTextResponse(runtime_metrics.render_prometheus())
