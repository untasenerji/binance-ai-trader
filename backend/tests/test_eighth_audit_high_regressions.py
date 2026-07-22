"""Red-first release blockers from the eighth independent audit."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import Barrier, Event

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.domain.types import Direction
from app.exchange.contracts import (
    AlgoOrderStatus,
    AlgoOrderType,
    ExchangeAlgoOrderObservation,
    ExchangeReconciliationObservationBatch,
    ReconciliationSnapshot,
)
from app.persistence.audit import AuditRepository
from app.persistence.circuit_breaker import PersistenceCircuitBreaker, PersistenceUnavailable
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.persistence.models import (
    DurableAccountSafetyState,
    DurableActualRiskState,
    DurableIntentFill,
    DurableOrderIntent,
    DurableRiskReductionRequirement,
    ExchangeFillFactJournal,
)
from app.planning.fills import (
    AccountPortfolioEnvelope,
    ActualRiskPolicy,
    FillEvent,
    FillLedgerError,
    FillObservationSource,
    FillSide,
)
from app.simulation.intent_ledger import (
    DurableIntentLedger,
    DurableIntentStatus,
    DurableRiskPolicyError,
)
from app.simulation.models import OrderRole, SimulatedOrderIntent
from app.simulation.simulator import ExchangeSimulator
from tests.reconciliation_factory import (
    exchange_reconciliation_batch,
    persist_reconciliation_query_receipt,
)

ACCOUNT_ID = "v1-primary"


def _envelope(*, version: int = 1, cap: Decimal = Decimal("10")) -> AccountPortfolioEnvelope:
    return AccountPortfolioEnvelope(
        account_scope="eighth-audit-account",
        account_id=ACCOUNT_ID,
        version=version,
        verified_account_equity_usdt=cap,
        bot_equity_cap_usdt=cap,
        required_reserve_usdt=Decimal("0"),
        max_total_exposure_usdt=cap,
        max_symbol_exposure_usdt=cap,
        symbol_exposure_caps_usdt={"BTCUSDT": cap},
        max_required_margin_usdt=cap,
        daily_remaining_risk_usdt=Decimal("100"),
        weekly_remaining_risk_usdt=Decimal("100"),
        open_position_count=0,
        pending_order_count=0,
    )


def _policy(
    plan_id: str,
    *,
    direction: Direction = Direction.LONG,
    envelope: AccountPortfolioEnvelope | None = None,
) -> ActualRiskPolicy:
    return ActualRiskPolicy(
        plan_id=plan_id,
        symbol="BTCUSDT",
        direction=direction,
        worst_stop_exit_price=(Decimal("90") if direction is Direction.LONG else Decimal("110")),
        exit_fee_rate=Decimal("0"),
        funding_buffer_rate=Decimal("0"),
        funding_interval_count=0,
        risk_budget=Decimal("100"),
        effective_leverage=2,
        protective_stop_reference=f"{plan_id}-stop",
        reduce_only_exit_reference=f"{plan_id}-reduce",
        portfolio_envelope=envelope or _envelope(),
    )


def _entry(
    client_order_id: str,
    plan_id: str,
    *,
    direction: Direction = Direction.LONG,
    quantity: Decimal = Decimal("0.06"),
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
        price=Decimal("100"),
    )


def _fill(
    intent: SimulatedOrderIntent,
    *,
    trade_id: str,
    quantity: Decimal,
) -> FillEvent:
    return FillEvent(
        account_id=intent.account_id,
        trade_id=trade_id,
        client_order_id=intent.client_order_id,
        symbol=intent.symbol,
        side=FillSide.BUY if intent.direction is Direction.LONG else FillSide.SELL,
        last_quantity=quantity,
        cumulative_quantity=quantity,
        fill_price=Decimal("100"),
        fee=Decimal("0.01"),
        fee_asset="USDT",
        occurred_at=datetime(2026, 7, 16, 8, 0, tzinfo=UTC),
        observation_source=FillObservationSource.SIMULATED_EXCHANGE,
        observation_reference=f"test:{trade_id}",
    )


@pytest.mark.parametrize("direction", (Direction.LONG, Direction.SHORT))
@pytest.mark.parametrize("fill_quantity", (Decimal("0.03"), Decimal("0.06")))
def test_stale_envelope_never_rolls_back_a_late_exchange_fill(
    durable_intent_ledger: DurableIntentLedger,
    direction: Direction,
    fill_quantity: Decimal,
) -> None:
    plan_id = f"stale-fill-{direction.value}-{fill_quantity}"
    intent = _entry(f"{plan_id}-entry", plan_id, direction=direction)
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(plan_id, direction=direction),
    )
    simulator.submit(intent)
    durable_intent_ledger.publish_portfolio_envelope_head(_envelope(version=2, cap=Decimal("2")))
    event = _fill(intent, trade_id=f"{plan_id}-trade", quantity=fill_quantity)

    receipt = durable_intent_ledger.record_fill(
        event,
        materialize_simulated_protection=True,
    )
    duplicate = durable_intent_ledger.record_fill(
        event,
        materialize_simulated_protection=True,
    )
    restarted = durable_intent_ledger.reopen_after_restart()
    restart_duplicate = restarted.record_fill(
        event,
        materialize_simulated_protection=True,
    )
    record = restarted.intent(intent.client_order_id)
    risk = restarted.actual_risk_state(plan_id)

    assert receipt.filled_quantity == fill_quantity
    assert duplicate.is_duplicate
    assert restart_duplicate.is_duplicate
    assert restarted.fill_ledger_for_plan(plan_id).filled_quantity == fill_quantity
    assert record.filled_quantity == fill_quantity
    assert record.status is (
        DurableIntentStatus.FILLED
        if fill_quantity == intent.quantity
        else DurableIntentStatus.CANCEL_REQUIRED
    )
    assert risk.position_quantity == fill_quantity
    assert risk.pending_entries_blocked
    assert restarted.risk_reduction_requirement(plan_id).is_open


def test_new_envelope_head_fences_every_risk_increasing_active_status(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'eighth-envelope-fence.sqlite'}")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    ledger = DurableIntentLedger(session_factory)
    plan_id = "envelope-fence-all-statuses"
    ledger.register_actual_risk_policy(_policy(plan_id))
    statuses = (
        DurableIntentStatus.PREPARED,
        DurableIntentStatus.SUBMITTING,
        DurableIntentStatus.UNKNOWN,
        DurableIntentStatus.NEW,
        DurableIntentStatus.PARTIALLY_FILLED,
    )
    try:
        with session_factory.begin() as session:
            for index, status in enumerate(statuses, 1):
                session.add(
                    DurableOrderIntent(
                        account_id=ACCOUNT_ID,
                        economic_key=f"{plan_id}:BTCUSDT:LONG:ENTRY:{index}",
                        attempt_number=1,
                        client_order_id=f"fenced-{status.value.lower()}",
                        plan_id=plan_id,
                        symbol="BTCUSDT",
                        direction=Direction.LONG.value,
                        role=OrderRole.ENTRY.value,
                        stage_index=index,
                        quantity="0.02",
                        price="100",
                        filled_quantity=(
                            "0.01" if status is DurableIntentStatus.PARTIALLY_FILLED else "0"
                        ),
                        status=status.value,
                    )
                )

        ledger.publish_portfolio_envelope_head(_envelope(version=2, cap=Decimal("5")))
        restarted = ledger.reopen_after_restart()

        for status in statuses:
            record = restarted.intent(f"fenced-{status.value.lower()}")
            assert record.status is DurableIntentStatus.CANCEL_REQUIRED
        assert restarted.intent("fenced-partially_filled").filled_quantity == Decimal("0.01")
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    (
        {"account_id": "other-account"},
        {"symbol": "ETHUSDT"},
        {"side": FillSide.SELL},
        {"observation_source": FillObservationSource.LEGACY_MIGRATION},
    ),
)
def test_fill_identity_must_match_the_durable_intent(
    durable_intent_ledger: DurableIntentLedger,
    mutation: dict[str, object],
) -> None:
    plan_id = "fill-identity-boundary"
    intent = _entry("fill-identity-entry", plan_id)
    ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(plan_id),
    ).submit(intent)
    event = replace(
        _fill(intent, trade_id="fill-identity-trade", quantity=intent.quantity),
        **mutation,  # type: ignore[arg-type]
    )

    with pytest.raises(FillLedgerError):
        durable_intent_ledger.record_fill(event)

    assert durable_intent_ledger.fill_ledger_for_plan(plan_id).filled_quantity == Decimal("0")


def test_risk_evaluation_failure_keeps_fill_and_durably_hard_blocks(
    durable_intent_ledger: DurableIntentLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_id = "post-fill-risk-failure"
    intent = _entry("post-fill-risk-failure-entry", plan_id)
    ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(plan_id),
    ).submit(intent)

    def fail_risk_evaluation(*_: object, **__: object) -> object:
        raise DurableRiskPolicyError("injected deterministic risk failure")

    monkeypatch.setattr(
        DurableIntentLedger,
        "_recalculate_actual_risk",
        classmethod(fail_risk_evaluation),
    )
    event = _fill(intent, trade_id="post-fill-risk-failure-trade", quantity=intent.quantity)

    durable_intent_ledger.record_fill(event, materialize_simulated_protection=True)
    restarted = durable_intent_ledger.reopen_after_restart()
    with restarted._session_factory() as session:  # noqa: SLF001 - durable proof assertion
        account_state = session.get(DurableAccountSafetyState, ACCOUNT_ID)
        risk_state = session.get(DurableActualRiskState, plan_id)

    assert restarted.fill_ledger_for_plan(plan_id).filled_quantity == intent.quantity
    assert restarted.intent(intent.client_order_id).status is DurableIntentStatus.FILLED
    assert risk_state is not None and risk_state.hard_halted
    assert risk_state.pending_entries_blocked
    assert restarted.risk_reduction_requirement(plan_id).is_open
    assert account_state is not None and account_state.recovery_required


def _algo_observation(
    client_algo_id: str,
    *,
    fetched_at: datetime,
    server_time: datetime,
    correlation_id: str,
) -> ExchangeAlgoOrderObservation:
    return ExchangeAlgoOrderObservation(
        source="authenticated_exchange_adapter",
        account_id=ACCOUNT_ID,
        fetched_at=fetched_at,
        server_time=server_time,
        freshness_window=timedelta(seconds=30),
        correlation_id=correlation_id,
        query_epoch=1,
        client_algo_id=client_algo_id,
        symbol="BTCUSDT",
        direction=Direction.LONG,
        algo_type=AlgoOrderType.STOP_MARKET,
        trigger_price=Decimal("90"),
        close_position=True,
        status=AlgoOrderStatus.NEW,
    )


def test_exchange_observation_rejects_future_fetch_and_old_server_time() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="future"):
        _algo_observation(
            "future-stop",
            fetched_at=now + timedelta(minutes=1),
            server_time=now + timedelta(minutes=1),
            correlation_id="future-query",
        )
    with pytest.raises(ValueError, match="server time"):
        _algo_observation(
            "old-server-stop",
            fetched_at=now,
            server_time=now - timedelta(minutes=5),
            correlation_id="old-server-query",
        )


def test_reconciliation_rejects_mixed_query_provenance() -> None:
    now = datetime.now(UTC)
    first = _algo_observation(
        "mixed-stop-one",
        fetched_at=now,
        server_time=now,
        correlation_id="query-one",
    )
    second = _algo_observation(
        "mixed-stop-two",
        fetched_at=now,
        server_time=now,
        correlation_id="query-two",
    )

    with pytest.raises(ValueError, match="correlation"):
        ReconciliationSnapshot(
            account_id=ACCOUNT_ID,
            positions_by_symbol={"BTCUSDT": Decimal("1")},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset({first.client_algo_id, second.client_algo_id}),
            algo_orders=(first, second),
        )


def test_breaker_rejects_raw_reconciliation_maps(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'eighth-raw-snapshot.sqlite'}")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    breaker = PersistenceCircuitBreaker()
    ledger = DurableIntentLedger(session_factory, persistence_breaker=breaker)
    repository = AuditRepository(session_factory, persistence_breaker=breaker)
    try:
        with pytest.raises(TypeError, match="observation batch"):
            breaker.reset_after_verified_reconciliation(
                audit_repository=repository,
                intent_ledger=ledger,
                reconciliation_snapshot=ReconciliationSnapshot(  # type: ignore[arg-type]
                    positions_by_symbol={},
                    normal_order_client_ids=frozenset(),
                    algo_order_client_ids=frozenset(),
                ),
            )
    finally:
        engine.dispose()


def test_reconciliation_batch_rejects_stale_local_and_mixed_provenance() -> None:
    now = datetime.now(UTC)
    pure = ReconciliationSnapshot(
        positions_by_symbol={},
        normal_order_client_ids=frozenset(),
        algo_order_client_ids=frozenset(),
    )
    with pytest.raises(ValueError, match="stale"):
        exchange_reconciliation_batch(
            pure,
            fetched_at=now - timedelta(minutes=1),
            max_age=timedelta(seconds=30),
        )
    with pytest.raises(ValueError, match="authenticated adapter"):
        ExchangeReconciliationObservationBatch(
            source="simulated_exchange",
            account_id=ACCOUNT_ID,
            query_epoch=1,
            correlation_id="local-snapshot",
            requested_at=now - timedelta(milliseconds=2),
            fetched_at=now - timedelta(milliseconds=1),
            server_time=now - timedelta(milliseconds=1),
            max_age=timedelta(seconds=30),
            max_clock_skew=timedelta(seconds=5),
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
        )
    observed = _algo_observation(
        "batch-mismatch-stop",
        fetched_at=now,
        server_time=now,
        correlation_id="child-correlation",
    )
    with pytest.raises(ValueError, match="correlation mismatch"):
        ExchangeReconciliationObservationBatch(
            source="authenticated_exchange_adapter",
            account_id=ACCOUNT_ID,
            query_epoch=1,
            correlation_id="different-batch-correlation",
            requested_at=now - timedelta(milliseconds=1),
            fetched_at=now,
            server_time=now,
            max_age=timedelta(seconds=30),
            max_clock_skew=timedelta(seconds=5),
            positions_by_symbol={"BTCUSDT": Decimal("1")},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset({observed.client_algo_id}),
            algo_orders=(observed,),
        )


def test_restart_rejects_an_expired_durable_reconciliation_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.exchange.contracts as exchange_contracts

    engine = create_database_engine(f"sqlite:///{tmp_path / 'expired-batch.sqlite'}")
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

    class FutureDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> FutureDatetime:
            del tz
            return cls.fromtimestamp(
                (batch.fetched_at + timedelta(minutes=11)).timestamp(),
                tz=UTC,
            )

    monkeypatch.setattr(exchange_contracts, "datetime", FutureDatetime)
    with pytest.raises(PersistenceUnavailable, match="EXCHANGE_SNAPSHOT_EVIDENCE_INVALID"):
        ledger.reopen_after_restart().entry_authorization_capability()
    engine.dispose()


@pytest.mark.postgresql
@pytest.mark.parametrize("privilege", ("INSERT", "UPDATE", "REFERENCES"))
def test_postgresql_runtime_column_level_write_is_detected(
    postgresql_session_factory: sessionmaker[Session],
    privilege: str,
) -> None:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0013_execution_safety_core.py"
    )
    module_spec = spec_from_file_location("audit_eight_0013", migration_path)
    assert module_spec is not None and module_spec.loader is not None
    migration = module_from_spec(module_spec)
    module_spec.loader.exec_module(migration)

    try:
        with postgresql_session_factory.begin() as session:
            connection = session.connection()
            connection.execute(
                text(f"GRANT {privilege} (risk_budget) ON durable_actual_risk_policies TO PUBLIC")
            )
            with pytest.raises(RuntimeError, match="write"):
                migration._verify_runtime_session_privileges(connection)
    finally:
        with postgresql_session_factory.begin() as session:
            session.execute(
                text(
                    f"REVOKE {privilege} (risk_budget) ON durable_actual_risk_policies FROM PUBLIC"
                )
            )


@pytest.mark.postgresql
def test_postgresql_inherited_column_grant_is_enumerated_and_revoked(
    postgresql_session_factory: sessionmaker[Session],
) -> None:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0013_execution_safety_core.py"
    )
    module_spec = spec_from_file_location("audit_eight_0013_inherited", migration_path)
    assert module_spec is not None and module_spec.loader is not None
    migration = module_from_spec(module_spec)
    module_spec.loader.exec_module(migration)
    bridge = f"uta_eighth_acl_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
    try:
        with postgresql_session_factory.begin() as session:
            connection = session.connection()
            connection.execute(text(f'CREATE ROLE "{bridge}" NOLOGIN'))
            connection.execute(
                text(f'GRANT UPDATE (risk_budget) ON durable_actual_risk_policies TO "{bridge}"')
            )
            connection.execute(
                text(f'GRANT "{bridge}" TO uta_runtime WITH INHERIT TRUE, SET FALSE, ADMIN FALSE')
            )
            migration._revoke_policy_acl(connection)
            migration._verify_runtime_session_privileges(connection)
            assert not connection.scalar(
                text(
                    "SELECT has_column_privilege('uta_runtime', "
                    "'durable_actual_risk_policies', 'risk_budget', 'UPDATE')"
                )
            )
    finally:
        with postgresql_session_factory.begin() as session:
            connection = session.connection()
            connection.execute(text(f'REVOKE "{bridge}" FROM uta_runtime'))
            connection.execute(text(f'DROP ROLE IF EXISTS "{bridge}"'))


@pytest.mark.postgresql
@pytest.mark.parametrize("direction", (Direction.LONG, Direction.SHORT))
def test_postgresql_stale_envelope_late_fill_is_durable_and_fenced(
    postgresql_session_factory: sessionmaker[Session],
    direction: Direction,
) -> None:
    ledger = DurableIntentLedger(postgresql_session_factory)
    plan_id = f"postgres-stale-fill-{direction.value}"
    intent = _entry(f"{plan_id}-entry", plan_id, direction=direction)
    ledger.register_actual_risk_policy(_policy(plan_id, direction=direction))
    with postgresql_session_factory.begin() as session:
        session.add(
            DurableOrderIntent(
                account_id=ACCOUNT_ID,
                economic_key=intent.economic_key,
                attempt_number=1,
                client_order_id=intent.client_order_id,
                plan_id=plan_id,
                symbol=intent.symbol,
                direction=direction.value,
                role=OrderRole.ENTRY.value,
                stage_index=1,
                quantity=format(intent.quantity, "f"),
                price=format(intent.price, "f"),
                filled_quantity="0",
                status=DurableIntentStatus.NEW.value,
            )
        )
    ledger.publish_portfolio_envelope_head(_envelope(version=2, cap=Decimal("2")))
    event = _fill(intent, trade_id=f"{plan_id}-trade", quantity=Decimal("0.03"))

    ledger.record_fill(event, materialize_simulated_protection=True)
    restarted = ledger.reopen_after_restart()

    assert restarted.fill_ledger_for_plan(plan_id).filled_quantity == Decimal("0.03")
    assert restarted.intent(intent.client_order_id).status is DurableIntentStatus.CANCEL_REQUIRED
    assert restarted.actual_risk_state(plan_id).pending_entries_blocked
    assert restarted.risk_reduction_requirement(plan_id).is_open


@pytest.mark.postgresql
@pytest.mark.parametrize(
    "crash_phase",
    (
        "after_fill_persisted",
        "before_protection_evaluation",
        "after_protection_evaluation",
        "after_actual_risk",
        "before_pending_cancel",
        "after_pending_cancel",
    ),
)
def test_postgresql_fill_crash_retains_the_journal_and_fences_every_entry(
    postgresql_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    crash_phase: str,
) -> None:
    ledger = DurableIntentLedger(postgresql_session_factory)
    plan_id = f"postgres-fill-uow-{crash_phase}"
    first = replace(
        _entry(f"{plan_id}-stage-1", plan_id),
        quantity=Decimal("0.05"),
    )
    second = replace(
        _entry(f"{plan_id}-stage-2", plan_id),
        stage_index=2,
        quantity=Decimal("0.05"),
    )
    ledger.register_actual_risk_policy(_policy(plan_id))
    with postgresql_session_factory.begin() as session:
        for intent in (first, second):
            session.add(
                DurableOrderIntent(
                    account_id=ACCOUNT_ID,
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
                    status=DurableIntentStatus.NEW.value,
                )
            )

    def crash_at_checkpoint(phase: str) -> None:
        if phase == crash_phase:
            raise RuntimeError(f"crash:{phase}")

    monkeypatch.setattr(
        DurableIntentLedger,
        "_fill_uow_checkpoint",
        staticmethod(crash_at_checkpoint),
    )
    with pytest.raises(RuntimeError, match=f"crash:{crash_phase}"):
        ledger.record_fill(_fill(first, trade_id=f"{plan_id}-fill", quantity=Decimal("0.05")))

    with postgresql_session_factory() as session:
        records = tuple(
            session.scalars(
                select(DurableOrderIntent).where(DurableOrderIntent.plan_id == plan_id)
            ).all()
        )
        fills = tuple(
            session.scalars(
                select(DurableIntentFill).where(
                    DurableIntentFill.client_order_id == first.client_order_id
                )
            ).all()
        )
        risk = session.get(DurableActualRiskState, plan_id)
        reductions = tuple(
            session.scalars(
                select(DurableRiskReductionRequirement).where(
                    DurableRiskReductionRequirement.plan_id == plan_id
                )
            ).all()
        )
        fill_facts = tuple(
            session.scalars(
                select(ExchangeFillFactJournal).where(
                    ExchangeFillFactJournal.exchange_trade_id == f"{plan_id}-fill"
                )
            ).all()
        )

    # Stage B is atomic, but a fill accepted by the exchange remains durable
    # evidence after any crash and closes all entry paths until recovery.
    assert not fills
    assert {row.status for row in records} == {DurableIntentStatus.CANCEL_REQUIRED.value}
    assert {row.filled_quantity for row in records} == {"0"}
    assert risk is not None and Decimal(risk.position_quantity) == Decimal("0")
    assert not reductions
    assert len(fill_facts) == 1
    assert fill_facts[0].apply_status == "RECOVERY_REQUIRED"


def _assert_narrowed_envelope_rechecks_concurrent_fills(
    session_factory: sessionmaker[Session],
) -> None:
    ledger = DurableIntentLedger(session_factory)
    plan_ids = ("concurrent-envelope-long", "concurrent-envelope-short")
    intents = (
        replace(
            _entry("concurrent-envelope-long-entry", plan_ids[0]),
            quantity=Decimal("0.04"),
        ),
        replace(
            _entry(
                "concurrent-envelope-short-entry",
                plan_ids[1],
                direction=Direction.SHORT,
            ),
            quantity=Decimal("0.04"),
        ),
    )
    for plan_id, direction in zip(
        plan_ids,
        (Direction.LONG, Direction.SHORT),
        strict=True,
    ):
        ledger.register_actual_risk_policy(_policy(plan_id, direction=direction))
    with session_factory.begin() as session:
        for intent in intents:
            session.add(
                DurableOrderIntent(
                    account_id=ACCOUNT_ID,
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
                    status=DurableIntentStatus.NEW.value,
                )
            )

    start = Barrier(3)
    fill_committed = (Event(), Event())

    def record(index: int) -> None:
        start.wait()
        try:
            DurableIntentLedger(session_factory).record_fill(
                _fill(
                    intents[index],
                    trade_id=f"concurrent-envelope-fill-{index}",
                    quantity=Decimal("0.04"),
                ),
                materialize_simulated_protection=True,
            )
        finally:
            fill_committed[index].set()

    def narrow_envelope() -> None:
        start.wait()
        assert all(event.wait(timeout=30) for event in fill_committed)
        DurableIntentLedger(session_factory).publish_portfolio_envelope_head(
            _envelope(version=2, cap=Decimal("5"))
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = (
            executor.submit(record, 0),
            executor.submit(record, 1),
            executor.submit(narrow_envelope),
        )
        for future in futures:
            future.result(timeout=45)

    with session_factory() as session:
        durable_states = {
            row.plan_id: row
            for row in session.scalars(
                select(DurableActualRiskState).where(DurableActualRiskState.plan_id.in_(plan_ids))
            ).all()
        }
        durable_reductions = {
            row.plan_id: row
            for row in session.scalars(
                select(DurableRiskReductionRequirement).where(
                    DurableRiskReductionRequirement.plan_id.in_(plan_ids)
                )
            ).all()
        }

    assert set(durable_states) == set(plan_ids)
    assert set(durable_reductions) == set(plan_ids)
    assert all(row.pending_entries_blocked for row in durable_states.values())
    assert all(row.status == "OPEN" for row in durable_reductions.values())

    restarted = ledger.reopen_after_restart()
    assert sum(
        (restarted.fill_ledger_for_plan(plan_id).filled_quantity for plan_id in plan_ids),
        start=Decimal("0"),
    ) == Decimal("0.08")
    for plan_id in plan_ids:
        state = restarted.actual_risk_state(plan_id)
        assert state.pending_entries_blocked
        assert restarted.risk_reduction_requirement(plan_id).is_open


def test_sqlite_narrowed_envelope_rechecks_concurrent_cross_plan_fills(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'concurrent-envelope.sqlite'}")
    create_schema(engine)
    try:
        _assert_narrowed_envelope_rechecks_concurrent_fills(create_session_factory(engine))
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_postgresql_narrowed_envelope_rechecks_concurrent_cross_plan_fills(
    postgresql_session_factory: sessionmaker[Session],
) -> None:
    _assert_narrowed_envelope_rechecks_concurrent_fills(postgresql_session_factory)
