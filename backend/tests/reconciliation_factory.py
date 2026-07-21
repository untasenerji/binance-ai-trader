"""Test-only construction of typed exchange reconciliation observations."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.exchange.contracts import (
    ExchangeReconciliationObservationBatch,
    ReconciliationSnapshot,
)


def exchange_reconciliation_batch(
    snapshot: ReconciliationSnapshot,
    *,
    fetched_at: datetime | None = None,
    max_age: timedelta | None = None,
    query_epoch: int | None = None,
    correlation_id: str | None = None,
) -> ExchangeReconciliationObservationBatch:
    """Attach one coherent authenticated-query provenance to pure snapshot facts."""
    first_order = snapshot.algo_orders[0] if snapshot.algo_orders else None
    observed_at = fetched_at or (
        first_order.fetched_at if first_order is not None else datetime.now(UTC)
    )
    age_bound = max_age or (
        first_order.freshness_window if first_order is not None else timedelta(minutes=10)
    )
    epoch = query_epoch or (first_order.query_epoch if first_order is not None else 1)
    correlation = correlation_id or (
        first_order.correlation_id
        if first_order is not None
        else f"test-reconciliation-{uuid4().hex}"
    )
    server_time = first_order.server_time if first_order is not None else observed_at
    return ExchangeReconciliationObservationBatch(
        source="authenticated_exchange_adapter",
        account_id=snapshot.account_id,
        query_epoch=epoch,
        correlation_id=correlation,
        requested_at=observed_at - timedelta(milliseconds=1),
        fetched_at=observed_at,
        server_time=server_time,
        max_age=age_bound,
        max_clock_skew=timedelta(seconds=5),
        positions_by_symbol=snapshot.positions_by_symbol,
        normal_order_client_ids=snapshot.normal_order_client_ids,
        algo_order_client_ids=snapshot.algo_order_client_ids,
        algo_orders=snapshot.algo_orders,
    )
