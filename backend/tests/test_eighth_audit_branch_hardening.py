"""Targeted branch regressions for eighth-audit authorization boundaries."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.types import Direction
from app.exchange.contracts import ExchangeReconciliationObservationBatch
from app.security.recovery import (
    RecoveryCheckpoint,
    RecoveryCheckpointError,
    RecoveryJournal,
    SimulatedPositionCheckpoint,
    SimulatedProtectionStatus,
)
from app.strategy.models import (
    Candle,
    SignalCandidate,
    StrategyLineage,
    StrategySpecification,
)
from tests.strategy_factory import make_strategy_lineage

ACCOUNT_ID = "v1-primary"


def _batch_fields() -> dict[str, object]:
    now = datetime.now(UTC) - timedelta(milliseconds=10)
    return {
        "source": "authenticated_exchange_adapter",
        "account_id": ACCOUNT_ID,
        "query_epoch": 1,
        "correlation_id": "eighth-branch-query",
        "requested_at": now - timedelta(milliseconds=1),
        "fetched_at": now,
        "server_time": now,
        "max_age": timedelta(seconds=30),
        "max_clock_skew": timedelta(seconds=5),
        "positions_by_symbol": {},
        "normal_order_client_ids": frozenset(),
        "algo_order_client_ids": frozenset(),
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ({"account_id": ""}, "account and correlation"),
        ({"correlation_id": ""}, "account and correlation"),
        ({"query_epoch": 0}, "positive query epoch"),
        ({"requested_at": datetime.now()}, "timezone-aware"),
        ({"max_age": timedelta(0)}, "max age"),
        ({"max_clock_skew": timedelta(minutes=2)}, "clock skew"),
    ),
)
def test_reconciliation_batch_rejects_invalid_authorization_metadata(
    mutation: dict[str, object],
    message: str,
) -> None:
    fields = _batch_fields()
    fields.update(mutation)

    with pytest.raises(ValueError, match=message):
        ExchangeReconciliationObservationBatch(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("time_mutation", "message"),
    (
        ("request_after_fetch", "request time"),
        ("query_too_slow", "query duration"),
        ("server_clock_skew", "server time"),
        ("future_fetch", "future"),
    ),
)
def test_reconciliation_batch_rejects_invalid_query_timeline(
    time_mutation: str,
    message: str,
) -> None:
    fields = _batch_fields()
    fetched_at = fields["fetched_at"]
    assert isinstance(fetched_at, datetime)
    if time_mutation == "request_after_fetch":
        fields["requested_at"] = fetched_at + timedelta(milliseconds=1)
    elif time_mutation == "query_too_slow":
        fields["max_age"] = timedelta(milliseconds=1)
        fields["requested_at"] = fetched_at - timedelta(seconds=1)
    elif time_mutation == "server_clock_skew":
        fields["server_time"] = fetched_at - timedelta(seconds=10)
    else:
        future = datetime.now(UTC) + timedelta(minutes=1)
        fields["requested_at"] = future - timedelta(milliseconds=1)
        fields["fetched_at"] = future
        fields["server_time"] = future

    with pytest.raises(ValueError, match=message):
        ExchangeReconciliationObservationBatch(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "mutation",
    (
        {"strategy_id": ""},
        {"strategy_specification_fingerprint": "not-a-sha256"},
    ),
)
def test_strategy_lineage_rejects_incomplete_or_malformed_identity(
    mutation: dict[str, str],
) -> None:
    lineage = make_strategy_lineage()

    with pytest.raises(ValueError):
        replace(lineage, **mutation)


def _valid_candle() -> Candle:
    return Candle(
        symbol="BTCUSDT",
        open_time_ms=0,
        close_time_ms=59_999,
        open_price=Decimal("100"),
        high_price=Decimal("101"),
        low_price=Decimal("99"),
        close_price=Decimal("100"),
        volume=Decimal("10"),
    )


@pytest.mark.parametrize(
    "mutation",
    (
        {"symbol": ""},
        {"close_time_ms": 0},
        {"open_price": Decimal("0")},
        {"high_price": Decimal("99")},
        {"low_price": Decimal("101")},
        {"volume": Decimal("-1")},
    ),
)
def test_candle_rejects_invalid_market_data(mutation: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(_valid_candle(), **mutation)  # type: ignore[arg-type]


def _valid_candidate(lineage: StrategyLineage | None = None) -> SignalCandidate:
    return SignalCandidate.from_lineage(
        lineage or make_strategy_lineage(),
        symbol="BTCUSDT",
        direction=Direction.LONG,
        reference_price=Decimal("100"),
        invalidation_price=Decimal("90"),
        timeframe="1m",
        valid_until_ms=120_000,
        reason_codes=("EIGHTH_AUDIT",),
    )


@pytest.mark.parametrize(
    "mutation",
    (
        {"symbol": ""},
        {"reference_price": Decimal("0")},
        {"invalidation_price": Decimal("100")},
        {"direction": Direction.SHORT},
        {"valid_until_ms": 0},
        {"reason_codes": ()},
    ),
)
def test_signal_candidate_rejects_invalid_execution_inputs(
    mutation: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        replace(_valid_candidate(), **mutation)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "specification",
    (
        StrategySpecification.no_trade(),
        StrategySpecification.volatility_breakout(lookback=2),
    ),
)
def test_strategy_specification_fingerprint_detects_mutation(
    specification: StrategySpecification,
) -> None:
    object.__setattr__(specification, "lookback", 99)

    with pytest.raises(ValueError, match="fingerprint"):
        specification.verify_fingerprint()


def _ready_position(plan_id: str = "eighth-plan") -> SimulatedPositionCheckpoint:
    return SimulatedPositionCheckpoint(
        plan_id=plan_id,
        symbol="BTCUSDT",
        quantity=Decimal("0.01"),
        simulated_protection=SimulatedProtectionStatus.REHEARSAL_READY,
        simulated_stop_reference="eighth-simulated-stop",
    )


def test_recovery_checkpoint_rejects_invalid_position_invariants() -> None:
    with pytest.raises(ValueError, match="positive quantity"):
        replace(_ready_position(), quantity=Decimal("0"))
    with pytest.raises(ValueError, match="requires a rehearsal reference"):
        replace(_ready_position(), simulated_stop_reference=None)
    with pytest.raises(ValueError, match="only ready"):
        replace(
            _ready_position(),
            simulated_protection=SimulatedProtectionStatus.UNPROTECTED,
        )
    with pytest.raises(ValueError, match="duplicate plan"):
        RecoveryCheckpoint(positions=(_ready_position(), _ready_position()))


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ([], "must be an object"),
        ({"positions": [], "schema_version": 2}, "schema is not supported"),
        ({"positions": {}, "schema_version": 3}, "positions must be a list"),
        (
            {"positions": ["not-an-object"], "schema_version": 3},
            "position must be an object",
        ),
        (
            {"positions": [{"plan_id": "missing-fields"}], "schema_version": 3},
            "position schema is not supported",
        ),
    ),
)
def test_recovery_journal_rejects_invalid_outer_schema(
    payload: object,
    message: str,
) -> None:
    with pytest.raises(RecoveryCheckpointError, match=message):
        RecoveryJournal._decode(payload)  # noqa: SLF001


def _position_payload() -> dict[str, object]:
    return {
        "plan_id": "eighth-plan",
        "quantity": "0.01",
        "simulated_protection": "REHEARSAL_READY",
        "simulated_stop_reference": "eighth-simulated-stop",
        "symbol": "BTCUSDT",
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ({"quantity": 1}, "fields are invalid"),
        ({"simulated_stop_reference": 1}, "stop evidence is invalid"),
        ({"quantity": "not-a-decimal"}, "values are invalid"),
        ({"simulated_protection": "EXCHANGE_CONFIRMED"}, "values are invalid"),
    ),
)
def test_recovery_journal_rejects_invalid_position_values(
    mutation: dict[str, object],
    message: str,
) -> None:
    payload = _position_payload()
    payload.update(mutation)

    with pytest.raises(RecoveryCheckpointError, match=message):
        RecoveryJournal._decode_position(payload)  # noqa: SLF001
