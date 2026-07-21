"""Red-first release blockers from the seventh independent audit."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from app.domain.types import Direction
from app.exchange.contracts import (
    AlgoOrderIntent,
    AlgoOrderStatus,
    AlgoOrderType,
    ExchangeAlgoOrderObservation,
    ExpectedStopContract,
    OrderSide,
    ReconciliationSnapshot,
    StopQuantitySemantics,
    StopWorkingType,
)
from app.persistence.circuit_breaker import PersistenceUnavailable
from app.planning.fills import (
    AccountPortfolioEnvelope,
    ActualRiskPolicy,
    ExposureSourceState,
    PortfolioExposureSlice,
)
from app.simulation.intent_ledger import (
    AbsenceEvidenceSource,
    DurableIntentLedger,
    DurableRiskPolicyError,
    UnknownIntentObservation,
    VerifiedQuarantineResolutionEvidence,
)
from app.simulation.models import OrderRole, SimulatedOrderIntent

ACCOUNT_ID = "v1-primary"


def _envelope(
    *,
    account_id: str = ACCOUNT_ID,
    version: int = 1,
    equity: Decimal = Decimal("100"),
    total_cap: Decimal = Decimal("100"),
    btc_cap: Decimal = Decimal("100"),
    eth_cap: Decimal = Decimal("100"),
    slices: tuple[PortfolioExposureSlice, ...] = (),
    effective_from: datetime | None = None,
) -> AccountPortfolioEnvelope:
    return AccountPortfolioEnvelope(
        account_scope="seventh-audit-legacy-label",
        account_id=account_id,
        version=version,
        verified_account_equity_usdt=equity,
        bot_equity_cap_usdt=equity,
        required_reserve_usdt=Decimal("0"),
        max_total_exposure_usdt=total_cap,
        max_symbol_exposure_usdt=max(btc_cap, eth_cap),
        symbol_exposure_caps_usdt={"BTCUSDT": btc_cap, "ETHUSDT": eth_cap},
        max_required_margin_usdt=equity,
        daily_remaining_risk_usdt=Decimal("1000"),
        weekly_remaining_risk_usdt=Decimal("1000"),
        open_position_count=0,
        pending_order_count=0,
        exposure_slices=slices,
        effective_from=effective_from,
    )


def _policy(
    plan_id: str,
    *,
    symbol: str = "BTCUSDT",
    direction: Direction = Direction.LONG,
    envelope: AccountPortfolioEnvelope,
) -> ActualRiskPolicy:
    return ActualRiskPolicy(
        plan_id=plan_id,
        symbol=symbol,
        direction=direction,
        worst_stop_exit_price=Decimal("90") if direction is Direction.LONG else Decimal("110"),
        exit_fee_rate=Decimal("0"),
        funding_buffer_rate=Decimal("0"),
        funding_interval_count=0,
        risk_budget=Decimal("1000"),
        effective_leverage=2,
        protective_stop_reference=f"{plan_id}-stop",
        reduce_only_exit_reference=f"{plan_id}-reduce",
        portfolio_envelope=envelope,
    )


def _entry(
    client_order_id: str,
    plan_id: str,
    *,
    symbol: str = "BTCUSDT",
    direction: Direction = Direction.LONG,
    quantity: Decimal = Decimal("0.01"),
    price: Decimal = Decimal("100"),
) -> SimulatedOrderIntent:
    return SimulatedOrderIntent(
        account_id=ACCOUNT_ID,
        client_order_id=client_order_id,
        plan_id=plan_id,
        symbol=symbol,
        direction=direction,
        role=OrderRole.ENTRY,
        stage_index=1,
        quantity=quantity,
        price=price,
    )


@pytest.mark.parametrize("direction", (Direction.LONG, Direction.SHORT))
@pytest.mark.parametrize("candidate_symbol", ("BTCUSDT", "ETHUSDT"))
def test_any_over_cap_symbol_blocks_every_new_account_entry(
    durable_intent_ledger: DurableIntentLedger,
    direction: Direction,
    candidate_symbol: str,
) -> None:
    """An ETH breach must also deny independent BTC long/short candidates."""
    over_cap_eth = PortfolioExposureSlice(
        slice_id="external-eth-over-cap",
        plan_id="external-eth",
        symbol="ETHUSDT",
        direction=Direction.LONG,
        notional_usdt=Decimal("11"),
        leverage=1,
        required_margin_usdt=Decimal("11"),
        source_state=ExposureSourceState.EXTERNAL_CONFIRMED,
    )
    envelope = _envelope(eth_cap=Decimal("10"), slices=(over_cap_eth,))
    plan_id = f"candidate-{candidate_symbol}-{direction.value}"
    durable_intent_ledger.register_actual_risk_policy(
        _policy(plan_id, symbol=candidate_symbol, direction=direction, envelope=envelope)
    )

    projected = durable_intent_ledger.projected_entry_risk(
        _entry(
            f"{plan_id}-entry",
            plan_id,
            symbol=candidate_symbol,
            direction=direction,
        )
    )

    assert projected.blocked
    assert projected.reason == "ACCOUNT_SYMBOL_EXPOSURE_LIMIT_BREACH:ETHUSDT"


def test_new_envelope_head_fences_old_plan_after_equity_drop(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    original = _envelope(equity=Decimal("100"), total_cap=Decimal("100"))
    plan_id = "stale-envelope-plan"
    durable_intent_ledger.register_actual_risk_policy(_policy(plan_id, envelope=original))
    tightened = _envelope(version=2, equity=Decimal("20"), total_cap=Decimal("20"))

    head = durable_intent_ledger.publish_portfolio_envelope_head(tightened)

    assert head.account_id == ACCOUNT_ID
    assert head.version == 2
    with pytest.raises(DurableRiskPolicyError, match="ACCOUNT_ENVELOPE_STALE"):
        durable_intent_ledger.projected_entry_risk(_entry("stale-envelope-entry", plan_id))


def test_explicit_effective_from_survives_durable_envelope_replay(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    effective_from = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    envelope = _envelope(effective_from=effective_from)
    plan_id = "effective-from-replay"
    durable_intent_ledger.register_actual_risk_policy(_policy(plan_id, envelope=envelope))

    projected = durable_intent_ledger.projected_entry_risk(
        _entry("effective-from-replay-entry", plan_id)
    )

    assert not projected.blocked


def test_second_account_is_rejected_before_any_risk_or_intent_write(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    durable_intent_ledger.register_actual_risk_policy(
        _policy("first-account-plan", envelope=_envelope())
    )
    second_account_envelope = _envelope(account_id="second-account")

    with pytest.raises(DurableRiskPolicyError, match="V1_SECOND_ACCOUNT_UNSUPPORTED"):
        durable_intent_ledger.register_actual_risk_policy(
            _policy("second-account-plan", envelope=second_account_envelope)
        )


def test_unresolved_quarantine_is_a_durable_retry_and_capability_deny_gate(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "quarantine-deny-plan"
    intent = _entry("quarantine-deny-entry", plan_id)
    durable_intent_ledger.register_actual_risk_policy(_policy(plan_id, envelope=_envelope()))
    durable_intent_ledger.record_evidence_quarantine(
        account_id=ACCOUNT_ID,
        economic_key=intent.economic_key,
        client_order_id=intent.client_order_id,
        query_reference="original-query-reference",
        provenance_fingerprint="a" * 64,
        reason="INVALID_LEGACY_TIMELINE",
    )

    restarted = durable_intent_ledger.reopen_after_restart()
    with pytest.raises(PersistenceUnavailable, match="UNRESOLVED_QUARANTINE"):
        restarted.prepare(intent)
    with pytest.raises(PersistenceUnavailable, match="UNRESOLVED_QUARANTINE"):
        restarted.entry_authorization_capability()


def test_quarantine_resolution_rejects_a_copied_original_provenance_fingerprint(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    quarantine_id = durable_intent_ledger.record_evidence_quarantine(
        account_id=ACCOUNT_ID,
        economic_key="quarantine-resolution:BTCUSDT:LONG:ENTRY:1",
        client_order_id="quarantine-resolution-entry",
        query_reference="original-quarantine-query",
        provenance_fingerprint="d" * 64,
        reason="INVALID_LEGACY_TIMELINE",
    )

    with pytest.raises(ValueError, match="new verified evidence"):
        durable_intent_ledger.resolve_evidence_quarantine(
            quarantine_id,
            operator_id="operator-1",
            verified_evidence_fingerprint="d" * 64,
        )


def test_quarantine_resolution_fences_the_pre_resolution_capability(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    quarantine_id = durable_intent_ledger.record_evidence_quarantine(
        account_id=ACCOUNT_ID,
        economic_key="quarantine-fence:BTCUSDT:LONG:ENTRY:1",
        client_order_id="quarantine-fence-entry",
        query_reference="original-quarantine-query",
        provenance_fingerprint="e" * 64,
        reason="INVALID_LEGACY_TIMELINE",
    )
    with pytest.raises(PersistenceUnavailable, match="UNRESOLVED_QUARANTINE"):
        durable_intent_ledger.entry_authorization_capability()

    durable_intent_ledger.resolve_evidence_quarantine(
        quarantine_id,
        operator_id="operator-1",
        verified_evidence=VerifiedQuarantineResolutionEvidence(
            account_id=ACCOUNT_ID,
            economic_key="quarantine-fence:BTCUSDT:LONG:ENTRY:1",
            client_order_id="quarantine-fence-entry",
            query_reference="fresh-verified-query",
            observed_at=datetime.now(UTC),
        ),
    )

    with pytest.raises(PersistenceUnavailable, match="EPOCH_STALE"):
        durable_intent_ledger.entry_authorization_capability()


def _stop_contract() -> ExpectedStopContract:
    return ExpectedStopContract(
        account_id=ACCOUNT_ID,
        plan_id="exchange-stop-plan",
        symbol="BTCUSDT",
        position_side=Direction.LONG,
        expected_order_side=OrderSide.SELL,
        client_algo_id="exchange-stop-id",
        algo_type=AlgoOrderType.STOP_MARKET,
        trigger_price=Decimal("90"),
        working_type=StopWorkingType.MARK_PRICE,
        close_position=True,
        quantity_semantics=StopQuantitySemantics.CLOSE_POSITION_FULL,
        active_status=AlgoOrderStatus.NEW,
        policy_version=1,
        account_envelope_version=1,
        policy_fingerprint="b" * 64,
        account_envelope_fingerprint="c" * 64,
    )


def test_local_algo_intent_cannot_substitute_for_exchange_observation() -> None:
    contract = _stop_contract()
    local_intent = AlgoOrderIntent(
        client_algo_id=contract.client_algo_id,
        symbol=contract.symbol,
        direction=contract.position_side,
        algo_type=contract.algo_type,
        trigger_price=contract.trigger_price,
        close_position=True,
        plan_id=contract.plan_id,
        policy_version=contract.policy_version,
        account_envelope_version=contract.account_envelope_version,
        policy_fingerprint=contract.policy_fingerprint,
        account_envelope_fingerprint=contract.account_envelope_fingerprint,
        stop_contract_fingerprint=contract.fingerprint,
    )

    assert not contract.matches(local_intent)
    with pytest.raises(TypeError, match="ExchangeAlgoOrderObservation"):
        ReconciliationSnapshot(
            account_id=ACCOUNT_ID,
            positions_by_symbol={"BTCUSDT": Decimal("1")},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset({local_intent.client_algo_id}),
            algo_orders=(cast(ExchangeAlgoOrderObservation, local_intent),),
        )


def test_exchange_stop_observation_requires_fresh_authenticated_provenance() -> None:
    contract = _stop_contract()
    now = datetime.now(UTC)
    observation = ExchangeAlgoOrderObservation(
        source="authenticated_exchange_adapter",
        account_id=ACCOUNT_ID,
        fetched_at=now,
        server_time=now,
        freshness_window=timedelta(seconds=30),
        correlation_id="reconciliation-query-1",
        query_epoch=1,
        client_algo_id=contract.client_algo_id,
        symbol=contract.symbol,
        direction=contract.position_side,
        algo_type=contract.algo_type,
        trigger_price=contract.trigger_price,
        close_position=True,
        plan_id=contract.plan_id,
        policy_version=contract.policy_version,
        account_envelope_version=contract.account_envelope_version,
        policy_fingerprint=contract.policy_fingerprint,
        account_envelope_fingerprint=contract.account_envelope_fingerprint,
        stop_contract_fingerprint=contract.fingerprint,
    )

    assert contract.matches(observation)
    with pytest.raises(ValueError, match="fresh"):
        replace(
            observation,
            fetched_at=now - timedelta(minutes=2),
            server_time=now - timedelta(minutes=2),
            freshness_window=timedelta(seconds=30),
        )


def test_portfolio_exposure_rejects_an_untyped_direction() -> None:
    """Account-wide exposure cannot be reconstructed from an untyped side."""
    with pytest.raises(TypeError, match="direction must be typed"):
        PortfolioExposureSlice(
            slice_id="untyped-direction",
            plan_id="untyped-direction-plan",
            symbol="BTCUSDT",
            direction=cast(Direction, "LONG"),
            notional_usdt=Decimal("1"),
            leverage=1,
            required_margin_usdt=Decimal("1"),
            source_state=ExposureSourceState.EXTERNAL_CONFIRMED,
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        ({"source": "operator"}, "durable reconciliation identity"),
        ({"observed_at": datetime(2026, 7, 16, 12, 0)}, "timezone-aware"),
        ({"observed_at": timedelta(minutes=1)}, "future"),
        ({"fingerprint": "0" * 64}, "fingerprint is invalid"),
    ),
)
def test_quarantine_resolution_evidence_rejects_unverifiable_provenance(
    kwargs: dict[str, object],
    message: str,
) -> None:
    """Resolution evidence must not acquire trust from a caller-controlled shape."""
    baseline: dict[str, object] = {
        "account_id": ACCOUNT_ID,
        "economic_key": "resolution-evidence:BTCUSDT:LONG:ENTRY:1",
        "client_order_id": "resolution-evidence-entry",
        "query_reference": "verified-query-reference",
        "observed_at": datetime.now(UTC),
    }
    baseline.update(kwargs)
    if isinstance(baseline["observed_at"], timedelta):
        baseline["observed_at"] = datetime.now(UTC) + baseline["observed_at"]

    with pytest.raises(ValueError, match=message):
        VerifiedQuarantineResolutionEvidence(**baseline)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "error_type", "message"),
    (
        ({"source": "NORMAL_OPEN_ORDERS"}, TypeError, "source must be typed"),
        ({"observed_at_ms": -1}, ValueError, "non-negative integers"),
        ({"stream_watermark_ms": 99}, ValueError, "cannot precede"),
        ({"query_reference": ""}, ValueError, "durable query identity"),
        ({"attempt_number": 0}, ValueError, "attempt number must be positive"),
        ({"client_order_namespace": "NORMAL"}, TypeError, "namespace must be typed"),
        ({"provenance_fingerprint": "0" * 64}, ValueError, "fingerprint is invalid"),
    ),
)
def test_unknown_intent_observation_rejects_malformed_durable_provenance(
    kwargs: dict[str, object],
    error_type: type[Exception],
    message: str,
) -> None:
    """Bounded absence may only be derived from typed, internally consistent facts."""
    baseline: dict[str, object] = {
        "source": AbsenceEvidenceSource.NORMAL_OPEN_ORDERS,
        "observed_at_ms": 100,
        "stream_watermark_ms": 100,
        "found": False,
        "query_reference": "bounded-query-reference",
        "query_client_order_id": "bounded-client-id",
        "query_economic_key": "bounded-key:BTCUSDT:LONG:ENTRY:1",
        "query_started_at_ms": 99,
        "attempt_number": 1,
    }
    baseline.update(kwargs)

    with pytest.raises(error_type, match=message):
        UnknownIntentObservation(**baseline)  # type: ignore[arg-type]


def test_quarantine_rejects_incomplete_original_provenance(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    """A deny record is only meaningful when it preserves the original query identity."""
    with pytest.raises(ValueError, match="complete original economic provenance"):
        durable_intent_ledger.record_evidence_quarantine(
            account_id=ACCOUNT_ID,
            economic_key="incomplete-quarantine:BTCUSDT:LONG:ENTRY:1",
            client_order_id="incomplete-quarantine-entry",
            query_reference="",
            provenance_fingerprint="f" * 64,
            reason="INVALID_LEGACY_TIMELINE",
        )


def test_duplicate_quarantine_append_is_idempotent_and_resolution_needs_an_operator(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    """Repeated ingest cannot fork a deny record or permit anonymous clearance."""
    economic_key = "duplicate-quarantine:BTCUSDT:LONG:ENTRY:1"
    client_order_id = "duplicate-quarantine-entry"
    query_reference = "duplicate-original-query"
    provenance_fingerprint = "a" * 64
    reason = "INVALID_LEGACY_TIMELINE"
    first = durable_intent_ledger.record_evidence_quarantine(
        account_id=ACCOUNT_ID,
        economic_key=economic_key,
        client_order_id=client_order_id,
        query_reference=query_reference,
        provenance_fingerprint=provenance_fingerprint,
        reason=reason,
    )
    assert (
        durable_intent_ledger.record_evidence_quarantine(
            account_id=ACCOUNT_ID,
            economic_key=economic_key,
            client_order_id=client_order_id,
            query_reference=query_reference,
            provenance_fingerprint=provenance_fingerprint,
            reason=reason,
        )
        == first
    )

    with pytest.raises(ValueError, match="operator identity"):
        durable_intent_ledger.resolve_evidence_quarantine(
            first,
            operator_id="",
            verified_evidence=VerifiedQuarantineResolutionEvidence(
                account_id=ACCOUNT_ID,
                economic_key=economic_key,
                client_order_id=client_order_id,
                query_reference="duplicate-fresh-verified-query",
                observed_at=datetime.now(UTC),
            ),
        )
