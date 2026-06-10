"""Statistical edge detection for Polymarket prediction markets.

Uses Kullback–Leibler divergence and expected-value calculations to
identify when your probability estimate diverges enough from the market
price to justify a trade.

Edge = your estimated probability minus the market-implied probability,
weighted by the information-theoretic divergence to account for
calibration uncertainty.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Final

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_PROB: Final[float] = 0.001   # Floor to avoid log(0) in KL divergence
MAX_PROB: Final[float] = 0.999   # Ceiling for same reason
DEFAULT_MIN_EDGE: Final[float] = 0.02  # 2% minimum edge to trade
DEFAULT_TAKER_FEE: Final[float] = 0.018


class TradeSide(str, Enum):
    """Which side of the market to take."""
    BUY_YES = "buy_yes"
    BUY_NO = "buy_no"


@dataclass(frozen=True, slots=True)
class StatisticalSignal:
    """Signal indicating a statistically significant edge.

    Attributes:
        estimated_prob: Your probability estimate for the event.
        market_prob: Market-implied probability (from the price).
        edge: Raw edge (estimated - market).
        kl_divergence_value: KL divergence D_KL(P || Q).
        expected_value: Expected value per dollar risked.
        kelly_fraction: Full Kelly fraction for position sizing.
        recommended_side: Whether to buy YES or NO.
        confidence: Confidence level (0–1) based on edge magnitude.
        detected_at: UTC timestamp.
    """

    estimated_prob: float
    market_prob: float
    edge: float
    kl_divergence_value: float
    expected_value: float
    kelly_fraction: float
    recommended_side: TradeSide
    confidence: float
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        return (
            f"[STAT {self.recommended_side.value}] est={self.estimated_prob:.3f} "
            f"mkt={self.market_prob:.3f} edge={self.edge:+.3f} "
            f"EV={self.expected_value:+.4f} kelly={self.kelly_fraction:.3f} "
            f"conf={self.confidence:.2f}"
        )


# ---------------------------------------------------------------------------
# Core math
# ---------------------------------------------------------------------------


def _clamp_prob(p: float) -> float:
    """Clamp probability to [MIN_PROB, MAX_PROB] to avoid numerical issues."""
    return max(MIN_PROB, min(MAX_PROB, p))


def calculate_edge(estimated_prob: float, market_prob: float) -> float:
    """Calculate the raw edge between estimated and market probability.

    Uses KL divergence as a measure of the information-theoretic distance
    between distributions, scaled by the sign of the directional edge.

    Args:
        estimated_prob: Your probability estimate (0–1).
        market_prob: Market-implied probability (0–1).

    Returns:
        Signed KL-divergence-weighted edge.  Positive means the market
        *underprices* the event; negative means it *overprices* it.
    """
    p = _clamp_prob(estimated_prob)
    q = _clamp_prob(market_prob)

    # KL divergence: D_KL(P || Q) = p*log(p/q) + (1-p)*log((1-p)/(1-q))
    kl = p * math.log(p / q) + (1.0 - p) * math.log((1.0 - p) / (1.0 - q))

    # Sign: positive if we think the event is more likely than market
    direction = 1.0 if p > q else -1.0
    return direction * kl


def kl_divergence(p: float, q: float) -> float:
    """Compute KL divergence D_KL(P || Q) for Bernoulli distributions.

    Args:
        p: True probability estimate.
        q: Market probability.

    Returns:
        Non-negative KL divergence value.
    """
    p = _clamp_prob(p)
    q = _clamp_prob(q)
    return p * math.log(p / q) + (1.0 - p) * math.log((1.0 - p) / (1.0 - q))


def calculate_expected_value(
    prob: float,
    price: float,
    *,
    taker_fee: float = DEFAULT_TAKER_FEE,
) -> float:
    """Calculate expected value per dollar for buying YES at a given price.

    EV = prob * (1/price - 1) - (1 - prob) * 1 - taker_fee

    Simplified:
    EV = prob / price - 1 - taker_fee

    Args:
        prob: Your estimated probability of YES.
        price: Market price of YES token.
        taker_fee: Transaction fee as a fraction.

    Returns:
        Expected value per dollar risked.  Positive = profitable trade.
    """
    if price <= 0 or price >= 1:
        return 0.0

    # If we buy YES at `price`:
    # Win:  probability `prob`, payout = $1, cost = price → profit = (1 - price)
    # Lose: probability (1 - prob), payout = $0 → loss = price
    ev = prob * (1.0 - price) - (1.0 - prob) * price - taker_fee * price
    return ev / price  # normalize per dollar invested


def kelly_for_binary(prob: float, price: float) -> float:
    """Calculate the Kelly criterion fraction for a binary outcome.

    For a bet where you pay `price` and receive $1 if correct:
    f* = (p * (1 - price) - (1 - p) * price) / (1 - price)
       = (p - price) / (1 - price)

    Args:
        prob: Estimated probability.
        price: Market price.

    Returns:
        Kelly fraction (0–1).  Negative means don't bet.
    """
    if price <= 0 or price >= 1:
        return 0.0
    f = (prob - price) / (1.0 - price)
    return max(0.0, f)


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------


def detect_statistical_edge(
    estimated_prob: float,
    market_price: float,
    *,
    min_edge: float = DEFAULT_MIN_EDGE,
    taker_fee: float = DEFAULT_TAKER_FEE,
    kelly_fraction: float = 0.25,  # Use quarter-Kelly by default
) -> StatisticalSignal | None:
    """Detect whether a statistically significant edge exists.

    Checks both the directional edge and expected value, accounting
    for fees.

    Args:
        estimated_prob: Your probability estimate for YES (0–1).
        market_price: Current YES token price (0–1).
        min_edge: Minimum absolute probability edge to consider.
        taker_fee: Per-side taker fee.
        kelly_fraction: Fraction of full Kelly for position sizing.

    Returns:
        A :class:`StatisticalSignal` if edge exceeds threshold,
        otherwise ``None``.
    """
    if not (0.0 < estimated_prob < 1.0):
        raise ValueError(f"estimated_prob must be in (0, 1), got {estimated_prob}")
    if not (0.0 < market_price < 1.0):
        raise ValueError(f"market_price must be in (0, 1), got {market_price}")

    market_prob = market_price  # on Polymarket, price ≈ implied prob
    raw_edge = estimated_prob - market_prob

    # Determine side
    if raw_edge > 0:
        # Market underprices YES → buy YES
        side = TradeSide.BUY_YES
        trade_price = market_price
        trade_prob = estimated_prob
    else:
        # Market overprices YES → buy NO (equivalent to selling YES)
        side = TradeSide.BUY_NO
        trade_price = 1.0 - market_price  # NO price
        trade_prob = 1.0 - estimated_prob  # prob of NO

    abs_edge = abs(raw_edge)

    if abs_edge < min_edge:
        logger.debug(
            "No stat edge: est=%.3f mkt=%.3f edge=%.3f (min=%.3f)",
            estimated_prob, market_prob, abs_edge, min_edge,
        )
        return None

    kl_val = kl_divergence(estimated_prob, market_prob)
    ev = calculate_expected_value(trade_prob, trade_price, taker_fee=taker_fee)
    full_kelly = kelly_for_binary(trade_prob, trade_price)
    sized_kelly = full_kelly * kelly_fraction

    # Only signal if EV is positive after fees
    if ev <= 0:
        logger.debug(
            "Negative EV after fees: est=%.3f mkt=%.3f ev=%.4f",
            estimated_prob, market_prob, ev,
        )
        return None

    # Confidence based on edge magnitude (sigmoid-like scaling)
    confidence = min(1.0, abs_edge / 0.15)  # ~15% edge → full confidence

    signal = StatisticalSignal(
        estimated_prob=estimated_prob,
        market_prob=market_prob,
        edge=round(raw_edge, 6),
        kl_divergence_value=round(kl_val, 6),
        expected_value=round(ev, 6),
        kelly_fraction=round(sized_kelly, 6),
        recommended_side=side,
        confidence=round(confidence, 4),
    )

    logger.info("Statistical edge detected: %s", signal)
    return signal
