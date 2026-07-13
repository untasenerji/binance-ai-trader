from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.persistence.audit import AuditDeliveryStatus, AuditRepository, verify_hash_chain
from app.persistence.database import create_database_engine

LOCAL_POSTGRES_TEST_URL = "postgresql+psycopg://postgres@127.0.0.1:5432/uta"


def _payload(index: int = 1) -> dict[str, object]:
    return {"delivery_index": index, "price": "100.0100", "quantity": "0.002000"}


@pytest.mark.postgresql
def test_postgresql_migration_reaches_head_with_audit_chain_metadata() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", LOCAL_POSTGRES_TEST_URL)

    command.upgrade(config, "head")

    engine = create_database_engine(LOCAL_POSTGRES_TEST_URL)
    try:
        inspector = inspect(engine)
        assert "audit_chain_heads" in inspector.get_table_names()
        audit_columns = {column["name"] for column in inspector.get_columns("audit_events")}
        assert {"chain_sequence", "semantic_fingerprint", "delivery_status"} <= audit_columns
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_postgresql_audit_trigger_rejects_update_delete_and_truncate() -> None:
    engine = create_database_engine(LOCAL_POSTGRES_TEST_URL)
    statements = (
        "UPDATE audit_events SET source = source WHERE FALSE",
        "DELETE FROM audit_events WHERE FALSE",
        "TRUNCATE audit_events",
    )
    try:
        for statement in statements:
            with engine.connect() as connection:
                with pytest.raises(DatabaseError):
                    connection.execute(text(statement))
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_postgresql_round_trip_and_concurrent_audit_chain_are_linear(
    postgresql_session_factory: sessionmaker[Session],
) -> None:
    repository = AuditRepository(postgresql_session_factory)
    occurred_at = datetime(2026, 7, 12, tzinfo=UTC)
    delivery_count = 8
    barrier = Barrier(delivery_count)

    def record_exact_duplicate() -> AuditDeliveryStatus:
        barrier.wait(timeout=10)
        receipt = repository.record_delivery(
            event_id="evt-postgres-duplicate",
            source="simulator",
            event_type="fill_observation",
            occurred_at=occurred_at,
            payload=_payload(),
        )
        return receipt.delivery_status

    with ThreadPoolExecutor(max_workers=delivery_count) as executor:
        statuses = tuple(executor.map(lambda _: record_exact_duplicate(), range(delivery_count)))

    events = repository.list_audit_events()
    head = repository.audit_chain_head()
    assert statuses.count(AuditDeliveryStatus.CANONICAL) == 1
    assert statuses.count(AuditDeliveryStatus.EXACT_DUPLICATE) == delivery_count - 1
    assert len(events) == delivery_count
    assert repository.processed_event_count() == 1
    assert [event.chain_sequence for event in events] == list(range(1, delivery_count + 1))
    assert verify_hash_chain(
        events,
        expected_count=head.event_count,
        expected_last_record_hash=head.last_record_hash,
    )


@pytest.mark.postgresql
def test_postgresql_normalizes_nested_decimal_datetime_and_records_semantic_conflict(
    postgresql_session_factory: sessionmaker[Session],
) -> None:
    repository = AuditRepository(postgresql_session_factory)
    occurred_at = datetime(2026, 7, 12, tzinfo=UTC)
    payload = {
        "pricing": {"entry": Decimal("100.0100"), "fee": Decimal("0.0004")},
        "received_at": datetime(2026, 7, 12, 3, tzinfo=UTC),
    }
    repository.record_delivery(
        event_id="evt-postgres-conflict",
        source="simulator",
        event_type="fill_observation",
        occurred_at=occurred_at,
        payload=payload,
    )
    conflict = repository.record_delivery(
        event_id="evt-postgres-conflict",
        source="simulator",
        event_type="fill_observation",
        occurred_at=occurred_at,
        payload={"pricing": {"entry": Decimal("100.0200")}},
    )

    events = repository.list_audit_events()
    assert events[0].payload == {
        "pricing": {"entry": "100.0100", "fee": "0.0004"},
        "received_at": "2026-07-12T03:00:00+00:00",
    }
    assert conflict.delivery_status is AuditDeliveryStatus.SEMANTIC_CONFLICT
    assert conflict.reconciliation_required
    assert repository.processed_event_count() == 1
    assert verify_hash_chain(events)


@pytest.mark.postgresql
def test_postgresql_concurrent_different_events_preserve_one_linear_chain(
    postgresql_session_factory: sessionmaker[Session],
) -> None:
    repository = AuditRepository(postgresql_session_factory)
    delivery_count = 8
    barrier = Barrier(delivery_count)

    def record_distinct(index: int) -> AuditDeliveryStatus:
        barrier.wait(timeout=10)
        receipt = repository.record_delivery(
            event_id=f"evt-postgres-distinct-{index}",
            source="simulator",
            event_type="fill_observation",
            occurred_at=datetime(2026, 7, 12, tzinfo=UTC),
            payload=_payload(index),
        )
        return receipt.delivery_status

    with ThreadPoolExecutor(max_workers=delivery_count) as executor:
        statuses = tuple(executor.map(record_distinct, range(delivery_count)))

    events = repository.list_audit_events()
    head = repository.audit_chain_head()
    assert statuses == (AuditDeliveryStatus.CANONICAL,) * delivery_count
    assert len(events) == delivery_count
    assert repository.processed_event_count() == delivery_count
    assert [event.chain_sequence for event in events] == list(range(1, delivery_count + 1))
    assert verify_hash_chain(
        events,
        expected_count=head.event_count,
        expected_last_record_hash=head.last_record_hash,
    )
