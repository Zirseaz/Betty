"""Inventory-aware market-making strategy for Polymarket.

Places GTC (Good-Till-Cancelled) limit orders on both sides of the book
and earns the bid-ask spread.  GTC orders are maker orders on Polymarket
and pay 0% fees — the maker also receives a 20% share of the daily fee
pool as a rebate.

Inventory skew adjusts quotes to reduce directional risk:
- Long inventory → lower bid (less eager to buy) and lower ask (more eager to sell)
- Short inventory → higher bid (more eager to buy) and higher ask (less eager to sell)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAKER_FEE: Final[float] = 0.0       # GTC orders have 0% maker fee
TAKER_FEE: Final[float] = 0.018     # for reference
MAKER_REBATE_SHARE: Final[float] = 0.20  # 20% of daily fee pool
MIN_SPREAD: Final[float] = 0.005    # 0.5 cent minimum half-spread
MAX_SPREAD: Final[float] = 0.10     # 10 cent maximum half-spread
MIN_PRICE: Final[float] = 0.01      # Polymarket minimum tick
MAX_PRICE: Final[float] = 0.99      # Polymarket maximum tick


@dataclass(frozen=True, slots=True)
class Quote:
    """A single limit-order quote.

    Attributes:
        side: ``"BUY"`` or ``"SELL"``.
        price: Limit price (0.01–0.99).
        size: Number of shares.
        order_type: Always ``"GTC"`` for market-making.
        maker_fee: Fee paid (0 for GTC).
        estimated_rebate: Estimated rebate from fee pool share.
    """

    side: str
    price: float
    size: float
    order_type: str = "GTC"
    maker_fee: float = 0.0
    estimated_rebate: float = 0.0

    def __str__(self) -> str:
        return (
            f"[{self.side}] {self.size:.2f} @ {self.price:.4f} "
            f"({self.order_type}) rebate≈${self.estimated_rebate:.4f}"
        )


@dataclass(frozen=True, slots=True)
class MarketMakingState:
    """Current state of the market-making strategy.

    Attributes:
        mid_price: Current mid-market price.
        bid_quote: The bid (buy) quote.
        ask_quote: The ask (sell) quote.
        spread: Full spread (ask - bid).
        inventory: Current net inventory (positive = long).
        skew_applied: The skew offset applied to quotes.
        estimated_daily_rebate: Estimated daily rebate in USD.
        created_at: Timestamp.
    """

    mid_price: float
    bid_quote: Quote
    ask_quote: Quote
    spread: float
    inventory: float
    skew_applied: float
    estimated_daily_rebate: float
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _clamp_price(price: float) -> float:
    """Clamp price to valid Polymarket tick range."""
    return round(max(MIN_PRICE, min(MAX_PRICE, price)), 4)


def calculate_inventory_skew(
    inventory: float,
    max_inventory: float,
    max_skew: float = 0.02,
) -> float:
    """Calculate price skew based on inventory imbalance.

    Skew moves both quotes in the same direction to encourage
    inventory-reducing fills.

    Args:
        inventory: Current net position (positive = long, negative = short).
        max_inventory: Inventory level at which max skew is applied.
        max_skew: Maximum price offset.

    Returns:
        Skew value to *subtract* from both bid and ask prices.
        Positive skew means we're long and want to sell → prices shift down.
    """
    if max_inventory <= 0:
        return 0.0
    ratio = max(-1.0, min(1.0, inventory / max_inventory))
    return round(ratio * max_skew, 6)


def calculate_mm_quotes(
    mid_price: float,
    spread: float,
    size: float,
    *,
    inventory_skew: float = 0.0,
    daily_volume_usd: float = 10_000.0,
) -> tuple[Quote, Quote]:
    """Calculate bid and ask quotes for market making.

    Args:
        mid_price: Current mid-market price (0–1 range).
        spread: Desired full spread (bid-to-ask distance).
        size: Number of shares per side.
        inventory_skew: Skew offset (from :func:`calculate_inventory_skew`).
            Positive = we're long → shift quotes down to sell more.
        daily_volume_usd: Estimated daily market volume for rebate calc.

    Returns:
        A tuple of ``(bid_quote, ask_quote)``.

    Raises:
        ValueError: If inputs are out of range.
    """
    if not (0.0 < mid_price < 1.0):
        raise ValueError(f"mid_price must be in (0, 1), got {mid_price}")
    if spread < 0:
        raise ValueError(f"spread must be non-negative, got {spread}")
    if size <= 0:
        raise ValueError(f"size must be positive, got {size}")

    half_spread = max(MIN_SPREAD, min(MAX_SPREAD, spread / 2.0))

    # Apply inventory skew: shift both prices
    bid_price = _clamp_price(mid_price - half_spread - inventory_skew)
    ask_price = _clamp_price(mid_price + half_spread - inventory_skew)

    # Ensure bid < ask
    if bid_price >= ask_price:
        adjustment = (bid_price - ask_price) / 2.0 + 0.001
        bid_price = _clamp_price(bid_price - adjustment)
        ask_price = _clamp_price(ask_price + adjustment)

    # Estimate rebate: 20% of daily fee pool, proportional to our share
    daily_fee_pool = daily_volume_usd * TAKER_FEE
    maker_pool = daily_fee_pool * MAKER_REBATE_SHARE
    # Assume we capture ~5% of maker volume as a conservative estimate
    our_volume = size * (bid_price + ask_price)
    our_share = min(1.0, our_volume / max(daily_volume_usd * 0.5, 1.0))
    estimated_rebate = maker_pool * our_share

    bid_quote = Quote(
        side="BUY",
        price=bid_price,
        size=round(size, 4),
        order_type="GTC",
        maker_fee=MAKER_FEE,
        estimated_rebate=round(estimated_rebate / 2, 6),
    )

    ask_quote = Quote(
        side="SELL",
        price=ask_price,
        size=round(size, 4),
        order_type="GTC",
        maker_fee=MAKER_FEE,
        estimated_rebate=round(estimated_rebate / 2, 6),
    )

    logger.info(
        "MM quotes: BID %s | ASK %s | skew=%.4f",
        bid_quote, ask_quote, inventory_skew,
    )

    return bid_quote, ask_quote


def create_mm_state(
    mid_price: float,
    spread: float,
    size: float,
    inventory: float = 0.0,
    max_inventory: float = 100.0,
    daily_volume_usd: float = 10_000.0,
) -> MarketMakingState:
    """Create a full market-making state with inventory-aware quotes.

    This is the main entry point for the market-making strategy.

    Args:
        mid_price: Current mid-market price.
        spread: Desired full spread.
        size: Order size per side.
        inventory: Current net inventory.
        max_inventory: Max inventory for skew calculation.
        daily_volume_usd: Daily volume for rebate estimation.

    Returns:
        A :class:`MarketMakingState` with computed quotes.
    """
    skew = calculate_inventory_skew(inventory, max_inventory)
    bid, ask = calculate_mm_quotes(
        mid_price, spread, size,
        inventory_skew=skew,
        daily_volume_usd=daily_volume_usd,
    )

    return MarketMakingState(
        mid_price=mid_price,
        bid_quote=bid,
        ask_quote=ask,
        spread=round(ask.price - bid.price, 6),
        inventory=inventory,
        skew_applied=skew,
        estimated_daily_rebate=round(
            (bid.estimated_rebate + ask.estimated_rebate) * 24, 4  # hourly → daily
        ),
    )


def should_requote(
    current_mid: float,
    quoted_mid: float,
    threshold: float = 0.005,
) -> bool:
    """Determine if quotes should be refreshed based on mid-price movement.

    Args:
        current_mid: Current mid-market price.
        quoted_mid: Mid-price when quotes were last placed.
        threshold: Minimum price movement to trigger re-quote.

    Returns:
        ``True`` if quotes should be updated.
    """
    return abs(current_mid - quoted_mid) >= threshold
