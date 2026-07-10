"""Backend-authoritative pilot safety limits required by D-026."""

from dataclasses import dataclass, fields
from decimal import Decimal

from app.domain.decimal_math import ZERO


@dataclass(frozen=True, slots=True)
class RiskSettings:
    """A requested local configuration expressed entirely with Decimal values."""

    pilot_equity_cap_usdt: Decimal
    max_leverage: int
    max_concurrent_positions: int
    max_active_strategy_count: int
    max_stages: int
    risk_per_trade_usdt: Decimal
    daily_loss_limit_usdt: Decimal
    weekly_drawdown_limit_usdt: Decimal
    consecutive_loss_limit: int
    openai_daily_budget_usd: Decimal

    def __post_init__(self) -> None:
        for setting in fields(self):
            value = getattr(self, setting.name)
            if isinstance(value, Decimal) and value < ZERO:
                raise ValueError(f"{setting.name} must not be negative")
            if isinstance(value, int) and value < 0:
                raise ValueError(f"{setting.name} must not be negative")


@dataclass(frozen=True, slots=True)
class PilotHardCaps:
    """Immutable live-pilot envelope. Lower requested values remain unchanged."""

    pilot_equity_cap_usdt: Decimal
    max_leverage: int
    max_concurrent_positions: int
    max_active_strategy_count: int
    max_stages: int
    risk_per_trade_usdt: Decimal
    daily_loss_limit_usdt: Decimal
    weekly_drawdown_limit_usdt: Decimal
    consecutive_loss_limit: int
    openai_daily_budget_usd: Decimal


DEFAULT_HARD_CAPS = PilotHardCaps(
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


class HardRiskLimitExceeded(ValueError):
    """The explicit backend rejection required when a request exceeds D-026."""

    code = "HARD_RISK_LIMIT_EXCEEDED"

    def __init__(self, violations: tuple[str, ...]) -> None:
        self.violations = violations
        super().__init__(f"{self.code}: {', '.join(violations)}")


def apply_hard_caps(
    requested: RiskSettings,
    caps: PilotHardCaps = DEFAULT_HARD_CAPS,
) -> RiskSettings:
    """Reject above-cap input; deliberately never clamp a requested value upward."""
    violations = tuple(
        setting.name
        for setting in fields(caps)
        if getattr(requested, setting.name) > getattr(caps, setting.name)
    )
    if violations:
        raise HardRiskLimitExceeded(violations)
    return requested
