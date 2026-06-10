"""Volume anomaly and volume spike signal detection."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VolumeSignal:
    """Signal representing an unusual volume spike on a prediction market."""
    current_volume: float
    avg_volume: float
    z_score: float
    ratio: float
    detected_at: datetime


def calculate_z_score(value: float, history: Sequence[float]) -> float:
    """Helper to calculate Z-score of a value compared to its historical series."""
    if not history:
        return 0.0
    n = len(history)
    mean = sum(history) / n
    if n < 2:
        return 0.0
    variance = sum((x - mean) ** 2 for x in history) / (n - 1)
    std_dev = math.sqrt(variance)
    if std_dev == 0.0:
        return 0.0
    return (value - mean) / std_dev


def detect_volume_spike(
    current_volume: float,
    historical_volumes: Sequence[float],
    threshold: float = 2.0,
) -> VolumeSignal | None:
    """Checks if the current 24h volume represents a statistical spike.
    
    Uses standard deviation and mean of historical volume to calculate a Z-score.
    If the volume is more than `threshold` standard deviations above the mean,
    it triggers a signal.
    """
    if not historical_volumes:
        return None
        
    n = len(historical_volumes)
    mean_volume = sum(historical_volumes) / n
    z_score = calculate_z_score(current_volume, historical_volumes)
    ratio = current_volume / mean_volume if mean_volume > 0 else 0.0
    
    # Trigger if Z-score is above threshold OR (fallback) ratio is extremely high
    if z_score >= threshold or (n < 5 and ratio >= threshold):
        logger.debug(
            "Volume spike detected: Current=%s Avg=%s Z-score=%s Ratio=%s",
            current_volume, mean_volume, z_score, ratio
        )
        return VolumeSignal(
            current_volume=current_volume,
            avg_volume=mean_volume,
            z_score=z_score,
            ratio=ratio,
            detected_at=datetime.now(timezone.utc),
        )
        
    return None
