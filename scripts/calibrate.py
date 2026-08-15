#!/usr/bin/env python
"""M1 calibration harness — how wrong is the estimate, and in which direction?

Everything downstream rests on estimation accuracy. This server tracks intake against
whatever targets Claude sets and reports adherence -- there is no downstream mechanism that
compensates for a biased estimate the way an inferred expenditure figure once did. A
consistent 15% under-estimate here just means every adherence number is wrong by 15%, plainly
and permanently, until it's caught. This harness exists so that error is a measured number
with a sample size attached, not an impression.

Two conditions are measured separately, because they fail differently:

  mass_known    You weighed the food. The estimate only has to identify what it is and
                recall its composition. This is the owner's normal case.
  mass_unknown  Restaurant or someone else's cooking. The estimate must also guess portion
                mass, which is a different and usually larger error term.

Workflow
--------
1. Pick an item whose true macros you know (a label, or a weighed portion of a food with a
   label). Do not look at the label first.
2. Photograph it, ask Claude for macros, and write down what it said.
3. Record both, then read the label:

    python scripts/calibrate.py add --name "Chobani 0% 150g" --condition mass_known \\
        --truth 90,16,7,0,0 --est 110,18,8,1,0 --est-confidence high

    (--truth and --est are kcal,protein,carb,fat,fiber)

4. python scripts/calibrate.py report

Bias matters more than spread. Scatter with no bias averages out over enough logged days --
some meals overestimated, some under, roughly cancelling in any trend or adherence figure
computed over a window. A consistent bias does not cancel; it shifts every average and every
"days over target" count by the same fixed amount, quietly, for as long as it goes unnoticed.
The report separates the two because they call for different fixes.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

import argparse
import csv
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

MACROS = ("kcal", "protein_g", "carb_g", "fat_g", "fiber_g")
CONDITIONS = ("mass_known", "mass_unknown")

#: Below this many samples in a condition, summary statistics are reported but explicitly
#: labelled as not yet meaningful, rather than being withheld or presented as settled.
MIN_MEANINGFUL_N = 5

DEFAULT_PATH = "./data/calibration.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sample (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    name           TEXT NOT NULL,
    condition      TEXT NOT NULL CHECK (condition IN ('mass_known','mass_unknown')),
    est_confidence TEXT CHECK (est_confidence IN ('high','medium','low')),
    notes          TEXT,
    truth_kcal REAL NOT NULL, truth_protein_g REAL NOT NULL, truth_carb_g REAL NOT NULL,
    truth_fat_g REAL NOT NULL, truth_fiber_g REAL NOT NULL,
    est_kcal REAL NOT NULL, est_protein_g REAL NOT NULL, est_carb_g REAL NOT NULL,
    est_fat_g REAL NOT NULL, est_fiber_g REAL NOT NULL
);
"""


def open_cal(path: str | None) -> sqlite3.Connection:
    target = Path(path or DEFAULT_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def parse_macros(spec: str, label: str) -> list[float]:
    parts = [p.strip() for p in spec.split(",")]
    if len(parts) != 5:
        raise SystemExit(
            f"error: --{label} needs 5 comma-separated numbers "
            f"(kcal,protein,carb,fat,fiber); got {len(parts)}"
        )
    try:
        values = [float(p) for p in parts]
    except ValueError as exc:
        raise SystemExit(f"error: --{label} must be numeric ({exc})") from exc
    if any(v < 0 for v in values):
        raise SystemExit(f"error: --{label} values cannot be negative")
    return values


def pct_error(estimate: float, truth: float) -> float | None:
    """Signed percentage error. ``None`` when truth is zero — the ratio is undefined.

    Returning None rather than 0 or 100 keeps a genuinely undefined case out of the stats
    instead of quietly biasing them.
    """
    if truth == 0:
        return None
    return (estimate - truth) / truth * 100.0


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile. Small-n safe, unlike bucketing approaches."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def cmd_add(conn: sqlite3.Connection, args) -> None:
    truth = parse_macros(args.truth, "truth")
    est = parse_macros(args.est, "est")
    conn.execute(
        """INSERT INTO sample
           (created_at, name, condition, est_confidence, notes,
            truth_kcal, truth_protein_g, truth_carb_g, truth_fat_g, truth_fiber_g,
            est_kcal, est_protein_g, est_carb_g, est_fat_g, est_fiber_g)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            datetime.now(timezone.utc).isoformat(), args.name, args.condition,
            args.est_confidence, args.notes, *truth, *est,
        ),
    )
    err = pct_error(est[0], truth[0])
    shown = "undefined (truth is 0)" if err is None else f"{err:+.1f}%"
    print(f"recorded '{args.name}' [{args.condition}] - calorie error {shown}")


def cmd_list(conn: sqlite3.Connection, args) -> None:
    rows = conn.execute("SELECT * FROM sample ORDER BY id").fetchall()
    if not rows:
        print("no samples recorded yet")
        return
    print(f"{'id':>4}  {'condition':<12} {'name':<30} {'truth':>7} {'est':>7} {'err':>8}")
    for r in rows:
        err = pct_error(r["est_kcal"], r["truth_kcal"])
        err_s = "  n/a" if err is None else f"{err:+.1f}%"
        print(
            f"{r['id']:>4}  {r['condition']:<12} {r['name'][:30]:<30} "
            f"{r['truth_kcal']:>7.0f} {r['est_kcal']:>7.0f} {err_s:>8}"
        )


def summarise(errors: list[float]) -> dict[str, float]:
    absolute = [abs(e) for e in errors]
    return {
        "n": len(errors),
        "bias": statistics.mean(errors),
        "median_abs": statistics.median(absolute),
        "p90_abs": percentile(absolute, 0.90),
        "worst": max(absolute),
        "spread": statistics.pstdev(errors) if len(errors) > 1 else 0.0,
    }


def cmd_report(conn: sqlite3.Connection, args) -> None:
    rows = conn.execute("SELECT * FROM sample").fetchall()
    if not rows:
        print("no samples recorded yet - nothing to report")
        return

    print("\nEstimation error by condition")
    print("=" * 74)

    for condition in CONDITIONS:
        subset = [r for r in rows if r["condition"] == condition]
        print(f"\n{condition}  (n = {len(subset)})")
        print("-" * 74)
        if not subset:
            print("  no samples in this condition")
            continue
        if len(subset) < MIN_MEANINGFUL_N:
            print(
                f"  ! fewer than {MIN_MEANINGFUL_N} samples - figures below are "
                f"indicative only, not a calibration."
            )

        print(
            f"  {'macro':<10} {'n':>3} {'bias':>9} {'med|err|':>9} "
            f"{'p90|err|':>9} {'worst':>8} {'spread':>8}"
        )
        for macro in MACROS:
            errors = [
                e for e in (
                    pct_error(r[f"est_{macro}"], r[f"truth_{macro}"]) for r in subset
                ) if e is not None
            ]
            if not errors:
                print(f"  {macro:<10} {'-':>3}   no samples with non-zero truth value")
                continue
            s = summarise(errors)
            print(
                f"  {macro:<10} {s['n']:>3} {s['bias']:>+8.1f}% {s['median_abs']:>8.1f}% "
                f"{s['p90_abs']:>8.1f}% {s['worst']:>7.1f}% {s['spread']:>7.1f}%"
            )

        kcal_errors = [
            e for e in (pct_error(r["est_kcal"], r["truth_kcal"]) for r in subset)
            if e is not None
        ]
        if kcal_errors and len(subset) >= MIN_MEANINGFUL_N:
            bias = statistics.mean(kcal_errors)
            direction = "over" if bias > 0 else "under"
            print(
                f"\n  Calorie estimates run {abs(bias):.1f}% {direction} on average. "
                f"A consistent bias of this size is largely absorbed by the expenditure\n"
                f"  engine; a bias that drifts over time is not. Re-run this harness "
                f"periodically rather than treating it as settled."
            )

    print("\n" + "=" * 74)
    print(f"total samples: {len(rows)}")
    gate = all(
        len([r for r in rows if r["condition"] == c]) >= MIN_MEANINGFUL_N
        for c in CONDITIONS
    )
    print(
        "M1 gate: satisfied - both conditions have enough samples to report."
        if gate else
        f"M1 gate: NOT satisfied - each condition needs at least {MIN_MEANINGFUL_N} samples."
    )
    print()


def cmd_export(conn: sqlite3.Connection, args) -> None:
    rows = conn.execute("SELECT * FROM sample ORDER BY id").fetchall()
    if not rows:
        print("no samples to export")
        return
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(list(rows[0].keys()) + [f"err_{m}_pct" for m in MACROS])
        for r in rows:
            errors = [pct_error(r[f"est_{m}"], r[f"truth_{m}"]) for m in MACROS]
            writer.writerow(
                list(r) + ["" if e is None else round(e, 2) for e in errors]
            )
    print(f"exported {len(rows)} samples to {out}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="calibrate", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db", help=f"calibration DB path (default: {DEFAULT_PATH})")
    sub = p.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="record one estimate-vs-truth sample")
    add.add_argument("--name", required=True)
    add.add_argument("--condition", required=True, choices=list(CONDITIONS))
    add.add_argument("--truth", required=True, metavar="kcal,p,c,f,fiber")
    add.add_argument("--est", required=True, metavar="kcal,p,c,f,fiber")
    add.add_argument("--est-confidence", choices=["high", "medium", "low"])
    add.add_argument("--notes")
    add.set_defaults(func=cmd_add)

    lst = sub.add_parser("list", help="list recorded samples")
    lst.set_defaults(func=cmd_list)

    rep = sub.add_parser("report", help="error distribution by condition and macro")
    rep.set_defaults(func=cmd_report)

    exp = sub.add_parser("export", help="write samples and errors to CSV")
    exp.add_argument("--out", default="./data/calibration.csv")
    exp.set_defaults(func=cmd_export)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = open_cal(args.db)
    try:
        args.func(conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
