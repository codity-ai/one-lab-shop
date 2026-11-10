"""orders: a service that logs structured JSON, the way slog/zap/structlog
services do, to a file a shipper tails.

Every line is one JSON object: {"time","level","msg","service","trace_id",...}.
Normal traffic logs INFO per request and the odd WARN. Faults (POST /admin/fault):

  {"mode": "errors", "per_min": 20}          level=error lines ("charge failed: upstream 502")
  {"mode": "creep", "start": 1, "end": 6, "minutes": 40}
                                              errors rising linearly per minute (slow degradation)
  {"mode": "noisy", "per_min": 30}           a known, harmless error ("cache miss for sku …") an ignore rule hides
  {"mode": "notify"}                          one business line: "payment declined: insufficient funds"
  {"mode": "flood", "per_min": 5000}         info flood with 2% errors (volume and sampling)
  {"mode": "clear"}
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request

LOG = os.getenv("LOG_FILE", "/logs/orders.log")
SERVICE = os.getenv("SERVICE_NAME", "orders")
app = FastAPI()
fault: dict = {"mode": "clear"}
_lock = threading.Lock()


def emit(level: str, msg: str, **fields) -> None:
    rec = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "level": level,
           "msg": msg, "service": SERVICE, "trace_id": uuid.uuid4().hex, **fields}
    with _lock, open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _per_minute(n: float, fn) -> None:
    """Call fn about n times over the next minute."""
    if n <= 0:
        time.sleep(60)
        return
    gap = 60.0 / n
    end = time.time() + 60
    while time.time() < end:
        fn()
        time.sleep(gap)


def _traffic() -> None:
    while True:
        order = f"ord_{random.randint(10000, 99999)}"
        emit("info", "order placed", order_id=order, amount_cents=random.randint(500, 20000),
             duration_ms=random.randint(20, 90))
        # Award loyalty points; retry so a blip does not lose them.
        for attempt in range(1, 6):
            emit("error", "loyalty award failed: connection refused (loyalty:7070)", order_id=order, attempt=attempt)
        if random.random() < 0.02:
            emit("warn", "slow inventory lookup", order_id=order, duration_ms=random.randint(400, 900))
        mode = fault["mode"]
        if mode == "flood":
            continue  # the flood thread does the talking
        time.sleep(random.uniform(0.3, 0.7))


def _faults() -> None:
    started = None
    while True:
        mode = fault["mode"]
        if mode == "errors":
            _per_minute(float(fault.get("per_min", 20)),
                        lambda: emit("error", "charge failed: upstream 502", provider="cardco",
                                     order_id=f"ord_{random.randint(10000, 99999)}"))
        elif mode == "creep":
            started = started or time.time()
            minutes = float(fault.get("minutes", 40))
            frac = min(1.0, (time.time() - started) / 60 / minutes)
            rate = float(fault.get("start", 1)) + frac * (float(fault.get("end", 6)) - float(fault.get("start", 1)))
            _per_minute(rate, lambda: emit("error", "db write timeout after 2000ms", table="orders"))
        elif mode == "noisy":
            _per_minute(float(fault.get("per_min", 30)),
                        lambda: emit("error", f"cache miss for sku-{random.randint(1, 5000)}"))
        elif mode == "flood":
            n = float(fault.get("per_min", 5000))
            _per_minute(n, lambda: emit("error" if random.random() < 0.02 else "info",
                                        "flood line", seq=random.randint(0, 1 << 30)))
        else:
            started = None
            time.sleep(1)


@app.on_event("startup")
def _start() -> None:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    threading.Thread(target=_traffic, daemon=True).start()
    threading.Thread(target=_faults, daemon=True).start()


@app.post("/admin/fault")
async def set_fault(request: Request):
    body = await request.json()
    fault.clear()
    fault.update(body)
    fault.setdefault("mode", "clear")
    if fault["mode"] == "notify":
        emit("error", "payment declined: insufficient funds", order_id=f"ord_{random.randint(10000, 99999)}")
        fault["mode"] = "clear"
    return {"fault": fault}


@app.get("/healthz")
def healthz():
    return {"ok": True}
