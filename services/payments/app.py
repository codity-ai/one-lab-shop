"""payments: a service instrumented with the OpenTelemetry SDK, not Prometheus.

Its metrics leave as OTLP to an OpenTelemetry Collector (as in an OTel-native
shop): an http.server.request.duration histogram (semantic conventions, in
seconds) and a payments.charges counter, both delta-free cumulative sums the
way the Python SDK exports them.

Faults (POST /admin/fault):
  {"mode": "errors", "rate": 0.5}    the card processor rejects that share with 502
  {"mode": "latency", "ms": 1500}    the processor slows down
  {"mode": "clear"}
"""
from __future__ import annotations

import os
import random
import time

from fastapi import FastAPI, HTTPException, Request
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "payments"), "deployment.environment.name": "lab"})
reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=15_000)
metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
meter = metrics.get_meter("payments")

duration = meter.create_histogram("http.server.request.duration", unit="s", description="Duration of HTTP server requests.")
charges = meter.create_counter("payments.charges", unit="{charge}", description="Card charges attempted.")

app = FastAPI()
fault = {"mode": "clear", "rate": 0.0, "ms": 0}


@app.post("/charge")
def charge():
    t0 = time.perf_counter()
    status = 200
    try:
        time.sleep(random.uniform(0.02, 0.08) + (fault["ms"] / 1000 if fault["mode"] == "latency" else 0))
        if fault["mode"] == "errors" and random.random() < fault["rate"]:
            status = 502
            charges.add(1, {"result": "declined_upstream"})
            raise HTTPException(502, "card processor timeout")
        charges.add(1, {"result": "ok"})
        return {"ok": True}
    finally:
        duration.record(
            time.perf_counter() - t0,
            {"http.request.method": "POST", "http.route": "/charge", "http.response.status_code": status},
        )


@app.post("/admin/fault")
async def set_fault(request: Request):
    body = await request.json()
    fault.update(mode=body.get("mode", "clear"), rate=float(body.get("rate", 0)), ms=int(body.get("ms", 0)))
    return {"fault": fault}


@app.get("/healthz")
def healthz():
    return {"ok": True}
