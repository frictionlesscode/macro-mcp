"""Inline SVG charts, built with plain string formatting.

No matplotlib, no charting library, no CDN. Claude's artifact sandbox blocks external
scripts, so anything library-based would have to inline hundreds of kilobytes of it; and a
base64 raster image is both larger on the wire and more expensive in an LLM context window
than the ~10-20 KB of markup a chart like this actually needs.

The server owns chart *design* deliberately (SPEC.md "Charting"): charts then look identical
every time instead of being re-invented per conversation, and the decisions that are easy to
get wrong -- unlogged days as gaps rather than zeros, suppressed trend lines on sparse data --
are made once, here.

Two things worth knowing if you edit this:

*Everything is escaped.* Food descriptions and notes reach tooltips, and an unescaped ``&``
or ``<`` silently corrupts the whole document rather than failing loudly.

*Colours are defined twice.* A ``<style>`` block with ``prefers-color-scheme`` keeps charts
legible on light and dark backgrounds. Hard-coded ``stroke="black"`` disappears in dark mode,
which is exactly the kind of bug nobody notices until the chart is on a phone at night.
"""

from __future__ import annotations

from datetime import date as Date
from typing import Any, Mapping, Sequence
from xml.sax.saxutils import escape

from .models import ValidationError

WIDTH = 720
HEIGHT = 280
PAD_LEFT = 52
PAD_RIGHT = 16
PAD_TOP = 28
PAD_BOTTOM = 34
#: dual_axis_chart's right margin -- wider than PAD_RIGHT so a second column of axis
#: labels (e.g. body-fat %) has room; PAD_RIGHT alone is sized for zero right-side labels.
PAD_RIGHT_DUAL = 44

_STYLE = """
  .bg { fill: #ffffff; }
  .grid { stroke: #e2e2e2; stroke-width: 1; }
  .axis-text { fill: #555555; font: 11px system-ui, sans-serif; }
  .title { fill: #1a1a1a; font: 600 13px system-ui, sans-serif; }
  .subtitle { fill: #666666; font: 11px system-ui, sans-serif; }
  .intake-line { stroke: #2563eb; stroke-width: 2; fill: none;
                 stroke-linejoin: round; stroke-linecap: round; }
  .intake-dot { fill: #2563eb; }
  .estimate-dot { fill: none; stroke: #888888; stroke-width: 1.5; stroke-dasharray: 2 1.5; }
  .bf-line { stroke: #ea580c; stroke-width: 2; fill: none;
             stroke-linejoin: round; stroke-linecap: round; }
  .bf-dot { fill: #ea580c; }
  .bf-estimate-dot { fill: none; stroke: #ea580c; stroke-width: 1.5; stroke-dasharray: 2 1.5; }
  .legend-left { fill: #2563eb; font: 11px system-ui, sans-serif; }
  .legend-right { fill: #ea580c; font: 11px system-ui, sans-serif; }
  .hover-guide { stroke: #999999; stroke-width: 1; stroke-dasharray: 3 2; display: none; }
  .photo-marker { fill: none; stroke: #a855f7; stroke-width: 1.5; }
  .target-line { stroke: #16a34a; stroke-width: 1.5; fill: none; stroke-dasharray: 4 3; }
  .target-band { fill: #16a34a; opacity: 0.10; }
  .bar-over { fill: #dc2626; }
  .bar-under { fill: #2563eb; }
  .bar-on { fill: #16a34a; }
  .zero-line { stroke: #999999; stroke-width: 1; }
  @media (prefers-color-scheme: dark) {
    .bg { fill: #1c1c1e; }
    .grid { stroke: #3a3a3c; }
    .axis-text { fill: #a1a1a6; }
    .title { fill: #f2f2f7; }
    .subtitle { fill: #a1a1a6; }
    .intake-line { stroke: #60a5fa; }
    .intake-dot { fill: #60a5fa; }
    .estimate-dot { stroke: #98989d; }
    .bf-line { stroke: #fb923c; }
    .bf-dot { fill: #fb923c; }
    .bf-estimate-dot { stroke: #fb923c; }
    .photo-marker { stroke: #c084fc; }
    .legend-left { fill: #60a5fa; }
    .legend-right { fill: #fb923c; }
    .hover-guide { stroke: #6e6e73; }
    .target-line { stroke: #4ade80; }
    .target-band { fill: #4ade80; opacity: 0.14; }
    .bar-over { fill: #f87171; }
    .bar-under { fill: #60a5fa; }
    .bar-on { fill: #4ade80; }
    .zero-line { stroke: #6e6e73; }
  }
"""

_LABELS = {
    "kcal": "Calories", "protein_g": "Protein (g)", "carb_g": "Carbs (g)",
    "fat_g": "Fat (g)", "fiber_g": "Fiber (g)",
}


def _nice_bounds(values: Sequence[float]) -> tuple[float, float]:
    """Axis bounds with headroom, snapped to a readable step.

    Guards the degenerate single-value case, where lo == hi would otherwise produce a
    zero-height plot area and a division by zero.
    """
    lo, hi = min(values), max(values)
    if lo == hi:
        pad = max(abs(lo) * 0.1, 1.0)
        lo, hi = lo - pad, hi + pad
    span = hi - lo
    step = 10 ** (len(str(int(span))) - 1) if span >= 1 else 0.1
    lo = (int(lo / step) - 1) * step if lo > 0 else lo - step
    hi = (int(hi / step) + 1) * step
    return lo, hi


def _short_date(iso_date: str) -> str:
    d = Date.fromisoformat(iso_date)
    return f"{d.month}/{d.day}"


def _tooltip(text: str) -> str:
    return f"<title>{escape(text)}</title>"


def line_chart(
    series: Sequence[Mapping[str, Any]],
    metric: str,
    title: str | None = None,
    subtitle: str | None = None,
) -> dict[str, Any]:
    """Intake over time with the target drawn as a reference line and tolerance band.

    ``series`` entries are ``{date, status, intake, target}`` as produced by
    ``trends.compute``. Entries with ``intake: None`` (unlogged or partial days) break the
    line rather than dropping it to zero -- a visual lie that would make a forgotten dinner
    look like a fast.
    """
    if not series:
        raise ValidationError("no points to plot")

    plotted = [p for p in series if p.get("intake") is not None]
    if not plotted:
        return {
            "svg": None,
            "svg_null_reason": "no days in this window have a logged total to plot",
            "points_plotted": 0,
        }

    values = [p["intake"] for p in plotted]
    values += [p["target"] for p in series if p.get("target") is not None]
    lo, hi = _nice_bounds(values)

    plot_w = WIDTH - PAD_LEFT - PAD_RIGHT
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM
    n = len(series)

    def x_of(i: int) -> float:
        return PAD_LEFT + (plot_w * i / max(n - 1, 1))

    def y_of(v: float) -> float:
        return PAD_TOP + plot_h * (1 - (v - lo) / (hi - lo))

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="100%" role="img" aria-label="{escape(title or _LABELS.get(metric, metric))}">',
        f"<style>{_STYLE}</style>",
        f'<rect class="bg" x="0" y="0" width="{WIDTH}" height="{HEIGHT}" rx="6"/>',
        f'<text class="title" x="{PAD_LEFT}" y="18">'
        f'{escape(title or _LABELS.get(metric, metric))}</text>',
    ]
    if subtitle:
        parts.append(f'<text class="subtitle" x="{WIDTH - PAD_RIGHT}" y="18" '
                     f'text-anchor="end">{escape(subtitle)}</text>')

    # horizontal gridlines + y labels
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        v = lo + (hi - lo) * frac
        y = y_of(v)
        parts.append(f'<line class="grid" x1="{PAD_LEFT}" y1="{y:.1f}" '
                     f'x2="{WIDTH - PAD_RIGHT}" y2="{y:.1f}"/>')
        parts.append(f'<text class="axis-text" x="{PAD_LEFT - 8}" y="{y + 4:.1f}" '
                     f'text-anchor="end">{v:.0f}</text>')

    # target band, drawn beneath the data so it reads as context rather than a series
    targets = [(i, p["target"]) for i, p in enumerate(series) if p.get("target") is not None]
    if targets:
        band = []
        for i, t in targets:
            band.append((x_of(i), y_of(t * 1.05), y_of(t * 0.95)))
        top = " ".join(f"{x:.1f},{y_hi:.1f}" for x, y_hi, _ in band)
        bottom = " ".join(f"{x:.1f},{y_lo:.1f}" for x, _, y_lo in reversed(band))
        parts.append(f'<polygon class="target-band" points="{top} {bottom}"/>')
        target_path = " ".join(
            f"{'M' if k == 0 else 'L'}{x_of(i):.1f},{y_of(t):.1f}"
            for k, (i, t) in enumerate(targets)
        )
        parts.append(f'<path class="target-line" d="{target_path}"/>')

    # intake line, broken across gaps
    path_cmds: list[str] = []
    pen_down = False
    for i, p in enumerate(series):
        if p.get("intake") is None:
            pen_down = False
            continue
        cmd = "L" if pen_down else "M"
        path_cmds.append(f"{cmd}{x_of(i):.1f},{y_of(p['intake']):.1f}")
        pen_down = True
    parts.append(f'<path class="intake-line" d="{" ".join(path_cmds)}"/>')

    # points, each carrying its own hover tooltip -- no JavaScript required
    for i, p in enumerate(series):
        if p.get("intake") is None:
            continue
        tip = f"{p['date']}: {p['intake']:.0f}"
        if p.get("target") is not None:
            delta = p["intake"] - p["target"]
            tip += f" (target {p['target']:.0f}, {delta:+.0f})"
        parts.append(
            f'<circle class="intake-dot" cx="{x_of(i):.1f}" cy="{y_of(p["intake"]):.1f}" '
            f'r="3">{_tooltip(tip)}</circle>'
        )

    # x labels, thinned so they never collide
    stride = max(1, n // 8)
    for i in range(0, n, stride):
        parts.append(
            f'<text class="axis-text" x="{x_of(i):.1f}" y="{HEIGHT - 12}" '
            f'text-anchor="middle">{_short_date(series[i]["date"])}</text>'
        )

    parts.append("</svg>")
    return {
        "svg": "".join(parts),
        "svg_null_reason": None,
        "points_plotted": len(plotted),
        "width": WIDTH,
        "height": HEIGHT,
    }


def point_series_chart(
    points: Sequence[Mapping[str, Any]],
    label: str,
    title: str | None = None,
    subtitle: str | None = None,
) -> dict[str, Any]:
    """A plain single-series line, no target line or band -- for series that have no
    stored target to compare against (weight, body-fat %), unlike the macro trend charts
    above. ``points`` are ``{date, value, estimated?}``; entries with ``value: None`` are
    dropped rather than breaking the line, since (unlike a day's macro intake) a missing
    weigh-in isn't a meaningful gap worth drawing attention to on its own.

    ``estimated: True`` (e.g. a body-fat reading with ``method="estimate"``, including a
    Claude-vision guess from a photo -- see macro-coach's SKILL.md) draws that point hollow
    and dashed instead of solid, and appends "(estimate)" to its tooltip. The line stays
    continuous either way; only the marker changes, so a rough guess never reads as a real
    measurement sitting in the same trend.
    """
    plotted = [p for p in points if p.get("value") is not None]
    if not plotted:
        return {
            "svg": None,
            "svg_null_reason": f"no {label.lower()} data in this window",
            "points_plotted": 0,
        }

    lo, hi = _nice_bounds([p["value"] for p in plotted])
    plot_w = WIDTH - PAD_LEFT - PAD_RIGHT
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM
    n = len(plotted)

    def x_of(i: int) -> float:
        return PAD_LEFT + (plot_w * i / max(n - 1, 1))

    def y_of(v: float) -> float:
        return PAD_TOP + plot_h * (1 - (v - lo) / (hi - lo))

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="100%" role="img" aria-label="{escape(title or label)}">',
        f"<style>{_STYLE}</style>",
        f'<rect class="bg" x="0" y="0" width="{WIDTH}" height="{HEIGHT}" rx="6"/>',
        f'<text class="title" x="{PAD_LEFT}" y="18">{escape(title or label)}</text>',
    ]
    if subtitle:
        parts.append(f'<text class="subtitle" x="{WIDTH - PAD_RIGHT}" y="18" '
                     f'text-anchor="end">{escape(subtitle)}</text>')

    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        v = lo + (hi - lo) * frac
        y = y_of(v)
        parts.append(f'<line class="grid" x1="{PAD_LEFT}" y1="{y:.1f}" '
                     f'x2="{WIDTH - PAD_RIGHT}" y2="{y:.1f}"/>')
        parts.append(f'<text class="axis-text" x="{PAD_LEFT - 8}" y="{y + 4:.1f}" '
                     f'text-anchor="end">{v:.1f}</text>')

    path_cmds = [
        f"{'M' if i == 0 else 'L'}{x_of(i):.1f},{y_of(p['value']):.1f}"
        for i, p in enumerate(plotted)
    ]
    parts.append(f'<path class="intake-line" d="{" ".join(path_cmds)}"/>')

    for i, p in enumerate(plotted):
        estimated = bool(p.get("estimated"))
        cls = "estimate-dot" if estimated else "intake-dot"
        suffix = " (estimate)" if estimated else ""
        parts.append(
            f'<circle class="{cls}" cx="{x_of(i):.1f}" cy="{y_of(p["value"]):.1f}" '
            f'r="3">{_tooltip(f"{p['date']}: {p['value']:.1f}{suffix}")}</circle>'
        )

    stride = max(1, n // 8)
    for i in range(0, n, stride):
        parts.append(
            f'<text class="axis-text" x="{x_of(i):.1f}" y="{HEIGHT - 12}" '
            f'text-anchor="middle">{_short_date(plotted[i]["date"])}</text>'
        )

    parts.append("</svg>")
    return {
        "svg": "".join(parts),
        "svg_null_reason": None,
        "points_plotted": len(plotted),
        "width": WIDTH,
        "height": HEIGHT,
    }


def dual_axis_chart(
    start: str,
    end: str,
    left: Mapping[str, float],
    left_label: str,
    right: Mapping[str, float],
    right_label: str,
    right_estimated: Mapping[str, bool] | None = None,
    photo_dates: Sequence[str] | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Two trend lines sharing one date-based x-axis, each with its own y-axis -- built for
    the dashboard's weight (left) + body-fat % (right) chart, but written generically.

    Unlike ``point_series_chart``, which spaces whatever non-null points exist evenly by
    index, ``left``/``right`` here are ``{date: value}`` maps: every calendar day from
    ``start`` to ``end`` (inclusive) is enumerated, the same "one slot per day, missing is a
    gap" convention ``line_chart`` uses for macro trends. That's what lets two series with
    different logging density (weight roughly daily, body fat sparse) land at *consistent*
    x-positions for the same date, which in turn is what makes hover-to-photo on the
    dashboard mean anything -- a pixel x-position has to map to one real date shared by both
    lines, not two different "the Nth thing I happened to log" positions.

    ``right_estimated`` flags dates whose right-axis value is a guess (``method="estimate"``,
    including a Claude-vision read from a shared photo -- see macro-coach's SKILL.md) rather
    than a measurement; those points draw hollow/dashed (``bf-estimate-dot``) instead of
    solid, same idea as ``point_series_chart``'s ``estimate-dot``.

    The gridlines are drawn once, at shared fractional height positions -- each axis's own
    numeric label is computed independently for that same y, so the two scales agree on
    *where* a gridline is without agreeing on what value it represents.

    The root ``<svg>`` carries ``data-start``/``data-end``/``data-pad-left``/``data-plot-w``/
    ``data-plot-top``/``data-plot-bottom`` attributes so dashboard.py's hover JS can invert a
    mouse x-coordinate back into a date, and draw a vertical guide line spanning the plot
    area, without the axis geometry being duplicated in Python and JavaScript separately.
    """
    right_estimated = right_estimated or {}
    start_d = Date.fromisoformat(start)
    end_d = Date.fromisoformat(end)
    n = (end_d - start_d).days + 1
    if n <= 0:
        raise ValidationError(f"end ({end}) must not be before start ({start})")
    dates = [Date.fromordinal(start_d.toordinal() + i).isoformat() for i in range(n)]

    left_vals = [left[d] for d in dates if left.get(d) is not None]
    right_vals = [right[d] for d in dates if right.get(d) is not None]
    if not left_vals and not right_vals:
        return {
            "svg": None,
            "svg_null_reason": f"no {left_label.lower()} or {right_label.lower()} "
                                f"data in this window",
            "points_plotted_left": 0,
            "points_plotted_right": 0,
        }

    plot_w = WIDTH - PAD_LEFT - PAD_RIGHT_DUAL
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM

    def x_of(i: int) -> float:
        return PAD_LEFT + (plot_w * i / max(n - 1, 1))

    def y_of(v: float, lo: float, hi: float) -> float:
        return PAD_TOP + plot_h * (1 - (v - lo) / (hi - lo))

    left_bounds = _nice_bounds(left_vals) if left_vals else None
    right_bounds = _nice_bounds(right_vals) if right_vals else None

    chart_title = title or f"{left_label} / {right_label}"
    parts: list[str] = [
        f'<svg id="trend-chart" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" width="100%" role="img" '
        f'aria-label="{escape(chart_title)}" data-start="{start}" data-end="{end}" '
        f'data-pad-left="{PAD_LEFT}" data-plot-w="{plot_w:.2f}" '
        f'data-plot-top="{PAD_TOP}" data-plot-bottom="{HEIGHT - PAD_BOTTOM}">',
        f"<style>{_STYLE}</style>",
        f'<rect class="bg" x="0" y="0" width="{WIDTH}" height="{HEIGHT}" rx="6"/>',
        f'<text class="title" x="{PAD_LEFT}" y="18">{escape(chart_title)}</text>',
        f'<text class="legend-left" x="{WIDTH - PAD_RIGHT_DUAL}" y="18" '
        f'text-anchor="end">— {escape(left_label)}</text>',
        f'<text class="legend-right" x="{WIDTH - PAD_RIGHT_DUAL}" y="30" '
        f'text-anchor="end">— {escape(right_label)}</text>',
    ]

    # shared gridlines at fractional heights; each axis labels the same line with its own value
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = PAD_TOP + plot_h * (1 - frac)
        parts.append(f'<line class="grid" x1="{PAD_LEFT}" y1="{y:.1f}" '
                     f'x2="{WIDTH - PAD_RIGHT_DUAL}" y2="{y:.1f}"/>')
        if left_bounds:
            v = left_bounds[0] + (left_bounds[1] - left_bounds[0]) * frac
            parts.append(f'<text class="axis-text" x="{PAD_LEFT - 8}" y="{y + 4:.1f}" '
                         f'text-anchor="end">{v:.0f}</text>')
        if right_bounds:
            v = right_bounds[0] + (right_bounds[1] - right_bounds[0]) * frac
            parts.append(f'<text class="axis-text" x="{WIDTH - PAD_RIGHT_DUAL + 6}" '
                         f'y="{y + 4:.1f}">{v:.1f}</text>')

    def draw_series(values: Mapping[str, float], bounds, line_cls: str, dot_cls: str,
                     estimate_dot_cls: str, estimated: Mapping[str, bool]) -> None:
        if not bounds:
            return
        lo, hi = bounds
        # Connects across missing days with a straight line rather than breaking -- days
        # without a reading are interpolated visually, not fabricated as data (no new point
        # is added, no value is invented; the line is just drawn between the two real ones).
        path_cmds: list[str] = []
        for i, d in enumerate(dates):
            v = values.get(d)
            if v is None:
                continue
            cmd = "L" if path_cmds else "M"
            path_cmds.append(f"{cmd}{x_of(i):.1f},{y_of(v, lo, hi):.1f}")
        parts.append(f'<path class="{line_cls}" d="{" ".join(path_cmds)}"/>')

        for i, d in enumerate(dates):
            v = values.get(d)
            if v is None:
                continue
            is_est = bool(estimated.get(d))
            cls = estimate_dot_cls if is_est else dot_cls
            suffix = " (estimate)" if is_est else ""
            tip = f"{d}: {v:.1f}{suffix}"
            parts.append(f'<circle class="{cls}" cx="{x_of(i):.1f}" cy="{y_of(v, lo, hi):.1f}" '
                         f'r="3">{_tooltip(tip)}</circle>')

    draw_series(left, left_bounds, "intake-line", "intake-dot", "estimate-dot", {})
    draw_series(right, right_bounds, "bf-line", "bf-dot", "bf-estimate-dot", right_estimated)

    # a photo on a date -- ring the weight *line* at that x, same as the eye would read it
    # off the chart, even on a day with no real weigh-in: the line itself already connects
    # across gaps (see draw_series above), so the marker interpolates along that same
    # straight segment rather than inventing a different rule that disagrees with what's
    # drawn. Days before/after every known point flat-extrapolate from the nearest edge
    # value -- still a real number on the line, never a fabricated one.
    if photo_dates:
        known = sorted(
            ((Date.fromisoformat(d) - start_d).days, v)
            for d, v in left.items() if v is not None
        )
        lo, hi = left_bounds or (0.0, 1.0)

        def interpolated_weight(qi: int) -> float | None:
            if not known:
                return None
            if qi <= known[0][0]:
                return known[0][1]
            if qi >= known[-1][0]:
                return known[-1][1]
            for (i0, v0), (i1, v1) in zip(known, known[1:]):
                if i0 <= qi <= i1:
                    if i1 == i0:
                        return v0
                    return v0 + (v1 - v0) * (qi - i0) / (i1 - i0)
            return None  # unreachable given the bounds checks above

        baseline_y = HEIGHT - PAD_BOTTOM
        for photo_day in photo_dates:
            i = (Date.fromisoformat(photo_day) - start_d).days
            if not (0 <= i < n):
                continue
            v = interpolated_weight(i)
            cy = y_of(v, lo, hi) if (v is not None and left_bounds) else baseline_y
            parts.append(
                f'<circle class="photo-marker" cx="{x_of(i):.1f}" cy="{cy:.1f}" '
                f'r="6">{_tooltip(f"{photo_day}: photo available")}</circle>'
            )

    stride = max(1, n // 8)
    for i in range(0, n, stride):
        parts.append(
            f'<text class="axis-text" x="{x_of(i):.1f}" y="{HEIGHT - 12}" '
            f'text-anchor="middle">{_short_date(dates[i])}</text>'
        )

    parts.append("</svg>")
    return {
        "svg": "".join(parts),
        "svg_null_reason": None,
        "points_plotted_left": len(left_vals),
        "points_plotted_right": len(right_vals),
        "width": WIDTH,
        "height": HEIGHT,
    }


def deviation_bars(
    series: Sequence[Mapping[str, Any]],
    metric: str,
    title: str | None = None,
    subtitle: str | None = None,
) -> dict[str, Any]:
    """Per-day deviation from target: bars above the line are over, below are under.

    Only days with both a logged total and a target appear -- a deviation is undefined
    otherwise, and inventing a zero would read as "hit it exactly".
    """
    pairs = [
        (p["date"], p["intake"] - p["target"])
        for p in series
        if p.get("intake") is not None and p.get("target") is not None
    ]
    if not pairs:
        return {
            "svg": None,
            "svg_null_reason": "no days in this window have both a logged total and a target",
            "points_plotted": 0,
        }

    deviations = [d for _, d in pairs]
    magnitude = max(abs(min(deviations)), abs(max(deviations)), 1.0)
    lo, hi = -magnitude * 1.15, magnitude * 1.15

    plot_w = WIDTH - PAD_LEFT - PAD_RIGHT
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM
    n = len(pairs)
    bar_w = max(2.0, min(18.0, plot_w / max(n, 1) * 0.65))

    def x_of(i: int) -> float:
        return PAD_LEFT + plot_w * (i + 0.5) / max(n, 1)

    def y_of(v: float) -> float:
        return PAD_TOP + plot_h * (1 - (v - lo) / (hi - lo))

    zero_y = y_of(0.0)
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="100%" role="img" aria-label="{escape(title or "Deviation from target")}">',
        f"<style>{_STYLE}</style>",
        f'<rect class="bg" x="0" y="0" width="{WIDTH}" height="{HEIGHT}" rx="6"/>',
        f'<text class="title" x="{PAD_LEFT}" y="18">'
        f'{escape(title or f"{_LABELS.get(metric, metric)} vs target")}</text>',
    ]
    if subtitle:
        parts.append(f'<text class="subtitle" x="{WIDTH - PAD_RIGHT}" y="18" '
                     f'text-anchor="end">{escape(subtitle)}</text>')

    for frac in (0.0, 0.5, 1.0):
        v = lo + (hi - lo) * frac
        y = y_of(v)
        parts.append(f'<line class="grid" x1="{PAD_LEFT}" y1="{y:.1f}" '
                     f'x2="{WIDTH - PAD_RIGHT}" y2="{y:.1f}"/>')
        parts.append(f'<text class="axis-text" x="{PAD_LEFT - 8}" y="{y + 4:.1f}" '
                     f'text-anchor="end">{v:+.0f}</text>')

    for i, (day, dev) in enumerate(pairs):
        band = magnitude * 0.05
        cls = "bar-on" if abs(dev) <= band else ("bar-over" if dev > 0 else "bar-under")
        y_top = y_of(max(dev, 0.0))
        height = abs(zero_y - y_of(dev))
        parts.append(
            f'<rect class="{cls}" x="{x_of(i) - bar_w / 2:.1f}" y="{y_top:.1f}" '
            f'width="{bar_w:.1f}" height="{max(height, 1.0):.1f}" rx="1">'
            f'{_tooltip(f"{day}: {dev:+.0f} vs target")}</rect>'
        )

    parts.append(f'<line class="zero-line" x1="{PAD_LEFT}" y1="{zero_y:.1f}" '
                 f'x2="{WIDTH - PAD_RIGHT}" y2="{zero_y:.1f}"/>')

    stride = max(1, n // 8)
    for i in range(0, n, stride):
        parts.append(
            f'<text class="axis-text" x="{x_of(i):.1f}" y="{HEIGHT - 12}" '
            f'text-anchor="middle">{_short_date(pairs[i][0])}</text>'
        )

    parts.append("</svg>")
    return {
        "svg": "".join(parts),
        "svg_null_reason": None,
        "points_plotted": len(pairs),
        "width": WIDTH,
        "height": HEIGHT,
    }
