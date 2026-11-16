"""One pass over the events, then the questions a person on call asks:

    When did it start?            -> error_onset()
    What started happening then?  -> new_templates()
    What got suddenly louder?     -> spikes()
    What is failing the most?     -> top_templates(errors_only=True)
    What changed between A and B? -> diff()

The statistics are deliberately simple and robust.  "Normal" is the median
count per time bucket; "unusual" is measured in units of MAD (median absolute
deviation) rather than standard deviation, because the outage we are looking
for would itself inflate a mean and a standard deviation and hide.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .drain import Drain, Template, mask
from .parse import SEVERITY, Event

BASE = 10  # seconds; counts are kept at this resolution and regrouped later
BUCKET_SIZES = (10, 30, 60, 300, 900, 3600, 6 * 3600, 86400)
MAX_BUCKETS = 120
RARE_MAX = 5  # a pattern seen at most this many times in the whole log is "rare"


def baseline(series: List[int]) -> Tuple[float, float]:
    """(median, scale) of a series.  ``scale`` is what one unit of "unusual"
    means: 1.4826 * MAD estimates the standard deviation for normal data.
    Counts are not normal - a quiet series has MAD 0 - so the scale is never
    allowed below sqrt(median) (what a Poisson process would give) or below 1."""
    if not series:
        return 0.0, 1.0
    ordered = sorted(series)
    median = _median(ordered)
    mad = _median(sorted(abs(x - median) for x in ordered))
    return median, max(1.4826 * mad, math.sqrt(median), 1.0)


def _median(ordered: List[float]) -> float:
    n = len(ordered)
    mid = n // 2
    return float(ordered[mid]) if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


class Analysis:
    def __init__(self, drain: Optional[Drain] = None):
        self.drain = drain or Drain()
        self.events = 0
        self.untimed = 0
        self.stack_lines = 0
        self.levels: Counter = Counter()
        self.exceptions: Counter = Counter()
        self.first_ts: Optional[float] = None
        self.last_ts: Optional[float] = None
        self._total: Counter = Counter()           # base bucket -> events
        self._by_level: Dict[str, Counter] = defaultdict(Counter)
        self._by_template: Dict[Template, Counter] = defaultdict(Counter)
        # the few occurrences of templates that stay rare: (ts, line, message)
        self._rare: Dict[Template, List[Tuple[float, int, str]]] = {}
        # access-log extras
        self.statuses: Counter = Counter()
        self.paths: Dict[str, Dict[str, Any]] = {}
        self.clients: Counter = Counter()

    # ------------------------------------------------------------------
    # building

    def add(self, event: Event) -> Template:
        template = self.drain.add(event.message)
        template.count += 1
        template.levels[event.level] = template.levels.get(event.level, 0) + 1
        if event.source:
            template.sources[event.source] = template.sources.get(event.source, 0) + 1
        if template.count == 1:
            template.example, template.first_line = event.message, event.line_no

        self.events += 1
        self.levels[event.level] += 1
        self.stack_lines += event.extra_lines
        if "exception" in event.fields:
            self.exceptions[event.fields["exception"]] += 1

        ts = event.ts
        if ts is None:
            self.untimed += 1
        else:
            if self.first_ts is None or ts < self.first_ts:
                self.first_ts = ts
            if self.last_ts is None or ts > self.last_ts:
                self.last_ts = ts
            if template.first_ts is None or ts < template.first_ts:
                template.first_ts = ts
            if template.last_ts is None or ts > template.last_ts:
                template.last_ts = ts
            if template.count <= RARE_MAX:
                self._rare.setdefault(template, []).append((ts, event.line_no, event.message))
            elif template in self._rare:
                del self._rare[template]  # no longer rare; stop remembering
            b = int(ts // BASE)
            self._total[b] += 1
            self._by_level[event.level][b] += 1
            self._by_template[template][b] += 1

        if "status" in event.fields:
            self._add_request(event.fields)
        return template

    def _add_request(self, f: Dict[str, Any]) -> None:
        status = f["status"]
        self.statuses[status] += 1
        self.clients[f.get("ip", "-")] += 1
        # /api/products/291 and /api/products/17 are the same endpoint
        path = mask(f["path"])
        p = self.paths.get(path)
        if p is None:
            p = self.paths[path] = {"count": 0, "errors": 0, "times": []}
        p["count"] += 1
        if status >= 500:
            p["errors"] += 1
        if "request_time" in f:
            p["times"].append(f["request_time"])

    def extend(self, events: Iterable[Event]) -> "Analysis":
        for event in events:
            self.add(event)
        return self

    # ------------------------------------------------------------------
    # time buckets

    @property
    def span(self) -> float:
        return (self.last_ts - self.first_ts) if self.first_ts is not None else 0.0

    @property
    def bucket(self) -> int:
        """Smallest bucket size that keeps the timeline at <= MAX_BUCKETS bars."""
        for size in BUCKET_SIZES:
            if self.span / size <= MAX_BUCKETS:
                return size
        return BUCKET_SIZES[-1]

    @property
    def bucket_count(self) -> int:
        if self.first_ts is None:
            return 0
        size = self.bucket
        return int(self.last_ts // size) - int(self.first_ts // size) + 1

    def bucket_start(self, index: int) -> float:
        return (int(self.first_ts // self.bucket) + index) * self.bucket

    def _series(self, counter: Counter) -> List[int]:
        n = self.bucket_count
        if not n:
            return []
        out = [0] * n
        factor = self.bucket // BASE
        origin = int(self.first_ts // self.bucket)
        for b, count in counter.items():
            out[b // factor - origin] += count
        return out

    def timeline(self) -> Dict[str, List[int]]:
        """Events per bucket, split into error / warn / other."""
        error = self._series(self._by_level["error"] + self._by_level["fatal"])
        warn = self._series(self._by_level["warn"])
        total = self._series(self._total)
        other = [t - e - w for t, e, w in zip(total, error, warn)]
        return {"error": error, "warn": warn, "other": other, "total": total}

    def template_series(self, template: Template) -> List[int]:
        return self._series(self._by_template.get(template, Counter()))

    # ------------------------------------------------------------------
    # questions

    def top_templates(self, n: int = 10, errors_only: bool = False,
                      min_level: Optional[str] = None) -> List[Template]:
        floor = SEVERITY["error"] if errors_only else SEVERITY.get(min_level or "debug", 0)

        def weight(t: Template) -> int:
            return sum(c for level, c in t.levels.items() if SEVERITY[level] >= floor)

        ranked = sorted((t for t in self.drain.templates if weight(t) > 0),
                        key=lambda t: (-weight(t), t.first_line))
        return ranked[:n]

    def error_onset(self) -> Optional[Dict[str, Any]]:
        """Find the period where the error rate left its normal range.

        A run is two or more consecutive buckets above median + 4 * scale
        (a single quiet bucket inside it does not break it).  If there are
        several runs, the one holding the most errors is returned.
        """
        series = self.timeline().get("error", [])
        if len(series) < 6:
            return None
        median, scale = baseline(series)
        threshold = median + 4 * scale
        runs, start = [], None
        for i, value in enumerate(series + [0]):
            if value > threshold and start is None:
                start = i
            elif value <= threshold and start is not None:
                # one quiet bucket inside an outage does not end it
                if i + 1 < len(series) and series[i + 1] > threshold:
                    continue
                runs.append((start, i - 1))
                start = None
        runs = [r for r in runs if r[1] - r[0] >= 1]
        if not runs:
            return None
        first, last = max(runs, key=lambda r: sum(series[r[0]: r[1] + 1]))
        return {
            "start": self.bucket_start(first),
            "end": self.bucket_start(last + 1),
            "errors": sum(series[first: last + 1]),
            "peak": max(series[first: last + 1]),
            "normal": median,
            "first_bucket": first,
            "last_bucket": last,
        }

    def before_onset(self, onset: Dict[str, Any], window: float = 600.0) -> List[Dict[str, Any]]:
        """Rare events in the minutes before the errors began.

        Deploys, config reloads, restarts and failovers are logged once, not
        thousands of times, so "rare, and shortly before the onset" is a
        cheap and surprisingly good filter for "what changed?".
        """
        lo, hi = onset["start"] - window, onset["start"] + self.bucket
        found = []
        for template, occurrences in self._rare.items():
            if template.count > RARE_MAX:
                continue
            for ts, line_no, message in occurrences:
                if lo <= ts < hi:
                    found.append({"ts": ts, "line": line_no, "message": message,
                                  "template": template})
        return sorted(found, key=lambda o: (o["ts"], o["line"]))

    def new_templates(self, min_count: int = 1, warmup: float = 0.25) -> List[Template]:
        """Patterns that never appeared during the first quarter of the log.

        Whatever a system was already saying before things went wrong is
        unlikely to be the cause; what it *started* saying is where to look.
        """
        if self.first_ts is None or self.span <= 0:
            return []
        cutoff = self.first_ts + self.span * warmup
        fresh = [t for t in self.drain.templates
                 if t.first_ts is not None and t.first_ts > cutoff and t.count >= min_count]
        return sorted(fresh, key=lambda t: t.first_ts)

    def spikes(self, min_peak: int = 10, min_score: float = 5.0) -> List[Dict[str, Any]]:
        """Existing patterns whose rate jumped far above their own normal."""
        fresh = set(self.new_templates())
        found = []
        for template in self.drain.templates:
            if template in fresh or template.count < 20:
                continue
            series = self.template_series(template)
            if len(series) < 6:
                continue
            median, scale = baseline(series)
            peak = max(series)
            score = (peak - median) / scale
            if peak >= min_peak and score >= min_score:
                found.append({"template": template, "peak": peak, "normal": median,
                              "score": score, "at": self.bucket_start(series.index(peak))})
        return sorted(found, key=lambda s: -s["score"])

    def requests(self) -> Optional[Dict[str, Any]]:
        """Access-log view: status classes, worst and slowest paths, top clients."""
        total = sum(self.statuses.values())
        if not total:
            return None
        classes: Counter = Counter()
        for status, count in self.statuses.items():
            classes["%dxx" % (status // 100)] += count
        rows = []
        for path, p in self.paths.items():
            times = sorted(p["times"])
            rows.append({"path": path, "count": p["count"], "errors": p["errors"],
                         "error_pct": 100.0 * p["errors"] / p["count"],
                         "p95": times[(len(times) * 95 + 99) // 100 - 1] if times else None})
        return {
            "total": total,
            "classes": dict(sorted(classes.items())),
            "failing": sorted((r for r in rows if r["errors"]), key=lambda r: -r["errors"])[:8],
            "slowest": sorted((r for r in rows if r["p95"] is not None and r["count"] >= 5),
                              key=lambda r: -r["p95"])[:8],
            "clients": self.clients.most_common(5),
        }


# --------------------------------------------------------------------------
# comparing two sets of events


def diff(before: Iterable[Event], after: Iterable[Event], min_count: int = 5,
         ratio: float = 3.0) -> Dict[str, Any]:
    """What is different about ``after``?

    Both sides are mined with the same Drain instance so that a pattern has
    the same identity on both.  Counts are compared as rates per 1000 events,
    so a longer "after" file does not make everything look like an increase.
    """
    drain = Drain()
    counts: Dict[Template, List[int]] = defaultdict(lambda: [0, 0])
    totals = [0, 0]
    for side, events in enumerate((before, after)):
        for event in events:
            template = drain.add(event.message)
            template.count += 1
            template.levels[event.level] = template.levels.get(event.level, 0) + 1
            if template.count == 1:
                template.example, template.first_line = event.message, event.line_no
            counts[template][side] += 1
            totals[side] += 1

    rows = []
    for template, (a, b) in counts.items():
        rate_a = 1000.0 * a / totals[0] if totals[0] else 0.0
        rate_b = 1000.0 * b / totals[1] if totals[1] else 0.0
        if a == 0 and b >= min_count:
            kind = "new"
        elif b == 0 and a >= min_count:
            kind = "gone"
        elif max(a, b) >= min_count and rate_b >= rate_a * ratio:
            kind = "more"
        elif max(a, b) >= min_count and rate_a >= rate_b * ratio:
            kind = "less"
        else:
            continue
        rows.append({"kind": kind, "template": template, "before": a, "after": b,
                     "rate_before": rate_a, "rate_after": rate_b})
    order = {"new": 0, "more": 1, "gone": 2, "less": 3}
    rows.sort(key=lambda r: (order[r["kind"]], -SEVERITY[r["template"].level],
                             -max(r["before"], r["after"])))
    return {"before": totals[0], "after": totals[1], "rows": rows}
