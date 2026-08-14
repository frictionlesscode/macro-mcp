#!/usr/bin/env python
"""M2 gate: does the expenditure engine recover a known TDEE from noisy synthetic data?

This is the actual gate SPEC.md requires before M2 is considered done -- not a demonstration,
a check with a pass/fail. It generates a synthetic person with a fixed, known TDEE, gives
them realistic day-to-day intake noise, realistic water-weight noise, and realistic missed
logging days, then asks whether `expenditure.compute_expenditure` can recover the true TDEE
from what a real user's data would actually look like.

The water-noise magnitude is not arbitrary: it's calibrated to a realistic ~6 lb swing across three consecutive days -- the kind of move that is obviously water, not tissue. If this harness used gentler synthetic
noise than the real account produces, a passing gate would be meaningless.

Usage
-----
    python scripts/simulate.py                 # run the full gate: recovery + sparse + sensitivity
    python scripts/simulate.py --trials 100     # more trials, tighter confidence interval
    python scripts/simulate.py --quiet          # print only the final gate verdict
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

import argparse
import random
import statistics
from datetime import date, timedelta

from macro_mcp.expenditure import (
    DEFAULT_ALPHA,
    DEFAULT_KCAL_PER_LB,
    DEFAULT_MIN_DAYS,
    DEFAULT_WINDOW_DAYS,
    compute_expenditure,
)

# --- ground truth for the simulated person -----------------------------------

TRUE_TDEE = 2600.0
MEAN_INTAKE = 2100.0          # -500 kcal/day average deficit -> -1 lb/week true fat loss
INTAKE_SD = 150.0             # day-to-day intake variation around that mean

# AR(1) water-weight process. Calibrated so a 3-day window reproduces swings on the order of
# a realistic ~6 lb / 72h swing, not an idealized smaller one.
WATER_AR_COEF = 0.55
WATER_SD = 1.35

WEIGHIN_SKIP_RATE = 0.08      # occasional missed weigh-in, exercising the gap-handling code
PARTIAL_DAY_RATE = 0.20       # fraction of days logged only partially -- excluded from the fit
PARTIAL_LOG_FRACTION = 0.55   # a partial day's *logged* kcal, as a fraction of what was truly eaten

SIM_DAYS = 60                 # warmup + the 28-day evaluation window, with room to spare
START_WEIGHT = 195.0          # a representative starting weight, for realism

TOLERANCE_KCAL = 150.0        # ~6% of TRUE_TDEE -- the M2 gate threshold
SPARSE_DAYS = 20              # short series used to prove the null-gate actually fires

CANDIDATE_ALPHAS = (0.05, 0.10, 0.15, 0.20, 0.30, 0.45)


def simulate_series(rng: random.Random, days: int = SIM_DAYS):
    """One synthetic person's intake and weight history."""
    d0 = date(2026, 1, 1)
    intake_points = []
    weight_points = []
    true_weight = START_WEIGHT
    water = 0.0

    for i in range(days):
        d = d0 + timedelta(days=i)
        actual_intake = max(0.0, rng.gauss(MEAN_INTAKE, INTAKE_SD))

        # Physiology doesn't care whether the day was logged -- weight responds to what was
        # actually eaten, at the *true* TDEE, every day.
        true_weight += (actual_intake - TRUE_TDEE) / DEFAULT_KCAL_PER_LB
        water = WATER_AR_COEF * water + rng.gauss(0.0, WATER_SD)

        if rng.random() >= WEIGHIN_SKIP_RATE:
            weight_points.append({"date": d.isoformat(), "weight_lb": true_weight + water})

        if rng.random() < PARTIAL_DAY_RATE:
            status, logged_kcal = "partial", actual_intake * PARTIAL_LOG_FRACTION
        else:
            status, logged_kcal = "complete", actual_intake
        intake_points.append({"date": d.isoformat(), "status": status, "kcal": logged_kcal})

    return intake_points, weight_points, d0 + timedelta(days=days - 1)


def run_trial(seed: int, alpha: float = DEFAULT_ALPHA) -> dict:
    rng = random.Random(seed)
    intake, weight, end = simulate_series(rng)
    result = compute_expenditure(
        intake, weight, end_date=end,
        window_days=DEFAULT_WINDOW_DAYS, min_days=DEFAULT_MIN_DAYS,
        alpha=alpha, kcal_per_lb=DEFAULT_KCAL_PER_LB,
    )
    naive = statistics.mean(
        p["kcal"] for p in intake if p["status"] == "complete"
    )
    return {"result": result, "naive_mean_intake": naive}


def check_sparse_data_returns_null(quiet: bool) -> bool:
    """A ~20-day series won't clear the 28-day-window/14-day-minimum bar. Must return None."""
    rng = random.Random(12345)
    intake, weight, end = simulate_series(rng, days=SPARSE_DAYS)
    result = compute_expenditure(
        intake, weight, end_date=end,
        window_days=DEFAULT_WINDOW_DAYS, min_days=DEFAULT_MIN_DAYS,
    )
    ok = result.tdee is None and bool(result.tdee_null_reason)
    if not quiet:
        print(f"sparse-data check ({SPARSE_DAYS} days): "
              f"tdee={result.tdee}  reason={result.tdee_null_reason!r}")
        print(f"  {'PASS' if ok else 'FAIL'}\n")
    return ok


def check_recovery(trials: int, quiet: bool) -> tuple[bool, float]:
    """Across many independent noisy synthetic people, does the mean estimate land near truth?"""
    errors = []
    confidences = []
    for seed in range(trials):
        outcome = run_trial(seed, alpha=DEFAULT_ALPHA)
        r = outcome["result"]
        if r.tdee is not None:
            errors.append(r.tdee - TRUE_TDEE)
            confidences.append(r.confidence)

    answered = len(errors)
    mean_abs_error = statistics.mean(abs(e) for e in errors) if errors else float("inf")
    bias = statistics.mean(errors) if errors else float("nan")
    naive_error = abs(MEAN_INTAKE - TRUE_TDEE)  # what you'd get ignoring weight change entirely

    if not quiet:
        print(f"recovery check ({trials} synthetic trials, true TDEE = {TRUE_TDEE:.0f} kcal)")
        print("-" * 64)
        print(f"  answered:        {answered}/{trials}")
        print(f"  mean abs error:  {mean_abs_error:.1f} kcal  (tolerance: {TOLERANCE_KCAL:.0f})")
        print(f"  bias:            {bias:+.1f} kcal")
        if confidences:
            for tier in ("high", "medium", "low"):
                n = confidences.count(tier)
                if n:
                    print(f"  confidence={tier:<7} {n}/{answered}")
        print(f"  (naive 'just use mean intake as TDEE' would be off by "
              f"{naive_error:.0f} kcal -- the weight-trend correction is doing the work)")
        print(f"  {'PASS' if mean_abs_error <= TOLERANCE_KCAL else 'FAIL'}\n")

    return mean_abs_error <= TOLERANCE_KCAL, mean_abs_error


def sensitivity_sweep(trials: int, quiet: bool) -> None:
    """How much does the smoothing constant matter? Required by SPEC.md's M2 gate --
    'the smoothing constant's sensitivity is documented, not just chosen.'
    """
    if quiet:
        return
    print(f"alpha sensitivity ({trials} trials per value)")
    print("-" * 64)
    print(f"  {'alpha':>6}  {'answered':>9}  {'mean|err|':>10}  {'bias':>8}")
    best_alpha, best_error = None, float("inf")
    for alpha in CANDIDATE_ALPHAS:
        errors = []
        for seed in range(trials):
            r = run_trial(seed, alpha=alpha)["result"]
            if r.tdee is not None:
                errors.append(r.tdee - TRUE_TDEE)
        if errors:
            mae = statistics.mean(abs(e) for e in errors)
            bias = statistics.mean(errors)
            print(f"  {alpha:>6.2f}  {len(errors):>9}  {mae:>9.1f}k  {bias:>+7.1f}k")
            if mae < best_error:
                best_alpha, best_error = alpha, mae
        else:
            print(f"  {alpha:>6.2f}  {0:>9}  {'--':>10}  {'--':>8}")
    print(f"\n  lowest mean|err| at alpha={best_alpha} ({best_error:.1f} kcal); "
          f"current default is {DEFAULT_ALPHA}.")
    if best_alpha != DEFAULT_ALPHA:
        print(f"  this synthetic sweep alone is not sufficient grounds to change the "
              f"default -- it reflects one noise model, not real-world logging "
              f"behavior. Worth re-running this harness once real data exists (M7).")
    print()


def simulate_step_change(rng: random.Random, step_day: int, tdee_before: float, tdee_after: float,
                         days: int = SIM_DAYS):
    """Like simulate_series, but true TDEE steps from tdee_before to tdee_after at step_day.

    Exists to answer the question the noise-only sensitivity sweep structurally cannot: a
    test where TDEE never changes will always reward heavier smoothing, since lag is never
    penalized. A real person's TDEE does change (training load shifts, a genuine metabolic
    adaptation, a deliberate refeed), and the smoothing constant that best rejects water-weight
    noise is not automatically the one that tracks a real change fast enough to matter.
    """
    d0 = date(2026, 1, 1)
    intake_points, weight_points = [], []
    true_weight, water = START_WEIGHT, 0.0
    for i in range(days):
        d = d0 + timedelta(days=i)
        true_tdee = tdee_before if i < step_day else tdee_after
        actual_intake = max(0.0, rng.gauss(MEAN_INTAKE, INTAKE_SD))
        true_weight += (actual_intake - true_tdee) / DEFAULT_KCAL_PER_LB
        water = WATER_AR_COEF * water + rng.gauss(0.0, WATER_SD)
        if rng.random() >= WEIGHIN_SKIP_RATE:
            weight_points.append({"date": d.isoformat(), "weight_lb": true_weight + water})
        if rng.random() < PARTIAL_DAY_RATE:
            status, logged_kcal = "partial", actual_intake * PARTIAL_LOG_FRACTION
        else:
            status, logged_kcal = "complete", actual_intake
        intake_points.append({"date": d.isoformat(), "status": status, "kcal": logged_kcal})
    return intake_points, weight_points, d0


def check_responsiveness(trials: int, quiet: bool) -> None:
    """How many days after a genuine TDEE step-change does each alpha reconverge?

    Complements sensitivity_sweep, which only measures noise-rejection on a *constant* true
    TDEE and therefore always favors smaller alpha. This measures the cost of that choice.
    """
    if quiet:
        return
    step_day = 35
    tdee_before, tdee_after = 2600.0, 2200.0  # a real, sustained 400 kcal/day drop
    reconverge_threshold = TOLERANCE_KCAL
    probe_alphas = (0.05, DEFAULT_ALPHA, 0.30)

    print(f"responsiveness after a step change (TDEE {tdee_before:.0f} -> {tdee_after:.0f} "
          f"kcal at day {step_day}, {trials} trials per alpha)")
    print("-" * 64)
    print(f"  {'alpha':>6}  {'median days to reconverge':>26}  {'never (of ' + str(trials) + ')':>16}")

    for alpha in probe_alphas:
        days_to_reconverge = []
        never = 0
        for seed in range(trials):
            rng = random.Random(1000 + seed)
            intake, weight, d0 = simulate_step_change(rng, step_day, tdee_before, tdee_after)
            converged_at = None
            # Probe every few days after the step, and require the estimate to land within
            # tolerance on two consecutive probes before calling it converged. First-passage
            # (any single hit) turned out to be measuring noise, not convergence: a noisy
            # trajectory drifting from ~2600 down to ~2200 passes *through* the tolerance band
            # by chance well before it actually settles there, and low alpha -- being noisier
            # per-estimate even though it's less biased -- did this disproportionately often at
            # offset 0, which is a physical impossibility (the window at the moment of the step
            # is still almost entirely pre-change data).
            prev_hit = False
            for offset in range(step_day, SIM_DAYS, 2):
                end = d0 + timedelta(days=offset)
                r = compute_expenditure(
                    intake, weight, end_date=end,
                    window_days=DEFAULT_WINDOW_DAYS, min_days=DEFAULT_MIN_DAYS, alpha=alpha,
                )
                hit = r.tdee is not None and abs(r.tdee - tdee_after) <= reconverge_threshold
                if hit and prev_hit:
                    converged_at = offset - step_day
                    break
                prev_hit = hit
            if converged_at is None:
                never += 1
            else:
                days_to_reconverge.append(converged_at)

        median = statistics.median(days_to_reconverge) if days_to_reconverge else float("nan")
        print(f"  {alpha:>6.2f}  {median:>26.0f}  {never:>16}")

    print(f"\n  this is the other half of the tradeoff the sensitivity sweep above leaves out:\n"
          f"  smaller alpha rejects more noise but takes longer to admit a real change happened.\n"
          f"  the current default ({DEFAULT_ALPHA}) is a judgment call between the two, not a\n"
          f"  value either check alone would select.\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="simulate", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--trials", type=int, default=50, help="synthetic people per check")
    p.add_argument("--quiet", action="store_true", help="print only the final verdict")
    args = p.parse_args(argv)

    sparse_ok = check_sparse_data_returns_null(args.quiet)
    recovery_ok, mae = check_recovery(args.trials, args.quiet)
    sensitivity_sweep(args.trials, args.quiet)
    check_responsiveness(max(args.trials // 2, 10), args.quiet)

    gate_ok = sparse_ok and recovery_ok
    print(f"M2 gate: {'satisfied' if gate_ok else 'NOT satisfied'} "
          f"(sparse-data null: {'ok' if sparse_ok else 'FAILED'}; "
          f"recovery mean|err|={mae:.1f} kcal vs {TOLERANCE_KCAL:.0f} tolerance)")
    return 0 if gate_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
