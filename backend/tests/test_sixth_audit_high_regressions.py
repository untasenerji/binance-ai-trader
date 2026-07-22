"""Red-first release blockers from the sixth independent audit."""

import hashlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Integer, MetaData, String, Table, create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.domain.types import Direction
from app.exchange.contracts import (
    AlgoOrderStatus,
    AlgoOrderType,
    ExchangeAlgoOrderObservation,
    ExpectedStopContract,
    LocalReconciliationState,
    ReconciliationSnapshot,
    StopWorkingType,
    reconcile_local_state,
)
from app.persistence.audit import AuditRepository
from app.persistence.circuit_breaker import PersistenceCircuitBreaker, PersistenceUnavailable
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.persistence.replay import ReplayRunner
from app.planning.fills import (
    AccountPortfolioEnvelope,
    ActualRiskPolicy,
    ExposureSourceState,
    FillEvent,
    FillLedgerError,
    FillObservationSource,
    FillSide,
    PortfolioExposureSlice,
)
from app.simulation.intent_ledger import (
    DurableIntentLedger,
    DurableIntentStatus,
    DurableRiskPolicyError,
)
from app.simulation.models import OrderRole, SimulatedFill, SimulatedOrderIntent
from app.simulation.simulator import (
    EntryRiskBlocked,
    ExchangeSimulator,
    FillSequencePlan,
    SimulatorError,
)
from tests.reconciliation_factory import (
    exchange_reconciliation_batch,
    persist_reconciliation_query_receipt,
)


def _policy(
    plan_id: str,
    *,
    direction: Direction = Direction.LONG,
    leverage: int = 2,
    equity: Decimal = Decimal("1000"),
    total_cap: Decimal = Decimal("1000"),
    symbol_cap: Decimal = Decimal("1000"),
    existing_total: Decimal = Decimal("0"),
    existing_leverage: int | None = None,
    portfolio_envelope: AccountPortfolioEnvelope | None = None,
) -> ActualRiskPolicy:
    if portfolio_envelope is None:
        baseline_leverage = leverage if existing_leverage is None else existing_leverage
        exposure_slices: tuple[PortfolioExposureSlice, ...] = ()
        if existing_total > 0:
            exposure_slices = (
                PortfolioExposureSlice(
                    slice_id="external-confirmed",
                    plan_id="external-plan",
                    symbol="BTCUSDT",
                    direction=Direction.LONG,
                    notional_usdt=existing_total,
                    leverage=baseline_leverage,
                    required_margin_usdt=existing_total / Decimal(baseline_leverage),
                    source_state=ExposureSourceState.EXTERNAL_CONFIRMED,
                ),
            )
        portfolio_envelope = AccountPortfolioEnvelope(
            account_scope="sixth-audit-account",
            version=1,
            verified_account_equity_usdt=equity,
            bot_equity_cap_usdt=equity,
            required_reserve_usdt=Decimal("0"),
            max_total_exposure_usdt=total_cap,
            max_symbol_exposure_usdt=symbol_cap,
            max_required_margin_usdt=equity,
            daily_remaining_risk_usdt=Decimal("1000000"),
            weekly_remaining_risk_usdt=Decimal("1000000"),
            open_position_count=0,
            pending_order_count=0,
            exposure_slices=exposure_slices,
        )
    return ActualRiskPolicy(
        plan_id=plan_id,
        symbol="BTCUSDT",
        direction=direction,
        worst_stop_exit_price=(Decimal("90") if direction is Direction.LONG else Decimal("110")),
        exit_fee_rate=Decimal("0"),
        funding_buffer_rate=Decimal("0"),
        funding_interval_count=0,
        risk_budget=Decimal("1000"),
        effective_leverage=leverage,
        protective_stop_reference=f"{plan_id}-expected-stop",
        reduce_only_exit_reference=f"{plan_id}-expected-exit",
        portfolio_envelope=portfolio_envelope,
    )


def _entry(
    client_order_id: str,
    plan_id: str,
    *,
    direction: Direction = Direction.LONG,
    stage_index: int = 1,
    quantity: Decimal = Decimal("0.01"),
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
    price: Decimal = Decimal("100"),
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


@pytest.mark.parametrize(
    ("slice_id", "leverage", "required_margin", "message"),
    (
        ("", 2, Decimal("5"), "identity"),
        ("invalid-leverage", 0, Decimal("5"), "leverage"),
        ("invalid-margin", 2, Decimal("6"), "margin"),
    ),
)
def test_portfolio_exposure_slice_rejects_invalid_financial_facts(
    slice_id: str,
    leverage: int,
    required_margin: Decimal,
    message: str,
) -> None:
    with pytest.raises(FillLedgerError, match=message):
        PortfolioExposureSlice(
            slice_id=slice_id,
            plan_id="slice-validation-plan",
            symbol="BTCUSDT",
            direction=Direction.LONG,
            notional_usdt=Decimal("10"),
            leverage=leverage,
            required_margin_usdt=required_margin,
            source_state=ExposureSourceState.EXTERNAL_CONFIRMED,
        )


@pytest.mark.parametrize(
    "fill_sequence",
    (
        (
            SimulatedFill(
                trade_id="decreasing-first",
                last_quantity=Decimal("0.02"),
                cumulative_quantity=Decimal("0.02"),
                fill_price=Decimal("100"),
                fee=Decimal("0"),
                fee_asset="USDT",
            ),
            SimulatedFill(
                trade_id="decreasing-second",
                last_quantity=Decimal("0.01"),
                cumulative_quantity=Decimal("0.01"),
                fill_price=Decimal("100"),
                fee=Decimal("0"),
                fee_asset="USDT",
            ),
        ),
        (
            SimulatedFill(
                trade_id="inconsistent-cumulative",
                last_quantity=Decimal("0.01"),
                cumulative_quantity=Decimal("0.02"),
                fill_price=Decimal("100"),
                fee=Decimal("0"),
                fee_asset="USDT",
            ),
        ),
        (
            SimulatedFill(
                trade_id="exceeds-planned",
                last_quantity=Decimal("0.06"),
                cumulative_quantity=Decimal("0.06"),
                fill_price=Decimal("100"),
                fee=Decimal("0"),
                fee_asset="USDT",
            ),
        ),
    ),
)
def test_simulator_rejects_unsafe_fill_quantity_sequences(
    durable_intent_ledger: DurableIntentLedger,
    fill_sequence: tuple[SimulatedFill, ...],
) -> None:
    plan_id = f"unsafe-fill-{fill_sequence[0].trade_id}"
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(plan_id),
        fill_plan=FillSequencePlan.from_sequences((fill_sequence,)),
    )

    with pytest.raises(SimulatorError, match="cumulative|planned"):
        simulator.submit(
            _entry(
                f"{plan_id}-entry",
                plan_id,
                quantity=Decimal("0.05"),
            )
        )


def test_external_confirmed_margin_cannot_use_candidate_plan_leverage(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "candidate-hundred-x"
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(
            plan_id,
            direction=Direction.SHORT,
            leverage=100,
            equity=Decimal("10"),
            existing_total=Decimal("9"),
            existing_leverage=1,
        ),
    )

    with pytest.raises(EntryRiskBlocked, match="MARGIN"):
        simulator.submit(
            _entry(
                "candidate-hundred-x-entry",
                plan_id,
                direction=Direction.SHORT,
                quantity=Decimal("2"),
            )
        )


@pytest.mark.parametrize("strict_first", [True, False])
def test_account_caps_are_independent_of_policy_registration_order(
    durable_intent_ledger: DurableIntentLedger,
    strict_first: bool,
) -> None:
    strict = _policy(
        "strict-account-plan",
        equity=Decimal("10"),
        total_cap=Decimal("10"),
        symbol_cap=Decimal("10"),
    )
    loose = _policy(
        "loose-account-plan",
        direction=Direction.SHORT,
        leverage=100,
        portfolio_envelope=strict.portfolio_envelope,
    )
    first, second = (strict, loose) if strict_first else (loose, strict)
    first_simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=first,
    )
    first_simulator.submit(
        _entry(
            f"{first.plan_id}-entry",
            first.plan_id,
            direction=first.direction,
            quantity=Decimal("0.09"),
        )
    )
    second_simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=second,
    )

    with pytest.raises(EntryRiskBlocked):
        second_simulator.submit(
            _entry(
                f"{second.plan_id}-entry",
                second.plan_id,
                direction=second.direction,
                quantity=Decimal("2"),
            )
        )


@pytest.mark.parametrize(
    "registration_order",
    (("mixed-long", "mixed-short"), ("mixed-short", "mixed-long")),
)
def test_mixed_portfolio_margin_and_exposure_are_order_independent(
    durable_intent_ledger: DurableIntentLedger,
    registration_order: tuple[str, str],
) -> None:
    envelope = AccountPortfolioEnvelope(
        account_scope="mixed-account",
        version=7,
        verified_account_equity_usdt=Decimal("100"),
        bot_equity_cap_usdt=Decimal("100"),
        required_reserve_usdt=Decimal("1"),
        max_total_exposure_usdt=Decimal("100"),
        max_symbol_exposure_usdt=Decimal("100"),
        max_required_margin_usdt=Decimal("100"),
        daily_remaining_risk_usdt=Decimal("1000"),
        weekly_remaining_risk_usdt=Decimal("1000"),
        open_position_count=1,
        pending_order_count=0,
        exposure_slices=(
            PortfolioExposureSlice(
                slice_id="external-long-one-x",
                plan_id="external-long",
                symbol="BTCUSDT",
                direction=Direction.LONG,
                notional_usdt=Decimal("9"),
                leverage=1,
                required_margin_usdt=Decimal("9"),
                source_state=ExposureSourceState.EXTERNAL_CONFIRMED,
            ),
        ),
    )
    policies = {
        "mixed-long": _policy(
            "mixed-long",
            direction=Direction.LONG,
            leverage=2,
            portfolio_envelope=envelope,
        ),
        "mixed-short": _policy(
            "mixed-short",
            direction=Direction.SHORT,
            leverage=10,
            portfolio_envelope=envelope,
        ),
    }
    quantities = {"mixed-long": Decimal("0.10"), "mixed-short": Decimal("0.20")}
    simulators: dict[str, ExchangeSimulator] = {}
    for plan_id in registration_order:
        simulators[plan_id] = ExchangeSimulator(
            intent_ledger=durable_intent_ledger,
            actual_risk_policy=policies[plan_id],
        )

    # Publishing the account envelope invalidates the fixture's earlier recovery
    # grant. A fresh, durable reconciliation must precede either entry.
    breaker = durable_intent_ledger._persistence_breaker  # noqa: SLF001
    repository = AuditRepository(
        durable_intent_ledger._session_factory,  # noqa: SLF001
        persistence_breaker=breaker,
    )
    reconciliation_batch = exchange_reconciliation_batch(
        ReconciliationSnapshot(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
        )
    )
    persist_reconciliation_query_receipt(durable_intent_ledger, reconciliation_batch)
    breaker.reset_after_verified_reconciliation(
        audit_repository=repository,
        intent_ledger=durable_intent_ledger,
        reconciliation_snapshot=reconciliation_batch,
    )

    for plan_id in registration_order:
        simulators[plan_id].submit(
            _entry(
                f"{plan_id}-entry",
                plan_id,
                direction=policies[plan_id].direction,
                quantity=quantities[plan_id],
            )
        )

    candidate = _policy(
        "mixed-candidate",
        direction=Direction.SHORT,
        leverage=100,
        portfolio_envelope=envelope,
    )
    durable_intent_ledger.register_actual_risk_policy(candidate)
    assessment = durable_intent_ledger.projected_entry_risk(
        _entry(
            "mixed-candidate-entry",
            candidate.plan_id,
            direction=Direction.SHORT,
            quantity=Decimal("0.30"),
        )
    )

    assert assessment.aggregate_total_exposure_usdt == Decimal("69")
    assert assessment.required_margin_usdt == Decimal("17.3")
    assert not assessment.blocked
    assert tuple(item.slice_id for item in assessment.exposure_slices) == tuple(
        sorted(item.slice_id for item in assessment.exposure_slices)
    )
    assert {item.direction for item in assessment.exposure_slices} == {
        Direction.LONG,
        Direction.SHORT,
    }


def test_confirmed_partial_pending_and_proposed_slices_are_aggregated_once(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "mixed-source-states"
    simulator = ExchangeSimulator(
        intent_ledger=durable_intent_ledger,
        actual_risk_policy=_policy(plan_id),
    )
    first = _entry(f"{plan_id}-stage-1", plan_id, quantity=Decimal("0.10"))
    simulator.submit(first)
    durable_intent_ledger.record_fill(
        _fill(
            first.client_order_id,
            "mixed-source-partial-fill",
            quantity=Decimal("0.04"),
        ),
        materialize_simulated_protection=True,
    )
    simulator.submit(
        _entry(
            f"{plan_id}-stage-2",
            plan_id,
            stage_index=2,
            quantity=Decimal("0.05"),
        )
    )

    assessment = durable_intent_ledger.projected_entry_risk(
        _entry(
            f"{plan_id}-stage-3",
            plan_id,
            stage_index=3,
            quantity=Decimal("0.02"),
        )
    )

    assert assessment.position_exposure_usdt == Decimal("4")
    assert assessment.pending_order_exposure_usdt == Decimal("13")
    assert assessment.aggregate_total_exposure_usdt == Decimal("17")
    assert {item.source_state for item in assessment.exposure_slices} == {
        ExposureSourceState.CONFIRMED,
        ExposureSourceState.PARTIALLY_FILLED,
        ExposureSourceState.PENDING,
    }


def test_conflicting_account_envelope_facts_fail_closed(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    first = _policy("envelope-authority-one")
    durable_intent_ledger.register_actual_risk_policy(first)
    conflicting = AccountPortfolioEnvelope(
        account_scope=first.portfolio_envelope.account_scope,
        version=first.portfolio_envelope.version,
        verified_account_equity_usdt=Decimal("999"),
        bot_equity_cap_usdt=first.portfolio_envelope.bot_equity_cap_usdt,
        required_reserve_usdt=first.portfolio_envelope.required_reserve_usdt,
        max_total_exposure_usdt=first.portfolio_envelope.max_total_exposure_usdt,
        max_symbol_exposure_usdt=first.portfolio_envelope.max_symbol_exposure_usdt,
        max_required_margin_usdt=first.portfolio_envelope.max_required_margin_usdt,
        daily_remaining_risk_usdt=first.portfolio_envelope.daily_remaining_risk_usdt,
        weekly_remaining_risk_usdt=first.portfolio_envelope.weekly_remaining_risk_usdt,
        open_position_count=first.portfolio_envelope.open_position_count,
        pending_order_count=first.portfolio_envelope.pending_order_count,
    )

    with pytest.raises(DurableRiskPolicyError, match="conflicting"):
        durable_intent_ledger.register_actual_risk_policy(
            _policy("envelope-authority-two", portfolio_envelope=conflicting)
        )


def test_v1_rejects_a_second_account_before_it_can_share_risk_state(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    first = _policy("independent-account-one")
    second_envelope = replace(
        first.portfolio_envelope,
        account_id="second-account",
        fingerprint="",
    )

    durable_intent_ledger.register_actual_risk_policy(first)
    with pytest.raises(DurableRiskPolicyError, match="V1_SECOND_ACCOUNT_UNSUPPORTED"):
        durable_intent_ledger.register_actual_risk_policy(
            _policy("independent-account-two", portfolio_envelope=second_envelope)
        )


def _prepare_two_active_stages(ledger: DurableIntentLedger, plan_id: str) -> None:
    simulator = ExchangeSimulator(
        intent_ledger=ledger,
        actual_risk_policy=_policy(plan_id),
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


def test_missing_protection_cancels_pending_and_writes_reduction_atomically(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    plan_id = "protection-uow"
    _prepare_two_active_stages(durable_intent_ledger, plan_id)

    durable_intent_ledger.record_fill(
        _fill(f"{plan_id}-stage-1", "protection-uow-fill", quantity=Decimal("0.05"))
    )
    restarted = durable_intent_ledger.reopen_after_restart()

    assert restarted.intent(f"{plan_id}-stage-2").status is DurableIntentStatus.CANCEL_REQUIRED
    requirement = restarted.risk_reduction_requirement(plan_id)
    assert requirement.reason == "SIMULATED_PROTECTION_MISSING"
    receipt = restarted.record_fill(
        _fill(f"{plan_id}-stage-2", "halted-late-fill", quantity=Decimal("0.05"))
    )
    assert receipt.filled_quantity == Decimal("0.05")
    assert restarted.intent(f"{plan_id}-stage-2").status is DurableIntentStatus.FILLED
    assert restarted.actual_risk_state(plan_id).position_quantity == Decimal("0.10")


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
def test_fill_protection_and_pending_cancel_crash_as_one_unit_of_work(
    durable_intent_ledger: DurableIntentLedger,
    monkeypatch: pytest.MonkeyPatch,
    crash_phase: str,
) -> None:
    plan_id = f"fill-uow-{crash_phase}"
    _prepare_two_active_stages(durable_intent_ledger, plan_id)

    def crash_at_checkpoint(phase: str) -> None:
        if phase == crash_phase:
            raise RuntimeError(f"crash:{phase}")

    monkeypatch.setattr(
        DurableIntentLedger,
        "_fill_uow_checkpoint",
        staticmethod(crash_at_checkpoint),
    )

    with pytest.raises(RuntimeError, match=f"crash:{crash_phase}"):
        durable_intent_ledger.record_fill(
            _fill(
                f"{plan_id}-stage-1",
                f"{plan_id}-fill",
                quantity=Decimal("0.05"),
            )
        )

    # Stage A must survive every application-side crash. Restoring the crash seam
    # simulates a fresh process whose recovery worker can apply the retained fact.
    monkeypatch.setattr(
        DurableIntentLedger,
        "_fill_uow_checkpoint",
        staticmethod(lambda _phase: None),
    )
    restarted = durable_intent_ledger.reopen_after_restart()
    assert restarted.fill_ledger_for_plan(plan_id).filled_quantity == Decimal("0.05")
    assert restarted.intent(f"{plan_id}-stage-1").status is DurableIntentStatus.FILLED
    assert restarted.intent(f"{plan_id}-stage-2").status is DurableIntentStatus.CANCEL_REQUIRED
    assert restarted.actual_risk_state(plan_id).position_quantity == Decimal("0.05")
    assert restarted.risk_reduction_requirement(plan_id).reason == "SIMULATED_PROTECTION_MISSING"


def test_published_0009_matches_current_head_bytes() -> None:
    migration_bytes = (
        Path(__file__).parents[1] / "alembic" / "versions" / "0009_evidence_risk_hardening.py"
    ).read_bytes()
    canonical_bytes = migration_bytes.replace(b"\r\n", b"\n")

    assert hashlib.sha256(canonical_bytes).hexdigest() == (
        "88b2bf7ebb564c09a02aad5590ae728680e5aa7db8eaa289de2db2ca96825f8c"
    )


def _migration_config(database_url: str) -> Config:
    backend_root = Path(__file__).parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _restore_legacy_0009_query_nullability(connection: Connection) -> None:
    operations = Operations(MigrationContext.configure(connection))
    columns = (
        ("query_reference", String(length=128)),
        ("query_client_order_id", String(length=128)),
        ("query_economic_key", String(length=256)),
        ("query_started_at_ms", Integer()),
    )
    if connection.dialect.name == "sqlite":
        with operations.batch_alter_table(
            "durable_intent_absence_observations",
            recreate="always",
        ) as batch_operations:
            for column_name, column_type in columns:
                batch_operations.alter_column(
                    column_name,
                    existing_type=column_type,
                    nullable=True,
                )
        return
    for column_name, column_type in columns:
        operations.alter_column(
            "durable_intent_absence_observations",
            column_name,
            existing_type=column_type,
            nullable=True,
        )


def _populate_published_0009(database_url: str) -> None:
    command.upgrade(_migration_config(database_url), "0009_evidence_risk_hardening")
    engine = create_database_engine(database_url)
    metadata = MetaData()
    intents = Table("durable_order_intents", metadata, autoload_with=engine)
    observations = Table(
        "durable_intent_absence_observations",
        metadata,
        autoload_with=engine,
    )
    cases = (
        ("valid", "valid-query", 1_500, "", "TRADE_HISTORY", 2_000),
        ("null-time", None, None, "", "TRADE_HISTORY", 2_100),
        ("duplicate-one", "duplicate-query", 1_500, "", "NORMAL_OPEN_ORDERS", 2_200),
        ("duplicate-two", "duplicate-query", 1_500, "", "ALGO_OPEN_ORDERS", 2_300),
        ("invalid-provenance", "invalid-query", 1_500, "f" * 64, "TRADE_HISTORY", 2_400),
    )
    try:
        with engine.begin() as connection:
            for index_name in (
                "uq_absence_query_reference",
                "uq_absence_provenance_fingerprint",
            ):
                connection.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
            _restore_legacy_0009_query_nullability(connection)
            for suffix, query_reference, query_started_at, provenance, source, observed_at in cases:
                client_order_id = f"published-0009-{suffix}"
                economic_key = f"published-0009-{suffix}:BTCUSDT:LONG:ENTRY:1"
                connection.execute(
                    intents.insert().values(
                        economic_key=economic_key,
                        attempt_number=1,
                        client_order_id=client_order_id,
                        plan_id=f"published-0009-{suffix}",
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
                        client_order_id=client_order_id,
                        economic_key=economic_key,
                        source=source,
                        query_reference=query_reference,
                        query_client_order_id=(None if suffix == "null-time" else client_order_id),
                        query_economic_key=(None if suffix == "null-time" else economic_key),
                        query_started_at_ms=query_started_at,
                        attempt_number=1,
                        client_order_namespace="NORMAL",
                        provenance_fingerprint=provenance,
                        observed_at_ms=observed_at,
                        stream_watermark_ms=observed_at,
                        found=False,
                    )
                )
    finally:
        engine.dispose()


def _assert_published_0009_upgrade(database_url: str) -> None:
    command.upgrade(_migration_config(database_url), "head")
    engine = create_database_engine(database_url)
    try:
        inspector = inspect(engine)
        query_columns = {
            column["name"]: column
            for column in inspector.get_columns("durable_intent_absence_observations")
        }
        for column_name in (
            "query_reference",
            "query_client_order_id",
            "query_economic_key",
            "query_started_at_ms",
        ):
            assert not query_columns[column_name]["nullable"]
        with engine.connect() as connection:
            reasons = tuple(
                connection.scalars(
                    text("SELECT reason FROM migration_quarantine_records ORDER BY reason")
                )
            )
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0014_durable_execution_facts"
            )
        assert reasons == (
            "LEGACY_DUPLICATE_QUERY_EVIDENCE",
            "LEGACY_PROVENANCE_INVALID",
        )

        session_factory = create_session_factory(engine)
        ledger = DurableIntentLedger(session_factory)
        repository = AuditRepository(session_factory)
        assert len(ledger.list_absence_observations("published-0009-valid")) == 1
        normalized_null = ledger.list_absence_observations("published-0009-null-time")
        assert len(normalized_null) == 1
        assert normalized_null[0].query_started_at_ms == 1_500
        assert (
            ReplayRunner()
            .replay(repository.list_audit_events(), repository.audit_chain_head())
            .is_valid
        )
        breaker = PersistenceCircuitBreaker()
        authorized_ledger = DurableIntentLedger(
            session_factory,
            persistence_breaker=breaker,
        )
        authorized_repository = AuditRepository(
            session_factory,
            persistence_breaker=breaker,
        )
        reconciliation_batch = exchange_reconciliation_batch(
            ReconciliationSnapshot(
                positions_by_symbol={},
                normal_order_client_ids=frozenset(),
                algo_order_client_ids=frozenset(),
            )
        )
        persist_reconciliation_query_receipt(authorized_ledger, reconciliation_batch)
        with pytest.raises(PersistenceUnavailable, match="UNRESOLVED_QUARANTINE"):
            breaker.reset_after_verified_reconciliation(
                audit_repository=authorized_repository,
                intent_ledger=authorized_ledger,
                reconciliation_snapshot=reconciliation_batch,
            )
    finally:
        engine.dispose()


def _head_schema_signature(database_url: str) -> dict[str, object]:
    engine = create_database_engine(database_url)
    try:
        inspector = inspect(engine)
        return {
            table_name: {
                "columns": tuple(
                    sorted(
                        (column["name"], bool(column["nullable"]))
                        for column in inspector.get_columns(table_name)
                    )
                ),
                "indexes": tuple(
                    sorted(
                        (index["name"], bool(index["unique"]))
                        for index in inspector.get_indexes(table_name)
                    )
                ),
            }
            for table_name in sorted(inspector.get_table_names())
            if table_name != "alembic_version"
        }
    finally:
        engine.dispose()


def test_forward_migration_0014_is_the_only_current_head(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'sixth-head.sqlite'}"
    config = _migration_config(database_url)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0014_durable_execution_facts"
            )
    finally:
        engine.dispose()


def test_sqlite_published_0009_upgrades_replays_and_matches_fresh_head(
    tmp_path: Path,
) -> None:
    legacy_url = f"sqlite:///{tmp_path / 'published-0009.sqlite'}"
    fresh_url = f"sqlite:///{tmp_path / 'fresh-head.sqlite'}"
    _populate_published_0009(legacy_url)
    _assert_published_0009_upgrade(legacy_url)
    command.upgrade(_migration_config(fresh_url), "head")

    assert _head_schema_signature(legacy_url) == _head_schema_signature(fresh_url)


def test_sqlite_forward_migration_retries_after_partial_ddl_failure(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'retry-0011.sqlite'}"
    _populate_published_0009(database_url)
    failed_once = False

    def fail_authorization_table_once(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal failed_once
        normalized = " ".join(statement.casefold().split())
        if not failed_once and "create table durable_entry_authorization_grants" in normalized:
            failed_once = True
            raise RuntimeError("injected 0011 migration interruption")

    event.listen(Engine, "before_cursor_execute", fail_authorization_table_once)
    try:
        with pytest.raises(RuntimeError, match="migration interruption"):
            command.upgrade(_migration_config(database_url), "head")
    finally:
        event.remove(Engine, "before_cursor_execute", fail_authorization_table_once)

    assert failed_once
    _assert_published_0009_upgrade(database_url)


@pytest.fixture
def sixth_postgresql_database() -> Iterator[str]:
    database_name = f"uta_sixth_audit_{uuid4().hex}"
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
def test_postgresql_published_0009_upgrades_and_recovers(
    sixth_postgresql_database: str,
) -> None:
    _populate_published_0009(sixth_postgresql_database)
    _assert_published_0009_upgrade(sixth_postgresql_database)


@pytest.mark.postgresql
def test_forward_migration_rejects_transitive_runtime_write_role_membership(
    sixth_postgresql_database: str,
) -> None:
    config = _migration_config(sixth_postgresql_database)
    command.upgrade(config, "0010_runtime_policy_owner")
    engine = create_database_engine(sixth_postgresql_database)
    bridge_role = f"uta_six_bridge_{uuid4().hex}"
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE ROLE "{bridge_role}" NOLOGIN'))
            connection.execute(text(f'GRANT uta_policy_config TO "{bridge_role}"'))
            connection.execute(text(f'GRANT "{bridge_role}" TO uta_runtime'))
        with pytest.raises((DBAPIError, RuntimeError)):
            command.upgrade(config, "head")
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'REVOKE "{bridge_role}" FROM uta_runtime'))
            connection.execute(text(f'REVOKE uta_policy_config FROM "{bridge_role}"'))
            connection.execute(text(f'DROP ROLE "{bridge_role}"'))
        command.upgrade(config, "head")
        engine.dispose()


@pytest.mark.postgresql
@pytest.mark.parametrize(
    ("target_kind", "bridge_depth"),
    (
        ("config_owner", 0),
        ("config_owner", 3),
        ("predefined_write", 2),
        ("table_owner", 2),
    ),
)
def test_forward_migration_rejects_effective_runtime_role_graph_paths(
    sixth_postgresql_database: str,
    target_kind: str,
    bridge_depth: int,
) -> None:
    config = _migration_config(sixth_postgresql_database)
    command.upgrade(config, "0010_runtime_policy_owner")
    engine = create_database_engine(sixth_postgresql_database)
    suffix = uuid4().hex
    bridges = [f"uta_six_bridge_{index}_{suffix}" for index in range(bridge_depth)]
    owner_role = f"uta_six_owner_{suffix}"
    target_role = {
        "config_owner": "uta_policy_config",
        "predefined_write": "pg_write_all_data",
        "table_owner": owner_role,
    }[target_kind]
    try:
        with engine.begin() as connection:
            if target_kind == "table_owner":
                connection.execute(text(f'CREATE ROLE "{owner_role}" NOLOGIN'))
                connection.execute(
                    text(f'ALTER TABLE durable_actual_risk_policies OWNER TO "{owner_role}"')
                )
            for bridge in bridges:
                connection.execute(text(f'CREATE ROLE "{bridge}" NOLOGIN'))
            membership_chain = [target_role, *reversed(bridges), "uta_runtime"]
            for granted_role, member_role in zip(
                membership_chain,
                membership_chain[1:],
                strict=False,
            ):
                connection.execute(text(f'GRANT "{granted_role}" TO "{member_role}" WITH SET TRUE'))

        with pytest.raises((DBAPIError, RuntimeError), match="SET ROLE"):
            command.upgrade(config, "head")
    finally:
        with engine.begin() as connection:
            membership_chain = [target_role, *reversed(bridges), "uta_runtime"]
            for granted_role, member_role in reversed(
                tuple(zip(membership_chain, membership_chain[1:], strict=False))
            ):
                connection.execute(text(f'REVOKE "{granted_role}" FROM "{member_role}"'))
            if target_kind == "table_owner":
                connection.execute(
                    text("ALTER TABLE durable_actual_risk_policies OWNER TO uta_policy_config")
                )
            for bridge in reversed(bridges):
                connection.execute(text(f'DROP ROLE "{bridge}"'))
            if target_kind == "table_owner":
                connection.execute(text(f'DROP ROLE "{owner_role}"'))
        command.upgrade(config, "head")
        engine.dispose()


def test_breaker_reset_rejects_correct_stop_id_with_wrong_trigger(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'wrong-stop-trigger.sqlite'}")
    create_schema(engine)
    session_factory = create_session_factory(engine)
    breaker = PersistenceCircuitBreaker()
    ledger = DurableIntentLedger(session_factory, persistence_breaker=breaker)
    repository = AuditRepository(session_factory, persistence_breaker=breaker)
    initial_batch = exchange_reconciliation_batch(
        ReconciliationSnapshot(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
        )
    )
    persist_reconciliation_query_receipt(ledger, initial_batch)
    breaker.reset_after_verified_reconciliation(
        audit_repository=repository,
        intent_ledger=ledger,
        reconciliation_snapshot=initial_batch,
    )
    plan_id = "wrong-stop-trigger"
    simulator = ExchangeSimulator(
        intent_ledger=ledger,
        actual_risk_policy=_policy(plan_id),
        fill_plan=FillSequencePlan.from_sequences(
            (
                (
                    SimulatedFill(
                        trade_id="wrong-stop-trigger-fill",
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
    simulator.submit(_entry("wrong-stop-trigger-entry", plan_id))
    breaker.record_write_failure(RuntimeError("force re-verification"))

    invalid_batch = exchange_reconciliation_batch(
        ReconciliationSnapshot(
            positions_by_symbol={"BTCUSDT": Decimal("0.01")},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset({f"{plan_id}-expected-stop"}),
            algo_orders=(
                ExchangeAlgoOrderObservation(
                    source="authenticated_exchange_adapter",
                    account_id="v1-primary",
                    fetched_at=datetime.now(UTC),
                    server_time=datetime.now(UTC),
                    freshness_window=timedelta(seconds=30),
                    correlation_id="wrong-stop-trigger-observation",
                    query_epoch=1,
                    client_algo_id=f"{plan_id}-expected-stop",
                    symbol="BTCUSDT",
                    direction=Direction.LONG,
                    algo_type=AlgoOrderType.STOP_MARKET,
                    trigger_price=Decimal("50"),
                    close_position=True,
                ),
            ),
        )
    )
    persist_reconciliation_query_receipt(ledger, invalid_batch)
    with pytest.raises(PersistenceUnavailable):
        breaker.reset_after_verified_reconciliation(
            audit_repository=repository,
            intent_ledger=ledger,
            reconciliation_snapshot=invalid_batch,
        )
    engine.dispose()


def _observed_stop(
    contract: ExpectedStopContract,
    **overrides: object,
) -> ExchangeAlgoOrderObservation:
    observed_at = datetime.now(UTC)
    values: dict[str, object] = {
        "source": "authenticated_exchange_adapter",
        "account_id": contract.account_id,
        "fetched_at": observed_at,
        "server_time": observed_at,
        "freshness_window": timedelta(seconds=30),
        "correlation_id": f"sixth-audit-{contract.client_algo_id}",
        "query_epoch": 1,
        "client_algo_id": contract.client_algo_id,
        "symbol": contract.symbol,
        "direction": contract.position_side,
        "algo_type": contract.algo_type,
        "trigger_price": contract.trigger_price,
        "close_position": contract.close_position,
        "quantity": None,
        "working_type": contract.working_type,
        "status": contract.active_status,
        "plan_id": contract.plan_id,
        "policy_version": contract.policy_version,
        "account_envelope_version": contract.account_envelope_version,
        "policy_fingerprint": contract.policy_fingerprint,
        "account_envelope_fingerprint": contract.account_envelope_fingerprint,
        "stop_contract_fingerprint": contract.fingerprint,
    }
    values.update(overrides)
    return ExchangeAlgoOrderObservation(**values)  # type: ignore[arg-type]


def _fill_plan_for_stop_contract(
    ledger: DurableIntentLedger,
    *,
    plan_id: str,
    direction: Direction,
) -> ExpectedStopContract:
    simulator = ExchangeSimulator(
        intent_ledger=ledger,
        actual_risk_policy=_policy(plan_id, direction=direction),
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
    facts = ledger.reconciliation_facts()
    return next(
        contract for contract in facts.expected_stop_contracts if contract.plan_id == plan_id
    )


def _local_reconciliation_state(ledger: DurableIntentLedger) -> LocalReconciliationState:
    facts = ledger.reconciliation_facts()
    return LocalReconciliationState(
        positions_by_symbol=facts.positions_by_symbol,
        normal_order_client_ids=facts.normal_order_client_ids,
        algo_order_client_ids=facts.algo_order_client_ids,
        required_stop_symbols=facts.required_stop_symbols,
        unresolved_unknown_intent_ids=facts.unresolved_unknown_intent_ids,
        audit_chain_valid=True,
        replay_valid=True,
        expected_stop_contracts=facts.expected_stop_contracts,
    )


@pytest.mark.parametrize("direction", (Direction.LONG, Direction.SHORT))
@pytest.mark.parametrize(
    "mismatch",
    (
        "trigger_price",
        "algo_type",
        "working_type",
        "close_position",
        "quantity",
        "direction",
        "status",
        "plan_id",
        "policy_fingerprint",
    ),
)
def test_complete_stop_contract_rejects_every_mismatched_exchange_field(
    durable_intent_ledger: DurableIntentLedger,
    direction: Direction,
    mismatch: str,
) -> None:
    plan_id = f"stop-matrix-{direction.value.lower()}-{mismatch}"
    contract = _fill_plan_for_stop_contract(
        durable_intent_ledger,
        plan_id=plan_id,
        direction=direction,
    )
    overrides: dict[str, object] = {
        "trigger_price": contract.trigger_price + Decimal("1"),
        "algo_type": AlgoOrderType.TAKE_PROFIT_MARKET,
        "working_type": (
            StopWorkingType.CONTRACT_PRICE
            if contract.working_type is StopWorkingType.MARK_PRICE
            else StopWorkingType.MARK_PRICE
        ),
        "close_position": False,
        "quantity": Decimal("0.01"),
        "direction": (Direction.SHORT if direction is Direction.LONG else Direction.LONG),
        "status": AlgoOrderStatus.CANCELED,
        "plan_id": f"{plan_id}-wrong",
        "policy_fingerprint": "f" * 64,
    }
    observed = _observed_stop(contract, **{mismatch: overrides[mismatch]})
    local = _local_reconciliation_state(durable_intent_ledger)

    exact = reconcile_local_state(
        local=local,
        snapshot=ReconciliationSnapshot(
            positions_by_symbol=local.positions_by_symbol,
            normal_order_client_ids=local.normal_order_client_ids,
            algo_order_client_ids=frozenset({contract.client_algo_id}),
            algo_orders=(_observed_stop(contract),),
        ),
    )
    mismatched = reconcile_local_state(
        local=local,
        snapshot=ReconciliationSnapshot(
            positions_by_symbol=local.positions_by_symbol,
            normal_order_client_ids=local.normal_order_client_ids,
            algo_order_client_ids=frozenset({contract.client_algo_id}),
            algo_orders=(observed,),
        ),
    )

    assert exact.is_clean
    assert not mismatched.is_clean
    assert mismatched.invalid_stop_contract_ids == (contract.client_algo_id,)
    assert mismatched.missing_stop_symbols == (contract.symbol,)


def test_same_symbol_multi_plan_stops_bind_to_plan_and_policy_fingerprint(
    durable_intent_ledger: DurableIntentLedger,
) -> None:
    first = _fill_plan_for_stop_contract(
        durable_intent_ledger,
        plan_id="same-symbol-plan-one",
        direction=Direction.LONG,
    )
    second = _fill_plan_for_stop_contract(
        durable_intent_ledger,
        plan_id="same-symbol-plan-two",
        direction=Direction.LONG,
    )
    local = _local_reconciliation_state(durable_intent_ledger)
    exact_orders = (
        _observed_stop(first, correlation_id="same-symbol-query"),
        _observed_stop(second, correlation_id="same-symbol-query"),
    )
    exact = reconcile_local_state(
        local=local,
        snapshot=ReconciliationSnapshot(
            positions_by_symbol=local.positions_by_symbol,
            normal_order_client_ids=local.normal_order_client_ids,
            algo_order_client_ids=frozenset(order.client_algo_id for order in exact_orders),
            algo_orders=exact_orders,
        ),
    )
    swapped_orders = (
        _observed_stop(
            first,
            correlation_id="same-symbol-swapped-query",
            plan_id=second.plan_id,
            policy_fingerprint=second.policy_fingerprint,
        ),
        _observed_stop(
            second,
            correlation_id="same-symbol-swapped-query",
            plan_id=first.plan_id,
            policy_fingerprint=first.policy_fingerprint,
        ),
    )
    swapped = reconcile_local_state(
        local=local,
        snapshot=ReconciliationSnapshot(
            positions_by_symbol=local.positions_by_symbol,
            normal_order_client_ids=local.normal_order_client_ids,
            algo_order_client_ids=frozenset(order.client_algo_id for order in swapped_orders),
            algo_orders=swapped_orders,
        ),
    )

    assert exact.is_clean
    assert not swapped.is_clean
    assert swapped.invalid_stop_contract_ids == tuple(
        sorted((first.client_algo_id, second.client_algo_id))
    )
