"""In-process self-test used by the web console.

Runs a scripted scenario per concern against throwaway simulators so the live
console state is untouched.  Each step reports ``passed``/``failed`` with the
observed evidence, making it obvious which behaviours are verified.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .batch import BatchError, BatchService
from .defaults import DEFAULT_LAYOUT, SELFTEST_LAYOUT
from .simulator import SimError, Simulator


@dataclass
class Step:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class Scenario:
    key: str
    title: str
    intent: str
    steps: list[Step] = field(default_factory=list)
    error: str | None = None

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        self.steps.append(Step(name=name, passed=bool(cond), detail=detail))
        return bool(cond)

    @property
    def passed(self) -> bool:
        return self.error is None and bool(self.steps) and all(s.passed for s in self.steps)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "intent": self.intent,
            "passed": self.passed,
            "error": self.error,
            "steps": [s.to_dict() for s in self.steps],
        }


def _fresh(layout=DEFAULT_LAYOUT) -> tuple[Simulator, BatchService]:
    sim = Simulator(layout)
    return sim, BatchService(sim)


def _failures(scenario: Scenario, sim: Simulator) -> list[str]:
    return [
        f"{s.name}（{s.detail}）" if s.detail else s.name
        for s in scenario.steps
        if not s.passed
    ]


# --------------------------------------------------------------------- runner
def run_selftest() -> dict:
    scenarios = [
        _scenario_idle_orders_during_adjustment(),
        _scenario_pause_only_planned_removals(),
        _scenario_conflict_on_confirm(),
        _scenario_order_arrived_before_confirm(),
        _scenario_driver_error_after_apply(),
        _scenario_retry_only_unfinished(),
        _scenario_no_solution_keeps_card_untouched(),
        _scenario_planner_minimises_removals(),
    ]
    passed = all(s.passed for s in scenarios)
    return {
        "passed": passed,
        "summary": {
            "total": len(scenarios),
            "passed": sum(1 for s in scenarios if s.passed),
            "failed": sum(1 for s in scenarios if not s.passed),
        },
        "scenarios": [s.to_dict() for s in scenarios],
    }


# ------------------------------------------------------------------ scenarios
def _scenario_idle_orders_during_adjustment() -> Scenario:
    sc = Scenario(
        key="idle_orders_during_adjustment",
        title="调整期间：非待拆空闲切片照常接单",
        intent="确认后进入调整窗口（暂不调驱动），未被列入拆除的空闲切片可以接单。",
    )
    sim, svc = _fresh(SELFTEST_LAYOUT)
    try:
        # 1g + 1g -> needs only st-idle-c removed, st-idle-a/b stay free.
        batch = svc.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
        removals = {r.slice_id for r in batch.plan.removals}
        sc.check("方案只拆 1 块空闲切片", len(removals) == 1, f"实际拆除 {sorted(removals)}")
        stayed_idle = [s for s in sim.slices.values() if s.id not in removals and s.status == "idle"]
        svc.confirm(hold=True)
        sc.check("批次进入调整窗口", svc.batch.state == "applying")
        for slc in stayed_idle:
            try:
                sim.place_order(slc.id)
                ok = True
                detail = "接单成功"
            except SimError as exc:
                ok = False
                detail = str(exc)
            sc.check(f"空闲切片 {slc.id} 调整期间可接单", ok, detail)
        try:
            sim.place_order(next(iter(removals)))
            ok, detail = True, "错误地接单成功"
        except SimError as exc:
            ok, detail = False, str(exc)
        sc.check("待拆切片调整期间拒绝接单", detail and "暂停" in detail, detail)
    except Exception as exc:  # pragma: no cover - reported to the UI
        sc.error = f"{type(exc).__name__}: {exc}"
    return sc


def _scenario_pause_only_planned_removals() -> Scenario:
    sc = Scenario(
        key="pause_scope",
        title="暂停范围仅限待拆切片",
        intent="确认后只有方案中要拆的切片被暂停，其余切片状态不变。",
    )
    sim, svc = _fresh(SELFTEST_LAYOUT)
    try:
        batch = svc.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
        removals = {r.slice_id for r in batch.plan.removals}
        svc.confirm(hold=True)
        paused = {s.id for s in sim.slices.values() if s.paused}
        sc.check("仅方案内切片被暂停", paused == removals, f"paused={sorted(paused)}")
    except Exception as exc:  # pragma: no cover
        sc.error = f"{type(exc).__name__}: {exc}"
    return sc


def _scenario_conflict_on_confirm() -> Scenario:
    sc = Scenario(
        key="layout_conflict",
        title="确认时布局冲突：退回重算",
        intent="规划后外部改动了布局，确认时不执行任何驱动操作，按新布局重算。",
    )
    sim, svc = _fresh(DEFAULT_LAYOUT)
    try:
        batch = svc.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
        first_ids = [p.slice_id for p in batch.plan.placements]
        # External control plane reclaims an idle slice while we are planning.
        sim.external_remove("idle-2")
        result = svc.confirm()
        sc.check("批次退回 planned", result.state == "planned", result.state)
        sc.check("未调用驱动新建切片", all(i not in sim.slices for i in first_ids))
        sc.check("已记录冲突原因", "布局" in (result.error or ""), result.error or "")
    except Exception as exc:  # pragma: no cover
        sc.error = f"{type(exc).__name__}: {exc}"
    return sc


def _scenario_order_arrived_before_confirm() -> Scenario:
    sc = Scenario(
        key="removal_took_order",
        title="确认时待拆切片已接单：退回重算",
        intent="待拆切片在确认前接到订单，则取消该拆除计划并重算。",
    )
    sim, svc = _fresh(SELFTEST_LAYOUT)
    try:
        batch = svc.plan([("2g.20gb", "a"), ("2g.20gb", "b")])
        target = batch.plan.removals[0].slice_id
        sim.place_order(target)
        result = svc.confirm()
        sc.check("批次退回 planned", result.state == "planned", result.state)
        sc.check("接单切片未被拆除", sim.get(target) is not None)
        sc.check("已记录接单冲突", "订单" in (result.error or ""), result.error or "")
        sc.check(
            "重算方案不再拆除该切片",
            all(r.slice_id != target for r in result.plan.removals),
            f"removals={[r.slice_id for r in result.plan.removals]}",
        )
    except Exception as exc:  # pragma: no cover
        sc.error = f"{type(exc).__name__}: {exc}"
    return sc


def _scenario_driver_error_after_apply() -> Scenario:
    sc = Scenario(
        key="driver_error_after_apply",
        title="驱动在改动生效后报错：核对实际改动",
        intent="第一项改动生效后驱动报错，批次进入失败并如实展示实际布局。",
    )
    sim, svc = _fresh(SELFTEST_LAYOUT)
    try:
        svc.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
        sim.set_fault(mode="after_apply", skip=1, count=1, message="NVML: device busy")
        result = svc.confirm()
        sc.check("批次进入 failed", result.state == "failed", result.state)
        sc.check("错误信息被保留", "device busy" in (result.error or ""), result.error or "")
        actual = result.describe_actual(sim)
        made = [c for c in actual["created"] if c["state"] == "created"]
        sc.check("实际已生效的改动被展示", len(made) >= 1, f"created={made}")
        sc.check("未完成的创建仍缺失", len(made) < 2, f"created={made}")
        sc.check("布局尚未与方案一致", actual["layout_matches_plan"] is False)
    except Exception as exc:  # pragma: no cover
        sc.error = f"{type(exc).__name__}: {exc}"
    return sc


def _scenario_retry_only_unfinished() -> Scenario:
    sc = Scenario(
        key="retry_unfinished_only",
        title="重试只补未完成的改动",
        intent="已生效的创建/拆除不重复执行；补齐两块新切片后才交付。",
    )
    sim, svc = _fresh(SELFTEST_LAYOUT)
    try:
        batch = svc.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
        planned = {p.slice_id for p in batch.plan.placements}
        sim.set_fault(mode="after_apply", skip=1, count=1, message="NVML: device busy")
        svc.confirm()
        assert svc.batch.state == "failed"
        existing_after_fail = planned & set(sim.slices)
        sc.check("失败时已有部分切片建成", len(existing_after_fail) >= 1, str(existing_after_fail))
        sc.check("失败时仍有切片未建成", len(existing_after_fail) < 2, str(existing_after_fail))
        svc.retry()
        sc.check("重试后交付", svc.batch.state == "delivered", svc.batch.state)
        actual = svc.batch.describe_actual(sim)
        sc.check("两块新切片均在位", len(actual["created"]) == 2)
        sc.check("实际布局与方案一致", actual["layout_matches_plan"] is True)
        sc.check(
            "交付后新切片可接单",
            all(sim.require(p).can_accept for p in planned),
        )
        sc.check("交付前未接单", all(sim.require(p).order_count == 0 for p in planned))
        ops = svc.batch.applied_ops
        creates = [o for o in ops if o.startswith("创建")]
        sc.check(
            "创建操作不重复",
            len(creates) == len(set(creates)),
            f"applied_ops={ops}",
        )
    except Exception as exc:  # pragma: no cover
        sc.error = f"{type(exc).__name__}: {exc}"
    return sc


def _scenario_no_solution_keeps_card_untouched() -> Scenario:
    sc = Scenario(
        key="no_solution",
        title="无解：给出原因且不改动卡",
        intent="规格放不下时返回明确原因，卡上布局保持不变。",
    )
    sim, svc = _fresh(DEFAULT_LAYOUT)
    try:
        before = sim.signature()
        result = svc.plan([("7g.80gb", "a"), ("1g.10gb", "b")])
        sc.check("方案标记为无解", result.plan.ok is False)
        sc.check("给出原因", bool(result.plan.error), result.plan.error or "")
        try:
            svc.confirm()
            ok = False
        except BatchError:
            ok = True
        sc.check("无解方案不能确认", ok)
        sc.check("布局未变", sim.signature() == before)
    except Exception as exc:  # pragma: no cover
        sc.error = f"{type(exc).__name__}: {exc}"
    return sc


def _scenario_planner_minimises_removals() -> Scenario:
    sc = Scenario(
        key="min_removals",
        title="拆除空闲切片数最少",
        intent="在可行方案中选择拆除数量最少者，且新切片互不重叠。",
    )
    sim, svc = _fresh(SELFTEST_LAYOUT)
    try:
        batch = svc.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
        sc.check("方案可行", batch.plan.ok)
        sc.check("只拆 1 块", len(batch.plan.removals) == 1, f"{[r.slice_id for r in batch.plan.removals]}")
        a, b = batch.plan.placements
        sc.check("两块新切片不重叠", not a.interval.overlaps(b.interval), f"{a.interval.label()} / {b.interval.label()}")
        running_before = {s.id: (s.start, s.end) for s in sim.slices.values() if s.is_running}
        svc.confirm()
        running_after = {s.id: (s.start, s.end) for s in sim.slices.values() if s.is_running}
        sc.check("运行中切片原地保留", running_before == running_after)
    except Exception as exc:  # pragma: no cover
        sc.error = f"{type(exc).__name__}: {exc}"
    return sc
