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


def _coverage_payload(coverage_path: Path) -> dict[str, object]:
    try:
        payload = json.loads(coverage_path.read_text(encoding="utf-8"))
    except OSError as error:
        _parser_error(f"cannot read coverage JSON: {error}")
    except json.JSONDecodeError as error:
        _parser_error(f"coverage JSON is invalid: {error.msg}")
    if not isinstance(payload, dict):
        _parser_error("coverage JSON root must be an object")
    return payload


def _branch_counts(summary: object, *, label: str) -> tuple[int, int]:
    if not isinstance(summary, dict):
        _parser_error(f"coverage JSON has no {label} summary")
    covered = summary.get("covered_branches")
    total = summary.get("num_branches")
    if (
        not isinstance(covered, int)
        or isinstance(covered, bool)
        or not isinstance(total, int)
        or isinstance(total, bool)
        or covered < 0
        or total <= 0
        or covered > total
    ):
        _parser_error(f"coverage JSON has invalid branch totals for {label}")
    return covered, total


def _module_requirement(value: str) -> tuple[str, Decimal]:
    module, separator, minimum = value.rpartition("=")
    normalized = module.strip().replace("\\", "/").removeprefix("./")
    if not separator or not normalized:
        raise argparse.ArgumentTypeError("module requirement must be PATH=PERCENT")
    return normalized, _non_negative_decimal(minimum)


def _module_branch_counts(payload: dict[str, object], module: str) -> tuple[int, int] | None:
    files = payload.get("files")
    if not isinstance(files, dict):
        return None
    for file_name, detail in files.items():
        normalized = str(file_name).replace("\\", "/").removeprefix("./")
        if normalized != module or not isinstance(detail, dict):
            continue
        return _branch_counts(detail.get("summary"), label=module)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-json", type=Path, required=True)
    parser.add_argument("--minimum", type=_non_negative_decimal, required=True)
    parser.add_argument(
        "--module",
        action="append",
        default=[],
        type=_module_requirement,
        metavar="PATH=PERCENT",
        help="Require an independent true-branch minimum for one module",
    )
    args = parser.parse_args()
    payload = _coverage_payload(args.coverage_json)
    totals = payload.get("totals")
    covered, total = _branch_counts(totals, label="total")
    percent = Decimal(covered) * Decimal("100") / Decimal(total)
    print(
        f"True branch coverage: {percent:.2f}% ({covered}/{total}); required: {args.minimum:.2f}%"
    )
    failed = percent < args.minimum
    if failed:
        print("FAIL: true branch coverage is below the required threshold")
    else:
        print("PASS: true branch coverage meets the required threshold")
    for module, minimum in args.module:
        counts = _module_branch_counts(payload, module)
        if counts is None:
            print(f"FAIL: required coverage module is missing: {module}")
            failed = True
            continue
        module_covered, module_total = counts
        module_percent = Decimal(module_covered) * Decimal("100") / Decimal(module_total)
        print(
            f"Module true branch coverage {module}: {module_percent:.2f}% "
            f"({module_covered}/{module_total}); required: {minimum:.2f}%"
        )
        if module_percent < minimum:
            print(f"FAIL: module true branch coverage is below threshold: {module}")
            failed = True
        else:
            print(f"PASS: module true branch coverage meets threshold: {module}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
