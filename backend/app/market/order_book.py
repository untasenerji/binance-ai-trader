"""Local depth-snapshot sequencing for shadow market-data use only."""

from dataclasses import dataclass, field
from decimal import Decimal

from app.domain.decimal_math import ZERO


class DepthSequenceGap(RuntimeError):
    """Raised when the documented U/u/pu sequence requires a fresh snapshot."""


@dataclass(frozen=True, slots=True)
class DepthUpdate:
    first_update_id: int
    final_update_id: int
    previous_final_update_id: int | None
    bids: tuple[tuple[Decimal, Decimal], ...] = ()
    asks: tuple[tuple[Decimal, Decimal], ...] = ()

    def __post_init__(self) -> None:
        if self.first_update_id < 0 or self.final_update_id < self.first_update_id:
            raise ValueError("depth update IDs are invalid")
        if self.previous_final_update_id is not None and self.previous_final_update_id < 0:
            raise ValueError("previous_final_update_id is invalid")


@dataclass(slots=True)
class LocalOrderBook:
    """Maintains a bounded shadow book after an exchange REST snapshot."""

    last_update_id: int | None = None
    synchronized: bool = False
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)

    def load_snapshot(
        self,
        *,
        last_update_id: int,
        bids: tuple[tuple[Decimal, Decimal], ...] = (),
        asks: tuple[tuple[Decimal, Decimal], ...] = (),
    ) -> None:
        if last_update_id < 0:
            raise ValueError("last_update_id must not be negative")
        self.last_update_id = last_update_id
        self.synchronized = False
        self.bids = _levels_to_book(bids)
        self.asks = _levels_to_book(asks)

    def apply(self, update: DepthUpdate) -> bool:
        """Apply an update, returning False for a pre-snapshot event that is obsolete."""
        if self.last_update_id is None:
            raise DepthSequenceGap("a REST snapshot is required before depth updates")

        if not self.synchronized:
            if update.final_update_id <= self.last_update_id:
                return False
            expected_first_id = self.last_update_id + 1
            if not update.first_update_id <= expected_first_id <= update.final_update_id:
                raise DepthSequenceGap("first post-snapshot event does not bridge the snapshot")
            self.synchronized = True
        elif update.previous_final_update_id != self.last_update_id:
            raise DepthSequenceGap("depth stream continuity gap requires a new snapshot")

        self._apply_levels(self.bids, update.bids)
        self._apply_levels(self.asks, update.asks)
        self.last_update_id = update.final_update_id
        return True

    @staticmethod
    def _apply_levels(
        book: dict[Decimal, Decimal], levels: tuple[tuple[Decimal, Decimal], ...]
    ) -> None:
        for price, quantity in levels:
            if price <= ZERO or quantity < ZERO:
                raise ValueError(
                    "depth price and quantity must be non-negative with positive price"
                )
            if quantity == ZERO:
                book.pop(price, None)
            else:
                book[price] = quantity


def _levels_to_book(levels: tuple[tuple[Decimal, Decimal], ...]) -> dict[Decimal, Decimal]:
    book: dict[Decimal, Decimal] = {}
    LocalOrderBook._apply_levels(book, levels)
    return book
