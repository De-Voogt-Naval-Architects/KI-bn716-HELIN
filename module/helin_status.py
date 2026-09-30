"""Answer the Helin portal's edge-module-requests while the extractor runs.

  get_health         extractor status as metrics (connected, rows, errors, ...)
  get_configuration  the effective settings (read-only)
  set_configuration  rejected - settings come from the container Cmd / environment
                     in the node template, so change them there and redeploy

Only starts inside the Helin/IoT Edge runtime (IOTEDGE_* environment) or, for
local tests, against a plain broker named by LOCAL_MQTT_HOST. Any failure here is
logged and ignored: the extraction never depends on the portal link.
"""

import os
import threading


class Handler:
    def __init__(self, args, signals, status):
        self.status = status
        self.settings = {
            "addr": args.addr, "mode": args.mode, "rate_hz": args.rate, "out": args.out,
            "signals_file": args.signals, "signal_count": len(signals),
            "start": args.start.isoformat() if args.start else None,
            "end": args.end.isoformat() if args.end else None,
            "interval_s": args.interval, "lag_s": args.lag, "chunk_minutes": args.chunk_minutes,
            "backfill_minutes": args.backfill_minutes, "keep_days": args.keep_days,
            "on_finish": args.on_finish,
            "dest_type": args.dest_type, "dest_addr": args.dest_addr, "insecure": args.insecure,
            "asset_mode": args.asset_mode, "datapoint": args.datapoint, "batch_size": args.batch_size,
        }

    def on_get_configuration(self, payload):
        return {"configuration": self.settings,
                "note": "Read-only: set flags in the node template's container Cmd or DL_* environment."}

    def on_set_configuration(self, payload):
        from helin_edge_sdk import ConfigurationFailedError
        raise ConfigurationFailedError(
            "This module is configured through its container Cmd / DL_* environment in the node "
            "template; change it there and redeploy.",
            status_code=400, current_configuration=self.settings,
        )

    def on_get_health(self, payload):
        s = dict(self.status)
        healthy = s["connected"] and s["last_error"] is None and not s.get("warning")
        return {
            "metrics": [
                {"name": "datalogger_connected", "value": int(s["connected"])},
                {"name": "healthy", "value": int(healthy)},
                {"name": "cycles", "value": s["cycles"]},
                {"name": "rows_total", "value": s["rows_total"]},
                {"name": "last_cycle_rows", "value": s["last_cycle_rows"]},
                {"name": "signals_ok", "value": s["signals_ok"]},
                {"name": "signals_no_data", "value": s["signals_no_data"]},
                {"name": "signals_error", "value": s["signals_error"]},
            ],
            "mode": s["mode"],
            "last_cycle_end": s["last_cycle_end"],
            "last_error": s["last_error"],
            "warning": s.get("warning"),
            "range_done": s["range_done"],
        }


def start(args, signals, status, log):
    """status: the extractor's live STATUS dict; log: its log function."""
    local = os.getenv("LOCAL_MQTT_HOST")
    if "IOTEDGE_MODULEID" not in os.environ and not local:
        return
    try:
        from helin_edge_sdk import EdgeModuleClient, EdgeModuleRequestsHandler

        class _Handler(Handler, EdgeModuleRequestsHandler):
            pass

        handler = _Handler(args, signals, status)
        if "IOTEDGE_MODULEID" in os.environ:
            client = EdgeModuleClient.from_edge_environment(handler)
        else:
            client = EdgeModuleClient.manual(handler, host=local,
                                             port=int(os.getenv("LOCAL_MQTT_PORT", "1883")),
                                             client_id="datalogger-extractor")
    except Exception as exc:  # never block extraction on the portal link
        log(f"Helin portal link not started: {exc!r}", err=True)
        return

    def run():
        try:
            client.run()
        except Exception as exc:
            log(f"Helin portal link stopped: {exc!r}", err=True)

    threading.Thread(target=run, name="helin-portal", daemon=True).start()
    log("Helin portal link started (get_health / get_configuration)")
