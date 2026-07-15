"""Red-first regressions for the fourth audit's medium-severity findings."""

from collections.abc import Callable, Sequence
from decimal import Decimal

import pytest

from app.observability.logging import redact_for_log, redact_text
from app.strategy.backtest import BacktestCosts, WalkForwardRunner, WalkForwardTrainingError
from app.strategy.models import Candle, FrozenStrategy, TrainableStrategy
from app.strategy.strategies import NoTradeBaseline


def _candle(index: int) -> Candle:
    close = Decimal("100") + Decimal(index)
    return Candle(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time_ms=index * 60_000,
        close_time_ms=(index + 1) * 60_000 - 1,
        open_price=close,
        high_price=close + Decimal("1"),
        low_price=close - Decimal("1"),
        close_price=close,
        volume=Decimal("1"),
    )


def _frozen(
    strategy_id: str,
    candles: Sequence[Candle],
    *,
    fingerprint: str,
) -> FrozenStrategy:
    def evaluate(
        evaluation_candles: Sequence[Candle],
        *,
        timeframe: str,
    ) -> None:
        del evaluation_candles, timeframe
        return None

    return FrozenStrategy(
        strategy_id=strategy_id,
        training_candle_count=len(candles),
        training_end_ms=candles[-1].close_time_ms,
        configuration_fingerprint=fingerprint,
        evaluator=evaluate,
    )


def _costs() -> BacktestCosts:
    return BacktestCosts(
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        slippage_bps=Decimal("0"),
    )


_GLOBAL_HELD_OUT: tuple[Candle, ...] = ()
_GLOBAL_HELD_OUT_TIMESTAMP = 0


class _GlobalMarketHolder:
    held_out: tuple[Candle, ...] = ()


_GLOBAL_MARKET_HOLDER = _GlobalMarketHolder()


class GlobalHeldOutTrainer:
    strategy_id = "global-held-out"

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        del timeframe
        threshold = _GLOBAL_HELD_OUT[-1].close_price
        return _frozen(self.strategy_id, candles, fingerprint=f"threshold:{threshold}")


class GlobalHolderTrainer:
    strategy_id = "global-holder"

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        del timeframe
        threshold = _GLOBAL_MARKET_HOLDER.held_out[-1].close_price
        return _frozen(self.strategy_id, candles, fingerprint=f"threshold:{threshold}")


class GlobalScalarTrainer:
    strategy_id = "global-scalar"

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        del timeframe
        return _frozen(
            self.strategy_id,
            candles,
            fingerprint=f"future-time:{_GLOBAL_HELD_OUT_TIMESTAMP}",
        )


class ClassHeldOutTrainer:
    strategy_id = "class-held-out"
    held_out: tuple[Candle, ...] = ()

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        del timeframe
        return _frozen(
            self.strategy_id,
            candles,
            fingerprint=f"threshold:{self.held_out[-1].close_price}",
        )


class ClassScalarHeldOutTrainer:
    strategy_id = "class-scalar-held-out"
    future_close_ms = 0

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        del timeframe
        return _frozen(
            self.strategy_id,
            candles,
            fingerprint=f"future-time:{self.future_close_ms}",
        )


class SlotHeldOutTrainer:
    __slots__ = ("held_out",)
    strategy_id = "slot-held-out"

    def __init__(self, held_out: tuple[Candle, ...]) -> None:
        self.held_out = held_out

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        del timeframe
        return _frozen(
            self.strategy_id,
            candles,
            fingerprint=f"threshold:{self.held_out[-1].close_price}",
        )


class ScalarHeldOutTrainer:
    strategy_id = "scalar-held-out"

    def __init__(self, held_out: tuple[Candle, ...]) -> None:
        self.threshold = held_out[-1].close_price

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        del timeframe
        return _frozen(
            self.strategy_id,
            candles,
            fingerprint=f"threshold:{self.threshold}",
        )


class ConvertedScalarHeldOutTrainer:
    strategy_id = "converted-scalar-held-out"

    def __init__(self, held_out: tuple[Candle, ...]) -> None:
        self.threshold = float(held_out[-1].close_price)

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        del timeframe
        return _frozen(
            self.strategy_id,
            candles,
            fingerprint=f"threshold:{self.threshold}",
        )


class DerivedScalarHeldOutTrainer:
    strategy_id = "derived-scalar-held-out"

    def __init__(self, held_out: tuple[Candle, ...]) -> None:
        self.threshold = held_out[-1].close_price + Decimal("0.125")

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        del timeframe
        return _frozen(
            self.strategy_id,
            candles,
            fingerprint=f"threshold:{self.threshold}",
        )


class OpaqueStateTrainer:
    strategy_id = "opaque-state"

    def __init__(self) -> None:
        self.opaque_state = object()

    def fit(self, candles: Sequence[Candle], *, timeframe: str) -> FrozenStrategy:
        del timeframe
        return _frozen(self.strategy_id, candles, fingerprint="opaque")


def _assert_training_rejected(
    factory: Callable[[], TrainableStrategy],
    candles: tuple[Candle, ...],
) -> None:
    with pytest.raises(WalkForwardTrainingError, match="held-out|isolate"):
        WalkForwardRunner(train_size=4, test_size=3, step_size=3).run(
            factory,
            candles,
            timeframe="1m",
            costs=_costs(),
        )


def test_walk_forward_rejects_global_held_out_market_state() -> None:
    global _GLOBAL_HELD_OUT
    candles = tuple(_candle(index) for index in range(7))
    _GLOBAL_HELD_OUT = candles[4:]
    try:
        _assert_training_rejected(GlobalHeldOutTrainer, candles)
    finally:
        _GLOBAL_HELD_OUT = ()


def test_walk_forward_rejects_global_holder_market_state() -> None:
    candles = tuple(_candle(index) for index in range(7))
    _GLOBAL_MARKET_HOLDER.held_out = candles[4:]
    try:
        _assert_training_rejected(GlobalHolderTrainer, candles)
    finally:
        _GLOBAL_MARKET_HOLDER.held_out = ()


def test_walk_forward_rejects_global_held_out_scalar() -> None:
    global _GLOBAL_HELD_OUT_TIMESTAMP
    candles = tuple(_candle(index) for index in range(7))
    _GLOBAL_HELD_OUT_TIMESTAMP = candles[-1].close_time_ms
    try:
        _assert_training_rejected(GlobalScalarTrainer, candles)
    finally:
        _GLOBAL_HELD_OUT_TIMESTAMP = 0


def test_walk_forward_rejects_class_held_out_market_state() -> None:
    candles = tuple(_candle(index) for index in range(7))
    ClassHeldOutTrainer.held_out = candles[4:]
    try:
        _assert_training_rejected(ClassHeldOutTrainer, candles)
    finally:
        ClassHeldOutTrainer.held_out = ()


def test_walk_forward_rejects_class_held_out_scalar_state() -> None:
    candles = tuple(_candle(index) for index in range(7))
    ClassScalarHeldOutTrainer.future_close_ms = candles[-1].close_time_ms
    try:
        _assert_training_rejected(ClassScalarHeldOutTrainer, candles)
    finally:
        ClassScalarHeldOutTrainer.future_close_ms = 0


def test_walk_forward_rejects_slot_held_out_market_state() -> None:
    candles = tuple(_candle(index) for index in range(7))
    _assert_training_rejected(lambda: SlotHeldOutTrainer(candles[4:]), candles)


def test_walk_forward_rejects_copied_held_out_scalar() -> None:
    candles = tuple(_candle(index) for index in range(7))
    _assert_training_rejected(lambda: ScalarHeldOutTrainer(candles[4:]), candles)


def test_walk_forward_rejects_converted_held_out_scalar() -> None:
    candles = tuple(_candle(index) for index in range(7))
    _assert_training_rejected(lambda: ConvertedScalarHeldOutTrainer(candles[4:]), candles)


def test_walk_forward_rejects_derived_held_out_scalar() -> None:
    candles = tuple(_candle(index) for index in range(7))
    _assert_training_rejected(lambda: DerivedScalarHeldOutTrainer(candles[4:]), candles)


def test_walk_forward_fails_closed_for_opaque_custom_trainer_state() -> None:
    candles = tuple(_candle(index) for index in range(7))
    _assert_training_rejected(OpaqueStateTrainer, candles)


def test_walk_forward_accepts_reviewed_builtin_trainer_without_custom_state() -> None:
    candles = tuple(_candle(index) for index in range(7))

    windows = WalkForwardRunner(train_size=4, test_size=3, step_size=3).run(
        NoTradeBaseline,
        candles,
        timeframe="1m",
        costs=_costs(),
    )

    assert len(windows) == 1
    assert windows[0].result.trades == ()


@pytest.mark.parametrize(
    ("payload", "canary"),
    (
        (r"{\"api_key\":\"escaped-json-canary\"}", "escaped-json-canary"),
        ("{'api_key':'single-quoted-canary'}", "single-quoted-canary"),
        ("%61%70%69%5F%6B%65%79%3Dfully-encoded-canary", "fully-encoded-canary"),
        ("Authorization%3A%20Basic%20encoded-basic-canary", "encoded-basic-canary"),
        ("Authorization%3A%20Bearer%20encoded-bearer-canary", "encoded-bearer-canary"),
    ),
)
def test_redact_text_handles_nested_encoding_and_quoted_forms(
    payload: str,
    canary: str,
) -> None:
    redacted = redact_text(payload)

    assert canary not in redacted
    assert "[REDACTED]" in redacted


def test_nested_structured_values_are_recursively_redacted() -> None:
    canaries = ("nested-basic-canary", "nested-json-canary")
    value = {
        "outer": [
            {
                "detail": "Authorization: Basic nested-basic-canary",
                "payload": r"{\"client_secret\":\"nested-json-canary\"}",
            }
        ]
    }

    redacted = repr(redact_for_log(value))

    assert all(canary not in redacted for canary in canaries)
    assert redacted.count("[REDACTED]") >= 2
