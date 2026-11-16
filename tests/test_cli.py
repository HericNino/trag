import io
import re

from trag import cli, sqlstore
from trag.parse import Reader
from trag.text import Out, sparkline, table


def run(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_summary_tells_the_story_of_the_demo_incident(demo_logs, capsys):
    code, out, _ = run(capsys, "summary", demo_logs["app"], "--no-color")
    assert code == 0
    assert "Errors left their normal range at 03:27:00 and returned to it at 03:48:00." in out
    # the deploy two minutes earlier is surfaced as a rare event
    assert re.search(r"03:25:10 +line \d+ +Starting release v2\.14\.0", out)
    assert "Loaded settings db.pool.max_size=5 db.pool.timeout_ms=5000" in out
    assert "Timeout acquiring connection after <NUM> ms" in out
    assert "shop.db.PoolTimeout" in out
    assert "stack-trace lines folded" in out
    # background noise that predates the incident is not reported as new
    new_section = out.split("New patterns")[1].split("Top error patterns")[0]
    assert "SMTP timeout" not in new_section and "Invalid coupon" not in new_section


def test_show_reveals_the_config_change(demo_logs, capsys):
    _, out, _ = run(capsys, "patterns", demo_logs["app"], "--no-color", "--top", "50")
    pattern_id = [l.split()[0] for l in out.splitlines() if "Loaded settings" in l][0]
    code, out, _ = run(capsys, "show", demo_logs["app"], pattern_id, "--no-color")
    assert code == 0
    assert re.search(r"from 03:25:11 +1× +5, 5000", out)
    assert re.search(r"from 03:47:01 +1× +20, 5000", out)


def test_diff_at_the_deploy_time(demo_logs, capsys):
    code, out, _ = run(capsys, "diff", demo_logs["app"], "--at", "03:25", "--no-color")
    assert code == 0
    new = out.split("New (never seen before)")[1].split("More frequent")[0]
    assert "Connection pool exhausted" in new and "status=500" in new
    assert "Retrying charge" in out.split("More frequent")[1]
    assert "status=200" not in out                 # ordinary traffic did not change


def test_diff_usage_errors(demo_logs, capsys):
    code, _, err = run(capsys, "diff", demo_logs["app"])
    assert code == 2 and "either" in err


def test_sql_over_the_log(demo_logs, capsys):
    code, out, _ = run(capsys, "sql", demo_logs["access"],
                       "SELECT path, COUNT(*) AS n, ROUND(AVG(request_time), 1) AS avg_s "
                       "FROM events WHERE status = 500 GROUP BY path", "--no-color")
    assert code == 0
    row = out.splitlines()[1].split()
    assert row[0] == "/api/orders" and int(row[1]) > 500 and row[2] == "5"
    code, _, err = run(capsys, "sql", demo_logs["app"], "SELECT nope FROM events")
    assert code == 2 and "SQL error" in err
    code, out, _ = run(capsys, "sql", demo_logs["app"])       # no query: print the schema
    assert code == 0 and "templates" in out


def test_sql_templates_table_agrees_with_events(demo_logs):
    db, analysis = sqlstore.load(Reader([demo_logs["app"]]))
    _, rows = sqlstore.run(db, "SELECT t.count, COUNT(e.line) FROM templates t "
                               "JOIN events e USING (template_id) GROUP BY t.template_id")
    assert rows and all(a == b for a, b in rows)
    assert sum(a for a, _ in rows) == analysis.events


def test_filters(demo_logs, capsys):
    code, out, _ = run(capsys, "summary", demo_logs["app"], "--since", "03:30", "--until",
                       "03:31", "--level", "error", "--no-color")
    assert code == 0 and "03:30:" in out.splitlines()[2] and "info" not in out.splitlines()[3]
    code, _, err = run(capsys, "summary", demo_logs["app"], "--since", "tomorrow")
    assert code == 2 and "cannot read time" in err
    code, _, err = run(capsys, "summary", demo_logs["app"], "--since", "23:00")
    assert code == 2 and "no events" in err


def test_report_is_self_contained_and_escapes_log_content(tmp_path, capsys):
    log = tmp_path / "evil.log"
    log.write_text("".join(
        "2026-11-16 03:%02d:00 ERROR web: <script>alert(%d)</script> failed for request\n" % (m, m)
        for m in range(30)))
    out_file = tmp_path / "r.html"
    code, _, _ = run(capsys, "report", str(log), "-o", str(out_file))
    html = out_file.read_text()
    assert code == 0 and "<script>alert" not in html and "&lt;script&gt;alert" in html
    assert "http://" not in html and "https://" not in html       # no external assets
    assert html.count("<svg") >= 2


def test_stdin_and_missing_file(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(
        "Nov 16 03:27:14 web-1 sshd[1]: Failed password for root from 10.0.0.1\n" * 3))
    code, out, _ = run(capsys, "patterns", "-", "--no-color")
    assert code == 0 and "Failed password for root from <IP>" in out
    code, _, err = run(capsys, "summary", "/no/such/file.log")
    assert code == 2 and "cannot open" in err
    assert cli.main([]) == 2


def test_sparkline_keeps_single_bucket_spikes():
    assert sparkline([0, 0, 0]) == "▁▁▁"
    assert sparkline([0, 1, 8]) == " ▁█"
    wide = [0] * 300
    wide[150] = 9
    line = sparkline(wide, width=60)
    assert len(line) == 60 and line.count("█") == 1


def test_table_aligns_numbers_right():
    out = Out(color=False, width=80)
    table(["name", "n"], [("a", 5), ("bbb", 1234)], out)
    assert out.lines == ["name     n", "a        5", "bbb   1234"]
