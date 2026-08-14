"""The target-resolution algorithm: weekly energy budget -> per-day grams.

Pure functions, no DB access -- same design ethos as expenditure.py, for the same reason
(testable against known-answer cases without a database in the loop). See SPEC.md's
"## Targets" section for the algorithm this implements and, importantly, *why* each piece is
shaped the way it is -- particularly why `protein_g_per_lb`/`fat_g_per_lb_floor` are required
caller-supplied values with no default here: picking a ratio would be a nutritional stance,
which is exactly what this codebase's non-goals list rules out of the server.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date, timedelta
from typing import Mapping

from .models import Macros, ValidationError

#: Reserved for a future calorie-cycling feature (varying *total* daily kcal by day type,
#: not just carb distribution). Unused by this module today -- see SPEC.md's "## Targets",
#: point 1, for why that's a deliberate omission rather than an oversight.
UNUSED_ENERGY_WEIGHT_NOTE = (
    "day_type.energy_weight is reserved and not used by resolve_week() -- see SPEC.md"
)


def weekly_budget(tdee: float, rate_lb_per_week: float, kcal_per_lb: float) -> float:
    """The week's total energy budget implied by a TDEE and a goal rate.

    A positive rate_lb_per_week means gaining (bulk); negative means losing (cut) --
    matching get_expenditure's own trend_lb_per_week sign convention (negative = losing).

    Note the sign: budget = 7*TDEE + rate*kcal_per_lb, not minus. A worked example, because
    getting this backwards would silently invert every cut/bulk target the same way a flipped
    sign in expenditure.py would have inverted every TDEE:
        cutting at -1 lb/week, TDEE 2600, kcal_per_lb 3500
        -> budget = 7*2600 + (-1.0 * 3500) = 18200 - 3500 = 14700
        (below the 18200 maintenance budget, which is what "cutting" has to mean)
    """
    return 7.0 * tdee + (rate_lb_per_week * kcal_per_lb)


def week_start(d: Date) -> Date:
    """Monday of the ISO week containing d -- SPEC.md's chosen, arbitrary-but-fixed boundary."""
    return d - timedelta(days=d.weekday())


@dataclass(frozen=True)
class DayTarget:
    date: str
    day_type: str | None  # None when explicit_macros was used instead
    macros: Macros
    explicit: bool


@dataclass(frozen=True)
class WeekResolution:
    week_start: str
    weekly_budget_kcal: float
    days: list[DayTarget]
    infeasible: bool
    shortfall_kcal: float  # 0 unless infeasible
    resolved_total_kcal: float
    week_budget_delta: float

    def target_for(self, d: str) -> DayTarget | None:
        return next((day for day in self.days if day.date == d), None)


def resolve_week(
    *,
    week_of: Date,
    tdee: float,
    trend_weight_lb: float,
    rate_lb_per_week: float,
    protein_g_per_lb: float,
    fat_g_per_lb_floor: float,
    kcal_per_lb: float,
    day_type_assignment: Mapping[str, str],  # {date_iso: day_type_name} for explicit-day-type days
    day_types: Mapping[str, tuple[float, float]],  # {name: (energy_weight, carb_weight)}
    explicit_macros: Mapping[str, Macros] | None = None,  # {date_iso: Macros} overrides
    default_day_type: str = "moderate",
) -> WeekResolution:
    """Resolve one Monday-start week's worth of daily targets.

    ``day_type_assignment`` should already reflect training_plan's recurring weekday pattern
    with any day_plan overrides applied -- merging those two sources is the caller's job
    (goals.py), not this function's; this function only knows about final, resolved
    day-type-or-explicit-macros per date.
    """
    if protein_g_per_lb <= 0:
        raise ValidationError(f"protein_g_per_lb must be positive; got {protein_g_per_lb}")
    if fat_g_per_lb_floor <= 0:
        raise ValidationError(f"fat_g_per_lb_floor must be positive; got {fat_g_per_lb_floor}")
    if trend_weight_lb <= 0:
        raise ValidationError(f"trend_weight_lb must be positive; got {trend_weight_lb}")

    start = week_start(week_of)
    dates = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    explicit_macros = explicit_macros or {}

    budget = weekly_budget(tdee, rate_lb_per_week, kcal_per_lb)

    protein_g = protein_g_per_lb * trend_weight_lb
    fat_g = fat_g_per_lb_floor * trend_weight_lb
    protein_fat_kcal_per_day = protein_g * 4 + fat_g * 9

    resolvable_dates = [d for d in dates if d not in explicit_macros]
    explicit_kcal_total = sum(m.kcal for d, m in explicit_macros.items() if d in dates)

    protein_fat_kcal_total = protein_fat_kcal_per_day * len(resolvable_dates)
    carb_pool_kcal = budget - protein_fat_kcal_total - explicit_kcal_total

    infeasible = carb_pool_kcal < 0
    shortfall = abs(carb_pool_kcal) if infeasible else 0.0
    carb_pool_kcal = max(0.0, carb_pool_kcal)

    weights: dict[str, float] = {}
    for d in resolvable_dates:
        type_name = day_type_assignment.get(d, default_day_type)
        if type_name not in day_types:
            raise ValidationError(f"unknown day_type {type_name!r} assigned to {d}")
        _, carb_weight = day_types[type_name]
        weights[d] = carb_weight
    total_weight = sum(weights.values())

    days: list[DayTarget] = []
    resolved_total = explicit_kcal_total
    for d in dates:
        if d in explicit_macros:
            days.append(DayTarget(date=d, day_type=None, macros=explicit_macros[d], explicit=True))
            continue
        type_name = day_type_assignment.get(d, default_day_type)
        share = (weights[d] / total_weight) if total_weight > 0 else (1.0 / len(resolvable_dates))
        carb_g = (carb_pool_kcal * share) / 4.0
        macros = Macros(
            kcal=protein_g * 4 + fat_g * 9 + carb_g * 4,
            protein_g=protein_g, carb_g=carb_g, fat_g=fat_g, fiber_g=0.0,
        )
        resolved_total += macros.kcal
        days.append(DayTarget(date=d, day_type=type_name, macros=macros, explicit=False))

    return WeekResolution(
        week_start=start.isoformat(),
        weekly_budget_kcal=budget,
        days=days,
        infeasible=infeasible,
        shortfall_kcal=shortfall,
        resolved_total_kcal=resolved_total,
        week_budget_delta=budget - resolved_total if not infeasible else -shortfall,
    )
