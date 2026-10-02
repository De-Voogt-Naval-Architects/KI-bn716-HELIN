"""Count stored samples per UTC day (SampleRate 1000, subscription mode) for a few tags.

Run in the module image:  python /devnode/probe_days.py <addr> <first-day> <last-day> <tag> [<tag> ...]
"""

import sys
from datetime import datetime, timedelta, timezone

import grpc

sys.path.insert(0, "/app")
import Trending_pb2  # noqa: E402
import Trending_pb2_grpc  # noqa: E402

addr, first, last, tags = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
stub = Trending_pb2_grpc.DataLoggerTrendsStub(
    grpc.insecure_channel(addr, options=[("grpc.max_receive_message_length", 512 * 1024 * 1024)]))
day = datetime.strptime(first, "%Y-%m-%d").replace(tzinfo=timezone.utc)
end = datetime.strptime(last, "%Y-%m-%d").replace(tzinfo=timezone.utc)

print("day         " + "  ".join(f"{t.rsplit('.', 1)[-1]:>22}" for t in tags))
while day <= end:
    counts = []
    for tag in tags:
        req = Trending_pb2.GetTrendRequest(
            ValueCSId=tag, SampleRate=1000, Epoch=Trending_pb2.DLEpoch.Epoch_UnixMilliseconds,
            IncludePrecedingSample=False, StartDateTime=int(day.timestamp() * 1000),
            EndDateTime=int((day + timedelta(days=1)).timestamp() * 1000) - 1)
        try:
            r = stub.GetTrend(req, timeout=300)
            n = next((len(getattr(r, a)) for a in ("Values64Float", "Values32Float", "Values64", "Values32",
                                                    "Values16", "Values8", "Values1") if len(getattr(r, a))), 0)
            counts.append(str(n))
        except grpc.RpcError as exc:
            counts.append(exc.code().name)
    print(f"{day:%Y-%m-%d}  " + "  ".join(f"{c:>22}" for c in counts), flush=True)
    day += timedelta(days=1)
