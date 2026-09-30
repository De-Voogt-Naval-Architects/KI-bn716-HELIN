"""Stand-in for the Helin Platform's edge-module-request API on the dev node.

Same routes as production, so the curl commands only differ in base URL/token:

    GET  /api/v1/edge-module-request/{node_id}/module/{meta_name}/configuration
    POST /api/v1/edge-module-request/{node_id}/module/{meta_name}/configuration
    GET  /api/v1/edge-module-request/{node_id}/module/{meta_name}/health

Each call becomes an IoT Hub direct method over MQTT, exactly as edgeHub
delivers it to the module (helin_edge_sdk/topics.py, compression.py):

    publish   $iothub/methods/POST/{method}/?$rid={rid}   gzip+base64 JSON envelope
    receive   $iothub/methods/res/{status}/?$rid={rid}    same envelope

The reply body is the decompressed envelope: {"status": ..., "response": ...}.
The Authorization header is accepted but not checked.
"""

import base64
import gzip
import json
import logging
import os
import re
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

logging.basicConfig(level="INFO", format="%(asctime)s gateway: %(message)s")
log = logging.getLogger()

MQTT_HOST = os.getenv("MQTT_HOST", "mosquitto")
TIMEOUT_S = float(os.getenv("METHOD_TIMEOUT_S", "30"))
ROUTE = re.compile(r"^/api/v1/edge-module-request/(?P<node>[^/]+)/module/(?P<meta>[^/]+)/(?P<what>configuration|health)/?$")

_pending: dict[str, dict] = {}
_lock = threading.Lock()


def compress(payload) -> dict:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return {"encoding": "gzip", "data": base64.b64encode(gzip.compress(raw, 9)).decode()}


def decompress(envelope):
    if isinstance(envelope, dict) and "data" in envelope:
        return json.loads(gzip.decompress(base64.b64decode(envelope["data"])))
    return envelope


def on_message(client, userdata, msg):
    m = re.match(r"^\$iothub/methods/res/(\d+)/\?\$rid=(.+)$", msg.topic)
    if not m:
        return
    rid = urllib.parse.unquote(m.group(2))
    with _lock:
        slot = _pending.get(rid)
    if slot is not None:
        slot["status"] = int(m.group(1))
        slot["body"] = decompress(json.loads(msg.payload or b"null"))
        slot["done"].set()


mqttc = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id="devnode-gateway")
mqttc.on_message = on_message
mqttc.on_connect = lambda c, *a: c.subscribe("$iothub/methods/res/#")
mqttc.reconnect_delay_set(1, 10)


def invoke(method: str, payload) -> tuple[int, object]:
    rid = uuid.uuid4().hex
    slot = {"done": threading.Event()}
    with _lock:
        _pending[rid] = slot
    try:
        mqttc.publish(f"$iothub/methods/POST/{method}/?$rid={rid}", json.dumps(compress(payload)))
        if not slot["done"].wait(TIMEOUT_S):
            return 504, {"status": 504, "error": f"module did not answer {method} within {TIMEOUT_S:.0f} s"}
        return slot["status"], slot["body"]
    finally:
        with _lock:
            _pending.pop(rid, None)


class Handler(BaseHTTPRequestHandler):
    def _route(self, verb: str):
        m = ROUTE.match(urllib.parse.urlparse(self.path).path)
        if not m:
            return self._send(404, {"error": "unknown route"})
        what = m.group("what")
        if verb == "GET":
            method = "get_configuration" if what == "configuration" else "get_health"
            payload = {}
        elif what == "configuration":
            method = "set_configuration"
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                return self._send(400, {"error": f"body is not JSON: {exc}"})
        else:
            return self._send(405, {"error": "health is GET only"})
        log.info("%s node=%s module=%s -> %s", verb, m.group("node"), m.group("meta"), method)
        status, body = invoke(method, payload)
        self._send(status, body)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _send(self, status: int, body):
        data = json.dumps(body, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    mqttc.connect_async(MQTT_HOST, 1883)
    mqttc.loop_start()
    log.info("listening on :8080, broker %s", MQTT_HOST)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
