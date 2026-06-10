"""Negative-risk strategy for multi-outcome Polymarket markets.

In a multi-outcome market (e.g., "Who will win the election?" with
candidates A, B, C, D), each outcome has a YES token.  If the sum of
all YES prices exceeds $1.00 (which it often does due to market
friction), you can buy NO on *every* outcome and guarantee profit.

Exactly one outcome will resolve YES → your NO on that outcome pays $0.
All other NOs resolve YES → each pays $1.00.

Cost  = sum(NO_prices) = sum(1 - YES_price) = N - sum(YES_prices)
Payout = (N - 1) * $1.00
Profit = Payout - Cost = (N - 1) - (N - sum_yes) = sum_yes - 1.0

So the strategy is profitable whenever sum(YES_prices) > 1.0 + fees.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final

logger = logging.getLogger(__name__)

DEFAULT_TAKER_FEE: Final[float] = 0.018
MIN_EDGE: Final[float] = 0.005  # half a cent minimum edge per share


@dataclass(frozen=True, slots=True)
class OutcomeAllocation:
    """How much to allocate to a single outcome's NO position."""

    outcome_id: str
    outcome_label: str
    yes_price: float
    no_price: float
    shares: float
    cost: float


@dataclass(frozen=True, slots=True)
class NegativeRiskSignal:
    """Signal for a negative-risk opportunity across a multi-outcome market.

    Attributes:
        market_id: The condition ID or slug of the multi-outcome market.
        num_outcomes: Total number of outcomes.
        sum_yes_prices: Sum of all YES prices (>1.0 means opportunity).
        gross_edge: ``sum_yes - 1.0`` — edge before fees.
        net_edge: Edge after all taker fees.
        total_cost: Total cost to buy NO on every outcome.
        guaranteed_payout: Guaranteed payout at settlement (N - 1).
        allocations: Per-outcome allocation details.
        roi_pct: Return on investment percentage.
        detected_at: UTC timestamp of detection.
    """

    market_id: str
    num_outcomes: int
    sum_yes_prices: float
    gross_edge: float
    net_edge: float
    total_cost: float
    guaranteed_payout: float
    allocations: tuple[OutcomeAllocation, ...]
    roi_pct: float
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        return (
            f"[NEG-RISK] market={self.market_id} outcomes={self.num_outcomes} "
            f"sum_yes={self.sum_yes_prices:.4f} net_edge={self.net_edge:.4f} "
            f"ROI={self.roi_pct:+.2f}%"
        )


def _calculate_optimal_shares(
    outcomes: list[dict],
    budget: float,
) -> float:
    """Calculate the number of share-sets we can afford.

    Each "share-set" means 1 NO share on every outcome.
    Cost per set = sum(1 - yes_price_i) for all i.

    Args:
        outcomes: List of outcome dicts with ``yes_price``.
        budget: Maximum dollar budget.

    Returns:
        Number of complete share-sets (can be fractional on Polymarket).
    """
    cost_per_set = sum(1.0 - o["yes_price"] for o in outcomes)
    if cost_per_set <= 0:
        return 0.0
    return budget / cost_per_set


def detect_negative_risk(
    outcomes: list[dict],
    *,
    market_id: str = "unknown",
    taker_fee: float = DEFAULT_TAKER_FEE,
    max_budget_usd: float = 1000.0,
) -> NegativeRiskSignal | None:
    """Detect a negative-risk opportunity in a multi-outcome market.

    Args:
        outcomes: List of outcome dicts, each containing at minimum:
            - ``outcome_id`` (str): Token ID or outcome identifier.
            - ``outcome_label`` (str): Human-readable label (e.g. "Trump").
            - ``yes_price`` (float): Current YES price in [0, 1].
        market_id: Identifier for the parent market.
        taker_fee: Per-side taker fee fraction.
        max_budget_usd: Maximum budget for the trade.

    Returns:
        A :class:`NegativeRiskSignal` if a profitable opportunity exists,
        otherwise ``None``.

    Raises:
        ValueError: If fewer than 2 outcomes are provided or prices invalid.
    """
    if len(outcomes) < 2:
        raise ValueError("Need at least 2 outcomes for negative-risk strategy")

    # Validate prices
    for o in outcomes:
        yp = o.get("yes_price", -1.0)
        if not (0.0 <= yp <= 1.0):
            raise ValueError(
                f"Invalid yes_price {yp} for outcome {o.get('outcome_label', '?')}"
            )

    n = len(outcomes)
    sum_yes = sum(o["yes_price"] for o in outcomes)
    gross_edge = sum_yes - 1.0

    # Total fees: one taker fee per leg (N legs)
    total_fee_rate = n * taker_fee
    net_edge = gross_edge - total_fee_rate

    if net_edge <= MIN_EDGE:
        logger.debug(
            "No negative-risk: market=%s sum_yes=%.4f gross=%.4f net=%.4f",
            market_id, sum_yes, gross_edge, net_edge,
        )
        return None

    # Calculate allocations
    shares = _calculate_optimal_shares(outcomes, max_budget_usd)
    allocations: list[OutcomeAllocation] = []

    for o in outcomes:
        no_price = 1.0 - o["yes_price"]
        cost = no_price * shares
        allocations.append(
            OutcomeAllocation(
                outcome_id=o.get("outcome_id", ""),
                outcome_label=o.get("outcome_label", ""),
                yes_price=o["yes_price"],
                no_price=round(no_price, 6),
                shares=round(shares, 4),
                cost=round(cost, 4),
            )
        )

    total_cost = sum(a.cost for a in allocations)
    guaranteed_payout = (n - 1) * shares  # N-1 NOs pay out
    roi_pct = ((guaranteed_payout - total_cost) / total_cost * 100.0) if total_cost > 0 else 0.0

    signal = NegativeRiskSignal(
        market_id=market_id,
        num_outcomes=n,
        sum_yes_prices=round(sum_yes, 6),
        gross_edge=round(gross_edge, 6),
        net_edge=round(net_edge, 6),
        total_cost=round(total_cost, 4),
        guaranteed_payout=round(guaranteed_payout, 4),
        allocations=tuple(allocations),
        roi_pct=round(roi_pct, 4),
    )

    logger.info("Negative-risk detected: %s", signal)
    return signal


def calculate_negative_risk_pnl(
    allocations: tuple[OutcomeAllocation, ...],
    winning_outcome_id: str,
) -> float:
    """Calculate realised P&L after settlement.

    At settlement, exactly one outcome resolves YES.  The NO token for
    the winning outcome pays $0; all others pay $1.00 per share.

    Args:
        allocations: The allocations from the signal.
        winning_outcome_id: The outcome that resolved YES.

    Returns:
        Net dollar P&L.
    """
    total_cost = sum(a.cost for a in allocations)
    payout = sum(
        a.shares  # $1.00 per share for NO tokens that resolve YES
        for a in allocations
        if a.outcome_id != winning_outcome_id
    )
    return round(payout - total_cost, 6)
