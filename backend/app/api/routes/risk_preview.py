"""Read-only D-026 pilot profile preview with no configuration mutation path."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.domain.decimal_math import decimal_to_wire
from app.domain.risk import DEFAULT_HARD_CAPS

router = APIRouter(tags=["risk"])


class RiskConfigPreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: Literal[13]
    profile: Literal["live_pilot_20_usdt"]
    authority: Literal["backend_hard_cap"]
    live_trading_enabled: Literal[False]
    pilot_equity_cap_usdt: str
    margin_type: Literal["ISOLATED"]
    position_mode: Literal["ONE_WAY"]
    max_leverage: int
    max_concurrent_positions: int
    max_active_strategy_count: int
    max_stages: int
    risk_per_trade_usdt: str
    daily_loss_limit_usdt: str
    weekly_drawdown_limit_usdt: str
    consecutive_loss_limit: int
    allow_market_entry: Literal[False]
    allow_back_loaded_ladder: Literal[False]
    require_server_side_stop: Literal[True]
    openai_mode: Literal["advisory"]
    openai_daily_budget_usd: str


@router.get("/risk-config-preview", response_model=RiskConfigPreviewResponse)
def get_risk_config_preview() -> RiskConfigPreviewResponse:
    """Expose fixed safety limits for display; callers cannot submit configuration."""
    caps = DEFAULT_HARD_CAPS
    return RiskConfigPreviewResponse(
        phase=13,
        profile="live_pilot_20_usdt",
        authority="backend_hard_cap",
        live_trading_enabled=False,
        pilot_equity_cap_usdt=decimal_to_wire(caps.pilot_equity_cap_usdt),
        margin_type="ISOLATED",
        position_mode="ONE_WAY",
        max_leverage=caps.max_leverage,
        max_concurrent_positions=caps.max_concurrent_positions,
        max_active_strategy_count=caps.max_active_strategy_count,
        max_stages=caps.max_stages,
        risk_per_trade_usdt=decimal_to_wire(caps.risk_per_trade_usdt),
        daily_loss_limit_usdt=decimal_to_wire(caps.daily_loss_limit_usdt),
        weekly_drawdown_limit_usdt=decimal_to_wire(caps.weekly_drawdown_limit_usdt),
        consecutive_loss_limit=caps.consecutive_loss_limit,
        allow_market_entry=False,
        allow_back_loaded_ladder=False,
        require_server_side_stop=True,
        openai_mode="advisory",
        openai_daily_budget_usd=decimal_to_wire(caps.openai_daily_budget_usd),
    )
