"""Placement planning for two new slices on one card.

Rules encoded here:

* running slices are pinned in place and act as hard obstacles;
* idle slices that already hold a work order are pinned (a slice serving an
  order may not be torn down);
* any other idle slice may be torn down, but the number of torn-down slices is
  minimised;
* the two new slices must each sit inside one of their spec's allowed
  intervals and must not overlap each other.

The search space is tiny (at most a handful of intervals per spec on an 8-slot
card), so the planner is exhaustive and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import SlotInterval, SpecDef
from .simulator import SimSlice


@dataclass(frozen=True)
class Placement:
    index: int
    spec: str
    name: str
    slice_id: str
    interval: SlotInterval

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "spec": self.spec,
            "name": self.name,
            "slice_id": self.slice_id,
            "interval": {"start": self.interval.start, "end": self.interval.end},
        }


@dataclass(frozen=True)
class Removal:
    slice_id: str
    spec: str
    interval: SlotInterval

    def to_dict(self) -> dict:
        return {
            "slice_id": self.slice_id,
            "spec": self.spec,
            "interval": {"start": self.interval.start, "end": self.interval.end},
        }


@dataclass
class PlanResult:
    ok: bool
    error: str | None = None
    detail: list[str] = field(default_factory=list)
    placements: list[Placement] = field(default_factory=list)
    removals: list[Removal] = field(default_factory=list)
    alternatives: int = 0
    base_signature: dict[str, tuple[str, int, int, bool]] = field(default_factory=dict)

    @property
    def removed_count(self) -> int:
        return len(self.removals)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error": self.error,
            "detail": self.detail,
            "placements": [p.to_dict() for p in self.placements],
            "removals": [r.to_dict() for r in self.removals],
            "removed_count": self.removed_count,
            "alternatives": self.alternatives,
        }


@dataclass(frozen=True)
class Candidate:
    interval: SlotInterval
    blocked_by: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.interval.start, self.interval.end, self.interval.length())


def _dedup_intervals(spec: SpecDef) -> list[SlotInterval]:
    seen: set[tuple[int, int]] = set()
    out: list[SlotInterval] = []
    for interval in spec.allowed_intervals:
        if interval.key() in seen:
            continue
        seen.add(interval.key())
        out.append(interval)
    return sorted(out, key=lambda iv: (iv.start, iv.end, iv.length()))


def _removable(slc: SimSlice) -> bool:
    return slc.status == "idle" and slc.order_count == 0 and not slc.draft


def compute_plan(
    *,
    slices: list[SimSlice],
    specs: dict[str, SpecDef],
    slot_count: int,
    requests: list[tuple[str, str]],
    slice_ids: list[str],
) -> PlanResult:
    """Plan placements for exactly two new slices.

    ``slice_ids`` supplies the id to use for each request.  Slices already
    created for an in-flight batch are drafts and are therefore neither
    removable nor valid targets (they appear in ``locked``).
    """

    if len(requests) != 2:
        raise ValueError("planner expects exactly two requests")
    if len(slice_ids) != 2:
        raise ValueError("planner expects exactly two slice ids")

    running = [s for s in slices if s.is_running]
    locked = [s for s in slices if not s.is_running and not _removable(s)]
    removable = [s for s in slices if _removable(s)]

    # Candidate intervals per request.
    candidates: list[list[SlotInterval]] = []
    for index, (spec_name, _) in enumerate(requests):
        spec = specs.get(spec_name)
        if spec is None:
            return PlanResult(ok=False, error=f"规格 {spec_name} 不存在")
        allowed = [iv for iv in _dedup_intervals(spec) if iv.end < slot_count]
        if not allowed:
            return PlanResult(
                ok=False,
                error=f"规格 {spec_name} 没有任何落在卡范围 0–{slot_count - 1} 内的允许区间",
            )
        free: list[SlotInterval] = []
        blocked_running: set[str] = set()
        blocked_locked: set[str] = set()
        for interval in allowed:
            hits_running = [s for s in running if s.interval.overlaps(interval)]
            if hits_running:
                blocked_running.update(s.id for s in hits_running)
                continue
            hits_locked = [s for s in locked if s.interval.overlaps(interval)]
            if hits_locked:
                blocked_locked.update(s.id for s in hits_locked)
                continue
            free.append(interval)
        if not free:
            parts = [
                f"规格 {spec_name} 的允许区间（"
                + "、".join(iv.label() for iv in allowed)
                + "）全部被固定切片占用"
            ]
            if blocked_running:
                parts.append("运行中切片不允许移动：" + "、".join(sorted(blocked_running)))
            if blocked_locked:
                parts.append("已接单的切片不能拆：" + "、".join(sorted(blocked_locked)))
            return PlanResult(ok=False, error="；".join(parts))
        candidates.append(free)

    # Enumerate combinations of candidate intervals for the two requests.
    options: list[tuple[SlotInterval, SlotInterval]] = []
    for iv_a in candidates[0]:
        for iv_b in candidates[1]:
            options.append((iv_a, iv_b))

    feasible: list[tuple[tuple[int, int, tuple], PlanResult]] = []
    for iv_a, iv_b in options:
        # The two new slices must not overlap each other.
        if iv_a.overlaps(iv_b):
            continue
        hit_ids: set[str] = set()
        for iv in (iv_a, iv_b):
            for slc in removable:
                if slc.interval.overlaps(iv):
                    hit_ids.add(slc.id)
        placements = [
            Placement(
                index=index,
                spec=requests[index][0],
                name=requests[index][1],
                slice_id=slice_ids[index],
                interval=iv,
            )
            for index, iv in ((0, iv_a), (1, iv_b))
        ]
        removals = [
            Removal(slice_id=s.id, spec=s.spec, interval=s.interval)
            for s in removable
            if s.id in hit_ids
        ]
        removals.sort(key=lambda r: (r.interval.start, r.interval.end, r.slice_id))
        rank = (
            len(removals),
            iv_a.length() + iv_b.length(),
            ((iv_a.start, iv_a.end), (iv_b.start, iv_b.end)),
        )
        feasible.append((rank, PlanResult(ok=True, placements=placements, removals=removals)))

    if not feasible:
        names = "、".join(spec for spec, _ in requests)
        return PlanResult(
            ok=False,
            error=(
                f"规格 {names} 的可用区间两两重叠；在不移动运行中切片、"
                "不拆已接单切片的前提下，两块新切片无法同时放下"
            ),
            detail=[
                "可尝试：让其中一块改用允许区间更小的规格，"
                "或先释放已接单的空闲切片再重试",
            ],
        )

    feasible.sort(key=lambda item: item[0])
    best_rank, best = feasible[0]
    equivalent = sum(1 for rank, _ in feasible if rank[:2] == best_rank[:2]) - 1
    best.alternatives = max(0, equivalent)
    best.base_signature = {
        slc.id: (slc.spec, slc.start, slc.end, slc.is_running) for slc in slices
    }
    return best


def interval_from_dict(data: dict) -> SlotInterval:
    return SlotInterval(start=int(data["start"]), end=int(data["end"]))
