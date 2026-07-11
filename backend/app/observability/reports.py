"""Deterministic daily operations summaries without account identifiers."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.domain.decimal_math import ZERO


@dataclass(frozen=True, slots=True)
class DailyReportInput:
    report_date: date
    candidates_evaluated: int
    candidates_accepted: int
    no_trade_outcomes: int
    realized_net_pnl_usdt: Decimal
    fees_usdt: Decimal
    ai_cost_usd: Decimal
    critical_alert_count: int

    def __post_init__(self) -> None:
        counts = (
            self.candidates_evaluated,
            self.candidates_accepted,
            self.no_trade_outcomes,
            self.critical_alert_count,
        )
        if any(count < 0 for count in counts):
            raise ValueError("daily report counts must not be negative")
        if self.candidates_accepted > self.candidates_evaluated:
            raise ValueError("accepted candidates cannot exceed evaluated candidates")
        if self.fees_usdt < ZERO or self.ai_cost_usd < ZERO:
            raise ValueError("daily report costs must not be negative")


@dataclass(frozen=True, slots=True)
class DailyOperationsReport:
    source: DailyReportInput

    def to_markdown(self) -> str:
        data = self.source
        return "\n".join(
            (
                f"# Daily Operations Report: {data.report_date.isoformat()}",
                "",
                f"- Candidates evaluated: {data.candidates_evaluated}",
                f"- Candidates accepted: {data.candidates_accepted}",
                f"- No-trade outcomes: {data.no_trade_outcomes}",
                f"- Realized net PnL: {format(data.realized_net_pnl_usdt, 'f')} USDT",
                f"- Fees: {format(data.fees_usdt, 'f')} USDT",
                f"- AI estimated cost: {format(data.ai_cost_usd, 'f')} USD",
                f"- Critical alerts: {data.critical_alert_count}",
                "",
                "This report is operational evidence only and does not authorize an order.",
            )
        )
