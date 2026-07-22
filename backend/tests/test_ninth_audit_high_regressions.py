"""Red-first regressions for the ninth independent audit release blockers."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypedDict, cast

import pytest
from sqlalchemy import select

from app.domain.types import Direction
from app.exchange.contracts import (
    AdapterQueryReceiptStatus,
    ReconciliationSnapshot,
)
from app.persistence.audit import AuditRepository
from app.persistence.circuit_breaker import PersistenceCircuitBreaker, PersistenceUnavailable
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.persistence.models import ExchangeFillFactJournal
from app.planning.fills import FillEvent, FillLedgerError, FillObservationSource, FillSide
from app.simulation.intent_ledger import (
    _ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY,
    DurableIntentLedger,
    DurableIntentStatus,
    VerifiedQuarantineResolutionEvidence,
)
from app.simulation.models import OrderRole, SimulatedOrderIntent
from app.simulation.simulator import (
    DurableIntentLedgerRequired,
    ExchangeSimulator,
    SpreadSlippageModel,
)
from app.strategy.models import FrozenStrategy, StrategyFitResult, StrategySpecification
from app.strategy.registry import StrategyImplementationRegistry
from app.strategy.strategies import frozen_strategy_from_fit
from tests.conftest import actual_risk_policy_for
from tests.reconciliation_factory import (
    exchange_reconciliation_batch,
    persist_reconciliation_query_receipt,
)

ACCOUNT_ID = "v1-primary"


class _QuarantineCommon(TypedDict):
    account_id: str
    economic_key: str
    provenance_fingerprint: str
    reason: str


def _entry(
    client_order_id: str,
    plan_id: str,
    *,
    quantity: Decimal = Decimal("0.02"),
    direction: Direction = Direction.LONG,
    price: Decimal = Decimal("100"),
) -> SimulatedOrderIntent:
    return SimulatedOrderIntent(
        account_id=ACCOUNT_ID,
        client_order_id=client_order_id,
        plan_id=plan_id,
        symbol="BTCUSDT",
        direction=direction,
        role=OrderRole.ENTRY,
        stage_index=1,
        quantity=quantity,
        price=price,
    )


def _fill(
    intent: SimulatedOrderIntent,
    trade_id: str,
    *,
    last_quantity: Decimal | None = None,
    cumulative_quantity: Decimal | None = None,
    fill_price: Decimal = Decimal("100"),
    fee: Decimal = Decimal("0.01"),
) -> FillEvent:
    quantity = intent.quantity if last_quantity is None else last_quantity
    return FillEvent(
        account_id=ACCOUNT_ID,
        trade_id=trade_id,
        client_order_id=intent.client_order_id,
        symbol=intent.symbol,
        side=FillSide.BUY if intent.direction is Direction.LONG else FillSide.SELL,
        last_quantity=quantity,
        cumulative_quantity=quantity if cumulative_quantity is None else cumulative_quantity,
        fill_price=fill_price,
        fee=fee,
        fee_asset="USDT",
        occurred_at=datetime.now(UTC),
        observation_source=FillObservationSource.SIMULATED_EXCHANGE,
        observation_reference=f"ninth:{trade_id}",
    )


def _reconciled_ledger(tmp_path: Path) -> DurableIntentLedger:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'ninth-audit.sqlite'}")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    breaker = PersistenceCircuitBreaker()
    ledger = DurableIntentLedger(session_factory, persistence_breaker=breaker)
    repository = AuditRepository(session_factory, persistence_breaker=breaker)
    batch = exchange_reconciliation_batch(
        ReconciliationSnapshot(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
        )
    )
    persist_reconciliation_query_receipt(ledger, batch)
    breaker.reset_after_verified_reconciliation(
        audit_repository=repository,
        intent_ledger=ledger,
        reconciliation_snapshot=batch,
    )
    return ledger


def test_runtime_failure_never_erases_an_accepted_fill_fact_and_restart_reapplies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _reconciled_ledger(tmp_path)
    plan_id = "ninth-fill-runtime"
    intent = _entry("ninth-fill-runtime-entry", plan_id)
    ExchangeSimulator(
        intent_ledger=ledger,
        actual_risk_policy=actual_risk_policy_for(plan_id),
    ).submit(intent)
    event = _fill(intent, "ninth-fill-runtime-trade")
    original_checkpoint = DurableIntentLedger._fill_uow_checkpoint

    def crash_after_stage_a(phase: str) -> None:
        if phase == "after_fill_persisted":
            raise RuntimeError("ninth audit injected application crash")
        original_checkpoint(phase)

    monkeypatch.setattr(
        DurableIntentLedger, "_fill_uow_checkpoint", staticmethod(crash_after_stage_a)
    )
    with pytest.raises(RuntimeError, match="application crash"):
        ledger.record_fill(event)

    with ledger._session_factory() as session:  # noqa: SLF001 - durable proof
        fact = session.scalar(select(ExchangeFillFactJournal))
    assert fact is not None
    assert fact.apply_status == "RECOVERY_REQUIRED"

    monkeypatch.setattr(
        DurableIntentLedger,
        "_fill_uow_checkpoint",
        staticmethod(original_checkpoint),
    )
    restarted = ledger.reopen_after_restart()
    assert restarted.intent(intent.client_order_id).status is DurableIntentStatus.FILLED
    assert restarted.fill_ledger_for_plan(plan_id).filled_quantity == intent.quantity


def test_stage_a_claim_failure_retains_the_fact_and_closes_the_entry_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _reconciled_ledger(tmp_path)
    plan_id = "ninth-fill-claim-failure"
    intent = _entry("ninth-fill-claim-failure-entry", plan_id)
    ExchangeSimulator(
        intent_ledger=ledger,
        actual_risk_policy=actual_risk_policy_for(plan_id),
    ).submit(intent)

    def fail_claim(_fact_id: int) -> None:
        raise RuntimeError("ninth audit injected stage-b claim failure")

    monkeypatch.setattr(ledger, "_claim_fill_fact_application", fail_claim)
    with pytest.raises(RuntimeError, match="stage-b claim failure"):
        ledger.record_fill(_fill(intent, "ninth-fill-claim-failure-trade"))

    with ledger._session_factory() as session:  # noqa: SLF001 - durable failure proof
        fact = session.scalar(select(ExchangeFillFactJournal))
    assert fact is not None
    assert fact.apply_status == "RECOVERY_REQUIRED"

    blocked_intent = _entry(
        "ninth-fill-claim-failure-blocked-entry",
        "ninth-fill-claim-failure-blocked",
    )
    ledger.register_actual_risk_policy(actual_risk_policy_for(blocked_intent.plan_id))
    with pytest.raises(PersistenceUnavailable, match="REVOKED|ENTRY_ADMISSION"):
        ledger.admit_entry(blocked_intent)


def test_entry_prepare_requires_a_durable_central_admission_decision(tmp_path: Path) -> None:
    ledger = _reconciled_ledger(tmp_path)
    plan_id = "ninth-admission"
    intent = _entry("ninth-admission-entry", plan_id)
    ledger.register_actual_risk_policy(actual_risk_policy_for(plan_id))

    with pytest.raises(PersistenceUnavailable, match="ENTRY_ADMISSION"):
        ledger.prepare(intent)

    decision = ledger.admit_entry(intent)
    with pytest.raises(PersistenceUnavailable, match="ADMISSION"):
        ledger.prepare(replace(intent, quantity=Decimal("999")), admission_decision=decision)
    assert (
        ledger.prepare(intent, admission_decision=decision).status is DurableIntentStatus.PREPARED
    )


def test_invalid_execution_inputs_and_unbacked_risk_configuration_fail_closed() -> None:
    """Execution facts and risk configuration must remain typed and durably backed."""
    intent = _entry("ninth-invalid-input-entry", "ninth-invalid-input")
    with pytest.raises(FillLedgerError, match="typed exchange side"):
        replace(_fill(intent, "ninth-invalid-input-trade"), side=cast(Any, "BUY"))
    with pytest.raises(DurableIntentLedgerRequired, match="durable intent ledger"):
        ExchangeSimulator(actual_risk_policy=actual_risk_policy_for("ninth-unbacked-risk"))
    with pytest.raises(ValueError, match="spread and slippage"):
        SpreadSlippageModel(bid_price=Decimal("101"), ask_price=Decimal("100"))


def test_local_authenticated_text_is_not_reconciliation_authority_without_receipt(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'ninth-receipt.sqlite'}")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    breaker = PersistenceCircuitBreaker()
    ledger = DurableIntentLedger(session_factory, persistence_breaker=breaker)
    repository = AuditRepository(session_factory, persistence_breaker=breaker)
    batch = exchange_reconciliation_batch(
        ReconciliationSnapshot(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
        )
    )

    with pytest.raises(PersistenceUnavailable, match="RECOVERY_EVIDENCE_COLLECTION_FAILED"):
        breaker.reset_after_verified_reconciliation(
            audit_repository=repository,
            intent_ledger=ledger,
            reconciliation_snapshot=batch,
        )

    assert batch.query_receipt is not None
    with pytest.raises(PersistenceUnavailable, match="PUBLIC_WRITE_FORBIDDEN"):
        ledger.begin_adapter_query_receipt(batch.query_receipt.started_record())
    with pytest.raises(PersistenceUnavailable, match="PUBLIC_WRITE_FORBIDDEN"):
        ledger.complete_adapter_query_receipt(batch.query_receipt)
    with pytest.raises(TypeError, match="authenticated adapter boundary"):
        ledger._record_adapter_query_receipt_from_authenticated_adapter(  # noqa: SLF001
            batch.query_receipt.started_record(),
            capability=object(),
        )
    persist_reconciliation_query_receipt(ledger, batch)
    assert breaker.reset_after_verified_reconciliation(
        audit_repository=repository,
        intent_ledger=ledger,
        reconciliation_snapshot=batch,
    ).is_complete

    restarted = ledger.reopen_after_restart()
    restarted.validate_reconciliation_observation(batch)


def test_adapter_receipt_lifecycle_is_idempotent_and_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    """Only the trusted test adapter boundary can persist coherent receipt transitions."""
    engine = create_database_engine(f"sqlite:///{tmp_path / 'ninth-receipt-lifecycle.sqlite'}")
    create_schema(engine)
    ledger = DurableIntentLedger(create_session_factory(engine))
    snapshot = ReconciliationSnapshot(
        positions_by_symbol={},
        normal_order_client_ids=frozenset(),
        algo_order_client_ids=frozenset(),
    )
    batch = exchange_reconciliation_batch(snapshot)
    assert batch.query_receipt is not None
    receipt = batch.query_receipt
    started = receipt.started_record()

    with pytest.raises(PersistenceUnavailable, match="PUBLIC_WRITE_FORBIDDEN"):
        ledger.record_adapter_query_receipt(receipt)
    with pytest.raises(PersistenceUnavailable, match="ACCOUNT_MISMATCH"):
        ledger._record_adapter_query_receipt_from_authenticated_adapter(  # noqa: SLF001
            replace(started, account_id="another-account", receipt_fingerprint=""),
            capability=_ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY,
        )

    ledger._record_adapter_query_receipt_from_authenticated_adapter(  # noqa: SLF001
        started,
        capability=_ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY,
    )
    ledger._record_adapter_query_receipt_from_authenticated_adapter(  # noqa: SLF001
        started,
        capability=_ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY,
    )
    with pytest.raises(PersistenceUnavailable, match="SEMANTIC_CONFLICT"):
        ledger._record_adapter_query_receipt_from_authenticated_adapter(  # noqa: SLF001
            replace(started, correlation_id="conflicting-correlation", receipt_fingerprint=""),
            capability=_ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY,
        )

    ledger._record_adapter_query_receipt_from_authenticated_adapter(  # noqa: SLF001
        receipt,
        capability=_ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY,
    )
    ledger._record_adapter_query_receipt_from_authenticated_adapter(  # noqa: SLF001
        receipt,
        capability=_ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY,
    )
    with pytest.raises(PersistenceUnavailable, match="SEMANTIC_CONFLICT"):
        ledger._record_adapter_query_receipt_from_authenticated_adapter(  # noqa: SLF001
            replace(receipt, response_fingerprint="e" * 64, receipt_fingerprint=""),
            capability=_ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY,
        )

    missing_start = exchange_reconciliation_batch(snapshot)
    assert missing_start.query_receipt is not None
    with pytest.raises(PersistenceUnavailable, match="START_MISSING"):
        ledger._record_adapter_query_receipt_from_authenticated_adapter(  # noqa: SLF001
            missing_start.query_receipt,
            capability=_ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY,
        )

    failed_batch = exchange_reconciliation_batch(snapshot)
    assert failed_batch.query_receipt is not None
    failed_completion = failed_batch.query_receipt
    failed_start = failed_completion.started_record()
    failed_receipt = replace(
        failed_start,
        status=AdapterQueryReceiptStatus.FAILED,
        receipt_fingerprint="",
    )
    ledger._record_adapter_query_receipt_from_authenticated_adapter(  # noqa: SLF001
        failed_start,
        capability=_ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY,
    )
    ledger._record_adapter_query_receipt_from_authenticated_adapter(  # noqa: SLF001
        failed_receipt,
        capability=_ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY,
    )
    ledger._record_adapter_query_receipt_from_authenticated_adapter(  # noqa: SLF001
        failed_receipt,
        capability=_ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY,
    )
    with pytest.raises(PersistenceUnavailable, match="STATUS_INVALID"):
        ledger._record_adapter_query_receipt_from_authenticated_adapter(  # noqa: SLF001
            failed_completion,
            capability=_ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY,
        )


def test_each_quarantine_source_needs_its_own_attempt_bound_resolution(tmp_path: Path) -> None:
    ledger = _reconciled_ledger(tmp_path)
    common: _QuarantineCommon = {
        "account_id": ACCOUNT_ID,
        "economic_key": "ninth-quarantine:BTCUSDT:LONG:ENTRY:1",
        "provenance_fingerprint": "a" * 64,
        "reason": "NINTH_AUDIT",
    }
    quarantine_id = ledger.record_evidence_quarantine(
        client_order_id="ninth-quarantine-entry",
        **common,
        query_reference="ninth-query-a",
        attempt_id="attempt-a",
    )
    assert (
        ledger.record_evidence_quarantine(
            client_order_id="ninth-quarantine-entry",
            **common,
            query_reference="ninth-query-b",
            attempt_id="attempt-b",
        )
        == quarantine_id
    )
    evidence_a = VerifiedQuarantineResolutionEvidence(
        account_id=ACCOUNT_ID,
        economic_key=common["economic_key"],
        client_order_id="ninth-quarantine-entry",
        attempt_id="attempt-a",
        query_reference="ninth-resolution-a",
        observed_at=datetime.now(UTC),
    )
    ledger.resolve_evidence_quarantine(
        quarantine_id,
        operator_id="ninth-operator",
        verified_evidence=evidence_a,
    )
    with ledger._session_factory() as session:  # noqa: SLF001 - durable proof
        assert ledger._unresolved_quarantine_count(session, account_id=ACCOUNT_ID) == 1  # noqa: SLF001


def test_0014_repairs_a_missing_postgresql_fill_guard_without_touching_0013() -> None:
    """The real PostgreSQL acceptance suite exercises trigger DDL and failure/retry."""

    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0014_durable_execution_facts.py"
    )
    assert migration.exists()
    assert "0013_execution_safety_core" in migration.read_text(encoding="utf-8")


def test_frozen_strategy_rejects_an_unregistered_callable_and_lineage_substitution() -> None:
    fit = StrategyFitResult(
        specification=StrategySpecification.volatility_breakout(lookback=2),
        training_data_fingerprint="b" * 64,
        training_candle_count=2,
        training_end_ms=119_999,
        timeframe="1m",
    )
    with pytest.raises(TypeError, match="registry|evaluator"):
        cast(Any, FrozenStrategy)(fit_result=fit, evaluator=lambda *_args, **_kwargs: None)

    frozen = frozen_strategy_from_fit(fit)
    assert frozen.lineage.evaluator_implementation_fingerprint


def test_strategy_registry_rejects_source_package_mutation_after_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fit = StrategyFitResult(
        specification=StrategySpecification.volatility_breakout(lookback=2),
        training_data_fingerprint="c" * 64,
        training_candle_count=2,
        training_end_ms=119_999,
        timeframe="1m",
    )
    monkeypatch.setattr("app.strategy.registry._source_package_fingerprint", lambda: "f" * 64)
    with pytest.raises(ValueError, match="source package"):
        StrategyImplementationRegistry.verify_fit_result(fit)


@pytest.mark.parametrize(
    "error_type",
    (RuntimeError, ValueError, InvalidOperation, OSError),
    ids=("runtime", "validation", "decimal", "repository"),
)
def test_stage_a_retains_every_unexpected_apply_failure_for_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    """No Stage-B error class may erase a committed exchange fill fact."""
    ledger = _reconciled_ledger(tmp_path)
    plan_id = f"ninth-stage-a-{error_type.__name__.lower()}"
    intent = _entry(f"{plan_id}-entry", plan_id)
    ExchangeSimulator(
        intent_ledger=ledger,
        actual_risk_policy=actual_risk_policy_for(plan_id),
    ).submit(intent)
    event = _fill(intent, f"{plan_id}-trade")
    original_apply = ledger._record_fill_locked  # noqa: SLF001 - injected durable failure

    def fail_apply(*_args: object, **_kwargs: object) -> None:
        raise error_type("ninth audit stage-b failure")

    monkeypatch.setattr(ledger, "_record_fill_locked", fail_apply)
    with pytest.raises(error_type):
        ledger.record_fill(event)

    with ledger._session_factory() as session:  # noqa: SLF001 - durable Stage-A proof
        fact = session.scalar(
            select(ExchangeFillFactJournal).where(
                ExchangeFillFactJournal.exchange_trade_id == event.trade_id
            )
        )
    assert fact is not None
    assert fact.apply_status == "RECOVERY_REQUIRED"
    assert fact.apply_attempt_count == 1

    monkeypatch.setattr(ledger, "_record_fill_locked", original_apply)
    restarted = ledger.reopen_after_restart()
    assert restarted.fill_ledger_for_plan(plan_id).filled_quantity == intent.quantity


@pytest.mark.parametrize("failure_seam", ("protection", "risk_calculator"))
def test_fill_recovery_replays_protection_and_risk_evaluator_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_seam: str,
) -> None:
    ledger = _reconciled_ledger(tmp_path)
    plan_id = f"ninth-{failure_seam}-failure"
    intent = _entry(f"{plan_id}-entry", plan_id)
    ExchangeSimulator(
        intent_ledger=ledger,
        actual_risk_policy=actual_risk_policy_for(plan_id),
    ).submit(intent)
    event = _fill(intent, f"{plan_id}-trade")

    def fail_evaluator(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"ninth audit {failure_seam} failure")

    original: Any
    if failure_seam == "protection":
        original = ledger._materialize_simulated_protection_in_session  # noqa: SLF001
        monkeypatch.setattr(ledger, "_materialize_simulated_protection_in_session", fail_evaluator)
    else:
        original = ledger._recalculate_actual_risk  # noqa: SLF001
        monkeypatch.setattr(ledger, "_recalculate_actual_risk", fail_evaluator)

    with pytest.raises(RuntimeError, match=failure_seam):
        ledger.record_fill(event, materialize_simulated_protection=failure_seam == "protection")

    with ledger._session_factory() as session:  # noqa: SLF001 - durable failure proof
        fact = session.scalar(
            select(ExchangeFillFactJournal).where(
                ExchangeFillFactJournal.exchange_trade_id == event.trade_id
            )
        )
    assert fact is not None
    assert fact.apply_status == "RECOVERY_REQUIRED"

    if failure_seam == "protection":
        monkeypatch.setattr(ledger, "_materialize_simulated_protection_in_session", original)
    else:
        monkeypatch.setattr(ledger, "_recalculate_actual_risk", original)
    assert (
        ledger.reopen_after_restart().fill_ledger_for_plan(plan_id).filled_quantity
        == intent.quantity
    )


@pytest.mark.parametrize("direction", (Direction.LONG, Direction.SHORT))
def test_fill_fact_journal_preserves_partial_full_vwap_fee_and_duplicate_semantics(
    tmp_path: Path,
    direction: Direction,
) -> None:
    plan_id = f"ninth-vwap-{direction.value.lower()}"
    ledger = _reconciled_ledger(tmp_path)
    intent = _entry(
        f"{plan_id}-entry",
        plan_id,
        quantity=Decimal("0.03"),
        direction=direction,
    )
    ExchangeSimulator(
        intent_ledger=ledger,
        actual_risk_policy=actual_risk_policy_for(
            plan_id,
            direction=direction,
            worst_stop_exit_price=(Decimal("1") if direction is Direction.LONG else Decimal("200")),
        ),
    ).submit(intent)
    first = _fill(
        intent,
        f"{plan_id}-one",
        last_quantity=Decimal("0.01"),
        cumulative_quantity=Decimal("0.01"),
        fill_price=Decimal("100"),
        fee=Decimal("0.01"),
    )
    second = _fill(
        intent,
        f"{plan_id}-two",
        last_quantity=Decimal("0.02"),
        cumulative_quantity=Decimal("0.03"),
        fill_price=Decimal("102"),
        fee=Decimal("0.02"),
    )

    assert not ledger.record_fill(first).is_duplicate
    receipt = ledger.record_fill(second)
    duplicate = ledger.record_fill(second)
    expected_vwap = (Decimal("0.01") * Decimal("100") + Decimal("0.02") * Decimal("102")) / Decimal(
        "0.03"
    )

    assert receipt.filled_quantity == Decimal("0.03")
    assert receipt.average_fill_price == expected_vwap
    assert receipt.total_fee == Decimal("0.03")
    assert duplicate.is_duplicate
    rebuilt = ledger.reopen_after_restart().fill_ledger_for_plan(plan_id)
    assert rebuilt.filled_quantity == Decimal("0.03")
    assert rebuilt.average_fill_price == expected_vwap
    assert rebuilt.total_fee == Decimal("0.03")


def test_fill_fact_semantic_conflict_is_durable_and_closes_new_entry_admission(
    tmp_path: Path,
) -> None:
    ledger = _reconciled_ledger(tmp_path)
    plan_id = "ninth-fill-semantic-conflict"
    intent = _entry(f"{plan_id}-entry", plan_id)
    ExchangeSimulator(
        intent_ledger=ledger,
        actual_risk_policy=actual_risk_policy_for(plan_id),
    ).submit(intent)
    accepted = _fill(intent, f"{plan_id}-trade")
    ledger.record_fill(accepted)

    with pytest.raises(Exception, match="semantic conflict"):
        ledger.record_fill(replace(accepted, fill_price=Decimal("101")))

    with ledger._session_factory() as session:  # noqa: SLF001 - durable conflict proof
        fact = session.scalar(
            select(ExchangeFillFactJournal).where(
                ExchangeFillFactJournal.exchange_trade_id == accepted.trade_id
            )
        )
    assert fact is not None
    assert fact.apply_status == "RECOVERY_REQUIRED"
    assert fact.last_apply_error == "SEMANTIC_CONFLICT"
    blocked = _entry(f"{plan_id}-later", f"{plan_id}-later")
    ledger.register_actual_risk_policy(actual_risk_policy_for(blocked.plan_id))
    with pytest.raises(PersistenceUnavailable):
        ledger.admit_entry(blocked)


def test_entry_admission_rejects_every_direct_prepare_bypass(tmp_path: Path) -> None:
    ledger = _reconciled_ledger(tmp_path)
    policyless = _entry("ninth-policyless-entry", "ninth-policyless")
    with pytest.raises(PersistenceUnavailable, match="POLICY_MISSING"):
        ledger.admit_entry(policyless)

    plan_id = "ninth-admission-matrix"
    policy = actual_risk_policy_for(plan_id)
    ledger.register_actual_risk_policy(policy)
    intent = _entry(f"{plan_id}-entry", plan_id)
    decision = ledger.admit_entry(intent)

    with pytest.raises(PersistenceUnavailable, match="DECISION_REQUIRED"):
        ledger.prepare(intent)
    for forged_intent in (
        replace(intent, symbol="ETHUSDT"),
        replace(intent, direction=Direction.SHORT),
        replace(intent, quantity=Decimal("999")),
    ):
        with pytest.raises(PersistenceUnavailable):
            ledger.prepare(forged_intent, admission_decision=decision)
    with pytest.raises(PersistenceUnavailable, match="UNVERIFIED"):
        ledger.prepare(
            intent,
            admission_decision=replace(
                decision,
                decision_id="forged-decision",
                decision_fingerprint="",
            ),
        )
    with pytest.raises(PersistenceUnavailable, match="EXPIRED"):
        ledger.prepare(
            intent,
            admission_decision=replace(
                decision,
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
                decision_fingerprint="",
            ),
        )

    updated_envelope = replace(policy.portfolio_envelope, version=2, fingerprint="")
    ledger.publish_portfolio_envelope_head(updated_envelope)
    with pytest.raises(PersistenceUnavailable):
        ledger.prepare(intent, admission_decision=decision)

    ledger._persistence_breaker.record_write_failure(RuntimeError("ninth old failure epoch"))  # noqa: SLF001
    with pytest.raises(PersistenceUnavailable):
        ledger.prepare(intent, admission_decision=decision)


def test_reconciliation_requires_matching_durable_receipt_not_local_adapter_text(
    tmp_path: Path,
) -> None:
    ledger = _reconciled_ledger(tmp_path)
    batch = exchange_reconciliation_batch(
        ReconciliationSnapshot(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
        )
    )
    with pytest.raises(PersistenceUnavailable, match="RECEIPT_MISSING"):
        ledger.validate_reconciliation_observation(replace(batch, query_receipt=None))

    persist_reconciliation_query_receipt(ledger, batch)
    ledger.validate_reconciliation_observation(batch)

    for attribute, value in (
        ("correlation_id", "forged-correlation"),
        ("account_id", "other-account"),
        ("response_fingerprint", "f" * 64),
    ):
        mutated = replace(batch)
        object.__setattr__(mutated, attribute, value)
        with pytest.raises(PersistenceUnavailable):
            ledger.validate_reconciliation_observation(mutated)

    incomplete = replace(batch)
    assert batch.query_receipt is not None
    object.__setattr__(incomplete, "query_receipt", batch.query_receipt.started_record())
    with pytest.raises(PersistenceUnavailable, match="INCOMPLETE"):
        ledger.validate_reconciliation_observation(incomplete)
    with pytest.raises(TypeError, match="exact observation batch"):
        ledger.validate_reconciliation_observation(
            ReconciliationSnapshot(
                positions_by_symbol={},
                normal_order_client_ids=frozenset(),
                algo_order_client_ids=frozenset(),
            )  # type: ignore[arg-type]
        )


def test_quarantine_source_identity_requires_all_sources_and_survives_restart(
    tmp_path: Path,
) -> None:
    ledger = _reconciled_ledger(tmp_path)
    common: _QuarantineCommon = {
        "account_id": ACCOUNT_ID,
        "economic_key": "ninth-quarantine-matrix:BTCUSDT:LONG:ENTRY:1",
        "provenance_fingerprint": "b" * 64,
        "reason": "NINTH_AUDIT",
    }
    quarantine_id = ledger.record_evidence_quarantine(
        client_order_id="ninth-client-a",
        attempt_id="attempt-a",
        query_reference="ninth-source-a",
        **common,
    )
    assert (
        ledger.record_evidence_quarantine(
            client_order_id="ninth-client-a",
            attempt_id="attempt-b",
            query_reference="ninth-source-b",
            **common,
        )
        == quarantine_id
    )
    assert (
        ledger.record_evidence_quarantine(
            client_order_id="ninth-client-b",
            attempt_id="attempt-a",
            query_reference="ninth-source-c",
            **common,
        )
        == quarantine_id
    )

    def evidence(
        client_order_id: str, attempt_id: str, reference: str
    ) -> VerifiedQuarantineResolutionEvidence:
        return VerifiedQuarantineResolutionEvidence(
            account_id=ACCOUNT_ID,
            economic_key=common["economic_key"],
            client_order_id=client_order_id,
            attempt_id=attempt_id,
            query_reference=reference,
            observed_at=datetime.now(UTC),
        )

    with pytest.raises(ValueError, match="source identity"):
        ledger.resolve_evidence_quarantine(
            quarantine_id,
            operator_id="ninth-operator",
            verified_evidence=evidence(
                "ninth-client-a", "attempt-missing", "ninth-resolution-missing"
            ),
        )
    resolution_a = ledger.resolve_evidence_quarantine(
        quarantine_id,
        operator_id="ninth-operator",
        verified_evidence=evidence("ninth-client-a", "attempt-a", "ninth-resolution-a"),
    )
    assert (
        ledger.resolve_evidence_quarantine(
            quarantine_id,
            operator_id="ninth-operator",
            verified_evidence=evidence("ninth-client-a", "attempt-a", "ninth-resolution-a"),
        )
        == resolution_a
    )
    with ledger._session_factory() as session:  # noqa: SLF001 - source-level count proof
        assert ledger._unresolved_quarantine_count(session, account_id=ACCOUNT_ID) == 1  # noqa: SLF001

    restarted = ledger.reopen_after_restart()
    restarted.resolve_evidence_quarantine(
        quarantine_id,
        operator_id="ninth-operator",
        verified_evidence=evidence("ninth-client-a", "attempt-b", "ninth-resolution-b"),
    )
    restarted.resolve_evidence_quarantine(
        quarantine_id,
        operator_id="ninth-operator",
        verified_evidence=evidence("ninth-client-b", "attempt-a", "ninth-resolution-c"),
    )
    with restarted._session_factory() as session:  # noqa: SLF001 - all-source resolution proof
        assert restarted._unresolved_quarantine_count(session, account_id=ACCOUNT_ID) == 0  # noqa: SLF001

    restarted.record_evidence_quarantine(
        client_order_id="ninth-client-c",
        attempt_id="attempt-a",
        query_reference="ninth-source-d",
        **common,
    )
    with restarted._session_factory() as session:  # noqa: SLF001 - new source reopens denial
        assert restarted._unresolved_quarantine_count(session, account_id=ACCOUNT_ID) == 1  # noqa: SLF001
