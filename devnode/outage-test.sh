#!/usr/bin/env bash
# "No internet on board": cut the Cloud HDC's network link, show the onboard copy
# (edge HDC -> TimescaleDB -> Grafana) carrying on, restore the link, and show the
# cloud output catching up with nothing lost.
# Usage (in the VM): bash outage-test.sh [seconds offline, default 180]
set -u
cd "${DEVNODE_DIR:-$HOME/app/devnode}"
OFFLINE="${1:-180}"
NET=helin-devnode_default
CLOUD=$(docker compose ps -q cloud-hdc)

edge() {
  python3 - <<'PY'
import json, ssl, urllib.request
c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
s = json.load(urllib.request.urlopen("https://127.0.0.1:6684/stats", context=c, timeout=10))
print(f"  edge HDC accepted from extractor : {s['readings']} readings")
for name, n in s["north"].items():
    err = f"  ({n['last_error'][:70]})" if n["last_error"] else ""
    print(f"  north {name:<10}: written {n['written']:>9}  backlog {n['backlog']:>7}  purged {n['purged']}{err}")
PY
}
cloud() {
  docker exec "$CLOUD" python -c "import json,ssl,urllib.request as u; c=ssl.create_default_context(); c.check_hostname=False; c.verify_mode=ssl.CERT_NONE; print(json.load(u.urlopen('https://localhost:6684/stats', context=c, timeout=5))['readings'])" 2>/dev/null || echo "?"
}
onboard() {
  docker compose exec -T timescaledb psql -U helin -d helindb -tA \
    -c "SELECT count(*) || ' rows in the last hour, newest ' || round(extract(epoch FROM now() - max(\"timestamp\"))) || ' s old' FROM readings WHERE \"timestamp\" > now() - interval '1 hour'"
}
snapshot() {
  echo
  echo "=== $1   ($(date -u +%H:%M:%S)Z)"
  edge
  echo "  Cloud HDC has received         : $(cloud) readings"
  echo "  onboard TimescaleDB            : $(onboard)"
}

snapshot "before: internet up"

echo; echo ">>> cutting the link to the Cloud HDC for ${OFFLINE}s"
docker network disconnect "$NET" "$CLOUD"
sleep "$OFFLINE"
snapshot "after ${OFFLINE}s without internet"
echo "  extractor meanwhile:"
docker compose logs --no-color --since "${OFFLINE}s" extractor 2>/dev/null | grep 'cycle up to' | tail -3 | sed 's/^/    /'

echo; echo ">>> restoring the link"
docker network connect --alias cloud-hdc "$NET" "$CLOUD"
for _ in $(seq 1 60); do
  sleep 5
  backlog=$(python3 -c "import json,ssl,urllib.request as u; c=ssl.create_default_context(); c.check_hostname=False; c.verify_mode=ssl.CERT_NONE; print(json.load(u.urlopen('https://127.0.0.1:6684/stats', context=c))['north']['cloud']['backlog'])")
  [ "$backlog" = "0" ] && break
done
snapshot "link restored, cloud backlog drained"
