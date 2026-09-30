#!/usr/bin/env bash
# Narrow down a real-DataLogger problem: one tag, one window, vary rate and destination.
# Usage (in the VM, from ~/app/devnode): bash diag-real.sh [tag] [start] [end] [addr]
tag="${1:-Fields.Tanks.TankTotals.TotalFuelTankLevel}"
start="${2:-2026-04-29 18:00:00}"
end="${3:-2026-04-29 18:10:00}"
addr="${4:-127.0.0.1:50715}"
run() {
  echo "== $*"
  docker compose --profile real run --rm -T real --addr="$addr" --signals= --signal="$tag" \
    --start="$start" --end="$end" --force --on-finish=exit --no-helin --insecure "$@" 2>&1 \
    | grep -E ' OK | NOT AVAILABLE | ERROR |Done\.' || true
}
run --rate=1    --dest-type=csv  --out=/data/out/diag-csv-1
run --rate=1000 --dest-type=csv  --out=/data/out/diag-csv-1000
run --rate=1    --dest-type=http --dest-addr=https://127.0.0.1:6684/sensor-reading --out=/data/out/diag-http-1
run --rate=1000 --dest-type=http --dest-addr=https://127.0.0.1:6684/sensor-reading --out=/data/out/diag-http-1000
run --rate=1000 --dest-type=http --dest-addr=https://127.0.0.1:6684/sensor-reading --out=/data/out/diag-http-1000u --send-unavailable
