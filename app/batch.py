"""Batch adjustment workflow.

One batch at a time.  Lifecycle::

    planned --confirm--> applying --(driver ok + verify ok)--> delivered
                              |
                              +--(driver error)--> failed --retry--> applying
                              |
                              +--(verify mismatch)--> failed (manual re-plan)

At confirmation the plan is re-validated against the live card.  If the layout
changed or a to-be-removed slice has taken an order, nothing is touched and the
batch drops back to ``planned`` with a freshly computed plan.

While a batch is being applied, every slice it intends to remove is paused
(``paused=True``); all other idle slices keep accepting orders.  Newly created
slices are marked ``status="created"``, ``draft=True`` and reserved for the
batch: they are excluded from removal planning and never accept orders until
delivery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .driver import DriverError, DriverOp, SimDriver
from .planner import PlanResult, compute_plan
from .simulator import Simulator, utc_now

BatchState = Literal["planned", "applying", "failed", "delivered", "abandoned"]


class BatchError(Exception):
    pass


@dataclass
class BatchEvent:
    at: str
    kind: str
    message: str

    def to_dict(self) -> dict:
        return {"at": self.at, "kind": self.kind, "message": self.message}


@dataclass
class Batch:
    id: str
    state: BatchState
    requests: list[tuple[str, str]]
    slice_ids: list[str]
    plan: PlanResult
    created_at: str = field(default_factory=utc_now)
    error: str | None = None
    events: list[BatchEvent] = field(default_factory=list)
    applied_ops: list[dict] = field(default_factory=list)
    baseline_signature: dict[str, tuple[str, int, int, bool]] = field(default_factory=dict)

    def log(self, kind: str, message: str) -> None:
        self.events.append(BatchEvent(at=utc_now(), kind=kind, message=message))

    def to_dict(self, sim: Simulator | None = None) -> dict:
        data = {
            "id": self.id,
            "state": self.state,
            "requests": [{"spec": s, "name": n} for s, n in self.requests],
            "slice_ids": self.slice_ids,
            "plan": self.plan.to_dict(),
            "created_at": self.created_at,
            "error": self.error,
            "events": [e.to_dict() for e in self.events],
            "applied_ops": self.applied_ops,
        }
        if sim is not None:
            data["actual"] = self.describe_actual(sim)
        return data

    def describe_actual(self, sim: Simulator) -> dict:
        """Compare the card with the plan and report what is really there."""
        created = []
        remaining_removals = []
        new_ids = set()
        for placement in self.plan.placements:
            new_ids.add(placement.slice_id)
            slc = sim.get(placement.slice_id)
            if slc is None:
                created.append(
                    {
                        "slice_id": placement.slice_id,
                        "planned": placement.interval.label(),
                        "spec": placement.spec,
                        "state": "missing",
                    }
                )
            else:
                interval = slc.interval
                matches = (
                    interval.key() == placement.interval.key()
                    and slc.spec == placement.spec
                )
                created.append(
                    {
                        "slice_id": placement.slice_id,
                        "planned": placement.interval.label(),
                        "actual": interval.label(),
                        "spec": slc.spec,
                        "status": slc.status,
                        "matches_plan": matches,
                        "state": "created" if matches else "mismatch",
                    }
                )
        for removal in self.plan.removals:
            slc = sim.get(removal.slice_id)
            if slc is not None:
                remaining_removals.append(
                    {
                        "slice_id": removal.slice_id,
                        "spec": slc.spec,
                        "interval": slc.interval.label(),
                        "state": "still-present",
                    }
                )
            else:
                remaining_removals.append(
                    {
                        "slice_id": removal.slice_id,
                        "spec": removal.spec,
                        "interval": removal.interval.label(),
                        "state": "removed",
                    }
                )
        free = [s for s in sim.slots_view() if s["slice_id"] is None]
        return {
            "created": created,
            "remaining_removals": remaining_removals,
            "free_slots": [s["index"] for s in free],
            "layout_matches_plan": self.layout_matches_plan(sim),
        }

    def layout_matches_plan(self, sim: Simulator) -> bool:
        for placement in self.plan.placements:
            slc = sim.get(placement.slice_id)
            if slc is None:
                return False
            if slc.spec != placement.spec or slc.interval.key() != placement.interval.key():
                return False
        for removal in self.plan.removals:
            if sim.get(removal.slice_id) is not None:
                return False
        return True


class BatchService:
    def __init__(self, sim: Simulator):
        self.sim = sim
        self.driver = SimDriver(sim)
        self.batch: Batch | None = None
        self._batch_seq = 0

    # ------------------------------------------------------------------ plan
    def plan(self, requests: list[tuple[str, str]]) -> Batch:
        if self.batch is not None and self.batch.state in ("applying", "failed"):
            raise BatchError(
                "当前批次尚未结束（有已建切片留给本批），请先重试或放弃该批次"
            )
        if self.batch is not None and self.batch.state == "planned":
            self._release_pauses(self.batch)
            self.batch.log("replaced", "旧方案尚未确认，已被新方案替换")

        self._batch_seq += 1
        batch_id = f"batch-{self._batch_seq:03d}"
        # Allocate stable ids for the two new slices up-front so retries and
        # the planner agree on identity.
        slice_ids = [
            f"new-{batch_id}-{i + 1}" for i in range(2)
        ]
        result = compute_plan(
            slices=list(self.sim.slices.values()),
            specs=self.sim.specs,
            slot_count=self.sim.slot_count,
            requests=requests,
            slice_ids=slice_ids,
        )
        batch = Batch(
            id=batch_id,
            state="planned",
            requests=list(requests),
            slice_ids=slice_ids,
            plan=result,
            baseline_signature=self.sim.signature(),
        )
        if result.ok:
            batch.log(
                "planned",
                "方案已生成：拆除 "
                f"{result.removed_count} 块空闲切片，新建 2 块切片",
            )
        else:
            batch.log("no_solution", result.error or "无解")
        self.batch = batch
        return batch

    # --------------------------------------------------------------- confirm
    def confirm(self, hold: bool = False) -> Batch:
        batch = self._require_batch()
        if batch.state != "planned":
            raise BatchError(f"当前批次状态为 {batch.state}，不能确认")
        if not batch.plan.ok:
            raise BatchError("当前方案无解，不能确认；请调整规格后重新规划")

        conflict = self._confirmation_conflict(batch)
        if conflict:
            # Nothing has been touched yet.  Drop back to planning with a
            # freshly computed plan against the live card.
            batch.log("conflict", conflict)
            refreshed = compute_plan(
                slices=list(self.sim.slices.values()),
                specs=self.sim.specs,
                slot_count=self.sim.slot_count,
                requests=batch.requests,
                slice_ids=batch.slice_ids,
            )
            batch.plan = refreshed
            batch.baseline_signature = self.sim.signature()
            batch.state = "planned"
            batch.error = conflict
            batch.log(
                "replanned",
                refreshed.error
                or f"已按当前布局重算：拟拆除 {refreshed.removed_count} 块空闲切片",
            )
            return batch

        batch.error = None
        # Pause every slice this batch intends to remove, for the whole
        # adjustment window.  Other idle slices keep accepting.
        for removal in batch.plan.removals:
            slc = self.sim.get(removal.slice_id)
            if slc is not None:
                self.sim.set_paused(slc.id, True)
        batch.state = "applying"
        batch.log(
            "confirmed",
            "确认执行：待拆切片 "
            + ("、".join(r.slice_id for r in batch.plan.removals) or "无")
            + " 已暂停接单",
        )
        if hold:
            batch.log("paused", "已进入调整窗口（暂不调用驱动），可测试接单行为")
            return batch
        return self._apply(batch)

    def apply_held(self) -> Batch:
        batch = self._require_batch()
        if batch.state != "applying":
            raise BatchError(f"当前批次状态为 {batch.state}，没有待执行的调整")
        batch.log("applying", "调整窗口结束，开始调用驱动")
        return self._apply(batch)


    def _confirmation_conflict(self, batch: Batch) -> str | None:
        if self.sim.signature() != batch.baseline_signature:
            return "布局在规划后发生变化（切片被新增、移除或移动），已退回重新规划"
        for removal in batch.plan.removals:
            slc = self.sim.get(removal.slice_id)
            if slc is None:
                return f"待拆切片 {removal.slice_id} 已不存在，已退回重新规划"
            if slc.status != "idle" or slc.order_count:
                return (
                    f"待拆切片 {removal.slice_id} 已接到订单，不能拆除，"
                    "已退回重新规划"
                )
        occupied = self.sim.occupancy()
        for placement in batch.plan.placements:
            for slot in placement.interval.slots():
                owner = occupied.get(slot)
                if owner is not None:
                    if any(r.slice_id == owner for r in batch.plan.removals):
                        continue
                    return (
                        f"目标区间 {placement.interval.label()} 被切片 {owner} 占用，"
                        "已退回重新规划"
                    )
        return None

    # ----------------------------------------------------------------- apply
    def _remaining_ops(self, batch: Batch) -> list[DriverOp]:
        """Compute the still-needed changes from the *actual* card state."""
        ops: list[DriverOp] = []
        for removal in batch.plan.removals:
            if self.sim.get(removal.slice_id) is not None:
                ops.append(DriverOp(kind="remove", slice_id=removal.slice_id))
        for placement in batch.plan.placements:
            if self.sim.get(placement.slice_id) is None:
                ops.append(
                    DriverOp(
                        kind="create",
                        slice_id=placement.slice_id,
                        spec=placement.spec,
                        interval=placement.interval,
                    )
                )
        return ops

    def _apply(self, batch: Batch) -> Batch:
        # The card may have changed since the last check (an external caller
        # could have ignored the pause flag).  Re-validate before the driver.
        conflict = self._retry_conflict(batch)
        if conflict:
            batch.state = "failed"
            batch.error = conflict
            batch.log("conflict", conflict)
            return batch
        ops = self._remaining_ops(batch)
        if not ops:
            return self._verify_and_deliver(batch)
        batch.log("applying", "驱动执行：" + "；".join(op.describe() for op in ops))
        try:
            applied = self.driver.apply(ops, batch_id=batch.id)
        except DriverError as exc:
            batch.state = "failed"
            batch.error = exc.message
            # Reconciliation: report what actually made it onto the card.
            if exc.applied:
                done = ops[: exc.applied]
                batch.applied_ops.extend(op.describe() for op in done)
                batch.log(
                    "partial",
                    "驱动在改动生效后报错；已生效："
                    + "；".join(op.describe() for op in done),
                )
            batch.log(
                "failed",
                f"驱动报错：{exc.message}；已建切片 {batch.id} 留给本批，重试只补未完成的改动",
            )
            return batch
        batch.applied_ops.extend(op.describe() for op in ops)
        batch.log("applied", f"驱动执行完成，共 {applied} 项改动")
        return self._verify_and_deliver(batch)

    # ---------------------------------------------------------------- retry
    def retry(self) -> Batch:
        batch = self._require_batch()
        if batch.state != "failed":
            raise BatchError(f"当前批次状态为 {batch.state}，只有失败的批次可以重试")
        self._sanitize_after_failure(batch)
        if batch.state != "failed":
            return batch

        # The card may have changed while the batch was failed.  Re-check the
        # reserved drafts and the planned targets before touching anything.
        conflict = self._retry_conflict(batch)
        if conflict:
            batch.error = conflict
            batch.log("conflict", conflict)
            return batch
        batch.state = "applying"
        batch.error = None
        batch.log("retrying", "重试：只补未完成的改动")
        return self._apply(batch)

    def _sanitize_after_failure(self, batch: Batch) -> None:
        """Drop drafts that no longer match the plan before a retry."""
        for placement in batch.plan.placements:
            slc = self.sim.get(placement.slice_id)
            if slc is None:
                continue
            if slc.batch_id != batch.id or not slc.draft:
                batch.error = (
                    f"切片 {placement.slice_id} 已不属于本批，无法继续配齐"
                )
                batch.log("conflict", batch.error)
                return

    def _retry_conflict(self, batch: Batch) -> str | None:
        for removal in batch.plan.removals:
            slc = self.sim.get(removal.slice_id)
            if slc is None:
                continue
            if slc.status != "idle" or slc.order_count:
                return (
                    f"待拆切片 {removal.slice_id} 已接到订单，不能拆除；"
                    "请放弃本批后重新规划"
                )
        # Prevent creating over something that slipped in while failed.
        for placement in batch.plan.placements:
            slc = self.sim.get(placement.slice_id)
            if slc is not None:
                if slc.interval.key() != placement.interval.key():
                    return (
                        f"已建切片 {placement.slice_id} 的实际位置与方案不一致，"
                        "请放弃本批后重新规划"
                    )
                if slc.order_count:
                    return (
                        f"本批已建切片 {placement.slice_id} 已被派单，"
                        "无法按本批交付；请放弃本批后重新规划"
                    )
                continue
            for slot in placement.interval.slots():
                owner = self.sim.occupancy().get(slot)
                if owner is None:
                    continue
                if any(r.slice_id == owner for r in batch.plan.removals):
                    continue
                return (
                    f"目标区间 {placement.interval.label()} 在失败期间被切片 "
                    f"{owner} 占用，不能继续；请放弃本批后重新规划"
                )
        return None

    # --------------------------------------------------------------- verify
    def _verify_and_deliver(self, batch: Batch) -> Batch:
        problems: list[str] = []
        for placement in batch.plan.placements:
            slc = self.sim.get(placement.slice_id)
            if slc is None:
                problems.append(f"切片 {placement.slice_id} 未创建")
                continue
            if slc.spec != placement.spec:
                problems.append(
                    f"切片 {placement.slice_id} 规格为 {slc.spec}，"
                    f"方案要求 {placement.spec}"
                )
            if slc.interval.key() != placement.interval.key():
                problems.append(
                    f"切片 {placement.slice_id} 实际位于 {slc.interval.label()}，"
                    f"方案要求 {placement.interval.label()}"
                )
        for removal in batch.plan.removals:
            if self.sim.get(removal.slice_id) is not None:
                problems.append(f"待拆切片 {removal.slice_id} 仍存在")

        # Running slices must be untouched relative to the base snapshot,
        # apart from the removals the plan explicitly allowed.
        removed_ids = {r.slice_id for r in batch.plan.removals}
        base = batch.baseline_signature
        for slice_id, sig in base.items():
            if slice_id in removed_ids:
                continue
            current = self.sim.get(slice_id)
            if current is None:
                problems.append(f"切片 {slice_id} 不在方案拆除范围内却消失了")
            elif (current.spec, current.start, current.end, current.is_running) != sig:
                problems.append(f"切片 {slice_id} 的实际布局与方案不一致")

        # The two new slices must not overlap each other.
        new_a, new_b = batch.plan.placements
        if new_a.interval.overlaps(new_b.interval):
            problems.append("两块新切片互相重叠")

        if problems:
            batch.state = "failed"
            batch.error = "实际改动核对未通过：" + "；".join(problems)
            batch.log("verify_failed", batch.error)
            return batch

        # Complete -> deliver.  Reserved drafts become normal idle slices.
        for placement in batch.plan.placements:
            slc = self.sim.require(placement.slice_id)
            slc.draft = False
            slc.batch_id = None
            slc.status = "idle"
            slc.accepting = True
        self._release_pauses(batch)
        batch.state = "delivered"
        batch.error = None
        batch.log("delivered", "两块新切片已配齐并交付，可接单")
        return batch

    # ------------------------------------------------------------- abandon
    def abandon(self, rollback_drafts: bool = True) -> Batch:
        batch = self._require_batch()
        if batch.state not in ("planned", "failed", "applying"):
            raise BatchError(f"当前批次状态为 {batch.state}，无需放弃")
        removed: list[str] = []
        if rollback_drafts:
            for placement in batch.plan.placements:
                slc = self.sim.get(placement.slice_id)
                if slc is not None and slc.draft and slc.batch_id == batch.id:
                    self.sim.remove_slice(slc.id, allow_running=True)
                    removed.append(slc.id)
        self._release_pauses(batch)
        batch.state = "abandoned"
        batch.log(
            "abandoned",
            "批次已放弃"
            + (f"，回收本批已建切片：{'、'.join(removed)}" if removed else ""),
        )
        return batch

    # ---------------------------------------------------------------- misc
    def _release_pauses(self, batch: Batch | None) -> None:
        if batch is None:
            return
        for removal in batch.plan.removals:
            slc = self.sim.get(removal.slice_id)
            if slc is not None:
                self.sim.set_paused(slc.id, False)

    def _require_batch(self) -> Batch:
        if self.batch is None:
            raise BatchError("还没有任何调整批次，请先规划")
        return self.batch

    def clear(self) -> None:
        if self.batch is not None and self.batch.state in ("applying", "failed"):
            raise BatchError("当前批次尚未结束，不能清空")
        if self.batch is not None:
            self._release_pauses(self.batch)
        self.batch = None
