"""Honest latency statistics.

Percentile definition: linear interpolation on sorted samples, i.e. rank
``p/100 * (n - 1)`` with fractional blending between adjacent samples
(the common "linear" method). This is a true percentile over individual
observations, never an average of averages.

Sample-size honesty: P50 needs at least 1 sample; P95/P99 need at least 5.
Below that the fields are NULL and ``insufficient_samples`` is True, so no
consumer can mistake noise for a tail measurement. Units: milliseconds in,
milliseconds out.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

MIN_SAMPLES_TAIL = 5


def percentile(samples: Sequence[float], pct: float) -> Optional[float]:
    """Linear-interpolation percentile, or None when empty."""
    values = sorted(float(value) for value in samples)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    rank = max(0.0, min(100.0, float(pct))) / 100.0 * (len(values) - 1)
    low = int(rank)
    high = min(low + 1, len(values) - 1)
    fraction = rank - low
    return values[low] + (values[high] - values[low]) * fraction


def summarize_latencies(samples: Sequence[Any]) -> Dict[str, Any]:
    """Summarize one latency sample set with explicit sample honesty."""
    values = []
    for item in samples or []:
        try:
            number = float(item)
        except (TypeError, ValueError):
            continue
        if number != number or number in (float("inf"), float("-inf")):
            continue
        values.append(number)
    count = len(values)
    if count == 0:
        return {
            "n": 0, "min": None, "max": None, "mean": None,
            "p50": None, "p95": None, "p99": None,
            "insufficient_samples": True,
        }
    enough = count >= MIN_SAMPLES_TAIL
    return {
        "n": count,
        "min": min(values),
        "max": max(values),
        "mean": sum(values) / count,
        "p50": percentile(values, 50),
        "p95": percentile(values, 95) if enough else None,
        "p99": percentile(values, 99) if enough else None,
        "insufficient_samples": not enough,
    }


def rate(numerator: Any, denominator: Any) -> Optional[float]:
    """Ratio in 0..1, or None when the denominator is not positive."""
    try:
        top, bottom = float(numerator), float(denominator)
    except (TypeError, ValueError):
        return None
    if bottom <= 0:
        return None
    return max(0.0, min(1.0, top / bottom))


def deltas(before: Optional[float], after: Optional[float]) -> Dict[str, Optional[float]]:
    """Before/after comparison with absolute and relative change."""
    if before is None or after is None:
        return {"before": before, "after": after,
                "absolute": None, "relative": None}
    absolute = after - before
    relative = (absolute / before) if before else None
    return {"before": before, "after": after,
            "absolute": absolute, "relative": relative}
