import calendar
import gzip

from trag.parse import (Reader, detect_format, parse_access, parse_app, parse_json,
                        parse_syslog, parse_time)

T = calendar.timegm((2026, 11, 16, 3, 27, 14))


def test_python_logging_line():
    e = parse_app("2026-11-16 03:27:14,512 ERROR db.pool: Timeout acquiring connection after 5000 ms")
    assert (e.level, e.source, e.message) == ("error", "db.pool",
                                              "Timeout acquiring connection after 5000 ms")
    assert abs(e.ts - (T + 0.512)) < 1e-6


def test_log4j_line_with_thread_and_dash_separator():
    e = parse_app("2026-11-16 03:27:14.001 [http-nio-8080-exec-3] WARN  com.shop.Pool - pool is full")
    assert (e.level, e.source, e.message) == ("warn", "com.shop.Pool", "pool is full")


def test_bracketed_level_and_iso_timestamp_with_zone():
    e = parse_app("2026-11-16T05:27:14+02:00 [ERROR] something broke")
    assert e.level == "error" and e.message == "something broke"
    assert e.ts == T                      # 05:27 at +02:00 is 03:27 UTC


def test_a_capitalised_first_word_is_not_mistaken_for_a_logger_name():
    e = parse_app("2026-11-16 03:27:14 ERROR Timeout: no free connection")
    assert e.source == "" and e.message == "Timeout: no free connection"


def test_line_without_level_gets_one_from_its_words():
    assert parse_app("2026-11-16 03:27:14 connection refused by upstream").level == "error"
    assert parse_app("2026-11-16 03:27:14 retrying in 5 s").level == "warn"
    assert parse_app("2026-11-16 03:27:14 listening on :8080").level == "info"


def test_access_log_line():
    e = parse_access('10.0.0.7 - - [16/Nov/2026:03:27:14 +0000] "POST /api/orders?x=1 HTTP/1.1" '
                     '500 512 "-" "curl/8" 5.003')
    assert e.ts == T and e.level == "error"
    assert e.message == "POST /api/orders status=500"       # query string dropped
    assert e.fields == {"ip": "10.0.0.7", "method": "POST", "path": "/api/orders",
                        "status": 500, "bytes": 512, "request_time": 5.003}
    assert parse_access('1.2.3.4 - - [16/Nov/2026:03:27:14 +0100] "GET / HTTP/1.1" 404 -').level == "warn"


def test_syslog_line_and_year_rollover():
    now = calendar.timegm((2027, 1, 2, 0, 0, 0))
    e = parse_syslog("Dec 31 23:59:58 web-1 sshd[812]: Failed password for root", _now=now)
    assert e.ts == calendar.timegm((2026, 12, 31, 23, 59, 58))   # not December 2027
    assert (e.source, e.level, e.fields) == ("sshd", "error", {"host": "web-1", "pid": 812})


def test_json_line_with_epoch_millis_and_extra_fields():
    e = parse_json('{"ts": %d, "level": "warning", "msg": "slow query", "logger": "db", "ms": 912}'
                   % (T * 1000))
    assert (e.ts, e.level, e.source, e.message, e.fields) == (T, "warn", "db", "slow query",
                                                              {"ms": 912})
    assert parse_json("not json") is None and parse_json("[1, 2]") is None


def test_parse_time():
    assert parse_time("2026-11-16T03:27:14Z") == T
    assert parse_time("nonsense") is None


def test_format_detection_tolerates_stack_traces():
    lines = ["2026-11-16 03:27:14 ERROR a.b: boom", "Traceback (most recent call last):",
             '  File "x.py", line 1, in f', "ValueError: bad", "2026-11-16 03:27:15 INFO a.b: ok"]
    assert detect_format(lines) == "app"
    assert detect_format(['{"msg": "a"}', '{"msg": "b"}']) == "json"
    assert detect_format(["just some text", "more text"]) == "plain"
    assert detect_format([]) == "plain"


def test_stack_trace_is_folded_into_its_event(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("2026-11-16 03:27:14 ERROR a.b: boom\n"
                    "Traceback (most recent call last):\n"
                    '  File "x.py", line 1, in f\n'
                    "shop.db.PoolTimeout: no free connection\n"
                    "\n"
                    "2026-11-16 03:27:15 INFO a.b: ok\n")
    reader = Reader([str(path)])
    events = list(reader)
    assert [e.message for e in events] == ["boom", "ok"]
    assert events[0].extra_lines == 3 and events[0].fields["exception"] == "shop.db.PoolTimeout"
    assert [e.line_no for e in events] == [1, 6]
    assert (reader.lines, reader.continuation_lines, reader.formats[str(path)]) == (6, 3, "app")


def test_gzip_files_are_read_transparently(tmp_path):
    path = tmp_path / "app.log.gz"
    with gzip.open(path, "wt") as f:
        f.write("2026-11-16 03:27:14 INFO a.b: hello\n")
    assert [e.message for e in Reader([str(path)])] == ["hello"]
