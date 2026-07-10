"""Routed public WebSocket support with explicit reconnect lifecycle policy."""

import json
import re
from collections.abc import Sequence

from websockets.asyncio.client import connect

PUBLIC_STREAM_BASE_URL = "wss://fstream.binance.com/public"
RECONNECT_BEFORE_MS = 86_100_000
_STREAM_NAME_PATTERN = re.compile(r"[a-z0-9_!@]+")


class StreamPayloadError(ValueError):
    """Raised when a public stream name or message is malformed."""


def build_public_stream_url(streams: Sequence[str]) -> str:
    normalized = tuple(stream.strip().lower() for stream in streams)
    if not normalized or any(not _STREAM_NAME_PATTERN.fullmatch(stream) for stream in normalized):
        raise StreamPayloadError(
            "public stream names must be non-empty lowercase Binance stream names"
        )
    return f"{PUBLIC_STREAM_BASE_URL}/stream?streams={'/'.join(normalized)}"


def reconnect_due(*, connected_at_ms: int, now_ms: int) -> bool:
    if now_ms < connected_at_ms:
        raise ValueError("now_ms cannot precede connected_at_ms")
    return now_ms - connected_at_ms >= RECONNECT_BEFORE_MS


def reconnect_delay_ms(attempt: int, *, maximum_ms: int = 30_000) -> int:
    if attempt < 0 or maximum_ms <= 0:
        raise ValueError("reconnect arguments are invalid")

    delay_ms = 1_000
    for _ in range(attempt):
        if delay_ms >= maximum_ms:
            return maximum_ms
        delay_ms *= 2
    return min(maximum_ms, delay_ms)


class PublicWebSocketClient:
    """A public-only receive primitive; it cannot accept credentials or send orders."""

    async def receive_one(self, streams: Sequence[str]) -> dict[str, object]:
        async with connect(build_public_stream_url(streams), ping_interval=None) as socket:
            payload = await socket.recv()

        raw_text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        decoded = json.loads(raw_text)
        if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
            raise StreamPayloadError("public stream payload must be an object")
        return decoded
