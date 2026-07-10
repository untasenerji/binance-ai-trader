import pytest

from app.market.stream import (
    RECONNECT_BEFORE_MS,
    StreamPayloadError,
    build_public_stream_url,
    reconnect_delay_ms,
    reconnect_due,
)


def test_public_stream_url_uses_documented_routed_endpoint_and_lowercase_names() -> None:
    assert (
        build_public_stream_url(("BTCUSDT@DEPTH", "btcusdt@markPrice"))
        == "wss://fstream.binance.com/public/stream?streams=btcusdt@depth/btcusdt@markprice"
    )


def test_stream_lifecycle_reconnects_before_documented_24_hour_limit() -> None:
    assert not reconnect_due(connected_at_ms=0, now_ms=RECONNECT_BEFORE_MS - 1)
    assert reconnect_due(connected_at_ms=0, now_ms=RECONNECT_BEFORE_MS)
    assert reconnect_delay_ms(0) == 1_000
    assert reconnect_delay_ms(10) == 30_000


def test_invalid_stream_name_is_rejected() -> None:
    with pytest.raises(StreamPayloadError):
        build_public_stream_url(("BTCUSDT@depth?private=true",))
