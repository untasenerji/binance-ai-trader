from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.persistence.audit import (
    AuditPayloadValidationError,
    AuditRepository,
    normalize_audit_payload,
    verify_hash_chain,
)
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.persistence.models import AuditEvent, ProcessedEvent, TradePlanProjection


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "audit-codec.sqlite"


@pytest.fixture
def session_factory(database_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{database_path}")
    create_schema(engine)
    return create_session_factory(engine)


def _nested_payload() -> dict[str, object]:
    return {
        "plan_id": "plan-codec",
        "from_state": "DRAFT",
        "to_state": "CANDIDATE",
        "plan_version": 1,
        "source_sequence": 1,
        "transition_evidence": {
            "risk_permits_entry": False,
            "stop_confirmed": False,
            "reduction_only": False,
        },
        "pricing": {
            "entry": Decimal("100.0100"),
            "quantity": Decimal("0.002000"),
            "fees": [Decimal("0.0004"), {"funding": Decimal("0.00000010")}],
        },
        "occurred": datetime(2026, 7, 10, 12, 0, tzinfo=UTC) + timedelta(hours=3),
    }


def _row_counts(session_factory: sessionmaker[Session]) -> tuple[int, int, int]:
    with session_factory() as session:
        return (
            int(session.scalar(select(func.count()).select_from(AuditEvent)) or 0),
            int(session.scalar(select(func.count()).select_from(ProcessedEvent)) or 0),
            int(session.scalar(select(func.count()).select_from(TradePlanProjection)) or 0),
        )


def test_recursive_normalizer_preserves_financial_decimal_strings_and_utc_datetime() -> None:
    normalized = normalize_audit_payload(_nested_payload())

    assert normalized == {
        "plan_id": "plan-codec",
        "from_state": "DRAFT",
        "to_state": "CANDIDATE",
        "plan_version": 1,
        "source_sequence": 1,
        "transition_evidence": {
            "risk_permits_entry": False,
            "stop_confirmed": False,
            "reduction_only": False,
        },
        "pricing": {
            "entry": "100.0100",
            "quantity": "0.002000",
            "fees": ["0.0004", {"funding": "0.00000010"}],
        },
        "occurred": "2026-07-10T15:00:00+00:00",
    }


def test_audit_stores_the_exact_normalized_payload_and_reloads_with_a_valid_hash_chain(
    database_path: Path,
) -> None:
    first_engine = create_database_engine(f"sqlite:///{database_path}")
    create_schema(first_engine)
    repository = AuditRepository(create_session_factory(first_engine))

    repository.record_delivery(
        event_id="evt-codec",
        source="strategy",
        event_type="state_transition",
        occurred_at=datetime(2026, 7, 10, tzinfo=UTC),
        payload=_nested_payload(),
    )
    first_engine.dispose()

    second_engine = create_database_engine(f"sqlite:///{database_path}")
    reloaded = AuditRepository(create_session_factory(second_engine)).list_audit_events()

    assert reloaded[0].payload == normalize_audit_payload(_nested_payload())
    assert verify_hash_chain(reloaded)
    second_engine.dispose()


@pytest.mark.parametrize(
    "payload",
    (
        {"price": 1.25},
        {"price": Decimal("NaN")},
        {"price": Decimal("Infinity")},
        {"occurred": datetime(2026, 7, 10)},
        cast(Mapping[str, object], {1: "non-string-key"}),
        {"unsupported": object()},
    ),
)
def test_invalid_payload_values_are_rejected_before_any_audit_dedupe_or_projection_write(
    session_factory: sessionmaker[Session], payload: Mapping[str, object]
) -> None:
    repository = AuditRepository(session_factory)

    with pytest.raises(AuditPayloadValidationError):
        repository.record_delivery(
            event_id="evt-invalid-payload",
            source="strategy",
            event_type="state_transition",
            occurred_at=datetime(2026, 7, 10, tzinfo=UTC),
            payload=payload,
        )

    assert _row_counts(session_factory) == (0, 0, 0)


def test_naive_audit_timestamp_is_rejected_before_any_write(
    session_factory: sessionmaker[Session],
) -> None:
    repository = AuditRepository(session_factory)

    with pytest.raises(AuditPayloadValidationError):
        repository.record_delivery(
            event_id="evt-naive-audit-time",
            source="strategy",
            event_type="state_transition",
            occurred_at=datetime(2026, 7, 10),
            payload={"plan_id": "plan-codec"},
        )

    assert _row_counts(session_factory) == (0, 0, 0)
