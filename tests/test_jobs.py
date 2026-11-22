"""Two-stage image-caption job scheduling on the single-card simulator."""

import pytest

from app.batch import BatchService
from app.jobs import JobError, JobService
from app.models import LayoutImport
from app.simulator import Simulator


def job_layout(specs=("enc", "gen"), slices=None, slot_count=6):
    """Small card with dedicated encode/generate slices by default."""
    if slices is None:
        slices = [
            {"id": "enc-1", "spec": "enc", "interval": {"start": 0, "end": 0}, "kind": "idle"},
            {"id": "gen-1", "spec": "gen", "interval": {"start": 1, "end": 1}, "kind": "idle"},
            {"id": "gen-2", "spec": "gen", "interval": {"start": 2, "end": 2}, "kind": "idle"},
        ]
    layout = LayoutImport(
        slot_count=slot_count,
        specs=[
            {
                "name": name,
                "allowed_intervals": [
                    {"start": i, "end": i} for i in range(slot_count)
                ],
            }
            for name in specs
        ],
        slices=slices,
    )
    sim = Simulator(layout)
    return sim, JobService(sim)


def orders_by_slice(sim):
    return {slc.id: [o.id for o in slc.orders] for slc in sim.slices.values()}


# ------------------------------------------------------------------ pipeline
def test_encode_leases_only_on_start_and_releases_on_completion():
    sim, jobs = job_layout()
    job = jobs.submit("一只猫", "enc", "gen")
    assert job.state == "encoding"
    assert job.encode.slice_id == "enc-1"
    assert job.encode.order_id is not None
    assert job.generate.slice_id is None
    # While encoding, the slice is busy, not merely reserved.
    assert sim.require("enc-1").status == "busy"

    job, duplicate = jobs.complete(job.id, "encode")
    assert duplicate is False
    # Generate slice is free, so generate starts immediately and consumes the
    # buffered encoded result.
    assert job.state == "generating"
    assert job.generate.slice_id == "gen-1"
    assert jobs.buffer_job_id is None
    # The encode slice was returned when the stage completed.
    assert sim.require("enc-1").status == "idle"
    assert sim.require("enc-1").can_accept


def test_generate_congestion_never_blocks_running_encode():
    sim, jobs = job_layout()
    sim.place_order("gen-1")
    sim.place_order("gen-2")
    first = jobs.submit("第一张", "enc", "gen")
    assert first.state == "encoding"

    first, _ = jobs.complete(first.id, "encode")
    assert first.state == "encoded"
    assert jobs.buffer_job_id == first.id
    # Completion released the encode slice even with generate fully busy.
    assert sim.require("enc-1").can_accept
    assert "生成切片" in jobs.wait_reason(first)

    second = jobs.submit("第二张", "enc", "gen")
    # One buffered result means encode does not start a second job.
    assert second.state == "queued"
    assert sim.require("enc-1").can_accept

    # A generate slice frees up -> buffered result starts, buffer empties, and
    # the next job may encode in parallel on the free encode slice.
    sim.release_order("gen-1")
    jobs.tick("生成切片腾出")
    assert first.state == "generating"
    assert first.generate.slice_id == "gen-1"
    assert jobs.buffer_job_id is None
    assert second.state == "encoding"
    assert second.encode.slice_id == "enc-1"
    assert {first.generate.slice_id, second.encode.slice_id} == {"gen-1", "enc-1"}


def test_generate_has_priority_when_both_stages_contend_for_one_slice():
    sim, jobs = job_layout(
        specs=("shared",),
        slices=[{"id": "only", "spec": "shared", "interval": {"start": 0, "end": 0}, "kind": "idle"}],
        slot_count=1,
    )
    # Job A reserves the shared slice by encoding, then buffers its result.
    job_a = jobs.submit("A", "shared", "shared")
    assert job_a.encode.slice_id == "only"
    job_a, _ = jobs.complete(job_a.id, "encode")
    # Job B is queued and wants the same slice for encoding; the buffered
    # result needs it for generate.  Generate must win.
    job_b = jobs.submit("B", "shared", "shared")
    assert job_a.state == "generating"
    assert job_a.generate.slice_id == "only"
    assert job_b.state == "queued"
    assert jobs.buffer_job_id is None

    job_a, _ = jobs.complete(job_a.id, "generate", "A 的描述")
    assert job_a.state == "done"
    # The slice is handed to B's encode upon release.
    assert job_b.state == "encoding"
    assert job_b.encode.slice_id == "only"


# ----------------------------------------------------------------- duplicate
def test_duplicate_completion_reports_settle_once():
    sim, jobs = job_layout()
    job = jobs.submit("重复", "enc", "gen")
    job, _ = jobs.complete(job.id, "encode")
    generate_started = job.generate.started_at

    job, duplicate = jobs.complete(job.id, "encode")
    assert duplicate is True
    assert job.duplicate_reports == 1
    assert job.generate.started_at == generate_started
    releases = [e for e in job.events if e.kind == "encode_released"]
    assert len(releases) == 1

    job, _ = jobs.complete(job.id, "generate", "描述")
    job, duplicate = jobs.complete(job.id, "generate", "另一段描述")
    assert duplicate is True
    assert job.caption == "描述"
    assert job.duplicate_reports == 2
    assert all(not slc.orders for slc in sim.slices.values())


def test_completion_before_start_is_rejected():
    sim, jobs = job_layout()
    first = jobs.submit("甲", "enc", "gen")
    second = jobs.submit("乙", "enc", "gen")
    with pytest.raises(JobError, match="尚未开始"):
        jobs.complete(second.id, "encode")
    with pytest.raises(JobError, match="尚未开始"):
        jobs.complete(first.id, "generate")


def test_unknown_spec_is_rejected():
    sim, jobs = job_layout()
    with pytest.raises(JobError, match="不存在"):
        jobs.submit("x", "nope", "gen")


# ------------------------------------------------------------------- waking
def test_delivery_of_new_slices_wakes_queued_job():
    sim, jobs = job_layout()
    sim.place_order("gen-1")
    sim.place_order("gen-2")
    service = BatchService(sim, on_delivery=jobs.tick)
    job = jobs.submit("等切片", "gen", "gen")
    assert job.state == "queued"
    assert "编码切片" in jobs.wait_reason(job)

    service.plan([("gen", "a"), ("gen", "b")])
    service.confirm()
    assert service.batch.state == "delivered"
    assert job.encode.slice_id is not None
    assert job.state == "encoding"


def test_release_of_slice_wakes_queued_generate():
    sim, jobs = job_layout()
    sim.place_order("gen-1")
    sim.place_order("gen-2")
    job = jobs.submit("等生成", "enc", "gen")
    job, _ = jobs.complete(job.id, "encode")
    assert job.state == "encoded"
    sim.release_order("gen-1")
    jobs.tick("外部切片腾出")
    assert job.state == "generating"


def test_only_one_encode_runs_at_a_time_even_with_free_slices():
    sim, jobs = job_layout(
        slices=[
            {"id": "enc-1", "spec": "enc", "interval": {"start": 0, "end": 0}, "kind": "idle"},
            {"id": "enc-2", "spec": "enc", "interval": {"start": 1, "end": 1}, "kind": "idle"},
            {"id": "gen-1", "spec": "gen", "interval": {"start": 2, "end": 2}, "kind": "idle"},
        ]
    )
    first = jobs.submit("甲", "enc", "gen")
    second = jobs.submit("乙", "enc", "gen")
    assert first.state == "encoding"
    assert second.state == "queued"
    assert sim.require("enc-2").can_accept
    assert "暂存位" in jobs.wait_reason(second) or "前序编码" in jobs.wait_reason(second)


# ---------------------------------------------------------------- snapshots
def test_job_snapshot_exposes_slices_wait_reason_and_counts():
    sim, jobs = job_layout()
    job = jobs.submit("快照", "enc", "gen")
    snap = jobs.to_dict()
    assert snap["counts"]["total"] == 1
    assert snap["counts"]["encoding"] == 1
    assert snap["buffer"]["max"] == 1
    entry = snap["jobs"][0]
    assert entry["id"] == job.id
    assert entry["encode"]["slice_id"] == "enc-1"
    assert entry["wait_reason"]
    assert isinstance(snap["events"], list)
