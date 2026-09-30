"""End-to-end tests against the fake DataLogger (tests/fake_datalogger.py)."""

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import extract
from fake_datalogger import serve, value_at

UTC = timezone.utc
SIG = "Fields.ElectricalSystem.AC2.ACPowerNominal2"
TEMP = "Fields.Engines.EnginePro1.EngineExhaustTemperature1Pro1"


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def args_for(addr, out, *extra):
    p = extract.build_parser()
    a = p.parse_args(["--addr", addr, "--out", str(out), "--signals", "", "--no-helin", *extra])
    extract.validate(p, a)
    return a


# --------------------------------------------------------------------------
# range mode - MVP behaviour
# --------------------------------------------------------------------------

def test_range_export_one_csv_per_signal(logger_server, tmp_path):
    addr, _ = logger_server
    extract.main(["--addr", addr, "--out", str(tmp_path), "--signals", "", "--no-helin",
                  "--signal", SIG, "--signal", TEMP, "--signal", "Missing.Tag",
                  "--start", "2026-04-01 00:00:00", "--end", "2026-04-01 00:10:00"])

    rows = read_rows(tmp_path / f"{SIG}.csv")
    assert list(rows[0]) == extract.CSV_HEADER
    # 600 samples in [00:00, 00:10) plus the preceding sample at 23:59:59
    assert len(rows) == 601
    assert rows[0]["DatetimeUTC"] == "2026-03-31 23:59:59.000"
    assert rows[-1]["DatetimeUTC"] == "2026-04-01 00:09:59.000"
    first_ok = next(r for r in rows if r["Available"] == "True")
    assert float(first_ok["Value"]) == value_at(int(first_ok["Timestamp_ms"]))
    assert all(r["Value"] == "" for r in rows if r["Available"] == "False")

    temp = read_rows(tmp_path / f"{TEMP}.csv")
    assert temp[1]["Unit"] == "K" and temp[1]["Name"] == "EngineExhaustTemperature1Pro1"
    assert not (tmp_path / "Missing.Tag.csv").exists()          # no empty files
    assert extract.STATUS["signals_ok"] == 2 and extract.STATUS["signals_no_data"] == 1


def test_range_is_not_repeated_after_restart(logger_server, tmp_path):
    addr, servicer = logger_server
    argv = ["--addr", addr, "--out", str(tmp_path), "--signals", "", "--no-helin", "--signal", SIG,
            "--start", "2026-04-01", "--end", "2026-04-01 00:05:00"]
    extract.main(argv)
    assert len(list((tmp_path / ".state").glob("range_*.json"))) == 1
    calls = len(servicer.requests)

    (tmp_path / f"{SIG}.csv").unlink()
    extract.main(argv)                                          # restarted container
    assert len(servicer.requests) == calls
    assert not (tmp_path / f"{SIG}.csv").exists()

    extract.main(argv + ["--force"])
    assert (tmp_path / f"{SIG}.csv").exists()


def test_seconds_epoch_fallback_gives_milliseconds(tmp_path):
    server, port, _ = serve(0, reject_ms=True)
    try:
        extract.main(["--addr", f"localhost:{port}", "--out", str(tmp_path), "--signals", "", "--no-helin",
                      "--signal", SIG, "--start", "2026-04-01", "--end", "2026-04-01 00:01:00"])
    finally:
        server.stop(None)
    rows = read_rows(tmp_path / f"{SIG}.csv")
    assert len(rows) == 61
    assert rows[1]["Timestamp_ms"] == str(int(datetime(2026, 4, 1, tzinfo=UTC).timestamp() * 1000))


def test_chunking_does_not_change_output(logger_server, tmp_path):
    addr, _ = logger_server
    base = ["--addr", addr, "--signals", "", "--no-helin", "--signal", SIG,
            "--start", "2026-04-01", "--end", "2026-04-01 01:00:00", "--force"]
    extract.main(base + ["--out", str(tmp_path / "a")])
    extract.main(base + ["--out", str(tmp_path / "b"), "--chunk-minutes", "7"])
    a, b = read_rows(tmp_path / "a" / f"{SIG}.csv"), read_rows(tmp_path / "b" / f"{SIG}.csv")
    # each chunk asks for its preceding sample, so compare the distinct timeline
    assert sorted({r["Timestamp_ms"] for r in a}) == sorted({r["Timestamp_ms"] for r in b})


# --------------------------------------------------------------------------
# follow mode - continuous, resumable, daily files
# --------------------------------------------------------------------------

def test_follow_splits_days_and_resumes_without_gaps(logger_server, tmp_path):
    addr, _ = logger_server
    a = args_for(addr, tmp_path, "--signal", SIG, "--signal", "Missing.Tag",
                 "--start", "2026-04-01 23:50:00", "--lag", "60")
    state = tmp_path / ".state" / "follow.json"
    logger = extract.DataLogger(a.addr)

    extract.follow_cycle(logger, a, [SIG, "Missing.Tag"], tmp_path, state,
                         now=datetime(2026, 4, 2, 0, 10, tzinfo=UTC))
    day1 = read_rows(tmp_path / "2026-04-01" / f"{SIG}.csv")
    day2 = read_rows(tmp_path / "2026-04-02" / f"{SIG}.csv")
    assert (len(day1), len(day2)) == (600, 540)                  # 23:50..00:09 (lag 60 s)
    saved = json.loads(state.read_text())
    assert saved["signals"][SIG] == int(datetime(2026, 4, 2, 0, 9, tzinfo=UTC).timestamp() * 1000)
    assert extract.STATUS["signals_no_data"] == 1

    # A restarted container: new logger, same state file, later clock.
    logger = extract.DataLogger(a.addr)
    extract.follow_cycle(logger, a, [SIG, "Missing.Tag"], tmp_path, state,
                         now=datetime(2026, 4, 2, 0, 20, tzinfo=UTC))
    day2 = read_rows(tmp_path / "2026-04-02" / f"{SIG}.csv")
    stamps = [int(r["Timestamp_ms"]) for r in day1 + day2]
    assert stamps == list(range(stamps[0], stamps[-1] + 1, 1000))  # no gaps, no duplicates
    assert day2[-1]["DatetimeUTC"] == "2026-04-02 00:18:59.000"
    assert sum(1 for r in day2 if r["Timestamp_ms"] == "Timestamp_ms") == 0  # header only once


def test_follow_backfill_without_state_or_start(logger_server, tmp_path):
    addr, _ = logger_server
    a = args_for(addr, tmp_path, "--signal", SIG, "--backfill-minutes", "5", "--lag", "0")
    extract.follow_cycle(extract.DataLogger(a.addr), a, [SIG], tmp_path, tmp_path / ".state" / "f.json",
                         now=datetime(2026, 4, 1, 12, 0, tzinfo=UTC))
    assert len(read_rows(tmp_path / "2026-04-01" / f"{SIG}.csv")) == 300


def test_follow_rate_reduces_samples(logger_server, tmp_path):
    addr, _ = logger_server
    a = args_for(addr, tmp_path, "--signal", SIG, "--start", "2026-04-01 12:00:00", "--lag", "0", "--rate", "0.1")
    extract.follow_cycle(extract.DataLogger(a.addr), a, [SIG], tmp_path, tmp_path / ".state" / "f.json",
                         now=datetime(2026, 4, 1, 12, 10, tzinfo=UTC))
    assert len(read_rows(tmp_path / "2026-04-01" / f"{SIG}.csv")) == 60


def test_prune_keeps_recent_days(tmp_path):
    for day in ("2026-03-28", "2026-03-30", "2026-04-01", ".state"):
        (tmp_path / day).mkdir()
    extract.prune(tmp_path, 2, datetime(2026, 4, 1, tzinfo=UTC))
    assert sorted(p.name for p in tmp_path.iterdir()) == [".state", "2026-03-30", "2026-04-01"]


# --------------------------------------------------------------------------
# options
# --------------------------------------------------------------------------

def test_mode_defaults_and_validation():
    p = extract.build_parser()
    a = p.parse_args(["--start", "2026-04-01", "--end", "2026-04-02"])
    extract.validate(p, a)
    assert a.mode == "range"
    a = p.parse_args([])
    extract.validate(p, a)
    assert a.mode == "follow"
    with pytest.raises(SystemExit):
        a = p.parse_args(["--mode", "range", "--start", "2026-04-02", "--end", "2026-04-01"])
        extract.validate(p, a)


def test_environment_variables_set_defaults(monkeypatch):
    monkeypatch.setenv("DL_ADDR", "10.0.51.25:50715")
    monkeypatch.setenv("DL_RATE", "0.5")
    monkeypatch.setenv("DL_SIGNAL", f"{SIG}, {TEMP}")
    monkeypatch.setenv("DL_INTERVAL", "120")
    a = extract.build_parser().parse_args([])
    assert (a.addr, a.rate, a.interval) == ("10.0.51.25:50715", 0.5, 120.0)
    assert a.signal == [SIG, TEMP]
    # an explicit flag still wins
    assert extract.build_parser().parse_args(["--rate", "2"]).rate == 2.0


def test_read_signals_comments_and_duplicates(tmp_path):
    f = tmp_path / "s.txt"
    f.write_text(f"# header\n{SIG}\n\n{TEMP}  # trailing comment\n{SIG}\n", encoding="utf-8")
    assert extract.read_signals(["Extra.Tag"], str(f)) == ["Extra.Tag", SIG, TEMP]


def test_bundled_bn716_signal_list():
    path = Path(__file__).resolve().parents[1] / "signals" / "bn716_tags_subset.txt"
    signals = extract.read_signals([], str(path))
    assert len(signals) == 169
    assert "Fields.Navigation.GPS.SpeedOverGround" in signals
    assert all(s.startswith("Fields.") for s in signals)


def test_listen_address_is_dialled_locally():
    assert extract.normalise_addr("0.0.0.0:50715") == "localhost:50715"
    assert extract.normalise_addr("10.0.51.25:50715") == "10.0.51.25:50715"


def test_unwritable_output_explains_the_fix(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(SystemExit, match="chown 1000:1000"):
        extract.check_writable(blocker / "out")


def test_health_handler_reports_status():
    import helin_status
    p = extract.build_parser()
    a = p.parse_args(["--addr", "10.0.51.25:50715"])
    extract.validate(p, a)
    h = helin_status.Handler(a, [SIG], extract.STATUS)
    extract.STATUS.update(connected=True, rows_total=42)
    health = h.on_get_health({})
    metrics = {m["name"]: m["value"] for m in health["metrics"]}
    assert metrics["datalogger_connected"] == 1 and metrics["rows_total"] == 42
    cfg = h.on_get_configuration({})["configuration"]
    assert cfg["addr"] == "10.0.51.25:50715" and cfg["signal_count"] == 1 and cfg["mode"] == "follow"
