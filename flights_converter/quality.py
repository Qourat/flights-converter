"""Pure completeness arithmetic; calibration requires caller-audited evidence."""

import re
from dataclasses import dataclass
from datetime import date
from statistics import median


@dataclass(frozen=True)
class ReleaseStats:
    day: date
    input_bytes: int
    hour_aircraft: tuple[int, ...]
    complete: bool = False

    def __post_init__(self):
        if (
            type(self.input_bytes) is not int
            or self.input_bytes <= 0
            or len(self.hour_aircraft) != 24
            or any(type(n) is not int or n < 0 for n in self.hour_aircraft)
        ):
            raise ValueError("invalid release statistics")


def assess(current: ReleaseStats, history, *, hourly_factor: float, rule_version: str):
    """Use the last four earlier complete same-weekday releases; no bootstrap fiction."""
    if (
        type(hourly_factor) not in (int, float)
        or not 0 < hourly_factor <= 1
        or not isinstance(rule_version, str)
        or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", rule_version)
    ):
        raise ValueError("invalid quality rule")
    references = sorted(
        (
            r
            for r in history
            if r.complete and r.day < current.day and r.day.weekday() == current.day.weekday()
        ),
        key=lambda r: r.day,
        reverse=True,
    )
    if len({r.day for r in references}) != len(references):
        raise ValueError("duplicate reference day")
    references = references[:4]
    result = {
        "rule_version": rule_version,
        "hourly_factor": hourly_factor,
        "reference_days": [r.day.isoformat() for r in references],
        "size_ratio": None,
        "hour_ratios": [None] * 24,
        "gap_hours": [],
        "good": False,
        "reason": "insufficient_references",
    }
    if len(references) != 4:
        return result
    size_ratio = current.input_bytes / median(r.input_bytes for r in references)
    denominators = [median(r.hour_aircraft[h] for r in references) for h in range(24)]
    ratios = [
        current.hour_aircraft[h] / denominator if denominator else None
        for h, denominator in enumerate(denominators)
    ]
    gaps = [h for h, ratio in enumerate(ratios) if ratio is None or ratio < hourly_factor]
    result.update(size_ratio=size_ratio, hour_ratios=ratios, gap_hours=gaps)
    if any(ratio is None for ratio in ratios):
        result["reason"] = "unusable_hour_references"
    elif size_ratio < 0.85:
        result["reason"] = "size_below_threshold"
    elif gaps:
        result["reason"] = "hour_below_threshold"
    else:
        result.update(good=True, reason="passed")
    return result


def calibrate(audit, *, rule_version: str):
    """audit: (stats, history, healthy_boolean) entries independently audited by caller.

    False labels denote known hourly outages. Size alone cannot detect those
    hours. No candidate is selected without both healthy and outage examples. The caller
    must supply the required eight-week audit; this function cannot certify it.
    """
    if not isinstance(rule_version, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", rule_version):
        raise ValueError("invalid quality rule")
    audit = list(audit)
    table = []
    for candidate in (0.50, 0.60, 0.70, 0.80):
        results = [
            (assess(stats, history, hourly_factor=candidate, rule_version=rule_version), healthy)
            for stats, history, healthy in audit
        ]
        false_rejections = sum(not result["good"] for result, healthy in results if healthy)
        missed_outages = sum(not result["gap_hours"] for result, healthy in results if not healthy)
        sufficient = bool(results) and any(h for _, h in results) and any(not h for _, h in results)
        sufficient &= all(
            r["reason"] not in ("insufficient_references", "unusable_hour_references")
            for r, _ in results
        )
        table.append(
            {
                "factor": candidate,
                "healthy_rejections": false_rejections,
                "missed_outages": missed_outages,
                "passes": sufficient and false_rejections == 0 and missed_outages == 0,
            }
        )
    passing = [row["factor"] for row in table if row["passes"]]
    return {
        "rule_version": rule_version,
        "factor": max(passing) if passing else None,
        "table": table,
    }
