"""Internal arbitrage detection for Polymarket binary markets.

Detects when the sum of YES and NO prices deviates from 1.0 by more than
the round-trip taker fee, creating a risk-free arbitrage opportunity.

On Polymarket, each binary market has a YES token and a NO token.  In
equilibrium YES + NO ≈ $1.00.  When the sum drops below $1.00 minus fees,
buying *both* sides yields a guaranteed profit at settlement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Final

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TAKER_FEE: Final[float] = 0.018  # 1.8% per side (worst-case taker)
MIN_EDGE_BPS: Final[float] = 0.0005      # 0.05 % minimum to bother


class ArbDirection(str, Enum):
    """Direction of the arbitrage trade."""
    BUY_BOTH = "buy_both"      # YES + NO < 1.0 → buy both
    SELL_BOTH = "sell_both"     # YES + NO > 1.0 → sell both (requires inventory)


@dataclass(frozen=True, slots=True)
class ArbitrageSignal:
    """Signal emitted when an internal arbitrage opportunity is detected.

    Attributes:
        yes_price: Current best ask for the YES token (0-1 scale).
        no_price: Current best ask for the NO token (0-1 scale).
        gross_edge: Raw edge before fees — ``1.0 - (yes + no)``.
        net_edge: Edge after subtracting round-trip taker fees.
        direction: Whether to buy or sell both sides.
        size_hint: Suggested notional size in dollars (conservative).
        roi_pct: Return on investment as a percentage.
        detected_at: UTC timestamp of detection.
    """

    yes_price: float
    no_price: float
    gross_edge: float
    net_edge: float
    direction: ArbDirection
    size_hint: float
    roi_pct: float
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        return (
            f"[ARB {self.direction.value}] YES={self.yes_price:.4f} "
            f"NO={self.no_price:.4f} | gross={self.gross_edge:.4f} "
            f"net={self.net_edge:.4f} ({self.roi_pct:+.2f}%)"
        )


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------


def detect_internal_arbitrage(
    yes_price: float,
    no_price: float,
    *,
    taker_fee: float = DEFAULT_TAKER_FEE,
    max_position_usd: float = 500.0,
) -> ArbitrageSignal | None:
    """Detect an internal YES/NO arbitrage in a binary market.

    The logic is symmetric:
    * **BUY_BOTH** — if ``YES + NO < 1.0`` by more than two taker fees,
      buying one share of each side costs ``YES + NO`` and pays out ``$1.00``
      at settlement regardless of outcome.
    * **SELL_BOTH** — if ``YES + NO > 1.0`` by more than two taker fees,
      selling one share of each side collects ``YES + NO`` and costs ``$1.00``
      at settlement.  (Requires existing inventory or a short mechanism.)

    Args:
        yes_price: Best ask price for YES token (0–1 range).
        no_price: Best ask price for NO token (0–1 range).
        taker_fee: Per-side taker fee fraction.  Default 1.8 %.
        max_position_usd: Maximum notional size hint.

    Returns:
        An :class:`ArbitrageSignal` if a positive-edge opportunity exists,
        otherwise ``None``.

    Raises:
        ValueError: If prices are outside the valid [0, 1] range.
    """
    if not (0.0 <= yes_price <= 1.0):
        raise ValueError(f"yes_price must be in [0, 1], got {yes_price}")
    if not (0.0 <= no_price <= 1.0):
        raise ValueError(f"no_price must be in [0, 1], got {no_price}")

    total = yes_price + no_price
    round_trip_fees = 2.0 * taker_fee  # fee on each leg

    # BUY_BOTH: profit = 1.0 - total - fees
    buy_gross_edge = 1.0 - total
    buy_net_edge = buy_gross_edge - round_trip_fees

    # SELL_BOTH: profit = total - 1.0 - fees
    sell_gross_edge = total - 1.0
    sell_net_edge = sell_gross_edge - round_trip_fees

    # Pick the profitable direction (if any)
    if buy_net_edge > MIN_EDGE_BPS:
        direction = ArbDirection.BUY_BOTH
        gross_edge = buy_gross_edge
        net_edge = buy_net_edge
        cost = total
    elif sell_net_edge > MIN_EDGE_BPS:
        direction = ArbDirection.SELL_BOTH
        gross_edge = sell_gross_edge
        net_edge = sell_net_edge
        cost = 1.0  # settlement cost
    else:
        logger.debug(
            "No arb: YES=%.4f NO=%.4f total=%.4f buy_net=%.4f sell_net=%.4f",
            yes_price, no_price, total, buy_net_edge, sell_net_edge,
        )
        return None

    roi_pct = (net_edge / cost) * 100.0 if cost > 0 else 0.0
    size_hint = min(max_position_usd, max_position_usd * (net_edge / 0.05))

    signal = ArbitrageSignal(
        yes_price=yes_price,
        no_price=no_price,
        gross_edge=gross_edge,
        net_edge=net_edge,
        direction=direction,
        size_hint=round(size_hint, 2),
        roi_pct=round(roi_pct, 4),
    )

    logger.info("Internal arb detected: %s", signal)
    return signal


def calculate_arb_profit(
    yes_price: float,
    no_price: float,
    shares: float,
    *,
    taker_fee: float = DEFAULT_TAKER_FEE,
) -> float:
    """Calculate the dollar profit from a buy-both arbitrage.

    Args:
        yes_price: Price paid per YES share.
        no_price: Price paid per NO share.
        shares: Number of share-pairs purchased.
        taker_fee: Per-side taker fee fraction.

    Returns:
        Net dollar profit (can be negative if edge < fees).
    """
    cost_per_pair = yes_price + no_price
    fee_per_pair = (yes_price + no_price) * taker_fee  # fee on total spend
    payout_per_pair = 1.0
    profit_per_pair = payout_per_pair - cost_per_pair - fee_per_pair
    return round(profit_per_pair * shares, 6)
