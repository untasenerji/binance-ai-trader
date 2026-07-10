from decimal import Decimal

import pytest

from app.domain.filters import FilterViolation, SymbolFilters


@pytest.fixture
def filters() -> SymbolFilters:
    return SymbolFilters(
        symbol="BTCUSDT",
        tick_size=Decimal("0.10"),
        min_price=Decimal("1"),
        max_price=Decimal("1000000"),
        step_size=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("1000"),
        min_notional=Decimal("5"),
    )


def test_filters_round_down_without_increasing_risk(filters: SymbolFilters) -> None:
    assert filters.round_price_down(Decimal("101.299")) == Decimal("101.20")
    assert filters.round_quantity_down(Decimal("0.1239")) == Decimal("0.123")


def test_filters_reject_entry_below_minimum_notional(filters: SymbolFilters) -> None:
    with pytest.raises(FilterViolation, match="notional"):
        filters.validate_entry(price=Decimal("100"), quantity=Decimal("0.001"))


def test_filters_require_exact_exchange_increments(filters: SymbolFilters) -> None:
    with pytest.raises(FilterViolation, match="tick_size"):
        filters.validate_entry(price=Decimal("100.01"), quantity=Decimal("0.1"))
