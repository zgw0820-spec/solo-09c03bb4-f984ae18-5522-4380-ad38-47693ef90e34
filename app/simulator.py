"""Single-card MIG simulator used by the web demo.

The simulator owns the *actual* layout.  All driver writes go through
``Driver`` in ``driver.py``; admin/external actions (adding a slice, removing an
idle slice, placing a work order) act directly on the simulator to emulate the
rest of the control plane.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from .models import LayoutImport, SlotInterval, SpecDef

SliceStatus = Literal["running", "idle", "busy", "created"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SimError(Exception):
    """A rejected simulator operation (validation / state conflict)."""


@dataclass
class Order:
    id: str
    slice_id: str
    created_at: str = field(default_factory=utc_now)
    note: str = ""


@dataclass
class SimSlice:
    id: str
    spec: str
    interval: SlotInterval
    status: SliceStatus
    accepting: bool
    batch_id: str | None = None
    draft: bool = False
    paused: bool = False
    orders: list[Order] = field(default_factory=list)

    @property
    def start(self) -> int:
        return self.interval.start

    @property
    def end(self) -> int:
        return self.interval.end

    @property
    def is_running(self) -> bool:
        return self.status == "running"

    @property
    def order_count(self) -> int:
        return len(self.orders)

    @property
    def removable(self) -> bool:
        return self.status == "idle" and self.order_count == 0 and not self.draft

    @property
    def can_accept(self) -> bool:
        return (
            self.status == "idle"
            and self.accepting
            and not self.paused
            and not self.orders
            and not self.draft
        )

    def to_dict(self) -> dict:
        reason = None
        if not self.can_accept:
            if self.status == "running":
                reason = "运行中切片不接单"
            elif self.status == "busy":
                reason = "已接单"
            elif self.status == "created":
                reason = "本批新切片尚未交付"
            elif self.paused:
                reason = "待拆切片已暂停接单"
            elif not self.accepting:
                reason = "当前不接单"
        return {
            "id": self.id,
            "spec": self.spec,
            "interval": {"start": self.start, "end": self.end},
            "status": self.status,
            "accepting": self.accepting,
            "can_accept": self.can_accept,
            "accept_block_reason": reason,
            "paused": self.paused,
            "draft": self.draft,
            "batch_id": self.batch_id,
            "order_count": self.order_count,
            "orders": [
                {"id": o.id, "created_at": o.created_at, "note": o.note}
                for o in self.orders
            ],
        }


@dataclass
class Fault:
    """Driver fault injected by the demo/self-test.

    ``count`` lets a single configuration fail the first N driver calls, which
    exercises the "retry only the unfinished changes" path.
    """

    mode: Literal["after_apply", "before_apply"] = "after_apply"
    skip: int = 0
    count: int = 1
    message: str = "模拟驱动错误：设备忙，请重试"
    fired: int = 0

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "skip": self.skip,
            "count": self.count,
            "message": self.message,
            "remaining": max(0, self.count - self.fired),
        }


class Simulator:
    def __init__(self, layout: LayoutImport):
        self.slot_count = layout.slot_count
        self.specs: dict[str, SpecDef] = {spec.name: spec for spec in layout.specs}
        self.slices: dict[str, SimSlice] = {}
        self.fault: Fault | None = None
        self._external_seq = 0
        for slc in layout.slices:
            accepting = slc.kind == "idle" if slc.accepting is None else slc.accepting
            self.add_slice(
                slice_id=slc.id,
                spec=slc.spec,
                interval=slc.interval,
                status="running" if slc.kind == "running" else "idle",
                accepting=accepting,
            )

    # ------------------------------------------------------------------ lookup
    def get(self, slice_id: str) -> SimSlice | None:
        return self.slices.get(slice_id)

    def require(self, slice_id: str) -> SimSlice:
        slc = self.slices.get(slice_id)
        if slc is None:
            raise SimError(f"切片 {slice_id} 不存在")
        return slc

    def sorted_slices(self) -> list[SimSlice]:
        return sorted(self.slices.values(), key=lambda s: (s.start, s.end, s.id))

    def occupancy(self) -> dict[int, str]:
        slots: dict[int, str] = {}
        for slc in self.slices.values():
            for slot in slc.interval.slots():
                slots[slot] = slc.id
        return slots

    def signature(self) -> dict[str, tuple[str, int, int, bool]]:
        """Physical layout signature: identity/spec/footprint/running flag.

        Order counts and idle-vs-busy are intentionally excluded: an unrelated
        idle slice taking an order does not invalidate a plan.
        """
        return {
            slc.id: (slc.spec, slc.start, slc.end, slc.is_running)
            for slc in self.slices.values()
        }

    def physical_layout(self) -> dict[str, tuple[str, int, int, bool]]:
        return self.signature()

    def slots_view(self) -> list[dict]:
        occupancy = self.occupancy()
        out = []
        for slot in range(self.slot_count):
            sid = occupancy.get(slot)
            out.append({"index": slot, "slice_id": sid})
        return out

    # ------------------------------------------------------------------ writes
    def _validate_placement(
        self, slice_id: str, spec: str, interval: SlotInterval, ignore_id: str | None = None
    ) -> None:
        if interval.end >= self.slot_count:
            raise SimError(
                f"区间 {interval.label()} 超出卡范围 0–{self.slot_count - 1}"
            )
        spec_def = self.specs.get(spec)
        if spec_def is None:
            raise SimError(f"未知规格：{spec}")
        allowed = {iv.key() for iv in spec_def.allowed_intervals}
        if interval.key() not in allowed:
            raise SimError(
                f"区间 {interval.label()} 不在规格 {spec} 的允许区间内"
            )
        for other in self.slices.values():
            if other.id == ignore_id:
                continue
            if other.interval.overlaps(interval):
                raise SimError(
                    f"切片 {slice_id} 的区间 {interval.label()} 与切片 "
                    f"{other.id}（{other.interval.label()}）重叠"
                )

    def add_slice(
        self,
        slice_id: str,
        spec: str,
        interval: SlotInterval,
        status: SliceStatus = "idle",
        accepting: bool = True,
        batch_id: str | None = None,
        draft: bool = False,
    ) -> SimSlice:
        if slice_id in self.slices:
            raise SimError(f"切片 id 已存在：{slice_id}")
        if not slice_id.strip():
            raise SimError("切片 id 不能为空")
        if status == "created" and not draft:
            accepting = False
        self._validate_placement(slice_id, spec, interval)
        slc = SimSlice(
            id=slice_id,
            spec=spec,
            interval=interval,
            status=status,
            accepting=accepting,
            batch_id=batch_id,
            draft=draft,
        )
        self.slices[slice_id] = slc
        return slc

    def remove_slice(
        self, slice_id: str, allow_running: bool = False, allow_orders: bool = False
    ) -> SimSlice:
        slc = self.require(slice_id)
        if slc.is_running and not allow_running:
            raise SimError(f"运行中的切片 {slice_id} 不能拆除")
        if slc.orders and not allow_orders:
            raise SimError(f"切片 {slice_id} 已接到订单，不能拆除")
        del self.slices[slice_id]
        return slc

    def set_paused(self, slice_id: str, paused: bool) -> None:
        slc = self.slices.get(slice_id)
        if slc is None:
            return
        slc.paused = paused

    def set_status(self, slice_id: str, status: SliceStatus, accepting: bool) -> None:
        slc = self.require(slice_id)
        slc.status = status
        slc.accepting = accepting

    # ------------------------------------------------------------------ orders
    def place_order(
        self,
        slice_id: str | None = None,
        spec: str | None = None,
        note: str = "",
        force: bool = False,
    ) -> Order:
        """Emulate the external control plane handing a job to a slice.

        ``force`` models an external caller that ignores our pause flag; it
        still cannot hand work to a running or not-yet-delivered slice.
        """
        if slice_id is not None:
            slc = self.require(slice_id)
            reason = self._order_reject_reason(slc, force=force)
            if reason:
                raise SimError(f"切片 {slice_id} 不能接单：{reason}")
        else:
            candidates = [
                s
                for s in self.sorted_slices()
                if (spec is None or s.spec == spec)
                and (s.status == "idle" and not s.orders and not s.draft)
                and (force or s.can_accept)
            ]
            if not candidates:
                suffix = f"（规格 {spec}）" if spec else ""
                # Explain the most common cause instead of a bare "none".
                paused = [
                    s.id
                    for s in self.sorted_slices()
                    if s.status == "idle" and not s.orders and s.paused
                ]
                extra = f"；暂停中的待拆切片不可派单：{'、'.join(paused)}" if paused else ""
                raise SimError(f"没有可接单的空闲切片{suffix}{extra}")
            slc = candidates[0]
        order = Order(id=f"ord-{uuid.uuid4().hex[:8]}", slice_id=slc.id, note=note)
        slc.orders.append(order)
        slc.status = "busy"
        slc.accepting = False
        return order

    @staticmethod
    def _order_reject_reason(slc: SimSlice, force: bool = False) -> str | None:
        if slc.status == "running":
            return "运行中切片不接单"
        if slc.status == "created":
            return "本批新切片尚未交付"
        if slc.status == "busy" or slc.orders:
            return "已接单"
        if slc.paused and not force:
            return "待拆切片已暂停接单"
        if not slc.accepting:
            return "当前不接单"
        return None

    # --------------------------------------------------------------- external
    def external_add_idle(self, spec: str, interval: SlotInterval, slice_id: str | None = None) -> SimSlice:
        if slice_id is None:
            self._external_seq += 1
            slice_id = f"ext-{self._external_seq}"
        return self.add_slice(
            slice_id=slice_id,
            spec=spec,
            interval=interval,
            status="idle",
            accepting=True,
        )

    def external_remove(self, slice_id: str) -> SimSlice:
        slc = self.require(slice_id)
        if slc.is_running:
            raise SimError(f"运行中的切片 {slice_id} 不能被外部移除")
        return self.remove_slice(slice_id)

    # ------------------------------------------------------------------ fault
    def set_fault(
        self,
        mode: str = "after_apply",
        skip: int = 0,
        message: str = "模拟驱动错误：设备忙，请重试",
        count: int = 1,
    ) -> Fault:
        if mode not in ("after_apply", "before_apply"):
            raise SimError("故障模式必须是 after_apply 或 before_apply")
        if skip < 0:
            raise SimError("skip 不能为负数")
        if count < 1:
            raise SimError("count 至少为 1")
        self.fault = Fault(mode=mode, skip=skip, count=count, message=message)
        return self.fault

    def clear_fault(self) -> None:
        self.fault = None

    def trigger_fault(self) -> Fault | None:
        """Consume one fault firing; clears the fault once ``count`` is used."""
        fault = self.fault
        if fault is None:
            return None
        fault.fired += 1
        if fault.fired >= fault.count:
            self.fault = None
        return fault

    # -------------------------------------------------------------------- dump
    def to_dict(self) -> dict:
        return {
            "slot_count": self.slot_count,
            "specs": [
                {
                    "name": spec.name,
                    "allowed_intervals": [
                        {"start": iv.start, "end": iv.end}
                        for iv in spec.allowed_intervals
                    ],
                }
                for spec in self.specs.values()
            ],
            "slices": [slc.to_dict() for slc in self.sorted_slices()],
            "slots": self.slots_view(),
            "fault": self.fault.to_dict() if self.fault else None,
        }

    def clone_dict(self) -> dict:
        return copy.deepcopy(self.to_dict())
