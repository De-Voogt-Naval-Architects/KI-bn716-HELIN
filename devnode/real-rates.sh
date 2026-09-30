#!/usr/bin/env bash
# Real DataLogger -> fake HDC over HTTPS, all bn716 tags, at each given rate.
# Usage (in the VM, from ~/app/devnode): bash real-rates.sh [rate ...]   (default: 1 1000)
cd "${DEVNODE_DIR:-$HOME/app/devnode}"
export DATALOGGER_ADDR="${DATALOGGER_ADDR:-127.0.0.1:50715}"
python3 show-hdc.py | head -1
for rate in "${@:-1 1000}"; do
  for r in $rate; do
    echo
    echo "=== --rate $r"
    REAL_RATE="$r" docker compose --profile real run --rm -T real 2>&1 | grep -E 'rate  |Done\.|WARNING'
    python3 show-hdc.py | head -1
  done
done
