"""A stand-in for the Helin Data Collector's HTTP south endpoint (/sensor-reading).

Accepts the Fledge http_south JSON format - a list of
{"timestamp": str, "asset": str, "readings": {datapoint: value}} - validates it,
and keeps what it received. GET /stats returns a summary.

    python fake_hdc.py --port 6684 --cert cert.pem --key key.pem     (HTTPS, as on the node)
    python fake_hdc.py --port 6683                                   (plain HTTP)
"""

import argparse
import json
import ssl
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.readings = []
        self.posts = 0
        self.fail_next = 0          # tests: answer the next N posts with HTTP 503

    def stats(self):
        with self.lock:
            per_asset = Counter(r["asset"] for r in self.readings)
            return {"posts": self.posts, "readings": len(self.readings), "assets": len(per_asset),
                    "per_asset": dict(sorted(per_asset.items())),
                    "last": self.readings[-1] if self.readings else None}


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
            with store.lock:
                store.posts += 1
                store.readings.extend(body)
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


def serve(port=0, cert=None, key=None):
    """Start in a thread; returns (server, port, store)."""
    store = Store()
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
    a = ap.parse_args()
    srv, p, _ = serve(a.port, a.cert, a.key)
    print(f"fake HDC listening on :{p} ({'https' if a.cert else 'http'})", flush=True)
    threading.Event().wait()
