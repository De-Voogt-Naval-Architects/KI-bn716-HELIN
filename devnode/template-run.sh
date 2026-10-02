#!/usr/bin/env bash
# Run the CI-built image with the node template's Cmd verbatim (only --addr swapped for the
# local DataLogger tunnel). HelinDataCollector resolves to the dev HDC stand-in via --add-host.
#   a) --loop as in the template: connects, cycles, and (logger holds no recent data) warns
#   b) the same flags + a past window that has data: readings reach HDC -> TimescaleDB
set -uo pipefail
cd "${DEVNODE_DIR:-$HOME/app/devnode}"
IMAGE="${IMAGE:-datalogger-extractor:ci}"
ADDR="${ADDR:-127.0.0.1:50715}"
nc -z -w 3 ${ADDR%:*} ${ADDR#*:} 2>/dev/null || { echo "DataLogger not reachable at $ADDR (tunnel down?)"; exit 1; }

# module_metadata.json Cmd, with --addr replaced
mapfile -t CMD < <(jq -r '.container_template.Cmd[]' ../module_metadata.json 2>/dev/null || jq -r '.container_template.Cmd[]' "$HOME/repo/module_metadata.json")
for i in "${!CMD[@]}"; do [ "${CMD[$i]}" = "--addr" ] && CMD[$((i+1))]="$ADDR"; done
echo "Cmd: ${CMD[*]}"
RUN=(docker run --rm --network host --add-host HelinDataCollector:127.0.0.1
     -v "$PWD/config:/config:ro" -e IOTEDGE_MODULEID= -e DL_ON_FINISH=exit "$IMAGE")

echo; echo "=== a) template Cmd as-is (--loop), first cycle"
"${RUN[@]}" "${CMD[@]}" --interval 3600 > /tmp/tpl-loop.log 2>&1 &
pid=$!
for _ in $(seq 1 120); do sleep 5; grep -q 'cycle up to' /tmp/tpl-loop.log && break; done
kill "$pid" 2>/dev/null; docker ps -q --filter ancestor="$IMAGE" | xargs -r docker stop >/dev/null
grep -vE '^\s*$' /tmp/tpl-loop.log | grep -vE 'OK |NOT AVAILABLE' | tail -12

echo; echo "=== b) template Cmd (minus --loop) + a past window with data (range), into the HDC stand-in"
RANGE=(); for a in "${CMD[@]}"; do [ "$a" = "--loop" ] || RANGE+=("$a"); done
before=$(docker compose exec -T timescaledb psql -U helin -d helindb -tAc "SELECT count(*) FROM readings")
"${RUN[@]}" "${RANGE[@]}" --mode range --start "2026-04-22 12:00:00" --end "2026-04-22 12:10:00" --force 2>&1 \
  | grep -E 'Done\.|WARNING|ERROR|error:'
sleep 3
after=$(docker compose exec -T timescaledb psql -U helin -d helindb -tAc "SELECT count(*) FROM readings")
echo "onboard TimescaleDB rows: $before -> $after (+$((after - before)))"
