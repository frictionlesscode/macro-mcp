"""Renders the /dashboard page: a combined weight + body-fat % trend chart, and an aligned
progress-photo viewer synced to it. server.py owns the route, the token gate, and the
separate /dashboard/photo image endpoint -- this module only builds HTML and answers "what
photos are there to show".

No JS framework, no CDN, no build step -- the same reasoning as charts.py: this has to run
self-hosted behind a Tailscale Funnel with nothing external to fetch. There is no auto-advancing
slideshow (removed on request -- the primary interaction is hovering the chart, not watching a
loop): the photo panel shows the most recent shot, prev/next scrub manually, and hovering the
chart drives the same <img> from cursor position, via the date-based x-axis geometry
charts.dual_axis_chart embeds as data-* attributes on its <svg>.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date as Date
from typing import Any
from xml.sax.saxutils import escape

from . import body, body_photos, charts
from .garmin_weight import GarminBridgeError, get_weight_points
from .models import PHOTO_ANGLES, today

_CSS = """
  :root { color-scheme: light dark; }
  body { font: 15px system-ui, sans-serif; margin: 0; padding: 16px;
         background: #ffffff; color: #1a1a1a; }
  @media (prefers-color-scheme: dark) { body { background: #1c1c1e; color: #f2f2f7; } }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .sub { color: #888; font-size: 13px; margin-bottom: 16px; }
  .stats { display: flex; gap: 24px; margin-bottom: 16px; flex-wrap: wrap; }
  .stat .n { font-size: 22px; font-weight: 600; }
  .stat .l { font-size: 12px; color: #888; }
  .chart-wrap { width: 80vw; max-width: 1400px; margin: 0 auto 4px; position: relative; }
  .chart-wrap svg { cursor: crosshair; }
  .chart-note { max-width: 1400px; margin: 0 auto 20px; font-size: 12px; color: #888; }
  .null { padding: 40px; text-align: center; color: #888; border: 1px dashed #999;
          border-radius: 6px; }
  nav a { margin-right: 10px; font-size: 13px; text-decoration: none; color: #2563eb; }
  nav a.active { font-weight: 600; text-decoration: underline; }
  @media (prefers-color-scheme: dark) { nav a { color: #60a5fa; } }
  .photo-panel { max-width: 480px; margin: 0 auto; }
  .photo-panel img { max-width: 100%; border-radius: 6px; background: #000;
                    display: block; }
  .controls { display: flex; align-items: center; justify-content: center; gap: 8px;
              margin-top: 8px; flex-wrap: wrap; font-size: 13px; }
  .controls button { font-size: 13px; padding: 4px 10px; cursor: pointer; }
  .frame-label { font-size: 13px; color: #888; margin-top: 4px; }
  .hover-tip { position: fixed; display: none; pointer-events: none; z-index: 10;
               background: rgba(20, 20, 22, 0.92); color: #f2f2f7; font-size: 12px;
               line-height: 1.5; padding: 6px 10px; border-radius: 5px; white-space: nowrap; }
"""


async def _weight_bodyfat_chart(
    conn: sqlite3.Connection, days: int, photo_dates: list[str]
) -> dict[str, Any]:
    """Fetches both series and renders them as one chart. Weight failing to load (garmin-mcp
    unreachable, no token configured, etc.) doesn't block body fat from rendering -- but the
    reason is carried back separately so the page can say why weight is missing rather than
    silently showing only half the chart with no explanation.
    """
    start = Date.fromordinal(today().toordinal() - days + 1)
    end = today()

    weight_error: str | None = None
    try:
        weight_points = await get_weight_points(days=days)
        weight_by_date = {p["date"]: p["weight_lb"] for p in weight_points}
    except GarminBridgeError as exc:
        weight_by_date = {}
        weight_error = str(exc)

    comp = body.get_body_comp(conn, days)
    bf_by_date = {p["date"]: p["percent_fat"] for p in comp["points"]}
    # method="estimate" covers both a typed guess and a Claude-vision read from a shared
    # photo (see macro-coach's SKILL.md) -- either way it's not a real measurement, so the
    # chart marks it distinctly rather than sitting indistinguishable next to scale readings.
    bf_estimated = {p["date"]: p["method"] == "estimate" for p in comp["points"]}

    # A wide window (e.g. "all") requested against a shorter real history would otherwise
    # plot mostly empty axis -- tighten the start bound to the earliest date actually present
    # across all three series rather than trusting `days` literally. Only ever moves start
    # later (toward `end`); a window narrower than the data is left alone.
    all_dates = list(weight_by_date) + list(bf_by_date) + photo_dates
    if all_dates:
        earliest = min(Date.fromisoformat(d) for d in all_dates)
        start = max(start, earliest)

    chart = charts.dual_axis_chart(
        start.isoformat(), end.isoformat(),
        weight_by_date, "Weight (lb)",
        bf_by_date, "Body fat %",
        right_estimated=bf_estimated,
        photo_dates=photo_dates,
    )
    return {
        "chart": chart,
        "weight_by_date": weight_by_date,
        "bf_by_date": bf_by_date,
        "weight_error": weight_error,
        "latest_bf": comp["latest"]["percent_fat"] if comp["latest"] else None,
    }


def _nav(angle: str, days: int, token: str) -> str:
    links = []
    for a in PHOTO_ANGLES:
        cls = ' class="active"' if a == angle else ""
        links.append(f'<a{cls} href="/dashboard?angle={a}&days={days}&token={token}">{a}</a>')
    for d in (30, 90, 180, 365, 3650):
        cls = ' class="active"' if d == days else ""
        label = "all" if d == 3650 else f"{d}d"
        links.append(f'<a{cls} href="/dashboard?angle={angle}&days={d}&token={token}">{label}</a>')
    return f"<nav>{''.join(links)}</nav>"


async def render_page(conn: sqlite3.Connection, angle: str, days: int, token: str) -> str:
    start = Date.fromordinal(today().toordinal() - days + 1)
    listing = body_photos.list_photos(conn, angle=angle, start=start.isoformat())
    photo_days = [p["day"] for p in listing["photos"]]
    unaligned_count = sum(1 for p in listing["photos"] if p["align_status"] != "ok")

    trend = await _weight_bodyfat_chart(conn, days, photo_days)
    chart = trend["chart"]

    stats = []
    if trend["latest_bf"] is not None:
        stats.append(f'<div class="stat"><div class="n">{trend["latest_bf"]:.1f}%</div>'
                      f'<div class="l">latest body fat</div></div>')
    stats.append(f'<div class="stat"><div class="n">{len(photo_days)}</div>'
                  f'<div class="l">{escape(angle)} photos in window</div></div>')

    if chart["svg"]:
        chart_html = f'<div class="chart-wrap">{chart["svg"]}</div>'
    else:
        chart_html = f'<div class="null">{escape(chart["svg_null_reason"])}</div>'
    if trend["weight_error"]:
        chart_html += f'<p class="chart-note">Weight unavailable: {escape(trend["weight_error"])}</p>'

    if photo_days:
        urls = [f"/dashboard/photo?day={d}&angle={angle}&token={token}" for d in photo_days]
        photo_panel = f"""
        <div class="photo-panel">
          <img id="slide" src="{urls[-1]}" alt="progress photo">
          <div class="frame-label" id="frame-label"></div>
          <div class="controls">
            <button type="button" id="prev">&larr; prev</button>
            <button type="button" id="next">next &rarr;</button>
          </div>
          {f'<div class="frame-label">{unaligned_count} of {len(photo_days)} unaligned '
           f'(no clear pose detected) -- shown as originally framed</div>' if unaligned_count else ''}
        </div>
        """
    else:
        urls = []
        photo_panel = (f'<div class="null">no {escape(angle)} photos stored in this window '
                        f'(see log_body_photo)</div>')

    script = f"""
    <div class="hover-tip" id="hover-tip"></div>
    <script>
      const photoDates = {json.dumps(photo_days)};
      const photoUrls = {json.dumps(urls)};
      const weightByDate = {json.dumps(trend["weight_by_date"])};
      const bfByDate = {json.dumps(trend["bf_by_date"])};

      let i = photoUrls.length - 1;
      const img = document.getElementById('slide');
      const label = document.getElementById('frame-label');

      function show(n) {{
        if (!photoUrls.length) return;
        i = ((n % photoUrls.length) + photoUrls.length) % photoUrls.length;
        img.src = photoUrls[i];
        label.textContent = photoDates[i] + '  (' + (i + 1) + ' / ' + photoUrls.length + ')';
      }}

      if (photoUrls.length) {{
        document.getElementById('prev').onclick = () => show(i - 1);
        document.getElementById('next').onclick = () => show(i + 1);
        show(i);
      }}

      // nearest date with a photo -- logging is sparser than daily, so an exact match is
      // the exception, not the rule; the label always says how far off it actually is.
      function nearestPhotoIndex(dateStr) {{
        if (!photoDates.length) return -1;
        const target = new Date(dateStr + 'T00:00:00').getTime();
        let best = 0, bestDiff = Infinity;
        for (let k = 0; k < photoDates.length; k++) {{
          const diff = Math.abs(new Date(photoDates[k] + 'T00:00:00').getTime() - target);
          if (diff < bestDiff) {{ bestDiff = diff; best = k; }}
        }}
        return best;
      }}

      const svg = document.getElementById('trend-chart');
      const tip = document.getElementById('hover-tip');
      if (svg) {{
        const startDate = new Date(svg.dataset.start + 'T00:00:00');
        const endDate = new Date(svg.dataset.end + 'T00:00:00');
        const totalDays = Math.round((endDate - startDate) / 86400000);
        const padLeft = parseFloat(svg.dataset.padLeft);
        const plotW = parseFloat(svg.dataset.plotW);
        const plotTop = svg.dataset.plotTop;
        const plotBottom = svg.dataset.plotBottom;
        const viewBoxW = svg.viewBox.baseVal.width;

        let guideLine = null;
        function guide() {{
          if (guideLine) return guideLine;
          guideLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
          guideLine.setAttribute('class', 'hover-guide');
          guideLine.setAttribute('y1', plotTop);
          guideLine.setAttribute('y2', plotBottom);
          svg.appendChild(guideLine);
          return guideLine;
        }}

        // resolves a client-space mouse x to the viewBox-space x (clamped to the plot area)
        // and the date it represents -- one source of truth so the guide line, the tooltip,
        // and the photo lookup can never disagree about what's under the cursor.
        function hoverInfoAtClientX(clientX) {{
          const rect = svg.getBoundingClientRect();
          const scale = viewBoxW / rect.width;
          const rawX = (clientX - rect.left) * scale;
          let frac = (rawX - padLeft) / plotW;
          frac = Math.max(0, Math.min(1, frac));
          const dayOffset = Math.round(frac * totalDays);
          const d = new Date(startDate.getTime() + dayOffset * 86400000);
          return {{ date: d.toISOString().slice(0, 10), x: padLeft + frac * plotW }};
        }}

        svg.addEventListener('mousemove', (e) => {{
          const {{ date: dateStr, x }} = hoverInfoAtClientX(e.clientX);

          const line = guide();
          line.setAttribute('x1', x);
          line.setAttribute('x2', x);
          line.style.display = 'block';

          const w = weightByDate[dateStr];
          const bf = bfByDate[dateStr];
          let html = dateStr;
          html += '<br>Weight: ' + (w !== undefined ? w.toFixed(1) + ' lb' : 'no reading');
          html += '<br>Body fat: ' + (bf !== undefined ? bf.toFixed(1) + '%' : 'no reading');
          tip.innerHTML = html;
          tip.style.display = 'block';
          tip.style.left = (e.clientX + 14) + 'px';
          tip.style.top = (e.clientY + 14) + 'px';

          if (photoDates.length) {{
            const idx = nearestPhotoIndex(dateStr);
            if (idx >= 0 && idx !== i) {{
              i = idx;
              img.src = photoUrls[i];
              const gapDays = Math.round(
                Math.abs(new Date(photoDates[i] + 'T00:00:00') - new Date(dateStr + 'T00:00:00'))
                / 86400000
              );
              label.textContent = gapDays === 0
                ? photoDates[i] + ' (' + (i + 1) + ' / ' + photoUrls.length + ')'
                : photoDates[i] + ' -- closest photo, ' + gapDays + 'd from hover';
            }}
          }}
        }});

        svg.addEventListener('mouseleave', () => {{
          tip.style.display = 'none';
          if (guideLine) guideLine.style.display = 'none';
        }});
      }}
    </script>
    """

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Body dashboard</title>
<style>{_CSS}</style>
</head><body>
<h1>Body dashboard</h1>
<div class="sub">last {days} days</div>
{_nav(angle, days, token)}
<div class="stats">{''.join(stats)}</div>
{chart_html}
{photo_panel}
{script}
</body></html>"""
