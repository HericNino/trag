"""Command line interface.

    trag summary  FILE...            the whole picture in one screen
    trag patterns FILE...            every mined pattern, with counts
    trag show     FILE ID            one pattern: timeline, values, example lines
    trag diff     FILE --at 03:25    what changed after a point in time
    trag diff     BEFORE AFTER       ... or between two files
    trag sql      FILE "SELECT ..."  ask anything
    trag report   FILE -o out.html   a shareable HTML report
    trag demo                        write a sample incident to ./demo-logs

FILE may be a path, a .gz file, or - for standard input.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import Counter
from typing import Iterable, Iterator, List, Optional, Sequence

from . import __version__, demo as demo_mod, htmlreport, sqlstore
from .analyze import Analysis, diff as diff_events
from .drain import parameters
from .parse import FORMATS, SEVERITY, Event, Reader, fmt_time, parse_time
from .text import Out, level_tag, sparkline, summary, table, template_row


class UsageError(Exception):
    pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args) or 0
    except UsageError as e:
        print("trag: %s" % e, file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print("trag: cannot open %s" % e.filename, file=sys.stderr)
        return 2
    except BrokenPipeError:  # `trag ... | head` closed the pipe; not an error
        return 0
    except KeyboardInterrupt:
        return 130


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trag", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Log triage: patterns, error onset, before/after diff and SQL "
                    "for any log file.", epilog=__doc__.split("\n\n", 1)[1])
    p.add_argument("--version", action="version", version="trag " + __version__)
    sub = p.add_subparsers(metavar="command")

    def cmd(name, func, help_text, files="+"):
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.set_defaults(func=func)
        if files:
            sp.add_argument("files", nargs=files, metavar="FILE")
            sp.add_argument("--format", choices=FORMATS, help="skip auto-detection")
            sp.add_argument("--since", metavar="TIME", help="only events at or after HH:MM[:SS] "
                            "(on the log's first day) or a full ISO timestamp")
            sp.add_argument("--until", metavar="TIME", help="only events before TIME")
            sp.add_argument("--level", choices=list(SEVERITY), help="only this level and above")
            sp.add_argument("--no-color", action="store_true")
        return sp

    sp = cmd("summary", cmd_summary, "overview: timeline, onset, new and top error patterns")
    sp.add_argument("--top", type=int, default=8)

    sp = cmd("patterns", cmd_patterns, "list mined patterns")
    sp.add_argument("--top", type=int, default=30)
    sp.add_argument("--sort", choices=("count", "time"), default="count")

    sp = cmd("show", cmd_show, "one pattern in detail", files=None)
    sp.add_argument("file", metavar="FILE")
    sp.add_argument("id", metavar="ID", help="pattern id (or a unique prefix of it)")
    sp.add_argument("-n", type=int, default=5, help="example lines to print (default 5)")
    sp.add_argument("--format", choices=FORMATS)
    sp.add_argument("--no-color", action="store_true")

    sp = cmd("diff", cmd_diff, "what changed: FILE --at TIME, or BEFORE AFTER", files=None)
    sp.add_argument("files", nargs="+", metavar="FILE")
    sp.add_argument("--at", metavar="TIME", help="split one file at this time")
    sp.add_argument("--format", choices=FORMATS)
    sp.add_argument("--no-color", action="store_true")

    sp = cmd("sql", cmd_sql, "run a SQL query over the log (tables: events, templates)",
             files=None)
    sp.add_argument("file", metavar="FILE")
    sp.add_argument("query", nargs="?", help="omit to print the schema")
    sp.add_argument("--format", choices=FORMATS)
    sp.add_argument("--no-color", action="store_true")

    sp = cmd("report", cmd_report, "write a self-contained HTML report")
    sp.add_argument("-o", "--output", default="report.html")

    sp = cmd("demo", cmd_demo, "write a sample incident (app.log, access.log)", files=None)
    sp.add_argument("-o", "--output", default="demo-logs", help="directory (default: demo-logs)")
    return p


# --------------------------------------------------------------------------
# reading with filters

_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d\d)(?::(\d\d))?$")


def _resolve(text: Optional[str], day_start: float) -> Optional[float]:
    if text is None:
        return None
    m = _CLOCK_RE.match(text)
    if m:
        return day_start + int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3) or 0)
    ts = parse_time(text)
    if ts is None:
        raise UsageError("cannot read time %r (use HH:MM, HH:MM:SS or 2026-11-16T03:25:00)" % text)
    return ts


def _filtered(events: Iterable[Event], since: Optional[str] = None, until: Optional[str] = None,
              level: Optional[str] = None) -> Iterator[Event]:
    floor = SEVERITY[level] if level else 0
    lo = hi = None
    resolved = False
    for event in events:
        if SEVERITY[event.level] < floor:
            continue
        if since or until:
            if event.ts is None:
                continue
            if not resolved:
                # "03:25" means 03:25 on the day the log starts
                day_start = event.ts - event.ts % 86400
                lo, hi = _resolve(since, day_start), _resolve(until, day_start)
                resolved = True
            if (lo is not None and event.ts < lo) or (hi is not None and event.ts >= hi):
                continue
        yield event


def _analyse(args) -> "tuple[Analysis, Reader]":
    reader = Reader(args.files, args.format)
    analysis = Analysis().extend(_filtered(reader, args.since, args.until, args.level))
    if analysis.events == 0:
        raise UsageError("no events found (empty file, or the filters excluded everything)")
    return analysis, reader


def _out(args) -> Out:
    return Out(color=False if getattr(args, "no_color", False) else None)


def _emit(out: Out) -> None:
    sys.stdout.write(str(out))


# --------------------------------------------------------------------------
# commands


def cmd_summary(args) -> int:
    analysis, reader = _analyse(args)
    out = _out(args)
    summary(analysis, reader, out, top=args.top)
    _emit(out)
    return 0


def cmd_patterns(args) -> int:
    analysis, _ = _analyse(args)
    out = _out(args)
    templates = analysis.drain.templates
    if args.sort == "time":
        templates = sorted(templates, key=lambda t: (t.first_ts or 0, t.first_line))
    else:
        templates = sorted(templates, key=lambda t: (-t.count, t.first_line))
    out.line(out.c("  id      level   count   pattern", "bold"))
    for t in templates[: args.top]:
        out.line(template_row(out, t, first=args.sort == "time"))
    if len(templates) > args.top:
        out.line(out.c("  … %d more (use --top)" % (len(templates) - args.top), "dim"))
    _emit(out)
    return 0


def cmd_show(args) -> int:
    reader = Reader([args.file], args.format)
    analysis = Analysis().extend(reader)
    matches = [t for t in analysis.drain.templates if t.id.startswith(args.id.lower())]
    if not matches:
        raise UsageError("no pattern with id %r - run `trag patterns %s`" % (args.id, args.file))
    if len(matches) > 1:
        raise UsageError("id %r is ambiguous: %s" % (args.id, ", ".join(t.id for t in matches)))
    template = matches[0]

    # Second pass: now that the pattern is final, collect its lines.
    examples: List[Event] = []
    per_position: List[Counter] = []
    runs: List[list] = []  # [first_ts, values, count] for each stretch of identical values
    for event in Reader([args.file], args.format):
        if analysis.drain.match(event.message) is not template:
            continue
        if len(examples) < args.n:
            examples.append(event)
        values = parameters(template, event.message)
        for i, value in enumerate(values):
            if i >= len(per_position):
                per_position.append(Counter())
            per_position[i][value] += 1
        if values and len(runs) <= 12:
            if runs and runs[-1][1] == values:
                runs[-1][2] += 1
            else:
                runs.append([event.ts, values, 1])

    out = _out(args)
    out.line("%s  %s %d×  %s" % (template.id, level_tag(out, template.level), template.count,
                                      out.c(template.text, "bold")))
    out.line("  first %s   last %s   %s" % (
        fmt_time(template.first_ts, True), fmt_time(template.last_ts, True),
        "source " + ", ".join(sorted(template.sources)) if template.sources else ""))
    series = analysis.template_series(template)
    if series:
        out.line("  %s  %s → %s" % (sparkline(series), fmt_time(analysis.first_ts),
                                         fmt_time(analysis.last_ts)))
    if runs and len(runs) <= 12:
        out.head("Values over time")
        for ts, values, count in runs:
            out.line("  from %s  %6d×  %s" % (fmt_time(ts), count, ", ".join(values)))
    elif per_position:
        out.head("Values at the variable positions")
        for i, counter in enumerate(per_position):
            top = ", ".join("%s (%d)" % kv for kv in counter.most_common(5))
            out.line("  #%d  %d distinct: %s" % (i + 1, len(counter), out.cut(top, 20)))
    out.head("First lines")
    for e in examples:
        left = "  line %-7d %s  " % (e.line_no, fmt_time(e.ts))
        out.line(left + out.cut(e.message, len(left)))
    _emit(out)
    return 0


def cmd_diff(args) -> int:
    if args.at and len(args.files) == 1:
        before = _filtered(Reader(args.files, args.format), until=args.at)
        after = _filtered(Reader(args.files, args.format), since=args.at)
        names = ("before " + args.at, "after " + args.at)
    elif not args.at and len(args.files) == 2:
        before, after = Reader(args.files[:1], args.format), Reader(args.files[1:], args.format)
        names = (args.files[0], args.files[1])
    else:
        raise UsageError("use either `trag diff FILE --at TIME` or `trag diff BEFORE AFTER`")

    result = diff_events(before, after)
    out = _out(args)
    out.line("%s: %s events   %s: %s events" % (names[0], "{:,}".format(result["before"]),
                                                names[1], "{:,}".format(result["after"])))
    if not result["before"] or not result["after"]:
        raise UsageError("one side has no events, nothing to compare")
    titles = {"new": "New (never seen before)", "more": "More frequent (3× or more)",
              "gone": "Gone", "less": "Less frequent"}
    current = None
    for row in result["rows"]:
        if row["kind"] != current:
            current = row["kind"]
            out.head(titles[current])
        t = row["template"]
        left = "  %s  %s %6d → %-6d " % (t.id, level_tag(out, t.level), row["before"], row["after"])
        out.line(left + out.cut(t.text, len(left) - (9 if out.color else 0)))
    if not result["rows"]:
        out.line("no pattern changed its frequency by 3× or more")
    _emit(out)
    return 0


def cmd_sql(args) -> int:
    out = _out(args)
    if not args.query:
        out.line(sqlstore.__doc__.strip())
        _emit(out)
        return 0
    db, _ = sqlstore.load(Reader([args.file], args.format))
    try:
        headers, rows = sqlstore.run(db, args.query)
    except sqlite3.Error as e:
        raise UsageError("SQL error: %s" % e)
    table(headers, rows, out)
    out.line(out.c("(%d row%s)" % (len(rows), "" if len(rows) == 1 else "s"), "dim"))
    _emit(out)
    return 0


def cmd_report(args) -> int:
    analysis, reader = _analyse(args)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(htmlreport.render(analysis, reader))
    print("wrote %s" % args.output)
    return 0


def cmd_demo(args) -> int:
    paths = demo_mod.write(args.output)
    print("wrote %s" % " and ".join(paths))
    print("try:  trag summary %s" % paths[0])
    return 0
