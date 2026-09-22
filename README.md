# Paws & Cart / Online Pet Store

A small online pet store project built with a Flask API, SQLite persistence, and a React + Vite frontend, instrumented end-to-end with Prometheus, Grafana, and the Elastic Stack (Filebeat, Elasticsearch, Kibana).

## Current architecture

- Frontend: React + Vite application served through nginx in a production container
- Backend: Flask REST API running in a dedicated Docker container
- Database: SQLite persisted in a Docker named volume at /app/data
- Metrics: Prometheus and Grafana running in Docker
- Infrastructure metrics: Node Exporter for the host machine running Docker
- Application metrics: Prometheus client metrics exposed at /metrics for the Flask backend
- Logs: Filebeat -> Elasticsearch -> Kibana, reading the backend's structured JSON logs from Docker
- Runtime: Docker Desktop on Windows (project files live on the Windows filesystem, not inside WSL)

## Prerequisites

- Docker Desktop for Windows (or Docker Engine + Compose on Linux/Mac)
- Python 3.12 and Node.js 20+ only if you want to run the frontend/backend natively outside Docker; not required for the Docker-based setup below

## Docker setup (recommended path)

Build and start every service:

```bash
docker compose up --build -d
```

View running services:

```bash
docker compose ps
```

Rebuild and restart just the backend after a code change:

```bash
docker compose up -d --build backend
```

If you only changed an environment variable or resource limit (not code), a full recreate is more reliable than a plain restart:

```bash
docker compose up -d --force-recreate <service-name>
```

Stop the project (keeps all named volumes/data):

```bash
docker compose stop
```

Do not run `docker compose down -v`, `docker volume prune`, or `docker system prune` for routine shutdown — these delete stored metrics/log history.

## Service ports

- Frontend: http://localhost:5173
- Backend: http://localhost:5000
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (login: admin / admin)
- Kibana: http://localhost:5601
- Elasticsearch: http://localhost:9200
- Node Exporter: http://localhost:9100

## Persistence

SQLite data, Prometheus's TSDB, Grafana's settings, and Elasticsearch's indices each live in their own named Docker volume, so they all survive normal container restarts and rebuilds.

## Application metrics

The backend exposes Prometheus metrics at http://localhost:5000/metrics.

### Implemented metrics

| Metric | Type | Category | Purpose | Unit | Labels | Code location | When recorded |
| --- | --- | --- | --- | --- | --- | --- | --- |
| petstore_orders_total | Counter | Business metric | Count successfully created orders | requests | none | app.py, metrics.py | After a successful POST /orders commit |
| petstore_pending_orders | Gauge | Business metric | Reflect the current number of pending orders in SQLite | orders | none | metrics.py | Reconstructed from SQLite on startup and after create/complete events |
| petstore_http_request_duration_seconds | Histogram | Application metric | Measure HTTP request latency | seconds | method, route, status_code | app.py, metrics.py | Flask before/after request hooks |
| petstore_order_processing_seconds | Summary | Business timing metric | Measure successful order creation processing time | seconds | none | app.py, metrics.py | After successful POST /orders completes |

### PromQL examples

```promql
histogram_quantile(
  0.95,
  sum by (le) (rate(petstore_http_request_duration_seconds_bucket[5m]))
)
```

```promql
histogram_quantile(
  0.99,
  sum by (le) (rate(petstore_http_request_duration_seconds_bucket[5m]))
)
```

```promql
rate(petstore_order_processing_seconds_sum[5m])
/
rate(petstore_order_processing_seconds_count[5m])
```

### Histogram vs Summary note

The Histogram is used for latency distribution and p95/p99 via `histogram_quantile()`.

The Summary exposes `_sum` and `_count` for average processing time in Python's `prometheus_client` implementation. It does not provide client-side quantile values, so p95/p99 must be calculated from the Histogram instead.

## Grafana dashboards

Grafana dashboards are provisioned as code and loaded automatically from the provisioning folder when the Grafana container starts.

### Included dashboards

- Pet Store application/business dashboard: `Paws & Cart - Application & Business Metrics`
- System overview dashboard: `Paws & Cart - System Overview`

### Dashboard contents and queries

#### Application & business dashboard

- Total successful orders: `petstore_orders_total`
- Pending orders: `petstore_pending_orders`
- Current request rate: `sum(rate(petstore_http_request_duration_seconds_count[5m]))`
- Average order processing time: `1000 * rate(petstore_order_processing_seconds_sum[5m]) / rate(petstore_order_processing_seconds_count[5m])`
- HTTP latency p95: `histogram_quantile(0.95, sum by (le) (rate(petstore_http_request_duration_seconds_bucket[5m])))`
- HTTP latency p99: `histogram_quantile(0.99, sum by (le) (rate(petstore_http_request_duration_seconds_bucket[5m])))`
- Self-explored route rate: `sum by (route) (rate(petstore_http_request_duration_seconds_count[5m]))`

Note: these p95/p99 panels use a 5-minute rolling window (`[5m]`). This means a real, brief anomaly takes a few minutes to fully "age out" of the graph even after the underlying cause is fixed — the fix itself is instant, but the chart's smoothing window lags behind. See the anomaly experiment below for a concrete example of this.

#### System overview dashboard

- CPU Usage % Stat: `100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)`
- Memory Usage % Stat: `100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))`
- Disk Usage % Stat: `100 * (1 - (node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}))`
- CPU usage trend: `100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)`
- Memory usage trend: `node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes`
- Disk usage trend: `node_filesystem_size_bytes{fstype="ext4",mountpoint="/"} - node_filesystem_avail_bytes{fstype="ext4",mountpoint="/"}`
- Network receive/transmit trend: `rate(node_network_receive_bytes_total[5m])` and `rate(node_network_transmit_bytes_total[5m])`

These charts come from Node Exporter and represent the Docker Desktop host machine running the whole stack.

All four required metric types are covered: `petstore_orders_total` (Counter), `petstore_pending_orders` (Gauge), `petstore_http_request_duration_seconds` (Histogram), and `petstore_order_processing_seconds` (Summary).

## Part C: Structured Logging and Elastic Observability

Pipeline for backend HTTP events:

```text
Flask JSON stdout -> Docker json-file log -> Filebeat -> Elasticsearch -> Kibana
```

### Structured application logging

The logging formatter and request hooks are in `app.py`. Every backend HTTP request emits one JSON object to stdout with these fields:

- `time`: UTC ISO-8601 timestamp
- `service`: `pet-store`
- `severity`: `INFO`, `WARNING`, or `ERROR`
- `message`: `HTTP request completed`
- `request_id`: supplied `X-Request-ID`, or a generated UUID
- `method`, `route`, `status_code`, and `duration_ms`

The response always includes the same `X-Request-ID`. Flask URL rules are used for normalized routes, so `/orders/<int:order_id>/complete` remains one route category. Request IDs are never Prometheus labels (see the cardinality experiment below for why). Four-hundred-level responses are logged as `WARNING`; five-hundred-level responses are logged as `ERROR`. Request bodies, credentials, tokens, and personal information are not logged.

### Docker, Filebeat, and storage

The backend service uses Docker's `json-file` logging driver with a 10 MiB / 3-file rotation limit. Filebeat reads Docker's log directory read-only and uses the read-only Docker socket only for metadata/discovery.

`monitoring/filebeat/filebeat.yml` uses Docker autodiscover and accepts only containers with the Compose label `com.petstore.role=backend`. The container input parses Docker's wrapper and `decode_json_fields` parses the inner Flask JSON into searchable fields. The output data stream is `petstore-final`.

Elasticsearch, Kibana, and Filebeat are pinned to `8.15.3`. Elasticsearch runs single-node with security disabled (local assignment only, no credentials committed). Current resource settings: Elasticsearch has a 512 MiB JVM heap (`-Xms512m -Xmx512m`) and a 1 GiB container memory limit; Kibana has a 1536 MiB Node.js heap (`--max-old-space-size=1536`) and a 2 GiB container memory limit; Filebeat has a 128 MiB limit.

The named Elasticsearch volume survives normal `docker compose stop/start` unless volumes are explicitly removed. Docker log files survive ordinary container restarts subject to rotation. There is no automatic Elasticsearch retention policy configured; retention is bounded only by available storage and Docker's log rotation.

### Kibana data view and searches

Open http://localhost:5601, go to Stack Management -> Data Views, and create a data view named `petstore-final*` with `@timestamp` as the time field. Useful KQL searches:

```text
request_id: "assignment-success-001"
request_id: "assignment-error-001"
severity: ("WARNING" or "ERROR")
```

### Controlled evidence requests

```bash
curl -i -H "X-Request-ID: assignment-success-001" http://localhost:5000/products

curl -i -X POST -H "Content-Type: application/json" \
  -H "X-Request-ID: assignment-error-001" \
  -d '{"product_id":1,"quantity":0}' \
  http://localhost:5000/orders
```

The first returns `200`, the second `400`; each response echoes its request ID back. Both are individually searchable in Kibana Discover by `request_id`, confirmed end-to-end.

### Part C status: verified end-to-end

Elasticsearch, Filebeat, and Kibana are all confirmed healthy. The `petstore-final` data view shows 112 parsed fields (not just raw text), and both a successful and an error controlled request were independently located in Kibana Discover by their `request_id`. Kibana initially failed to start with a Node.js out-of-memory crash under its original 256 MiB heap cap; raising the heap and the Elasticsearch JVM heap (see settings above) resolved it. See "Known issues" below for a second, unrelated blocker encountered along the way.

## Part D: System Design

See the accompanying report for the full architecture diagram and the "follow one metric, follow one log" trace through the system (from `app.py` code, through Prometheus/Grafana and Filebeat/Elasticsearch/Kibana respectively, with real values from this project).

## Part E: Experiments

### E.1 — Reproducible anomaly (slowdown)

A reversible, environment-variable-gated delay is built into `app.py`: when `PETSTORE_INJECT_DELAY=true` (set in `docker-compose.yml`'s backend `environment:` block), every 5th request sleeps ~500ms before being handled, simulating a slow dependency. Default is `"false"` — the app behaves normally unless explicitly toggled.

To run the experiment:

```bash
# Baseline (delay off): run a load test, note p95/p99 in Grafana.

# Turn the delay on:
# edit docker-compose.yml -> PETSTORE_INJECT_DELAY: "true"
docker compose up -d --build backend

# Re-run the same load test, compare p95/p99 (expect a visible spike).

# Turn it back off:
# edit docker-compose.yml -> PETSTORE_INJECT_DELAY: "false"
docker compose up -d --build backend

# Re-run once more to confirm recovery.
```

Result: with the delay on, a burst of 50 rapid requests produced a clear p95 spike to ~700ms and p99 to ~950ms-1s against a near-zero baseline. Turning the delay off and re-testing showed individual request times immediately back to 20-100ms, confirming instant recovery at the request level (see the "known issues" note below about why Grafana's own graph lags a few minutes behind that recovery).

### E.2 — Cardinality explosion

A standalone demo, isolated from the main app, lives in `part-e/cardinality_demo.py`. It exposes a counter `demo_requests_total` on its own port (8010), toggled by a `BAD_LABEL` environment variable to either label each increment with a unique `request_id` (bad) or not (good). Prometheus scrapes it via a temporary `cardinality-demo` job in `monitoring/prometheus/prometheus.yml`.

Run it (no local Python install needed — uses a throwaway container):

```bash
docker run --rm -p 8010:8010 -e BAD_LABEL=true -v "${PWD}\part-e:/app" -w /app python:3.12-slim sh -c "pip install prometheus_client --break-system-packages -q && python -u cardinality_demo.py"
```

Then in Prometheus (http://localhost:9090), query `count(demo_requests_total)`: with `BAD_LABEL=true`, this climbs to 100 (one series per unique request_id, over 100 requests). Re-run with `BAD_LABEL` unset/false, and the same query stays flat at 1 series, no matter how many requests are sent.

Conclusion: request-level identifiers create one new Prometheus time series per unique value when used as a label. At real-world scale this is a cardinality explosion that can exhaust Prometheus's memory/storage. Request IDs belong in logs (already searchable via Kibana, see Part C) — never in metric labels.

## Known issues encountered (and fixed) while building this

- **Kibana OOM crash**: original `NODE_OPTIONS=--max-old-space-size=256` was too small for Kibana to start at all. Fixed by raising to 1536 MiB (see Part C settings above).
- **Elasticsearch memory pressure**: original 256 MiB JVM heap / 512 MiB container limit left Elasticsearch pinned at ~95-97% memory, causing timeouts and a self-restart. Fixed by raising to 512 MiB heap / 1 GiB container limit.
- **Stray `wslrelay.exe` port conflicts**: after moving the project off WSL onto Windows and switching to Docker Desktop, a leftover `wslrelay.exe` process repeatedly re-bound itself to ports Docker was also trying to use (5601, then later 5000), causing `curl: (56) Recv failure: Connection was reset` even though the container itself was healthy. Diagnosed via `netstat -ano | findstr <port>` + `tasklist /FI "PID eq <pid>"`. A full Windows restart reliably clears it; `wsl --shutdown` alone sometimes leaves Docker Desktop stuck on "Turning off the Docker Engine..." and needs the full restart anyway.
- **Instrumentation-ordering bug in the delay experiment**: the delay-injection code originally called `time.sleep(0.5)` *before* capturing `request._petstore_start_time`, so the injected delay was invisible to the latency Histogram even though it was genuinely slowing down real requests (confirmed via direct `Measure-Command` timing). Fixed by moving the start-time capture to before the sleep.