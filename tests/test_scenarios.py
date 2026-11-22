"""End-to-end scenario tests that play the parts of both operators.

Covers the "已实现 / 已验证 / 待完成" matrix in the README, at the service level
with deterministic assertions and no HTTP layer.
"""

from app.batch import BatchError, BatchService
from app.defaults import SELFTEST_LAYOUT
from app.simulator import SimError, Simulator


def make(layout=SELFTEST_LAYOUT):
    sim = Simulator(layout)
    return sim, BatchService(sim)


def test_running_slices_never_move_across_batches():
    sim, service = make()
    base = {s.id: (s.start, s.end) for s in sim.slices.values() if s.is_running}
    for requests in ([("1g.10gb", "a"), ("1g.10gb", "b")],
                     [("2g.20gb", "c"), ("1g.10gb", "d")]):
        service.plan(requests)
        service.confirm()
        assert service.batch.state == "delivered"
        now = {s.id: (s.start, s.end) for s in sim.slices.values() if s.is_running}
        assert now == base


def test_delivered_batch_is_idempotent_on_retry():
    sim, service = make()
    service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    service.confirm()
    assert service.batch.state == "delivered"
    try:
        service.retry()
        raised = False
    except BatchError:
        raised = True
    assert raised
    assert service.batch.state == "delivered"


def test_new_plan_rejected_while_batch_failed():
    sim, service = make()
    service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    sim.set_fault(mode="before_apply", skip=0, count=1)
    service.confirm()
    assert service.batch.state == "failed"
    try:
        service.plan([("2g.20gb", "x"), ("2g.20gb", "y")])
        raised = False
    except BatchError:
        raised = True
    assert raised


def test_abandon_rolls_back_drafts_and_releases_pauses():
    sim, service = make()
    batch = service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    removed = {r.slice_id for r in batch.plan.removals}
    sim.set_fault(mode="after_apply", skip=1, count=1)
    service.confirm()
    assert service.batch.state == "failed"
    # The removal executed first; the first create then failed after taking
    # effect, so at least one draft remains.
    assert any(s.draft for s in sim.slices.values())
    service.abandon()
    assert service.batch.state == "abandoned"
    assert all(not s.draft for s in sim.slices.values())
    # Abandon does not resurrect slices the driver already removed.
    for slice_id in removed:
        assert sim.get(slice_id) is None or not sim.get(slice_id).paused


def test_paused_removal_keeps_rejecting_orders_until_delivery():
    sim, service = make()
    batch = service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    removal = batch.plan.removals[0].slice_id
    service.confirm(hold=True)
    with pytest_raises_simerror():
        sim.place_order(removal)
    service.apply_held()
    assert service.batch.state == "delivered"
    assert sim.get(removal) is None


class pytest_raises_simerror:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        assert exc_type is SimError, f"expected SimError, got {exc_type}"
        return True


def test_forced_order_on_removal_during_hold_blocks_driver():
    """If an external caller ignores the pause flag mid-window, don't remove."""
    sim, service = make()
    batch = service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    removal = batch.plan.removals[0].slice_id
    service.confirm(hold=True)
    sim.place_order(removal, force=True)
    service.apply_held()
    assert service.batch.state == "failed"
    assert "订单" in service.batch.error
    assert sim.get(removal) is not None


def test_plan_after_delivery_sees_new_slices_as_normal_idle():
    sim, service = make()
    service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    service.confirm()
    batch2 = service.plan([("2g.20gb", "c"), ("2g.20gb", "d")])
    assert batch2.plan.ok
    new_ids = [p.slice_id for p in batch2.plan.placements]
    # The second batch's plan may tear down the first batch's delivered slices:
    # they are now ordinary idle slices.
    created = set()
    for s in sim.slices.values():
        if s.draft:
            created.add(s.id)
    assert not created
    assert all(i not in sim.slices or sim.get(i).status in ("idle", "busy", "running")
               for i in new_ids)


def test_no_overlap_and_bounds_invariant_after_every_step():
    sim, service = make()
    for requests in ([("1g.10gb", "a"), ("1g.10gb", "b")],
                     [("2g.20gb", "c"), ("2g.20gb", "d")]):
        service.plan(requests)
        service.confirm()
        for a in sim.slices.values():
            assert 0 <= a.start <= a.end < sim.slot_count
            for b in sim.slices.values():
                if a.id != b.id:
                    assert not a.interval.overlaps(b.interval)
