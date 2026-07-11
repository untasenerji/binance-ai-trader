"""Operational fault decisions preserve the no-new-entry safety posture."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.observability.alerts import Alert, AlertDispatcher, AlertSeverity
from app.observability.metrics import MetricRegistry
from app.persistence.circuit_breaker import PersistenceCircuitBreaker


class RecoveryAction(StrEnum):
    PAUSE_NEW_ENTRIES = "PAUSE_NEW_ENTRIES"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    VERIFY_STOP_PROTECTION = "VERIFY_STOP_PROTECTION"
    RESYNC_CLOCK = "RESYNC_CLOCK"
    HARD_HALT = "HARD_HALT"
    VERIFY_BACKUP = "VERIFY_BACKUP"


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    new_entries_allowed: bool
    actions: tuple[RecoveryAction, ...]
    alert_code: str | None


class OperationsMonitor:
    """Maps local operational faults to safe, observable entry-gating decisions."""

    def __init__(
        self,
        *,
        persistence_breaker: PersistenceCircuitBreaker,
        alerts: AlertDispatcher,
        metrics: MetricRegistry,
        max_clock_skew_ms: int = 1_000,
    ) -> None:
        if max_clock_skew_ms <= 0:
            raise ValueError("max_clock_skew_ms must be positive")
        self._persistence_breaker = persistence_breaker
        self._alerts = alerts
        self._metrics = metrics
        self._max_clock_skew_ms = max_clock_skew_ms

    def database_down(self, error: Exception, *, now_ms: int) -> RecoveryDecision:
        self._persistence_breaker.record_write_failure(error)
        self._metrics.set_gauge("uta_database_ready", Decimal("0"))
        return self._critical(
            code="DATABASE_DOWN",
            detail=f"database write unavailable: {type(error).__name__}",
            actions=(RecoveryAction.PAUSE_NEW_ENTRIES, RecoveryAction.RECONCILE_REQUIRED),
            now_ms=now_ms,
        )

    def disk_full(self, error: OSError, *, now_ms: int) -> RecoveryDecision:
        self._persistence_breaker.record_write_failure(error)
        self._metrics.set_gauge("uta_disk_writable", Decimal("0"))
        return self._critical(
            code="DISK_FULL",
            detail=f"durable storage unavailable: {type(error).__name__}",
            actions=(
                RecoveryAction.PAUSE_NEW_ENTRIES,
                RecoveryAction.RECONCILE_REQUIRED,
                RecoveryAction.VERIFY_BACKUP,
            ),
            now_ms=now_ms,
        )

    def clock_skew(self, offset_ms: int, *, now_ms: int) -> RecoveryDecision:
        self._metrics.set_gauge("uta_clock_skew_ms", Decimal(offset_ms))
        if abs(offset_ms) <= self._max_clock_skew_ms:
            return RecoveryDecision(new_entries_allowed=True, actions=(), alert_code=None)
        return self._critical(
            code="CLOCK_SKEW",
            detail="clock offset exceeds the local safe threshold",
            actions=(RecoveryAction.PAUSE_NEW_ENTRIES, RecoveryAction.RESYNC_CLOCK),
            now_ms=now_ms,
        )

    def restart(
        self,
        *,
        reconciliation_clean: bool,
        protected_position_confirmed: bool,
        now_ms: int,
    ) -> RecoveryDecision:
        self._metrics.increment("uta_restart_total")
        if reconciliation_clean and protected_position_confirmed:
            return RecoveryDecision(new_entries_allowed=True, actions=(), alert_code=None)
        actions = [RecoveryAction.PAUSE_NEW_ENTRIES, RecoveryAction.RECONCILE_REQUIRED]
        code = "RESTART_RECONCILIATION_REQUIRED"
        detail = "restart requires reconciliation before entries can resume"
        if not protected_position_confirmed:
            actions.append(RecoveryAction.HARD_HALT)
            code = "RESTART_STOP_UNCONFIRMED"
            detail = "restart found an unconfirmed position protection state"
        return self._critical(
            code=code,
            detail=detail,
            actions=tuple(actions),
            now_ms=now_ms,
        )

    def _critical(
        self,
        *,
        code: str,
        detail: str,
        actions: tuple[RecoveryAction, ...],
        now_ms: int,
    ) -> RecoveryDecision:
        delivery = self._alerts.dispatch(
            Alert(
                code=code,
                severity=AlertSeverity.CRITICAL,
                detail=detail,
                dedupe_key=code,
            ),
            now_ms=now_ms,
        )
        self._metrics.increment("uta_recovery_blocks_total")
        return RecoveryDecision(
            new_entries_allowed=False,
            actions=actions,
            alert_code=delivery.alert.code,
        )
