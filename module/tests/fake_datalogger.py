"""A stand-in Rhodium DataLogger (DataLoggerTrends.GetTrend) with synthetic data.

Every signal has one sample per `period_ms` (default 1 s) on exact multiples of
the period, up to "now". Values are deterministic, so tests can predict them:

    value(t) = (t_ms // 1000) % 1000      Available is False on whole minutes

Signals starting with "Missing." do not exist (NOT_FOUND). Names containing
"Temperature" come back as Values32Float in Kelvin, everything else as
Values64Float in W. `reject_ms=True` mimics an old logger that only accepts
Epoch_UnixSeconds.

Run standalone for the dev node:  python fake_datalogger.py --port 50715
"""

import argparse
import sys
import time
from concurrent import futures
from pathlib import Path

import grpc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import Trending_pb2  # noqa: E402
import Trending_pb2_grpc  # noqa: E402

MS = Trending_pb2.DLEpoch.Epoch_UnixMilliseconds
S = Trending_pb2.DLEpoch.Epoch_UnixSeconds


def value_at(t_ms):
    return float((t_ms // 1000) % 1000)


def available_at(t_ms):
    return (t_ms // 1000) % 60 != 0


class FakeTrends(Trending_pb2_grpc.DataLoggerTrendsServicer):
    def __init__(self, period_ms=1000, reject_ms=False, now_ms=None, empty_at_1000=False):
        self.period_ms = period_ms
        self.reject_ms = reject_ms
        self.empty_at_1000 = empty_at_1000   # subscription mode with nothing stored: empty OK answer
        self.now_ms = now_ms          # None = wall clock
        self.requests = []

    def GetTrend(self, request, context):
        self.requests.append(request)
        if request.Epoch == MS and self.reject_ms:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Epoch_UnixMilliseconds not supported")
        if request.ValueCSId.startswith("Missing."):
            context.abort(grpc.StatusCode.NOT_FOUND, f"{request.ValueCSId} not logged")

        if self.empty_at_1000 and request.SampleRate >= 1000:
            return Trending_pb2.GetTrendResponse(Name=request.ValueCSId.rsplit(".", 1)[-1], Units="W")

        scale = 1 if request.Epoch == MS else 1000
        start_ms, end_ms = request.StartDateTime * scale, request.EndDateTime * scale + (scale - 1)
        now_ms = self.now_ms if self.now_ms is not None else int(time.time() * 1000)
        end_ms = min(end_ms, now_ms)

        period = self.period_ms
        if request.SampleRate and request.SampleRate < 1000:
            period = max(period, int(round(1000 / request.SampleRate)))

        first = -(-start_ms // period) * period          # first multiple >= start
        times = list(range(first, end_ms + 1, period))
        if request.IncludePrecedingSample and first - period >= 0:
            times.insert(0, first - period)
        if not times:
            context.abort(grpc.StatusCode.NOT_FOUND, "no data in window")

        response = Trending_pb2.GetTrendResponse(
            Name=request.ValueCSId.rsplit(".", 1)[-1], ValueCSId=request.ValueCSId, Epoch=request.Epoch)
        temperature = "Temperature" in request.ValueCSId
        response.Units = "K" if temperature else "W"
        array = response.Values32Float if temperature else response.Values64Float
        for t in times:
            sample = array.add()
            sample.TimeStamp = t // scale
            sample.Available = available_at(t)
            if sample.Available:
                sample.Value = value_at(t)
        response.NrOfValues = len(times)
        return response


def serve(port=0, **kwargs):
    """Start a server; returns (server, port, servicer)."""
    servicer = FakeTrends(**kwargs)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    Trending_pb2_grpc.add_DataLoggerTrendsServicer_to_server(servicer, server)
    port = server.add_insecure_port(f"0.0.0.0:{port}")
    server.start()
    return server, port, servicer


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=50715)
    ap.add_argument("--period-ms", type=int, default=1000)
    ap.add_argument("--reject-ms", action="store_true")
    a = ap.parse_args()
    srv, p, _ = serve(a.port, period_ms=a.period_ms, reject_ms=a.reject_ms)
    print(f"fake DataLogger listening on 0.0.0.0:{p}", flush=True)
    srv.wait_for_termination()
