"""Medium-severity regressions from the third independent audit.

Each test captures the unsafe pre-remediation behavior before the corresponding
production change is introduced.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from app.domain.types import Direction
from app.observability.logging import redact_text
from app.persistence.audit import AuditDeliveryStatus, AuditRepository
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.strategy.backtest import BacktestCosts, WalkForwardRunner, WalkForwardTrainingError
from app.strategy.models import Candle, FrozenStrategy, SignalCandidate, TrainableStrategy

_POSTGRES_ADMIN_URL = "postgresql+psycopg://postgres@127.0.0.1:5432/uta"
_POSTGRES_RUNTIME_URL = "postgresql+psycopg://uta_runtime@127.0.0.1:5432/uta"


def _candle(index: int, close: Decimal) -> Candle:
    return Candle(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time_ms=index * 60_000,
        close_time_ms=(index + 1) * 60_000 - 1,
        open_price=close,
        high_price=close + Decimal("1"),
        low_price=close - Decimal("1"),
        close_price=close,
        volume=Decimal("1"),
    )


def _costs() -> BacktestCosts:
    return BacktestCosts(
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        slippage_bps=Decimal("0"),
    )


class HeldOutDataTrainer:
    """Carries future candles while reporting plausible train-slice metadata."""

    strategy_id = "held-out-data-trainer"

    def __init__(self, held_out_candles: tuple[Candle, ...]) -> None:
        self._held_out_candles = held_out_candles

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        future_threshold = self._held_out_candles[-1].close_price

        def evaluate(
            evaluation_candles: Sequence[Candle],
            *,
            timeframe: str,
        ) -> SignalCandidate | None:
            last = evaluation_candles[-1]
            if last.close_price <= future_threshold:
                return None
            return SignalCandidate(
                strategy_id=self.strategy_id,
                symbol=last.symbol,
                direction=Direction.LONG,
                reference_price=last.close_price,
                invalidation_price=last.low_price,
                timeframe=timeframe,
                valid_until_ms=last.close_time_ms + 60_000,
                reason_codes=("ILLEGAL_HELD_OUT_THRESHOLD",),
            )

        return FrozenStrategy(
            strategy_id=self.strategy_id,
            training_candle_count=len(candles),
            training_end_ms=candles[-1].close_time_ms,
            configuration_fingerprint=f"future-threshold:{future_threshold}",
            evaluator=evaluate,
        )


def test_walk_forward_rejects_a_trainer_that_retains_held_out_market_data() -> None:
    candles = tuple(_candle(index, Decimal("100") + Decimal(index)) for index in range(7))

    def factory() -> TrainableStrategy:
        return HeldOutDataTrainer(candles[4:])

    with pytest.raises(WalkForwardTrainingError, match="held-out"):
        WalkForwardRunner(train_size=4, test_size=3, step_size=3).run(
            factory,
            candles,
            timeframe="1m",
            costs=_costs(),
        )


def test_redact_text_removes_json_and_quoted_secret_values() -> None:
    json_api_key = "third-audit-json-api-key-canary"
    json_client_secret = "third-audit-json-client-secret-canary"
    quoted_password = "third-audit-quoted-password-canary"
    quoted_signature = "third-audit-quoted-signature-canary"
    payload = (
        f'{{"api_key":"{json_api_key}",'
        f'"clientSecret":"{json_client_secret}"}} '
        f"password='{quoted_password}' signature=\"{quoted_signature}\""
    )

    redacted = redact_text(payload)

    assert all(
        canary not in redacted
        for canary in (
            json_api_key,
            json_client_secret,
            quoted_password,
            quoted_signature,
        )
    )
    assert redacted.count("[REDACTED]") == 4


def test_sqlite_processed_event_claims_are_append_only(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'processed-events.sqlite'}")
    create_schema(engine)
    repository = AuditRepository(create_session_factory(engine))
    occurred_at = datetime(2026, 7, 14, tzinfo=UTC)
    first = repository.record_delivery(
        event_id="processed-event-append-only",
        source="third-audit",
        event_type="processed_event_probe",
        occurred_at=occurred_at,
        payload={"probe": "processed-event"},
    )

    assert first.delivery_status is AuditDeliveryStatus.CANONICAL
    with pytest.raises(IntegrityError, match="processed_events are append-only"):
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM processed_events WHERE event_id = 'processed-event-append-only'")
            )

    duplicate = repository.record_delivery(
        event_id="processed-event-append-only",
        source="third-audit",
        event_type="processed_event_probe",
        occurred_at=occurred_at,
        payload={"probe": "processed-event"},
    )

    assert duplicate.delivery_status is AuditDeliveryStatus.EXACT_DUPLICATE


@pytest.mark.postgresql
def test_postgresql_runtime_role_cannot_create_temp_shadow_tables() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", _POSTGRES_ADMIN_URL)
    command.upgrade(config, "head")
    engine = create_database_engine(_POSTGRES_RUNTIME_URL)
    try:
        with engine.connect() as connection:
            can_create_temp = connection.scalar(
                text("SELECT has_database_privilege(current_user, current_database(), 'TEMPORARY')")
            )
            assert not can_create_temp
            with pytest.raises(DBAPIError):
                connection.execute(text("CREATE TEMPORARY TABLE audit_events (id integer)"))
            connection.rollback()
    finally:
        engine.dispose()
