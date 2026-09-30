# Deploying the DataLogger extractor to the bn716 edge node

The module pulls Rhodium DataLogger trends over gRPC (`DataLoggerTrends.GetTrend`) and hands each sample to the **Helin Data Collector** through its HTTP input (`https://HelinDataCollector:6684/sensor-reading`). It runs continuously, and after a restart it resumes where it stopped. It is the `container-mvp` extractor, adapted into a Helin edge module. It can still write CSVs instead (`--dest-type csv`).

## 1. The node template (container create options)

This is what `module_metadata.json` ships:

```json
{
  "HostConfig": {
    "Binds": [
      "/var/lib/helin/config/datalogger:/config:ro",
      "datalogger-extractor-state:/data/out"
    ]
  },
  "Cmd": [
    "--addr", "192.168.192.5:50052",
    "--signals", "/config/tags",
    "--loop",
    "--rate", "1000",
    "--dest-type", "http",
    "--dest-addr", "https://HelinDataCollector:6684/sensor-reading",
    "--insecure"
  ]
}
```

The second bind, `datalogger-extractor-state:/data/out`, is an addition to the options you were given. It keeps the extractor's position (`.state/follow-http.json`) across **redeploys**. Without it, every redeploy recreates the container and loses that position. The extractor then starts from `--backfill-minutes` ago (60 by default), which re-sends data HDC already has, or skips anything older if the module was down longer than that. Remove it if that's acceptable.

What the flags do:
- `--loop`: runs continuously. Every `--interval` seconds (default 300) it fetches `[last position, now − --lag)` for each tag (lag default 60 s).
- `--dest-type http`: posts batches of `--batch-size` readings (default 500) in HDC's http_south JSON format:
  `[{"timestamp": "2026-04-29 18:00:01.000000+00:00", "asset": "<ClientSpecificId>", "readings": {"value": 47.58}}]`
- `--insecure`: accepts HDC's self-signed certificate.
- On failure: if HDC or the DataLogger is unreachable, the tag's position isn't advanced, so the same window is retried next cycle and nothing is lost. A batch posted just before a failure can be sent twice.

## 2. Prepare the node (shell on the edge node)

Put the tag list where the template mounts it. The file is `module/signals/bn716_tags_subset.txt` in this repo: one `ClientSpecificId` per line, and `#` starts a comment.

```bash
sudo mkdir -p /var/lib/helin/config/datalogger
```

```bash
sudo cp bn716_tags_subset.txt /var/lib/helin/config/datalogger/tags && sudo chmod 644 /var/lib/helin/config/datalogger/tags
```

Check that the node reaches the DataLogger's gRPC port, and that HDC's HTTP input is listening. The HDC needs an HTTP south service on port 6684.

```bash
nc -zv 192.168.192.5 50052
```

```bash
sudo docker ps --format '{{.Names}}' | grep -i datacollector
```

## 3. `--rate 1000`: check it on the ship's logger first

`--rate` is the `SampleRate` of the gRPC request. Two DataLoggers answer it differently:

| DataLogger | `--rate 1` | `--rate 10` / `999` | `--rate 1000` |
|---|---|---|---|
| The one `container-mvp` was developed against (port 50052) | 1/s | | **unfiltered**: every stored change (867,575 rows per week for one AC power tag) |
| `DataLoggerGRPC_bn715` on the office PC (port 50715), tested 30 Sep | 599 values per 10 min | resampled to 10 / 999 values **per second**, even for a constant tag | **empty answer, no error** |

On a logger that behaves like the second one, `--rate 1000` delivers nothing. The module then reports `WARNING No data from any of 169 signals at --rate 1000 ... try --rate 1` in its log and in the portal's health response (`"warning"`, and `healthy` goes to 0). If you see that on bn716, change the template to `"--rate", "1"`. Avoid high values like 999 on such a logger: they are interpolated, not measured.

## 4. Check it's running

Portal health, or the same call with `curl` and a platform token:

```bash
curl -s "$HELIN_INSTANCE/api/v1/edge-module-request/<node_uuid>/module/datalogger-extractor/health" -H "Authorization: Bearer $(scripts/get-token.sh)"
```

The response includes `datalogger_connected`, `healthy`, `rows_total`, `last_cycle_rows`, `signals_ok`, `signals_no_data`, `signals_error`, `last_error` and `warning`.

On the node (the container is named after the module in the template):

```bash
sudo docker ps --format '{{.Names}}  {{.Image}}' | grep -i datalogger
```

```bash
sudo docker logs --tail 20 <container name>
```

A healthy cycle looks like `cycle up to ...: 29351 rows, 49 signals with data, 120 without, 0 errors`. "Without" includes tags whose samples were all `Available=False`: HTTP delivery skips those by default. Add `--send-unavailable` to send them with a `null` value, but check that HDC accepts nulls before relying on it.

## 5. Publish the image (once per version)

1. Set the six GitHub Action secrets (README → Required secrets). The registry URL, user and password are in `H:\Helin\cred.txt`. `HELIN_INSTANCE`, `HELIN_CLIENT_ID` and `HELIN_CLIENT_SECRET` come from Helin.
2. Run `make register` once and commit the `module_id` / `module_uuid` it writes into `module.yaml`.
3. Push a tag, e.g. `git tag v0.1.0 && git push origin v0.1.0`. CI builds linux/amd64 and linux/arm64, pushes `<REGISTRY_URL>/datalogger-extractor:0.1.0` and publishes the module version.

## All flags

Every flag also has a `DL_<FLAG>` environment variable (for example `DL_RATE`, `DL_DEST_ADDR`; `DL_SIGNAL` takes a comma-separated list). The Cmd flag wins.

| Flag | Default | Meaning |
|---|---|---|
| `--addr` | `0.0.0.0:50052` | DataLogger `host:port`. `0.0.0.0:<port>` is dialled as `localhost` |
| `--signals` / `--signal` | bundled bn716 list / none | Tag list file / an extra tag (repeatable) |
| `--loop` / `--mode` | follow | `--loop` = `--mode follow` (continuous). `--mode range` with `--start`/`--end` = one export, then idle |
| `--rate` | `1` | `SampleRate` in Hz. See section 3 |
| `--dest-type` | `csv` | `http` = post to HDC. `csv` = files under `--out` |
| `--dest-addr` | none | http: endpoint URL |
| `--insecure` | off | http: don't verify the TLS certificate |
| `--batch-size` / `--http-timeout` | `500` / `30` s | http: readings per POST / seconds per POST |
| `--asset-mode` / `--datapoint` | `signal` / `value` | http: asset = the ClientSpecificId with datapoint `value`; or `group` = asset `Fields.Navigation.GPS`, datapoint `SpeedOverGround` |
| `--send-unavailable` | off | http: also send `Available=False` samples as `null` |
| `--interval` / `--lag` | `300` / `60` s | follow: cycle period / distance behind now |
| `--backfill-minutes` | `60` | follow: first start when there's no saved state and no `--start` |
| `--start` / `--end` | none | UTC `YYYY-MM-DD[ HH:MM:SS]` |
| `--out` | `/data/out` | csv output, and where `.state/` is kept for both destinations |
| `--keep-days` | `0` | follow, csv: delete day folders older than N days |
| `--chunk-minutes` | `1440` | Minutes per gRPC request. Lower it on `RESOURCE_EXHAUSTED` |
| `--connect-attempts` | `0` | `0` retries forever, every 5 s |
| `--on-finish` / `--force` | `idle` on the node / off | range: after finishing / re-run a completed range |
| `--no-helin` | off | Don't answer portal health/configuration requests |
