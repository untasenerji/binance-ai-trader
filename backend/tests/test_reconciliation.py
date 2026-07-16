from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.types import Direction
from app.exchange.contracts import (
    AlgoOrderStatus,
    AlgoOrderType,
    ExchangeAlgoOrderObservation,
    ExpectedStopContract,
    LocalReconciliationState,
    OrderSide,
    PositionAmount,
    PositionQuantityMismatch,
    ReconciliationReasonCode,
    ReconciliationSnapshot,
    StopQuantitySemantics,
    StopWorkingType,
    reconcile_local_state,
)
from app.simulation.failure import FailureCoordinator
from app.simulation.models import SimulatedFault


def _local_state(
    *,
    positions_by_symbol: dict[str, Decimal] | None = None,
    normal_order_client_ids: frozenset[str] = frozenset(),
    algo_order_client_ids: frozenset[str] = frozenset(),
    required_stop_symbols: frozenset[str] = frozenset(),
    unresolved_unknown_intent_ids: frozenset[str] = frozenset(),
    audit_chain_valid: bool = True,
    replay_valid: bool = True,
    expected_stop_contracts: tuple[ExpectedStopContract, ...] = (),
) -> LocalReconciliationState:
    return LocalReconciliationState(
        positions_by_symbol=positions_by_symbol or {},
        normal_order_client_ids=normal_order_client_ids,
        algo_order_client_ids=algo_order_client_ids,
        required_stop_symbols=required_stop_symbols,
        unresolved_unknown_intent_ids=unresolved_unknown_intent_ids,
        audit_chain_valid=audit_chain_valid,
        replay_valid=replay_valid,
        expected_stop_contracts=expected_stop_contracts,
    )


def _snapshot(
    *,
    positions_by_symbol: dict[str, Decimal] | None = None,
    normal_order_client_ids: frozenset[str] = frozenset(),
    algo_orders: tuple[ExchangeAlgoOrderObservation, ...] = (),
    stop_protected_symbols: frozenset[str] = frozenset(),
) -> ReconciliationSnapshot:
    return ReconciliationSnapshot(
        positions_by_symbol=positions_by_symbol or {},
        normal_order_client_ids=normal_order_client_ids,
        algo_order_client_ids=frozenset(order.client_algo_id for order in algo_orders),
        algo_orders=algo_orders,
        stop_protected_symbols=stop_protected_symbols,
    )


def _stop_contract(
    client_algo_id: str,
    *,
    symbol: str = "BTCUSDT",
) -> ExpectedStopContract:
    return ExpectedStopContract(
        plan_id="reconciliation-plan",
        symbol=symbol,
        position_side=Direction.LONG,
        expected_order_side=OrderSide.SELL,
        client_algo_id=client_algo_id,
        algo_type=AlgoOrderType.STOP_MARKET,
        trigger_price=Decimal("90"),
        working_type=StopWorkingType.MARK_PRICE,
        close_position=True,
        quantity_semantics=StopQuantitySemantics.CLOSE_POSITION_FULL,
        active_status=AlgoOrderStatus.NEW,
        policy_version=1,
        account_envelope_version=1,
        policy_fingerprint="a" * 64,
        account_envelope_fingerprint="b" * 64,
    )


def _algo_order(
    client_algo_id: str,
    *,
    symbol: str = "BTCUSDT",
    contract: ExpectedStopContract | None = None,
) -> ExchangeAlgoOrderObservation:
    contract = contract or _stop_contract(client_algo_id, symbol=symbol)
    observed_at = datetime.now(UTC)
    return ExchangeAlgoOrderObservation(
        source="authenticated_exchange_adapter",
        account_id=contract.account_id,
        fetched_at=observed_at,
        server_time=observed_at,
        freshness_window=timedelta(seconds=30),
        correlation_id=f"reconciliation-{client_algo_id}",
        client_algo_id=client_algo_id,
        symbol=symbol,
        direction=Direction.LONG,
        algo_type=AlgoOrderType.STOP_MARKET,
        trigger_price=Decimal("90"),
        close_position=True,
        working_type=contract.working_type,
        status=contract.active_status,
        plan_id=contract.plan_id,
        policy_version=contract.policy_version,
        account_envelope_version=contract.account_envelope_version,
        policy_fingerprint=contract.policy_fingerprint,
        account_envelope_fingerprint=contract.account_envelope_fingerprint,
        stop_contract_fingerprint=contract.fingerprint,
    )


def test_normal_and_algo_namespaces_are_compared_independently() -> None:
    outcome = reconcile_local_state(
        local=_local_state(
            normal_order_client_ids=frozenset({"normal-1"}),
            algo_order_client_ids=frozenset({"algo-1"}),
        ),
        snapshot=_snapshot(
            normal_order_client_ids=frozenset({"algo-1"}),
            algo_orders=(_algo_order("normal-1"),),
        ),
    )

    assert not outcome.is_clean
    assert outcome.missing_normal_order_ids == ("normal-1",)
    assert outcome.unexpected_normal_order_ids == ("algo-1",)
    assert outcome.missing_algo_order_ids == ("algo-1",)
    assert outcome.unexpected_algo_order_ids == ("normal-1",)
    assert outcome.reason_codes == (
        ReconciliationReasonCode.MISSING_NORMAL_ORDER,
        ReconciliationReasonCode.UNEXPECTED_NORMAL_ORDER,
        ReconciliationReasonCode.MISSING_ALGO_ORDER,
        ReconciliationReasonCode.UNEXPECTED_ALGO_ORDER,
    )


def test_positions_stops_unknowns_and_audit_health_each_keep_a_typed_reason() -> None:
    outcome = reconcile_local_state(
        local=_local_state(
            positions_by_symbol={"BTCUSDT": Decimal("1"), "ETHUSDT": Decimal("-2")},
            required_stop_symbols=frozenset({"BTCUSDT"}),
            unresolved_unknown_intent_ids=frozenset({"intent-unknown"}),
            audit_chain_valid=False,
            replay_valid=False,
        ),
        snapshot=_snapshot(
            positions_by_symbol={
                "BTCUSDT": Decimal("0.5"),
                "ETHUSDT": Decimal("0"),
                "XRPUSDT": Decimal("0.1"),
            },
        ),
    )

    assert outcome.position_quantity_mismatches == (
        PositionQuantityMismatch(
            symbol="BTCUSDT", expected_quantity=Decimal("1"), exchange_quantity=Decimal("0.5")
        ),
    )
    assert outcome.missing_expected_positions == (
        PositionAmount(symbol="ETHUSDT", quantity=Decimal("-2")),
    )
    assert outcome.unexpected_exchange_positions == (
        PositionAmount(symbol="XRPUSDT", quantity=Decimal("0.1")),
    )
    assert outcome.missing_stop_symbols == ("BTCUSDT",)
    assert outcome.unresolved_unknown_intent_ids == ("intent-unknown",)
    assert ReconciliationReasonCode.AUDIT_CHAIN_INVALID in outcome.reason_codes
    assert ReconciliationReasonCode.REPLAY_INVALID in outcome.reason_codes


def test_only_a_fully_clean_snapshot_can_release_failure_coordinator_pause() -> None:
    coordinator = FailureCoordinator()
    coordinator.handle(SimulatedFault.UNKNOWN_503)
    with pytest.raises(TypeError):
        coordinator.mark_reconciled()  # type: ignore[call-arg]
    dirty = reconcile_local_state(
        local=_local_state(unresolved_unknown_intent_ids=frozenset({"intent-unknown"})),
        snapshot=_snapshot(),
    )

    assert not coordinator.mark_reconciled(dirty)
    assert coordinator.new_entries_paused
    assert coordinator.reconciliation_required

    contract = _stop_contract("algo-1")
    clean = reconcile_local_state(
        local=_local_state(
            positions_by_symbol={"BTCUSDT": Decimal("1")},
            normal_order_client_ids=frozenset({"normal-1"}),
            algo_order_client_ids=frozenset({"algo-1"}),
            required_stop_symbols=frozenset({"BTCUSDT"}),
            expected_stop_contracts=(contract,),
        ),
        snapshot=_snapshot(
            positions_by_symbol={"BTCUSDT": Decimal("1")},
            normal_order_client_ids=frozenset({"normal-1"}),
            algo_orders=(_algo_order("algo-1", contract=contract),),
        ),
    )

    assert clean.is_clean
    assert coordinator.mark_reconciled(clean)
    assert (coordinator.new_entries_paused, coordinator.reconciliation_required) == (False, False)
