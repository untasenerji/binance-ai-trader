from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.unitofwork import UOWTransaction

from app.domain.types import Direction
from app.exchange.contracts import (
    LocalReconciliationState,
    ReconciliationSnapshot,
    reconcile_local_state,
)
from app.persistence.circuit_breaker import PersistenceCircuitBreaker, PersistenceRecoveryEvidence
from app.persistence.database import create_database_engine, create_schema, create_session_factory
from app.persistence.models import DurableOrderIntent
from app.simulation.intent_ledger import (
    BoundedAbsenceEvidence,
    BoundedAbsenceEvidenceError,
    DurableIntentLedger,
    DurableIntentStatus,
    UnresolvedEconomicAction,
)
from app.simulation.models import (
    OrderRole,
    SimulatedFault,
    SimulatedOrderIntent,
    SimulatedOrderStatus,
)
from app.simulation.simulator import (
    DurableIntentLedgerRequired,
    ExchangeSimulator,
    FaultPlan,
    UnknownOrderOutcome,
)


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'durable-intents.sqlite'}")
    create_schema(engine)
    return create_session_factory(engine)


def _open_breaker() -> PersistenceCircuitBreaker:
    breaker = PersistenceCircuitBreaker()
    clean_reconciliation = reconcile_local_state(
        local=LocalReconciliationState(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
            required_stop_symbols=frozenset(),
            unresolved_unknown_intent_ids=frozenset(),
            audit_chain_valid=True,
            replay_valid=True,
        ),
        snapshot=ReconciliationSnapshot(
            positions_by_symbol={},
            normal_order_client_ids=frozenset(),
            algo_order_client_ids=frozenset(),
        ),
    )
    breaker.reset_after_verified_reconciliation(
        PersistenceRecoveryEvidence(
            durable_write_probe_succeeded=True,
            audit_chain_valid=True,
            replay_valid=True,
            reconciliation_outcome=clean_reconciliation,
            unresolved_prepared_count=0,
            unresolved_submitting_count=0,
            unresolved_unknown_count=0,
        )
    )
    return breaker


@pytest.fixture
def ledger(session_factory: sessionmaker[Session]) -> DurableIntentLedger:
    return DurableIntentLedger(session_factory, persistence_breaker=_open_breaker())


def _intent(
    *,
    client_order_id: str = "UTA1-plan-1-EN-1",
    plan_id: str = "plan-1",
    stage_index: int = 1,
) -> SimulatedOrderIntent:
    return SimulatedOrderIntent(
        client_order_id=client_order_id,
        plan_id=plan_id,
        symbol="BTCUSDT",
        direction=Direction.LONG,
        role=OrderRole.ENTRY,
        stage_index=stage_index,
        quantity=Decimal("0.010"),
        price=Decimal("1000"),
    )


def _unknown_simulator(ledger: DurableIntentLedger) -> ExchangeSimulator:
    return ExchangeSimulator(
        intent_ledger=ledger,
        fault_plan=FaultPlan.from_faults((SimulatedFault.UNKNOWN_503,)),
    )


def _absence_evidence(intent: SimulatedOrderIntent) -> BoundedAbsenceEvidence:
    return BoundedAbsenceEvidence(
        client_order_id=intent.client_order_id,
        economic_key=intent.economic_key,
        first_not_found_at_ms=0,
        last_not_found_at_ms=1_000,
        not_found_observation_count=2,
    )


def test_simulator_refuses_submission_without_a_durable_intent_ledger() -> None:
    with pytest.raises(DurableIntentLedgerRequired):
        ExchangeSimulator().submit(_intent())


@pytest.mark.parametrize(
    ("status", "filled_quantity"),
    (
        (SimulatedOrderStatus.NEW, Decimal("0")),
        (SimulatedOrderStatus.PARTIALLY_FILLED, Decimal("0.005")),
        (SimulatedOrderStatus.FILLED, Decimal("0.010")),
        (SimulatedOrderStatus.CANCELLED, Decimal("0")),
    ),
)
def test_unknown_503_can_only_be_resolved_to_a_durable_known_outcome(
    ledger: DurableIntentLedger,
    status: SimulatedOrderStatus,
    filled_quantity: Decimal,
) -> None:
    intent = _intent()
    simulator = _unknown_simulator(ledger)

    with pytest.raises(UnknownOrderOutcome):
        simulator.submit(intent)
    assert ledger.unresolved_client_order_ids() == (intent.client_order_id,)

    simulator.reconcile_unknown(
        intent.client_order_id,
        status=status,
        filled_quantity=filled_quantity,
    )

    assert ledger.intent(intent.client_order_id).status is DurableIntentStatus(status.value)
    assert ledger.unresolved_client_order_ids() == ()


def test_unknown_absence_requires_bounded_evidence_before_a_retry_is_possible(
    ledger: DurableIntentLedger,
) -> None:
    intent = _intent()
    simulator = _unknown_simulator(ledger)
    with pytest.raises(UnknownOrderOutcome):
        simulator.submit(intent)

    early_not_found = BoundedAbsenceEvidence(
        client_order_id=intent.client_order_id,
        economic_key=intent.economic_key,
        first_not_found_at_ms=0,
        last_not_found_at_ms=0,
        not_found_observation_count=1,
    )
    with pytest.raises(BoundedAbsenceEvidenceError):
        simulator.resolve_unknown_as_absent(intent.client_order_id, early_not_found)
    with pytest.raises(UnresolvedEconomicAction):
        simulator.submit(_intent(client_order_id="UTA1-plan-1-EN-2"))

    simulator.resolve_unknown_as_absent(intent.client_order_id, _absence_evidence(intent))
    assert ledger.intent(intent.client_order_id).status is DurableIntentStatus.ABSENT

    replacement = simulator.submit(_intent(client_order_id="UTA1-plan-1-EN-2"))
    assert replacement.status is SimulatedOrderStatus.NEW


def test_restart_keeps_prepared_submitting_and_unknown_intents_unresolved(
    session_factory: sessionmaker[Session], ledger: DurableIntentLedger
) -> None:
    prepared = _intent(client_order_id="prepared", stage_index=1)
    submitting = _intent(client_order_id="submitting", stage_index=2)
    unknown = _intent(client_order_id="unknown", stage_index=3)
    ledger.prepare(prepared)
    ledger.prepare(submitting)
    ledger.mark_submitting(submitting.client_order_id)
    ledger.prepare(unknown)
    ledger.mark_submitting(unknown.client_order_id)
    ledger.mark_unknown(unknown.client_order_id)

    restarted = DurableIntentLedger(session_factory, persistence_breaker=_open_breaker())

    assert restarted.unresolved_client_order_ids() == ("prepared", "submitting", "unknown")
    assert restarted.unresolved_counts() == {
        DurableIntentStatus.PREPARED: 1,
        DurableIntentStatus.SUBMITTING: 1,
        DurableIntentStatus.UNKNOWN: 1,
    }


def test_derived_economic_identity_blocks_modified_client_ids_and_caller_supplied_keys(
    ledger: DurableIntentLedger,
) -> None:
    intent = _intent()
    simulator = _unknown_simulator(ledger)
    with pytest.raises(UnknownOrderOutcome):
        simulator.submit(intent)

    with pytest.raises(UnresolvedEconomicAction):
        simulator.submit(_intent(client_order_id="modified-client"))
    with pytest.raises(TypeError):
        SimulatedOrderIntent(
            client_order_id="manual-key",
            economic_key="attacker-controlled",  # type: ignore[call-arg]
            plan_id="plan-1",
            symbol="BTCUSDT",
            direction=Direction.LONG,
            role=OrderRole.ENTRY,
            stage_index=1,
            quantity=Decimal("0.010"),
            price=Decimal("1000"),
        )


def test_concurrent_same_economic_retry_creates_only_one_prepared_intent(
    ledger: DurableIntentLedger,
) -> None:
    barrier = Barrier(4)

    def prepare(client_index: int) -> bool:
        barrier.wait()
        try:
            ledger.prepare(_intent(client_order_id=f"concurrent-{client_index}"))
        except UnresolvedEconomicAction:
            return False
        return True

    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = tuple(executor.map(prepare, range(4)))

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 3
    assert len(ledger.list_intents()) == 1


def test_database_failure_after_simulated_submit_leaves_a_durable_unresolved_attempt(
    ledger: DurableIntentLedger,
) -> None:
    intent = _intent()
    simulator = ExchangeSimulator(intent_ledger=ledger)

    def fail_flush(
        session: Session,
        _: UOWTransaction,
        __: Sequence[Any] | None,
    ) -> None:
        if any(
            isinstance(record, DurableOrderIntent)
            and record.status is DurableIntentStatus.NEW.value
            for record in session.dirty
        ):
            raise OSError("injected durable outcome failure")

    event.listen(Session, "before_flush", fail_flush)
    try:
        with pytest.raises(OSError, match="durable outcome failure"):
            simulator.submit(intent)
    finally:
        event.remove(Session, "before_flush", fail_flush)

    assert simulator.order(intent.client_order_id).status is SimulatedOrderStatus.NEW
    assert ledger.intent(intent.client_order_id).status is DurableIntentStatus.SUBMITTING
    assert not ledger.persistence_breaker.new_entries_allowed
