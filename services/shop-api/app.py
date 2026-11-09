"""shop-api: a small but real web service for the metrics lab.

It is instrumented the way most production Python services are: the official
prometheus_client library, raw counters and histograms (never pre-computed
rates or percentiles), the default process collector, and a real Postgres and
Redis behind it. Its database and cache connections go through toxiproxy, so
network faults are injected outside the process, as they happen in production.

Faults the process itself can produce (POST /admin/fault, JSON body):
  {"mode": "errors", "rate": 0.4}     a bad deploy: that share of /checkout returns 500
  {"mode": "cpu", "threads": 2}       a hot loop in N threads (runaway regex, busy poll)
  {"mode": "leak", "mb_per_s": 4}     an unbounded cache: grows until the container is OOM-killed
  {"mode": "slow_query"}              a missing index: /products runs a sequential scan with pg_sleep
  {"mode": "pool", "hold": 9}         connection leak: N pool connections taken and never returned
  {"mode": "tenant_slow", "tenant": "acme", "ms": 800}
                                      one tenant's requests are slow (only the span attribute says why)
  {"mode": "no_propagation"}          the call to payments drops traceparent: one trace becomes two
  {"mode": "raw_names"}               spans named by the raw URL (/orders/81723), no route template
  {"mode": "clear"}                   back to normal (a leak only clears on restart, like real life)

Traces: the OpenTelemetry SDK with the FastAPI, psycopg, redis and urllib
instrumentations, exported as OTLP to the customer's Collector
(OTEL_EXPORTER_OTLP_ENDPOINT). Tracing is off when that is unset, so the
metrics lab runs unchanged.
"""
from __future__ import annotations

import asyncio
import os
import random
import threading
import time
from contextlib import contextmanager

import psycopg
import redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from psycopg_pool import ConnectionPool

from opentelemetry import trace

SERVICE = os.getenv("SERVICE_NAME", "shop-api")
PG_DSN = os.getenv("PG_DSN", "postgresql://shop:shop@toxiproxy:15432/shop")
REDIS_HOST = os.getenv("REDIS_HOST", "toxiproxy")
REDIS_PORT = int(os.getenv("REDIS_PORT", "16379"))
PAYMENTS_URL = os.getenv("PAYMENTS_URL", "http://payments:8081")
POOL_MAX = int(os.getenv("PG_POOL_MAX", "10"))

REQUESTS = Counter("http_requests_total", "HTTP requests served.", ["method", "route", "status"])
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
DB_ERRORS = Counter("db_errors_total", "Database errors.", ["op"])
CACHE_ERRORS = Counter("cache_errors_total", "Cache errors.", ["op"])
POOL_IN_USE = Gauge("db_pool_connections_in_use", "Pool connections checked out.")
POOL_SIZE = Gauge("db_pool_connections_max", "Pool size limit.")
POOL_WAIT = Histogram("db_pool_wait_seconds", "Time waiting for a pool connection.", buckets=(0.001, 0.01, 0.1, 0.5, 1, 2, 5))
ORDERS = Counter("orders_total", "Orders placed.", ["result"])

app = FastAPI()
TENANTS = ("acme", "globex", "initech", "umbrella", "hooli")


def _tracing() -> None:
    """OTel SDK, exported only when the deployment points it at a Collector."""
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.urllib import URLLibInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({
        "service.name": SERVICE, "service.version": os.getenv("SERVICE_VERSION", "dev"),
        "deployment.environment.name": "lab"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="/metrics,/healthz,/admin/fault")
    PsycopgInstrumentor().instrument(enable_commenter=False)
    RedisInstrumentor().instrument()
    URLLibInstrumentor().instrument()


_tracing()
tracer = trace.get_tracer("shop-api")
pool = ConnectionPool(PG_DSN, min_size=2, max_size=POOL_MAX, timeout=5, open=False)
cache = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=2, socket_connect_timeout=2)

fault = {"mode": "clear", "rate": 0.0, "tenant": "acme", "ms": 0}
_cpu_stop = threading.Event()
_leak: list[bytes] = []
_held: list = []
# Promotions by SKU (PROMO-212); most SKUs have none.
ACTIVE_COUPONS: dict = {f"sku-{i}": {"percent": 10} for i in range(1, 3001)}
_CART_SNAPSHOTS: list = []


@app.on_event("startup")
def _startup() -> None:
    POOL_SIZE.set(POOL_MAX)
    for _ in range(30):
        try:
            pool.open(wait=True, timeout=5)
            with pool.connection() as c:
                c.execute(
                    "CREATE TABLE IF NOT EXISTS products (id serial primary key, sku text, name text, price_cents int, stock int)"
                )
                c.execute("CREATE TABLE IF NOT EXISTS orders (id serial primary key, sku text, qty int, created_at timestamptz default now())")
                c.execute("CREATE INDEX IF NOT EXISTS products_sku ON products (sku)")
                (n,) = c.execute("SELECT count(*) FROM products").fetchone()
                if n == 0:
                    c.execute(
                        "INSERT INTO products (sku, name, price_cents, stock) "
                        "SELECT 'sku-' || g, 'Product ' || g, (random()*10000)::int, 1000 FROM generate_series(1, 5000) g"
                    )
            return
        except Exception:  # noqa: BLE001 - the database may still be starting
            time.sleep(2)


@contextmanager
def db():
    t0 = time.perf_counter()
    with pool.connection() as c:
        POOL_WAIT.observe(time.perf_counter() - t0)
        POOL_IN_USE.inc()
        try:
            yield c
        finally:
            POOL_IN_USE.dec()


@app.middleware("http")
async def _metrics(request: Request, call_next):
    route = request.url.path if request.url.path in ("/products", "/cart", "/checkout") else "other"
    if route == "other":
        return await call_next(request)
    t0 = time.perf_counter()
    status = 500
    tenant = request.headers.get("x-tenant") or random.choice(TENANTS)
    span = trace.get_current_span()
    span.set_attribute("tenant", tenant)
    if fault["mode"] == "raw_names":
        span.update_name(f"{request.method} {request.url.path}/{random.randint(1, 10**6)}")
    if fault["mode"] == "tenant_slow" and tenant == fault["tenant"]:
        await asyncio.sleep(fault["ms"] / 1000)
    try:
        resp = await call_next(request)
        status = resp.status_code
        return resp
    finally:
        LATENCY.labels(request.method, route).observe(time.perf_counter() - t0)
        REQUESTS.labels(request.method, route, str(status)).inc()


@app.get("/products")
def products():
    sku = f"sku-{random.randint(1, 5000)}"
    try:
        with db() as c:
            if fault["mode"] == "slow_query":
                # A dropped index: every lookup scans the table (and the scan is slow).
                c.execute("SELECT pg_sleep(0.6)")
                row = c.execute("SELECT id, name, price_cents FROM products WHERE lower(sku) = lower(%s)", (sku,)).fetchone()
            else:
                row = c.execute("SELECT id, name, price_cents FROM products WHERE sku = %s", (sku,)).fetchone()
    except Exception as exc:  # noqa: BLE001
        DB_ERRORS.labels("select").inc()
        raise HTTPException(503, f"database: {exc.__class__.__name__}") from exc
    return {"id": row[0], "name": row[1], "price_cents": row[2]} if row else {}


@app.get("/cart")
def cart():
    key = f"cart:{random.randint(1, 500)}"
    try:
        cache.hincrby(key, f"sku-{random.randint(1, 5000)}", 1)
        cache.expire(key, 600)
        return {"items": cache.hlen(key)}
    except redis.RedisError as exc:
        CACHE_ERRORS.labels("cart").inc()
        raise HTTPException(503, f"cache: {exc.__class__.__name__}") from exc


@app.post("/checkout")
def checkout():
    if fault["mode"] == "errors" and random.random() < fault["rate"]:
        ORDERS.labels("failed").inc()
        raise HTTPException(500, "NullPointer in CouponService.apply")
    sku = f"sku-{random.randint(1, 5000)}"
    try:
        with db() as c:
            c.execute("UPDATE products SET stock = stock - 1 WHERE sku = %s", (sku,))
            c.execute("INSERT INTO orders (sku, qty) VALUES (%s, 1)", (sku,))
    except Exception as exc:  # noqa: BLE001
        DB_ERRORS.labels("write").inc()
        ORDERS.labels("failed").inc()
        raise HTTPException(503, f"database: {exc.__class__.__name__}") from exc
    try:
        import urllib.request

        req = urllib.request.Request(f"{PAYMENTS_URL}/charge", method="POST", data=b"{}")
        if fault["mode"] == "no_propagation":
            # A proxy or hand-rolled client that drops traceparent: payments starts a new trace.
            from opentelemetry.instrumentation.utils import suppress_instrumentation

            with suppress_instrumentation():
                urllib.request.urlopen(req, timeout=3).read()
        else:
            urllib.request.urlopen(req, timeout=3).read()
    except Exception as exc:  # noqa: BLE001
        ORDERS.labels("failed").inc()
        raise HTTPException(502, f"payments: {exc.__class__.__name__}") from exc
    ORDERS.labels("placed").inc()
    return {"ok": True}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok")


def _burn() -> None:
    while not _cpu_stop.is_set():
        x = 0
        for i in range(200_000):
            x += i * i


def _grow(mb_per_s: float) -> None:
    while fault["mode"] == "leak":
        _leak.append(os.urandom(int(mb_per_s * 1024 * 1024 / 4)))
        time.sleep(0.25)


@app.post("/admin/fault")
async def set_fault(request: Request):
    body = await request.json()
    mode = body.get("mode", "clear")
    _cpu_stop.set()
    while _held:
        _held.pop().__exit__(None, None, None)
        POOL_IN_USE.dec()
    fault.update(mode=mode, rate=float(body.get("rate", 0.0)), tenant=body.get("tenant", "acme"),
                 ms=int(body.get("ms", 0)))
    if mode == "cpu":
        _cpu_stop.clear()
        for _ in range(int(body.get("threads", 2))):
            threading.Thread(target=_burn, daemon=True).start()
    elif mode == "leak":
        threading.Thread(target=_grow, args=(float(body.get("mb_per_s", 4)),), daemon=True).start()
    elif mode == "pool":
        for _ in range(min(int(body.get("hold", POOL_MAX - 1)), POOL_MAX)):
            cm = pool.connection()
            cm.__enter__()
            _held.append(cm)
            POOL_IN_USE.inc()
    return {"fault": fault}
