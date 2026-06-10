"""Price anomaly and complement invariant signal detection."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PriceSignal:
    """Signal representing a price anomaly in YES/NO contracts."""
    yes_price: float
    no_price: float
    deviation: float
    detected_at: datetime


def detect_price_discrepancy(yes_price: float, no_price: float, threshold: float = 0.01) -> PriceSignal | None:
    """Checks if the sum of YES and NO prices deviates significantly from 1.0.
    
    In a standard binary market, YES + NO must equal 1.0. Deviations suggest
    either a delay in order book syncing or an imbalance.
    """
    total_price = yes_price + no_price
    deviation = abs(total_price - 1.0)
    
    if deviation >= threshold:
        logger.debug(
            "Price discrepancy detected: YES=%s NO=%s Sum=%s Dev=%s",
            yes_price, no_price, total_price, deviation
        )
        return PriceSignal(
            yes_price=yes_price,
            no_price=no_price,
            deviation=deviation,
            detected_at=datetime.now(timezone.utc),
        )
        
    return None
