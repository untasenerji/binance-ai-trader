from decimal import Decimal

import pytest

from app.domain.types import Direction
from app.exchange import locked_adapter
from app.exchange.contracts import (
    AlgoOrderIntent,
    AlgoOrderType,
    NormalOrderIntent,
    NormalOrderType,
    ReconciliationSnapshot,
    reconcile_known_order_ids,
)
from app.exchange.locked_adapter import (
    LIVE_TRADING_ENABLED,
    LiveTradingLockedError,
    LockedBinanceAdapter,
)


def _normal_order() -> NormalOrderIntent:
    return NormalOrderIntent(
        client_order_id="UTA1-plan-EN-1-1",
        symbol="BTCUSDT",
        direction=Direction.LONG,
        order_type=NormalOrderType.LIMIT,
        quantity=Decimal("0.01"),
        price=Decimal("1000"),
    )


def _stop_order() -> AlgoOrderIntent:
    return AlgoOrderIntent(
        client_algo_id="UTA1-plan-ST-1-1",
        symbol="BTCUSDT",
        direction=Direction.LONG,
        algo_type=AlgoOrderType.STOP_MARKET,
        trigger_price=Decimal("990"),
        close_position=True,
    )


@pytest.mark.anyio
async def test_live_adapter_is_hard_locked_for_normal_algo_test_and_stream_calls() -> None:
    adapter = LockedBinanceAdapter()

    assert LIVE_TRADING_ENABLED is False
    with pytest.raises(LiveTradingLockedError, match="Phase 14"):
        await adapter.place_normal_order(_normal_order())
    with pytest.raises(LiveTradingLockedError):
        await adapter.place_algo_order(_stop_order())
    with pytest.raises(LiveTradingLockedError):
        await adapter.test_order(_normal_order())
    with pytest.raises(LiveTradingLockedError):
        await adapter.start_user_data_stream()
    with pytest.raises(LiveTradingLockedError):
        await adapter.reconcile()


@pytest.mark.anyio
async def test_live_adapter_remains_fail_closed_when_lock_flag_is_monkeypatched_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = LockedBinanceAdapter()
    monkeypatch.setattr(locked_adapter, "LIVE_TRADING_ENABLED", True)

    with pytest.raises(LiveTradingLockedError):
        await adapter.place_normal_order(_normal_order())
    with pytest.raises(LiveTradingLockedError):
        await adapter.place_algo_order(_stop_order())
    with pytest.raises(LiveTradingLockedError):
        await adapter.test_order(_normal_order())
    with pytest.raises(LiveTradingLockedError):
        await adapter.start_user_data_stream()
    with pytest.raises(LiveTradingLockedError):
        await adapter.reconcile()


def test_reconciliation_contract_reports_exchange_local_order_mismatch() -> None:
    outcome = reconcile_known_order_ids(
        local_normal_order_ids=frozenset({"normal-1"}),
        local_algo_order_ids=frozenset({"algo-1"}),
        snapshot=ReconciliationSnapshot(
            positions_by_symbol={"BTCUSDT": Decimal("0")},
            normal_order_client_ids=frozenset({"normal-1"}),
            algo_order_client_ids=frozenset({"UTA1-plan-ST-1-1"}),
            algo_orders=(_stop_order(),),
        ),
    )

    assert not outcome.is_clean
    assert outcome.missing_local_order_ids == ("algo-1",)
    assert outcome.unexpected_exchange_order_ids == ("UTA1-plan-ST-1-1",)
