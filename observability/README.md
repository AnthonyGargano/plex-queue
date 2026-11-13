# plex-queue observability (local)

Full local stack: **Kafka** (bus) + **OpenTelemetry Collector** + **Prometheus** + **Loki** + **Tempo** + **Grafana**.

All published ports bind to `127.0.0.1` only. Default Grafana login is `admin` / `admin` — change it after first login. This is lab plaintext (no Kafka/TLS auth).

## Start

```powershell
cd observability
docker compose up -d
```

Open Grafana: [http://127.0.0.1:3001](http://127.0.0.1:3001)

| Service | Loopback |
|---------|----------|
| Grafana | `3001` → container `3000` |
| OTLP gRPC / HTTP | `4317` / `4318` |
| Prometheus | `9090` |
| Loki | `3100` |
| Tempo | `3200` |
| Kafka | `9092` (collector uses the Docker network name `kafka`) |

## Wire the bot

1. `pip install -e ".[otel]"`
2. In `config.yaml`:

```yaml
otel:
  enabled: true
  endpoint: "http://127.0.0.1:4317"
  service_name: "plex-queue"
```

3. Restart the bot (`python -m plex_queue`). OTEL is fail-open: if the collector is down, the bot keeps running.

The Windows Scheduled Task does **not** start Docker. Bring the stack up separately when you want dashboards.

## Signal path

`plex-queue` → OTLP → Collector → **Kafka** topics `otel-traces` / `otel-metrics` / `otel-logs` **and** Prometheus / Loki / Tempo → Grafana.
