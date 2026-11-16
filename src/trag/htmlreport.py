"""A self-contained HTML report: one file, no external assets, safe to attach
to a ticket.  It shows the same findings as ``trag summary``; every value
that comes from the log goes through html.escape, because a log line can
contain anything, including markup.
"""
from __future__ import annotations

from html import escape
from typing import Any, Dict, List, Optional

from .analyze import Analysis
from .drain import Template
from .parse import fmt_time
from .text import bucket_label

W, H = 960, 250                       # chart viewBox
LEFT, RIGHT, TOP, BOTTOM = 44, 8, 12, 26
LEVEL_NAMES = {"fatal": "Fatal", "error": "Error", "warn": "Warning", "info": "Info",
               "debug": "Debug"}


def render(a: Analysis, reader) -> str:
    onset = a.error_onset()
    parts: List[str] = [_HEAD]
    parts.append("<h1>Log report</h1><p class='sub'>%s</p>" % escape(", ".join(reader.paths)))

    errors = a.levels["error"] + a.levels["fatal"]
    tiles = [("Events", _n(a.events)), ("Errors", _n(errors)),
             ("Warnings", _n(a.levels["warn"])), ("Patterns", _n(len(a.drain.templates)))]
    if a.first_ts is not None:
        tiles.append(("Period (UTC)", "%s &ndash; %s" % (fmt_time(a.first_ts), fmt_time(a.last_ts))))
    parts.append("<div class='tiles'>%s</div>" % "".join(
        "<div><span>%s</span><b>%s</b></div>" % t for t in tiles))

    timeline = a.timeline()
    if timeline.get("total"):
        parts.append("<h2>Events per %s</h2>" % bucket_label(a.bucket))
        parts.append(_chart(a, timeline, onset))

    if onset:
        parts.append("<h2>When it started</h2><p>Errors left their normal range at <b>%s</b> and "
                     "returned to it at <b>%s</b>: %s errors, peaking at %d per %s against a "
                     "normal %g.</p>" % (fmt_time(onset["start"]), fmt_time(onset["end"]),
                                         _n(onset["errors"]), onset["peak"],
                                         bucket_label(a.bucket), onset["normal"]))
        before = a.before_onset(onset)
        if before:
            parts.append("<h2>Rare events in the 10 minutes before</h2>"
                         "<p class='sub'>Deploys, restarts and config changes are logged once, "
                         "not thousands of times.</p>")
            parts.append(_table(["Time", "Line", "Message"], [
                [fmt_time(o["ts"]), str(o["line"]), "<code>%s</code>" % escape(o["message"])]
                for o in before[:12]], numeric=(1,)))

    fresh = a.new_templates()
    if fresh:
        parts.append("<h2>New patterns</h2><p class='sub'>Never seen during the first quarter "
                     "of the log, in order of first appearance.</p>")
        parts.append(_patterns(a, fresh[:20], first=True))

    top = a.top_templates(10, errors_only=True)
    if top:
        parts.append("<h2>Top error patterns</h2>")
        parts.append(_patterns(a, top))

    spikes = a.spikes()
    if spikes:
        parts.append("<h2>Existing patterns that got louder</h2>")
        parts.append(_table(["Pattern", "Peak", "Normal", "At", "Activity"], [
            ["<code>%s</code>" % escape(s["template"].text),
             "%d per %s" % (s["peak"], bucket_label(a.bucket)), "%g" % s["normal"],
             fmt_time(s["at"]), _spark(a.template_series(s["template"]))]
            for s in spikes[:8]], numeric=(1, 2)))

    if a.exceptions:
        parts.append("<h2>Exceptions in stack traces</h2>")
        parts.append(_table(["Exception", "Count"], [
            ["<code>%s</code>" % escape(name), _n(count)]
            for name, count in a.exceptions.most_common(8)], numeric=(1,)))

    requests = a.requests()
    if requests:
        parts.append("<h2>Requests</h2><p>%s</p>" % " &middot; ".join(
            "%s: <b>%s</b>" % (k, _n(v)) for k, v in requests["classes"].items()))
        if requests["failing"]:
            parts.append(_table(["Failing path", "5xx", "Requests", "Error rate"], [
                ["<code>%s</code>" % escape(r["path"]), _n(r["errors"]), _n(r["count"]),
                 "%.1f%%" % r["error_pct"]] for r in requests["failing"]], numeric=(1, 2, 3)))
        if requests["slowest"]:
            parts.append(_table(["Slowest path", "p95 seconds", "Requests"], [
                ["<code>%s</code>" % escape(r["path"]), "%.3f" % r["p95"], _n(r["count"])]
                for r in requests["slowest"]], numeric=(1, 2)))

    everything = sorted(a.drain.templates, key=lambda t: -t.count)
    parts.append("<details><summary>All %d patterns</summary>%s</details>" % (
        len(everything), _patterns(a, everything[:300])))
    parts.append(_FOOT)
    return "".join(parts)


# --------------------------------------------------------------------------
# pieces


def _chart(a: Analysis, timeline: Dict[str, List[int]], onset: Optional[Dict[str, Any]]) -> str:
    n = len(timeline["total"])
    top = _nice(max(timeline["total"]))
    plot_w, plot_h = W - LEFT - RIGHT, H - TOP - BOTTOM
    slot = plot_w / float(n)
    bar = max(1.0, slot - (2.0 if slot >= 5 else 1.0))

    def y(value: float) -> float:
        return TOP + plot_h - value / top * plot_h

    svg = ['<svg viewBox="0 0 %d %d" role="img" aria-label="Events per %s, split into errors, '
           'warnings and other">' % (W, H, bucket_label(a.bucket))]
    if onset:  # shade the period the error rate was abnormal
        x0 = LEFT + onset["first_bucket"] * slot
        x1 = LEFT + (onset["last_bucket"] + 1) * slot
        svg.append('<rect class="band" x="%.1f" y="%d" width="%.1f" height="%d"/>' % (
            x0, TOP, x1 - x0, plot_h))
        svg.append('<text class="tick" x="%.1f" y="%d">errors above normal</text>' % (x0 + 4, TOP + 12))
    for i in range(5):  # horizontal gridlines with value labels
        value = top * i / 4.0
        svg.append('<line class="grid" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>' % (
            LEFT, W - RIGHT, y(value), y(value)))
        svg.append('<text class="tick" x="%d" y="%.1f" text-anchor="end">%s</text>' % (
            LEFT - 6, y(value) + 4, _n(int(value))))
    for i in range(n):
        x = LEFT + i * slot + (slot - bar) / 2.0
        base = 0
        # errors sit on the baseline so their heights can be compared directly
        for name in ("error", "warn", "other"):
            value = timeline[name][i]
            if value:
                svg.append('<rect class="%s" x="%.1f" y="%.1f" width="%.1f" height="%.1f"/>' % (
                    name, x, y(base + value), bar, max(0.5, y(base) - y(base + value) - 0.6)))
            base += value
        tip = "%s · %d errors, %d warnings, %d other" % (
            fmt_time(a.bucket_start(i)), timeline["error"][i], timeline["warn"][i],
            timeline["other"][i])
        svg.append('<rect class="hit" x="%.1f" y="%d" width="%.1f" height="%d" data-tip="%s"/>' % (
            LEFT + i * slot, TOP, slot, plot_h, escape(tip)))
    for i in range(0, n, max(1, n // 6)):  # time labels
        if LEFT + i * slot > W - RIGHT - 52:
            break  # a label this close to the edge would be cut off
        svg.append('<text class="tick" x="%.1f" y="%d">%s</text>' % (
            LEFT + i * slot, H - 8, fmt_time(a.bucket_start(i))))
    svg.append("</svg>")
    legend = ("<p class='legend'><span><i class='error'></i>Errors</span>"
              "<span><i class='warn'></i>Warnings</span><span><i class='other'></i>Other</span></p>")
    return "<figure>%s%s</figure>" % ("".join(svg), legend)


def _patterns(a: Analysis, templates: List[Template], first: bool = False) -> str:
    headers = (["First seen"] if first else []) + ["Level", "Count", "Pattern", "Activity"]
    rows = []
    for t in templates:
        row = [fmt_time(t.first_ts)] if first else []
        row += ["<span class='lvl %s'><i></i>%s</span>" % (t.level, LEVEL_NAMES[t.level]),
                _n(t.count),
                "<code>%s</code><small>id %s &middot; e.g. %s</small>" % (
                    escape(t.text), t.id, escape(t.example[:160])),
                _spark(a.template_series(t))]
        rows.append(row)
    return _table(headers, rows, numeric=(2,) if first else (1,))


def _spark(series: List[int]) -> str:
    """A tiny bar chart of one pattern's activity over the whole log."""
    if not series or max(series) == 0:
        return ""
    width, height, top = 120.0, 22.0, float(max(series))
    step = width / len(series)
    bars = "".join('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f"/>' % (
        i * step, height - v / top * height, max(0.6, step - 0.3), v / top * height)
        for i, v in enumerate(series) if v)
    return ('<svg class="spark" viewBox="0 0 120 22" role="img" aria-label="peak %d">%s</svg>'
            % (int(top), bars))


def _table(headers: List[str], rows: List[List[str]], numeric=()) -> str:
    def cls(i):
        return " class='num'" if i in numeric else ""
    head = "".join("<th%s>%s</th>" % (cls(i), escape(h)) for i, h in enumerate(headers))
    body = "".join("<tr>%s</tr>" % "".join(
        "<td%s>%s</td>" % (cls(i), cell) for i, cell in enumerate(row)) for row in rows)
    return "<div class='scroll'><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>" % (
        head, body)


def _nice(value: int) -> int:
    """Round an axis maximum up to a multiple of 4 'round' steps."""
    if value <= 4:
        return 4
    magnitude = 10 ** (len(str(value)) - 1)
    for step in (1, 2, 2.5, 5, 10):
        top = step * magnitude
        if top >= value and top % 4 == 0:
            return int(top)
    return int(-(-value // 4) * 4)


def _n(value: int) -> str:
    return "{:,}".format(value)


_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Log report</title>
<style>
:root{--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--line:#e1e0d9;--axis:#c3c2b7;--error:#d03b3b;--warn:#fab219;--other:#2a78d6;--band:#d03b3b14}
@media (prefers-color-scheme:dark){:root{--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;
--ink2:#c3c2b7;--line:#2c2c2a;--axis:#383835;--other:#3987e5;--band:#d03b3b26}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1040px;margin:0 auto;padding:28px 16px 56px}
h1{font-size:24px;margin:0}
h2{font-size:16px;margin:36px 0 8px}
p{margin:6px 0}
.sub,small{color:var(--muted);font-size:13px}
small{display:block;margin-top:2px;word-break:break-word}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:18px}
.tiles div{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.tiles span{display:block;font-size:12px;color:var(--muted)}
.tiles b{font-size:22px;font-variant-numeric:tabular-nums}
figure{margin:0;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px}
figure svg{width:100%;height:auto;display:block}
.grid{stroke:var(--line);stroke-width:1}
.tick{fill:var(--muted);font-size:11px}
.band{fill:var(--band)}
rect.error,i.error{fill:var(--error);background:var(--error)}
rect.warn,i.warn{fill:var(--warn);background:var(--warn)}
rect.other,i.other{fill:var(--other);background:var(--other)}
.hit{fill:transparent}
.hit:hover{fill:var(--ink);fill-opacity:.08}
.legend{display:flex;gap:16px;font-size:12px;color:var(--ink2);margin:8px 4px 0}
.legend span{display:flex;align-items:center;gap:6px}
.legend i{width:10px;height:10px;border-radius:2px}
.scroll{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:9px 12px;border-top:1px solid var(--line);vertical-align:top}
thead th{border-top:0;font-size:12px;color:var(--muted);font-weight:500;white-space:nowrap}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td:first-child{white-space:nowrap}
code{font:13px ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-word}
.lvl{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.lvl i{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.lvl.error i,.lvl.fatal i{background:var(--error)}
.lvl.warn i{background:var(--warn)}
.spark{width:120px;height:22px;display:block}
.spark rect{fill:var(--ink2)}
details{margin-top:36px}
summary{cursor:pointer;font-weight:600;margin-bottom:8px}
footer{margin-top:36px;font-size:12px;color:var(--muted)}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--page);font-size:12px;
padding:5px 8px;border-radius:6px;white-space:nowrap;display:none;z-index:2}
</style></head><body><main>
"""

_FOOT = """<footer>Generated by trag. Times are UTC.</footer></main>
<div id="tip" role="tooltip"></div>
<script>
(function(){var tip=document.getElementById('tip');
document.addEventListener('mousemove',function(e){var t=e.target;
var text=t.getAttribute&&t.getAttribute('data-tip');
if(!text){tip.style.display='none';return}
tip.textContent=text;tip.style.display='block';
var w=tip.offsetWidth,x=Math.min(Math.max(8,e.clientX-w/2),innerWidth-w-8);
tip.style.left=x+'px';tip.style.top=(e.clientY-36)+'px'})})();
</script></body></html>
"""
