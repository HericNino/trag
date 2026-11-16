"""Load a log into SQLite so it can be questioned with SQL.

Two things you can query:

    events      one row per log event:
                line, file, ts (Unix seconds), time (text), level, source,
                message, extra_lines, exception, template_id, template,
                and for access logs: ip, method, path, status, bytes, request_time
    templates   one row per mined pattern:
                template_id, template, count, level, first_seen, last_seen

The database lives in memory and is thrown away afterwards; the log file is
never modified.
"""
from __future__ import annotations

import sqlite3
from typing import Dict, Iterable, List, Tuple

from .analyze import Analysis
from .drain import Template
from .parse import Event, fmt_time

_SCHEMA = """
CREATE TABLE events_raw (
    line INTEGER, file TEXT, ts REAL, time TEXT, level TEXT, source TEXT, message TEXT,
    extra_lines INTEGER, exception TEXT,
    ip TEXT, method TEXT, path TEXT, status INTEGER, bytes INTEGER, request_time REAL,
    tpl INTEGER
);
CREATE TABLE templates (
    tpl INTEGER PRIMARY KEY, template_id TEXT, template TEXT, count INTEGER, level TEXT,
    first_seen TEXT, last_seen TEXT
);
CREATE VIEW events AS
    SELECT e.line, e.file, e.ts, e.time, e.level, e.source, e.message, e.extra_lines,
           e.exception, e.ip, e.method, e.path, e.status, e.bytes, e.request_time,
           t.template_id, t.template
    FROM events_raw e JOIN templates t USING (tpl);
"""
_BATCH = 5000


def load(events: Iterable[Event]) -> Tuple[sqlite3.Connection, Analysis]:
    db = sqlite3.connect(":memory:")
    db.executescript(_SCHEMA)
    analysis = Analysis()
    numbers: Dict[Template, int] = {}
    batch: List[tuple] = []
    insert = "INSERT INTO events_raw VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    for event in events:
        template = analysis.add(event)
        # A template's text keeps changing while the file is read, so events
        # point at it by number and the text is written once, at the end.
        number = numbers.setdefault(template, len(numbers))
        f = event.fields
        batch.append((event.line_no, event.file, event.ts, fmt_time(event.ts, True),
                      event.level, event.source, event.message, event.extra_lines,
                      f.get("exception"), f.get("ip"), f.get("method"), f.get("path"),
                      f.get("status"), f.get("bytes"), f.get("request_time"), number))
        if len(batch) >= _BATCH:
            db.executemany(insert, batch)
            batch.clear()
    db.executemany(insert, batch)
    db.executemany(
        "INSERT INTO templates VALUES (?,?,?,?,?,?,?)",
        [(n, t.id, t.text, t.count, t.level, fmt_time(t.first_ts, True), fmt_time(t.last_ts, True))
         for t, n in numbers.items()])
    db.execute("CREATE INDEX idx_events_tpl ON events_raw (tpl)")
    db.commit()
    return db, analysis


def run(db: sqlite3.Connection, query: str) -> Tuple[List[str], List[tuple]]:
    cursor = db.execute(query)
    headers = [d[0] for d in cursor.description] if cursor.description else []
    return headers, cursor.fetchall()
