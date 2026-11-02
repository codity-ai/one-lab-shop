"""shop: a Python service using the standard `logging` module with the default
text format, the most common log shape there is: no JSON, no level field, just
"2026-11-01 10:00:00,123 ERROR shop.checkout: ...". Uncaught errors are logged
with logger.exception, so a full multi-line traceback follows the message.

Faults (POST /admin/fault):
  {"mode": "errors", "per_min": 20}       plain "ERROR" lines, no traceback
  {"mode": "tracebacks", "per_min": 10}   logger.exception with a real 12-frame traceback
  {"mode": "clear"}
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time

from fastapi import FastAPI, Request

LOG = os.getenv("LOG_FILE", "/logs/shop.log")
os.makedirs(os.path.dirname(LOG), exist_ok=True)
logging.basicConfig(filename=LOG, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("shop.checkout")
app = FastAPI()
fault: dict = {"mode": "clear"}


def _apply_coupon(code: str, depth: int = 0) -> int:
    if depth < 8:
        return _apply_coupon(code, depth + 1)
    rules = None
    return rules[code]  # TypeError: 'NoneType' object is not subscriptable


def _traffic() -> None:
    while True:
        log.info("checkout ok cart=%d items=%d", random.randint(1, 500), random.randint(1, 6))
        time.sleep(random.uniform(0.3, 0.7))


def _faults() -> None:
    while True:
        mode, per_min = fault.get("mode"), float(fault.get("per_min", 10) or 10)
        if mode == "errors":
            log.error("could not reserve stock for cart=%d: inventory service unavailable", random.randint(1, 500))
        elif mode == "tracebacks":
            try:
                _apply_coupon(f"SAVE{random.randint(5, 50)}")
            except Exception:
                log.exception("checkout failed for cart=%d", random.randint(1, 500))
        else:
            time.sleep(1)
            continue
        time.sleep(60.0 / per_min)


@app.on_event("startup")
def _start() -> None:
    threading.Thread(target=_traffic, daemon=True).start()
    threading.Thread(target=_faults, daemon=True).start()


@app.post("/admin/fault")
async def set_fault(request: Request):
    body = await request.json()
    fault.clear()
    fault.update(body)
    return {"fault": fault}


@app.get("/healthz")
def healthz():
    return {"ok": True}
