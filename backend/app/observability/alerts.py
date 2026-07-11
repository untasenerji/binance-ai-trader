"""Local alert contracts with deterministic deduplication and no network adapter."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from app.observability.logging import redact_text
from app.observability.metrics import MetricRegistry


class AlertSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AlertDeliveryStatus(StrEnum):
    DELIVERED = "DELIVERED"
    SUPPRESSED = "SUPPRESSED"


@dataclass(frozen=True, slots=True)
class Alert:
    code: str
    severity: AlertSeverity
    detail: str
    dedupe_key: str

    def __post_init__(self) -> None:
        if not self.code or not self.detail or not self.dedupe_key:
            raise ValueError("alert code, detail, and dedupe_key are required")

    @property
    def redacted(self) -> "Alert":
        return replace(self, detail=redact_text(self.detail))


@dataclass(frozen=True, slots=True)
class AlertDelivery:
    status: AlertDeliveryStatus
    alert: Alert


class AlertAdapter:
    """Future adapters must implement this local contract; Phase 11 ships no network adapter."""

    def send(self, alert: Alert) -> None:
        raise NotImplementedError


class InMemoryAlertAdapter(AlertAdapter):
    def __init__(self) -> None:
        self.deliveries: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.deliveries.append(alert)


class AlertDeduplicator:
    def __init__(self, *, window_ms: int) -> None:
        if window_ms <= 0:
            raise ValueError("window_ms must be positive")
        self._window_ms = window_ms
        self._last_delivered_at_ms: dict[str, int] = {}

    def claim(self, alert: Alert, *, now_ms: int) -> bool:
        if now_ms < 0:
            raise ValueError("now_ms must not be negative")
        last_delivered = self._last_delivered_at_ms.get(alert.dedupe_key)
        if last_delivered is not None and now_ms - last_delivered < self._window_ms:
            return False
        self._last_delivered_at_ms[alert.dedupe_key] = now_ms
        return True


class AlertDispatcher:
    def __init__(
        self,
        *,
        adapters: Sequence[AlertAdapter],
        deduplicator: AlertDeduplicator,
        metrics: MetricRegistry,
    ) -> None:
        self._adapters = tuple(adapters)
        self._deduplicator = deduplicator
        self._metrics = metrics

    def dispatch(self, alert: Alert, *, now_ms: int) -> AlertDelivery:
        safe_alert = alert.redacted
        if not self._deduplicator.claim(safe_alert, now_ms=now_ms):
            self._metrics.increment("uta_alerts_suppressed_total")
            return AlertDelivery(AlertDeliveryStatus.SUPPRESSED, safe_alert)
        for adapter in self._adapters:
            adapter.send(safe_alert)
        self._metrics.increment("uta_alerts_delivered_total")
        return AlertDelivery(AlertDeliveryStatus.DELIVERED, safe_alert)
