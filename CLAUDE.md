# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Helin Platform edge module for yacht bn716, built from the Helin starter template. Its **primary function** is the **DataLogger extractor**. It pulls trend data out of the ship's Rhodium DataLogger over gRPC (`DataLoggerTrends.GetTrend`, defined in `module/Trending.proto`) and delivers it to one of two places:
- **The Helin Data Collector**, over HTTP: this is what the node template uses (`--dest-type http --dest-addr https://HelinDataCollector:6684/sensor-reading --insecure`).
- **CSV files** (`--dest-type csv`).

The node template in `module_metadata.json` is the user-supplied container create options, plus a state volume: the DataLogger at `192.168.192.5:50052`, and tags from the host file `/var/lib/helin/config/datalogger/tags` mounted at `/config/tags`. It started from the `container-mvp` extractor (`H:\Helin\container-mvp v1.zip`), and its request handling follows §1.2.5 of the Rhodium Datalogger Manual (4381F-T016-DL).

`DEPLOY.md` is the operator guide: publishing, preparing the node, and the flags to set in the portal's node template.

Paused work lives on the local branch `sea-margin-era5`: a sea margin module using ERA5 data from the CDS, a TimescaleDB/Grafana dev node, and dashboards. Don't mix it into this branch. **Don't push to GitHub unless the user says so.**

## Commands

The Windows workstation has no Python or Docker. Tests, `poetry lock`, stub generation and end-to-end runs all happen in the Multipass VM `helin-edge`, via `devnode\devnode.ps1`:

- `.\devnode\devnode.ps1 test`: runs `poetry lock`, regenerates the gRPC stubs and runs pytest in the VM, then copies `poetry.lock`, `Trending_pb2.py` and `Trending_pb2_grpc.py` back to `module/`.
- `.\devnode\devnode.ps1 up` / `sync`: builds the image and runs it **with the node template's Cmd** (only the address and interval differ) against the fake DataLogger. It delivers over HTTPS to a fake HDC (`helindatacollector`, alias `HelinDataCollector`, self-signed certificate generated in the VM), with Mosquitto and a gateway for the portal routes.
- `.\devnode\devnode.ps1 hdc`: shows what the fake HDC received. `logs` follows the extractor's log. `files` shows CSV output from a `real` run with `--dest-type csv`.
- `devnode/real-rates.sh` (run in the VM): the real logger → fake HDC at several rates. `devnode/probe_rate.py` prints the raw `GetTrend` answers per SampleRate and epoch.
- `.\devnode\devnode.ps1 real`: runs the container once in range mode against the **real** `DataLoggerGRPC.exe` on this PC (`C:\Users\svc_aiws\DataLoggerGRPC_bn715`, port 50715, bn715 database on `I:\bnXXX\Database`).
  - Windows Firewall blocks the VM from reaching port 50715, so it goes through an SSH reverse tunnel, which `real` detects on the VM's `127.0.0.1:50715`. Open the tunnel with: `ssh -i %USERPROFILE%\.ssh\helin-devnode -N -R 127.0.0.1:50715:127.0.0.1:50715 ubuntu@<vm-ip>`. Without the tunnel, `real` falls back to the VM's default gateway.
  - `REAL_START`, `REAL_END`, `REAL_RATE` and `REAL_MODE` override the test window and settings.
  - The server must be running in its own window (a keypress stops it) and allowed through Windows Firewall.

Inside `module/` with Poetry:
- `poetry run pytest tests/test_extract.py::test_follow_splits_days_and_resumes_without_gaps` runs a single test.
- `make proto` regenerates the stubs after editing `Trending.proto`.

The generated stubs are committed and pinned: grpcio 1.83.0 needs protobuf 7. Keep `grpcio`, `grpcio-tools` and `protobuf` in step in `pyproject.toml`.

Releases work the template's way (`make register` once, then push a `v*` tag). CI builds linux/amd64 and linux/arm64 only; `build.yml` also runs the tests.

## Architecture (`module/`)

- **`extract.py`**: the entrypoint (`ENTRYPOINT ["python", "/app/extract.py"]`). Flags come from the node template's container `Cmd`. Each flag also has a `DL_<FLAG>` environment variable default, and the Cmd flag wins.
  - `DataLogger.get_trend` tries millisecond epochs first, then falls back to seconds. It treats `NOT_FOUND`, `INVALID_ARGUMENT`, `OUT_OF_RANGE` and `UNKNOWN` as "no data" and raises anything else. `iter_samples` reads whichever `Values*` array the response populated. This logic is the MVP's, unchanged.
  - **range mode** keeps the MVP's behaviour: one CSV per signal in `--out`.
    - `IncludePrecedingSample` is on, so each file starts with a row before `--start`.
    - A completed range writes `.state/range_<...>.json` and is skipped on restart unless `--force`.
    - Afterwards `--on-finish=idle` (the default when `IOTEDGE_MODULEID` is set) keeps the module from restart-looping.
  - **follow mode** (the default) runs `follow_cycle` every `--interval` seconds.
    - Per signal it fetches `[cursor, now - lag)`, with `IncludePrecedingSample` off and rows filtered to that window. It appends them to `YYYY-MM-DD/<csid>.csv` (UTC days, header only on new files).
    - It saves the cursor to `.state/follow.json` atomically after each signal.
    - A gRPC error leaves that signal's cursor unchanged, so it's retried; `UNAVAILABLE` ends the cycle. `--keep-days` prunes old day folders.
  - `STATUS` is the live state shared with the portal handler.
- **`destinations.py`**: `CsvDaySink` writes daily files. `HttpSink`/`HttpClient` post to HDC's http_south in the Fledge format `[{"timestamp": "YYYY-MM-DD HH:MM:SS.ffffff+00:00", "asset": ..., "readings": {dp: value}}]`.
  - `--asset-mode signal` sends asset = ClientSpecificId with datapoint `value`. `group` sends the parent path as the asset and the last part as the datapoint.
  - HTTP skips `Available=False` samples unless `--send-unavailable`.
  - A failed post raises `DestinationError`: the cycle stops and cursors aren't advanced.
  - `send_signal` returns rows *delivered* (`sink.count`), not rows read.
- **`--rate 1000`** is the MVP's "unfiltered" sentinel. The bn715 test logger answers it with an empty OK response (and treats other rates as resample-to-N-per-second). `check_all_empty` turns "every signal empty" into `STATUS["warning"]`, which also makes portal health unhealthy. Don't treat an empty response as proof there's no data.
- **`helin_status.py`**: answers the portal's edge-module-requests on a background thread using `helin-edge-sdk`.
  - `get_health` returns metrics from `STATUS`. `get_configuration` returns the effective flags. `set_configuration` is rejected with 400, because settings live in the template's Cmd or environment.
  - It starts only under the IoT Edge runtime (`IOTEDGE_*`), or with `LOCAL_MQTT_HOST` for the dev node. Failures are logged and never stop the extraction.
  - `start()` is given `STATUS` and `log` explicitly, because `extract.py` runs as `__main__`; importing `extract` there would create a second, empty `STATUS`.
- **`tests/fake_datalogger.py`**: a gRPC `DataLoggerTrends` server with deterministic data.
  - One sample per second on exact multiples of the period; value `(t_s % 1000)`; unavailable on whole minutes.
  - `Missing.*` returns `NOT_FOUND`. `reject_ms=True` mimics a seconds-only logger.
  - The tests and the dev node's `fake-datalogger` service both use it.

## Node deployment facts

- The container runs as uid 1000 and writes to `/data/out`. The default template uses the named volume `datalogger-extractor-data`. A host bind mount must be `chown 1000:1000`, and `check_writable` exits with that hint otherwise.
- `--addr` must be the DataLogger machine's real IP. `0.0.0.0:<port>`, as in `DataLoggerGRPC.exe.config`, is rewritten to `localhost`, which is only right when the logger runs on the same host.
- The CSV columns are `Timestamp_ms, DatetimeUTC, Value, Available, Unit, Name, ValueCSId`. `Value` is blank when `Available` is False. Units are as stored (temperatures in K).
