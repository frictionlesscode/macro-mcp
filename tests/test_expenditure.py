import random
import statistics
from datetime import date, timedelta

import pytest

from macro_mcp.expenditure import (
    DEFAULT_ALPHA,
    DEFAULT_KCAL_PER_LB,
    compute_expenditure,
    compute_trend,
)
from macro_mcp.models import ValidationError


# --- compute_trend -------------------------------------------------------------


def test_trend_of_constant_weight_is_flat():
    points = [{"date": f"2026-01-{d:02d}", "weight_lb": 190.0} for d in range(1, 11)]
    trend = compute_trend(points, alpha=0.15)
    assert all(tp.trend_lb == pytest.approx(190.0) for tp in trend)


def test_trend_first_point_equals_raw_weight():
    points = [{"date": "2026-01-01", "weight_lb": 201.5}]
    trend = compute_trend(points, alpha=0.15)
    assert trend[0].trend_lb == pytest.approx(201.5)


def test_trend_moves_toward_a_step_change():
    points = [{"date": "2026-01-01", "weight_lb": 190.0},
              {"date": "2026-01-02", "weight_lb": 200.0}]
    trend = compute_trend(points, alpha=0.15)
    # moves toward the new value but does not jump all the way -- that's the point of smoothing
    assert 190.0 < trend[-1].trend_lb < 200.0


def test_two_one_day_gaps_equal_one_two_day_gap():
    """Two consecutive 1-day updates toward the *same* target value must equal one
    time-aware 2-day update toward that value -- that's the compounding identity the
    effective-alpha formula is built on. (A differing intermediate observation would
    correctly produce a different result; that's not what this checks.)
    """
    daily = compute_trend(
        [{"date": "2026-01-01", "weight_lb": 190.0},
         {"date": "2026-01-02", "weight_lb": 200.0},
         {"date": "2026-01-03", "weight_lb": 200.0}],
        alpha=0.2,
    )
    gapped = compute_trend(
        [{"date": "2026-01-01", "weight_lb": 190.0},
         {"date": "2026-01-03", "weight_lb": 200.0}],
        alpha=0.2,
    )
    assert daily[-1].trend_lb == pytest.approx(gapped[-1].trend_lb)


def test_trend_sorts_and_dedupes_by_date():
    points = [
        {"date": "2026-01-03", "weight_lb": 195.0},
        {"date": "2026-01-01", "weight_lb": 190.0},
        {"date": "2026-01-01", "weight_lb": 191.0},  # duplicate date -- last one should win
    ]
    trend = compute_trend(points, alpha=0.15)
    assert [tp.date for tp in trend] == ["2026-01-01", "2026-01-03"]
    assert trend[0].weight_lb == 191.0


def test_trend_rejects_out_of_range_alpha():
    with pytest.raises(ValidationError):
        compute_trend([{"date": "2026-01-01", "weight_lb": 190.0}], alpha=0.0)
    with pytest.raises(ValidationError):
        compute_trend([{"date": "2026-01-01", "weight_lb": 190.0}], alpha=1.0)


# --- compute_expenditure: gating behaviour --------------------------------------


def _dates(start: str, n: int):
    d = date.fromisoformat(start)
    return [(d + timedelta(days=i)).isoformat() for i in range(n)]


def _complete_intake(dates, kcal=2100.0):
    return [{"date": d, "status": "complete", "kcal": kcal} for d in dates]


def _daily_weights(dates, start=195.0, step=0.0):
    return [{"date": d, "weight_lb": start + step * i} for i, d in enumerate(dates)]


def test_no_data_returns_null():
    r = compute_expenditure([], [])
    assert r.tdee is None
    assert "no intake or weight data" in r.tdee_null_reason


def test_too_few_complete_days_returns_null_with_reason():
    dates = _dates("2026-01-01", 28)
    intake = _complete_intake(dates[:5])  # only 5 complete days, need 14
    weight = _daily_weights(dates)
    r = compute_expenditure(intake, weight, end_date=dates[-1], min_days=14)
    assert r.tdee is None
    assert "5 complete day" in r.tdee_null_reason
    assert "at least 14" in r.tdee_null_reason


def test_too_few_weigh_ins_returns_null_even_with_enough_intake_days():
    dates = _dates("2026-01-01", 28)
    intake = _complete_intake(dates)  # every day logged
    weight = _daily_weights(dates[:1])  # only one weigh-in
    r = compute_expenditure(intake, weight, end_date=dates[-1], min_days=14)
    assert r.tdee is None
    assert "weigh-in" in r.tdee_null_reason


def test_short_weight_span_returns_null():
    dates = _dates("2026-01-01", 28)
    intake = _complete_intake(dates)
    # two weigh-ins, but only 3 days apart -- not enough to trust a rate
    weight = _daily_weights(dates[-3:])
    r = compute_expenditure(intake, weight, end_date=dates[-1], min_days=14)
    assert r.tdee is None
    assert "span only" in r.tdee_null_reason


def test_partial_and_unlogged_days_are_excluded_from_days_used():
    dates = _dates("2026-01-01", 28)
    intake = [
        {"date": d, "status": "complete", "kcal": 2000.0} if i % 2 == 0
        else {"date": d, "status": "partial", "kcal": 500.0}
        for i, d in enumerate(dates)
    ]
    weight = _daily_weights(dates)
    r = compute_expenditure(intake, weight, end_date=dates[-1], min_days=10)
    assert r.days_used == 14  # every other day out of 28


# --- compute_expenditure: sign convention ---------------------------------------


def test_losing_weight_means_tdee_exceeds_intake():
    """The sign-convention case a bug here would get backwards silently (see expenditure.py's
    worked-example comment). Losing weight on a given intake means TDEE > that intake.
    """
    dates = _dates("2026-01-01", 28)
    intake = _complete_intake(dates, kcal=2100.0)
    # loses 2 lb over the 28-day window
    weight = _daily_weights(dates, start=195.0, step=-2.0 / 27)
    r = compute_expenditure(intake, weight, end_date=dates[-1], min_days=14)
    assert r.tdee is not None
    assert r.tdee > 2100.0
    assert r.trend_lb_per_week < 0  # matches garmin-mcp's get_body_trend sign convention


def test_gaining_weight_means_tdee_below_intake():
    dates = _dates("2026-01-01", 28)
    intake = _complete_intake(dates, kcal=2800.0)
    weight = _daily_weights(dates, start=180.0, step=2.0 / 27)
    r = compute_expenditure(intake, weight, end_date=dates[-1], min_days=14)
    assert r.tdee is not None
    assert r.tdee < 2800.0
    assert r.trend_lb_per_week > 0


def test_stable_weight_means_tdee_equals_intake():
    dates = _dates("2026-01-01", 28)
    intake = _complete_intake(dates, kcal=2400.0)
    weight = _daily_weights(dates, start=190.0, step=0.0)
    r = compute_expenditure(intake, weight, end_date=dates[-1], min_days=14)
    assert r.tdee == pytest.approx(2400.0, abs=1.0)


def test_known_worked_example_from_module_docstring():
    """2 lb lost over 14 days at 3500 kcal/lb implies a 500 kcal/day deficit.

    Even at alpha=0.9 the EWMA trend doesn't exactly track a steadily-moving raw series --
    it settles into a small constant lag behind it -- so this allows a wider tolerance than
    the "500 kcal/day" arithmetic alone would suggest, rather than asserting an exactness the
    smoothing doesn't provide.
    """
    dates = _dates("2026-01-01", 14)
    intake = _complete_intake(dates, kcal=2000.0)
    weight = _daily_weights(dates, start=195.0, step=-2.0 / 13)
    r = compute_expenditure(
        intake, weight, end_date=dates[-1],
        window_days=14, min_days=14, kcal_per_lb=3500.0, alpha=0.9,
    )
    assert r.tdee == pytest.approx(2500.0, abs=50.0)


# --- confidence and coverage reporting ------------------------------------------


def test_confidence_is_high_for_dense_daily_data():
    dates = _dates("2026-01-01", 28)
    intake = _complete_intake(dates)
    weight = _daily_weights(dates)
    r = compute_expenditure(intake, weight, end_date=dates[-1], min_days=14)
    assert r.confidence == "high"


def test_confidence_degrades_with_sparse_intake_logging():
    dates = _dates("2026-01-01", 28)
    intake = [
        {"date": d, "status": "complete" if i % 3 == 0 else "unlogged", "kcal": 2100.0 if i % 3 == 0 else None}
        for i, d in enumerate(dates)
    ]
    weight = _daily_weights(dates)
    r = compute_expenditure(intake, weight, end_date=dates[-1], min_days=9)
    assert r.tdee is not None
    assert r.confidence in ("medium", "low")


def test_weight_coverage_reports_largest_gap():
    dates = _dates("2026-01-01", 28)
    intake = _complete_intake(dates)
    # last logged weight is dates[13]; next is dates[20] -- 7 calendar days apart
    weight = _daily_weights(dates[:14]) + _daily_weights(dates[20:], start=193.0)
    r = compute_expenditure(intake, weight, end_date=dates[-1], min_days=14)
    assert r.weight_coverage["largest_gap_days"] == 7


def test_as_dict_rounds_and_is_json_shaped():
    dates = _dates("2026-01-01", 28)
    intake = _complete_intake(dates)
    weight = _daily_weights(dates, step=-1.0 / 27)
    r = compute_expenditure(intake, weight, end_date=dates[-1], min_days=14)
    d = r.as_dict()
    assert set(d) == {
        "tdee", "tdee_null_reason", "confidence", "method", "days_used", "days_requested",
        "trend_weight_lb", "trend_lb_per_week", "kcal_per_lb_used",
        "avg_kcal_complete_days", "weight_coverage",
    }
    assert isinstance(d["tdee"], float)


# --- seeded regression test mirroring scripts/simulate.py ----------------------


def test_recovers_known_tdee_from_noisy_synthetic_data():
    """A permanent regression test for the M2 gate itself (see scripts/simulate.py for the
    full, documented version with sensitivity/responsiveness analysis). Fixed seeds, so this
    is deterministic -- if this ever starts failing, the engine's accuracy regressed.
    """
    true_tdee = 2600.0
    mean_intake, intake_sd = 2100.0, 150.0
    water_ar, water_sd = 0.55, 1.35
    errors = []

    # 40 trials, matching scripts/simulate.py's own default order of magnitude -- fewer
    # trials makes the mean-error assertion below noisy enough to fail by chance even when
    # the engine is correct (a 20-trial version of this test measured 153 vs a 150 bound).
    for seed in range(40):
        rng = random.Random(seed)
        d0 = date(2026, 1, 1)
        intake, weight = [], []
        true_weight, water = 195.0, 0.0
        for i in range(60):
            d = d0 + timedelta(days=i)
            actual = max(0.0, rng.gauss(mean_intake, intake_sd))
            true_weight += (actual - true_tdee) / DEFAULT_KCAL_PER_LB
            water = water_ar * water + rng.gauss(0.0, water_sd)
            weight.append({"date": d.isoformat(), "weight_lb": true_weight + water})
            status = "complete" if rng.random() >= 0.20 else "partial"
            logged = actual if status == "complete" else actual * 0.55
            intake.append({"date": d.isoformat(), "status": status, "kcal": logged})

        r = compute_expenditure(intake, weight, end_date=d0 + timedelta(days=59), alpha=DEFAULT_ALPHA)
        if r.tdee is not None:
            errors.append(abs(r.tdee - true_tdee))

    assert len(errors) >= 36  # almost every trial should produce an answer
    assert statistics.mean(errors) <= 150.0  # the same tolerance scripts/simulate.py gates on
