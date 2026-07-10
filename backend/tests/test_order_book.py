from decimal import Decimal

import pytest

from app.market.order_book import DepthSequenceGap, DepthUpdate, LocalOrderBook


def test_snapshot_and_depth_updates_follow_documented_sequence() -> None:
    book = LocalOrderBook()
    book.load_snapshot(
        last_update_id=100,
        bids=((Decimal("100"), Decimal("1")),),
        asks=((Decimal("101"), Decimal("2")),),
    )

    assert not book.apply(
        DepthUpdate(first_update_id=90, final_update_id=100, previous_final_update_id=89)
    )
    assert book.apply(
        DepthUpdate(
            first_update_id=99,
            final_update_id=102,
            previous_final_update_id=98,
            bids=((Decimal("100"), Decimal("0")),),
            asks=((Decimal("101"), Decimal("3")),),
        )
    )
    assert book.last_update_id == 102
    assert Decimal("100") not in book.bids
    assert book.asks[Decimal("101")] == Decimal("3")


def test_depth_gap_requires_fresh_snapshot() -> None:
    book = LocalOrderBook(last_update_id=100, synchronized=True, bids={}, asks={})

    with pytest.raises(DepthSequenceGap, match="continuity gap"):
        book.apply(
            DepthUpdate(first_update_id=104, final_update_id=105, previous_final_update_id=103)
        )
