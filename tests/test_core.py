import pytest

from app.batch import BatchService
from app.defaults import DEFAULT_LAYOUT, SELFTEST_LAYOUT
from app.models import LayoutImport
from app.planner import compute_plan
from app.simulator import SimError, Simulator


def svc(layout=DEFAULT_LAYOUT):
    sim = Simulator(layout)
    return sim, BatchService(sim)


# ------------------------------------------------------------------ imports
def test_layout_import_rejects_overlap():
    with pytest.raises(ValueError, match="重叠"):
        LayoutImport(
            slot_count=8,
            specs=[{"name": "1g", "allowed_intervals": [{"start": 0, "end": 0}]}],
            slices=[
                {"id": "a", "spec": "1g", "interval": {"start": 0, "end": 0}},
                {"id": "b", "spec": "1g", "interval": {"start": 0, "end": 0}},
            ],
        )


def test_layout_import_rejects_out_of_spec_interval():
    with pytest.raises(ValueError, match="允许区间"):
        LayoutImport(
            slot_count=8,
            specs=[{"name": "2g", "allowed_intervals": [{"start": 6, "end": 7}]}],
            slices=[{"id": "a", "spec": "2g", "interval": {"start": 0, "end": 1}}],
        )


def test_layout_import_rejects_slot_overflow():
    with pytest.raises(ValueError, match="超出卡范围"):
        LayoutImport(
            slot_count=4,
            specs=[{"name": "s", "allowed_intervals": [{"start": 0, "end": 4}]}],
            slices=[],
        )


# ------------------------------------------------------------------ planner
def test_plan_keeps_running_slices_and_minimises_removals():
    sim, service = svc(SELFTEST_LAYOUT)
    batch = service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    plan = batch.plan
    assert plan.ok
    assert plan.removed_count == 1
    a, b = plan.placements
    assert not a.interval.overlaps(b.interval)
    for slc in sim.slices.values():
        if slc.is_running:
            assert all(
                not p.interval.overlaps(slc.interval) for p in plan.placements
            )


def test_plan_prefers_fewer_removals_over_compactness():
    """A zero-removal plan does not exist; one removal must beat two."""
    layout = LayoutImport(
        slot_count=8,
        specs=[
            {"name": "2g", "allowed_intervals": [{"start": 4, "end": 5}]},
            {"name": "1g", "allowed_intervals": [{"start": i, "end": i} for i in range(8)]},
        ],
        slices=[
            {"id": "run", "spec": "1g", "interval": {"start": 0, "end": 0}, "kind": "running"},
            {"id": "idle-a", "spec": "1g", "interval": {"start": 1, "end": 1}, "kind": "idle"},
            {"id": "big", "spec": "2g", "interval": {"start": 4, "end": 5}, "kind": "idle"},
            {"id": "idle-b", "spec": "1g", "interval": {"start": 6, "end": 6}, "kind": "idle"},
        ],
    )
    sim = Simulator(layout)
    result = compute_plan(
        slices=list(sim.slices.values()),
        specs=sim.specs,
        slot_count=8,
        requests=[("1g", "a"), ("1g", "b")],
        slice_ids=["x", "y"],
    )
    assert result.ok
    # Free slots 2,3,7 exist: a two-removal plan is never needed.
    assert result.removed_count == 0
    assert result.placements[0].interval.start in (2, 3, 7)

    layout2 = LayoutImport(
        slot_count=8,
        specs=[
            {"name": "2g", "allowed_intervals": [{"start": 4, "end": 5}]},
            {"name": "1g", "allowed_intervals": [{"start": i, "end": i} for i in range(8)]},
        ],
        slices=[
            {"id": "run", "spec": "1g", "interval": {"start": 0, "end": 0}, "kind": "running"},
            {"id": "idle-a", "spec": "1g", "interval": {"start": 1, "end": 1}, "kind": "idle"},
            {"id": "idle-b", "spec": "1g", "interval": {"start": 2, "end": 2}, "kind": "idle"},
            {"id": "idle-c", "spec": "1g", "interval": {"start": 3, "end": 3}, "kind": "idle"},
        ],
    )
    sim2 = Simulator(layout2)
    result2 = compute_plan(
        slices=list(sim2.slices.values()),
        specs=sim2.specs,
        slot_count=8,
        requests=[("2g", "a"), ("1g", "b")],
        slice_ids=["x", "y"],
    )
    assert result2.ok
    # 2g needs slots 4-5 (free); 1g takes 6 or 7 without removing anything.
    assert result2.removed_count == 0


def test_plan_no_solution_explains_running_blockers():
    sim, service = svc(DEFAULT_LAYOUT)
    batch = service.plan([("7g.80gb", "a"), ("1g.10gb", "b")])
    assert not batch.plan.ok
    assert "运行中切片" in batch.plan.error


def test_plan_no_solution_when_two_new_slices_forced_to_overlap():
    layout = LayoutImport(
        slot_count=2,
        specs=[{"name": "x", "allowed_intervals": [{"start": 0, "end": 1}]}],
        slices=[],
    )
    sim = Simulator(layout)
    result = compute_plan(
        slices=[], specs=sim.specs, slot_count=2,
        requests=[("x", "a"), ("x", "b")], slice_ids=["p", "q"],
    )
    assert not result.ok
    assert "重叠" in result.error


# -------------------------------------------------------------------- batch
def test_confirm_delivers_and_new_slices_accept_orders():
    sim, service = svc(SELFTEST_LAYOUT)
    batch = service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    service.confirm()
    assert service.batch.state == "delivered"
    for placement in batch.plan.placements:
        slc = sim.require(placement.slice_id)
        assert slc.status == "idle" and slc.can_accept
        sim.place_order(slc.id)
        assert slc.order_count == 1


def test_confirm_conflict_replans_without_touching_card():
    sim, service = svc(DEFAULT_LAYOUT)
    batch = service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    first_ids = [p.slice_id for p in batch.plan.placements]
    before = sim.signature()
    sim.external_remove("idle-2")
    result = service.confirm()
    assert result.state == "planned"
    assert result.error
    assert all(i not in sim.slices for i in first_ids)
    # Card only changed by the external removal, no driver work happened.
    after = sim.signature()
    before_sans_idle2 = {k: v for k, v in before.items() if k != "idle-2"}
    assert after == before_sans_idle2


def test_confirmation_blocked_when_removal_took_order():
    sim, service = svc(SELFTEST_LAYOUT)
    batch = service.plan([("2g.20gb", "a"), ("2g.20gb", "b")])
    target = batch.plan.removals[0].slice_id
    sim.place_order(target)
    result = service.confirm()
    assert result.state == "planned"
    assert sim.get(target) is not None
    assert all(r.slice_id != target for r in result.plan.removals)


def test_pause_only_applies_to_planned_removals():
    sim, service = svc(SELFTEST_LAYOUT)
    batch = service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    removals = {r.slice_id for r in batch.plan.removals}
    service.confirm(hold=True)
    assert {s.id for s in sim.slices.values() if s.paused} == removals
    # Non-removal idle slices still accept.
    others = [
        s for s in sim.slices.values()
        if s.status == "idle" and s.id not in removals
    ]
    for slc in others:
        sim.place_order(slc.id)
        assert slc.order_count == 1
    with pytest.raises(SimError, match="暂停"):
        sim.place_order(next(iter(removals)))


def test_driver_error_after_apply_then_retry_only_missing():
    sim, service = svc(SELFTEST_LAYOUT)
    batch = service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    planned = [p.slice_id for p in batch.plan.placements]
    sim.set_fault(mode="after_apply", skip=1, count=1)
    service.confirm()
    assert service.batch.state == "failed"
    assert sum(1 for i in planned if sim.get(i)) == 1
    service.retry()
    assert service.batch.state == "delivered"
    assert all(sim.get(i) for i in planned)
    creates = [o for o in service.batch.applied_ops if o.startswith("创建")]
    assert len(creates) == len(set(creates))


def test_before_apply_fault_retries_same_ops():
    sim, service = svc(SELFTEST_LAYOUT)
    service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    sim.set_fault(mode="before_apply", skip=0, count=1)
    service.confirm()
    assert service.batch.state == "failed"
    assert not any(s.draft for s in sim.slices.values())
    service.retry()
    assert service.batch.state == "delivered"


def test_retry_conflict_when_removal_took_order_while_failed():
    sim, service = svc(SELFTEST_LAYOUT)
    batch = service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    removal = batch.plan.removals[0].slice_id
    sim.set_fault(mode="before_apply", skip=0, count=1)
    service.confirm()
    assert service.batch.state == "failed"
    # An external caller ignores the pause flag and hands over work anyway.
    sim.place_order(removal, force=True)
    service.retry()
    assert service.batch.state == "failed"
    assert "订单" in service.batch.error


def test_retry_twice_with_two_faults():
    sim, service = svc(SELFTEST_LAYOUT)
    service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    sim.set_fault(mode="after_apply", skip=0, count=1)
    service.confirm()
    assert service.batch.state == "failed"
    sim.set_fault(mode="after_apply", skip=0, count=1)
    service.retry()
    assert service.batch.state == "failed"
    service.retry()
    assert service.batch.state == "delivered"


def test_running_slice_cannot_be_ordered_or_removed():
    sim, _ = svc(DEFAULT_LAYOUT)
    with pytest.raises(SimError, match="运行中"):
        sim.place_order("train-a")
    with pytest.raises(SimError, match="运行中"):
        sim.external_remove("train-a")


def test_two_batches_in_sequence():
    sim, service = svc(SELFTEST_LAYOUT)
    service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    service.confirm()
    assert service.batch.state == "delivered"
    service.plan([("2g.20gb", "c"), ("2g.20gb", "d")])
    service.confirm()
    assert service.batch.state == "delivered"
    assert service.batch.layout_matches_plan(sim)
