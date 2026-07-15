"""Structured log events that redact secret-bearing fields before a sink sees them."""

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from urllib.parse import unquote


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


_SENSITIVE_SINGLE_ALIASES = frozenset(
    {
        "api",
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
_SENSITIVE_COMPOUND_ALIAS_PARTS = (
    ("api", "key"),
    ("api", "secret"),
    ("api", "token"),
    ("client", "secret"),
    ("access", "token"),
    ("refresh", "token"),
    ("secret", "key"),
)
_SENSITIVE_CANONICAL_ALIASES = _SENSITIVE_SINGLE_ALIASES | frozenset(
    "".join(parts) for parts in _SENSITIVE_COMPOUND_ALIAS_PARTS
)
_ALIAS_SEPARATOR_PATTERN = r"[\s_-]*"
_SENSITIVE_ASSIGNMENT_NAME = "|".join(
    (
        *(
            _ALIAS_SEPARATOR_PATTERN.join(re.escape(part) for part in parts)
            for parts in _SENSITIVE_COMPOUND_ALIAS_PARTS
        ),
        *(re.escape(alias) for alias in sorted(_SENSITIVE_SINGLE_ALIASES)),
    )
)
_ASSIGNMENT_PATTERN = re.compile(rf"(?i)\b({_SENSITIVE_ASSIGNMENT_NAME})\s*[:=]\s*([^\s,;;&]+)")
_JSON_QUOTED_ASSIGNMENT_PATTERN = re.compile(
    rf'(?i)(?P<prefix>"(?P<key>{_SENSITIVE_ASSIGNMENT_NAME})"\s*:\s*")'
    r'(?P<value>(?:\\.|[^"])*)"'
)
_SINGLE_QUOTED_JSON_ASSIGNMENT_PATTERN = re.compile(
    rf"(?i)(?P<prefix>'(?P<key>{_SENSITIVE_ASSIGNMENT_NAME})'\s*:\s*')"
    r"(?P<value>(?:\\.|[^'])*)'"
)
_QUOTED_ASSIGNMENT_PATTERN = re.compile(
    rf"(?i)(?P<prefix>\b(?:{_SENSITIVE_ASSIGNMENT_NAME})\s*[:=]\s*(?P<quote>['\"]))"
    r"(?P<value>(?:\\.|(?!\2).)*)\2"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_BASIC_PATTERN = re.compile(r"(?i)\bbasic\s+[^\s,;]+")
_OPENAI_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_UNICODE_ESCAPE_PATTERN = re.compile(r"\\u([0-9a-fA-F]{4})")
_MAX_DECODE_LAYERS = 8
_MAX_TEXT_LENGTH = 65_536
_MAX_STRUCTURE_DEPTH = 24
_MAX_COLLECTION_ITEMS = 4_096
_MAX_STRUCTURE_NODES = 16_384


def _decode_one_layer(value: str) -> str:
    decoded = unquote(value)
    decoded = decoded.replace(r"\\", "\\")
    decoded = decoded.replace(r"\"", '"').replace(r"\'", "'")
    return _UNICODE_ESCAPE_PATTERN.sub(
        lambda match: chr(int(match.group(1), 16)),
        decoded,
    )


def _bounded_decode(value: str) -> tuple[str, bool]:
    if len(value) > _MAX_TEXT_LENGTH:
        return "[REDACTED]", True
    decoded = value
    for _ in range(_MAX_DECODE_LAYERS):
        next_value = _decode_one_layer(decoded)
        if len(next_value) > _MAX_TEXT_LENGTH:
            return "[REDACTED]", True
        if next_value == decoded:
            return decoded, False
        decoded = next_value
    if _decode_one_layer(decoded) != decoded:
        return "[REDACTED]", True
    return decoded, False


def redact_text(value: str) -> str:
    """Remove recognizable secret assignments without returning their original values."""
    redacted, decode_limit_exceeded = _bounded_decode(value)
    if decode_limit_exceeded:
        return redacted
    redacted = _JSON_QUOTED_ASSIGNMENT_PATTERN.sub(
        lambda match: f'{match.group("prefix")}[REDACTED]"',
        redacted,
    )
    redacted = _SINGLE_QUOTED_JSON_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]'",
        redacted,
    )
    redacted = _QUOTED_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]{match.group('quote')}",
        redacted,
    )
    redacted = _BASIC_PATTERN.sub("Basic [REDACTED]", redacted)
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", redacted)
    redacted = _ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    return _OPENAI_KEY_PATTERN.sub("[REDACTED]", redacted)


def redact_for_log(value: object) -> object:
    """Return JSON-safe observability data that cannot expose sensitive field values."""
    return _redact_for_log(value, depth=0, remaining_nodes=[_MAX_STRUCTURE_NODES])


def _redact_for_log(
    value: object,
    *,
    depth: int,
    remaining_nodes: list[int],
) -> object:
    if depth > _MAX_STRUCTURE_DEPTH or remaining_nodes[0] <= 0:
        return "[REDACTED]"
    remaining_nodes[0] -= 1
    if isinstance(value, Mapping):
        if len(value) > _MAX_COLLECTION_ITEMS:
            return "[REDACTED]"
        redacted: dict[str, object] = {}
        for key, nested_value in value.items():
            normalized_key = str(key)
            redacted[normalized_key] = (
                "[REDACTED]"
                if _is_sensitive_field(normalized_key)
                else _redact_for_log(
                    nested_value,
                    depth=depth + 1,
                    remaining_nodes=remaining_nodes,
                )
            )
        return redacted
    if isinstance(value, (list, tuple, set, frozenset)):
        if len(value) > _MAX_COLLECTION_ITEMS:
            return "[REDACTED]"
        return [
            _redact_for_log(
                item,
                depth=depth + 1,
                remaining_nodes=remaining_nodes,
            )
            for item in value
        ]
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
    decoded_key, decode_limit_exceeded = _bounded_decode(key)
    if decode_limit_exceeded:
        return True
    separated_key = _CAMEL_CASE_BOUNDARY.sub(" ", decoded_key)
    key_parts = re.split(r"[^a-z0-9]+", separated_key.casefold())
    compact_key = "".join(key_parts)
    return (
        any(part in _SENSITIVE_SINGLE_ALIASES for part in key_parts)
        or compact_key in _SENSITIVE_CANONICAL_ALIASES
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
