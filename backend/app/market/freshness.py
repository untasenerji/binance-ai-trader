"""Freshness tracking blocks downstream planning when public data becomes stale."""

from dataclasses import dataclass, field


class StaleMarketData(RuntimeError):
    """Raised when data is too old to support a new candidate or entry."""


@dataclass(slots=True)
class DataFreshness:
    max_age_ms: int
    _observed_at_ms: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_age_ms <= 0:
            raise ValueError("max_age_ms must be positive")

    def record(self, stream: str, *, observed_at_ms: int) -> None:
        if observed_at_ms < 0:
            raise ValueError("observed_at_ms must not be negative")
        self._observed_at_ms[stream] = observed_at_ms

    def age_ms(self, stream: str, *, now_ms: int) -> int | None:
        observed_at_ms = self._observed_at_ms.get(stream)
        if observed_at_ms is None:
            return None
        return max(0, now_ms - observed_at_ms)

    def is_fresh(self, stream: str, *, now_ms: int) -> bool:
        age = self.age_ms(stream, now_ms=now_ms)
        return age is not None and age <= self.max_age_ms

    def require_fresh(self, stream: str, *, now_ms: int) -> None:
        if not self.is_fresh(stream, now_ms=now_ms):
            raise StaleMarketData(f"{stream} is stale or absent")
