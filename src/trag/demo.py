"""`trag demo`: write a realistic incident to disk so there is something to dig in.

The story (one hour of a web shop's logs, 03:00-04:00 UTC):

    03:25:10  release v2.14.0 is deployed; its config shrinks the database
              connection pool from 20 to 5
    03:27     under normal traffic 5 connections are not enough: requests
              queue for a connection, time out after 5 s and fail with 500
    03:47:00  the release is rolled back; errors stop within a minute

The log also contains ordinary background noise - a mail error every few
minutes, invalid coupons, payment retries - because real logs are never
clean and a triage tool has to tell "always like that" from "new".

Everything is generated from a fixed seed, so the README's walkthrough shows
exactly what you will see.
"""
from __future__ import annotations

import calendar
import os
import random
import time
from typing import List, Tuple

START = calendar.timegm((2026, 11, 16, 3, 0, 0))
DEPLOY = START + 25 * 60 + 10
INCIDENT = START + 27 * 60
ROLLBACK = START + 47 * 60
END = START + 3600

_TRACEBACK = [
    "Traceback (most recent call last):",
    '  File "/app/shop/orders.py", line 88, in create_order',
    "    conn = pool.acquire(timeout=5.0)",
    '  File "/app/shop/db/pool.py", line 142, in acquire',
    '    raise PoolTimeout("no free connection")',
    "shop.db.PoolTimeout: no free connection",
]


def generate(seed: int = 7) -> Tuple[List[str], List[str]]:
    """Return (app log lines, access log lines), each in time order."""
    rng = random.Random(seed)
    app: List[Tuple[float, List[str]]] = []
    access: List[Tuple[float, str]] = []

    def log(ts: float, level: str, source: str, message: str, extra: List[str] = ()):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) + ",%03d" % int(ts % 1 * 1000)
        app.append((ts, ["%s %s %s: %s" % (stamp, level, source, message)] + list(extra)))

    def hit(ts: float, method: str, path: str, status: int, seconds: float):
        stamp = time.strftime("%d/%b/%Y:%H:%M:%S +0000", time.gmtime(ts))
        access.append((ts, '10.0.%d.%d - - [%s] "%s %s HTTP/1.1" %d %d "-" "Mozilla/5.0" %.3f' % (
            rng.randint(0, 3), rng.randint(2, 250), stamp, method, path, status,
            rng.randint(200, 9000), seconds)))

    def severity(ts: float) -> float:
        """0 outside the incident, ramping to 1 over its first three minutes."""
        if not INCIDENT <= ts < ROLLBACK + 30:
            return 0.0
        return min(1.0, (ts - INCIDENT) / 180.0 + 0.25)

    # --- one-off events: the deploy and the rollback -------------------
    log(DEPLOY, "INFO", "deploy", "Starting release v2.14.0 (build 8841)")
    log(DEPLOY + 1.2, "INFO", "config", "Loaded settings db.pool.max_size=5 db.pool.timeout_ms=5000")
    log(DEPLOY + 2.5, "INFO", "deploy", "Release v2.14.0 is live")
    log(ROLLBACK, "WARN", "deploy", "Rolling back to release v2.13.2 (build 8790)")
    log(ROLLBACK + 1.4, "INFO", "config", "Loaded settings db.pool.max_size=20 db.pool.timeout_ms=5000")
    log(ROLLBACK + 2.8, "INFO", "deploy", "Release v2.13.2 is live")

    # --- periodic housekeeping -----------------------------------------
    for ts in range(START + 30, END, 60):
        small = DEPLOY < ts < ROLLBACK
        waiting = int(severity(ts) * rng.randint(20, 60)) if small else 0
        size = 5 if small else 20
        in_use = size if waiting else rng.randint(3, min(size, 9))
        log(ts, "INFO", "db.pool", "pool stats size=%d in_use=%d waiting=%d" % (size, in_use, waiting))
    for ts in range(START + 120, END, 300):
        log(ts + rng.random(), "INFO", "jobs.scheduler",
            "Job cleanup_sessions finished in %d ms, removed %d rows" % (
                rng.randint(40, 400), rng.randint(0, 90)))

    # --- traffic --------------------------------------------------------
    ts = float(START)
    while ts < END:
        ts += rng.expovariate(2.5)  # about 2.5 requests per second
        sev = severity(ts)
        user = rng.randint(1000, 9999)
        kind = rng.random()

        if kind < 0.62:  # browsing: served from cache, unaffected by the pool
            product = rng.randint(1, 400)
            ms = rng.randint(8, 60)
            log(ts, "INFO", "api.request",
                "GET /api/products/%d status=200 duration=%dms user=%d" % (product, ms, user))
            if rng.random() < 0.3:
                log(ts - 0.002, "DEBUG", "cache", "hit key=product:%d ttl=%ds" % (
                    product, rng.randint(1, 300)))
            hit(ts, "GET", "/api/products/%d" % product, 200, ms / 1000.0)

        elif kind < 0.92:  # ordering: needs a database connection
            if rng.random() < sev * 0.8:
                waiting = rng.randint(8, 60)
                ms = 5000 + rng.randint(1, 40)
                log(ts, "WARN", "db.pool",
                    "Connection pool exhausted (size=5, in_use=5, waiting=%d)" % waiting)
                log(ts + 5.0, "ERROR", "db.pool",
                    "Timeout acquiring connection after 5000 ms", _TRACEBACK)
                log(ts + 5.001, "ERROR", "orders.service",
                    "Failed to create order for user=%d: PoolTimeout" % user)
                log(ts + 5.002, "ERROR", "api.request",
                    "POST /api/orders status=500 duration=%dms user=%d" % (ms, user))
                hit(ts + 5.002, "POST", "/api/orders", 500, ms / 1000.0)
            else:
                ms = rng.randint(40, 180) + int(sev * rng.randint(500, 3500))
                log(ts, "INFO", "api.request",
                    "POST /api/orders status=201 duration=%dms user=%d" % (ms, user))
                hit(ts, "POST", "/api/orders", 201, ms / 1000.0)
            # payment retries exist on a normal night too, but multiply
            # when orders are slow
            if rng.random() < 0.012 + sev * 0.12:
                log(ts + 0.3, "WARN", "payments.client",
                    "Retrying charge attempt=%d order=%d" % (rng.randint(2, 4), rng.randint(50000, 59999)))

        elif kind < 0.97:  # health checks
            if rng.random() < sev * 0.5:
                log(ts, "ERROR", "health", "Readiness probe failed: database check timed out")
                hit(ts, "GET", "/health", 503, 2.0)
            else:
                hit(ts, "GET", "/health", 200, 0.002)

        else:  # background noise that has nothing to do with the incident
            if rng.random() < 0.5:
                code = rng.choice(["SAVE10", "WELCOME", "BLACKFRIDAY", "FREESHIP"])
                log(ts, "WARN", "api.validation",
                    "Invalid coupon code %s for user=%d" % (code, user))
                hit(ts, "POST", "/api/coupons", 422, 0.02)
            elif rng.random() < 0.12:
                log(ts, "ERROR", "mail.smtp",
                    "Failed to send receipt to user=%d: SMTP timeout after 30 s" % user)

    app.sort(key=lambda item: item[0])
    access.sort(key=lambda item: item[0])
    return [line for _, lines in app for line in lines], [line for _, line in access]


def write(directory: str, seed: int = 7) -> List[str]:
    os.makedirs(directory, exist_ok=True)
    app, access = generate(seed)
    paths = []
    for name, lines in (("app.log", app), ("access.log", access)):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        paths.append(path)
    return paths
