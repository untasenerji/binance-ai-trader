"""Strict local validation for the Responses structured-output contract."""

import json
from datetime import datetime
from typing import cast

from app.ai.models import (
    AIAssessment,
    AssessmentStatus,
    MarketRegime,
    RiskLevel,
    VetoReasonCode,
)

AI_OUTPUT_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "MarketRiskAssessment",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "market_regime",
        "risk_level",
        "veto",
        "veto_reason_codes",
        "data_quality_flags",
        "observations",
        "valid_until_utc",
        "model_version",
    ],
    "properties": {
        "status": {"enum": ["OK", "INSUFFICIENT_DATA", "ERROR"]},
        "market_regime": {"enum": ["TREND", "RANGE", "HIGH_VOLATILITY", "DISLOCATED", "UNKNOWN"]},
        "risk_level": {"enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
        "veto": {"type": "boolean"},
        "veto_reason_codes": {
            "type": "array",
            "items": {
                "enum": [
                    "STALE_DATA",
                    "SPREAD_SPIKE",
                    "VOLATILITY_SHOCK",
                    "EVENT_RISK",
                    "SIGNAL_CONTRADICTION",
                    "INSUFFICIENT_DATA",
                ]
            },
            "uniqueItems": True,
        },
        "data_quality_flags": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 10,
        },
        "observations": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
        },
        "valid_until_utc": {"type": "string", "format": "date-time"},
        "model_version": {"type": "string", "minLength": 1},
    },
}

_REQUIRED_KEYS = frozenset(
    {
        "status",
        "market_regime",
        "risk_level",
        "veto",
        "veto_reason_codes",
        "data_quality_flags",
        "observations",
        "valid_until_utc",
        "model_version",
    }
)


class InvalidStructuredAssessment(ValueError):
    code = "INVALID_STRUCTURED_ASSESSMENT"


def parse_structured_assessment(raw_text: str) -> AIAssessment:
    """Reject malformed, incomplete, extra, or semantically unsafe model output."""
    try:
        decoded: object = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise InvalidStructuredAssessment("AI output is not valid JSON") from error

    if not isinstance(decoded, dict):
        raise InvalidStructuredAssessment("AI output must be a JSON object")

    payload: dict[str, object] = {}
    for key, value in decoded.items():
        if not isinstance(key, str):
            raise InvalidStructuredAssessment("AI output keys must be strings")
        payload[key] = value
    if set(payload) != _REQUIRED_KEYS:
        raise InvalidStructuredAssessment(
            "AI output does not exactly match the required schema keys"
        )

    status = _parse_enum(AssessmentStatus, payload["status"], "status")
    market_regime = _parse_enum(MarketRegime, payload["market_regime"], "market_regime")
    risk_level = _parse_enum(RiskLevel, payload["risk_level"], "risk_level")
    veto = payload["veto"]
    if not isinstance(veto, bool):
        raise InvalidStructuredAssessment("veto must be a boolean")

    veto_codes = tuple(
        _parse_enum(VetoReasonCode, item, "veto_reason_codes")
        for item in _parse_string_list(payload["veto_reason_codes"], "veto_reason_codes", 6)
    )
    data_quality_flags = _parse_string_list(payload["data_quality_flags"], "data_quality_flags", 10)
    observations = _parse_string_list(payload["observations"], "observations", 3)
    valid_until_utc = _parse_datetime(payload["valid_until_utc"])
    model_version = _parse_nonempty_string(payload["model_version"], "model_version")

    try:
        return AIAssessment(
            status=status,
            market_regime=market_regime,
            risk_level=risk_level,
            veto=veto,
            veto_reason_codes=veto_codes,
            data_quality_flags=data_quality_flags,
            observations=observations,
            valid_until_utc=valid_until_utc,
            model_version=model_version,
        )
    except ValueError as error:
        raise InvalidStructuredAssessment(str(error)) from error


def _parse_enum[T: AssessmentStatus | MarketRegime | RiskLevel | VetoReasonCode](
    enum_type: type[T], value: object, field_name: str
) -> T:
    if not isinstance(value, str):
        raise InvalidStructuredAssessment(f"{field_name} must be a string")
    try:
        return cast(T, enum_type(value))
    except ValueError as error:
        raise InvalidStructuredAssessment(f"{field_name} contains an unsupported value") from error


def _parse_string_list(value: object, field_name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InvalidStructuredAssessment(f"{field_name} must be an array of strings")
    if len(value) > maximum:
        raise InvalidStructuredAssessment(f"{field_name} exceeds its maximum length")
    return tuple(value)


def _parse_nonempty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidStructuredAssessment(f"{field_name} must be a non-empty string")
    return value


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise InvalidStructuredAssessment("valid_until_utc must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidStructuredAssessment("valid_until_utc must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise InvalidStructuredAssessment("valid_until_utc must include a timezone")
    return parsed
