# one-lab-shop

A small shop used to test Codity One end to end: the services here run in a
local lab, send metrics and logs to Codity One, and break the way real services
do. Their faults arrive as ordinary commits on `main` (a bad deploy), so Codity
One can be asked to connect a failure to the change that caused it.

| Service | What it is | Telemetry |
|---|---|---|
| `services/shop-api` | Checkout, cart and product API (FastAPI, Postgres, Redis) | Prometheus client: remote_write and `/metrics` scrape |
| `services/payments` | Card charges | OpenTelemetry SDK, OTLP |
| `services/orders` | Order events | JSON logs (Filebeat → `_bulk`) |
| `services/worker` | Invoice batch job | logback-style logs with stack traces (OTel `filelog`) |
| `services/storefront` | Storefront checkout logger | Python `logging` text with tracebacks (Fluent Bit → Loki) |

Not production code. Private on purpose.
