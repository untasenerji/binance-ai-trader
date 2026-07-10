import pytest

from app.market.freshness import DataFreshness, StaleMarketData


def test_data_freshness_requires_recent_observation() -> None:
    freshness = DataFreshness(max_age_ms=1_000)
    freshness.record("book_ticker:BTCUSDT", observed_at_ms=10_000)

    assert freshness.is_fresh("book_ticker:BTCUSDT", now_ms=11_000)
    with pytest.raises(StaleMarketData):
        freshness.require_fresh("book_ticker:BTCUSDT", now_ms=11_001)


def test_missing_data_is_stale() -> None:
    with pytest.raises(StaleMarketData, match="stale or absent"):
        DataFreshness(max_age_ms=1).require_fresh("mark_price:BTCUSDT", now_ms=1)
