"""Governed integer dimension helpers shared by model-input producers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Mapping


def round_dimension(value: object) -> int:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"dimension is not numeric: {value!r}") from error
    if not number.is_finite() or number <= 0:
        raise ValueError("dimension must be a positive finite number")
    return int(number.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def govern_dimensions(values: Mapping[str, object]) -> dict[str, int]:
    result = {axis: round_dimension(values.get(axis)) for axis in ("width", "depth", "height")}
    return result


__all__ = ["govern_dimensions", "round_dimension"]
