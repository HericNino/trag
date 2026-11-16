"""Terminal output: the summary and the small tables the commands print."""
from __future__ import annotations

import os
import shutil
import sys
from typing import Any, List, Optional, Sequence

from .analyze import Analysis
from .drain import Template
from .parse import fmt_time

_BLOCKS = " ▁▂▃▄▅▆▇█"
_COLORS = {"error": "31", "fatal": "31;1", "warn": "33", "dim": "2", "bold": "1", "head": "1;36"}


class Out:
    """Collects lines; knows the terminal width and whether to use colour."""

    def __init__(self, color: Optional[bool] = None, width: Optional[int] = None):
        if color is None:
            # NO_COLOR is the cross-tool convention (https://no-color.org)
            color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        self.color = color
        self.width = width or shutil.get_terminal_size((110, 24)).columns
        self.lines: List[str] = []

    def c(self, text: str, style: str) -> str:
        if not self.color or style not in _COLORS:
            return text
        return "\033[%sm%s\033[0m" % (_COLORS[style], text)

    def line(self, text: str = "") -> None:
        self.lines.append(text)

    def head(self, text: str) -> None:
        self.lines.append("")
        self.lines.append(self.c(text, "head"))

    def cut(self, text: str, used: int) -> str:
        room = max(20, self.width - used)
        return text if len(text) <= room else text[: room - 1] + "…"

    def __str__(self) -> str:
        return "\n".join(self.lines) + "\n"


def sparkline(series: Sequence[int], width: int = 60) -> str:
    """Compress a series to ``width`` characters, one block per group (max of
    the group, so a one-bucket spike is never averaged away)."""
    if not series:
        return ""
    if len(series) > width:
        step = len(series) / float(width)
        series = [max(series[int(i * step): max(int(i * step) + 1, int((i + 1) * step))])
                  for i in range(width)]
    top = max(series)
    if top == 0:
        return _BLOCKS[1] * len(series)
    return "".join(_BLOCKS[0] if v == 0 else _BLOCKS[-(-v * 8 // top)] for v in series)


def bucket_label(seconds: int) -> str:
    if seconds < 60:
        return "%d s" % seconds
    if seconds < 3600:
        return "%d min" % (seconds // 60)
    return "%d h" % (seconds // 3600)


def level_tag(out: Out, level: str) -> str:
    return out.c("%-5s" % level.upper(), level)


def template_row(out: Out, t: Template, count: Optional[int] = None, first: bool = False) -> str:
    left = "  %s  %s %6d×  " % (t.id, level_tag(out, t.level), t.count if count is None else count)
    if first:
        left = "  %s  %s" % (fmt_time(t.first_ts), left.lstrip())
    return left + out.cut(t.text, len(left) - (9 if out.color else 0))


def summary(a: Analysis, reader, out: Out, top: int = 8) -> None:
    formats = sorted(set(reader.formats.values()))
    out.line(out.c("trag", "bold") + "  %s" % ", ".join(reader.paths))
    out.line("  format %s · %s lines · %s events · %d patterns" % (
        "/".join(formats), _n(reader.lines), _n(a.events), len(a.drain.templates)))
    if a.first_ts is not None:
        out.line("  %s → %s UTC (%s)" % (fmt_time(a.first_ts, True), fmt_time(a.last_ts, True),
                                              _span(a.span)))
    levels = "  ".join("%s %s" % (level, _n(a.levels[level]))
                       for level in ("fatal", "error", "warn", "info", "debug") if a.levels[level])
    out.line("  " + levels)
    if a.stack_lines:
        out.line("  %s stack-trace lines folded into their events" % _n(a.stack_lines))

    timeline = a.timeline()
    if timeline.get("total"):
        out.head("Timeline  (one bar ≈ %s)" % bucket_label(
            a.bucket * max(1, -(-len(timeline["total"]) // 60))))
        out.line("  all     %s  peak %s" % (sparkline(timeline["total"]), _n(max(timeline["total"]))))
        out.line("  errors  %s  peak %s" % (out.c(sparkline(timeline["error"]), "error"),
                                            _n(max(timeline["error"]))))
        out.line("          %-30s%30s" % (fmt_time(a.first_ts), fmt_time(a.last_ts)))

    onset = a.error_onset()
    if onset:
        out.head("When it started")
        out.line("  Errors left their normal range at %s and returned to it at %s." % (
            out.c(fmt_time(onset["start"]), "bold"), out.c(fmt_time(onset["end"]), "bold")))
        out.line("  %s errors in that period; peak %d per %s against a normal %g." % (
            _n(onset["errors"]), onset["peak"], bucket_label(a.bucket), onset["normal"]))
        before = a.before_onset(onset)
        if before:
            out.head("Rare events in the 10 minutes before  (deploys, restarts, config changes)")
            for o in before[:12]:
                left = "  %s  line %-7d " % (fmt_time(o["ts"]), o["line"])
                out.line(left + out.cut(o["message"], len(left)))

    fresh = a.new_templates()
    if fresh:
        out.head("New patterns  (never seen in the first quarter of the log)")
        for t in fresh[:15]:
            out.line(template_row(out, t, first=True))
        if len(fresh) > 15:
            out.line(out.c("  … and %d more" % (len(fresh) - 15), "dim"))

    errors = a.top_templates(top, errors_only=True)
    if errors:
        out.head("Top error patterns")
        for t in errors:
            out.line(template_row(out, t))

    spikes = a.spikes()
    if spikes:
        out.head("Existing patterns that got louder")
        for s in spikes[:6]:
            t = s["template"]
            left = "  %s  %s peak %d per %s at %s (normal %g)  " % (
                t.id, level_tag(out, t.level), s["peak"], bucket_label(a.bucket),
                fmt_time(s["at"]), s["normal"])
            out.line(left + out.cut(t.text, len(left) - (9 if out.color else 0)))

    if a.exceptions:
        out.head("Exceptions in stack traces")
        for name, count in a.exceptions.most_common(6):
            out.line("  %6d×  %s" % (count, name))

    requests = a.requests()
    if requests:
        out.head("Requests")
        out.line("  " + "  ".join("%s %s" % (k, _n(v)) for k, v in requests["classes"].items()))
        if requests["failing"]:
            out.line("  failing paths (5xx):")
            for r in requests["failing"]:
                out.line("    %6d of %-6d %5.1f%%  %s" % (r["errors"], r["count"], r["error_pct"],
                                                         out.cut(r["path"], 34)))
        if requests["slowest"]:
            out.line("  slowest paths (p95 seconds):")
            for r in requests["slowest"][:5]:
                out.line("    %8.3f  %6d×  %s" % (r["p95"], r["count"], out.cut(r["path"], 28)))

    out.line()
    out.line(out.c("  next:  trag show FILE <id>   ·   trag diff FILE --at HH:MM   ·   "
                   "trag sql FILE \"SELECT ...\"", "dim"))


def table(headers: List[str], rows: List[Sequence[Any]], out: Out) -> None:
    cells = [[_cell(v) for v in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    # keep the table inside the terminal by shrinking the widest column
    overflow = sum(widths) + 2 * (len(widths) - 1) - out.width
    if overflow > 0 and widths:
        widest = widths.index(max(widths))
        widths[widest] = max(12, widths[widest] - overflow)
    numeric = [bool(cells) and all(_is_number(r[i]) for r in rows) for i in range(len(headers))]

    def fmt(row: List[str]) -> str:
        parts = []
        for i, cell in enumerate(row):
            if len(cell) > widths[i]:
                cell = cell[: widths[i] - 1] + "…"
            parts.append(cell.rjust(widths[i]) if numeric[i] else cell.ljust(widths[i]))
        return "  ".join(parts).rstrip()

    out.line(out.c(fmt(headers), "bold"))
    for row in cells:
        out.line(fmt(row))


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return ("%.3f" % value).rstrip("0").rstrip(".")
    return str(value).replace("\n", " ")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _n(value: int) -> str:
    return "{:,}".format(value)


def _span(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return "%d s" % seconds
    if seconds < 7200:
        return "%d min" % (seconds // 60)
    if seconds < 2 * 86400:
        return "%.1f h" % (seconds / 3600.0)
    return "%.1f days" % (seconds / 86400.0)
