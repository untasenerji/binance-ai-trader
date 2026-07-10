from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.decimal_math import (
    DecimalValidationError,
    decimal_to_wire,
    floor_to_increment,
    to_decimal,
)


def test_decimal_parser_rejects_binary_float() -> None:
    with pytest.raises(DecimalValidationError, match="binary float"):
        to_decimal(1.25, field="price")


def test_decimal_wire_rendering_has_no_exponent_or_trailing_zeros() -> None:
    assert decimal_to_wire(Decimal("12.3400")) == "12.34"
    assert decimal_to_wire(Decimal("0.000")) == "0"


@given(
    value=st.decimals(min_value=Decimal("0"), max_value=Decimal("100000"), places=6),
    increment=st.sampled_from((Decimal("0.0001"), Decimal("0.01"), Decimal("1"))),
)
def test_floor_to_increment_is_decimal_aligned(value: Decimal, increment: Decimal) -> None:
    rounded = floor_to_increment(value, increment, field="quantity")

    assert isinstance(rounded, Decimal)
    assert ZERO <= rounded <= value
    assert rounded % increment == Decimal("0")


ZERO = Decimal("0")
