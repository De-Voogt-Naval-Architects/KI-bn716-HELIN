-- What the HDC Timescale north (flatmap) has written, as the local Grafana sees it.
-- multipass exec helin-edge -- bash -lc "cd ~/app/devnode && docker compose exec -T timescaledb psql -U helin -d helindb < replica.sql"
SELECT count(*) AS rows, count(DISTINCT asset) AS assets,
       min("timestamp") AS first, max("timestamp") AS last,
       round(extract(epoch FROM now() - max("timestamp"))) AS age_s
FROM readings WHERE "timestamp" > now() - interval '1 day';

SELECT asset, datapoint, count(*) AS n, max("timestamp") AS last, (array_agg(value ORDER BY "timestamp" DESC))[1] AS latest
FROM readings WHERE "timestamp" > now() - interval '1 day'
GROUP BY 1, 2 ORDER BY 1 LIMIT 8;
