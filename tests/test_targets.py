from datetime import date

import pytest

from macro_mcp.models import Macros, ValidationError
from macro_mcp.targets import resolve_week, week_start, weekly_budget

DAY_TYPES = {"rest": (0.90, 0.80), "moderate": (1.00, 1.00), "heavy": (1.15, 1.30)}


def test_weekly_budget_cut_is_lower_than_tdee():
    # losing 1 lb/week (negative rate, per convention): budget should be 3500 kcal below
    # 7x maintenance -- budget = 7*TDEE + rate*kcal_per_lb, so a negative rate subtracts.
    budget = weekly_budget(tdee=2600, rate_lb_per_week=-1.0, kcal_per_lb=3500)
    assert budget == pytest.approx(7 * 2600 + (-1.0 * 3500))
    assert budget < 7 * 2600


def test_weekly_budget_bulk_is_higher_than_tdee():
    budget = weekly_budget(tdee=2600, rate_lb_per_week=0.5, kcal_per_lb=3500)
    assert budget > 7 * 2600


def test_weekly_budget_maintain_equals_tdee_times_seven():
    budget = weekly_budget(tdee=2600, rate_lb_per_week=0.0, kcal_per_lb=3500)
    assert budget == pytest.approx(7 * 2600)


@pytest.mark.parametrize("d,expected_monday", [
    ("2026-08-10", "2026-08-10"),  # a Monday
    ("2026-08-11", "2026-08-10"),  # Tuesday
    ("2026-08-16", "2026-08-10"),  # Sunday
    ("2026-08-17", "2026-08-17"),  # next Monday
])
def test_week_start_finds_the_iso_monday(d, expected_monday):
    assert week_start(date.fromisoformat(d)) == date.fromisoformat(expected_monday)


def _resolve(**overrides):
    kwargs = dict(
        week_of=date(2026, 8, 10),
        tdee=2600.0,
        trend_weight_lb=190.0,
        rate_lb_per_week=-1.0,
        protein_g_per_lb=1.0,
        fat_g_per_lb_floor=0.35,
        kcal_per_lb=3500.0,
        day_type_assignment={},
        day_types=DAY_TYPES,
    )
    kwargs.update(overrides)
    return resolve_week(**kwargs)


# --- basic shape and week-total invariants ---------------------------------------


def test_resolves_seven_days():
    r = _resolve()
    assert len(r.days) == 7
    assert r.days[0].date == "2026-08-10"
    assert r.days[-1].date == "2026-08-16"


def test_week_totals_sum_to_budget_when_feasible():
    r = _resolve()
    assert not r.infeasible
    assert r.resolved_total_kcal == pytest.approx(r.weekly_budget_kcal, abs=0.01)
    assert r.week_budget_delta == pytest.approx(0.0, abs=0.01)


def test_protein_and_fat_are_flat_across_all_days():
    r = _resolve()
    proteins = {round(d.macros.protein_g, 4) for d in r.days}
    fats = {round(d.macros.fat_g, 4) for d in r.days}
    assert len(proteins) == 1
    assert len(fats) == 1
    assert proteins.pop() == pytest.approx(1.0 * 190.0)
    assert fats.pop() == pytest.approx(0.35 * 190.0)


def test_all_days_default_to_moderate_day_type_when_unassigned():
    r = _resolve()
    assert all(d.day_type == "moderate" for d in r.days)
    # uniform day type -> uniform carb_weight -> uniform carbs too
    carbs = {round(d.macros.carb_g, 4) for d in r.days}
    assert len(carbs) == 1


# --- carb_weight distribution ------------------------------------------------------


def test_heavy_day_gets_more_carbs_than_rest_day():
    assignment = {
        "2026-08-10": "heavy", "2026-08-11": "moderate", "2026-08-12": "moderate",
        "2026-08-13": "moderate", "2026-08-14": "moderate", "2026-08-15": "moderate",
        "2026-08-16": "rest",
    }
    r = _resolve(day_type_assignment=assignment)
    heavy = r.target_for("2026-08-10")
    rest = r.target_for("2026-08-16")
    moderate = r.target_for("2026-08-11")
    assert heavy.macros.carb_g > moderate.macros.carb_g > rest.macros.carb_g


def test_carb_distribution_is_proportional_to_carb_weight():
    assignment = {d: "heavy" for d in [
        "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
        "2026-08-14", "2026-08-15", "2026-08-16",
    ]}
    assignment["2026-08-10"] = "rest"  # one rest day among six heavy days
    r = _resolve(day_type_assignment=assignment)
    rest_carb = r.target_for("2026-08-10").macros.carb_g
    heavy_carb = r.target_for("2026-08-11").macros.carb_g
    # ratio should match the day_type table's carb_weight ratio (0.80 : 1.30)
    assert heavy_carb / rest_carb == pytest.approx(1.30 / 0.80, rel=0.01)


def test_unknown_day_type_raises():
    with pytest.raises(ValidationError, match="unknown day_type"):
        _resolve(day_type_assignment={"2026-08-10": "ultra-mega-day"})


# --- explicit macro overrides -------------------------------------------------------


def test_explicit_macros_override_a_specific_day_exactly():
    explicit = {"2026-08-12": Macros(kcal=1800, protein_g=150, carb_g=150, fat_g=60)}
    r = _resolve(explicit_macros=explicit)
    wednesday = r.target_for("2026-08-12")
    assert wednesday.explicit is True
    assert wednesday.day_type is None
    assert wednesday.macros.kcal == 1800
    assert wednesday.macros.protein_g == 150


def test_explicit_day_still_counts_toward_week_total():
    explicit = {"2026-08-12": Macros(kcal=1800, protein_g=150, carb_g=150, fat_g=60)}
    r = _resolve(explicit_macros=explicit)
    assert not r.infeasible
    assert r.resolved_total_kcal == pytest.approx(r.weekly_budget_kcal, abs=0.01)


def test_explicit_day_lighter_than_average_leaves_more_carbs_for_the_rest():
    # this explicit day (1800 kcal) is lighter than a normally-resolved day works out to
    # (~3100 kcal in the baseline case) -- removing a below-average day from the pool of
    # resolvable days leaves *more* carb-pool kcal per remaining day, not less. The direction
    # depends on whether the explicit day is above or below the week's average -- this is not
    # a fixed "explicit days shrink everyone else" rule.
    explicit = {"2026-08-12": Macros(kcal=1800, protein_g=150, carb_g=150, fat_g=60)}
    r = _resolve(explicit_macros=explicit)
    baseline = _resolve()
    assert r.target_for("2026-08-11").macros.carb_g > baseline.target_for("2026-08-11").macros.carb_g


# --- infeasibility ----------------------------------------------------------------


def test_infeasible_week_clamps_carbs_to_zero_and_reports_shortfall():
    # a very aggressive cut with high protein/fat floors on a low TDEE
    r = _resolve(tdee=1400.0, rate_lb_per_week=-2.0, protein_g_per_lb=1.5, fat_g_per_lb_floor=0.6)
    assert r.infeasible
    assert r.shortfall_kcal > 0
    assert all(d.macros.carb_g == 0 for d in r.days if not d.explicit)
    # protein and fat floors are NOT reduced to compensate -- server reports, doesn't rebalance
    assert r.days[0].macros.protein_g == pytest.approx(1.5 * 190.0)
    assert r.days[0].macros.fat_g == pytest.approx(0.6 * 190.0)


def test_infeasible_week_budget_delta_is_negative_shortfall():
    r = _resolve(tdee=1400.0, rate_lb_per_week=-2.0, protein_g_per_lb=1.5, fat_g_per_lb_floor=0.6)
    assert r.week_budget_delta == pytest.approx(-r.shortfall_kcal)


# --- validation ----------------------------------------------------------------------


def test_rejects_non_positive_protein_ratio():
    with pytest.raises(ValidationError):
        _resolve(protein_g_per_lb=0)


def test_rejects_non_positive_fat_floor():
    with pytest.raises(ValidationError):
        _resolve(fat_g_per_lb_floor=-0.1)


def test_rejects_non_positive_trend_weight():
    with pytest.raises(ValidationError):
        _resolve(trend_weight_lb=0)
