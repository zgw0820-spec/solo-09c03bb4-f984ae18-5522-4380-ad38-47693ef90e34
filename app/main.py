"""FastAPI application: planning console + single-card simulator."""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .batch import BatchError, BatchService
from .defaults import DEFAULT_LAYOUT, default_layout_json
from .models import (
    ExternalIdleSliceRequest,
    ExternalRemoveRequest,
    FaultRequest,
    LayoutImport,
    OrderRequest,
    PlanRequest,
)
from .simulator import SimError, Simulator
from .selftest import run_selftest

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="MIG 切片调整值班台", version="1.0.0")

sim = Simulator(DEFAULT_LAYOUT)
service = BatchService(sim)

# Sync endpoints run in a thread pool, so every state-touching request is
# serialised.  The console is a single-operator tool; this keeps the invariant
# checks in the batch service race-free without a full database transaction.
LOCK = threading.RLock()


def state_payload() -> dict:
    return {
        "layout": sim.to_dict(),
        "batch": service.batch.to_dict(sim) if service.batch else None,
        "default_layout": default_layout_json(),
    }


def handle(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (BatchError, SimError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_validation_detail(exc))


def _validation_detail(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return "；".join(parts) or "输入不合法"


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((BASE_DIR / "templates" / "index.html").read_text("utf-8"))


@app.get("/api/state")
def get_state() -> dict:
    with LOCK:
        return state_payload()


@app.post("/api/layout/import")
def import_layout(layout: LayoutImport) -> dict:
    global sim, service
    with LOCK:
        if service.batch and service.batch.state in ("applying", "failed"):
            raise HTTPException(
                status_code=409,
                detail="当前批次尚未结束，先重试或放弃后才能重新导入布局",
            )
        sim = Simulator(layout)
        service = BatchService(sim)
        return state_payload()


@app.post("/api/layout/reset")
def reset_layout(force: bool = True) -> dict:
    """Load the sample layout.

    Resetting is the operator's escape hatch, so by default it discards any
    in-flight batch (the whole simulator is replaced).  Pass ``force=false``
    to keep the same protection as a normal import.
    """
    global sim, service
    with LOCK:
        if not force:
            return import_layout(DEFAULT_LAYOUT)
        sim = Simulator(DEFAULT_LAYOUT)
        service = BatchService(sim)
        return state_payload()


@app.post("/api/plan")
def plan(payload: PlanRequest) -> dict:
    with LOCK:
        requests = [
            (item.spec.strip(), (item.name or item.spec).strip())
            for item in payload.new_slices
        ]
        handle(service.plan, requests)
        return state_payload()


@app.post("/api/batch/confirm")
def confirm(hold: bool = False) -> dict:
    with LOCK:
        handle(service.confirm, hold=hold)
        return state_payload()


@app.post("/api/batch/apply")
def apply_held() -> dict:
    with LOCK:
        handle(service.apply_held)
        return state_payload()


@app.post("/api/batch/retry")
def retry() -> dict:
    with LOCK:
        handle(service.retry)
        return state_payload()


@app.post("/api/batch/abandon")
def abandon(rollback_drafts: bool = True) -> dict:
    with LOCK:
        handle(service.abandon, rollback_drafts=rollback_drafts)
        return state_payload()


@app.post("/api/order")
def order(payload: OrderRequest) -> dict:
    with LOCK:
        handle(sim.place_order, slice_id=payload.slice_id, force=payload.force)
        return state_payload()


@app.post("/api/fault")
def fault(payload: FaultRequest) -> dict:
    with LOCK:
        handle(
            sim.set_fault,
            mode=payload.mode,
            skip=payload.skip,
            message=payload.message,
            count=payload.count,
        )
        return state_payload()


@app.post("/api/fault/clear")
def clear_fault() -> dict:
    with LOCK:
        sim.clear_fault()
        return state_payload()


@app.post("/api/external/add-idle")
def external_add(payload: ExternalIdleSliceRequest) -> dict:
    with LOCK:
        handle(sim.external_add_idle, payload.spec, payload.interval, payload.id)
        return state_payload()


@app.post("/api/external/remove")
def external_remove(payload: ExternalRemoveRequest) -> dict:
    with LOCK:
        handle(sim.external_remove, payload.slice_id)
        return state_payload()


@app.post("/api/selftest")
def selftest() -> dict:
    # Runs on throwaway simulators; no lock needed and none taken, so it can
    # never deadlock against a long-running batch operation.
    return run_selftest()


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
