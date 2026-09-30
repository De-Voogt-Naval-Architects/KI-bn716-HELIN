"""A stand-in for the Helin Data Collector's HTTP south endpoint (/sensor-reading).

Accepts the Fledge http_south JSON format - a list of
{"timestamp": str, "asset": str, "readings": {datapoint: value}} - and, like HDC,
stores it in a local buffer first and answers 200. Each "north" output then
drains its own copy of the buffer on its own thread, retrying with backoff, so
one output being down (the cloud, with no internet) never stops the others (the
onboard database):

  cloud      --cloud-url: forward over HTTPS to the Cloud HDC (another fake_hdc)
             without --cloud-url: count only
  timescale  --timescale host:port/db: HDC Timescale north, flatmap rows in
             <schema>.<table> ("timestamp", asset, datapoint, value TEXT)

A north's backlog is capped at --max-backlog readings; beyond that the oldest are
dropped and counted as purged (HDC's buffer purge). GET /stats returns per-north
written / backlog / purged / last_error.

    python fake_hdc.py --port 6684 --cert c.pem --key k.pem \
        --timescale timescaledb:5432/helindb --cloud-url https://cloud-hdc:6684/sensor-reading
"""

import argparse
import collections
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class North:
    """One HDC output: its own queue of batches, drained on its own thread."""

    name = "north"

    def __init__(self, max_backlog=2_000_000, retry_min=1.0, retry_max=30.0):
        self.queue = collections.deque()
        self.cv = threading.Condition()
        self.backlog = 0              # readings waiting
        self.written = 0              # datapoints/readings delivered
        self.purged = 0
        self.last_error = None
        self.max_backlog = max_backlog
        self.retry_min, self.retry_max = retry_min, retry_max
        threading.Thread(target=self._run, name=f"north-{self.name}", daemon=True).start()

    def enqueue(self, batch):
        with self.cv:
            self.queue.append(batch)
            self.backlog += len(batch)
            while self.backlog > self.max_backlog and len(self.queue) > 1:
                dropped = self.queue.popleft()
                self.backlog -= len(dropped)
                self.purged += len(dropped)
            self.cv.notify()

    def _run(self):
        delay = self.retry_min
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                batch = self.queue[0]
            try:
                n = self.write(batch)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(delay)
                delay = min(delay * 2, self.retry_max)
                continue
            delay = self.retry_min
            with self.cv:
                if self.queue and self.queue[0] is batch:
                    self.queue.popleft()
                    self.backlog -= len(batch)
                self.written += n
                if not self.queue:
                    self.last_error = None

    def write(self, batch):           # returns what it counts as written
        raise NotImplementedError

    def stats(self):
        with self.cv:
            return {"written": self.written, "backlog": self.backlog, "purged": self.purged,
                    "last_error": self.last_error}


class CountNorth(North):
    name = "cloud"

    def write(self, batch):
        return sum(len(r["readings"]) for r in batch)


class ForwardNorth(North):
    """Cloud output: forward batches over HTTP(S) to the Cloud HDC."""

    name = "cloud"

    def __init__(self, url, insecure=True, timeout=15, **kw):
        self.url, self.timeout = url, timeout
        self.ctx = None
        if url.startswith("https://"):
            self.ctx = ssl.create_default_context()
            if insecure:
                self.ctx.check_hostname, self.ctx.verify_mode = False, ssl.CERT_NONE
        super().__init__(**kw)

    def write(self, batch):
        req = urllib.request.Request(self.url, data=json.dumps(batch).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx):
                pass
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code} from {self.url}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"cannot reach {self.url}: {getattr(exc, 'reason', exc)}") from None
        return sum(len(r["readings"]) for r in batch)


class TimescaleNorth(North):
    name = "timescale"

    def __init__(self, target, user="helin", password=None, schema="public", table="readings", **kw):
        hostport, _, self.database = target.partition("/")
        self.host, _, port = hostport.partition(":")
        self.port = int(port or 5432)
        self.user, self.password = user, password or os.getenv("TIMESCALE_PASSWORD")
        self.relation = f'"{schema}"."{table}"'
        self.con = None
        super().__init__(**kw)

    def _connect(self):
        import pg8000.native
        con = pg8000.native.Connection(user=self.user, password=self.password, host=self.host,
                                       port=self.port, database=self.database or "helindb", timeout=30)
        con.run(f'CREATE TABLE IF NOT EXISTS {self.relation} ("timestamp" TIMESTAMPTZ NOT NULL, '
                f'asset VARCHAR(255) NOT NULL, datapoint VARCHAR(255) NOT NULL, value TEXT NOT NULL)')
        try:
            con.run("CREATE EXTENSION IF NOT EXISTS timescaledb")
            con.run(f"SELECT create_hypertable('{self.relation}', 'timestamp', if_not_exists => TRUE, "
                    f"migrate_data => TRUE)")
        except Exception:
            pass                      # plain PostgreSQL works too, just without the hypertable
        con.run(f'CREATE INDEX IF NOT EXISTS readings_asset_dp_ts ON {self.relation} (asset, datapoint, "timestamp")')
        return con

    def write(self, batch):
        ts, asset, dp, value = [], [], [], []
        for r in batch:
            for point, v in r["readings"].items():
                if v is None:
                    continue          # flatmap value is NOT NULL
                ts.append(r["timestamp"]); asset.append(r["asset"]); dp.append(point); value.append(str(v))
        if not ts:
            return 0
        try:
            self.con = self.con or self._connect()
            self.con.run(f"INSERT INTO {self.relation} SELECT * FROM unnest(CAST(:ts AS timestamptz[]), "
                         f"CAST(:a AS text[]), CAST(:d AS text[]), CAST(:v AS text[]))",
                         ts=ts, a=asset, d=dp, v=value)
        except Exception:
            self.con = None           # reconnect next time
            raise
        return len(ts)


class Store:
    def __init__(self, keep_readings=True, norths=()):
        self.lock = threading.Lock()
        self.keep_readings = keep_readings
        self.readings = []            # only filled when keep_readings (tests)
        self.per_asset = Counter()
        self.total = 0
        self.last = None
        self.posts = 0
        self.fail_next = 0            # tests: answer the next N posts with HTTP 503
        self.norths = list(norths)

    def accept(self, batch):
        """Buffer the batch locally and hand it to every north; never waits for them."""
        with self.lock:
            self.posts += 1
            self.total += len(batch)
            self.per_asset.update(r["asset"] for r in batch)
            self.last = batch[-1]
            if self.keep_readings:
                self.readings.extend(batch)
        for north in self.norths:
            north.enqueue(batch)

    def drain(self, timeout=10.0):
        """Tests: wait until every north has emptied its queue (or timeout)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(n.stats()["backlog"] == 0 for n in self.norths):
                return True
            time.sleep(0.05)
        return False

    def stats(self):
        with self.lock:
            base = {"posts": self.posts, "readings": self.total, "assets": len(self.per_asset),
                    "per_asset": dict(sorted(self.per_asset.items())), "last": self.last}
        base["north"] = {n.name: n.stats() for n in self.norths}
        return base


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path.rstrip("/") != "/sensor-reading":
                return self._send(404, {"error": "unknown path"})
            with store.lock:
                if store.fail_next:
                    store.fail_next -= 1
                    return self._send(503, {"error": "unavailable (test)"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                if not isinstance(body, list):
                    raise ValueError("body must be a JSON list of readings")
                for r in body:
                    if not isinstance(r, dict) or not {"timestamp", "asset", "readings"} <= set(r):
                        raise ValueError(f"bad reading {r!r}")
                    if not isinstance(r["readings"], dict) or not r["readings"]:
                        raise ValueError(f"readings must be a non-empty object: {r!r}")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send(400, {"error": str(exc)})
            store.accept(body)
            self._send(200, {"result": "success"})

        def do_GET(self):
            if self.path.rstrip("/") == "/stats":
                return self._send(200, store.stats())
            self._send(404, {"error": "unknown path"})

        def _send(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return Handler


def serve(port=0, cert=None, key=None, keep_readings=True, norths=()):
    """Start in a thread; returns (server, port, store)."""
    store = Store(keep_readings=keep_readings, norths=norths)
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(store))
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1], store


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=6684)
    ap.add_argument("--cert")
    ap.add_argument("--key")
    ap.add_argument("--name", default="edge HDC", help="label in the startup log")
    ap.add_argument("--timescale", help="host:port/database for the Timescale north (flatmap)")
    ap.add_argument("--timescale-user", default="helin")
    ap.add_argument("--cloud-url", help="forward the cloud north to this Cloud HDC URL")
    ap.add_argument("--no-cloud", action="store_true", help="no cloud north (e.g. for the Cloud HDC itself)")
    ap.add_argument("--max-backlog", type=int, default=2_000_000)
    a = ap.parse_args()
    norths = []
    if not a.no_cloud:
        norths.append(ForwardNorth(a.cloud_url, max_backlog=a.max_backlog) if a.cloud_url
                      else CountNorth(max_backlog=a.max_backlog))
    if a.timescale:
        norths.append(TimescaleNorth(a.timescale, user=a.timescale_user, max_backlog=a.max_backlog))
    srv, p, _ = serve(a.port, a.cert, a.key, keep_readings=False, norths=norths)
    print(f"fake {a.name} listening on :{p} ({'https' if a.cert else 'http'}), north: "
          + (", ".join(n.name + ("->" + a.cloud_url if n.name == "cloud" and a.cloud_url else "")
                       for n in norths) or "none"), flush=True)
    threading.Event().wait()
