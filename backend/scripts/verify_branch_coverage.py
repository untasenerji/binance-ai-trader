"""Fail a quality gate from coverage.py's branch totals only."""

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NoReturn


def _parser_error(message: str) -> NoReturn:
    raise ValueError(message)


def _non_negative_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("minimum must be a Decimal percentage") from error
    if not parsed.is_finite() or parsed < Decimal("0") or parsed > Decimal("100"):
        raise argparse.ArgumentTypeError("minimum must be between 0 and 100")
    return parsed


def _branch_totals(coverage_path: Path) -> tuple[int, int]:
    try:
        payload = json.loads(coverage_path.read_text(encoding="utf-8"))
    except OSError as error:
        _parser_error(f"cannot read coverage JSON: {error}")
    except json.JSONDecodeError as error:
        _parser_error(f"coverage JSON is invalid: {error.msg}")
    totals = payload.get("totals") if isinstance(payload, dict) else None
    if not isinstance(totals, dict):
        _parser_error("coverage JSON has no totals object")
    covered = totals.get("covered_branches")
    total = totals.get("num_branches")
    if (
        not isinstance(covered, int)
        or isinstance(covered, bool)
        or not isinstance(total, int)
        or isinstance(total, bool)
        or covered < 0
        or total <= 0
        or covered > total
    ):
        _parser_error("coverage JSON has invalid branch totals")
    return covered, total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-json", type=Path, required=True)
    parser.add_argument("--minimum", type=_non_negative_decimal, required=True)
    args = parser.parse_args()
    covered, total = _branch_totals(args.coverage_json)
    percent = Decimal(covered) * Decimal("100") / Decimal(total)
    print(
        f"True branch coverage: {percent:.2f}% ({covered}/{total}); required: {args.minimum:.2f}%"
    )
    if percent < args.minimum:
        print("FAIL: true branch coverage is below the required threshold")
        return 1
    print("PASS: true branch coverage meets the required threshold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
