"""Fail-safe advisory orchestration with an optional one-way veto guard."""

import asyncio
import time

from app.ai.models import (
    AIMode,
    AIOutcome,
    AIOutcomeStatus,
    AIPurpose,
    AISettings,
    AIUsage,
    AssessmentInput,
)
from app.ai.provider import AIProvider, ResponsesRequest
from app.ai.schema import InvalidStructuredAssessment, parse_structured_assessment
from app.ai.usage import AIBudgetExhausted, AIUsageLedger
from app.strategy.gate import CandidateDecision


class AIOrchestrator:
    """AI can provide a display-only assessment or an allowlisted one-way veto."""

    def __init__(self, *, settings: AISettings, provider: AIProvider, usage: AIUsageLedger) -> None:
        self._settings = settings
        self._provider = provider
        self._usage = usage

    async def assess(
        self,
        assessment_input: AssessmentInput,
        *,
        purpose: AIPurpose = AIPurpose.PRE_TRADE,
    ) -> AIOutcome:
        if self._settings.mode is AIMode.OFF:
            return self._outcome(AIOutcomeStatus.DISABLED, "AI_MODE_OFF", purpose)
        if self._settings.mode is AIMode.POST_TRADE_ONLY and purpose is not AIPurpose.POST_TRADE:
            return self._outcome(AIOutcomeStatus.DISABLED, "POST_TRADE_ONLY", purpose)
        if not self._settings.model_is_available:
            return self._outcome(
                AIOutcomeStatus.MODEL_UNAVAILABLE,
                self._settings.model_availability.reason_code,
                purpose,
            )
        if not self._usage.can_start(self._settings.max_request_cost_usd):
            return self._outcome(
                AIOutcomeStatus.BUDGET_EXHAUSTED, "AI_DAILY_BUDGET_EXHAUSTED", purpose
            )

        request = ResponsesRequest(
            model=self._settings.operational_model,
            assessment_input=assessment_input,
        )
        started_at_ns = time.monotonic_ns()
        try:
            response = await asyncio.wait_for(
                self._provider.create_response(request),
                timeout=self._settings.timeout_ms / 1_000,
            )
        except TimeoutError:
            return self._outcome(AIOutcomeStatus.TIMED_OUT, "AI_TIMEOUT", purpose)
        except Exception:
            return self._outcome(AIOutcomeStatus.INVALID_OUTPUT, "AI_PROVIDER_FAILURE", purpose)

        latency_ms = (time.monotonic_ns() - started_at_ns) // 1_000_000
        usage = AIUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            estimated_cost_usd=response.usage.estimated_cost_usd,
            latency_ms=latency_ms,
        )
        if usage.estimated_cost_usd > self._settings.max_request_cost_usd:
            self._usage.exhaust()
            return self._outcome(
                AIOutcomeStatus.COST_BOUND_EXCEEDED,
                "AI_PROVIDER_COST_BOUND_EXCEEDED",
                purpose,
                usage=usage,
            )
        try:
            self._usage.record(usage)
        except AIBudgetExhausted:
            return self._outcome(
                AIOutcomeStatus.BUDGET_EXHAUSTED,
                "AI_DAILY_BUDGET_EXHAUSTED",
                purpose,
                usage=usage,
            )

        if response.refusal is not None:
            return self._outcome(AIOutcomeStatus.REFUSED, "AI_REFUSAL", purpose, usage=usage)
        if response.output_text is None:
            return self._outcome(
                AIOutcomeStatus.INVALID_OUTPUT, "AI_EMPTY_OUTPUT", purpose, usage=usage
            )
        try:
            assessment = parse_structured_assessment(response.output_text)
        except InvalidStructuredAssessment:
            return self._outcome(
                AIOutcomeStatus.INVALID_OUTPUT, "AI_INVALID_JSON", purpose, usage=usage
            )

        should_veto = (
            purpose is AIPurpose.PRE_TRADE
            and self._settings.mode is AIMode.VETO_ONLY
            and assessment.veto
        )
        return AIOutcome(
            status=AIOutcomeStatus.ASSESSED,
            assessment=assessment,
            usage=usage,
            blocks_candidate=should_veto,
        )

    def _outcome(
        self,
        status: AIOutcomeStatus,
        detail_code: str,
        purpose: AIPurpose,
        *,
        usage: AIUsage | None = None,
    ) -> AIOutcome:
        return AIOutcome(
            status=status,
            assessment=None,
            usage=usage,
            blocks_candidate=(
                purpose is AIPurpose.PRE_TRADE and self._settings.mode is AIMode.VETO_ONLY
            ),
            detail_code=detail_code,
        )


def apply_ai_veto(candidate_decision: CandidateDecision, outcome: AIOutcome) -> CandidateDecision:
    """AI may only convert an accepted candidate to rejected; it cannot make one pass."""
    if not outcome.blocks_candidate or not candidate_decision.accepted:
        return candidate_decision
    return CandidateDecision(
        accepted=False,
        candidate=None,
        reason_codes=(*candidate_decision.reason_codes, "AI_VETO"),
    )
