"""Extract trend data from a Rhodium DataLogger over gRPC, on a Helin edge node.

Two modes:

  follow  (default) every --interval seconds, fetch what each signal logged since
          the last run, up to now - --lag, and append it to one CSV per signal
          per UTC day:  <out>/YYYY-MM-DD/<ClientSpecificId>.csv
          Progress is kept in <out>/.state/follow.json, so a restarted container
          carries on where it stopped instead of starting over.

  range   one export of --start..--end, one CSV per signal in <out> (the MVP's
          behaviour). A completed range is recorded in <out>/.state/ and is not
          repeated when the container restarts; --force re-runs it.

When a range finishes, --on-finish decides what happens: `exit` (a CLI run) or
`idle` (default under the Helin/IoT Edge runtime, which would otherwise restart
the container and loop).

Every flag can also come from an environment variable (DL_ADDR, DL_MODE, ...)
so the node template can use either the container Cmd or its environment.

Request/response handling follows the Rhodium Datalogger Manual (4381F-T016-DL)
section 1.2.5, "Query samples", as in the original container-mvp extract.py.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import signal as signals_module
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import grpc

import Trending_pb2
import Trending_pb2_grpc

# The manual puts the ceiling for a single request at 300 MB in wire format.
MAX_MSG = 512 * 1024 * 1024

# A failed readiness check waits this long before the next attempt.
CONNECT_RETRY_DELAY = 5

# Milliseconds first, deliberately: at high rates the logger holds several
# samples within one second and second-granularity timestamps flatten them onto
# the same instant (867,575 unfiltered samples collapsed to 543,315 distinct
# timestamps on a real logger). Seconds stays as a fallback.
EPOCHS = [
    ("ms", Trending_pb2.DLEpoch.Epoch_UnixMilliseconds),
    ("s", Trending_pb2.DLEpoch.Epoch_UnixSeconds),
]

# How the logger says "nothing here" or "I don't understand that request" -
# either way, worth trying the other epoch encoding before giving up.
NO_DATA = (
    grpc.StatusCode.NOT_FOUND,
    grpc.StatusCode.INVALID_ARGUMENT,
    grpc.StatusCode.OUT_OF_RANGE,
    grpc.StatusCode.UNKNOWN,
)

# A response carries its samples in whichever array matches the signal's
# datatype; only one is ever populated.
VALUE_ARRAYS = [
    "Values64Float",
    "Values32Float",
    "Values64",
    "Values32",
    "Values16",
    "Values8",
    "Values1",
]

CSV_HEADER = ["Timestamp_ms", "DatetimeUTC", "Value", "Available", "Unit", "Name", "ValueCSId"]

DEFAULT_SIGNALS = "/app/signals/bn716_tags_subset.txt"
ON_EDGE = "IOTEDGE_MODULEID" in os.environ

# Shared with helin_status.py for the portal's get_health / get_configuration.
STATUS = {
    "mode": None,
    "connected": False,
    "cycles": 0,
    "last_cycle_start": None,
    "last_cycle_end": None,
    "last_cycle_rows": 0,
    "rows_total": 0,
    "signals_ok": 0,
    "signals_no_data": 0,
    "signals_error": 0,
    "last_error": None,
    "range_done": None,
}
STOP = threading.Event()


def log(msg, err=False):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp}Z {msg}", file=sys.stderr if err else sys.stdout, flush=True)


def normalise_addr(addr):
    """Turn a listen address (0.0.0.0:<port>, as in DataLoggerGRPC.exe.config)
    into one a client can actually dial."""
    host, _, port = addr.rpartition(":")
    if host in ("0.0.0.0", "::", "[::]"):
        return f"localhost:{port}"
    return addr


class DataLogger:
    """Minimal client for the DataLoggerTrends service."""

    def __init__(self, addr, timeout=600, connect_attempts=0):
        self.addr = addr = normalise_addr(addr)
        self.timeout = timeout
        self.epoch = None  # settled on the first request the logger answers

        self.channel = grpc.insecure_channel(
            addr,
            options=[
                ("grpc.max_receive_message_length", MAX_MSG),
                ("grpc.max_send_message_length", MAX_MSG),
            ],
        )
        ready = grpc.channel_ready_future(self.channel)
        attempt = 1

        while not STOP.is_set():
            try:
                ready.result(timeout=15)
                break
            except grpc.FutureTimeoutError:
                if connect_attempts and attempt >= connect_attempts:
                    sys.exit(
                        f"No DataLogger answered at {addr} after {attempt} attempts.\n"
                        f"Check the address, that DataLoggerGRPC.exe is running, and that its\n"
                        f"rpcAddress is 0.0.0.0:<port> rather than 127.0.0.1:<port>."
                    )
                limit = f"/{connect_attempts}" if connect_attempts else ""
                log(f"No DataLogger answered at {addr} (attempt {attempt}{limit}); "
                    f"retrying in {CONNECT_RETRY_DELAY} seconds...", err=True)
                STOP.wait(CONNECT_RETRY_DELAY)
                attempt += 1

        STATUS["connected"] = True
        self.stub = Trending_pb2_grpc.DataLoggerTrendsStub(self.channel)

    def _request(self, csid, start, end, rate, epoch, include_preceding):
        tag, epoch_enum = epoch
        multiplier = 1 if tag == "s" else 1000

        request = Trending_pb2.GetTrendRequest()
        request.ValueCSId = csid
        request.SampleRate = rate
        request.Epoch = epoch_enum
        request.IncludePrecedingSample = include_preceding
        request.StartDateTime = int(start.timestamp() * multiplier)
        request.EndDateTime = int(end.timestamp() * multiplier) - 1

        return self.stub.GetTrend(request, timeout=self.timeout)

    def get_trend(self, csid, start, end, rate, include_preceding=True):
        """One window of data. Returns None when the logger has nothing for it.

        Anything that is not a "no data" answer is raised, so a broken address or
        an oversized response surfaces rather than being silently skipped.
        """
        for epoch in [self.epoch] if self.epoch else EPOCHS:
            try:
                response = self._request(csid, start, end, rate, epoch, include_preceding)
            except grpc.RpcError as exc:
                if exc.code() in NO_DATA:
                    continue  # no data, or this encoding was rejected
                raise
            self.epoch = epoch
            return response
        return None

    @property
    def epoch_tag(self):
        return self.epoch[0] if self.epoch else "s"


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def parse_time(text):
    """Accept 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS', always read as UTC."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Invalid time '{text}'. Use YYYY-MM-DD[ HH:MM:SS].")


def read_signals(signal_args, signals_file):
    """Signals come from --signals (a file) and/or --signal (repeatable)."""
    signals = list(signal_args)

    if signals_file:
        path = Path(signals_file)
        if not path.exists():
            sys.exit(f"Signal list not found: {path}")
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                signals.append(line)

    if not signals:
        sys.exit("No signals given. Use --signals <file> or --signal <ClientSpecificId>.")

    return list(dict.fromkeys(signals))  # keep order, drop duplicates


def iter_samples(response, epoch_tag):
    """Yield (timestamp_ms, available, value) from whichever array is populated."""
    scale = 1000 if epoch_tag == "s" else 1

    for name in VALUE_ARRAYS:
        samples = getattr(response, name, None)
        if not samples:
            continue

        for sample in samples:
            timestamp_ms = int(sample.TimeStamp) * scale
            if not sample.Available:
                yield timestamp_ms, False, None
                continue
            try:
                yield timestamp_ms, True, float(sample.Value)
            except (TypeError, ValueError):
                yield timestamp_ms, True, None
        return


def windows(start, end, minutes):
    """Split the range into request-sized chunks."""
    cursor = start
    while cursor < end:
        stop = min(cursor + timedelta(minutes=minutes), end)
        yield cursor, stop
        cursor = stop


def safe_filename(csid):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", csid)


def csv_row(timestamp_ms, available, value, response, csid):
    stamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return [
        timestamp_ms,
        stamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "" if value is None else value,
        available,
        response.Units,
        response.Name,
        csid,
    ]


def write_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.replace(tmp, path)


def check_writable(out_dir):
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        sys.exit(
            f"Output directory {out_dir} is not writable ({exc}).\n"
            f"The container runs as uid 1000. For a host bind mount, prepare the folder on\n"
            f"the node first:  sudo mkdir -p <host dir> && sudo chown 1000:1000 <host dir>\n"
            f"or use a named Docker volume, which needs no preparation."
        )


# --------------------------------------------------------------------------
# Range mode - the MVP's behaviour
# --------------------------------------------------------------------------

def export(logger, csid, start, end, rate, chunk_minutes, out_dir):
    """Write one CSV for one signal. Returns the number of rows written."""
    out_file = out_dir / f"{safe_filename(csid)}.csv"
    rows = 0

    with out_file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)

        for window_start, window_end in windows(start, end, chunk_minutes):
            response = logger.get_trend(csid, window_start, window_end, rate)
            if response is None:
                continue
            for timestamp_ms, available, value in iter_samples(response, logger.epoch_tag):
                writer.writerow(csv_row(timestamp_ms, available, value, response, csid))
                rows += 1

    if rows == 0:
        out_file.unlink()  # leave no empty files behind

    return rows


def range_key(args, signals):
    digest = hashlib.sha1("\n".join(signals).encode()).hexdigest()[:10]
    return f"range_{args.start:%Y%m%dT%H%M%S}_{args.end:%Y%m%dT%H%M%S}_{args.rate:g}Hz_{digest}"


def run_range(args, signals, out_dir):
    marker = out_dir / ".state" / f"{range_key(args, signals)}.json"
    if marker.exists() and not args.force:
        STATUS["range_done"] = json.loads(marker.read_text())
        log(f"Range already exported ({marker.name}); nothing to do. Use --force to re-run.")
        return

    logger = DataLogger(args.addr, timeout=args.timeout, connect_attempts=args.connect_attempts)
    written = 0
    STATUS["last_cycle_start"] = datetime.now(timezone.utc).isoformat()

    for csid in signals:
        if STOP.is_set():
            return
        try:
            rows = export(logger, csid, args.start, args.end, args.rate, args.chunk_minutes, out_dir)
        except grpc.RpcError as exc:
            STATUS["signals_error"] += 1
            STATUS["last_error"] = f"{csid}: {exc.code().name} {exc.details() or ''}"
            log(f"  ERROR          {csid}: {exc.code().name} {exc.details() or ''}")
            continue

        STATUS["rows_total"] += rows
        if rows:
            written += 1
            STATUS["signals_ok"] += 1
            log(f"  OK             {csid}  {rows} rows")
        else:
            STATUS["signals_no_data"] += 1
            log(f"  NOT AVAILABLE  {csid}")

    STATUS["last_cycle_end"] = datetime.now(timezone.utc).isoformat()
    summary = {"finished": STATUS["last_cycle_end"], "signals": len(signals), "written": written,
               "errors": STATUS["signals_error"], "rows": STATUS["rows_total"]}
    if STATUS["signals_error"] == 0:
        write_json_atomic(marker, summary)   # errors leave it unmarked so a restart retries
    STATUS["range_done"] = summary
    log(f"Done. {written} of {len(signals)} signals written to {out_dir}")


# --------------------------------------------------------------------------
# Follow mode - continuous, resumable, daily files
# --------------------------------------------------------------------------

class DayWriter:
    """Appends rows to <out>/<YYYY-MM-DD>/<csid>.csv, adding the header to new files."""

    def __init__(self, out_dir, csid):
        self.out_dir = out_dir
        self.name = f"{safe_filename(csid)}.csv"
        self.day = None
        self.handle = None
        self.writer = None

    def write(self, row):
        day = row[1][:10]
        if day != self.day:
            self.close()
            folder = self.out_dir / day
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / self.name
            new = not path.exists() or path.stat().st_size == 0
            self.handle = path.open("a", newline="", encoding="utf-8")
            self.writer = csv.writer(self.handle)
            if new:
                self.writer.writerow(CSV_HEADER)
            self.day = day
        self.writer.writerow(row)

    def close(self):
        if self.handle:
            self.handle.flush()
            os.fsync(self.handle.fileno())
            self.handle.close()
        self.handle = self.writer = self.day = None


def follow_signal(logger, csid, start, end, rate, chunk_minutes, out_dir):
    """Append [start, end) of one signal. Returns rows written; raises on gRPC errors."""
    lo, hi = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    writer = DayWriter(out_dir, csid)
    rows = 0
    try:
        for window_start, window_end in windows(start, end, chunk_minutes):
            # No preceding sample: the previous cycle already wrote it.
            response = logger.get_trend(csid, window_start, window_end, rate, include_preceding=False)
            if response is None:
                continue
            for timestamp_ms, available, value in iter_samples(response, logger.epoch_tag):
                if lo <= timestamp_ms < hi:
                    writer.write(csv_row(timestamp_ms, available, value, response, csid))
                    rows += 1
    finally:
        writer.close()
    return rows


def prune(out_dir, keep_days, today):
    if keep_days <= 0:
        return
    cutoff = (today - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    for folder in out_dir.iterdir():
        if folder.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", folder.name) and folder.name < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            log(f"Pruned {folder.name} (older than {keep_days} days)")


def follow_cycle(logger, args, signals, out_dir, state_path, now=None):
    """One pass over all signals. Returns rows written."""
    now = now or datetime.now(timezone.utc)
    end = (now - timedelta(seconds=args.lag)).replace(microsecond=0)
    initial = args.start or (end - timedelta(minutes=args.backfill_minutes))

    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get("rate") not in (None, args.rate):
        log(f"Sample rate changed {state.get('rate')} -> {args.rate} Hz; continuing from the "
            f"saved positions at the new rate.")
    cursors = state.get("signals", {})

    STATUS["last_cycle_start"] = now.isoformat()
    ok = no_data = errors = cycle_rows = 0
    for csid in signals:
        if STOP.is_set():
            break
        start = datetime.fromtimestamp(cursors[csid] / 1000, tz=timezone.utc) if csid in cursors else initial
        if start >= end:
            continue
        try:
            rows = follow_signal(logger, csid, start, end, args.rate, args.chunk_minutes, out_dir)
        except grpc.RpcError as exc:
            errors += 1
            STATUS["last_error"] = f"{csid}: {exc.code().name} {exc.details() or ''}"
            log(f"  ERROR          {csid}: {exc.code().name} {exc.details() or ''}", err=True)
            if exc.code() == grpc.StatusCode.UNAVAILABLE:
                STATUS["connected"] = False
                break  # logger gone - stop this cycle, keep cursors, retry next time
            continue

        cursors[csid] = int(end.timestamp() * 1000)
        write_json_atomic(state_path, {"rate": args.rate, "signals": cursors})
        cycle_rows += rows
        if rows:
            ok += 1
        else:
            no_data += 1

    STATUS.update(cycles=STATUS["cycles"] + 1, last_cycle_end=end.isoformat(), last_cycle_rows=cycle_rows,
                  rows_total=STATUS["rows_total"] + cycle_rows, signals_ok=ok, signals_no_data=no_data,
                  signals_error=errors)
    if errors == 0:
        STATUS["last_error"] = None
        STATUS["connected"] = True
    log(f"cycle up to {end:%Y-%m-%d %H:%M:%S}Z: {cycle_rows} rows, {ok} signals with data, "
        f"{no_data} without, {errors} errors")
    prune(out_dir, args.keep_days, end)
    return cycle_rows


def run_follow(args, signals, out_dir):
    state_path = out_dir / ".state" / "follow.json"
    logger = DataLogger(args.addr, timeout=args.timeout, connect_attempts=args.connect_attempts)
    while not STOP.is_set():
        started = time.monotonic()
        try:
            follow_cycle(logger, args, signals, out_dir, state_path)
        except Exception as exc:  # keep the service alive; report via logs and health
            STATUS["last_error"] = f"{type(exc).__name__}: {exc}"
            log(f"cycle failed: {exc!r}", err=True)
        STOP.wait(max(0.0, args.interval - (time.monotonic() - started)))


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def env(name, default=None):
    value = os.environ.get(f"DL_{name}")
    return value if value not in (None, "") else default


def build_parser():
    p = argparse.ArgumentParser(
        description="Extract DataLogger trends to CSV (follow = continuous, range = one export).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Every option can also be set with an environment variable DL_<OPTION>, "
               "e.g. DL_ADDR, DL_MODE, DL_RATE (DL_SIGNAL takes a comma-separated list).",
    )
    p.add_argument("--addr", default=env("ADDR", "0.0.0.0:50052"), help="DataLogger address, host:port")
    p.add_argument("--signals", default=env("SIGNALS", DEFAULT_SIGNALS),
                   help="Text file with one ClientSpecificId per line ('' for none)")
    p.add_argument("--signal", action="append",
                   default=[s.strip() for s in (env("SIGNAL", "") or "").split(",") if s.strip()],
                   help="A single ClientSpecificId; repeatable")
    p.add_argument("--mode", choices=["follow", "range"], default=env("MODE"),
                   help="Default: range when --end is given, otherwise follow")
    p.add_argument("--start", type=parse_time, default=env("START"),
                   help="UTC. range: required. follow: where to begin when there is no saved state")
    p.add_argument("--end", type=parse_time, default=env("END"), help="UTC, exclusive (range mode)")
    p.add_argument("--rate", type=float, default=float(env("RATE", 1.0)),
                   help="Sample rate in Hz. 1 = one per second, 0.1 = one per 10 s, 1000 = unfiltered")
    p.add_argument("--out", default=env("OUT", "/data/out"), help="Output directory")
    p.add_argument("--chunk-minutes", type=int, default=int(env("CHUNK_MINUTES", 1440)),
                   help="Minutes of data per request; lower it if responses get too large")
    p.add_argument("--connect-attempts", type=int, default=int(env("CONNECT_ATTEMPTS", 0)),
                   help="Maximum connection attempts; 0 keeps retrying")
    p.add_argument("--timeout", type=float, default=float(env("TIMEOUT", 600)), help="Seconds per gRPC request")
    p.add_argument("--interval", type=float, default=float(env("INTERVAL", 300)),
                   help="follow: seconds between cycles")
    p.add_argument("--lag", type=float, default=float(env("LAG", 60)),
                   help="follow: stay this many seconds behind now, so the logger has written the data")
    p.add_argument("--backfill-minutes", type=int, default=int(env("BACKFILL_MINUTES", 60)),
                   help="follow: without saved state or --start, begin this long before now")
    p.add_argument("--keep-days", type=int, default=int(env("KEEP_DAYS", 0)),
                   help="follow: delete day folders older than this; 0 keeps everything")
    p.add_argument("--on-finish", choices=["exit", "idle"], default=env("ON_FINISH", "idle" if ON_EDGE else "exit"),
                   help="range: what to do when done (idle keeps an edge module from restart-looping)")
    p.add_argument("--force", action="store_true", default=env("FORCE", "") in ("1", "true", "yes"),
                   help="range: re-run even if this exact range already completed")
    p.add_argument("--no-helin", action="store_true", default=env("NO_HELIN", "") in ("1", "true", "yes"),
                   help="Do not answer the Helin portal's health/configuration requests")
    return p


def validate(p, args):
    if args.mode is None:
        args.mode = "range" if args.end else "follow"
    if args.mode == "range":
        if not args.start or not args.end:
            p.error("range mode needs --start and --end")
        if args.end <= args.start:
            p.error(f"--end ({args.end}) must be after --start ({args.start}).")
    for name in ("connect_attempts", "keep_days", "backfill_minutes"):
        if getattr(args, name) < 0:
            p.error(f"--{name.replace('_', '-')} must be 0 or greater.")
    for name in ("rate", "interval", "chunk_minutes", "timeout"):
        if getattr(args, name) <= 0:
            p.error(f"--{name.replace('_', '-')} must be greater than 0.")
    if args.lag < 0:
        p.error("--lag must be 0 or greater.")


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    validate(p, args)

    signals = read_signals(args.signal, args.signals)
    out_dir = Path(args.out)
    check_writable(out_dir)
    STATUS["mode"] = args.mode

    for sig in (signals_module.SIGTERM, signals_module.SIGINT):
        signals_module.signal(sig, lambda *_: STOP.set())

    if not args.no_helin:
        import helin_status
        helin_status.start(args, signals, STATUS, log)

    dialled = normalise_addr(args.addr)
    log(f"datalogger : {args.addr}" + ("" if dialled == args.addr else f" (dialling {dialled})"))
    log(f"mode       : {args.mode}")
    if args.mode == "range":
        log(f"range      : {args.start:%Y-%m-%d %H:%M} .. {args.end:%Y-%m-%d %H:%M} UTC")
    else:
        log(f"interval   : every {args.interval:g} s, {args.lag:g} s behind now"
            + (f", keeping {args.keep_days} days" if args.keep_days else ""))
    log(f"rate       : {args.rate:g} Hz")
    log(f"signals    : {len(signals)}")
    log(f"output     : {out_dir}")

    if args.mode == "follow":
        run_follow(args, signals, out_dir)
        return

    run_range(args, signals, out_dir)
    if args.on_finish == "idle" and not STOP.is_set():
        log("Range finished; idling (on-finish=idle). Stop or redeploy the module to end.")
        STOP.wait()


if __name__ == "__main__":
    main()
