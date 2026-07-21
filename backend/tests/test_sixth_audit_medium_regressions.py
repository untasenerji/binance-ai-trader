"""Red-first medium-severity regressions from the sixth independent audit."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import pytest

from app.domain.types import Direction
from app.exchange.contracts import ReconciliationSnapshot
from app.observability.logging import redact_for_log, redact_text
from app.persistence.audit import AuditRepository
from app.persistence.circuit_breaker import PersistenceCircuitBreaker, PersistenceUnavailable
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.simulation.intent_ledger import DurableIntentLedger
from app.simulation.models import OrderRole, SimulatedOrderIntent
from app.strategy.backtest import BacktestCosts, WalkForwardRunner
from app.strategy.models import Candle, StrategySpecification
from tests.reconciliation_factory import exchange_reconciliation_batch


def _entry(client_order_id: str, plan_id: str) -> SimulatedOrderIntent:
    return SimulatedOrderIntent(
        client_order_id=client_order_id,
        plan_id=plan_id,
        symbol="BTCUSDT",
        direction=Direction.LONG,
        role=OrderRole.ENTRY,
        stage_index=1,
        quantity=Decimal("0.01"),
        price=Decimal("100"),
    )


@pytest.mark.parametrize(
    "alias",
    (
        "clientsecret",
        "client_secret",
        "client-secret",
        "secretkey",
        "accessToken",
        "refresh-token",
        "apitoken",
        "apiKey",
    ),
)
def test_mapping_and_text_redaction_share_canonical_secret_aliases(alias: str) -> None:
    mapping_secret = f"MAPPING_{alias}_CANARY"
    text_secret = f"TEXT_{alias}_CANARY"
    encoded_alias = quote(quote(alias, safe=""), safe="")
    event = {
        "outer": {
            encoded_alias: mapping_secret,
            "message": f"{{'{alias}': '{text_secret}'}}",
        }
    }

    serialized = json.dumps(redact_for_log(event), ensure_ascii=True)

    assert mapping_secret not in serialized
    assert text_secret not in serialized
    assert serialized.count("[REDACTED]") >= 2


def test_redaction_fails_closed_at_text_size_and_structure_depth_bounds() -> None:
    oversized_secret = "OVERSIZED_SECRET_CANARY"
    oversized = "x" * 70_000 + f" apiKey={oversized_secret}"
    nested: object = {"clientsecret": "DEEPLY_NESTED_SECRET_CANARY"}
    for _ in range(64):
        nested = {"level": nested}

    assert redact_text(oversized) == "[REDACTED]"
    serialized = json.dumps(redact_for_log(nested), ensure_ascii=True)
    assert "DEEPLY_NESTED_SECRET_CANARY" not in serialized
    assert "[REDACTED]" in serialized


def test_durable_reconciliation_payloads_reject_malformed_collections() -> None:
    with pytest.raises(PersistenceUnavailable, match="EXCHANGE_SNAPSHOT_EVIDENCE_INVALID"):
        DurableIntentLedger._snapshot_from_payload(  # noqa: SLF001
            {
                "positions_by_symbol": {},
                "algo_orders": {},
            }
        )
    with pytest.raises(PersistenceUnavailable, match="LOCAL_RECONCILIATION_EVIDENCE_INVALID"):
        DurableIntentLedger._local_state_from_payload(  # noqa: SLF001
            {
                "positions_by_symbol": {},
                "expected_stop_contracts": {},
            },
            audit_chain_valid=True,
            replay_valid=True,
        )
    with pytest.raises(PersistenceUnavailable, match="EXCHANGE_SNAPSHOT_EVIDENCE_INVALID"):
        DurableIntentLedger._string_set_from_payload(  # noqa: SLF001
            {"normal_order_client_ids": ["valid", 7]},
            "normal_order_client_ids",
            "EXCHANGE_SNAPSHOT_EVIDENCE_INVALID",
        )


def test_private_breaker_state_mutation_cannot_authorize_entry(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'unauthorized-breaker.sqlite'}")
    create_schema(engine)
    breaker = PersistenceCircuitBreaker()
    ledger = DurableIntentLedger(
        create_session_factory(engine),
        persistence_breaker=breaker,
    )
    object.__setattr__(breaker, "_PersistenceCircuitBreaker__halted_reason", None)

    with pytest.raises(PersistenceUnavailable):
        ledger.prepare(_entry("forged-breaker-entry", "forged-breaker-plan"))

    engine.dispose()


def test_verified_durable_capability_survives_process_object_restart(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'durable-capability.sqlite'}")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    initial_breaker = PersistenceCircuitBreaker()
    initial_ledger = DurableIntentLedger(
        session_factory,
        persistence_breaker=initial_breaker,
    )
    repository = AuditRepository(session_factory, persistence_breaker=initial_breaker)
    initial_breaker.reset_after_verified_reconciliation(
        audit_repository=repository,
        intent_ledger=initial_ledger,
        reconciliation_snapshot=exchange_reconciliation_batch(
            ReconciliationSnapshot(
                positions_by_symbol={},
                normal_order_client_ids=frozenset(),
                algo_order_client_ids=frozenset(),
            )
        ),
    )

    restarted = DurableIntentLedger(
        session_factory,
        persistence_breaker=PersistenceCircuitBreaker(),
    )

    assert restarted.prepare(_entry("durable-capability-entry", "durable-capability-plan"))
    engine.dispose()


def test_persistence_failure_durably_revokes_existing_capability(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'revoked-capability.sqlite'}")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    breaker = PersistenceCircuitBreaker()
    ledger = DurableIntentLedger(session_factory, persistence_breaker=breaker)
    repository = AuditRepository(session_factory, persistence_breaker=breaker)
    snapshot = exchange_reconciliation_batch(
        ReconciliationSnapshot(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
        )
    )
    evidence = breaker.reset_after_verified_reconciliation(
        audit_repository=repository,
        intent_ledger=ledger,
        reconciliation_snapshot=snapshot,
    )
    breaker.record_write_failure(RuntimeError("injected durable failure"))
    object.__setattr__(breaker, "_PersistenceCircuitBreaker__halted_reason", None)

    with pytest.raises(PersistenceUnavailable):
        ledger.prepare(_entry("revoked-capability-entry", "revoked-capability-plan"))
    with pytest.raises(PersistenceUnavailable, match="NOT_FRESH"):
        ledger.issue_entry_authorization_capability(
            evidence=evidence,
            reconciliation_snapshot=snapshot,
        )

    engine.dispose()


def test_durable_authorization_rejects_audit_head_change_after_grant(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'changed-audit-head.sqlite'}")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    breaker = PersistenceCircuitBreaker()
    ledger = DurableIntentLedger(session_factory, persistence_breaker=breaker)
    repository = AuditRepository(session_factory, persistence_breaker=breaker)
    snapshot = exchange_reconciliation_batch(
        ReconciliationSnapshot(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
        )
    )
    breaker.reset_after_verified_reconciliation(
        audit_repository=repository,
        intent_ledger=ledger,
        reconciliation_snapshot=snapshot,
    )
    repository.record_delivery(
        event_id="post-grant-audit-change",
        source="test",
        event_type="non_authorizing_observation",
        occurred_at=datetime(2026, 7, 15, tzinfo=UTC),
        payload={"purpose": "invalidate stale authorization"},
    )

    with pytest.raises(PersistenceUnavailable, match="AUDIT_REPLAY_CHANGED"):
        ledger.prepare(_entry("changed-head-entry", "changed-head-plan"))
    engine.dispose()


def test_durable_authorization_rejects_new_unresolved_intent(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'unresolved-after-grant.sqlite'}")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    breaker = PersistenceCircuitBreaker()
    ledger = DurableIntentLedger(session_factory, persistence_breaker=breaker)
    repository = AuditRepository(session_factory, persistence_breaker=breaker)
    breaker.reset_after_verified_reconciliation(
        audit_repository=repository,
        intent_ledger=ledger,
        reconciliation_snapshot=exchange_reconciliation_batch(
            ReconciliationSnapshot(
                positions_by_symbol={},
                normal_order_client_ids=frozenset(),
                algo_order_client_ids=frozenset(),
            )
        ),
    )
    ledger.prepare(_entry("first-unresolved-entry", "first-unresolved-plan"))

    with pytest.raises(PersistenceUnavailable, match="UNRESOLVED_INTENT_PRESENT"):
        ledger.prepare(_entry("second-unresolved-entry", "second-unresolved-plan"))
    engine.dispose()


def _candle(index: int, close: Decimal) -> Candle:
    return Candle(
        symbol="BTCUSDT",
        open_time_ms=index * 60_000,
        close_time_ms=(index + 1) * 60_000 - 1,
        open_price=close,
        high_price=close + Decimal("1"),
        low_price=close - Decimal("1"),
        close_price=close,
        volume=Decimal("10"),
        timeframe="1m",
    )


def test_walk_forward_fit_fingerprint_is_invariant_to_held_out_mutation() -> None:
    train = tuple(
        _candle(index, close)
        for index, close in enumerate(
            (Decimal("100"), Decimal("101"), Decimal("102"), Decimal("103"))
        )
    )
    held_out_a = (
        _candle(4, Decimal("104")),
        _candle(5, Decimal("105")),
    )
    held_out_b = (
        _candle(4, Decimal("80")),
        _candle(5, Decimal("120")),
    )
    runner = WalkForwardRunner(train_size=4, test_size=2, step_size=2)
    specification = StrategySpecification.mean_reversion(
        lookback=3,
        deviation_percent=Decimal("1.5"),
    )
    costs = BacktestCosts(
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        slippage_bps=Decimal("0"),
    )

    first = runner.run(
        specification,
        (*train, *held_out_a),
        timeframe="1m",
        costs=costs,
    )
    second = runner.run(
        specification,
        (*train, *held_out_b),
        timeframe="1m",
        costs=costs,
    )

    assert first[0].configuration_fingerprint == specification.fingerprint
    assert second[0].configuration_fingerprint == specification.fingerprint
    assert first[0].training_data_fingerprint == second[0].training_data_fingerprint
