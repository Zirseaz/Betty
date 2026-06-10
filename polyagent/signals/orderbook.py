"""Order book depth and bid/ask volume imbalance signal detection."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ImbalanceSignal:
    """Signal representing an order book depth imbalance."""
    bid_volume: float
    ask_volume: float
    imbalance_ratio: float  # Range: -1.0 (pure ask) to 1.0 (pure bid)
    detected_at: datetime


def detect_orderbook_imbalance(
    bids: Sequence[list[str | float] | dict],
    asks: Sequence[list[str | float] | dict],
    depth_ticks: int = 5,
    threshold: float = 0.3,
) -> ImbalanceSignal | None:
    """Calculates order book imbalance at a certain depth.
    
    Bids/Asks are lists of [price, size] or dicts {"price": ..., "size": ...}.
    Imbalance is calculated as: (bid_vol - ask_vol) / (bid_vol + ask_vol).
    An imbalance near 1.0 indicates heavy buying pressure.
    An imbalance near -1.0 indicates heavy selling pressure.
    """
    bid_volume = 0.0
    ask_volume = 0.0
    
    # Process bids up to depth_ticks
    for i, bid in enumerate(bids[:depth_ticks]):
        try:
            if isinstance(bid, (list, tuple)):
                size = float(bid[1])
            elif isinstance(bid, dict):
                size = float(bid.get("size", 0.0))
            else:
                continue
            bid_volume += size
        except (ValueError, TypeError, IndexError) as e:
            logger.warning("Error parsing bid at index %s: %s", i, e)

    # Process asks up to depth_ticks
    for i, ask in enumerate(asks[:depth_ticks]):
        try:
            if isinstance(ask, (list, tuple)):
                size = float(ask[1])
            elif isinstance(ask, dict):
                size = float(ask.get("size", 0.0))
            else:
                continue
            ask_volume += size
        except (ValueError, TypeError, IndexError) as e:
            logger.warning("Error parsing ask at index %s: %s", i, e)

    total_volume = bid_volume + ask_volume
    if total_volume <= 0.0:
        return None
        
    imbalance_ratio = (bid_volume - ask_volume) / total_volume
    
    if abs(imbalance_ratio) >= threshold:
        logger.debug(
            "Order book imbalance: BidVol=%s AskVol=%s Ratio=%s",
            bid_volume, ask_volume, imbalance_ratio
        )
        return ImbalanceSignal(
            bid_volume=bid_volume,
            ask_volume=ask_volume,
            imbalance_ratio=imbalance_ratio,
            detected_at=datetime.now(timezone.utc),
        )
        
    return None
