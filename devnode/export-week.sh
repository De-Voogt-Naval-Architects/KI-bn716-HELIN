#!/usr/bin/env bash
# Export a window of DataLogger data to per-tag CSVs (+ README) and zip it.
# Uses the module image in range mode, subscription mode (--rate 1000), csv destination.
# Usage (in the VM): START='2026-04-18' END='2026-04-25' NAME=bn715_... bash export-week.sh
set -euo pipefail
START="${START:-2026-04-18}"
END="${END:-2026-04-25}"
NAME="${NAME:-bn715_${START}_to_${END}_rate1000}"
ADDR="${ADDR:-127.0.0.1:50715}"
TAGS="${TAGS:-$HOME/app/devnode/config/tags}"
OUT="$HOME/exports/$NAME"

mkdir -p "$OUT"
rm -f "$OUT"/*.csv
cp "$TAGS" "$OUT/tags_requested.txt"
echo "$(date -u +%H:%M:%S)Z exporting $START .. $END UTC, $(grep -cv '^\s*\(#\|$\)' "$TAGS") tags -> $OUT"

docker run --rm --network host \
  -v "$TAGS:/config/tags:ro" -v "$OUT:/data/out" \
  datalogger-extractor:dev \
  --addr "$ADDR" --signals /config/tags --mode range --start "$START" --end "$END" \
  --rate 1000 --dest-type csv --out /data/out --chunk-minutes 360 \
  --on-finish exit --no-helin --force 2>&1 | tee "$OUT/export.log" | grep -E 'Done\.|ERROR|WARNING' || true

python3 - "$OUT" "$START" "$END" "$ADDR" <<'PY'
import csv, glob, os, sys, zipfile
out, start, end, addr = sys.argv[1:5]
rows, tags, units, size = {}, [], {}, 0
for path in sorted(glob.glob(os.path.join(out, "*.csv"))):
    n = 0
    with open(path, newline="", encoding="utf-8") as fh:
        r = csv.reader(fh); next(r)
        for row in r:
            n += 1
            if row[4] and path not in units:
                units[path] = row[4]
    tag = os.path.basename(path)[:-4]
    rows[tag] = n; size += os.path.getsize(path)
    tags.append((tag, n, units.get(path, "")))
requested = [l.strip() for l in open(os.path.join(out, "tags_requested.txt"), encoding="utf-8-sig") if l.strip() and not l.startswith("#")]
missing = sorted(set(requested) - set(rows))
with open(os.path.join(out, "tags_summary.csv"), "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh); w.writerow(["ValueCSId", "rows", "unit"])
    for t, n, u in tags: w.writerow([t, n, u])
    for t in missing: w.writerow([t, 0, ""])
with open(os.path.join(out, "README.txt"), "w", encoding="utf-8") as fh:
    fh.write(f"""DataLogger export - bn715 (test data for building the bn716 dashboards)

Window      : {start} 00:00 .. {end} 00:00 UTC (end exclusive)
Source      : DataLoggerGRPC (bn715 database), gRPC DataLoggerTrends.GetTrend at {addr}
Sample rate : 1000 = subscription mode: the samples exactly as stored (only when a value
              changed), not resampled - the same data the bn716 edge module sends to HDC
Tags        : {len(requested)} requested, {len(rows)} with data, {len(missing)} without (see tags_summary.csv)
Rows        : {sum(rows.values()):,} in total, {size / 1e9:.2f} GB of CSV

One CSV per tag, named after its ClientSpecificId. Columns:
  Timestamp_ms  Unix epoch milliseconds (UTC)
  DatetimeUTC   same instant, YYYY-MM-DD HH:MM:SS.mmm (UTC)
  Value         the reading; blank when Available is False
  Available     the logger's validity flag
  Unit          as stored - SI units: W, K (temperatures in Kelvin), m/s, Rad, m3, ...
  Name          the logger's short name
  ValueCSId     the full tag name (ClientSpecificId), so files can be concatenated

Notes
- Rows are irregular in time (stored on change): resample/forward-fill for plots.
- Several rows can share a millisecond; keep file order.
- bn716 will differ: some tags renamed/added/removed; units as stored (convert for display).
- On the edge, the module sends each sample to HDC as asset = ClientSpecificId,
  datapoint = "value" (default --asset-mode signal).
""")
zpath = out.rstrip("/") + ".zip"
with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for name in sorted(os.listdir(out)):
        z.write(os.path.join(out, name), arcname=os.path.join(os.path.basename(out), name))
print(f"tags with data {len(rows)}/{len(requested)}, rows {sum(rows.values()):,}, csv {size/1e9:.2f} GB, "
      f"zip {os.path.getsize(zpath)/1e6:.0f} MB -> {zpath}")
PY
echo "$(date -u +%H:%M:%S)Z done"
