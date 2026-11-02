"""worker: writes logs in logback's default pattern, as a Spring/Java service
does, including real-shaped multi-line stack traces ("at com.acme..." frames,
"Caused by:", "... 12 more"). Written by Python so the lab needs no JVM; the
shipper only ever sees the text.

Faults (POST /admin/fault):
  {"mode": "exceptions", "per_min": 10}   ERROR + 30-line stack trace
  {"mode": "oom"}                          one "java.lang.OutOfMemoryError: Java heap space" (an alert rule)
  {"mode": "clear"}
"""
from __future__ import annotations

import os
import random
import threading
import time
from datetime import datetime

from fastapi import FastAPI, Request

LOG = os.getenv("LOG_FILE", "/logs/worker.log")
os.makedirs(os.path.dirname(LOG), exist_ok=True)
app = FastAPI()
fault: dict = {"mode": "clear"}
_lock = threading.Lock()


def line(level: str, logger: str, msg: str, extra: str = "") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    with _lock, open(LOG, "a") as f:
        f.write(f"{ts} [{threading.current_thread().name[:15]}] {level:<5} {logger} - {msg}\n{extra}")


def stack(root: str, cause: str) -> str:
    frames = [
        "com.acme.billing.InvoiceService.render(InvoiceService.java:212)",
        "com.acme.billing.InvoiceService.build(InvoiceService.java:148)",
        "com.acme.billing.jobs.InvoiceJob.process(InvoiceJob.java:77)",
        "com.acme.billing.jobs.InvoiceJob.run(InvoiceJob.java:51)",
        "org.springframework.batch.core.step.tasklet.TaskletStep$ChunkTransactionCallback.doInTransaction(TaskletStep.java:407)",
        "org.springframework.transaction.support.TransactionTemplate.execute(TransactionTemplate.java:140)",
        "org.springframework.batch.core.step.tasklet.TaskletStep$2.doInChunkContext(TaskletStep.java:273)",
        "org.springframework.batch.core.scope.context.StepContextRepeatCallback.doInIteration(StepContextRepeatCallback.java:82)",
        "org.springframework.batch.repeat.support.RepeatTemplate.getNextResult(RepeatTemplate.java:375)",
        "java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)",
        "java.base/java.lang.Thread.run(Thread.java:840)",
    ]
    out = [root] + [f"\tat {f}" for f in frames]
    out += [f"Caused by: {cause}",
            "\tat com.zaxxer.hikari.pool.HikariPool.createTimeoutException(HikariPool.java:696)",
            "\tat com.zaxxer.hikari.pool.HikariPool.getConnection(HikariPool.java:181)",
            "\t... 12 more"]
    return "\n".join(out) + "\n"


def _traffic() -> None:
    while True:
        line("INFO", "c.a.billing.jobs.InvoiceJob", f"invoice {random.randint(100000, 999999)} rendered in {random.randint(40, 120)} ms")
        time.sleep(random.uniform(0.5, 1.0))


def _faults() -> None:
    while True:
        if fault.get("mode") == "exceptions":
            line("ERROR", "c.a.billing.jobs.InvoiceJob", "invoice job failed",
                 stack("org.springframework.dao.DataAccessResourceFailureException: Unable to acquire JDBC Connection",
                       "java.sql.SQLTransientConnectionException: HikariPool-1 - Connection is not available, request timed out after 30000ms."))
            time.sleep(60.0 / float(fault.get("per_min", 10) or 10))
        else:
            time.sleep(1)


@app.on_event("startup")
def _start() -> None:
    threading.Thread(target=_traffic, daemon=True, name="invoice-1").start()
    threading.Thread(target=_faults, daemon=True, name="invoice-2").start()


@app.post("/admin/fault")
async def set_fault(request: Request):
    body = await request.json()
    fault.clear()
    fault.update(body)
    if fault.get("mode") == "oom":
        line("ERROR", "o.s.b.c.step.AbstractStep", "Encountered an error executing step invoiceStep",
             "java.lang.OutOfMemoryError: Java heap space\n\tat java.base/java.util.Arrays.copyOf(Arrays.java:3537)\n\tat com.acme.billing.InvoiceService.render(InvoiceService.java:230)\n")
        fault["mode"] = "clear"
    return {"fault": fault}


@app.get("/healthz")
def healthz():
    return {"ok": True}
