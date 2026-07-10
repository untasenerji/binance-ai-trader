from dataclasses import replace
from decimal import Decimal

import pytest

from app.domain.risk import DEFAULT_HARD_CAPS, HardRiskLimitExceeded, RiskSettings, apply_hard_caps


@pytest.fixture
def safe_settings() -> RiskSettings:
    return RiskSettings(
        pilot_equity_cap_usdt=Decimal("20"),
        max_leverage=2,
        max_concurrent_positions=1,
        max_active_strategy_count=1,
        max_stages=2,
        risk_per_trade_usdt=Decimal("0.10"),
        daily_loss_limit_usdt=Decimal("0.30"),
        weekly_drawdown_limit_usdt=Decimal("0.80"),
        consecutive_loss_limit=3,
        openai_daily_budget_usd=Decimal("0.02"),
    )


def test_hard_caps_accept_equal_or_lower_values(safe_settings: RiskSettings) -> None:
    requested = replace(safe_settings, max_leverage=1, risk_per_trade_usdt=Decimal("0.05"))

    assert apply_hard_caps(requested) == requested


def test_hard_caps_reject_over_cap_input_without_clamping(safe_settings: RiskSettings) -> None:
    requested = replace(safe_settings, pilot_equity_cap_usdt=Decimal("20.01"), max_stages=3)

    with pytest.raises(HardRiskLimitExceeded) as error:
        apply_hard_caps(requested, DEFAULT_HARD_CAPS)

    assert error.value.code == "HARD_RISK_LIMIT_EXCEEDED"
    assert error.value.violations == ("pilot_equity_cap_usdt", "max_stages")
