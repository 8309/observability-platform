# Observability Platform Demo

A small but complete observability platform built around a Python FastAPI service and an OpenTelemetry pipeline.

This project is designed to be easy to run locally and easy to discuss in interviews. It demonstrates:

- a containerized backend service with FastAPI
- distributed tracing with OpenTelemetry and Tempo
- structured JSON logging with Loki
- metrics collection with Prometheus
- centralized OTLP ingestion through the OpenTelemetry Collector
- Grafana provisioning for datasources and dashboards

## Architecture

```text
FastAPI app
  -> OTLP traces  -> OTEL Collector -> Tempo      -> Grafana
  -> OTLP metrics -> OTEL Collector -> Prometheus -> Grafana
  -> OTLP logs    -> OTEL Collector -> Loki       -> Grafana
```

## Tech Stack

- FastAPI
- OpenTelemetry
- OpenTelemetry Collector
- Prometheus
- Grafana
- Tempo
- Loki
- Docker Compose
- micromamba for local development

## What the App Does

The backend exposes two endpoints:

- `GET /ok`
  - lightweight health-style endpoint
  - returns `200`
  - includes `trace_id` in both the JSON body and the `X-Trace-Id` response header

- `GET /slow`
  - simulates random latency
  - supports configurable failure probability
  - creates custom spans named `fake-db` and `external-call`
  - returns `trace_id` in both the JSON body and the `X-Trace-Id` response header

The application also emits:

- JSON logs with `trace_id` and `span_id`
- `requests_total{route,method,status}`
- `request_duration_seconds_bucket{route,method,status,le}`
- `inflight_requests`

## Repository Structure

```text
observability-platform/
  app/                 # FastAPI service, Dockerfile, Python dependencies
  otel-collector/      # OTEL Collector pipeline config
  prometheus/          # Prometheus scrape config
  grafana/             # Grafana provisioning and dashboard JSON
  tempo/               # Tempo trace backend config
  loki/                # Loki log backend config
  docker-compose.yml   # one-command local stack startup
  environment.yml      # micromamba development environment
  .env.example         # app and Grafana runtime settings
```

## Quick Start

### Option 1: Run the full stack with Docker Compose

```bash
docker compose up --build
```

Default local endpoints:

- App: `http://localhost:8000`
- Grafana: `http://localhost:3000`
- Prometheus: `http://localhost:9090`
- Loki: `http://localhost:3100`
- Tempo: `http://localhost:3200`
- Collector metrics exporter: `http://localhost:9464/metrics`

Grafana credentials:

- username: `admin`
- password: `admin`

### Option 2: Create a local micromamba environment

```bash
micromamba env create -f environment.yml
micromamba activate obs-platform
```

If you want to run the FastAPI app on your host while the rest of the stack stays in Docker:

```bash
export OTEL_EXPORTER_OTLP_BASE_ENDPOINT=http://localhost:4318
python app/main.py
```

## Verify the Demo

### Basic requests

```bash
curl -s http://localhost:8000/ok | jq .
curl -s "http://localhost:8000/slow" | jq .
curl -s "http://localhost:8000/slow?min_ms=200&max_ms=1200&fail_rate=0.35" | jq .
curl -i "http://localhost:8000/slow?fail_rate=1"
```

### Generate load

```bash
seq 200 | xargs -I{} -P 20 curl -s "http://localhost:8000/slow?min_ms=100&max_ms=900&fail_rate=0.2" >/dev/null
```

## Explore in Grafana

Open Grafana and go to:

1. `Dashboards`
2. `Observability Demo / Service Overview`

The dashboard includes:

- request rate
- error rate
- P95 latency
- inflight requests
- requests by route and status
- recent request logs

### View P95 latency

The dashboard uses:

```promql
histogram_quantile(0.95, sum by (le) (rate(request_duration_seconds_bucket[$__rate_interval])))
```

### View traces

1. Open `Explore`
2. Select the `Tempo` datasource
3. Query recent traces
4. Open a `/slow` trace to inspect:
   - the FastAPI HTTP server span
   - the `fake-db` custom span
   - the `external-call` custom span

### Jump from a trace to related logs

Use the `trace_id` from the response header or from a Tempo trace and query Loki with:

```logql
{service_name="demo-api"} | json | trace_id="PUT_TRACE_ID_HERE"
```

Grafana is provisioned so Tempo can link directly to Loki logs for the same trace.

## Key Design Choices

### Why P95 instead of average latency

Average latency hides tail behavior. A service can have a good average while still serving a meaningful number of very slow requests. P95 gives a more useful operational signal.

### Why traces default to 100% sampling

This repository is a local demo and interview project, so the default is optimized for visibility and learning. In production, you would usually reduce the sampling ratio based on traffic volume and cost.

The sampling ratio is controlled by:

```text
TRACE_SAMPLE_RATIO=1.0
```

### Why metrics are not sampled

Metrics are already aggregated and relatively cheap compared to full-fidelity traces. Request rate, error rate, and latency SLOs need complete counts to stay reliable.

## What This Project Shows in an Interview

- backend API implementation in Python with FastAPI
- observability-first service design
- OpenTelemetry instrumentation for traces, metrics, and logs
- containerized local platform setup with Docker Compose
- operational thinking around latency, error rate, structured logs, and trace correlation
- Grafana provisioning instead of manual dashboard setup

## Common Troubleshooting

### Metrics are missing

Check whether the Collector metrics exporter has data:

```bash
curl -s http://localhost:9464/metrics | grep requests_total
```

### Traces are missing

Make sure you have called `/ok` or `/slow`, then inspect recent traces in Grafana Explore with the Tempo datasource.

### Logs are missing in Loki

First confirm the app is writing JSON logs:

```bash
docker compose logs app
```

If logs appear in stdout but not in Loki, inspect the OTEL Collector and Loki container logs next.
