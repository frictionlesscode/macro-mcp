"""The expenditure engine: trend weight and adaptive TDEE from energy balance.

This is the highest-risk module in the project (SPEC.md: "M1 and M2 carry all the real
risk"). Its output silently sets every macro target downstream, so every gate here fails
closed — a `None` with a stated reason, never a number the data doesn't support. The actual
validation gate is `scripts/simulate.py`, not this module's docstrings.

Point shapes are deliberately identical to the tools that will feed this engine in M4, so no
reshaping is needed at the call site:

    weight point  {"date": "YYYY-MM-DD", "weight_lb": float}   -- matches garmin-mcp's
                  get_body_trend()["points"]
    intake point  {"date": "YYYY-MM-DD", "status": "complete"|"partial"|"unlogged",
                  "kcal": float | None}   -- matches foods.get_intake_trend()["points"]
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date as Date
from typing import Any, Mapping, Sequence

from .models import CONFIDENCES, ValidationError

DEFAULT_ALPHA = 0.15
DEFAULT_KCAL_PER_LB = 3500.0
DEFAULT_WINDOW_DAYS = 28
DEFAULT_MIN_DAYS = 14

#: Confidence thresholds. An initial, documented heuristic -- not a fitted model. SPEC.md's
#: M7 (staged weekly proposals) is where real accept/decline outcomes exist to calibrate
#: this against; until then these are deliberately conservative round numbers.
_HIGH_DENSITY = 0.70   # >= 70% of window days logged complete, and weight gaps small
_HIGH_MAX_GAP = 3
_MEDIUM_MAX_GAP = 7


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def config_defaults() -> dict[str, float]:
    """Read engine parameters from the environment, falling back to the documented defaults."""
    return {
        "alpha": _env_float("TREND_SMOOTHING_ALPHA", DEFAULT_ALPHA),
        "kcal_per_lb": _env_float("KCAL_PER_LB", DEFAULT_KCAL_PER_LB),
        "window_days": _env_int("EXPENDITURE_WINDOW_DAYS", DEFAULT_WINDOW_DAYS),
        "min_days": _env_int("EXPENDITURE_MIN_DAYS", DEFAULT_MIN_DAYS),
    }


# --- trend weight ------------------------------------------------------------


@dataclass(frozen=True)
class TrendPoint:
    date: str
    weight_lb: float
    trend_lb: float


def compute_trend(points: Sequence[Mapping[str, Any]], alpha: float) -> list[TrendPoint]:
    """Time-aware exponentially-weighted moving average over (possibly irregular) weigh-ins.

    Standard EWMA (``trend[t] = trend[t-1] + alpha * (weight[t] - trend[t-1])``) assumes a
    fixed sampling interval. Weigh-ins aren't perfectly daily -- a realistic series has a largest gap of about a day, but any gap is possible -- so a multi-day gap needs to
    pull the trend proportionally further than a one-day gap would, or the trend lags behind
    reality after every missed day. The fix is a time-aware effective alpha:

        eff_alpha = 1 - (1 - alpha) ** gap_days

    which reduces to plain EWMA when gap_days == 1, and compounds correctly for longer gaps
    (two consecutive 1-day steps produce the same result as one 2-day step).
    """
    if not 0.0 < alpha < 1.0:
        raise ValidationError(f"alpha must be strictly between 0 and 1; got {alpha}")

    # Sorted, de-duplicated by date (last value wins) -- defensive against a caller passing
    # an unsorted or corrected-in-place series.
    by_date: dict[str, float] = {}
    for p in points:
        by_date[p["date"]] = float(p["weight_lb"])

    out: list[TrendPoint] = []
    prev_date: Date | None = None
    prev_trend: float | None = None
    for date_str in sorted(by_date):
        d = Date.fromisoformat(date_str)
        w = by_date[date_str]
        if prev_trend is None:
            trend = w
        else:
            gap = max((d - prev_date).days, 1)
            eff_alpha = 1 - (1 - alpha) ** gap
            trend = prev_trend + eff_alpha * (w - prev_trend)
        out.append(TrendPoint(date=date_str, weight_lb=w, trend_lb=trend))
        prev_date, prev_trend = d, trend
    return out


def _recency_weighted_mean(
    dated_values: Sequence[tuple[str, float]], alpha: float, end_date: Date
) -> float | None:
    """Mean that discounts older days, using the same smoothing constant as trend weight.

    SPEC.md calls for expenditure to weight "recent data ... more heavily" against a single
    tunable smoothing constant, rather than introducing a second unrelated knob. A day
    ``g`` days before ``end_date`` gets weight ``(1 - alpha) ** g`` -- the same decay factor
    that governs how fast the weight trend responds to a new observation.
    """
    if not dated_values:
        return None
    total = 0.0
    total_weight = 0.0
    for date_str, value in dated_values:
        gap = (end_date - Date.fromisoformat(date_str)).days
        w = (1 - alpha) ** max(gap, 0)
        total += w * value
        total_weight += w
    return total / total_weight if total_weight else None


# --- expenditure ---------------------------------------------------------------


@dataclass(frozen=True)
class ExpenditureResult:
    tdee: float | None
    tdee_null_reason: str | None
    confidence: str | None
    method: str
    days_used: int
    days_requested: int
    trend_weight_lb: float | None
    trend_lb_per_week: float | None
    kcal_per_lb_used: float
    avg_kcal_complete_days: float | None
    weight_coverage: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "tdee": None if self.tdee is None else round(self.tdee, 1),
            "tdee_null_reason": self.tdee_null_reason,
            "confidence": self.confidence,
            "method": self.method,
            "days_used": self.days_used,
            "days_requested": self.days_requested,
            "trend_weight_lb": (
                None if self.trend_weight_lb is None else round(self.trend_weight_lb, 2)
            ),
            "trend_lb_per_week": (
                None if self.trend_lb_per_week is None else round(self.trend_lb_per_week, 3)
            ),
            "kcal_per_lb_used": self.kcal_per_lb_used,
            "avg_kcal_complete_days": (
                None if self.avg_kcal_complete_days is None
                else round(self.avg_kcal_complete_days, 1)
            ),
            "weight_coverage": self.weight_coverage,
        }


def _null(
    reason: str,
    days_used: int,
    window_days: int,
    kcal_per_lb: float,
    avg_kcal: float | None = None,
    trend_weight_lb: float | None = None,
    trend_lb_per_week: float | None = None,
    coverage: dict[str, Any] | None = None,
) -> ExpenditureResult:
    return ExpenditureResult(
        tdee=None,
        tdee_null_reason=reason,
        confidence=None,
        method="energy_balance_v1",
        days_used=days_used,
        days_requested=window_days,
        trend_weight_lb=trend_weight_lb,
        trend_lb_per_week=trend_lb_per_week,
        kcal_per_lb_used=kcal_per_lb,
        avg_kcal_complete_days=avg_kcal,
        weight_coverage=coverage or {"point_count": 0, "days_spanned": 0, "largest_gap_days": None},
    )


def _confidence(days_used: int, window_days: int, largest_gap_days: int) -> str:
    density = days_used / window_days
    if density >= _HIGH_DENSITY and largest_gap_days <= _HIGH_MAX_GAP:
        result = "high"
    elif largest_gap_days <= _MEDIUM_MAX_GAP:
        result = "medium"
    else:
        result = "low"
    assert result in CONFIDENCES  # guards against the heuristic and the vocabulary drifting apart
    return result


def compute_expenditure(
    intake_points: Sequence[Mapping[str, Any]],
    weight_points: Sequence[Mapping[str, Any]],
    *,
    end_date: str | Date | None = None,
    window_days: int | None = None,
    min_days: int | None = None,
    alpha: float | None = None,
    kcal_per_lb: float | None = None,
) -> ExpenditureResult:
    """Estimate TDEE from energy balance over a trailing window.

    ``TDEE = mean_daily_intake + (weight_lost_lb * kcal_per_lb) / elapsed_days``

    Intentionally does **not** take activity data as an input. Expenditure is inferred from
    how the body actually responded to intake, so training is already reflected in the
    weight-change term -- adding exercise calories on top would double-count it (SPEC.md,
    "The expenditure engine").

    ``weight_points`` may (and, in real use via M4, should) extend earlier than the analysis
    window -- extra history lets the EWMA warm up before the window starts, which is a more
    reliable trend value than one that has just been initialized.

    Fails closed: if either the intake or the weight side of the calculation doesn't clear
    its minimum bar, the whole result is ``None`` with a reason naming which side failed and
    why -- never a number computed from insufficient data.
    """
    cfg = config_defaults()
    window_days = window_days if window_days is not None else int(cfg["window_days"])
    min_days = min_days if min_days is not None else int(cfg["min_days"])
    alpha = alpha if alpha is not None else cfg["alpha"]
    kcal_per_lb = kcal_per_lb if kcal_per_lb is not None else cfg["kcal_per_lb"]

    if window_days <= 0:
        raise ValidationError(f"window_days must be positive; got {window_days}")
    if min_days <= 0:
        raise ValidationError(f"min_days must be positive; got {min_days}")
    if kcal_per_lb <= 0:
        raise ValidationError(f"kcal_per_lb must be positive; got {kcal_per_lb}")

    all_intake_dates = [Date.fromisoformat(p["date"]) for p in intake_points]
    all_weight_dates = [Date.fromisoformat(p["date"]) for p in weight_points]
    candidate_dates = all_intake_dates + all_weight_dates
    if end_date is not None:
        resolved_end = end_date if isinstance(end_date, Date) else Date.fromisoformat(end_date)
    elif candidate_dates:
        resolved_end = max(candidate_dates)
    else:
        return _null("no intake or weight data supplied", 0, window_days, kcal_per_lb)

    start_date = Date.fromordinal(resolved_end.toordinal() - window_days + 1)

    # --- intake side ---------------------------------------------------------
    complete_in_window = [
        (p["date"], float(p["kcal"]))
        for p in intake_points
        if p.get("status") == "complete"
        and p.get("kcal") is not None
        and start_date <= Date.fromisoformat(p["date"]) <= resolved_end
    ]
    days_used = len(complete_in_window)
    avg_kcal = _recency_weighted_mean(complete_in_window, alpha, resolved_end)

    if days_used < min_days:
        return _null(
            f"only {days_used} complete day(s) logged in the trailing {window_days}-day "
            f"window; need at least {min_days}",
            days_used, window_days, kcal_per_lb, avg_kcal=avg_kcal,
        )

    # --- weight side -----------------------------------------------------------
    full_trend = compute_trend(weight_points, alpha)
    in_window = [tp for tp in full_trend if start_date <= Date.fromisoformat(tp.date) <= resolved_end]

    if len(in_window) < 2:
        coverage = {
            "point_count": len(in_window), "days_spanned": 0,
            "largest_gap_days": None,
        }
        return _null(
            f"only {len(in_window)} weigh-in(s) in the trailing {window_days}-day window; "
            f"need at least 2 to measure a trend",
            days_used, window_days, kcal_per_lb, avg_kcal=avg_kcal, coverage=coverage,
        )

    trend_start, trend_end = in_window[0], in_window[-1]
    # elapsed_days is the raw date difference -- the correct unit for the rate calculation
    # below (a rate is mass change over elapsed *time*, not point count). days_spanned is
    # the inclusive calendar-day count (elapsed_days + 1), matching garmin-mcp's own
    # get_body_trend convention (its real output for 12 daily points from Aug 1-12 reports
    # days_spanned: 12, not 11) -- used for reporting and for the coverage gate below, since
    # "need at least 14 days of data" means 14 calendar days, not a 14-day gap between points.
    elapsed_days = (Date.fromisoformat(trend_end.date) - Date.fromisoformat(trend_start.date)).days
    days_spanned = elapsed_days + 1
    gaps = [
        (Date.fromisoformat(b.date) - Date.fromisoformat(a.date)).days
        for a, b in zip(in_window, in_window[1:])
    ]
    largest_gap = max(gaps) if gaps else 0
    coverage = {
        "point_count": len(in_window),
        "days_spanned": days_spanned,
        "largest_gap_days": largest_gap,
    }

    if days_spanned < min_days:
        return _null(
            f"weigh-ins in the trailing {window_days}-day window span only {days_spanned} "
            f"day(s); need at least {min_days} to trust a weight-change rate",
            days_used, window_days, kcal_per_lb, avg_kcal=avg_kcal, coverage=coverage,
        )

    # --- energy balance -----------------------------------------------------
    # raw_delta follows garmin-mcp's get_body_trend sign convention: negative = weight loss.
    # A worked example, because a sign error here would silently invert every target:
    #   lost 2 lb over 14 days -> raw_delta = -2.0, elapsed_days = 14, kcal_per_lb = 3500
    #   -> deficit = -raw_delta * 3500 / 14 = +500 kcal/day
    #   -> tdee = avg_kcal + 500  (expenditure exceeds intake, which is what "losing weight" means)
    raw_delta = trend_end.trend_lb - trend_start.trend_lb
    trend_lb_per_week = raw_delta / elapsed_days * 7
    deficit_kcal_per_day = -raw_delta * kcal_per_lb / elapsed_days
    tdee = avg_kcal + deficit_kcal_per_day

    confidence = _confidence(days_used, window_days, largest_gap)

    return ExpenditureResult(
        tdee=tdee,
        tdee_null_reason=None,
        confidence=confidence,
        method="energy_balance_v1",
        days_used=days_used,
        days_requested=window_days,
        trend_weight_lb=trend_end.trend_lb,
        trend_lb_per_week=trend_lb_per_week,
        kcal_per_lb_used=kcal_per_lb,
        avg_kcal_complete_days=avg_kcal,
        weight_coverage=coverage,
    )
