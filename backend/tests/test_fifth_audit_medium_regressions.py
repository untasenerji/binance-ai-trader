"""Red-first logging and hardening cases from the fifth independent audit."""

import json
from urllib.parse import quote

import pytest

from app.observability.logging import redact_for_log, redact_text


@pytest.mark.parametrize(
    ("payload", "secret"),
    (
        (
            r"{\"payload\":\"{\\\"api_key\\\":\\\"DOUBLE_ESCAPED_SECRET\\\"}\"}",
            "DOUBLE_ESCAPED_SECRET",
        ),
        ('{"\\u0061pi_key":"UNICODE_KEY_SECRET"}', "UNICODE_KEY_SECRET"),
        ("{'client_secret': 'SINGLE_QUOTED_SECRET'}", "SINGLE_QUOTED_SECRET"),
        ("Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==", "QWxhZGRpbjpvcGVuIHNlc2FtZQ=="),
        ("Proxy-Authorization: Bearer BEARER_HEADER_SECRET", "BEARER_HEADER_SECRET"),
    ),
)
def test_redact_text_handles_escaped_json_and_authorization_schemes(
    payload: str,
    secret: str,
) -> None:
    redacted = redact_text(payload)

    assert secret not in redacted
    assert "[REDACTED]" in redacted


def test_redact_text_recursively_decodes_multiple_percent_layers() -> None:
    secret = "MULTI_PERCENT_SECRET"
    payload = f"api_key={secret}"
    for _ in range(6):
        payload = quote(payload, safe="")

    redacted = redact_text(payload)

    assert secret not in redacted
    assert "[REDACTED]" in redacted


def test_redact_text_fails_closed_when_decode_depth_exceeds_bound() -> None:
    secret = "OVER_BOUND_SECRET"
    payload = f"api_key={secret}"
    for _ in range(32):
        payload = quote(payload, safe="")

    redacted = redact_text(payload)

    assert secret not in redacted
    assert "[REDACTED]" in redacted


def test_redact_for_log_decodes_encoded_mapping_keys_and_nested_events() -> None:
    event = {
        "api%255Fkey": "ENCODED_MAPPING_SECRET",
        "outer": [
            {"\\u0061uthorization": "Bearer NESTED_BEARER_SECRET"},
            {
                "message": (
                    r"{\"inner\":\"{\\\"refresh_token\\\":"
                    r"\\\"NESTED_JSON_SECRET\\\"}\"}"
                )
            },
        ],
    }

    redacted = redact_for_log(event)
    serialized = json.dumps(redacted, ensure_ascii=True)

    for secret in (
        "ENCODED_MAPPING_SECRET",
        "NESTED_BEARER_SECRET",
        "NESTED_JSON_SECRET",
    ):
        assert secret not in serialized
    assert serialized.count("[REDACTED]") >= 3
