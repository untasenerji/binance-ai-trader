import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.observability.alerts import (
    Alert,
    AlertDeduplicator,
    AlertDeliveryStatus,
    AlertDispatcher,
    AlertSeverity,
    InMemoryAlertAdapter,
)
from app.observability.backup import BackupNotReady, create_backup_manifest, verify_restore
from app.observability.logging import (
    LogLevel,
    StructuredLogEvent,
    StructuredLogger,
    python_structured_logger,
    redact_for_log,
    redact_text,
)
from app.observability.metrics import MetricRegistry
from app.observability.recovery import OperationsMonitor, RecoveryAction
from app.observability.reports import DailyOperationsReport, DailyReportInput
from app.persistence.audit import AuditRepository
from app.persistence.circuit_breaker import PersistenceCircuitBreaker
from app.persistence.database import create_database_engine, create_schema, create_session_factory


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    database_path = tmp_path / "observability.sqlite"
    engine = create_database_engine(f"sqlite:///{database_path}")
    create_schema(engine)
    return create_session_factory(engine)


def _dispatcher(metrics: MetricRegistry) -> tuple[AlertDispatcher, InMemoryAlertAdapter]:
    adapter = InMemoryAlertAdapter()
    return (
        AlertDispatcher(
            adapters=(adapter,),
            deduplicator=AlertDeduplicator(window_ms=1_000),
            metrics=metrics,
        ),
        adapter,
    )


def test_structured_log_redacts_nested_fields_and_secret_like_text() -> None:
    emitted: list[str] = []
    logger = StructuredLogger(emitted.append)

    serialized = logger.emit(
        event="phase11_log_test",
        level=LogLevel.WARNING,
        fields={
            "api_key": "placeholder-value",
            "nested": {"authorization": "Bearer placeholder-value"},
            "message": "token=placeholder-value",
        },
        occurred_at_utc=datetime(2026, 7, 11, tzinfo=UTC),
    )

    assert emitted == [serialized]
    assert "placeholder-value" not in serialized
    assert "[REDACTED]" in serialized
    assert redact_for_log({"secret": "value"}) == {"secret": "[REDACTED]"}


def test_structured_log_redacts_camel_case_headers_queries_and_nested_values() -> None:
    emitted: list[str] = []
    logger = StructuredLogger(emitted.append)
    canaries = (
        "log-canary-client-secret",
        "log-canary-access-token",
        "log-canary-refresh-token",
        "log-canary-secret-key",
        "log-canary-api-key",
        "log-canary-auth-header",
        "log-canary-cookie",
        "log-canary-passphrase",
    )

    serialized = logger.emit(
        event="phase11_identifier_redaction",
        fields={
            "clientSecret": canaries[0],
            "AccessToken": canaries[1],
            "nested": {
                "headers": {
                    "Authorization": f"Bearer {canaries[5]}",
                    "X-Api-Key": canaries[4],
                    "Cookie": canaries[6],
                },
                "query": {
                    "refreshToken": canaries[2],
                    "secretKey": canaries[3],
                    "passphrase": canaries[7],
                },
            },
            "message": (
                f"https://local.invalid/check?clientSecret={canaries[0]}&"
                f"accessToken={canaries[1]}&refreshToken={canaries[2]}"
            ),
        },
        occurred_at_utc=datetime(2026, 7, 12, tzinfo=UTC),
    )

    assert emitted == [serialized]
    assert all(canary not in serialized for canary in canaries)
    assert serialized.count("[REDACTED]") >= len(canaries)


def test_log_redaction_covers_basic_credentials_url_encoded_secrets_and_event_text() -> None:
    basic_canary = "basic-credential-canary"
    encoded_access_canary = "encoded-access-canary"
    encoded_api_canary = "encoded-api-canary"
    event_canary = "event-secret-canary"
    emitted: list[str] = []
    logger = StructuredLogger(emitted.append)
    encoded_text = (
        f"Authorization: Basic {basic_canary}; "
        f"access%5Ftoken%3D{encoded_access_canary}&"
        f"api%5Fkey%3D{encoded_api_canary}"
    )

    serialized = logger.emit(
        event=f"upstream failed: {encoded_text}; secret={event_canary}",
        fields={"detail": encoded_text},
        occurred_at_utc=datetime(2026, 7, 13, tzinfo=UTC),
    )

    assert emitted == [serialized]
    assert all(
        canary not in serialized
        for canary in (
            basic_canary,
            encoded_access_canary,
            encoded_api_canary,
            event_canary,
        )
    )
    assert basic_canary not in redact_text(encoded_text)


def test_log_boundary_handles_collection_scalar_datetime_and_validation_branches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class UnserializableValue:
        pass

    naive_time = datetime(2026, 7, 14, 12, 0)
    aware_time = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    redacted = redact_for_log(
        [
            "token=collection-canary",
            Decimal("1.25"),
            naive_time,
            aware_time,
            None,
            True,
            3,
            0.5,
            UnserializableValue(),
        ]
    )

    assert redacted == [
        "token=[REDACTED]",
        "1.25",
        "2026-07-14T12:00:00+00:00",
        "2026-07-14T12:00:00+00:00",
        None,
        True,
        3,
        0.5,
        "<UnserializableValue>",
    ]
    with pytest.raises(ValueError, match="event is required"):
        StructuredLogEvent(
            event="  ",
            level=LogLevel.INFO,
            occurred_at_utc=aware_time,
            fields={},
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        StructuredLogEvent(
            event="naive-event",
            level=LogLevel.INFO,
            occurred_at_utc=naive_time,
            fields={},
        )

    logger = python_structured_logger("third-audit-logging-branches")
    with caplog.at_level(logging.INFO, logger="third-audit-logging-branches"):
        serialized = logger.emit(event="branch-coverage", fields={})

    assert serialized in caplog.messages


def test_metrics_are_label_free_decimal_safe_and_renderable() -> None:
    metrics = MetricRegistry()
    metrics.increment("uta_recovery_blocks_total")
    metrics.set_gauge("uta_clock_skew_ms", Decimal("1500"))
    metrics.observe_latency("uta_reconciliation_latency", 42)

    rendered = metrics.render_prometheus()

    assert "uta_recovery_blocks_total 1" in rendered
    assert "uta_clock_skew_ms 1500" in rendered
    assert "uta_reconciliation_latency_last_ms 42" in rendered
    with pytest.raises(ValueError):
        metrics.set_gauge("unsafe metric", Decimal("1"))


def test_alert_deduplication_and_redaction_are_deterministic() -> None:
    metrics = MetricRegistry()
    dispatcher, adapter = _dispatcher(metrics)
    alert = Alert(
        code="DATABASE_DOWN",
        severity=AlertSeverity.CRITICAL,
        detail="authorization=placeholder-value",
        dedupe_key="DATABASE_DOWN",
    )

    first = dispatcher.dispatch(alert, now_ms=0)
    duplicate = dispatcher.dispatch(alert, now_ms=500)
    after_window = dispatcher.dispatch(alert, now_ms=1_000)

    assert first.status is AlertDeliveryStatus.DELIVERED
    assert duplicate.status is AlertDeliveryStatus.SUPPRESSED
    assert after_window.status is AlertDeliveryStatus.DELIVERED
    assert len(adapter.deliveries) == 2
    assert "placeholder-value" not in adapter.deliveries[0].detail


def test_database_disk_clock_and_restart_faults_block_entries() -> None:
    metrics = MetricRegistry()
    dispatcher, adapter = _dispatcher(metrics)
    breaker = PersistenceCircuitBreaker()
    monitor = OperationsMonitor(
        persistence_breaker=breaker,
        alerts=dispatcher,
        metrics=metrics,
        max_clock_skew_ms=1_000,
    )

    database = monitor.database_down(RuntimeError("database unavailable"), now_ms=0)
    disk = monitor.disk_full(OSError("disk full"), now_ms=2_000)
    clock = monitor.clock_skew(1_500, now_ms=4_000)
    restart = monitor.restart(
        reconciliation_clean=False,
        protected_position_confirmed=False,
        now_ms=6_000,
    )

    assert not breaker.new_entries_allowed
    assert not database.new_entries_allowed
    assert not disk.new_entries_allowed
    assert RecoveryAction.RESYNC_CLOCK in clock.actions
    assert RecoveryAction.HARD_HALT in restart.actions
    assert len(adapter.deliveries) == 4


def test_daily_report_and_backup_restore_readiness(session_factory: sessionmaker[Session]) -> None:
    repository = AuditRepository(session_factory)
    repository.record_delivery(
        event_id="evt-backup",
        source="strategy",
        event_type="state_transition",
        occurred_at=datetime(2026, 7, 11, tzinfo=UTC),
        payload={
            "plan_id": "plan-backup",
            "from_state": "DRAFT",
            "to_state": "CANDIDATE",
            "plan_version": 1,
            "source_sequence": 1,
            "transition_evidence": {
                "risk_permits_entry": False,
                "stop_confirmed": False,
                "reduction_only": False,
            },
        },
    )
    events = repository.list_audit_events()
    manifest = create_backup_manifest(events)
    report = DailyOperationsReport(
        DailyReportInput(
            report_date=date(2026, 7, 11),
            candidates_evaluated=3,
            candidates_accepted=0,
            no_trade_outcomes=3,
            realized_net_pnl_usdt=Decimal("0"),
            fees_usdt=Decimal("0"),
            ai_cost_usd=Decimal("0"),
            critical_alert_count=0,
        )
    )

    assert manifest.audit_hash_chain_valid
    assert verify_restore(events, manifest)
    assert "No-trade outcomes: 3" in report.to_markdown()
    tampered_manifest = manifest.__class__(
        created_at_utc=manifest.created_at_utc,
        audit_event_count=manifest.audit_event_count + 1,
        last_record_hash=manifest.last_record_hash,
        audit_hash_chain_valid=True,
    )
    assert not verify_restore(events, tampered_manifest)
    with pytest.raises(BackupNotReady):
        create_backup_manifest((events[0], events[0]))
