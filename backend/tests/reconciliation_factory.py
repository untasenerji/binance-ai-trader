"""Test-only construction of typed exchange reconciliation observations."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import count
from uuid import uuid4

from app.exchange.contracts import (
    AdapterQueryReceipt,
    AdapterQueryReceiptStatus,
    ExchangeReconciliationObservationBatch,
    ReconciliationSnapshot,
)
from app.simulation.intent_ledger import (
    _ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY,
    DurableIntentLedger,
)

_QUERY_EPOCHS = count(1)


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
    epoch = (
        query_epoch
        if query_epoch is not None
        else (first_order.query_epoch if first_order is not None else next(_QUERY_EPOCHS))
    )
    correlation = correlation_id or (
        first_order.correlation_id
        if first_order is not None
        else f"test-reconciliation-{uuid4().hex}"
    )
    server_time = first_order.server_time if first_order is not None else observed_at
    provisional = ExchangeReconciliationObservationBatch(
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
    receipt = AdapterQueryReceipt(
        receipt_id=f"test-receipt-{uuid4().hex}",
        account_id=provisional.account_id,
        adapter_instance_id="test-authenticated-adapter",
        query_id=f"test-query-{uuid4().hex}",
        correlation_id=provisional.correlation_id,
        query_epoch=provisional.query_epoch,
        requested_at=provisional.requested_at,
        completed_at=provisional.fetched_at,
        server_time=provisional.server_time,
        query_type="reconciliation",
        response_fingerprint=provisional.response_fingerprint,
        status=AdapterQueryReceiptStatus.COMPLETED,
    )
    return replace(provisional, query_receipt=receipt)


def persist_reconciliation_query_receipt(
    ledger: DurableIntentLedger,
    batch: ExchangeReconciliationObservationBatch,
) -> ExchangeReconciliationObservationBatch:
    """Persist the pre-query and completion receipts before recovery consumes a batch."""
    receipt = batch.query_receipt
    if receipt is None:
        raise ValueError("test reconciliation batch requires an adapter query receipt")
    ledger._record_adapter_query_receipt_from_authenticated_adapter(  # noqa: SLF001
        receipt.started_record(),
        capability=_ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY,
    )
    ledger._record_adapter_query_receipt_from_authenticated_adapter(  # noqa: SLF001
        receipt,
        capability=_ADAPTER_QUERY_RECEIPT_WRITE_CAPABILITY,
    )
    return batch
