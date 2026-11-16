from trag.analyze import Analysis, baseline, diff

from conftest import ev

T0 = 1_800_000_000 - 1_800_000_000 % 86400      # a midnight, so buckets line up


def quiet_then_outage():
    """60 minutes; 2 info/min throughout; minutes 30-39 add 40 errors/min."""
    events = []
    for minute in range(60):
        t = T0 + minute * 60
        events += [ev(t + 5, "heartbeat ok from worker 1"), ev(t + 35, "heartbeat ok from worker 2")]
        if 30 <= minute < 40:
            events += [ev(t + s, "Timeout acquiring connection after 5000 ms", "error")
                       for s in range(0, 60, 2)]
            events += [ev(t + s, "Timeout acquiring connection after 5000 ms", "error")
                       for s in range(1, 20, 2)]
    events.append(ev(T0 + 29 * 60 + 10, "Starting release v2.14.0 (build 8841)"))
    events.append(ev(T0 + 5 * 60, "Starting backup of volume data"))
    return sorted(events, key=lambda e: e.ts)


def test_baseline_ignores_the_outlier_it_is_looking_for():
    median, scale = baseline([2, 3, 2, 3, 2, 400, 3, 2])
    assert median == 2.5 and scale < 2          # a mean would be 52, a stdev 140
    assert baseline([0] * 50) == (0.0, 1.0)     # never a zero scale
    assert baseline([]) == (0.0, 1.0)


def test_bucket_size_adapts_to_the_span():
    a = Analysis().extend(quiet_then_outage())
    assert a.bucket == 30 and a.bucket_count == 120
    assert sum(a.timeline()["total"]) == a.events


def test_error_onset_finds_the_outage_window():
    a = Analysis().extend(quiet_then_outage())
    onset = a.error_onset()
    assert onset["start"] == T0 + 30 * 60 and onset["end"] == T0 + 40 * 60
    assert onset["errors"] == 400 and onset["normal"] == 0


def test_no_onset_when_errors_are_steady():
    events = [ev(T0 + i * 20, "disk check failed on node 3", "error") for i in range(180)]
    assert Analysis().extend(events).error_onset() is None


def test_rare_events_just_before_the_onset_are_surfaced():
    a = Analysis().extend(quiet_then_outage())
    before = a.before_onset(a.error_onset())
    assert [o["message"] for o in before] == ["Starting release v2.14.0 (build 8841)"]
    # the backup at minute 5 is rare too, but it is not near the onset


def test_new_templates_are_those_absent_from_the_first_quarter():
    a = Analysis().extend(quiet_then_outage())
    assert [t.text for t in a.new_templates()] == [
        "Starting release v2.<NUM> (build <NUM>)",
        "Timeout acquiring connection after <NUM> ms"]


def test_top_error_templates_and_levels():
    a = Analysis().extend(quiet_then_outage())
    top = a.top_templates(5, errors_only=True)
    assert [(t.text, t.count, t.level) for t in top] == [
        ("Timeout acquiring connection after <NUM> ms", 400, "error")]
    assert a.levels["error"] == 400 and a.levels["info"] == 122


def test_spike_in_a_pattern_that_already_existed():
    events = []
    for minute in range(60):
        n = 30 if minute == 45 else 1
        events += [ev(T0 + minute * 60 + i, "Retrying charge attempt=%d order=%d now" % (i, minute),
                      "warn") for i in range(n)]
    spikes = Analysis().extend(events).spikes()
    assert len(spikes) == 1 and spikes[0]["peak"] == 30 and spikes[0]["at"] == T0 + 45 * 60


def test_events_without_timestamps_are_counted_but_not_charted():
    a = Analysis().extend([ev(None, "plain line one two three"), ev(None, "plain line one two four")])
    assert (a.events, a.untimed, a.bucket_count) == (2, 2, 0)
    assert a.error_onset() is None and a.new_templates() == []


def test_requests_view_groups_paths_by_endpoint():
    events = []
    for i in range(20):
        events.append(ev(T0 + i, "GET /api/products/%d status=200" % i, ip="1.1.1.1", method="GET",
                         path="/api/products/%d" % i, status=200, request_time=0.01 * (i + 1)))
    for i in range(5):
        events.append(ev(T0 + i, "POST /api/orders status=500", "error", ip="2.2.2.2",
                         method="POST", path="/api/orders", status=500, request_time=5.0))
    r = Analysis().extend(events).requests()
    assert r["classes"] == {"2xx": 20, "5xx": 5}
    assert r["failing"] == [{"path": "/api/orders", "count": 5, "errors": 5, "error_pct": 100.0,
                             "p95": 5.0}]
    products = [x for x in r["slowest"] if x["path"] == "/api/products/<NUM>"][0]
    assert products["count"] == 20 and products["p95"] == 0.19     # 19th of 20, nearest rank
    assert r["clients"][0] == ("1.1.1.1", 20)


def test_diff_classifies_new_gone_more_and_less():
    def lines(spec):
        return [ev(T0, message, level) for message, level, n in spec for _ in range(n)]

    before = lines([("request served in 5 ms for client 9", "info", 100),
                    ("cache warmed for region eu west", "info", 10),
                    ("Retrying charge attempt=2 order=7 now", "warn", 2)])
    after = lines([("request served in 7 ms for client 3", "info", 100),
                   ("Timeout acquiring connection after 5000 ms", "error", 40),
                   ("Retrying charge attempt=3 order=8 now", "warn", 30)])
    result = diff(before, after)
    kinds = {r["template"].text: (r["kind"], r["before"], r["after"]) for r in result["rows"]}
    assert kinds == {
        "Timeout acquiring connection after <NUM> ms": ("new", 0, 40),
        "Retrying charge attempt=<NUM> order=<NUM> now": ("more", 2, 30),
        "cache warmed for region eu west": ("gone", 10, 0),
    }
    assert result["rows"][0]["kind"] == "new"          # most important first
    assert (result["before"], result["after"]) == (112, 170)
