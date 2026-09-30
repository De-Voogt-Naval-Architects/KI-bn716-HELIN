#!/usr/bin/env bash
# Summarise an extractor output volume: day folders, file counts, one sample file.
# Usage (in the VM): bash show-output.sh extractor-data|real-data
set -euo pipefail
root="/var/lib/docker/volumes/helin-devnode_${1:-extractor-data}/_data"
sudo test -d "$root" || { echo "no volume data at $root"; exit 0; }
echo "== $root"
sudo find "$root" -maxdepth 1 -mindepth 1 -printf '%f\n' | sort
for dir in $(sudo find "$root" -maxdepth 1 -mindepth 1 -type d -name '20*' -printf '%f\n' | sort); do
  n=$(sudo find "$root/$dir" -name '*.csv' | wc -l)
  rows=$(sudo find "$root/$dir" -name '*.csv' -exec cat {} + | grep -vc '^Timestamp_ms' || true)
  echo "  $dir: $n files, $rows rows"
done
sample=$(sudo find "$root" -name '*.csv' | sort | head -1)
if [ -n "$sample" ]; then
  echo "== ${sample#$root/} (first 3 lines, last line, rows)"
  sudo head -3 "$sample"; sudo tail -1 "$sample"; sudo grep -vc '^Timestamp_ms' "$sample"
fi
if sudo test -f "$root/.state/follow.json"; then
  echo "== follow state: $(sudo python3 -c "import json,sys; d=json.load(open('$root/.state/follow.json')); s=d['signals']; print(len(s), 'signals, cursors', min(s.values()), '..', max(s.values()), 'rate', d['rate'])")"
fi
sudo find "$root/.state" -name 'range_*.json' -exec sh -c 'echo "== $(basename {})"; cat {}; echo' \; 2>/dev/null || true
