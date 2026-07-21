"""Red-first regressions for the eighth audit's medium findings."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, text

from alembic import command
from app.domain.risk import RiskSettings
from app.persistence.database import create_session_factory
from app.planning.risk import (
    LadderPlan,
    RiskEnvelope,
    RiskEnvelopeState,
    RiskPlanningError,
)
from app.simulation.intent_ledger import (
    DurableIntentLedger,
    VerifiedQuarantineResolutionEvidence,
)
from app.strategy.backtest import BacktestCosts, WalkForwardRunner
from app.strategy.gate import CandidateGate, CandidateGateContext
from app.strategy.models import (
    Candle,
    FrozenStrategy,
    SignalCandidate,
    StrategyFitResult,
    StrategySpecification,
)
from app.strategy.strategies import frozen_strategy_from_fit
from tests.strategy_factory import make_strategy_lineage

ACCOUNT_ID = "v1-primary"


def _migration_config(database_url: str) -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_duplicate_legacy_quarantines_keep_every_source_and_resolve_as_one_case(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'many-to-one-quarantine.sqlite'}"
    config = _migration_config(database_url)
    command.upgrade(config, "0011_forward_invariants")
    engine = create_engine(database_url)
    metadata = MetaData()
    source = Table("migration_quarantine_records", metadata, autoload_with=engine)
    economic_key = "legacy-many-to-one:BTCUSDT:LONG:ENTRY:1"
    provenance = "a" * 64
    with engine.begin() as connection:
        for index, query_reference in enumerate(("legacy-query-one", "legacy-query-two"), 1):
            connection.execute(
                source.insert().values(
                    migration_revision="test-eighth",
                    source_table="durable_order_intents",
                    source_identity=f"legacy-row-{index}",
                    reason="INVALID_LEGACY_TIMELINE",
                    evidence={
                        "account_id": ACCOUNT_ID,
                        "client_order_id": "legacy-many-to-one-entry",
                        "economic_key": economic_key,
                        "provenance_fingerprint": provenance,
                        "query_reference": query_reference,
                    },
                    reconciliation_required=True,
                )
            )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    session_factory = create_session_factory(engine)
    ledger = DurableIntentLedger(session_factory)
    try:
        with engine.connect() as connection:
            canonical_count = connection.scalar(
                text("SELECT COUNT(*) FROM durable_evidence_quarantines")
            )
            source_rows = tuple(
                connection.execute(
                    text(
                        "SELECT source_row_id, query_reference "
                        "FROM durable_evidence_quarantine_sources "
                        "ORDER BY source_row_id"
                    )
                )
            )
            quarantine_id = connection.scalar(
                text("SELECT quarantine_id FROM durable_evidence_quarantines")
            )

        assert canonical_count == 1
        assert len(source_rows) == 2
        assert {row.query_reference for row in source_rows} == {
            "legacy-query-one",
            "legacy-query-two",
        }
        with session_factory() as session:
            unresolved_before = ledger._unresolved_quarantine_count(  # noqa: SLF001
                session,
                account_id=ACCOUNT_ID,
            )
        assert unresolved_before == 1
        assert isinstance(quarantine_id, str)
        evidence = VerifiedQuarantineResolutionEvidence(
            account_id=ACCOUNT_ID,
            economic_key=economic_key,
            client_order_id="legacy-many-to-one-entry",
            query_reference="fresh-verified-query",
            observed_at=datetime.now(UTC),
        )
        resolution_id = ledger.resolve_evidence_quarantine(
            quarantine_id,
            operator_id="operator-eight",
            verified_evidence=evidence,
        )
        duplicate_resolution_id = ledger.resolve_evidence_quarantine(
            quarantine_id,
            operator_id="operator-eight",
            verified_evidence=evidence,
        )
        with session_factory() as session:
            unresolved = ledger._unresolved_quarantine_count(  # noqa: SLF001
                session,
                account_id=ACCOUNT_ID,
            )
        with ledger.reopen_after_restart()._session_factory() as session:  # noqa: SLF001
            restart_unresolved = ledger._unresolved_quarantine_count(  # noqa: SLF001
                session,
                account_id=ACCOUNT_ID,
            )

        assert resolution_id == duplicate_resolution_id
        assert unresolved == restart_unresolved == 0
    finally:
        engine.dispose()


def _candle(index: int, close: str) -> Candle:
    price = Decimal(close)
    return Candle(
        symbol="BTCUSDT",
        open_time_ms=index * 60_000,
        close_time_ms=(index + 1) * 60_000 - 1,
        open_price=price,
        high_price=price + Decimal("1"),
        low_price=price - Decimal("1"),
        close_price=price,
        volume=Decimal("10"),
        timeframe="1m",
    )


def test_frozen_candidate_carries_the_verified_fit_lineage() -> None:
    specification = StrategySpecification.volatility_breakout(lookback=2)
    fit_result = StrategyFitResult(
        specification=specification,
        training_data_fingerprint="b" * 64,
        training_candle_count=2,
        training_end_ms=119_999,
        timeframe="1m",
    )
    frozen = frozen_strategy_from_fit(fit_result)
    candidate = frozen.evaluate(
        (_candle(0, "100"), _candle(1, "103")),
        timeframe="1m",
    )

    assert candidate is not None
    assert candidate.strategy_id == frozen.strategy_id
    assert candidate.strategy_version == frozen.strategy_version
    assert (
        candidate.strategy_specification_fingerprint
        == specification.fingerprint
        == frozen.strategy_specification_fingerprint
    )
    assert candidate.fit_result_fingerprint == fit_result.fingerprint
    assert candidate.train_dataset_fingerprint == fit_result.training_data_fingerprint
    assert candidate.trainer_version == fit_result.trainer_version


def _verified_candidate() -> tuple[StrategySpecification, StrategyFitResult, SignalCandidate]:
    specification = StrategySpecification.volatility_breakout(lookback=2)
    fit_result = StrategyFitResult(
        specification=specification,
        training_data_fingerprint="c" * 64,
        training_candle_count=2,
        training_end_ms=119_999,
        timeframe="1m",
    )
    candidate = frozen_strategy_from_fit(fit_result).evaluate(
        (_candle(0, "100"), _candle(1, "103")),
        timeframe="1m",
    )
    assert candidate is not None
    return specification, fit_result, candidate


@pytest.mark.parametrize(
    "mutation",
    (
        {"strategy_id": "mean_reversion_v1"},
        {"strategy_version": "v99"},
        {"fit_result_fingerprint": "d" * 64},
        {"strategy_specification_fingerprint": "e" * 64},
        {"train_dataset_fingerprint": "f" * 64},
        {"trainer_version": "forged-trainer-v9"},
    ),
)
def test_frozen_strategy_rejects_candidate_lineage_substitution(
    mutation: dict[str, str],
) -> None:
    _, fit_result, candidate = _verified_candidate()
    forged = replace(candidate, **mutation)  # type: ignore[arg-type]
    frozen = FrozenStrategy(
        fit_result=fit_result,
        evaluator=lambda candles, *, timeframe: forged,
    )

    with pytest.raises(ValueError, match="lineage"):
        frozen.evaluate((_candle(0, "100"), _candle(1, "103")), timeframe="1m")


def test_frozen_strategy_rechecks_specification_and_fit_after_construction() -> None:
    specification, fit_result, _ = _verified_candidate()
    frozen = frozen_strategy_from_fit(fit_result)
    object.__setattr__(specification, "lookback", 99)

    with pytest.raises(ValueError, match="fingerprint"):
        frozen.evaluate((_candle(0, "100"), _candle(1, "103")), timeframe="1m")


def test_candidate_gate_and_ladder_plan_reject_lineage_swaps() -> None:
    _, _, candidate = _verified_candidate()
    wrong_lineage = make_strategy_lineage("swapped-strategy")
    context = CandidateGateContext(
        expected_lineage=wrong_lineage,
        risk_allows=True,
        data_is_fresh=True,
        estimated_cost=Decimal("0.01"),
        max_estimated_cost=Decimal("0.02"),
        expected_net_value=Decimal("0.03"),
        evaluated_at_ms=1_000,
    )
    decision = CandidateGate().evaluate(candidate, context=context)

    assert not decision.accepted
    assert "STRATEGY_LINEAGE_MISMATCH" in decision.reason_codes

    settings = RiskSettings(
        pilot_equity_cap_usdt=Decimal("20"),
        max_leverage=2,
        max_concurrent_positions=1,
        max_active_strategy_count=1,
        max_stages=1,
        risk_per_trade_usdt=Decimal("0.10"),
        daily_loss_limit_usdt=Decimal("0.30"),
        weekly_drawdown_limit_usdt=Decimal("0.80"),
        consecutive_loss_limit=3,
        openai_daily_budget_usd=Decimal("0.02"),
    )
    plan = LadderPlan(
        strategy_lineage=candidate.lineage,
        direction=candidate.direction,
        stop_price=candidate.invalidation_price,
        risk_budget=Decimal("0.10"),
        projected_total_loss=Decimal("0"),
        planned_notional_usdt=Decimal("0"),
        required_margin_usdt=Decimal("0"),
        risk_envelope=RiskEnvelope(
            state=RiskEnvelopeState.READY,
            reason=None,
            settings=settings,
            effective_equity_usdt=Decimal("20"),
            effective_leverage=2,
            risk_budget_usdt=Decimal("0.10"),
            remaining_symbol_exposure_usdt=Decimal("20"),
            remaining_total_exposure_usdt=Decimal("20"),
            required_reserve_usdt=Decimal("0"),
        ),
        stages=(),
    )
    plan.verify_candidate_lineage(candidate)
    forged_candidate = SignalCandidate.from_lineage(
        wrong_lineage,
        symbol=candidate.symbol,
        direction=candidate.direction,
        reference_price=candidate.reference_price,
        invalidation_price=candidate.invalidation_price,
        timeframe=candidate.timeframe,
        valid_until_ms=candidate.valid_until_ms,
        reason_codes=candidate.reason_codes,
    )
    with pytest.raises(RiskPlanningError, match="lineage"):
        plan.verify_candidate_lineage(forged_candidate)


def test_fit_lineage_is_invariant_when_only_held_out_candles_change() -> None:
    runner = WalkForwardRunner(train_size=4, test_size=2, step_size=2)
    specification = StrategySpecification.volatility_breakout(lookback=2)
    training = tuple(_candle(index, str(100 + index)) for index in range(4))
    first_held_out = (_candle(4, "110"), _candle(5, "111"))
    second_held_out = (_candle(4, "20"), _candle(5, "19"))
    costs = BacktestCosts(
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        slippage_bps=Decimal("0"),
    )

    first = runner.run(specification, (*training, *first_held_out), timeframe="1m", costs=costs)
    second = runner.run(
        specification,
        (*training, *second_held_out),
        timeframe="1m",
        costs=costs,
    )

    assert len(first) == len(second) == 1
    assert first[0].training_data_fingerprint == second[0].training_data_fingerprint
    assert first[0].fit_result_fingerprint == second[0].fit_result_fingerprint
    assert (
        first[0].strategy_specification_fingerprint == second[0].strategy_specification_fingerprint
    )
    assert first[0].strategy_id == second[0].strategy_id
    assert first[0].strategy_version == second[0].strategy_version
    assert first[0].trainer_version == second[0].trainer_version
