"""Thread-safe, label-free local metrics suitable for a small control plane."""

import re
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock

from app.domain.decimal_math import ZERO

_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


@dataclass(frozen=True, slots=True)
class MetricPoint:
    name: str
    value: Decimal


class MetricRegistry:
    """Counters and gauges intentionally omit labels to avoid leaking account identifiers."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._values: dict[str, Decimal] = {}

    def increment(self, name: str, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError("metric increment amount must not be negative")
        self._validate_name(name)
        with self._lock:
            self._values[name] = self._values.get(name, ZERO) + Decimal(amount)

    def set_gauge(self, name: str, value: Decimal) -> None:
        if not value.is_finite():
            raise ValueError("metric gauge must be finite")
        self._validate_name(name)
        with self._lock:
            self._values[name] = value

    def observe_latency(self, name: str, latency_ms: int) -> None:
        if latency_ms < 0:
            raise ValueError("latency_ms must not be negative")
        self.increment(f"{name}_count")
        self.set_gauge(f"{name}_last_ms", Decimal(latency_ms))

    def points(self) -> tuple[MetricPoint, ...]:
        with self._lock:
            return tuple(
                MetricPoint(name=name, value=value) for name, value in sorted(self._values.items())
            )

    def render_prometheus(self) -> str:
        return "".join(f"{point.name} {format(point.value, 'f')}\n" for point in self.points())

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _METRIC_NAME.fullmatch(name):
            raise ValueError("metric name contains unsupported characters")


runtime_metrics = MetricRegistry()
