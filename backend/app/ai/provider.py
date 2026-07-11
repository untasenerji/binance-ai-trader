"""Responses API request contract and a local mock-only provider."""

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

from app.ai.models import AIUsage, AssessmentInput
from app.ai.schema import AI_OUTPUT_SCHEMA

SYSTEM_INSTRUCTIONS = """You are a risk-analysis assistant for Binance USD-SM Futures.
You have no order, quantity, leverage, stop, configuration, or execution authority.
Use only the supplied structured input. Do not infer missing live data.
If data is stale, contradictory, or unverifiable, return INSUFFICIENT_DATA.
Return only the requested JSON schema with at most three observations."""


@dataclass(frozen=True, slots=True)
class ResponsesRequest:
    """A serializable, no-tools request shape for `POST /v1/responses`."""

    model: str
    assessment_input: AssessmentInput
    tools: tuple[object, ...] = ()
    tool_choice: Literal["none"] = "none"
    store: Literal[False] = False
    parallel_tool_calls: Literal[False] = False

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model is required")
        if self.tools:
            raise ValueError("AI tools are forbidden by the project authority boundary")

    def to_payload(self) -> dict[str, object]:
        prompt_json = json.dumps(
            self.assessment_input.to_prompt_payload(), sort_keys=True, separators=(",", ":")
        )
        return {
            "model": self.model,
            "instructions": SYSTEM_INSTRUCTIONS,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt_json}],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "market_risk_assessment",
                    "strict": True,
                    "schema": AI_OUTPUT_SCHEMA,
                }
            },
            "tools": [],
            "tool_choice": self.tool_choice,
            "parallel_tool_calls": self.parallel_tool_calls,
            "store": self.store,
        }


@dataclass(frozen=True, slots=True)
class AIProviderResponse:
    output_text: str | None
    usage: AIUsage
    refusal: str | None = None


class AIProvider(Protocol):
    """Only receives the no-tools Responses API contract; no API client is supplied yet."""

    async def create_response(self, request: ResponsesRequest) -> AIProviderResponse: ...


@dataclass(slots=True)
class MockAIProvider:
    """Local test double. It has no network, credential, or tool implementation."""

    response: AIProviderResponse | Exception
    requests: list[ResponsesRequest] = field(default_factory=list)

    async def create_response(self, request: ResponsesRequest) -> AIProviderResponse:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def mocked_usage(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_cost_usd: Decimal = Decimal("0"),
) -> AIUsage:
    """Convenience factory that keeps mock accounting Decimal-only."""
    return AIUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost_usd,
        latency_ms=0,
    )
