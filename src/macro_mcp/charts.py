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

_STYLE = """
  .bg { fill: #ffffff; }
  .grid { stroke: #e2e2e2; stroke-width: 1; }
  .axis-text { fill: #555555; font: 11px system-ui, sans-serif; }
  .title { fill: #1a1a1a; font: 600 13px system-ui, sans-serif; }
  .subtitle { fill: #666666; font: 11px system-ui, sans-serif; }
  .intake-line { stroke: #2563eb; stroke-width: 2; fill: none;
                 stroke-linejoin: round; stroke-linecap: round; }
  .intake-dot { fill: #2563eb; }
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
    above. ``points`` are ``{date, value}``; entries with ``value: None`` are dropped
    rather than breaking the line, since (unlike a day's macro intake) a missing weigh-in
    isn't a meaningful gap worth drawing attention to on its own.
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
        parts.append(
            f'<circle class="intake-dot" cx="{x_of(i):.1f}" cy="{y_of(p["value"]):.1f}" '
            f'r="3">{_tooltip(f"{p['date']}: {p['value']:.1f}")}</circle>'
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
