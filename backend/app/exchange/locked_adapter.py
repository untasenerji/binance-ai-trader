"""A hard runtime lock around every authenticated Binance adapter operation."""

from app.exchange.contracts import AlgoOrderIntent, NormalOrderIntent, ReconciliationSnapshot

LIVE_TRADING_ENABLED = False


class LiveTradingLockedError(PermissionError):
    code = "LIVE_TRADING_DISABLED"


class LockedBinanceAdapter:
    """Describes later operations but blocks before a signer or transport can be reached."""

    async def place_normal_order(self, intent: NormalOrderIntent) -> None:
        del intent
        self._reject()

    async def place_algo_order(self, intent: AlgoOrderIntent) -> None:
        del intent
        self._reject()

    async def test_order(self, intent: NormalOrderIntent) -> None:
        del intent
        self._reject()

    async def start_user_data_stream(self) -> None:
        self._reject()

    async def reconcile(self) -> ReconciliationSnapshot:
        self._reject()
        raise AssertionError("unreachable")

    @staticmethod
    def _reject() -> None:
        if not LIVE_TRADING_ENABLED:
            raise LiveTradingLockedError(
                "Phase 8 adapter is locked until the Phase 14 activation flow"
            )
        raise AssertionError("LIVE_TRADING_ENABLED must remain false before Phase 14")
