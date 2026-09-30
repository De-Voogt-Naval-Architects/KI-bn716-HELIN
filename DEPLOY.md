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

## 3. `--rate 1000` is subscription mode

`--rate` is the `SampleRate` of the gRPC request. **`1000` is the DataLogger's subscription mode: it returns the samples exactly as stored**, which is what the template uses to copy the data. Any other value resamples to that many values per second. Tested against `DataLoggerGRPC_bn715` on the office PC on 30 Sep:

| `--rate` | 29 Apr 12:00–13:00, total fuel level | Days with nothing stored |
|---|---|---|
| `1000` | 24,510 stored samples, all valid | **empty answer, no error** |
| `1` | 3,599: a 1-per-second grid | 3,599 rows, all `Available=False` |
| `999` | about 3.6 million interpolated values | same, all unavailable |

So in subscription mode an empty answer means "nothing stored in this window". If every tag comes back empty in a cycle, the module reports `WARNING No data from any of N signals at --rate 1000 ...` in its log and in the portal's health response (`"warning"`, and `healthy` goes to 0). Check that the DataLogger is recording.

Real run on 30 Sep: `--rate 1000`, 29 Apr 12:00–12:10, through HTTPS to an HDC stand-in. 26 of the 169 bn716 tags had stored samples: **31,604 readings delivered, 0 errors**.

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

A healthy cycle looks like `cycle up to ...: 31604 rows, 26 signals with data, 143 without, 0 errors`. "Without" means tags with no stored samples in that window, or whose samples were all `Available=False`: HTTP delivery skips those by default. Add `--send-unavailable` to send them with a `null` value, but check that HDC accepts nulls before relying on it.

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
