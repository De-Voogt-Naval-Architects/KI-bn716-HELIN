"""Where extracted samples go: daily CSV files, or the Helin Data Collector over HTTP.

Both sinks take the extractor's CSV-shaped rows
    [Timestamp_ms, DatetimeUTC, Value, Available, Unit, Name, ValueCSId]
one signal at a time, and must be close()d; close() flushes, and the extractor
only advances a signal's cursor after close() returned without an error.

HTTP posts use the HDC (Fledge) http_south JSON format, a list of readings:

    [{"timestamp": "2026-04-29 18:00:01.000000+00:00",
      "asset": "Fields.Navigation.GPS.SpeedOverGround",
      "readings": {"value": 3.2}}]

--asset-mode signal : asset = ClientSpecificId, datapoint = --datapoint (default "value")
--asset-mode group  : asset = ClientSpecificId minus its last part, datapoint = last part
                      (Fields.Navigation.GPS / SpeedOverGround)
"""

import csv
import json
import os
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone

CSV_HEADER = ["Timestamp_ms", "DatetimeUTC", "Value", "Available", "Unit", "Name", "ValueCSId"]


class DestinationError(Exception):
    """The destination did not accept data; the signal's cursor must not advance."""


class CsvDaySink:
    """Appends rows to <out>/<YYYY-MM-DD>/<file>.csv, adding the header to new files."""

    def __init__(self, out_dir, filename):
        self.out_dir = out_dir
        self.name = filename
        self.day = None
        self.handle = None
        self.writer = None
        self.count = 0

    def write(self, row):
        day = row[1][:10]
        if day != self.day:
            self._close_file()
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
        self.count += 1

    def _close_file(self):
        if self.handle:
            self.handle.flush()
            os.fsync(self.handle.fileno())
            self.handle.close()
        self.handle = self.writer = self.day = None

    def close(self):
        self._close_file()

    def discard(self):
        # Rows already appended stay; the retried window may repeat a few of them.
        self._close_file()


class HttpClient:
    """Posts reading batches to an HDC http_south endpoint. One per process."""

    def __init__(self, url, insecure=False, timeout=30.0):
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"--dest-addr must be an http(s) URL, got {url!r}")
        self.url = url
        self.timeout = timeout
        self.context = None
        if url.startswith("https://"):
            self.context = ssl.create_default_context()
            if insecure:                   # HDC on the node uses a self-signed certificate
                self.context.check_hostname = False
                self.context.verify_mode = ssl.CERT_NONE

    def post(self, readings):
        body = json.dumps(readings, separators=(",", ":")).encode()
        request = urllib.request.Request(self.url, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self.context) as response:
                if response.status >= 300:
                    raise DestinationError(f"HTTP {response.status} from {self.url}")
        except urllib.error.HTTPError as exc:
            detail = exc.read(300).decode(errors="replace").strip()
            raise DestinationError(f"HTTP {exc.code} from {self.url}: {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise DestinationError(f"cannot reach {self.url}: {reason}") from None


def fledge_timestamp(timestamp_ms):
    stamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return stamp.strftime("%Y-%m-%d %H:%M:%S.%f+00:00")


def asset_and_datapoint(csid, mode, datapoint):
    if mode == "group" and "." in csid:
        asset, _, point = csid.rpartition(".")
        return asset, point
    return csid, datapoint


class HttpSink:
    """Buffers one signal's rows and posts them to HDC in batches."""

    def __init__(self, client, csid, batch_size=500, asset_mode="signal", datapoint="value",
                 send_unavailable=False):
        self.client = client
        self.asset, self.datapoint = asset_and_datapoint(csid, asset_mode, datapoint)
        self.batch_size = batch_size
        self.send_unavailable = send_unavailable
        self.buffer = []
        self.count = 0

    def write(self, row):
        timestamp_ms, _, value, available = row[0], row[1], row[2], row[3]
        if not available or value == "" or value is None:
            if not self.send_unavailable:
                return
            value = None
        self.buffer.append({"timestamp": fledge_timestamp(timestamp_ms), "asset": self.asset,
                            "readings": {self.datapoint: value}})
        if len(self.buffer) >= self.batch_size:
            self.flush()

    def flush(self):
        if self.buffer:
            self.client.post(self.buffer)
            self.count += len(self.buffer)
            self.buffer = []

    def close(self):
        self.flush()

    def discard(self):
        # Batches already posted stay in HDC; the retried window may repeat them.
        self.buffer = []
