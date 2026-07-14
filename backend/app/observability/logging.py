"""Structured log events that redact secret-bearing fields before a sink sees them."""

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


_SENSITIVE_FIELD_PARTS = frozenset(
    {
        "api",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "key",
        "password",
        "passphrase",
        "secret",
        "signature",
        "token",
    }
)
_SENSITIVE_ASSIGNMENT_NAME = (
    r"api[_-]?key|api[_-]?secret|client[_-]?secret|access[_-]?token|"
    r"refresh[_-]?token|secret[_-]?key|authorization|cookie|password|passphrase|"
    r"secret|signature|token"
)
_ASSIGNMENT_PATTERN = re.compile(rf"(?i)\b({_SENSITIVE_ASSIGNMENT_NAME})\s*[:=]\s*([^\s,;;&]+)")
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_BASIC_PATTERN = re.compile(r"(?i)\bbasic\s+[^\s,;]+")
_URL_ENCODED_SEPARATOR = r"(?:[_-]|%5[fF]|%2[dD])"
_URL_ENCODED_SENSITIVE_ASSIGNMENT_NAME = (
    rf"api{_URL_ENCODED_SEPARATOR}?(?:key|secret)|"
    rf"client{_URL_ENCODED_SEPARATOR}?secret|"
    rf"access{_URL_ENCODED_SEPARATOR}?token|"
    rf"refresh{_URL_ENCODED_SEPARATOR}?token|"
    rf"secret{_URL_ENCODED_SEPARATOR}?key|"
    r"authorization|cookie|password|passphrase|secret|signature|token"
)
_URL_ENCODED_ASSIGNMENT_PATTERN = re.compile(
    rf"(?i)\b({_URL_ENCODED_SENSITIVE_ASSIGNMENT_NAME})(?:%3[aA]|%3[dD])([^\s,;;&]+)"
)
_OPENAI_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def redact_text(value: str) -> str:
    """Remove recognizable secret assignments without returning their original values."""
    redacted = _BASIC_PATTERN.sub("Basic [REDACTED]", value)
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", redacted)
    redacted = _ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    redacted = _URL_ENCODED_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}=[REDACTED]", redacted
    )
    return _OPENAI_KEY_PATTERN.sub("[REDACTED]", redacted)


def redact_for_log(value: object) -> object:
    """Return JSON-safe observability data that cannot expose sensitive field values."""
    if isinstance(value, Mapping):
        redacted: dict[str, object] = {}
        for key, nested_value in value.items():
            normalized_key = str(key)
            redacted[normalized_key] = (
                "[REDACTED]"
                if _is_sensitive_field(normalized_key)
                else redact_for_log(nested_value)
            )
        return redacted
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_for_log(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__name__}>"


def _is_sensitive_field(key: str) -> bool:
    separated_key = _CAMEL_CASE_BOUNDARY.sub(" ", key)
    key_parts = re.split(r"[^a-z0-9]+", separated_key.casefold())
    compact_key = "".join(key_parts)
    return (
        any(part in _SENSITIVE_FIELD_PARTS for part in key_parts)
        or compact_key in _SENSITIVE_FIELD_PARTS
        or "apikey" in compact_key
    )


@dataclass(frozen=True, slots=True)
class StructuredLogEvent:
    event: str
    level: LogLevel
    occurred_at_utc: datetime
    fields: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.event.strip():
            raise ValueError("event is required")
        if self.occurred_at_utc.tzinfo is None:
            raise ValueError("occurred_at_utc must be timezone-aware")

    def to_json(self) -> str:
        return json.dumps(
            {
                "event": redact_text(self.event),
                "fields": redact_for_log(self.fields),
                "level": self.level.value,
                "occurred_at_utc": self.occurred_at_utc.astimezone(UTC).isoformat(),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


class StructuredLogger:
    """A small sink abstraction so structured logs stay independently testable."""

    def __init__(self, sink: Callable[[str], None]) -> None:
        self._sink = sink

    def emit(
        self,
        *,
        event: str,
        level: LogLevel = LogLevel.INFO,
        fields: Mapping[str, object] | None = None,
        occurred_at_utc: datetime | None = None,
    ) -> str:
        record = StructuredLogEvent(
            event=event,
            level=level,
            occurred_at_utc=occurred_at_utc or datetime.now(UTC),
            fields=fields or {},
        )
        serialized = record.to_json()
        self._sink(serialized)
        return serialized


def python_structured_logger(name: str) -> StructuredLogger:
    logger = logging.getLogger(name)

    def sink(serialized: str) -> None:
        logger.info(serialized)

    return StructuredLogger(sink)
