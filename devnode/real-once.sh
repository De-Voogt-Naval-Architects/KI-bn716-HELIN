#!/usr/bin/env bash
# One real-DataLogger run (compose service "real"), with a per-tag summary.
# Usage (in the VM): REAL_RATE=1000 REAL_START='...' REAL_END='...' bash real-once.sh
cd "${DEVNODE_DIR:-$HOME/app/devnode}"
export DATALOGGER_ADDR="${DATALOGGER_ADDR:-127.0.0.1:50715}"
docker compose --profile real run --rm -T real 2>&1 | grep -E ' OK | ERROR |Done\.|WARNING|rate  |range ' > /tmp/real-once.log
grep -E 'rate  |range ' /tmp/real-once.log
grep ' OK ' /tmp/real-once.log | awk '{print $(NF-1), $(NF-2)}' | sort -rn | head -12
grep ' OK ' /tmp/real-once.log | awk '{s+=$(NF-1)} END {print "total readings delivered:", s+0}'
grep -E 'ERROR|Done\.|WARNING' /tmp/real-once.log
