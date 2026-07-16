"""Red-first hardening regressions carried into the seventh audit round."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.observability.logging import redact_for_log, redact_text
from app.strategy.backtest import BacktestCosts, WalkForwardRunner, WalkForwardTrainingError
from app.strategy.models import Candle, StrategySpecification


def _candle(index: int, close: Decimal) -> Candle:
    return Candle(
        symbol="BTCUSDT",
        open_time_ms=index * 60_000,
        close_time_ms=(index + 1) * 60_000 - 1,
        open_price=close,
        high_price=close + Decimal("1"),
        low_price=close - Decimal("1"),
        close_price=close,
        volume=Decimal("10"),
        timeframe="1m",
    )


def test_walk_forward_revalidates_tampered_specification_fingerprint() -> None:
    specification = StrategySpecification.volatility_breakout(lookback=3)
    object.__setattr__(specification, "lookback", 99)
    runner = WalkForwardRunner(train_size=4, test_size=2, step_size=2)
    candles = tuple(_candle(index, Decimal("100") + Decimal(index)) for index in range(6))

    with pytest.raises(WalkForwardTrainingError, match="fingerprint"):
        runner.run(
            specification,
            candles,
            timeframe="1m",
            costs=BacktestCosts(
                entry_fee_rate=Decimal("0"),
                exit_fee_rate=Decimal("0"),
                slippage_bps=Decimal("0"),
            ),
        )


def test_walk_forward_fit_is_invariant_when_only_held_out_candles_change() -> None:
    runner = WalkForwardRunner(train_size=4, test_size=2, step_size=2)
    specification = StrategySpecification.volatility_breakout(lookback=3)
    training = tuple(_candle(index, Decimal("100") + Decimal(index)) for index in range(4))
    first_held_out = (_candle(4, Decimal("104")), _candle(5, Decimal("105")))
    second_held_out = (_candle(4, Decimal("10")), _candle(5, Decimal("5")))
    costs = BacktestCosts(
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        slippage_bps=Decimal("0"),
    )

    first = runner.run(specification, (*training, *first_held_out), timeframe="1m", costs=costs)
    second = runner.run(specification, (*training, *second_held_out), timeframe="1m", costs=costs)

    assert len(first) == len(second) == 1
    assert first[0].training_data_fingerprint == second[0].training_data_fingerprint
    assert first[0].configuration_fingerprint == second[0].configuration_fingerprint


def test_compound_encoded_secret_mapping_keys_and_values_are_both_redacted() -> None:
    raw = {
        "nested": {
            "client%255Fsecret": "leak-one",
            "\\u0061ccessToken": "leak-two",
            "safe": '{"secretKey":"leak-three"}',
        }
    }

    redacted = redact_for_log(raw)
    serialized = repr(redacted)

    assert "leak-one" not in serialized
    assert "leak-two" not in serialized
    assert "leak-three" not in serialized
    assert "client%255Fsecret" not in serialized
    assert "accessToken" not in serialized
    assert redact_text('\\"apiToken\\":\\"leak-four\\"') == '"apiToken":"[REDACTED]"'
