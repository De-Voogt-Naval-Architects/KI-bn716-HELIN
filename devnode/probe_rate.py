"""Ask the DataLogger for one tag at several SampleRates and print the raw answer.

Run in the module image:  python /devnode/probe_rate.py <addr> <tag> <start> <end>
"""

import sys
from datetime import datetime, timezone

import grpc

sys.path.insert(0, "/app")
import Trending_pb2  # noqa: E402
import Trending_pb2_grpc  # noqa: E402

addr, tag, start, end = sys.argv[1:5]
parse = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
t0, t1 = parse(start), parse(end)
stub = Trending_pb2_grpc.DataLoggerTrendsStub(
    grpc.insecure_channel(addr, options=[("grpc.max_receive_message_length", 512 * 1024 * 1024)]))

for rate in (1, 10, 100, 999, 1000, 0):
    for epoch_name, epoch, mult in (("ms", Trending_pb2.DLEpoch.Epoch_UnixMilliseconds, 1000),
                                    ("s", Trending_pb2.DLEpoch.Epoch_UnixSeconds, 1)):
        for preceding in (True, False):
            req = Trending_pb2.GetTrendRequest(
                ValueCSId=tag, SampleRate=rate, Epoch=epoch, IncludePrecedingSample=preceding,
                StartDateTime=int(t0.timestamp() * mult), EndDateTime=int(t1.timestamp() * mult) - 1)
            label = f"rate={rate:<5} epoch={epoch_name:<2} preceding={preceding!s:<5}"
            try:
                r = stub.GetTrend(req, timeout=120)
                arrays = {n: len(getattr(r, n)) for n in ("Values64Float", "Values32Float", "Values64",
                                                         "Values32", "Values16", "Values8", "Values1")
                          if len(getattr(r, n))}
                print(f"{label} OK  NrOfValues={r.NrOfValues} arrays={arrays} datatype={r.datatype} units={r.Units!r}")
            except grpc.RpcError as exc:
                print(f"{label} {exc.code().name}: {(exc.details() or '')[:160]}")
