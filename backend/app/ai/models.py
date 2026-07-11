"""Value objects for the advisory-only AI boundary."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from app.domain.decimal_math import ZERO
from app.domain.risk import DEFAULT_HARD_CAPS


class AIMode(StrEnum):
    OFF = "off"
    ADVISORY = "advisory"
    VETO_ONLY = "veto_only"
    POST_TRADE_ONLY = "post_trade_only"


class AIPurpose(StrEnum):
    PRE_TRADE = "pre_trade"
    POST_TRADE = "post_trade"


class AssessmentStatus(StrEnum):
    OK = "OK"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    ERROR = "ERROR"


class MarketRegime(StrEnum):
    TREND = "TREND"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    DISLOCATED = "DISLOCATED"
    UNKNOWN = "UNKNOWN"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class VetoReasonCode(StrEnum):
    STALE_DATA = "STALE_DATA"
    SPREAD_SPIKE = "SPREAD_SPIKE"
    VOLATILITY_SHOCK = "VOLATILITY_SHOCK"
    EVENT_RISK = "EVENT_RISK"
    SIGNAL_CONTRADICTION = "SIGNAL_CONTRADICTION"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class AIOutcomeStatus(StrEnum):
    DISABLED = "DISABLED"
    ASSESSED = "ASSESSED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    TIMED_OUT = "TIMED_OUT"
    REFUSED = "REFUSED"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    COST_BOUND_EXCEEDED = "COST_BOUND_EXCEEDED"


class SensitiveAIInputRejected(ValueError):
    code = "SENSITIVE_AI_INPUT_REJECTED"


class AIBudgetLimitExceeded(ValueError):
    code = "AI_BUDGET_ABOVE_HARD_CAP"


_SENSITIVE_MARKERS = (
    "api_key",
    "api secret",
    "authorization:",
    "x-mbx-apikey",
    "private key",
    "begin private",
)


@dataclass(frozen=True, slots=True)
class ModelAvailability:
    """A real account check is represented, never fabricated from catalog presence."""

    checked_at_utc: datetime | None
    available_model_ids: frozenset[str]
    reason_code: str

    def __post_init__(self) -> None:
        if self.checked_at_utc is not None and self.checked_at_utc.tzinfo is None:
            raise ValueError("checked_at_utc must be timezone-aware")
        if not self.reason_code:
            raise ValueError("reason_code is required")

    @classmethod
    def unverified_without_credentials(cls) -> "ModelAvailability":
        return cls(
            checked_at_utc=None,
            available_model_ids=frozenset(),
            reason_code="MODEL_AVAILABILITY_UNVERIFIED",
        )

    @classmethod
    def verified_for_test(cls, *model_ids: str) -> "ModelAvailability":
        return cls(
            checked_at_utc=datetime.now(UTC),
            available_model_ids=frozenset(model_ids),
            reason_code="MODEL_AVAILABILITY_VERIFIED_TEST_ONLY",
        )

    def includes(self, model_id: str) -> bool:
        return self.checked_at_utc is not None and model_id in self.available_model_ids


@dataclass(frozen=True, slots=True)
class AISettings:
    mode: AIMode
    operational_model: str
    daily_budget_usd: Decimal
    max_request_cost_usd: Decimal
    timeout_ms: int
    model_availability: ModelAvailability

    def __post_init__(self) -> None:
        if not self.operational_model.strip():
            raise ValueError("operational_model is required")
        if self.daily_budget_usd < ZERO or self.max_request_cost_usd < ZERO:
            raise ValueError("AI budget values must not be negative")
        if self.daily_budget_usd > DEFAULT_HARD_CAPS.openai_daily_budget_usd:
            raise AIBudgetLimitExceeded("AI daily budget exceeds the D-026 backend hard cap")
        if self.max_request_cost_usd > self.daily_budget_usd:
            raise ValueError("max_request_cost_usd must not exceed daily_budget_usd")
        if self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")

    @property
    def model_is_available(self) -> bool:
        return self.model_availability.includes(self.operational_model)


@dataclass(frozen=True, slots=True)
class AssessmentInput:
    """Minimized prompt input; no credential, order, or mutable risk fields exist."""

    symbol: str
    observed_at_utc: datetime
    market_data_is_fresh: bool
    strategy_summary: str
    risk_summary: str
    data_quality_flags: tuple[str, ...] = ()
    untrusted_context: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol is required")
        if self.observed_at_utc.tzinfo is None:
            raise ValueError("observed_at_utc must be timezone-aware")
        if not self.strategy_summary.strip() or not self.risk_summary.strip():
            raise ValueError("strategy_summary and risk_summary are required")
        if len(self.data_quality_flags) > 10:
            raise ValueError("data_quality_flags may contain at most 10 entries")
        for value in (
            self.strategy_summary,
            self.risk_summary,
            self.untrusted_context or "",
            *self.data_quality_flags,
        ):
            if any(marker in value.lower() for marker in _SENSITIVE_MARKERS):
                raise SensitiveAIInputRejected(
                    "Potential secret-bearing content cannot enter the AI prompt"
                )

    def to_prompt_payload(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "observed_at_utc": self.observed_at_utc.isoformat(),
            "market_data_is_fresh": self.market_data_is_fresh,
            "strategy_summary": self.strategy_summary,
            "risk_summary": self.risk_summary,
            "data_quality_flags": list(self.data_quality_flags),
            "untrusted_context": self.untrusted_context,
        }


@dataclass(frozen=True, slots=True)
class AIAssessment:
    status: AssessmentStatus
    market_regime: MarketRegime
    risk_level: RiskLevel
    veto: bool
    veto_reason_codes: tuple[VetoReasonCode, ...]
    data_quality_flags: tuple[str, ...]
    observations: tuple[str, ...]
    valid_until_utc: datetime
    model_version: str

    def __post_init__(self) -> None:
        if self.valid_until_utc.tzinfo is None:
            raise ValueError("valid_until_utc must be timezone-aware")
        if len(self.veto_reason_codes) != len(set(self.veto_reason_codes)):
            raise ValueError("veto_reason_codes must be unique")
        if self.veto and not self.veto_reason_codes:
            raise ValueError("a veto requires an allowlisted reason code")
        if not self.veto and self.veto_reason_codes:
            raise ValueError("veto_reason_codes require veto=true")
        if len(self.data_quality_flags) > 10:
            raise ValueError("data_quality_flags may contain at most 10 entries")
        if len(self.observations) > 3:
            raise ValueError("observations may contain at most 3 entries")
        if not self.model_version.strip():
            raise ValueError("model_version is required")


@dataclass(frozen=True, slots=True)
class AIUsage:
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: Decimal
    latency_ms: int

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0 or self.latency_ms < 0:
            raise ValueError("token counts and latency must not be negative")
        if self.estimated_cost_usd < ZERO:
            raise ValueError("estimated_cost_usd must not be negative")


@dataclass(frozen=True, slots=True)
class AIOutcome:
    status: AIOutcomeStatus
    assessment: AIAssessment | None
    usage: AIUsage | None
    blocks_candidate: bool
    detail_code: str | None = None

    def __post_init__(self) -> None:
        if self.status is AIOutcomeStatus.ASSESSED and self.assessment is None:
            raise ValueError("ASSESSED outcomes require an assessment")
        if self.status is not AIOutcomeStatus.ASSESSED and self.assessment is not None:
            raise ValueError("only ASSESSED outcomes may contain an assessment")
