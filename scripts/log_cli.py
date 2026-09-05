#!/usr/bin/env python
"""M1 logging CLI — the interface the M1 gate is exercised through.

This exists to prove the logging path and gather calibration data before any MCP server,
auth, or Docker work. It is not the long-term interface; Claude is.

Examples
--------
    python scripts/log_cli.py save --name "Oats" --serving "40 g dry" --serving-g 40 \\
        --kcal 150 --protein 5 --carb 27 --fat 2.5 --fiber 4 --source label

    python scripts/log_cli.py log --meal breakfast --desc "oats and whey" \\
        --item "name=Oats,qty=80,unit=g,kcal=300,protein_g=10,carb_g=54,fat_g=5,fiber_g=8,source=label,confidence=high" \\
        --item "name=Whey,qty=30,unit=g,kcal=120,protein_g=24,carb_g=3,fat_g=1,source=label,confidence=high"

    python scripts/log_cli.py quick --food-id 1 --grams 80 --meal breakfast
    python scripts/log_cli.py day
    python scripts/log_cli.py status --complete
    python scripts/log_cli.py trend --days 14
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (path setup must run before macro_mcp imports)

import argparse
import json
import sys

from macro_mcp import foods
from macro_mcp.models import MACRO_FIELDS, FoodItem, ValidationError
from macro_mcp.store import open_db

NUMERIC_ITEM_FIELDS = set(MACRO_FIELDS) | {"qty"}


def parse_item(spec: str) -> FoodItem:
    """Parse ``key=value,key=value`` into a FoodItem.

    Chosen over positional fields because a mis-ordered ``carb_g``/``fat_g`` is silent and
    corrupts the calibration data this milestone exists to collect.
    """
    data: dict[str, object] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValidationError(f"malformed item field {chunk!r}; expected key=value")
        key, _, value = chunk.partition("=")
        key, value = key.strip(), value.strip()
        if key in NUMERIC_ITEM_FIELDS:
            try:
                data[key] = float(value)
            except ValueError as exc:
                raise ValidationError(f"{key} must be a number; got {value!r}") from exc
        else:
            data[key] = value
    return FoodItem.from_dict(data)


def fmt_macros(m: dict[str, float]) -> str:
    return (
        f"{m['kcal']:>7.0f} kcal   "
        f"P {m['protein_g']:>5.1f}  C {m['carb_g']:>5.1f}  "
        f"F {m['fat_g']:>5.1f}  fib {m['fiber_g']:>4.1f}"
    )


def print_day(day: dict) -> None:
    print(f"\n{day['date']}   [{day['status']}]")
    print("-" * 64)
    if not day["entries"]:
        print("  (nothing logged)")
    for entry in day["entries"]:
        tag = " (planned)" if entry["planned"] else ""
        label = entry["description"] or entry["meal"]
        print(f"  {entry['meal']:<10} {label}{tag}")
        for item in entry["items"]:
            qty = ""
            if item["qty"] is not None:
                qty = f"{item['qty']:g}{item['unit'] or ''}"
            print(
                f"      {item['name'][:24]:<24} {qty:>9}  "
                f"{fmt_macros(item['macros'])}  [{item['confidence']}]"
            )
        print(f"      {'':<24} {'':>9}  entry_id {entry['entry_id']}")
    print("-" * 64)
    print(f"  {'TOTAL':<24} {'':>9}  {fmt_macros(day['totals'])}")
    mix = day["confidence_mix"]
    print(
        f"  confidence: {mix['high']} high / {mix['medium']} medium / {mix['low']} low"
    )
    if day.get("planned_totals"):
        print(f"  {'PLANNED':<24} {'':>9}  {fmt_macros(day['planned_totals'])}")
    print(f"  targets: not available - {day['targets_null_reason']}\n")


def cmd_log(conn, args) -> None:
    items = [parse_item(spec) for spec in args.item]
    result = foods.log_food(
        conn,
        description=args.desc,
        meal=args.meal,
        items=items,
        when=args.when,
        planned=args.planned,
    )
    for warning in result.get("warnings", []):
        print(f"  ! {warning}", file=sys.stderr)
    print(f"logged {result['item_count']} item(s) as {result['entry_id']}")
    print_day(result)


def cmd_quick(conn, args) -> None:
    result = foods.log_from_library(
        conn,
        meal=args.meal,
        food_id=args.food_id,
        recipe_id=args.recipe_id,
        servings=args.servings,
        grams=args.grams,
        when=args.when,
        planned=args.planned,
    )
    print_day(result)


def cmd_day(conn, args) -> None:
    print_day(foods.get_day(conn, args.date))


def cmd_status(conn, args) -> None:
    status = "complete" if args.complete else ("partial" if args.partial else "unlogged")
    print_day(foods.set_day_status(conn, args.date, status, args.notes))


def cmd_save(conn, args) -> None:
    result = foods.save_food(
        conn,
        name=args.name,
        serving_desc=args.serving,
        serving_g=args.serving_g,
        kcal=args.kcal,
        protein_g=args.protein,
        carb_g=args.carb,
        fat_g=args.fat,
        fiber_g=args.fiber,
        brand=args.brand,
        barcode=args.barcode,
        source=args.source,
    )
    print(f"saved '{result['name']}' as id {result['id']}")


def cmd_search(conn, args) -> None:
    rows = foods.search_library(conn, args.query, args.limit)
    if not rows:
        print("no matches")
        return
    for r in rows:
        brand = f" [{r['brand']}]" if r["brand"] else ""
        print(
            f"  {r['id']:>4}  {r['name']}{brand}  ({r['serving_desc']})  "
            f"{fmt_macros(r['macros'])}  used {r['times_used']}x"
        )


def cmd_trend(conn, args) -> None:
    trend = foods.get_intake_trend(conn, args.days)
    for p in trend["points"]:
        if p["kcal"] is None:
            print(f"  {p['date']}  {'--':>7}        [{p['status']}]")
        else:
            print(f"  {p['date']}  {fmt_macros(p)}  [{p['status']}]")
    print(
        f"\n  {trend['days_complete']} complete / {trend['days_partial']} partial / "
        f"{trend['days_unlogged']} unlogged"
    )
    if trend["avg_kcal_complete_days"] is None:
        print(f"  mean intake: unavailable - {trend['avg_kcal_null_reason']}")
    else:
        print(
            f"  mean intake over complete days: "
            f"{trend['avg_kcal_complete_days']:.0f} kcal"
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="log_cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", help="SQLite path (default: $SQLITE_PATH or ./data/macro.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    log = sub.add_parser("log", help="log a meal from explicit items")
    log.add_argument("--meal", required=True,
                     choices=["breakfast", "lunch", "dinner", "snack", "other"])
    log.add_argument("--desc", help="what the meal was, in your words")
    log.add_argument("--item", action="append", required=True,
                     help="key=value,key=value (repeatable)")
    log.add_argument("--when", help="ISO timestamp; defaults to now")
    log.add_argument("--planned", action="store_true",
                     help="a meal you intend to eat, excluded from actual totals")
    log.set_defaults(func=cmd_log)

    quick = sub.add_parser("quick", help="log a saved food or recipe")
    quick.add_argument("--meal", required=True,
                       choices=["breakfast", "lunch", "dinner", "snack", "other"])
    quick.add_argument("--food-id", type=int)
    quick.add_argument("--recipe-id", type=int)
    quick.add_argument("--servings", type=float)
    quick.add_argument("--grams", type=float)
    quick.add_argument("--when")
    quick.add_argument("--planned", action="store_true")
    quick.set_defaults(func=cmd_quick)

    day = sub.add_parser("day", help="show a day")
    day.add_argument("--date", help="YYYY-MM-DD; defaults to today")
    day.set_defaults(func=cmd_day)

    status = sub.add_parser("status", help="mark how completely a day was logged")
    status.add_argument("--date")
    group = status.add_mutually_exclusive_group(required=True)
    group.add_argument("--complete", action="store_true")
    group.add_argument("--partial", action="store_true")
    group.add_argument("--unlogged", action="store_true")
    status.add_argument("--notes")
    status.set_defaults(func=cmd_status)

    save = sub.add_parser("save", help="save a food to the library")
    save.add_argument("--name", required=True)
    save.add_argument("--serving", required=True, help="e.g. '40 g dry' or '1 cup'")
    save.add_argument("--serving-g", type=float,
                      help="mass of one serving; required to log this food by grams")
    save.add_argument("--kcal", type=float, required=True)
    save.add_argument("--protein", type=float, required=True)
    save.add_argument("--carb", type=float, required=True)
    save.add_argument("--fat", type=float, required=True)
    save.add_argument("--fiber", type=float, default=0.0)
    save.add_argument("--brand")
    save.add_argument("--barcode")
    save.add_argument("--source", default="label",
                      choices=["label", "barcode", "library", "estimate"])
    save.set_defaults(func=cmd_save)

    search = sub.add_parser("search", help="search the library")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--limit", type=int, default=10)
    search.set_defaults(func=cmd_search)

    trend = sub.add_parser("trend", help="daily intake over a window")
    trend.add_argument("--days", type=int, default=14)
    trend.set_defaults(func=cmd_trend)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = open_db(args.db)
    try:
        args.func(conn, args)
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
