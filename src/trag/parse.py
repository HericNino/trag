"""Reading log files: format detection and one parser per format.

Every parser turns a line into the same ``Event`` (time, level, source,
message, extra fields), so nothing after this module cares what the file
looked like.  Supported:

    app     2026-11-16 03:27:14,512 ERROR db.pool: Timeout acquiring ...
            (Python logging, log4j/logback, most "timestamp level message" logs)
    access  10.0.0.7 - - [16/Nov/2026:03:27:14 +0000] "GET /x HTTP/1.1" 500 12 ...
            (nginx / Apache combined, optional trailing request time)
    syslog  Nov 16 03:27:14 web-1 sshd[812]: Failed password for ...
    json    {"time": "...", "level": "error", "msg": "..."}   (one object per line)
    plain   anything else: the whole line is the message, no timestamp

A line that does not match the file's format is treated as a continuation
of the previous event - that is how stack traces are kept attached to the
error that produced them instead of becoming hundreds of junk events.
"""
from __future__ import annotations

import calendar
import gzip
import io
import json
import re
import sys
import time
from itertools import chain
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

SEVERITY = {"debug": 0, "info": 1, "warn": 2, "error": 3, "fatal": 4}
_LEVEL_NAMES = {
    "TRACE": "debug", "DEBUG": "debug", "INFO": "info", "NOTICE": "info",
    "WARN": "warn", "WARNING": "warn", "ERROR": "error", "ERR": "error",
    "SEVERE": "error", "FATAL": "fatal", "CRITICAL": "fatal", "CRIT": "fatal",
    "EMERG": "fatal", "ALERT": "fatal", "PANIC": "fatal",
}
FORMATS = ("app", "access", "syslog", "json", "plain")


class Event:
    __slots__ = ("ts", "level", "source", "message", "line_no", "file", "fields", "extra_lines")

    def __init__(self, ts: Optional[float], level: str, source: str, message: str,
                 fields: Optional[Dict[str, Any]] = None):
        self.ts = ts
        self.level = level
        self.source = source
        self.message = message
        self.fields = fields or {}
        self.line_no = 0
        self.file = ""
        self.extra_lines = 0

    @property
    def is_error(self) -> bool:
        return SEVERITY[self.level] >= SEVERITY["error"]


# --------------------------------------------------------------------------
# timestamps -> Unix seconds.  A timestamp without a zone is read as UTC:
# the arithmetic stays consistent and the times printed back match the file.

_ISO = (r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:[.,](\d+))?(Z|[+-]\d\d:?\d\d)?")
_ISO_RE = re.compile(_ISO)
_MONTHS = {m: i + 1 for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}


def _offset(zone: Optional[str]) -> int:
    if not zone or zone == "Z":
        return 0
    sign = -1 if zone[0] == "-" else 1
    digits = zone[1:].replace(":", "")
    return sign * (int(digits[:2]) * 3600 + int(digits[2:4]) * 60)


def _iso_epoch(m: "re.Match", first_group: int = 1) -> float:
    g = m.group
    i = first_group
    whole = calendar.timegm((int(g(i)), int(g(i + 1)), int(g(i + 2)),
                             int(g(i + 3)), int(g(i + 4)), int(g(i + 5))))
    frac = float("0." + g(i + 6)) if g(i + 6) else 0.0
    return whole + frac - _offset(g(i + 7))


def parse_time(text: str) -> Optional[float]:
    m = _ISO_RE.search(text)
    return _iso_epoch(m) if m else None


# --------------------------------------------------------------------------
# level inference for formats that do not carry one

_ERROR_WORDS = re.compile(
    r"\b(error|errors|fail(?:ed|ure|ing)?|exception|fatal|panic|critical|denied|refused|"
    r"timed out|timeout|unreachable|segfault|oom|killed)\b", re.I)
_WARN_WORDS = re.compile(r"\b(warn(?:ing)?|deprecated|retry(?:ing)?|slow)\b", re.I)


def infer_level(message: str) -> str:
    if _ERROR_WORDS.search(message):
        return "error"
    if _WARN_WORDS.search(message):
        return "warn"
    return "info"


# --------------------------------------------------------------------------
# parsers: line -> Event, or None if the line is not in this format

_APP_RE = re.compile(r"^\[?" + _ISO + r"\]?[ \t]*(.*)$")
# up to two tokens (thread name, pid ...) may sit between timestamp and level
_APP_LEVEL_RE = re.compile(
    r"^(?:\[[^\]]*\][ \t]+|[^\s\[]+[ \t]+){0,2}?[\[<]?"
    r"(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|SEVERE|FATAL|CRITICAL|CRIT)[\]>]?:?[ \t]+(.*)$")
# a logger name: [bracketed], dotted.name:, lowercase_word:, or "Name - "
_APP_SOURCE_RE = re.compile(
    r"^(\[[^\]]+\]|[\w$-]+(?:\.[\w$-]+)+:|[a-z][a-z0-9_-]*:|[\w.$-]+ -)[ \t]+(.*)$")


def parse_app(line: str) -> Optional[Event]:
    m = _APP_RE.match(line)
    if not m:
        return None
    ts = _iso_epoch(m)
    rest = m.group(9)
    lm = _APP_LEVEL_RE.match(rest)
    if lm:
        level, message = _LEVEL_NAMES[lm.group(1)], lm.group(2)
    else:
        level, message = infer_level(rest), rest
    source = ""
    sm = _APP_SOURCE_RE.match(message)
    if sm:
        source, message = sm.group(1).strip("[]: -"), sm.group(2)
    return Event(ts, level, source, message)


_ACCESS_RE = re.compile(
    r'^(\S+) \S+ \S+ \[(\d\d)/(\w{3})/(\d{4}):(\d\d):(\d\d):(\d\d) ([+-]\d{4})\] '
    r'"(\S+) (\S+)[^"]*" (\d{3}) (\S+)(?: "[^"]*" "([^"]*)")?(?: (\d+\.\d+))?')


def parse_access(line: str) -> Optional[Event]:
    m = _ACCESS_RE.match(line)
    if not m:
        return None
    g = m.group
    month = _MONTHS.get(g(3))
    if not month:
        return None
    ts = calendar.timegm((int(g(4)), month, int(g(2)), int(g(5)), int(g(6)), int(g(7))))
    ts -= _offset(g(8))
    status = int(g(11))
    level = "error" if status >= 500 else "warn" if status >= 400 else "info"
    path = g(10).split("?", 1)[0]  # the query string is noise for grouping
    fields = {"ip": g(1), "method": g(9), "path": path, "status": status,
              "bytes": int(g(12)) if g(12).isdigit() else 0}
    if g(14):
        fields["request_time"] = float(g(14))
    return Event(ts, level, "", "%s %s status=%d" % (g(9), path, status), fields)


_SYSLOG_RE = re.compile(
    r"^([A-Z][a-z]{2})\s+(\d{1,2}) (\d\d):(\d\d):(\d\d) (\S+) ([^\s:\[]+)(?:\[(\d+)\])?:\s?(.*)$")


def parse_syslog(line: str, _now: Optional[float] = None) -> Optional[Event]:
    m = _SYSLOG_RE.match(line)
    if not m or m.group(1) not in _MONTHS:
        return None
    g = m.group
    # Classic syslog has no year.  Assume this year, unless that puts the
    # line in the future (a December log read in January).
    now = time.time() if _now is None else _now
    year = time.gmtime(now).tm_year
    stamp = (_MONTHS[g(1)], int(g(2)), int(g(3)), int(g(4)), int(g(5)))
    ts = calendar.timegm((year,) + stamp)
    if ts > now + 86400:
        ts = calendar.timegm((year - 1,) + stamp)
    fields = {"host": g(6)}
    if g(8):
        fields["pid"] = int(g(8))
    return Event(ts, infer_level(g(9)), g(7), g(9), fields)


_JSON_TIME = ("time", "timestamp", "ts", "@timestamp", "t", "datetime")
_JSON_LEVEL = ("level", "severity", "lvl", "levelname", "log.level")
_JSON_MESSAGE = ("msg", "message", "event", "text")
_JSON_SOURCE = ("logger", "name", "source", "component", "service", "module")


def parse_json(line: str) -> Optional[Event]:
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None

    def take(keys):
        for k in keys:
            if k in obj:
                return obj.pop(k)
        return None

    raw_time, ts = take(_JSON_TIME), None
    if isinstance(raw_time, (int, float)) and not isinstance(raw_time, bool):
        ts = raw_time / 1000.0 if raw_time > 1e11 else float(raw_time)  # ms vs s
    elif isinstance(raw_time, str):
        ts = parse_time(raw_time)
    raw_level = take(_JSON_LEVEL)
    message = take(_JSON_MESSAGE)
    message = "" if message is None else str(message)
    level = _LEVEL_NAMES.get(str(raw_level).upper()) if raw_level is not None else None
    source = take(_JSON_SOURCE)
    return Event(ts, level or infer_level(message), str(source) if source else "", message, obj)


def parse_plain(line: str) -> Optional[Event]:
    return Event(None, infer_level(line), "", line)


PARSERS: Dict[str, Callable[[str], Optional[Event]]] = {
    "json": parse_json, "access": parse_access, "app": parse_app,
    "syslog": parse_syslog, "plain": parse_plain,
}


def detect_format(sample: Iterable[str]) -> str:
    """Pick the format that parses the most sample lines.

    Continuation lines (stack traces) match nothing, so the bar is "more
    than a third of the lines", not "all of them".  Ties go to the more
    specific format, which is the order of the tuple below.
    """
    lines = [l for l in sample if l.strip()]
    if not lines:
        return "plain"
    best, best_hits = "plain", 0
    for name in ("json", "access", "app", "syslog"):
        hits = sum(1 for l in lines if PARSERS[name](l) is not None)
        if hits > best_hits:
            best, best_hits = name, hits
    return best if best_hits * 3 > len(lines) else "plain"


# --------------------------------------------------------------------------
# reading

_EXCEPTION_RE = re.compile(r"^(?:Caused by: )?([A-Za-z_][\w.]*(?:Error|Exception|Timeout|Fault))\b")
SAMPLE_LINES = 200


def open_log(path: str) -> io.TextIOBase:
    if path == "-":
        return sys.stdin  # type: ignore[return-value]
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")  # type: ignore[return-value]
    return open(path, "r", encoding="utf-8", errors="replace")


class Reader:
    """Iterates the events of one or more files and keeps a few counters."""

    def __init__(self, paths: List[str], fmt: Optional[str] = None):
        self.paths = paths
        self.forced = fmt
        self.formats: Dict[str, str] = {}
        self.lines = 0
        self.continuation_lines = 0

    def __iter__(self) -> Iterator[Event]:
        for path in self.paths:
            with open_log(path) as f:
                yield from self._read(path, f)

    def _read(self, path: str, f: Iterable[str]) -> Iterator[Event]:
        it = iter(f)
        head = []
        for line in it:
            head.append(line)
            if len(head) >= SAMPLE_LINES:
                break
        fmt = self.forced or detect_format(l.rstrip("\r\n") for l in head)
        self.formats[path] = fmt
        parse = PARSERS[fmt]
        pending: Optional[Event] = None
        line_no = 0
        for raw in chain(head, it):
            line_no += 1
            self.lines += 1
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            event = parse(line)
            if event is None:
                # Not a new event: fold it into the previous one.
                self.continuation_lines += 1
                if pending is not None:
                    pending.extra_lines += 1
                    m = _EXCEPTION_RE.match(line.strip())
                    if m:
                        pending.fields["exception"] = m.group(1)
                continue
            if pending is not None:
                yield pending
            event.line_no, event.file = line_no, path
            pending = event
        if pending is not None:
            yield pending


def fmt_time(ts: Optional[float], with_date: bool = False) -> str:
    if ts is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S", time.gmtime(ts))
