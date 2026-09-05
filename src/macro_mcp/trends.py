"""Trend statistics over logged intake and stored targets.

Deterministic arithmetic, no opinions. This module reports how intake compared to whatever
targets were set; it never judges whether those targets were right, and it has no thresholds
of its own. "Is 12% over on carbs a problem" is a question for the conversation, not for a
server.

Two rules shape everything here:

*Unlogged days are unknown, not zero.* They are excluded from every average and every
adherence rate rather than counted as zero-intake days, which would silently drag every
figure downward in proportion to how busy the user's week was.

*Sparse windows suppress rather than guess.* A "28-day average" computed from four logged
days is not an average. Below ``TREND_MIN_DAYS`` usable days the statistics come back as
``None`` with a stated reason, matching the fail-closed convention used throughout.
"""

from __future__ import annotations

import os
import statistics
from datetime import date as Date, timedelta
from typing import Any, Mapping, Sequence

from .models import MACRO_FIELDS, ValidationError, today

DEFAULT_MIN_DAYS = 7

#: Intake within this fraction of target counts as "on target" rather than over or under.
#: A band is necessary because exact equality never happens with real food; the specific
#: width is a reporting convention, not a nutritional claim, and it is stated in the output
#: so a caller can apply a different one.
ON_TARGET_BAND = 0.05


def min_days() -> int:
    raw = os.environ.get("TREND_MIN_DAYS")
    return int(raw) if raw else DEFAULT_MIN_DAYS


def _mean(values: Sequence[float]) -> float | None:
    return statistics.mean(values) if values else None


def compute(
    intake_points: Sequence[Mapping[str, Any]],
    targets_by_day: Mapping[str, Mapping[str, float]],
    metrics: Sequence[str] = MACRO_FIELDS,
    minimum_days: int | None = None,
) -> dict[str, Any]:
    """Statistics for a window of intake points against the targets set for those dates.

    ``intake_points`` is ``foods.get_intake_trend``'s ``points`` shape: one entry per date in
    the window with a ``status`` and either macro values or ``None``.
    """
    for m in metrics:
        if m not in MACRO_FIELDS:
            raise ValidationError(f"unknown metric {m!r}; expected any of {', '.join(MACRO_FIELDS)}")

    floor = min_days() if minimum_days is None else minimum_days

    complete = [p for p in intake_points if p.get("status") == "complete"
                and p.get(metrics[0]) is not None]
    coverage = {
        "days_requested": len(intake_points),
        "days_complete": sum(1 for p in intake_points if p.get("status") == "complete"),
        "days_partial": sum(1 for p in intake_points if p.get("status") == "partial"),
        "days_unlogged": sum(1 for p in intake_points if p.get("status") == "unlogged"),
        "days_with_targets": sum(1 for p in intake_points if p["date"] in targets_by_day),
    }

    series = {
        m: [
            {
                "date": p["date"],
                "status": p["status"],
                # None, never 0 -- an unlogged day is a gap in the series, and charts must
                # render it as a break rather than a dive to the axis.
                "intake": p.get(m) if p.get("status") == "complete" else None,
                "target": targets_by_day.get(p["date"], {}).get(m),
            }
            for p in intake_points
        ]
        for m in metrics
    }

    if len(complete) < floor:
        return {
            "metrics": list(metrics),
            "series": series,
            "coverage": coverage,
            "averages": None,
            "adherence": None,
            "suppressed_reason": (
                f"only {len(complete)} day(s) marked complete in this window; "
                f"need at least {floor} for a meaningful average"
            ),
        }

    averages: dict[str, float] = {}
    adherence: dict[str, Any] = {}

    for m in metrics:
        logged = [p[m] for p in complete if p.get(m) is not None]
        averages[m] = round(statistics.mean(logged), 1) if logged else None

        paired = [
            (p[m], targets_by_day[p["date"]][m])
            for p in complete
            if p.get(m) is not None
            and p["date"] in targets_by_day
            and m in targets_by_day[p["date"]]
        ]
        if not paired:
            adherence[m] = {
                "days_compared": 0,
                "null_reason": "no days in this window had both a logged total and a target",
            }
            continue

        deviations = [intake - target for intake, target in paired]
        relative = [
            (intake - target) / target for intake, target in paired if target > 0
        ]
        over = sum(1 for r in relative if r > ON_TARGET_BAND)
        under = sum(1 for r in relative if r < -ON_TARGET_BAND)
        on_target = len(relative) - over - under

        adherence[m] = {
            "days_compared": len(paired),
            # Signed mean is the bias (consistently over/under); absolute mean is the scatter.
            # Reported separately because a steady small overshoot and wild swings that
            # average to zero are different problems with different fixes.
            "mean_deviation": round(_mean(deviations), 1),
            "mean_abs_deviation": round(_mean([abs(d) for d in deviations]), 1),
            "days_over": over,
            "days_under": under,
            "days_on_target": on_target,
            "on_target_band": ON_TARGET_BAND,
            "mean_pct_deviation": (
                round(_mean(relative) * 100, 1) if relative else None
            ),
        }

    return {
        "metrics": list(metrics),
        "series": series,
        "coverage": coverage,
        "averages": averages,
        "adherence": adherence,
        "suppressed_reason": None,
    }


def rolling_average(
    points: Sequence[Mapping[str, Any]], metric: str, window: int = 7
) -> list[dict[str, Any]]:
    """Trailing rolling average, skipping unlogged days rather than treating them as zero.

    A point is emitted only once at least half the window has usable data, so the leading
    edge of a series isn't a misleading near-vertical line built from one or two days.
    """
    if window < 1:
        raise ValidationError(f"window must be positive; got {window}")

    out: list[dict[str, Any]] = []
    for i, p in enumerate(points):
        window_slice = points[max(0, i - window + 1): i + 1]
        usable = [
            q[metric] for q in window_slice
            if q.get("status") == "complete" and q.get(metric) is not None
        ]
        out.append({
            "date": p["date"],
            "value": round(statistics.mean(usable), 1) if len(usable) >= max(1, window // 2) else None,
            "days_used": len(usable),
        })
    return out
