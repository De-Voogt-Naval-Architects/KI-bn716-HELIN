"""HTTP destination: readings posted to the HDC http_south endpoint (tests/fake_hdc.py)."""

import ssl
from datetime import datetime, timezone

import pytest

import extract
from destinations import HttpClient, asset_and_datapoint, fledge_timestamp
from fake_hdc import serve as serve_hdc
from fake_datalogger import value_at

UTC = timezone.utc
SIG = "Fields.Navigation.GPS.SpeedOverGround"
SIG2 = "Fields.ElectricalSystem.Drive5.DrivePowerPro5"

# The node template's container create options, verbatim apart from paths.
TEMPLATE_CMD = [
    "--addr", "192.168.192.5:50052",
    "--signals", "/config/tags",
    "--loop",
    "--rate", "1000",
    "--dest-type", "http",
    "--dest-addr", "https://HelinDataCollector:6684/sensor-reading",
    "--insecure",
]


@pytest.fixture
def hdc():
    server, port, store = serve_hdc(0)
    yield f"http://localhost:{port}/sensor-reading", store
    server.shutdown()


def args_for(addr, out, *extra):
    p = extract.build_parser()
    a = p.parse_args(["--addr", addr, "--out", str(out), "--signals", "", "--no-helin", *extra])
    extract.validate(p, a)
    return a


def test_template_cmd_parses_to_follow_over_https(tmp_path):
    p = extract.build_parser()
    a = p.parse_args(TEMPLATE_CMD)
    extract.validate(p, a)
    assert (a.mode, a.rate, a.dest_type, a.insecure) == ("follow", 1000.0, "http", True)
    assert a.addr == "192.168.192.5:50052" and a.signals == "/config/tags"
    client = HttpClient(a.dest_addr, insecure=a.insecure)
    assert client.context.verify_mode == ssl.CERT_NONE and client.context.check_hostname is False
    assert HttpClient(a.dest_addr).context.verify_mode == ssl.CERT_REQUIRED


def test_follow_posts_readings_and_advances_cursor(logger_server, hdc, tmp_path):
    addr, _ = logger_server
    url, store = hdc
    a = args_for(addr, tmp_path, "--signal", SIG, "--signal", SIG2, "--signal", "Missing.Tag",
                 "--loop", "--dest-type", "http", "--dest-addr", url, "--batch-size", "100",
                 "--start", "2026-04-01 12:00:00", "--lag", "0")
    state = tmp_path / ".state" / "follow-http.json"
    rows = extract.follow_cycle(extract.DataLogger(a.addr), a, [SIG, SIG2, "Missing.Tag"], tmp_path, state,
                                now=datetime(2026, 4, 1, 12, 5, tzinfo=UTC),
                                make_sink=extract.sink_factory(a, tmp_path))
    # 300 samples per signal, minus the 5 whole-minute ones the fake marks unavailable
    assert rows == 2 * 295 and len(store.readings) == 590
    assert store.posts == 2 * 3                                # batches of 100
    first = store.readings[0]
    assert first == {"timestamp": "2026-04-01 12:00:01.000000+00:00", "asset": SIG,
                     "readings": {"value": value_at(int(datetime(2026, 4, 1, 12, 0, 1, tzinfo=UTC).timestamp() * 1000))}}
    assert not list(tmp_path.glob("20*"))                     # no CSV files in http mode
    assert extract.STATUS["signals_ok"] == 2 and extract.STATUS["signals_no_data"] == 1
    assert state.exists()


def test_destination_down_keeps_cursor_and_retries(logger_server, hdc, tmp_path):
    addr, _ = logger_server
    url, store = hdc
    a = args_for(addr, tmp_path, "--signal", SIG, "--dest-type", "http", "--dest-addr", url,
                 "--start", "2026-04-01 12:00:00", "--lag", "0")
    state = tmp_path / ".state" / "follow-http.json"
    make_sink = extract.sink_factory(a, tmp_path)
    logger = extract.DataLogger(a.addr)

    store.fail_next = 1
    extract.follow_cycle(logger, a, [SIG], tmp_path, state, now=datetime(2026, 4, 1, 12, 1, tzinfo=UTC),
                         make_sink=make_sink)
    assert "HTTP 503" in extract.STATUS["last_error"] and not state.exists()
    assert store.readings == []

    extract.follow_cycle(logger, a, [SIG], tmp_path, state, now=datetime(2026, 4, 1, 12, 2, tzinfo=UTC),
                         make_sink=make_sink)
    stamps = [r["timestamp"] for r in store.readings]
    assert stamps[0] == "2026-04-01 12:00:01.000000+00:00"    # nothing lost after the outage
    assert stamps[-1] == "2026-04-01 12:01:59.000000+00:00" and len(stamps) == len(set(stamps))


def test_unreachable_destination_is_a_clear_error(logger_server, tmp_path):
    addr, _ = logger_server
    a = args_for(addr, tmp_path, "--signal", SIG, "--dest-type", "http",
                 "--dest-addr", "http://127.0.0.1:9/sensor-reading", "--start", "2026-04-01 12:00:00", "--lag", "0")
    extract.follow_cycle(extract.DataLogger(a.addr), a, [SIG], tmp_path, tmp_path / ".state" / "f.json",
                         now=datetime(2026, 4, 1, 12, 1, tzinfo=UTC), make_sink=extract.sink_factory(a, tmp_path))
    assert "cannot reach http://127.0.0.1:9/sensor-reading" in extract.STATUS["last_error"]


def test_range_over_http_and_send_unavailable(logger_server, hdc, tmp_path):
    addr, _ = logger_server
    url, store = hdc
    extract.main(["--addr", addr, "--out", str(tmp_path), "--signals", "", "--no-helin", "--signal", SIG,
                  "--dest-type", "http", "--dest-addr", url, "--send-unavailable", "--asset-mode", "group",
                  "--start", "2026-04-01 12:00:00", "--end", "2026-04-01 12:02:00"])
    assert len(store.readings) == 120                         # unavailable ones included
    assert {r["asset"] for r in store.readings} == {"Fields.Navigation.GPS"}
    nulls = [r for r in store.readings if r["readings"]["SpeedOverGround"] is None]
    assert len(nulls) == 2                                    # 12:00:00 and 12:01:00
    assert list((tmp_path / ".state").glob("range_http_*.json"))


def test_all_empty_in_subscription_mode_raises_a_visible_warning(hdc, tmp_path):
    from fake_datalogger import serve
    server, port, _ = serve(0, empty_at_1000=True)
    url, store = hdc
    try:
        a = args_for(f"localhost:{port}", tmp_path, "--signal", SIG, "--signal", SIG2, "--rate", "1000",
                     "--dest-type", "http", "--dest-addr", url, "--start", "2026-04-01 12:00:00", "--lag", "0")
        extract.follow_cycle(extract.DataLogger(a.addr), a, [SIG, SIG2], tmp_path, tmp_path / ".state" / "f.json",
                             now=datetime(2026, 4, 1, 12, 1, tzinfo=UTC), make_sink=extract.sink_factory(a, tmp_path))
    finally:
        server.stop(None)
    assert store.readings == []
    assert "--rate 1000" in extract.STATUS["warning"] and "stored no samples" in extract.STATUS["warning"]

    import helin_status
    health = helin_status.Handler(a, [SIG, SIG2], extract.STATUS).on_get_health({})
    assert health["warning"] and {m["name"]: m["value"] for m in health["metrics"]}["healthy"] == 0


def test_asset_modes_and_timestamps():
    assert asset_and_datapoint(SIG, "signal", "value") == (SIG, "value")
    assert asset_and_datapoint(SIG, "group", "value") == ("Fields.Navigation.GPS", "SpeedOverGround")
    assert fledge_timestamp(1777485601000) == "2026-04-29 18:00:01.000000+00:00"


@pytest.mark.parametrize("argv, message", [
    (["--loop", "--mode", "range", "--start", "2026-04-01", "--end", "2026-04-02"], "contradict"),
    (["--dest-type", "http"], "needs --dest-addr"),
    (["--dest-type", "http", "--dest-addr", "HelinDataCollector:6684"], "must start with http"),
])
def test_invalid_combinations(argv, message, capsys):
    p = extract.build_parser()
    with pytest.raises(SystemExit):
        extract.validate(p, p.parse_args(argv))
    assert message in capsys.readouterr().err
