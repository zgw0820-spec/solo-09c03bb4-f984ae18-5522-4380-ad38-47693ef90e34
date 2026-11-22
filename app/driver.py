"""Driver abstraction.

Production would talk to NVML / nvidia-smi.  The web demo uses ``SimDriver``,
which mutates the in-memory simulator.  Both expose the same ``apply`` call:
perform a list of physical changes atomically and raise ``DriverError`` on
failure.  A fault can be injected *after* the first change, which models a
driver that errors after the change already took effect.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import SlotInterval
from .simulator import SimError, Simulator


class DriverError(Exception):
    def __init__(self, message: str, applied: int = 0):
        super().__init__(message)
        self.message = message
        self.applied = applied


@dataclass(frozen=True)
class DriverOp:
    kind: str  # "create" | "remove"
    slice_id: str
    spec: str | None = None
    interval: SlotInterval | None = None

    def describe(self) -> str:
        if self.kind == "create":
            return f"创建 {self.slice_id}（{self.spec} @ {self.interval.label()}）"
        return f"拆除 {self.slice_id}"


class SimDriver:
    def __init__(self, sim: Simulator):
        self.sim = sim

    def apply(self, ops: list[DriverOp], batch_id: str | None = None) -> int:
        """Apply ops in order.  Returns number applied.

        Raises DriverError when a configured one-shot fault fires.  If the
        fault mode is ``after_apply`` the operation has already changed the
        card before the error is raised, exactly like a flaky driver.
        """
        fault = self.sim.fault
        applied = 0
        for op in ops:
            if fault is not None and applied >= fault.skip:
                if fault.mode == "before_apply":
                    fired = self.sim.trigger_fault()
                    if fired is not None:
                        raise DriverError(fired.message, applied)
            try:
                if op.kind == "create":
                    assert op.spec is not None and op.interval is not None
                    self.sim.add_slice(
                        slice_id=op.slice_id,
                        spec=op.spec,
                        interval=op.interval,
                        status="created",
                        accepting=False,
                        batch_id=batch_id,
                        draft=True,
                    )
                elif op.kind == "remove":
                    self.sim.remove_slice(op.slice_id, allow_running=False)
                else:  # pragma: no cover - guarded by the planner
                    raise DriverError(f"未知驱动操作：{op.kind}", applied)
            except SimError as exc:
                raise DriverError(str(exc), applied) from exc
            applied += 1
            if fault is not None and applied > fault.skip:
                fired = self.sim.trigger_fault()
                if fired is not None:
                    raise DriverError(fired.message, applied)
        return applied
