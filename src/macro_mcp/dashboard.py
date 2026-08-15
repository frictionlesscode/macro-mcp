"""Renders the /dashboard page: weight + body-fat % trend charts, and an aligned
progress-photo slideshow. server.py owns the route, the token gate, and the separate
/dashboard/photo image endpoint -- this module only builds HTML and answers "what photos
are there to show".

No JS framework, no CDN, no build step -- the same reasoning as charts.py: this has to run
self-hosted behind a Tailscale Funnel with nothing external to fetch. The slideshow is
~40 lines of vanilla JS that swaps one <img>'s src on a timer; the browser caches each
photo after its first load, so scrubbing back and forth is instant after the first pass.
"""

from __future__ import annotations

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
  .charts { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
  .charts > div { flex: 1 1 320px; max-width: 100%; }
  .null { padding: 40px; text-align: center; color: #888; border: 1px dashed #999;
          border-radius: 6px; }
  nav a { margin-right: 10px; font-size: 13px; text-decoration: none; color: #2563eb; }
  nav a.active { font-weight: 600; text-decoration: underline; }
  @media (prefers-color-scheme: dark) { nav a { color: #60a5fa; } }
  .slideshow { max-width: 480px; }
  .slideshow img { max-width: 100%; border-radius: 6px; background: #000;
                    display: block; }
  .controls { display: flex; align-items: center; gap: 8px; margin-top: 8px;
              flex-wrap: wrap; font-size: 13px; }
  .controls input[type=range] { width: 120px; }
  .controls button { font-size: 13px; padding: 4px 10px; cursor: pointer; }
  .frame-label { font-size: 13px; color: #888; margin-top: 4px; }
"""


async def _weight_chart(days: int) -> dict[str, Any]:
    try:
        points = await get_weight_points(days=days)
    except GarminBridgeError as exc:
        return {"svg": None, "svg_null_reason": str(exc)}
    series = [{"date": p["date"], "value": p["weight_lb"]} for p in points]
    return charts.point_series_chart(series, "Weight (lb)")


def _bodyfat_chart(conn: sqlite3.Connection, days: int) -> dict[str, Any]:
    result = body.get_body_comp(conn, days)
    series = [{"date": p["date"], "value": p["percent_fat"]} for p in result["points"]]
    return charts.point_series_chart(series, "Body fat %")


def _chart_block(chart: dict[str, Any]) -> str:
    if chart["svg"]:
        return chart["svg"]
    return f'<div class="null">{escape(chart["svg_null_reason"])}</div>'


def _nav(angle: str, days: int, token: str) -> str:
    links = []
    for a in PHOTO_ANGLES:
        cls = ' class="active"' if a == angle else ""
        links.append(f'<a{cls} href="/dashboard?angle={a}&days={days}&token={token}">{a}</a>')
    for d in (30, 90, 180, 365):
        cls = ' class="active"' if d == days else ""
        links.append(f'<a{cls} href="/dashboard?angle={angle}&days={d}&token={token}">{d}d</a>')
    return f"<nav>{''.join(links)}</nav>"


async def render_page(conn: sqlite3.Connection, angle: str, days: int, token: str) -> str:
    weight = await _weight_chart(days)
    bodyfat = _bodyfat_chart(conn, days)
    comp = body.get_body_comp(conn, days)

    start = Date.fromordinal(today().toordinal() - days + 1)
    listing = body_photos.list_photos(conn, angle=angle, start=start.isoformat())
    photo_days = [p["day"] for p in listing["photos"]]
    unaligned_count = sum(1 for p in listing["photos"] if p["align_status"] != "ok")

    latest_bf = comp["latest"]["percent_fat"] if comp["latest"] else None

    stats = []
    if latest_bf is not None:
        stats.append(f'<div class="stat"><div class="n">{latest_bf:.1f}%</div>'
                      f'<div class="l">latest body fat</div></div>')
    stats.append(f'<div class="stat"><div class="n">{len(photo_days)}</div>'
                  f'<div class="l">{escape(angle)} photos in window</div></div>')

    if photo_days:
        urls = [f"/dashboard/photo?day={d}&angle={angle}&token={token}" for d in photo_days]
        slideshow = f"""
        <div class="slideshow">
          <img id="slide" src="{urls[-1]}" alt="progress photo">
          <div class="frame-label" id="frame-label"></div>
          <div class="controls">
            <button type="button" id="prev">&larr; prev</button>
            <button type="button" id="playpause">pause</button>
            <button type="button" id="next">next &rarr;</button>
            <label>speed <input type="range" id="speed" min="200" max="3000" step="100"
                                 value="900"></label>
          </div>
          {f'<div class="frame-label">{unaligned_count} of {len(photo_days)} unaligned '
           f'(no clear pose detected) -- shown as originally framed</div>' if unaligned_count else ''}
        </div>
        <script>
          const urls = {urls!r};
          const dates = {photo_days!r};
          let i = urls.length - 1, playing = true, timer = null;
          const img = document.getElementById('slide');
          const label = document.getElementById('frame-label');
          const speed = document.getElementById('speed');
          function show(n) {{
            i = ((n % urls.length) + urls.length) % urls.length;
            img.src = urls[i];
            label.textContent = dates[i] + '  (' + (i + 1) + ' / ' + urls.length + ')';
          }}
          function tick() {{ show(i + 1); }}
          function restart() {{
            if (timer) clearInterval(timer);
            if (playing) timer = setInterval(tick, parseInt(speed.value, 10));
          }}
          document.getElementById('prev').onclick = () => {{ playing = false; restart();
            document.getElementById('playpause').textContent = 'play'; show(i - 1); }};
          document.getElementById('next').onclick = () => {{ playing = false; restart();
            document.getElementById('playpause').textContent = 'play'; show(i + 1); }};
          document.getElementById('playpause').onclick = (e) => {{
            playing = !playing; e.target.textContent = playing ? 'pause' : 'play'; restart();
          }};
          speed.oninput = restart;
          show(i);
          restart();
        </script>
        """
    else:
        slideshow = (f'<div class="null">no {escape(angle)} photos stored in this window '
                     f'(see log_body_photo)</div>')

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
<div class="charts">
  <div>{_chart_block(weight)}</div>
  <div>{_chart_block(bodyfat)}</div>
</div>
{slideshow}
</body></html>"""
