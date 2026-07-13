"""Exact decimal parsing, rounding, and wire rendering helpers."""

from decimal import Decimal, InvalidOperation

ZERO = Decimal("0")


class DecimalValidationError(ValueError):
    """Raised when an external numeric value cannot enter the Decimal domain."""


def to_decimal(value: object, *, field: str) -> Decimal:
    """Parse a finite Decimal without accepting binary floating-point input."""
    if isinstance(value, float):
        raise DecimalValidationError(f"{field} must not use binary float input")

    try:
        if isinstance(value, Decimal):
            parsed = value
        elif isinstance(value, (str, int)):
            parsed = Decimal(value)
        else:
            raise DecimalValidationError(f"{field} must be a decimal-compatible value")
    except (InvalidOperation, TypeError, ValueError) as error:
        raise DecimalValidationError(f"{field} must be a decimal-compatible value") from error

    if not parsed.is_finite():
        raise DecimalValidationError(f"{field} must be finite")

    return parsed


def floor_to_increment(value: Decimal, increment: Decimal, *, field: str) -> Decimal:
    """Round a non-negative trading value down to an exchange increment."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise DecimalValidationError(f"{field} must be a finite Decimal")
    if not isinstance(increment, Decimal) or not increment.is_finite():
        raise DecimalValidationError("increment must be a finite Decimal")
    if value < ZERO:
        raise DecimalValidationError(f"{field} must be non-negative")
    if increment <= ZERO:
        raise DecimalValidationError("increment must be positive")

    return (value // increment) * increment


def ceil_to_increment(value: Decimal, increment: Decimal, *, field: str) -> Decimal:
    """Round a non-negative trading value up without changing an aligned value."""
    rounded_down = floor_to_increment(value, increment, field=field)
    return rounded_down if rounded_down == value else rounded_down + increment


def decimal_to_wire(value: Decimal) -> str:
    """Return a non-exponent Decimal string suitable for API payloads."""
    rendered = format(value, "f")
    if "." not in rendered:
        return rendered

    stripped = rendered.rstrip("0").rstrip(".")
    return "0" if stripped in {"", "-0"} else stripped
