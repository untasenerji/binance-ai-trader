"""Allowlisted implementation identities for frozen strategy evaluation.

This registry deliberately contains metadata only.  It never accepts a caller
provided evaluator or trainer: the reviewed evaluator factory remains inside
``app.strategy.strategies`` and is reached only after all lineage fields have
been verified.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.strategy.models import (
        StrategyEvaluator,
        StrategyFitResult,
        StrategyLineage,
    )


def _fingerprint(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _source_package_fingerprint() -> str:
    """Bind every record to the reviewed evaluator module shipped with this build."""
    strategies_path = Path(__file__).with_name("strategies.py")
    return hashlib.sha256(strategies_path.read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class StrategyImplementationRecord:
    strategy_id: str
    implementation_version: str
    trainer_version: str
    trainer_implementation_fingerprint: str
    evaluator_implementation_fingerprint: str
    parameter_schema_fingerprint: str
    source_package_fingerprint: str

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.implementation_version or not self.trainer_version:
            raise ValueError("strategy implementation identity is incomplete")
        for fingerprint in (
            self.trainer_implementation_fingerprint,
            self.evaluator_implementation_fingerprint,
            self.parameter_schema_fingerprint,
            self.source_package_fingerprint,
        ):
            if len(fingerprint) != 64 or any(
                character not in "0123456789abcdef" for character in fingerprint.lower()
            ):
                raise ValueError("strategy implementation fingerprints must be SHA-256 hex")


_SOURCE_PACKAGE_FINGERPRINT = _source_package_fingerprint()
_PARAMETER_SCHEMAS = {
    "no_trade_baseline_v1": (),
    "trend_pullback_v1": ("lookback", "validity_ms"),
    "volatility_breakout_v1": ("lookback", "validity_ms"),
    "mean_reversion_v1": ("deviation_percent", "lookback", "validity_ms"),
}
_IMPLEMENTATION_RECORDS = {
    strategy_id: StrategyImplementationRecord(
        strategy_id=strategy_id,
        implementation_version="v1",
        trainer_version="builtin-registry-v1",
        trainer_implementation_fingerprint=_fingerprint(
            {"implementation": "reviewed-trainer", "strategy_id": strategy_id, "version": "v1"}
        ),
        evaluator_implementation_fingerprint=_fingerprint(
            {"implementation": "reviewed-evaluator", "strategy_id": strategy_id, "version": "v1"}
        ),
        parameter_schema_fingerprint=_fingerprint(
            {"parameters": parameters, "strategy_id": strategy_id}
        ),
        source_package_fingerprint=_SOURCE_PACKAGE_FINGERPRINT,
    )
    for strategy_id, parameters in _PARAMETER_SCHEMAS.items()
}


class StrategyImplementationRegistry:
    """The only production evaluator identities accepted by candidate/plan gates."""

    version = "strategy-implementation-registry-v1"
    _records = _IMPLEMENTATION_RECORDS

    @classmethod
    def record_for(
        cls,
        *,
        strategy_id: str,
        implementation_version: str,
    ) -> StrategyImplementationRecord:
        record = cls._records.get(strategy_id)
        if record is None or record.implementation_version != implementation_version:
            raise ValueError("strategy implementation is not allowlisted")
        if record.source_package_fingerprint != _source_package_fingerprint():
            raise ValueError("strategy source package fingerprint changed")
        return record

    @classmethod
    def maybe_record_for(
        cls,
        *,
        strategy_id: str,
        implementation_version: str,
    ) -> StrategyImplementationRecord | None:
        try:
            return cls.record_for(
                strategy_id=strategy_id,
                implementation_version=implementation_version,
            )
        except ValueError:
            return None

    @classmethod
    def verify_lineage(cls, lineage: StrategyLineage) -> None:
        record = cls.record_for(
            strategy_id=lineage.strategy_id,
            implementation_version=lineage.strategy_version,
        )
        expected = (
            ("trainer version", lineage.trainer_version, record.trainer_version),
            (
                "trainer implementation",
                lineage.trainer_implementation_fingerprint,
                record.trainer_implementation_fingerprint,
            ),
            (
                "evaluator implementation",
                lineage.evaluator_implementation_fingerprint,
                record.evaluator_implementation_fingerprint,
            ),
            (
                "parameter schema",
                lineage.parameter_schema_fingerprint,
                record.parameter_schema_fingerprint,
            ),
            (
                "source package",
                lineage.source_package_fingerprint,
                record.source_package_fingerprint,
            ),
            ("registry", lineage.registry_version, cls.version),
        )
        for label, actual, required in expected:
            if actual != required:
                raise ValueError(f"strategy {label} lineage is not allowlisted")

    @classmethod
    def is_verified_lineage(cls, lineage: StrategyLineage) -> bool:
        try:
            cls.verify_lineage(lineage)
        except ValueError:
            return False
        return True

    @classmethod
    def verify_fit_result(cls, fit_result: StrategyFitResult) -> None:
        record = cls.record_for(
            strategy_id=fit_result.strategy_id,
            implementation_version=fit_result.implementation_version,
        )
        if fit_result.strategy_version != record.implementation_version:
            raise ValueError("strategy fit implementation version is invalid")
        expected = (
            ("trainer version", fit_result.trainer_version, record.trainer_version),
            (
                "trainer implementation",
                fit_result.trainer_implementation_fingerprint,
                record.trainer_implementation_fingerprint,
            ),
            (
                "evaluator implementation",
                fit_result.evaluator_implementation_fingerprint,
                record.evaluator_implementation_fingerprint,
            ),
            (
                "parameter schema",
                fit_result.parameter_schema_fingerprint,
                record.parameter_schema_fingerprint,
            ),
            (
                "source package",
                fit_result.source_package_fingerprint,
                record.source_package_fingerprint,
            ),
            ("registry", fit_result.registry_version, cls.version),
        )
        for label, actual, required in expected:
            if actual != required:
                raise ValueError(f"strategy fit {label} is not allowlisted")

    @classmethod
    def evaluator_for_fit(
        cls,
        fit_result: StrategyFitResult,
    ) -> StrategyEvaluator:
        """Return a reviewed evaluator only after immutable fit verification."""
        cls.verify_fit_result(fit_result)
        from app.strategy.strategies import _registry_evaluator_for_fit

        evaluator = _registry_evaluator_for_fit(fit_result)
        if not callable(evaluator):
            raise TypeError("registry evaluator factory returned a non-callable")
        return evaluator
