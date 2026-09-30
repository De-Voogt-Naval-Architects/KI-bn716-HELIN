"""A stand-in for the Helin Data Collector's HTTP south endpoint (/sensor-reading).

Accepts the Fledge http_south JSON format - a list of
{"timestamp": str, "asset": str, "readings": {datapoint: value}} - validates it,
and hands every accepted batch to its "north" outputs, the way HDC fans data out:

  cloud      stand-in for the cloud north (ADX via Cloud HDC): counts only
  timescale  like HDC's Timescale north plugin in flatmap mode: one row per
             datapoint in <schema>.<table> ("timestamp", asset, datapoint, value TEXT)
             on the node's TimescaleDB (--timescale host:port/db, TIMESCALE_PASSWORD)

A failing north answers the post with 503, so the extractor retries the window.
GET /stats returns a summary.

    python fake_hdc.py --port 6684 --cert cert.pem --key key.pem --timescale timescaledb:5432/helindb
    python fake_hdc.py --port 6683                                   (plain HTTP, no database)
"""

import argparse
import json
import os
import ssl
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class CloudNorth:
    name = "cloud"

    def __init__(self):
        self.count = 0

    def write(self, batch):
        self.count += sum(len(r["readings"]) for r in batch)


class TimescaleNorth:
    name = "timescale"

    def __init__(self, target, user="helin", password=None, schema="public", table="readings"):
        hostport, _, self.database = target.partition("/")
        self.host, _, port = hostport.partition(":")
        self.port = int(port or 5432)
        self.user, self.password = user, password or os.getenv("TIMESCALE_PASSWORD")
        self.relation = f'"{schema}"."{table}"'
        self.con = None
        self.count = 0

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
            return
        try:
            self.con = self.con or self._connect()
            self.con.run(f"INSERT INTO {self.relation} SELECT * FROM unnest(CAST(:ts AS timestamptz[]), "
                         f"CAST(:a AS text[]), CAST(:d AS text[]), CAST(:v AS text[]))",
                         ts=ts, a=asset, d=dp, v=value)
        except Exception:
            self.con = None           # reconnect next time
            raise
        self.count += len(ts)


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
        with self.lock:
            for north in self.norths:
                north.write(batch)
            self.posts += 1
            self.total += len(batch)
            self.per_asset.update(r["asset"] for r in batch)
            self.last = batch[-1]
            if self.keep_readings:
                self.readings.extend(batch)

    def stats(self):
        with self.lock:
            return {"posts": self.posts, "readings": self.total, "assets": len(self.per_asset),
                    "north": {n.name: n.count for n in self.norths},
                    "per_asset": dict(sorted(self.per_asset.items())), "last": self.last}


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
            try:
                store.accept(body)
            except Exception as exc:
                return self._send(503, {"error": f"north output failed: {exc}"})
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
    ap.add_argument("--timescale", help="host:port/database for the Timescale north (flatmap)")
    ap.add_argument("--timescale-user", default="helin")
    a = ap.parse_args()
    norths = [CloudNorth()]
    if a.timescale:
        norths.append(TimescaleNorth(a.timescale, user=a.timescale_user))
    srv, p, _ = serve(a.port, a.cert, a.key, keep_readings=False, norths=norths)
    print(f"fake HDC listening on :{p} ({'https' if a.cert else 'http'}), north: "
          + ", ".join(n.name for n in norths), flush=True)
    threading.Event().wait()
