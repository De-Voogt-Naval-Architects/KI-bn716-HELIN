"""Find windows where the DataLogger returns raw (SampleRate 1000, subscription mode) data.

Run in the module image:  python /devnode/probe_windows.py <addr> <tag> [<tag> ...]
Probes one hour at the start of each listed day, at rate 1000 and at rate 1.
"""

import sys
from datetime import datetime, timedelta, timezone

import grpc

sys.path.insert(0, "/app")
import Trending_pb2  # noqa: E402
import Trending_pb2_grpc  # noqa: E402

addr, tags = sys.argv[1], sys.argv[2:]
stub = Trending_pb2_grpc.DataLoggerTrendsStub(
    grpc.insecure_channel(addr, options=[("grpc.max_receive_message_length", 512 * 1024 * 1024)]))
days = ["2026-09-25", "2026-09-24", "2026-09-20", "2026-09-10", "2026-08-15", "2026-07-01",
        "2026-06-01", "2026-05-15", "2026-04-29"]


def count(tag, t0, t1, rate):
    req = Trending_pb2.GetTrendRequest(
        ValueCSId=tag, SampleRate=rate, Epoch=Trending_pb2.DLEpoch.Epoch_UnixMilliseconds,
        IncludePrecedingSample=False, StartDateTime=int(t0.timestamp() * 1000),
        EndDateTime=int(t1.timestamp() * 1000) - 1)
    try:
        r = stub.GetTrend(req, timeout=120)
    except grpc.RpcError as exc:
        return exc.code().name
    for name in ("Values64Float", "Values32Float", "Values64", "Values32", "Values16", "Values8", "Values1"):
        values = getattr(r, name)
        if len(values):
            ok = sum(1 for v in values if v.Available)
            return f"{len(values)} ({ok} avail)"
    return "0"


for tag in tags:
    print(f"== {tag}")
    for day in days:
        t0 = datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
        t1 = t0 + timedelta(hours=1)
        print(f"  {day} 12-13Z   rate 1000: {count(tag, t0, t1, 1000):<18} rate 1: {count(tag, t0, t1, 1)}")
