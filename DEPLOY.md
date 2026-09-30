# Deploying the DataLogger extractor to the bn716 edge node

The module pulls Rhodium DataLogger trends over gRPC (`DataLoggerTrends.GetTrend`) for the 169 bn716 tags in `module/signals/bn716_tags_subset.txt`, and writes CSVs on the node. It is your `container-mvp` extractor, adapted for a Helin edge module:

- **follow mode (default):** every `--interval` seconds it fetches what each tag logged since the last cycle and appends it to `/data/out/YYYY-MM-DD/<ClientSpecificId>.csv`. The files have the MVP's columns. Progress is saved in `/data/out/.state/follow.json`, so a restart resumes where it stopped, with no gaps or duplicates.
- **range mode:** a one-off backfill of `--start`..`--end` (one CSV per tag, the MVP's layout). It isn't repeated after a restart, and afterwards the module idles instead of exiting, so the edge runtime doesn't restart-loop it.
- **Portal:** the portal's **health** request returns connection state, row counts and the last error. The **configuration** request shows the effective flags (read-only).

## 1. Publish the image (once per version)

1. Set the six GitHub Action secrets (README → Required secrets). `REGISTRY_URL` / `REGISTRY_USER` / `REGISTRY_PASSWORD` are the container registry values.
2. Run `make register` once and commit the `module_id` / `module_uuid` it writes into `module.yaml`.
3. Tag and push, e.g. `git tag v0.1.0 && git push origin v0.1.0`. CI builds linux/amd64 and linux/arm64, pushes `<REGISTRY_URL>/datalogger-extractor:0.1.0` and publishes the module version.

## 2. Prepare the node (shell on the edge node)

Find the DataLogger machine and check the node can reach its gRPC port. `rpcAddress` in `DataLoggerGRPC.exe.config` is `0.0.0.0:50715`, which means "all interfaces". Use that machine's real IP:

```bash
nc -zv <datalogger-ip> 50715
```

Output goes to a named Docker volume (`datalogger-extractor-data`), which needs no preparation. To read the CSVs on the node:

```bash
sudo ls /var/lib/docker/volumes/datalogger-extractor-data/_data/
```

To write to a host folder instead, create it for the container user (uid 1000) first, then use `"/srv/datalogger:/data/out"` in `Binds` below:

```bash
sudo mkdir -p /srv/datalogger && sudo chown 1000:1000 /srv/datalogger
```

## 3. Configure the module in the node template (portal)

`module_metadata.json` carries the defaults the portal starts from. Its `--addr=DATALOGGER_HOST:50715` is a placeholder. In the node template, set the container's **Cmd** flags with the DataLogger machine's real IP (`10.0.51.25` below is only an example):

```json
"Cmd": [
  "--addr=10.0.51.25:50715",
  "--mode=follow",
  "--rate=1",
  "--interval=300",
  "--lag=60",
  "--backfill-minutes=60",
  "--keep-days=30"
],
"HostConfig": {
  "Binds": ["datalogger-extractor-data:/data/out"],
  "LogConfig": { "Type": "json-file", "Config": { "max-size": "10m", "max-file": "3" } }
}
```

Every flag can instead be set as an environment variable in the template: `DL_ADDR`, `DL_MODE`, `DL_RATE`, `DL_INTERVAL`, `DL_LAG`, `DL_BACKFILL_MINUTES`, `DL_KEEP_DAYS`, `DL_START`, `DL_END`, `DL_CHUNK_MINUTES`, `DL_SIGNALS`, and `DL_SIGNAL` (a comma-separated list). A Cmd flag wins over its environment variable.

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--addr` | `0.0.0.0:50052` | DataLogger `host:port`. **Always set it on the node.** `0.0.0.0:<port>` is dialled as `localhost` |
| `--mode` | `follow` (`range` if `--end` is given) | `follow` = continuous, `range` = one export |
| `--rate` | `1` | Hz. `1` = 1/s, `0.1` = 1 per 10 s, `1000` = unfiltered (every stored change) |
| `--interval` | `300` | follow: seconds between cycles |
| `--lag` | `60` | follow: stay this many seconds behind now |
| `--backfill-minutes` | `60` | follow: how far back the first cycle starts when there is no saved state and no `--start` |
| `--start` / `--end` | none | UTC, `YYYY-MM-DD[ HH:MM:SS]`. range: the window. follow: `--start` sets the first cycle's start |
| `--keep-days` | `0` | follow: delete day folders older than N days (`0` keeps everything) |
| `--chunk-minutes` | `1440` | Minutes per gRPC request. Halve it on `RESOURCE_EXHAUSTED` |
| `--signals` | bundled bn716 list | Path to a tag list inside the container |
| `--signal` | none | Extra tag. Repeatable |
| `--connect-attempts` | `0` | `0` retries forever, every 5 s |
| `--on-finish` | `idle` on the node | range: `idle` or `exit` |
| `--force` | off | range: re-run a range that already completed |
| `--no-helin` | off | Don't answer portal health/configuration requests |

### Examples

A one-week backfill, then idle (redeploy with follow flags afterwards):

```json
"Cmd": ["--addr=10.0.51.25:50715", "--mode=range", "--start=2026-04-01", "--end=2026-04-08", "--rate=1"]
```

Unfiltered data for two tags only:

```json
"Cmd": ["--addr=10.0.51.25:50715", "--signals=", "--signal=Fields.Navigation.GPS.SpeedOverGround", "--signal=Fields.Navigation.Compass.TrueHeading", "--rate=1000", "--chunk-minutes=60"]
```

## 4. Check it

Portal health (or `curl` with a platform token):

```bash
curl -s "$HELIN_INSTANCE/api/v1/edge-module-request/<node_uuid>/module/datalogger-extractor/health" -H "Authorization: Bearer $(scripts/get-token.sh)"
```

On the node (the container is named after the module in the template; `docker ps` shows it):

```bash
sudo docker ps --format '{{.Names}}  {{.Image}}' | grep datalogger
```

```bash
sudo docker logs --tail 20 <container name>
```

A healthy log shows one line per cycle, for example `cycle up to ...: 10140 rows, 169 signals with data, 0 without, 0 errors`.

## Disk use

In follow mode at `--rate=1`, a tag that updates every second produces about 86,400 rows (about 9 MB) of CSV a day. For all 169 tags that's up to about 1.5 GB a day. Most tags change less often, and the logger only stores changes. Use `--keep-days`, a lower `--rate`, or a shorter tag list to bound it.
