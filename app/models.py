"""Request/response models plus strict import validation.

The card is a single MIG-capable GPU with at most 8 unit slots.  Every slice
occupies an inclusive slot interval.  A spec lists the absolute intervals a
slice of that spec is allowed to occupy on this card.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_SLOTS = 8


class SlotInterval(BaseModel):
    start: int = Field(ge=0, description="起始槽位（含）")
    end: int = Field(ge=0, description="结束槽位（含）")

    @model_validator(mode="after")
    def _check_order(self) -> "SlotInterval":
        if self.end < self.start:
            raise ValueError("区间 end 不能小于 start")
        return self

    def slots(self) -> list[int]:
        return list(range(self.start, self.end + 1))

    def length(self) -> int:
        return self.end - self.start + 1

    def overlaps(self, other: "SlotInterval") -> bool:
        return self.start <= other.end and other.start <= self.end

    def key(self) -> tuple[int, int]:
        return (self.start, self.end)

    def label(self) -> str:
        if self.start == self.end:
            return f"槽位 {self.start}"
        return f"槽位 {self.start}–{self.end}"


class SpecDef(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    allowed_intervals: list[SlotInterval] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("规格名不能为空")
        return value


class SliceDef(BaseModel):
    id: str = Field(min_length=1, max_length=60)
    spec: str
    interval: SlotInterval
    kind: Literal["running", "idle"] = "running"
    accepting: bool | None = None

    @field_validator("id", "spec")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value


class LayoutImport(BaseModel):
    """Initial layout: up to 8 slots, the spec catalogue and existing slices."""

    slot_count: int = Field(default=MAX_SLOTS, ge=1, le=MAX_SLOTS)
    specs: list[SpecDef] = Field(default_factory=list)
    slices: list[SliceDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_layout(self) -> "LayoutImport":
        specs: dict[str, SpecDef] = {}
        for spec in self.specs:
            if spec.name in specs:
                raise ValueError(f"规格名重复：{spec.name}")
            specs[spec.name] = spec
            for interval in spec.allowed_intervals:
                if interval.end >= self.slot_count:
                    raise ValueError(
                        f"规格 {spec.name} 的允许区间 {interval.label()} 超出卡范围 "
                        f"(0–{self.slot_count - 1})"
                    )

        ids: set[str] = set()
        occupied: dict[int, str] = {}
        for slc in self.slices:
            if slc.id in ids:
                raise ValueError(f"切片 id 重复：{slc.id}")
            ids.add(slc.id)
            if slc.spec not in specs:
                raise ValueError(f"切片 {slc.id} 引用了未知规格：{slc.spec}")
            if slc.interval.end >= self.slot_count:
                raise ValueError(
                    f"切片 {slc.id} 的区间 {slc.interval.label()} 超出卡范围 "
                    f"(0–{self.slot_count - 1})"
                )
            allowed = {iv.key() for iv in specs[slc.spec].allowed_intervals}
            if slc.interval.key() not in allowed:
                raise ValueError(
                    f"切片 {slc.id} 的区间 {slc.interval.label()} 不在规格 "
                    f"{slc.spec} 的允许区间内"
                )
            for slot in slc.interval.slots():
                if slot in occupied:
                    raise ValueError(
                        f"切片 {slc.id} 与切片 {occupied[slot]} 在槽位 {slot} 重叠"
                    )
                occupied[slot] = slc.id
        return self


class NewSliceRequest(BaseModel):
    spec: str = Field(min_length=1)
    name: str | None = Field(default=None, max_length=60)


class PlanRequest(BaseModel):
    new_slices: list[NewSliceRequest]

    @model_validator(mode="after")
    def _exactly_two(self) -> "PlanRequest":
        if len(self.new_slices) != 2:
            raise ValueError("每次必须恰好填写两块新切片的规格")
        return self


class OrderRequest(BaseModel):
    slice_id: str | None = None
    force: bool = False


class ReleaseRequest(BaseModel):
    slice_id: str
    order_id: str | None = None


class JobRequest(BaseModel):
    """Submit one image-caption inference job.

    The two stages may ask for different MIG specs; both are matched exactly
    against the card's slice specs.
    """

    prompt: str = Field(default="一张待描述的图片", min_length=1, max_length=200)
    encode_spec: str = Field(min_length=1, max_length=40)
    generate_spec: str = Field(min_length=1, max_length=40)

    @field_validator("prompt", "encode_spec", "generate_spec")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value


class CompleteStageRequest(BaseModel):
    stage: Literal["encode", "generate"]
    caption: str | None = Field(default=None, max_length=500)


class FaultRequest(BaseModel):
    mode: Literal["after_apply", "before_apply"] = "after_apply"
    skip: int = Field(default=0, ge=0, le=10)
    count: int = Field(default=1, ge=1, le=10)
    message: str = "模拟驱动错误：设备忙，请重试"


class ExternalIdleSliceRequest(BaseModel):
    spec: str
    interval: SlotInterval
    id: str | None = Field(default=None, max_length=60)


class ExternalRemoveRequest(BaseModel):
    slice_id: str
