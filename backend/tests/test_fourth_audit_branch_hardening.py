"""Target risky validation branches called out by the fourth audit."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest

from app.domain.types import Direction
from app.exchange.contracts import (
    AlgoOrderIntent,
    AlgoOrderType,
    ExchangeProtectionEvidence,
    LiveProtectionEvidenceGate,
    LocalReconciliationState,
    NormalOrderIntent,
    NormalOrderRole,
    NormalOrderType,
    OrderSide,
    PositionAmount,
    ReconciliationSnapshot,
)
from app.exchange.contracts import (
    PositionQuantityMismatch as ReconciliationPositionMismatch,
)
from app.persistence.audit import AuditRepository
from app.persistence.recovery_service import PersistenceRecoveryService
from app.planning.fills import (
    FillEvent,
    FillLedgerError,
    PositionQuantityMismatch,
    evaluate_simulated_position_risk,
)
from app.simulation.intent_ledger import (
    AbsenceEvidenceSource,
    BoundedAbsenceEvidence,
    ClientOrderNamespace,
    DurableIntentLedger,
    UnknownIntentObservation,
)


def _normal_order(**overrides: object) -> NormalOrderIntent:
    values: dict[str, object] = {
        "client_order_id": "normal-branch",
        "symbol": "BTCUSDT",
        "direction": Direction.LONG,
        "order_type": NormalOrderType.LIMIT,
        "quantity": Decimal("0.01"),
        "price": Decimal("100"),
        "role": NormalOrderRole.ENTRY,
        "reduce_only": False,
    }
    values.update(overrides)
    return NormalOrderIntent(**cast(Any, values))


@pytest.mark.parametrize(
    "overrides",
    (
        {"client_order_id": ""},
        {"quantity": Decimal("0")},
        {"reduce_only": True},
        {"role": NormalOrderRole.TAKE_PROFIT},
        {
            "role": NormalOrderRole.EMERGENCY_REDUCE,
            "order_type": NormalOrderType.LIMIT,
        },
    ),
)
def test_normal_order_contract_rejects_unsafe_role_combinations(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _normal_order(**overrides)


def test_normal_order_side_is_directional_for_entries_and_reductions() -> None:
    assert _normal_order().side is OrderSide.BUY
    assert _normal_order(direction=Direction.SHORT).side is OrderSide.SELL
    assert (
        _normal_order(
            direction=Direction.LONG,
            role=NormalOrderRole.TAKE_PROFIT,
            order_type=NormalOrderType.REDUCE_ONLY_LIMIT,
            reduce_only=True,
        ).side
        is OrderSide.SELL
    )
    assert (
        _normal_order(
            direction=Direction.SHORT,
            role=NormalOrderRole.TAKE_PROFIT,
            order_type=NormalOrderType.REDUCE_ONLY_LIMIT,
            reduce_only=True,
        ).side
        is OrderSide.BUY
    )


def _algo(**overrides: object) -> AlgoOrderIntent:
    values: dict[str, object] = {
        "client_algo_id": "algo-branch",
        "symbol": "BTCUSDT",
        "direction": Direction.LONG,
        "algo_type": AlgoOrderType.STOP_MARKET,
        "trigger_price": Decimal("90"),
        "close_position": True,
        "quantity": None,
    }
    values.update(overrides)
    return AlgoOrderIntent(**cast(Any, values))


@pytest.mark.parametrize(
    "overrides",
    (
        {"client_algo_id": ""},
        {"quantity": Decimal("NaN"), "algo_type": AlgoOrderType.TAKE_PROFIT_MARKET},
        {"close_position": False},
        {"quantity": Decimal("0.01")},
    ),
)
def test_algo_contract_rejects_invalid_protective_records(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _algo(**overrides)


def test_exchange_evidence_gate_accepts_only_complete_side_correct_evidence() -> None:
    stop = _algo()
    evidence = ExchangeProtectionEvidence(
        plan_id="plan-branch",
        symbol="BTCUSDT",
        direction=Direction.LONG,
        position_quantity=Decimal("0.01"),
        stop_order=stop,
        reduce_only_exit_reference="reduce-branch",
        position_observed_at_ms=1,
        protection_observed_at_ms=2,
    )

    assert stop.side is OrderSide.SELL
    assert _algo(direction=Direction.SHORT).side is OrderSide.BUY
    assert LiveProtectionEvidenceGate().require(evidence) is evidence
    with pytest.raises(ValueError, match="identifiers"):
        ExchangeProtectionEvidence(
            plan_id="",
            symbol="BTCUSDT",
            direction=Direction.LONG,
            position_quantity=Decimal("0.01"),
            stop_order=stop,
            reduce_only_exit_reference="reduce-branch",
            position_observed_at_ms=1,
            protection_observed_at_ms=2,
        )
    with pytest.raises(ValueError, match="position quantity"):
        ExchangeProtectionEvidence(
            plan_id="plan-branch",
            symbol="BTCUSDT",
            direction=Direction.LONG,
            position_quantity=Decimal("0"),
            stop_order=stop,
            reduce_only_exit_reference="reduce-branch",
            position_observed_at_ms=1,
            protection_observed_at_ms=2,
        )
    with pytest.raises(ValueError, match="does not protect"):
        ExchangeProtectionEvidence(
            plan_id="plan-branch",
            symbol="ETHUSDT",
            direction=Direction.LONG,
            position_quantity=Decimal("0.01"),
            stop_order=stop,
            reduce_only_exit_reference="reduce-branch",
            position_observed_at_ms=1,
            protection_observed_at_ms=2,
        )
    with pytest.raises(ValueError, match="timestamps"):
        ExchangeProtectionEvidence(
            plan_id="plan-branch",
            symbol="BTCUSDT",
            direction=Direction.LONG,
            position_quantity=Decimal("0.01"),
            stop_order=stop,
            reduce_only_exit_reference="reduce-branch",
            position_observed_at_ms=-1,
            protection_observed_at_ms=2,
        )


def test_reconciliation_records_reject_ambiguous_or_untyped_content() -> None:
    with pytest.raises(ValueError, match="symbol"):
        PositionAmount(symbol="", quantity=Decimal("1"))
    with pytest.raises(ValueError, match="finite"):
        PositionAmount(symbol="BTCUSDT", quantity=Decimal("NaN"))
    with pytest.raises(ValueError, match="must differ"):
        ReconciliationPositionMismatch(
            symbol="BTCUSDT",
            expected_quantity=Decimal("1"),
            exchange_quantity=Decimal("1"),
        )
    with pytest.raises(ValueError, match="normal order IDs"):
        ReconciliationSnapshot(
            positions_by_symbol={},
            normal_order_client_ids=frozenset({""}),
            algo_order_client_ids=frozenset(),
        )
    duplicate = (_algo(client_algo_id="duplicate"), _algo(client_algo_id="duplicate"))
    with pytest.raises(ValueError, match="duplicate"):
        ReconciliationSnapshot(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset({"duplicate"}),
            algo_orders=duplicate,
        )
    with pytest.raises(ValueError, match="must match"):
        ReconciliationSnapshot(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset({"different"}),
            algo_orders=(_algo(),),
        )
    with pytest.raises(TypeError, match="boolean"):
        LocalReconciliationState(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
            required_stop_symbols=frozenset(),
            unresolved_unknown_intent_ids=frozenset(),
            audit_chain_valid=cast(Any, 1),
            replay_valid=True,
        )
    with pytest.raises(ValueError, match="nonzero"):
        LocalReconciliationState(
            positions_by_symbol={"BTCUSDT": Decimal("0")},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
            required_stop_symbols=frozenset({"BTCUSDT"}),
            unresolved_unknown_intent_ids=frozenset(),
            audit_chain_valid=True,
            replay_valid=True,
        )


def _fill(**overrides: object) -> FillEvent:
    values: dict[str, object] = {
        "trade_id": "fill-branch",
        "client_order_id": "entry-branch",
        "last_quantity": Decimal("0.01"),
        "cumulative_quantity": Decimal("0.01"),
        "fill_price": Decimal("100"),
        "fee": Decimal("0"),
        "fee_asset": "USDT",
        "occurred_at": datetime(2026, 7, 15, tzinfo=UTC),
    }
    values.update(overrides)
    return FillEvent(**cast(Any, values))


@pytest.mark.parametrize(
    "overrides",
    (
        {"trade_id": ""},
        {"fee": cast(Any, 0)},
        {"cumulative_quantity": Decimal("0.001")},
        {"fill_price": Decimal("0")},
        {"occurred_at": datetime(2026, 7, 15)},
    ),
)
def test_fill_contract_rejects_ambiguous_financial_evidence(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(FillLedgerError):
        _fill(**overrides)


def test_position_risk_rejects_direction_mismatch_before_financial_use() -> None:
    with pytest.raises(PositionQuantityMismatch, match="long"):
        evaluate_simulated_position_risk(
            direction=Direction.LONG,
            signed_simulated_position_quantity=Decimal("-0.01"),
            fills=cast(Any, object()),
            worst_stop_exit_price=Decimal("90"),
            exit_fee_rate=Decimal("0"),
            funding_buffer_rate=Decimal("0"),
            funding_interval_count=0,
            risk_budget=Decimal("1"),
            simulated_protection_ready=True,
        )


def test_absence_contract_rejects_untyped_attempt_namespace_and_provenance() -> None:
    with pytest.raises(ValueError, match="attempt"):
        BoundedAbsenceEvidence(
            client_order_id="absence-branch",
            economic_key="economic-branch",
            first_not_found_at_ms=0,
            last_not_found_at_ms=1_000,
            not_found_observation_count=10,
            attempt_number=0,
        )
    with pytest.raises(TypeError, match="namespace"):
        BoundedAbsenceEvidence(
            client_order_id="absence-branch",
            economic_key="economic-branch",
            first_not_found_at_ms=0,
            last_not_found_at_ms=1_000,
            not_found_observation_count=10,
            client_order_namespace=cast(Any, "NORMAL"),
        )
    with pytest.raises(TypeError, match="source"):
        UnknownIntentObservation(
            source=cast(Any, "TRADE_HISTORY"),
            observed_at_ms=1,
            stream_watermark_ms=1,
            found=False,
            query_reference="query-branch",
            query_client_order_id="absence-branch",
            query_economic_key="economic-branch",
            query_started_at_ms=1,
        )
    with pytest.raises(TypeError, match="found"):
        UnknownIntentObservation(
            source=AbsenceEvidenceSource.TRADE_HISTORY,
            observed_at_ms=1,
            stream_watermark_ms=1,
            found=cast(Any, 0),
            query_reference="query-branch",
            query_client_order_id="absence-branch",
            query_economic_key="economic-branch",
            query_started_at_ms=1,
        )
    with pytest.raises(ValueError, match="fingerprint"):
        UnknownIntentObservation(
            source=AbsenceEvidenceSource.TRADE_HISTORY,
            observed_at_ms=1,
            stream_watermark_ms=1,
            found=False,
            query_reference="query-branch",
            query_client_order_id="absence-branch",
            query_economic_key="economic-branch",
            query_started_at_ms=1,
            attempt_number=1,
            client_order_namespace=ClientOrderNamespace.NORMAL,
            provenance_fingerprint="0" * 64,
        )


def test_ledger_rejects_untyped_policy_and_missing_evidence(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    with pytest.raises(TypeError, match="typed"):
        durable_intent_ledger.register_actual_risk_policy(cast(Any, object()))
    with pytest.raises(KeyError, match="simulated protection"):
        durable_intent_ledger.simulated_protection_evidence("missing-plan")
    with pytest.raises(KeyError, match="risk-reduction"):
        durable_intent_ledger.risk_reduction_requirement("missing-plan")


def test_recovery_service_rejects_substituted_inputs_and_output(
    durable_intent_ledger: DurableIntentLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = AuditRepository(
        durable_intent_ledger._session_factory,  # noqa: SLF001 - exact-type boundary test
        persistence_breaker=durable_intent_ledger.persistence_breaker,
    )
    with pytest.raises(TypeError, match="concrete production audit"):
        PersistenceRecoveryService(
            audit_repository=cast(Any, object()),
            intent_ledger=durable_intent_ledger,
        )
    with pytest.raises(TypeError, match="concrete durable intent"):
        PersistenceRecoveryService(
            audit_repository=repository,
            intent_ledger=cast(Any, object()),
        )

    service = PersistenceRecoveryService(
        audit_repository=repository,
        intent_ledger=durable_intent_ledger,
    )
    with pytest.raises(TypeError, match="concrete reconciliation snapshot"):
        service.collect(cast(Any, object()))

    def invalid_evidence(*args: object, **kwargs: object) -> object:
        return object()

    monkeypatch.setattr(
        AuditRepository,
        "collect_persistence_recovery_evidence",
        invalid_evidence,
    )
    with pytest.raises(TypeError, match="invalid persistence evidence"):
        service.collect(
            ReconciliationSnapshot(
                positions_by_symbol={},
                normal_order_client_ids=frozenset(),
                algo_order_client_ids=frozenset(),
            )
        )
