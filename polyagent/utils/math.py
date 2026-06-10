"""Mathematical utilities for quantitative trading, position sizing, and edge calculation."""

from __future__ import annotations

import math
import logging
from typing import Sequence

logger = logging.getLogger(__name__)


def implied_probability(price: float) -> float:
    """Converts a binary contract price (0.0 to 1.0) to an implied probability.
    
    Ensures the probability remains within bounds [0.001, 0.999] to avoid division
    by zero or log of zero in downstream calculations.
    """
    return max(0.001, min(0.999, price))


def kl_divergence(p: float, q: float) -> float:
    """Computes the Kullback-Leibler (KL) divergence D_KL(P || Q) for binary distributions.
    
    p: Estimated/True probability
    q: Market-implied probability (price)
    """
    p = max(0.001, min(0.999, p))
    q = max(0.001, min(0.999, q))
    
    kl_p = p * math.log(p / q)
    kl_q = (1 - p) * math.log((1 - p) / (1 - q))
    return kl_p + kl_q


def kelly_criterion(edge: float, odds: float) -> float:
    """Calculates the standard Kelly Criterion fraction.
    
    odds: Decimal payout multiplier (e.g., if price is 0.40, odds = 1 / 0.40 = 2.5)
    edge: Probability of winning * odds - 1 (Expected Value)
    
    Formula: f* = edge / (odds - 1)
    """
    if odds <= 1.0:
        return 0.0
    
    b = odds - 1.0  # net odds
    fraction = edge / b
    return max(0.0, fraction)


def fractional_kelly(edge: float, odds: float, fraction: float = 0.25) -> float:
    """Calculates the fractional Kelly Criterion fraction to scale down risk.
    
    fraction: Kelly multiplier (defaults to 0.25 for Quarter Kelly)
    """
    return kelly_criterion(edge, odds) * fraction


def calculate_ev(prob: float, payout: float, cost: float) -> float:
    """Calculates the expected value (EV) of a trade per unit of cost.
    
    prob: Probability of winning (0.0 to 1.0)
    payout: Value at settlement (e.g., $1.00 per share)
    cost: Price paid per share (0.01 to 0.99)
    """
    if cost <= 0:
        return 0.0
    expected_payout = prob * payout
    return (expected_payout - cost) / cost


def calculate_sharpe(returns: Sequence[float], risk_free: float = 0.0) -> float:
    """Calculates the Sharpe ratio of a sequence of returns.
    
    returns: List of return percentages or dollar returns
    risk_free: Risk-free rate of return (per period)
    """
    if not returns or len(returns) < 2:
        return 0.0
        
    n = len(returns)
    mean_return = sum(returns) / n
    excess_returns = [r - risk_free for r in returns]
    mean_excess = sum(excess_returns) / n
    
    # Calculate sample standard deviation
    variance = sum((r - mean_excess) ** 2 for r in excess_returns) / (n - 1)
    std_dev = math.sqrt(variance)
    
    if std_dev == 0.0:
        return 0.0
        
    return mean_excess / std_dev
