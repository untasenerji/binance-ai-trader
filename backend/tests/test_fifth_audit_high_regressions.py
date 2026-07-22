"""Red-first release blockers from the fifth independent audit."""

from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.domain.types import Direction
from app.exchange.contracts import (
    ExchangeAlgoOrderObservation,
    LocalReconciliationState,
    ReconciliationSnapshot,
    reconcile_local_state,
)
from app.persistence.audit import AuditRepository
from app.persistence.circuit_breaker import PersistenceCircuitBreaker, PersistenceUnavailable
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.persistence.models import (
    DurableActualRiskPolicyVersion,
    DurableIntentAbsenceObservation,
    DurableOrderIntent,
    ExchangeFillFactJournal,
)
from app.persistence.replay import ReplayRunner
from app.planning.fills import (
    AccountPortfolioEnvelope,
    ActualRiskPolicy,
    FillEvent,
    FillObservationSource,
    FillSide,
)
from app.simulation.intent_ledger import (
    BoundedAbsenceEvidenceError,
    ClientOrderNamespace,
    DurableIntentLedger,
    DurableIntentStatus,
    DurableRiskPolicyError,
    FillFactApplyStatus,
    IntentLifecycleError,
)
from app.simulation.models import (
    OrderRole,
    SimulatedFault,
    SimulatedFill,
    SimulatedOrderIntent,
)
from app.simulation.simulator import (
    EntryRiskBlocked,
    ExchangeSimulator,
    FaultPlan,
    FillSequencePlan,
    SimulatedUnknownQueryPlan,
    SimulatedUnknownRemoteState,
    UnknownOrderOutcome,
)
from app.strategy.backtest import (
    BacktestCosts,
    WalkForwardRunner,
    WalkForwardTrainingError,
)
from app.strategy.models import Candle, FrozenStrategy, SignalCandidate
from tests.reconciliation_factory import (
    exchange_reconciliation_batch,
    persist_reconciliation_query_receipt,
)
from tests.strategy_factory import make_frozen_strategy, make_strategy_lineage


def _policy(
    plan_id: str,
    *,
    direction: Direction = Direction.LONG,
    leverage: int = 2,
    equity: Decimal = Decimal("1000"),
    max_symbol_exposure: Decimal = Decimal("1000"),
    max_total_exposure: Decimal = Decimal("1000"),
    risk_budget: Decimal = Decimal("1000"),
) -> ActualRiskPolicy:
    return ActualRiskPolicy(
        plan_id=plan_id,
        symbol="BTCUSDT",
        direction=direction,
        worst_stop_exit_price=(Decimal("90") if direction is Direction.LONG else Decimal("110")),
        exit_fee_rate=Decimal("0"),
        funding_buffer_rate=Decimal("0"),
        funding_interval_count=0,
        risk_budget=risk_budget,
        effective_leverage=leverage,
        protective_stop_reference=f"{plan_id}-expected-algo-stop",
        reduce_only_exit_reference=f"{plan_id}-expected-reduce-exit",
        portfolio_envelope=AccountPortfolioEnvelope(
            account_scope="fifth-audit-account",
            version=1,
            verified_account_equity_usdt=equity,
            bot_equity_cap_usdt=equity,
            required_reserve_usdt=Decimal("0"),
            max_total_exposure_usdt=max_total_exposure,
            max_symbol_exposure_usdt=max_symbol_exposure,
            max_required_margin_usdt=equity,
            daily_remaining_risk_usdt=Decimal("1000000"),
            weekly_remaining_risk_usdt=Decimal("1000000"),
            open_position_count=0,
            pending_order_count=0,
        ),
    )


def _entry(
    client_order_id: str,
    plan_id: str,
    *,
    direction: Direction = Direction.LONG,
    stage_index: int = 1,
    quantity: Decimal = Decimal("0.010"),
    price: Decimal = Decimal("100"),
) -> SimulatedOrderIntent:
    return SimulatedOrderIntent(
        client_order_id=client_order_id,
        plan_id=plan_id,
        symbol="BTCUSDT",
        direction=direction,
        role=OrderRole.ENTRY,
        stage_index=stage_index,
        quantity=quantity,
        price=price,
    )


def _fill(
    client_order_id: str,
    trade_id: str,
    *,
    quantity: Decimal,
    price: Decimal,
) -> FillEvent:
    return FillEvent(
        account_id="v1-primary",
        trade_id=trade_id,
        client_order_id=client_order_id,
        symbol="BTCUSDT",
        side=FillSide.BUY,
        last_quantity=quantity,
        cumulative_quantity=quantity,
        fill_price=price,
        fee=Decimal("0"),
        fee_asset="USDT",
        occurred_at=datetime(2026, 7, 15, tzinfo=UTC),
        observation_source=FillObservationSource.SIMULATED_EXCHANGE,
        observation_reference=f"test:{trade_id}",
    )


@pytest.fixture
def fifth_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'fifth-audit.sqlite'}")
    create_schema(engine)
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


def test_cross_plan_margin_uses_each_plans_own_leverage(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    confirmed_plan = "confirmed-one-x"
    pending_plan = "pending-hundred-x"
    confirmed = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(
            confirmed_plan,
            leverage=1,
            equity=Decimal("10"),
        ),
        fill_plan=FillSequencePlan.from_sequences(
            (
                (
                    SimulatedFill(
                        trade_id="confirmed-one-x-fill",
                        last_quantity=Decimal("0.09"),
                        cumulative_quantity=Decimal("0.09"),
                        fill_price=Decimal("100"),
                        fee=Decimal("0"),
                        fee_asset="USDT",
                    ),
                ),
            )
        ),
    )
    confirmed.submit(
        _entry(
            "confirmed-one-x-entry",
            confirmed_plan,
            quantity=Decimal("0.09"),
        )
    )
    pending = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(
            pending_plan,
            direction=Direction.SHORT,
            leverage=100,
            equity=Decimal("10"),
        ),
    )

    with pytest.raises(EntryRiskBlocked, match="MARGIN"):
        pending.submit(
            _entry(
                "pending-hundred-x-entry",
                pending_plan,
                direction=Direction.SHORT,
                quantity=Decimal("2"),
            )
        )


def _prepare_two_pending_stages(ledger: DurableIntentLedger, plan_id: str) -> None:
    simulator = ExchangeSimulator(
        intent_ledger=ledger,
        actual_risk_policy=_policy(
            plan_id,
            max_symbol_exposure=Decimal("10"),
            max_total_exposure=Decimal("10"),
        ),
    )
    simulator.submit(_entry(f"{plan_id}-stage-1", plan_id, quantity=Decimal("0.05")))
    simulator.submit(
        _entry(
            f"{plan_id}-stage-2",
            plan_id,
            stage_index=2,
            quantity=Decimal("0.05"),
        )
    )


def test_fill_risk_block_and_pending_stage_cancellation_commit_atomically(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "atomic-fill-risk"
    _prepare_two_pending_stages(durable_intent_ledger, plan_id)

    durable_intent_ledger.record_fill(
        _fill(
            f"{plan_id}-stage-1",
            "atomic-risk-breach-fill",
            quantity=Decimal("0.05"),
            price=Decimal("100.2"),
        )
    )
    restarted = durable_intent_ledger.reopen_after_restart()

    assert restarted.intent(f"{plan_id}-stage-1").status is DurableIntentStatus.FILLED
    assert restarted.intent(f"{plan_id}-stage-2").status is DurableIntentStatus.CANCEL_REQUIRED
    assert restarted.actual_risk_state(plan_id).pending_entries_blocked
    assert restarted.risk_reduction_requirement(plan_id).reason


def test_fill_application_failure_preserves_durable_fact_and_fences_entries(
    durable_intent_ledger: DurableIntentLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_id = "atomic-fill-rollback"
    _prepare_two_pending_stages(durable_intent_ledger, plan_id)

    def fail_risk_recalculation(*_: object, **__: object) -> object:
        raise RuntimeError("simulated transaction-boundary crash")

    monkeypatch.setattr(
        DurableIntentLedger,
        "_recalculate_actual_risk",
        classmethod(fail_risk_recalculation),
    )
    with pytest.raises(RuntimeError, match="transaction-boundary"):
        durable_intent_ledger.record_fill(
            _fill(
                f"{plan_id}-stage-1",
                "rolled-back-fill",
                quantity=Decimal("0.05"),
                price=Decimal("100.2"),
            )
        )

    with durable_intent_ledger._session_factory() as session:  # noqa: SLF001 - durable proof
        fact = session.scalar(
            select(ExchangeFillFactJournal).where(
                ExchangeFillFactJournal.exchange_trade_id == "rolled-back-fill"
            )
        )
    assert fact is not None
    assert fact.apply_status == FillFactApplyStatus.RECOVERY_REQUIRED.value
    assert fact.apply_attempt_count >= 1
    assert "transaction-boundary crash" in (fact.last_apply_error or "")

    restarted = durable_intent_ledger.reopen_after_restart()
    assert restarted.fill_ledger_for_plan(plan_id).filled_quantity == Decimal("0")
    assert restarted.intent(f"{plan_id}-stage-1").status is DurableIntentStatus.CANCEL_REQUIRED
    assert restarted.intent(f"{plan_id}-stage-2").status is DurableIntentStatus.CANCEL_REQUIRED


def test_breaker_cannot_be_opened_by_constructor_or_attribute_assignment() -> None:
    with pytest.raises(TypeError):
        PersistenceCircuitBreaker(None)  # type: ignore[call-arg]

    breaker = PersistenceCircuitBreaker()
    with pytest.raises(AttributeError):
        breaker.halted_reason = None  # type: ignore[misc]
    assert not breaker.new_entries_allowed


def test_unknown_timestamp_must_not_precede_submission(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    intent = _entry("unknown-clock-reversal", "unknown-clock-plan")
    durable_intent_ledger.register_actual_risk_policy(_policy(intent.plan_id))
    durable_intent_ledger.prepare(
        intent,
        admission_decision=durable_intent_ledger.admit_entry(intent),
    )
    durable_intent_ledger.mark_submitting(intent.client_order_id, submitted_at_ms=1_000_000)

    with pytest.raises(IntentLifecycleError, match="timestamp|UNKNOWN|submission"):
        durable_intent_ledger.mark_unknown(intent.client_order_id, unknown_at_ms=0)

    assert (
        durable_intent_ledger.intent(intent.client_order_id).status
        is DurableIntentStatus.SUBMITTING
    )


def test_restart_revalidates_corrupt_unknown_timeline(
    fifth_session_factory: sessionmaker[Session],
) -> None:
    ledger = DurableIntentLedger(fifth_session_factory)
    intent = _entry("unknown-corrupt-restart", "unknown-corrupt-plan")
    with fifth_session_factory.begin() as session:
        session.add(
            DurableOrderIntent(
                economic_key=intent.economic_key,
                attempt_number=1,
                client_order_id=intent.client_order_id,
                plan_id=intent.plan_id,
                symbol=intent.symbol,
                direction=intent.direction.value,
                role=intent.role.value,
                stage_index=intent.stage_index,
                quantity=format(intent.quantity, "f"),
                price=format(intent.price, "f"),
                filled_quantity="0",
                status=DurableIntentStatus.UNKNOWN.value,
                submitted_at_ms=1_000_000,
                unknown_at_ms=0,
            )
        )

    with pytest.raises(IntentLifecycleError, match="timestamp|UNKNOWN|submission"):
        ledger.reopen_after_restart().intent(intent.client_order_id)


def test_restart_revalidates_negative_query_duration(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "unknown-query-duration"
    intent = _entry("unknown-query-duration-entry", plan_id)
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(plan_id),
        fault_plan=FaultPlan.from_faults((SimulatedFault.UNKNOWN_503,)),
        unknown_query_plan=SimulatedUnknownQueryPlan.from_states(
            (SimulatedUnknownRemoteState.ABSENT,)
        ),
        now_ms=1_000,
    )
    with pytest.raises(UnknownOrderOutcome):
        simulator.submit(intent)
    with durable_intent_ledger._session_factory.begin() as session:
        session.add(
            DurableIntentAbsenceObservation(
                client_order_id=intent.client_order_id,
                economic_key=intent.economic_key,
                source="TRADE_HISTORY",
                query_reference="invalid-negative-duration",
                query_client_order_id=intent.client_order_id,
                query_economic_key=intent.economic_key,
                query_started_at_ms=2_000,
                attempt_number=1,
                client_order_namespace=ClientOrderNamespace.NORMAL.value,
                provenance_fingerprint="f" * 64,
                observed_at_ms=1_000,
                stream_watermark_ms=1_000,
                found=False,
            )
        )

    with pytest.raises(BoundedAbsenceEvidenceError, match="time|duration|future"):
        durable_intent_ledger.reopen_after_restart().list_absence_observations(
            intent.client_order_id
        )


def test_policy_loader_rejects_non_contiguous_forged_high_version(
    fifth_session_factory: sessionmaker[Session],
) -> None:
    ledger = DurableIntentLedger(fifth_session_factory)
    base = _policy("forged-policy-lineage")
    ledger.register_actual_risk_policy(base)
    forged = replace(base, risk_budget=Decimal("999999"))
    with fifth_session_factory.begin() as session:
        session.add(
            DurableActualRiskPolicyVersion(
                plan_id=base.plan_id,
                version=99,
                **ledger._policy_row_values(
                    forged,
                    fingerprint=ledger._policy_fingerprint(forged),
                ),
            )
        )

    with pytest.raises(DurableRiskPolicyError, match="lineage|version"):
        ledger.actual_risk_policy(base.plan_id)


_HELD_OUT_DERIVED_SCALAR = Decimal("606")


class _GlobalsBypassTrainer:
    strategy_id = "globals-bypass"

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        threshold = globals()["_HELD_OUT_DERIVED_SCALAR"]

        def evaluate(
            evaluation_candles: Sequence[Candle],
            *,
            timeframe: str,
        ) -> SignalCandidate | None:
            last = evaluation_candles[-1]
            if last.close_price <= threshold:
                return None
            return SignalCandidate.from_lineage(
                make_strategy_lineage(self.strategy_id),
                symbol=last.symbol,
                direction=Direction.LONG,
                reference_price=last.close_price,
                invalidation_price=last.low_price,
                timeframe=timeframe,
                valid_until_ms=last.close_time_ms + 60_000,
                reason_codes=("UNSAFE_GLOBAL",),
            )

        return make_frozen_strategy(candles, evaluate)


def _candles() -> tuple[Candle, ...]:
    return tuple(
        Candle(
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
        for index, close in enumerate(
            (
                Decimal("100"),
                Decimal("101"),
                Decimal("102"),
                Decimal("103"),
                Decimal("200"),
                Decimal("201"),
                Decimal("205"),
            )
        )
    )


def test_walk_forward_rejects_unprovable_custom_trainer_global_scalar() -> None:
    with pytest.raises(WalkForwardTrainingError, match="trusted|custom|isolate"):
        WalkForwardRunner(train_size=4, test_size=3, step_size=3).run(
            _GlobalsBypassTrainer,  # type: ignore[arg-type]
            _candles(),
            timeframe="1m",
            costs=BacktestCosts(
                entry_fee_rate=Decimal("0"),
                exit_fee_rate=Decimal("0"),
                slippage_bps=Decimal("0"),
            ),
        )


@pytest.mark.parametrize("direction", (Direction.LONG, Direction.SHORT))
def test_reconciliation_matches_durable_expected_algo_stop(
    durable_intent_ledger: DurableIntentLedger,
    direction: Direction,
) -> None:
    plan_id = f"expected-algo-{direction.value.lower()}"
    policy = _policy(plan_id, direction=direction)
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=policy,
        fill_plan=FillSequencePlan.from_sequences(
            (
                (
                    SimulatedFill(
                        trade_id=f"{plan_id}-fill",
                        last_quantity=Decimal("0.01"),
                        cumulative_quantity=Decimal("0.01"),
                        fill_price=Decimal("100"),
                        fee=Decimal("0"),
                        fee_asset="USDT",
                    ),
                ),
            )
        ),
    )
    simulator.submit(
        _entry(
            f"{plan_id}-entry",
            plan_id,
            direction=direction,
        )
    )
    facts = durable_intent_ledger.reopen_after_restart().reconciliation_facts()
    expected_quantity = Decimal("0.01") if direction is Direction.LONG else Decimal("-0.01")
    contract = facts.expected_stop_contracts[0]
    observed_at = datetime.now(UTC)
    stop = ExchangeAlgoOrderObservation(
        source="authenticated_exchange_adapter",
        account_id=contract.account_id,
        fetched_at=observed_at,
        server_time=observed_at,
        freshness_window=timedelta(seconds=30),
        correlation_id=f"{plan_id}-reconciliation-query",
        query_epoch=1,
        client_algo_id=contract.client_algo_id,
        symbol=contract.symbol,
        direction=contract.position_side,
        algo_type=contract.algo_type,
        trigger_price=contract.trigger_price,
        close_position=contract.close_position,
        working_type=contract.working_type,
        status=contract.active_status,
        plan_id=contract.plan_id,
        policy_version=contract.policy_version,
        account_envelope_version=contract.account_envelope_version,
        policy_fingerprint=contract.policy_fingerprint,
        account_envelope_fingerprint=contract.account_envelope_fingerprint,
        stop_contract_fingerprint=contract.fingerprint,
    )
    local = LocalReconciliationState(
        positions_by_symbol=facts.positions_by_symbol,
        normal_order_client_ids=facts.normal_order_client_ids,
        algo_order_client_ids=facts.algo_order_client_ids,
        required_stop_symbols=facts.required_stop_symbols,
        unresolved_unknown_intent_ids=facts.unresolved_unknown_intent_ids,
        audit_chain_valid=True,
        replay_valid=True,
        expected_stop_contracts=facts.expected_stop_contracts,
    )
    clean = reconcile_local_state(
        local=local,
        snapshot=ReconciliationSnapshot(
            positions_by_symbol={policy.symbol: expected_quantity},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset({stop.client_algo_id}),
            algo_orders=(stop,),
        ),
    )
    missing = reconcile_local_state(
        local=local,
        snapshot=ReconciliationSnapshot(
            positions_by_symbol={policy.symbol: expected_quantity},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
        ),
    )

    assert policy.protective_stop_reference in facts.algo_order_client_ids
    assert clean.is_clean
    assert not clean.unexpected_algo_order_ids
    assert missing.missing_algo_order_ids == (policy.protective_stop_reference,)
    assert missing.missing_stop_symbols == (policy.symbol,)


def _migration_config(database_url: str) -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _populate_legacy_0008(
    database_url: str,
    *,
    nullable_query_fields: bool = True,
) -> None:
    command.upgrade(_migration_config(database_url), "0008_processed_events_temp")
    engine = create_database_engine(database_url)
    metadata = MetaData()
    intents = Table("durable_order_intents", metadata, autoload_with=engine)
    observations = Table("durable_intent_absence_observations", metadata, autoload_with=engine)
    economic_key = "legacy-0008-plan:BTCUSDT:LONG:ENTRY:1"
    try:
        with engine.begin() as connection:
            connection.execute(
                intents.insert().values(
                    economic_key=economic_key,
                    attempt_number=1,
                    client_order_id="legacy-0008-client",
                    plan_id="legacy-0008-plan",
                    symbol="BTCUSDT",
                    direction="LONG",
                    role="ENTRY",
                    stage_index=1,
                    quantity="0.01",
                    price="100",
                    filled_quantity="0",
                    status="ABSENT",
                    submitted_at_ms=1_000,
                    unknown_at_ms=1_500,
                )
            )
            connection.execute(
                observations.insert().values(
                    client_order_id="legacy-0008-client",
                    economic_key=economic_key,
                    source="TRADE_HISTORY",
                    query_reference=(None if nullable_query_fields else "legacy-complete-query"),
                    query_client_order_id=(None if nullable_query_fields else "legacy-0008-client"),
                    query_economic_key=None if nullable_query_fields else economic_key,
                    query_started_at_ms=None if nullable_query_fields else 1_500,
                    observed_at_ms=2_000,
                    stream_watermark_ms=2_000,
                    found=False,
                )
            )
    finally:
        engine.dispose()


def _assert_populated_0008_upgrade(
    database_url: str,
    *,
    expected_query_reference: str | None = None,
) -> None:
    command.upgrade(_migration_config(database_url), "head")
    engine = create_database_engine(database_url)
    session_factory = create_session_factory(engine)
    breaker = PersistenceCircuitBreaker()
    ledger = DurableIntentLedger(session_factory, persistence_breaker=breaker)
    repository = AuditRepository(session_factory, persistence_breaker=breaker)
    try:
        observations = ledger.list_absence_observations("legacy-0008-client")
        assert len(observations) == 1
        observation = observations[0]
        if expected_query_reference is None:
            assert observation.query_reference.startswith("legacy-query:")
        else:
            assert observation.query_reference == expected_query_reference
        assert observation.query_client_order_id == "legacy-0008-client"
        assert observation.query_economic_key == observation.economic_key
        assert observation.query_started_at_ms == 1_500
        assert observation.provenance_fingerprint
        assert (
            ReplayRunner()
            .replay(repository.list_audit_events(), repository.audit_chain_head())
            .is_valid
        )
        batch = persist_reconciliation_query_receipt(
            ledger,
            exchange_reconciliation_batch(
                ReconciliationSnapshot(
                    positions_by_symbol={},
                    normal_order_client_ids=frozenset(),
                    algo_order_client_ids=frozenset(),
                )
            ),
        )
        evidence = breaker.reset_after_verified_reconciliation(
            audit_repository=repository,
            intent_ledger=ledger,
            reconciliation_snapshot=batch,
        )
        assert evidence.is_complete
        with engine.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "UPDATE durable_intent_absence_observations "
                    "SET found = true WHERE client_order_id = 'legacy-0008-client'"
                )
            )
    finally:
        engine.dispose()


def test_sqlite_populated_0008_complete_evidence_upgrades_to_head_and_recovers(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'populated-0008.sqlite'}"
    _populate_legacy_0008(database_url, nullable_query_fields=False)
    _assert_populated_0008_upgrade(
        database_url,
        expected_query_reference="legacy-complete-query",
    )


def test_sqlite_populated_0008_null_query_provenance_stays_quarantined(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'populated-0008-null-query.sqlite'}"
    _populate_legacy_0008(database_url, nullable_query_fields=True)
    command.upgrade(_migration_config(database_url), "head")
    engine = create_database_engine(database_url)
    try:
        session_factory = create_session_factory(engine)
        breaker = PersistenceCircuitBreaker()
        ledger = DurableIntentLedger(session_factory, persistence_breaker=breaker)
        repository = AuditRepository(session_factory, persistence_breaker=breaker)
        assert ledger.list_absence_observations("legacy-0008-client") == ()
        batch = persist_reconciliation_query_receipt(
            ledger,
            exchange_reconciliation_batch(
                ReconciliationSnapshot(
                    positions_by_symbol={},
                    normal_order_client_ids=frozenset(),
                    algo_order_client_ids=frozenset(),
                )
            ),
        )
        with pytest.raises(PersistenceUnavailable, match="UNRESOLVED_QUARANTINE"):
            breaker.reset_after_verified_reconciliation(
                audit_repository=repository,
                intent_ledger=ledger,
                reconciliation_snapshot=batch,
            )
    finally:
        engine.dispose()


def test_sqlite_populated_0008_temporarily_manages_append_only_trigger(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'populated-0008-trigger.sqlite'}"
    _populate_legacy_0008(database_url, nullable_query_fields=False)
    _assert_populated_0008_upgrade(
        database_url,
        expected_query_reference="legacy-complete-query",
    )


def test_sqlite_policy_owner_migration_downgrades_and_reapplies(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'policy-owner-roundtrip.sqlite'}"
    config = _migration_config(database_url)
    command.upgrade(config, "0011_forward_invariants")
    command.downgrade(config, "0009_evidence_risk_hardening")
    command.upgrade(config, "0011_forward_invariants")

    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0011_forward_invariants"
            )
    finally:
        engine.dispose()


@pytest.fixture
def fifth_postgresql_database() -> Iterator[str]:
    database_name = f"uta_fifth_audit_{uuid4().hex}"
    database_url = f"postgresql+psycopg://postgres@127.0.0.1:5432/{database_name}"
    admin_engine = create_engine(
        "postgresql+psycopg://postgres@127.0.0.1:5432/postgres",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        yield database_url
    finally:
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin_engine.dispose()


@pytest.mark.postgresql
def test_postgresql_populated_0008_upgrades_to_head_and_recovers(
    fifth_postgresql_database: str,
) -> None:
    _populate_legacy_0008(fifth_postgresql_database, nullable_query_fields=False)
    _assert_populated_0008_upgrade(
        fifth_postgresql_database,
        expected_query_reference="legacy-complete-query",
    )


@pytest.mark.postgresql
def test_postgresql_populated_0008_null_query_provenance_stays_quarantined(
    fifth_postgresql_database: str,
) -> None:
    _populate_legacy_0008(fifth_postgresql_database, nullable_query_fields=True)
    command.upgrade(_migration_config(fifth_postgresql_database), "head")
    engine = create_database_engine(fifth_postgresql_database)
    try:
        session_factory = create_session_factory(engine)
        breaker = PersistenceCircuitBreaker()
        ledger = DurableIntentLedger(session_factory, persistence_breaker=breaker)
        repository = AuditRepository(session_factory, persistence_breaker=breaker)
        assert ledger.list_absence_observations("legacy-0008-client") == ()
        batch = persist_reconciliation_query_receipt(
            ledger,
            exchange_reconciliation_batch(
                ReconciliationSnapshot(
                    positions_by_symbol={},
                    normal_order_client_ids=frozenset(),
                    algo_order_client_ids=frozenset(),
                )
            ),
        )
        with pytest.raises(PersistenceUnavailable, match="UNRESOLVED_QUARANTINE"):
            breaker.reset_after_verified_reconciliation(
                audit_repository=repository,
                intent_ledger=ledger,
                reconciliation_snapshot=batch,
            )
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_postgresql_runtime_policy_tables_are_select_only(
    fifth_postgresql_database: str,
) -> None:
    command.upgrade(_migration_config(fifth_postgresql_database), "head")
    admin_engine = create_database_engine(fifth_postgresql_database)
    database_name = fifth_postgresql_database.rsplit("/", maxsplit=1)[-1]
    runtime_engine = create_database_engine(
        f"postgresql+psycopg://uta_runtime@127.0.0.1:5432/{database_name}"
    )
    plan_id = f"runtime-select-only-{uuid4().hex}"
    try:
        DurableIntentLedger(create_session_factory(admin_engine)).register_actual_risk_policy(
            _policy(plan_id)
        )
        with runtime_engine.connect() as connection:
            for table_name in (
                "durable_actual_risk_policies",
                "durable_actual_risk_policy_versions",
            ):
                assert connection.scalar(
                    text("SELECT has_table_privilege(current_user, :table_name, 'SELECT')"),
                    {"table_name": table_name},
                )
                for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    assert not connection.scalar(
                        text("SELECT has_table_privilege(current_user, :table_name, :privilege)"),
                        {"table_name": table_name, "privilege": privilege},
                    )
            with pytest.raises(DBAPIError):
                connection.execute(
                    text(
                        "INSERT INTO durable_actual_risk_policy_versions "
                        "(plan_id, version, symbol, direction, worst_stop_exit_price, "
                        "exit_fee_rate, funding_buffer_rate, funding_interval_count, "
                        "risk_budget, max_symbol_exposure_usdt, max_total_exposure_usdt, "
                        "existing_symbol_exposure_usdt, existing_total_exposure_usdt, "
                        "effective_leverage, required_reserve_usdt, effective_equity_usdt, "
                        "protective_stop_reference, reduce_only_exit_reference, "
                        "policy_fingerprint) "
                        "SELECT plan_id, 99, symbol, direction, worst_stop_exit_price, "
                        "exit_fee_rate, funding_buffer_rate, funding_interval_count, "
                        "'999999', max_symbol_exposure_usdt, max_total_exposure_usdt, "
                        "existing_symbol_exposure_usdt, existing_total_exposure_usdt, "
                        "effective_leverage, required_reserve_usdt, effective_equity_usdt, "
                        "protective_stop_reference, reduce_only_exit_reference, "
                        ":fingerprint FROM durable_actual_risk_policies WHERE plan_id = :plan_id"
                    ),
                    {"fingerprint": "a" * 64, "plan_id": plan_id},
                )
    finally:
        runtime_engine.dispose()
        admin_engine.dispose()


@pytest.mark.postgresql
def test_postgresql_policy_writes_belong_to_non_login_config_owner(
    fifth_postgresql_database: str,
) -> None:
    command.upgrade(_migration_config(fifth_postgresql_database), "head")
    engine = create_database_engine(fifth_postgresql_database)
    try:
        with engine.connect() as connection:
            role = connection.execute(
                text(
                    "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, "
                    "rolreplication, rolbypassrls FROM pg_roles "
                    "WHERE rolname = 'uta_policy_config'"
                )
            ).one()
            assert role == (False, False, False, False, False, False)
            assert not connection.scalar(
                text("SELECT pg_has_role('uta_runtime', 'uta_policy_config', 'MEMBER')")
            )
            for table_name in (
                "durable_actual_risk_policies",
                "durable_actual_risk_policy_versions",
            ):
                assert (
                    connection.scalar(
                        text(
                            "SELECT tableowner FROM pg_tables "
                            "WHERE schemaname = current_schema() AND tablename = :table_name"
                        ),
                        {"table_name": table_name},
                    )
                    == "uta_policy_config"
                )
                assert connection.scalar(
                    text("SELECT has_table_privilege('uta_policy_config', :table_name, 'INSERT')"),
                    {"table_name": table_name},
                )
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_postgresql_migration_normalizes_preprivileged_runtime_role(
    fifth_postgresql_database: str,
) -> None:
    command.upgrade(_migration_config(fifth_postgresql_database), "0008_processed_events_temp")
    engine = create_database_engine(fifth_postgresql_database)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER ROLE uta_runtime WITH SUPERUSER CREATEDB CREATEROLE "
                    "REPLICATION BYPASSRLS"
                )
            )
        command.upgrade(_migration_config(fifth_postgresql_database), "head")
        with engine.connect() as connection:
            role = connection.execute(
                text(
                    "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, "
                    "rolbypassrls FROM pg_roles WHERE rolname = 'uta_runtime'"
                )
            ).one()
            assert role == (False, False, False, False, False)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER ROLE uta_runtime WITH NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS"
                )
            )
        engine.dispose()


@pytest.mark.postgresql
def test_postgresql_policy_owner_migration_downgrades_and_reapplies(
    fifth_postgresql_database: str,
) -> None:
    config = _migration_config(fifth_postgresql_database)
    command.upgrade(config, "0011_forward_invariants")
    engine = create_database_engine(fifth_postgresql_database)
    try:
        command.downgrade(config, "0009_evidence_risk_hardening")
        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT has_table_privilege('uta_runtime', "
                    "'durable_actual_risk_policies', 'INSERT')"
                )
            )

        command.upgrade(config, "0011_forward_invariants")
        with engine.connect() as connection:
            assert not connection.scalar(
                text(
                    "SELECT has_table_privilege('uta_runtime', "
                    "'durable_actual_risk_policies', 'INSERT')"
                )
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT tableowner FROM pg_tables "
                        "WHERE schemaname = current_schema() "
                        "AND tablename = 'durable_actual_risk_policies'"
                    )
                )
                == "uta_policy_config"
            )
    finally:
        engine.dispose()
