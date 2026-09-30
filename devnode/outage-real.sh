#!/usr/bin/env bash
# Onboard-independence test with the REAL DataLogger:
#   1. clean the dev TimescaleDB and reset the HDC stand-ins
#   2. cut the Cloud HDC link ("no internet")
#   3. run the extractor (node template flags, test tags, one past day) to completion
#   4. show: everything landed in the onboard TimescaleDB (and so in Grafana),
#      while the cloud output queued it
#   5. restore the link and show the cloud backlog draining to zero
# Usage (in the VM): TEST_START=... TEST_END=... bash outage-real.sh
set -u
cd "${DEVNODE_DIR:-$HOME/app/devnode}"
export TEST_START="${TEST_START:-2026-04-29 00:00:00}" TEST_END="${TEST_END:-2026-04-30 00:00:00}"
NET=helin-devnode_default

stats() {
  python3 - <<'PY'
import json, ssl, urllib.request
c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
s = json.load(urllib.request.urlopen("https://127.0.0.1:6684/stats", context=c, timeout=10))
print(f"  edge HDC accepted from extractor : {s['readings']} readings, {s['assets']} assets")
for name, n in s["north"].items():
    err = f"  ({n['last_error'][:60]})" if n["last_error"] else ""
    print(f"  north {name:<10}: written {n['written']:>9}  backlog {n['backlog']:>8}  purged {n['purged']}{err}")
PY
}
cloud_count() {
  docker exec "$(docker compose ps -q cloud-hdc)" python -c "import json,ssl,urllib.request as u; c=ssl.create_default_context(); c.check_hostname=False; c.verify_mode=ssl.CERT_NONE; print(json.load(u.urlopen('https://localhost:6684/stats', context=c, timeout=5))['readings'])" 2>/dev/null || echo "unreachable"
}
onboard() {
  docker compose exec -T timescaledb psql -U helin -d helindb -tA -F ' | ' \
    -c "SELECT count(*), count(DISTINCT asset), min(\"timestamp\"), max(\"timestamp\") FROM readings"
}
snapshot() {
  echo; echo "=== $1   ($(date -u +%H:%M:%S)Z)"
  stats
  echo "  Cloud HDC has received          : $(cloud_count) readings"
  echo "  onboard TimescaleDB (rows | tags | first | last): $(onboard)"
}
backlog() {
  python3 -c "import json,ssl,urllib.request as u; c=ssl.create_default_context(); c.check_hostname=False; c.verify_mode=ssl.CERT_NONE; print(json.load(u.urlopen('https://127.0.0.1:6684/stats', context=c))['north']['cloud']['backlog'])"
}

echo ">>> clean start: empty readings table, reset HDC stand-ins, stop any extractor"
docker compose --profile extractor --profile fake rm -sf extractor fake-datalogger >/dev/null 2>&1
docker volume rm -f helin-devnode_extractor-state >/dev/null
docker compose exec -T timescaledb psql -U helin -d helindb -qc "TRUNCATE readings" 2>/dev/null
docker compose restart helindatacollector cloud-hdc >/dev/null 2>&1
sleep 5
echo "tags: $(grep -cv '^\s*\(#\|$\)' config/tags) in config/tags;  window: $TEST_START .. $TEST_END UTC"
snapshot "before"

echo; echo ">>> cutting the Cloud HDC link (no internet on board)"
docker network disconnect "$NET" "$(docker compose ps -q cloud-hdc)"

echo ">>> running the extractor against the real DataLogger"
docker compose --profile extractor up -d extractor >/dev/null 2>&1
for _ in $(seq 1 360); do
  sleep 5
  docker compose logs --no-color extractor 2>/dev/null | grep -q 'Done\.' && break
done
docker compose logs --no-color extractor 2>/dev/null | grep -E 'Done\.|WARNING|ERROR' | sed 's/^/  /'
docker compose logs --no-color extractor 2>/dev/null | grep ' OK ' | awk '{print $(NF-1), $(NF-2)}' | sort -rn | head -8 | sed 's/^/    /'
sleep 5
snapshot "extraction finished, still no internet"

echo; echo ">>> restoring the Cloud HDC link"
docker network connect --alias cloud-hdc "$NET" "$(docker compose ps -q cloud-hdc)"
for _ in $(seq 1 120); do
  sleep 5
  [ "$(backlog)" = "0" ] && break
done
snapshot "link restored"
