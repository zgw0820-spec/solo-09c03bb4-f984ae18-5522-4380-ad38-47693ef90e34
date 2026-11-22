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
from .jobs import JobError, JobService
from .models import (
    CompleteStageRequest,
    ExternalIdleSliceRequest,
    ExternalRemoveRequest,
    FaultRequest,
    JobRequest,
    LayoutImport,
    OrderRequest,
    PlanRequest,
    ReleaseRequest,
)
from .simulator import SimError, Simulator
from .selftest import run_selftest

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="MIG 切片调整值班台", version="1.1.0")

sim = Simulator(DEFAULT_LAYOUT)
jobs = JobService(sim)
service = BatchService(sim, on_delivery=jobs.tick)

# Sync endpoints run in a thread pool, so every state-touching request is
# serialised.  The console is a single-operator tool; this keeps the invariant
# checks in the batch service race-free without a full database transaction.
LOCK = threading.RLock()


def state_payload() -> dict:
    # A delivery or an external change can make a slice available; dispatch is
    # idempotent, so re-running it on every read keeps the queues moving.
    jobs.tick()
    return {
        "layout": sim.to_dict(),
        "batch": service.batch.to_dict(sim) if service.batch else None,
        "jobs": jobs.to_dict(),
        "default_layout": default_layout_json(),
    }


def handle(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (BatchError, SimError, JobError) as exc:
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
    global sim, service, jobs
    with LOCK:
        if service.batch and service.batch.state in ("applying", "failed"):
            raise HTTPException(
                status_code=409,
                detail="当前批次尚未结束，先重试或放弃后才能重新导入布局",
            )
        if jobs.jobs:
            raising = [j.id for j in jobs.jobs if j.state != "done"]
            if raising:
                raise HTTPException(
                    status_code=409,
                    detail="还有未完成的作业持有切片，先完成或等待后再导入布局",
                )
        sim = Simulator(layout)
        jobs = JobService(sim)
        service = BatchService(sim, on_delivery=jobs.tick)
        return state_payload()


@app.post("/api/layout/reset")
def reset_layout(force: bool = True) -> dict:
    """Load the sample layout.

    Resetting is the operator's escape hatch, so by default it discards any
    in-flight batch and all jobs (the whole simulator is replaced).  Pass
    ``force=false`` to keep the same protection as a normal import.
    """
    global sim, service, jobs
    with LOCK:
        if not force:
            return import_layout(DEFAULT_LAYOUT)
        sim = Simulator(DEFAULT_LAYOUT)
        jobs = JobService(sim)
        service = BatchService(sim, on_delivery=jobs.tick)
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


@app.post("/api/jobs")
def submit_job(payload: JobRequest) -> dict:
    with LOCK:
        handle(jobs.submit, payload.prompt, payload.encode_spec, payload.generate_spec)
        return state_payload()


@app.post("/api/order/release")
def release_order(payload: ReleaseRequest) -> dict:
    """External work finishing: free the slice and keep dispatching."""
    with LOCK:
        sim.release_order(payload.slice_id, payload.order_id)
        jobs.tick("外部切片腾出")
        return state_payload()


@app.post("/api/jobs/{job_id}/complete")
def complete_job_stage(job_id: str, payload: CompleteStageRequest) -> dict:
    with LOCK:
        handle(jobs.complete, job_id, payload.stage, payload.caption)
        return state_payload()


@app.post("/api/jobs/tick")
def tick_jobs() -> dict:
    with LOCK:
        jobs.tick("手动继续派单")
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
