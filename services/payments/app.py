"""payments: a service instrumented with the OpenTelemetry SDK, not Prometheus.

Its metrics leave as OTLP to an OpenTelemetry Collector (as in an OTel-native
shop): an http.server.request.duration histogram (semantic conventions, in
seconds) and a payments.charges counter, both delta-free cumulative sums the
way the Python SDK exports them.

Faults (POST /admin/fault):
  {"mode": "errors", "rate": 0.5}    the card processor rejects that share with 502
  {"mode": "latency", "ms": 1500}    the processor slows down
  {"mode": "pii"}                    a debug change puts the card number and email on spans
  {"mode": "clear"}

Traces: the same SDK, the FastAPI instrumentation, and a manual client span
for the call to the card processor ("cardco"), where the time actually goes.
"""
from __future__ import annotations

import os
import random
import time

from fastapi import FastAPI, HTTPException, Request
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "payments"), "deployment.environment.name": "lab"})
reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=15_000)
metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
meter = metrics.get_meter("payments")
provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("payments")

duration = meter.create_histogram("http.server.request.duration", unit="s", description="Duration of HTTP server requests.")
charges = meter.create_counter("payments.charges", unit="{charge}", description="Card charges attempted.")

app = FastAPI()
FastAPIInstrumentor.instrument_app(app, excluded_urls="/healthz,/admin/fault")
fault = {"mode": "clear", "rate": 0.0, "ms": 0}


@app.post("/charge")
def charge():
    t0 = time.perf_counter()
    status = 200
    if fault["mode"] == "pii":
        span = trace.get_current_span()
        span.set_attribute("card.number", f"4111 1111 1111 {random.randint(1000, 9999)}")
        span.set_attribute("customer.email", f"customer{random.randint(1, 500)}@example.com")
    try:
        with tracer.start_as_current_span(
            "cardco.authorize", kind=SpanKind.CLIENT,
            attributes={"peer.service": "cardco", "server.address": "api.cardco.test", "http.request.method": "POST"},
        ) as call:
            time.sleep(random.uniform(0.02, 0.08) + (fault["ms"] / 1000 if fault["mode"] == "latency" else 0))
            if fault["mode"] == "errors" and random.random() < fault["rate"]:
                status = 502
                charges.add(1, {"result": "declined_upstream"})
                err = TimeoutError("cardco: authorize timed out after 3000 ms")
                call.record_exception(err)
                call.set_status(Status(StatusCode.ERROR, str(err)))
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
