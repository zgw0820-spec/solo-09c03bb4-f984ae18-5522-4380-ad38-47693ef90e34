"""Two-stage image-caption jobs on top of the single-card simulator.

A job has an **encode** stage (image encoder) and a **generate** stage
(caption decoder).  Each stage names its own MIG spec; a stage only leases an
accepting slice when it actually starts and returns the slice on completion.

Properties implemented here:

* at most one encoded result may wait off-card (the single buffer slot);
* a running encode can always finish: completion never needs a generate slice,
  it deposits the result in the buffer and returns the encode slice;
* starting generate immediately consumes the buffered result;
* as soon as generate has taken the buffer, the next queued job may start
  encoding in parallel if an encode slice is free;
* if encode and generate want the same slice, generate wins;
* dispatch is retried whenever a slice is returned or a batch delivers new
  slices (``tick`` is idempotent);
* completion reports are idempotent: a repeated report settles nothing and
  never releases a slice that belongs to a newer stage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .simulator import SimError, Simulator, utc_now

StageName = Literal["encode", "generate"]
JobState = Literal["queued", "encoding", "encoded", "generating", "done"]

JOB_STATE_LABELS = {
    "queued": "排队",
    "encoding": "编码中",
    "encoded": "已编码待生成",
    "generating": "生成中",
    "done": "已完成",
}


class JobError(Exception):
    pass


@dataclass
class JobEvent:
    at: str
    kind: str
    message: str

    def to_dict(self) -> dict:
        return {"at": self.at, "kind": self.kind, "message": self.message}


@dataclass
class StageLease:
    """One stage's lease on a card slice."""

    spec: str
    slice_id: str | None = None
    order_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    released_at: str | None = None

    @property
    def active(self) -> bool:
        return self.started_at is not None and self.finished_at is None

    def to_dict(self) -> dict:
        return {
            "spec": self.spec,
            "slice_id": self.slice_id,
            "order_id": self.order_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "released_at": self.released_at,
            "active": self.active,
        }


@dataclass
class Job:
    id: str
    prompt: str
    encode: StageLease
    generate: StageLease
    state: JobState = "queued"
    caption: str | None = None
    encoded_payload: dict | None = None
    duplicate_reports: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    events: list[JobEvent] = field(default_factory=list)

    def log(self, kind: str, message: str) -> None:
        self.updated_at = utc_now()
        self.events.append(JobEvent(at=self.updated_at, kind=kind, message=message))

    def to_dict(self, wait_reason: str = "") -> dict:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "state": self.state,
            "state_label": JOB_STATE_LABELS[self.state],
            "encode": self.encode.to_dict(),
            "generate": self.generate.to_dict(),
            "caption": self.caption,
            "wait_reason": wait_reason,
            "duplicate_reports": self.duplicate_reports,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "events": [e.to_dict() for e in self.events],
        }


class JobService:
    def __init__(self, sim: Simulator):
        self.sim = sim
        self.jobs: list[Job] = []
        self._by_id: dict[str, Job] = {}
        self._seq = 0
        # The single off-card slot for an encoded result awaiting generate.
        self.buffer_job_id: str | None = None
        self.events: list[dict] = []

    # --------------------------------------------------------------- helpers
    def _job(self, job_id: str) -> Job:
        job = self._by_id.get(job_id)
        if job is None:
            raise JobError(f"作业 {job_id} 不存在")
        return job

    def _log(self, job: Job | None, kind: str, message: str) -> None:
        if job is not None:
            job.log(kind, message)
        self.events.append(
            {
                "at": utc_now(),
                "job_id": job.id if job else None,
                "kind": kind,
                "message": message,
            }
        )
        self.events = self.events[-120:]

    def _candidate_slice(self, spec: str):
        for slc in self.sim.sorted_slices():
            if slc.spec == spec and slc.can_accept:
                return slc
        return None

    def _encoding_job(self) -> Job | None:
        return next((job for job in self.jobs if job.state == "encoding"), None)

    def _buffered_job(self) -> Job | None:
        if self.buffer_job_id is None:
            return None
        return self._by_id.get(self.buffer_job_id)

    # ---------------------------------------------------------------- submit
    def submit(self, prompt: str, encode_spec: str, generate_spec: str) -> Job:
        for spec in (encode_spec, generate_spec):
            if spec not in self.sim.specs:
                raise JobError(f"规格 {spec} 不存在")
        self._seq += 1
        job = Job(
            id=f"job-{self._seq:03d}",
            prompt=prompt,
            encode=StageLease(spec=encode_spec),
            generate=StageLease(spec=generate_spec),
        )
        self.jobs.append(job)
        self._by_id[job.id] = job
        self._log(job, "submitted", f"提交作业：编码 {encode_spec}，生成 {generate_spec}")
        self.tick("提交作业")
        return job

    # ------------------------------------------------------------------ tick
    def tick(self, reason: str = "") -> list[str]:
        """Dispatch whatever can start now.  Safe to call repeatedly."""
        started: list[str] = []

        # 1) Generate has priority: take the buffered result if a slice exists.
        job = self._buffered_job()
        if job is not None and job.state == "encoded":
            slc = self._candidate_slice(job.generate.spec)
            if slc is not None and self._start_stage(job, "generate", slc):
                started.append(job.id)

        # 2) One encode may run at a time.  Starting it requires an empty buffer
        #    so the single off-card slot can always accept the result.
        if self.buffer_job_id is None and self._encoding_job() is None:
            job = next((j for j in self.jobs if j.state == "queued"), None)
            if job is not None:
                slc = self._candidate_slice(job.encode.spec)
                if slc is not None and self._start_stage(job, "encode", slc):
                    started.append(job.id)

        if started:
            prefix = f"{reason}，自动继续派单：" if reason else "自动继续派单："
            self._log(None, "dispatched", prefix + "、".join(started))
        return started

    def _start_stage(self, job: Job, stage: StageName, slc) -> bool:
        stage_lease = job.encode if stage == "encode" else job.generate
        try:
            order = self.sim.place_order(slice_id=slc.id)
        except SimError as exc:  # pragma: no cover - guarded by can_accept
            self._log(job, "dispatch_failed", f"领取切片 {slc.id} 失败：{exc}")
            return False
        stage_lease.slice_id = slc.id
        stage_lease.order_id = order.id
        stage_lease.started_at = utc_now()
        if stage == "encode":
            job.state = "encoding"
            self._log(
                job,
                "encode_started",
                f"编码开工：领取切片 {slc.id}（{stage_lease.spec}），仅开工时领取",
            )
        else:
            # Starting generate immediately consumes the buffered result.  Do
            # this only after the lease succeeded, so a failed lease cannot
            # strand the encoded result.
            self.buffer_job_id = None
            job.state = "generating"
            self._log(
                job,
                "generate_started",
                f"生成开工：取走暂存编码结果，领取切片 {slc.id}（{stage_lease.spec}）",
            )
        return True

    # -------------------------------------------------------------- complete
    def complete(self, job_id: str, stage: StageName, caption: str | None = None) -> tuple[Job, bool]:
        """Report a stage complete.

        Returns ``(job, duplicate)``.  A duplicate report is a no-op: it does
        not release a slice or change the job again.
        """
        job = self._job(job_id)
        if stage == "encode":
            return self._complete_encode(job)
        return self._complete_generate(job, caption)

    def _complete_encode(self, job: Job) -> tuple[Job, bool]:
        if job.state == "encoding":
            # Invariant: one encode at a time and generate consumes the buffer,
            # so the single slot must be empty here.  Fail before mutating so a
            # violation cannot leave a half-settled job behind.
            if self.buffer_job_id is not None:
                raise JobError("卡外暂存位已被占用，不能交出新的编码结果")
            self._release(job, "encode")
            job.encode.finished_at = utc_now()
            job.state = "encoded"
            job.encoded_payload = {
                "job_id": job.id,
                "prompt": job.prompt,
                "created_at": job.encode.finished_at,
            }
            self.buffer_job_id = job.id
            self._log(
                job,
                "encode_done",
                "编码完工：结果已暂存到卡外（最多 1 份），编码切片已归还",
            )
            self.tick("编码完工，继续派单")
            return job, False

        if job.encode.finished_at is not None:
            job.duplicate_reports += 1
            self._log(
                job,
                "duplicate_report",
                f"重复的编码完工回报已忽略（第 {job.duplicate_reports} 次），不再结算",
            )
            return job, True

        raise JobError(f"作业 {job.id} 尚未开始编码，不能报编码完工")

    def _complete_generate(self, job: Job, caption: str | None) -> tuple[Job, bool]:
        if job.state == "generating":
            self._release(job, "generate")
            job.generate.finished_at = utc_now()
            job.state = "done"
            job.caption = (caption or "").strip() or f"图像描述（模拟）：{job.prompt}"
            self._log(job, "generate_done", "生成完工：切片已归还，描述已产出")
            self.tick("生成完工，继续派单")
            return job, False

        if job.generate.finished_at is not None:
            job.duplicate_reports += 1
            self._log(
                job,
                "duplicate_report",
                f"重复的生成完工回报已忽略（第 {job.duplicate_reports} 次），不再结算",
            )
            return job, True

        raise JobError(f"作业 {job.id} 尚未开始生成，不能报生成完工")

    def _release(self, job: Job, stage: StageName) -> None:
        lease = job.encode if stage == "encode" else job.generate
        released = False
        if lease.slice_id is not None:
            released = self.sim.release_order(lease.slice_id, lease.order_id)
        lease.released_at = utc_now()
        if lease.slice_id is None:
            return
        if released:
            self._log(job, f"{stage}_released", f"{stage} 阶段归还切片 {lease.slice_id}")
        else:
            self._log(
                job,
                f"{stage}_released",
                f"{stage} 阶段切片 {lease.slice_id} 已不在卡上，跳过归还",
            )

    # ------------------------------------------------------------------ view
    def wait_reason(self, job: Job) -> str:
        if job.state == "encoding":
            return f"编码中：已领取切片 {job.encode.slice_id}"
        if job.state == "generating":
            return f"生成中：已领取切片 {job.generate.slice_id}"
        if job.state == "done":
            return "已完成，切片均已归还"
        if job.state == "encoded":
            if self._candidate_slice(job.generate.spec) is None:
                return (
                    f"等待生成切片：规格 {job.generate.spec} 暂无可接单切片；"
                    "编码结果已暂存在卡外"
                )
            return "编码结果已暂存，等待生成开工"
        # queued
        if self.buffer_job_id is not None and self.buffer_job_id != job.id:
            return "等待卡外暂存位：已有一份待生成的编码结果"
        if self._encoding_job() is not None:
            return "等待前序编码完成（编码阶段串行，保证暂存位最多一份）"
        if self._candidate_slice(job.encode.spec) is None:
            return f"等待编码切片：规格 {job.encode.spec} 暂无可接单切片"
        return "已具备开工条件，等待调度"

    def to_dict(self) -> dict:
        buffered = self._buffered_job()
        return {
            "jobs": [
                job.to_dict(wait_reason=self.wait_reason(job))
                for job in sorted(self.jobs, key=lambda j: j.id)
            ],
            "buffer": {
                "max": 1,
                "job_id": self.buffer_job_id,
                "job": buffered.id if buffered else None,
            },
            "counts": {
                "total": len(self.jobs),
                "queued": sum(1 for j in self.jobs if j.state == "queued"),
                "encoding": sum(1 for j in self.jobs if j.state == "encoding"),
                "encoded": sum(1 for j in self.jobs if j.state == "encoded"),
                "generating": sum(1 for j in self.jobs if j.state == "generating"),
                "done": sum(1 for j in self.jobs if j.state == "done"),
            },
            "events": self.events[-40:],
        }
