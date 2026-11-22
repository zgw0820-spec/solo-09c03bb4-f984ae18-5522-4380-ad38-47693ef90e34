"""Built-in demo layout (one card, 8 slots)."""

from __future__ import annotations

from .models import LayoutImport, SliceDef, SlotInterval, SpecDef


def _single_slots(slot_count: int = 8) -> list[SlotInterval]:
    return [SlotInterval(start=i, end=i) for i in range(slot_count)]


DEFAULT_LAYOUT = LayoutImport(
    slot_count=8,
    specs=[
        SpecDef(name="1g.10gb", allowed_intervals=_single_slots()),
        SpecDef(
            name="2g.20gb",
            allowed_intervals=[
                SlotInterval(start=0, end=1),
                SlotInterval(start=2, end=3),
                SlotInterval(start=4, end=5),
                SlotInterval(start=6, end=7),
            ],
        ),
        SpecDef(
            name="3g.40gb",
            allowed_intervals=[
                SlotInterval(start=0, end=2),
                SlotInterval(start=4, end=6),
            ],
        ),
        SpecDef(
            name="7g.80gb",
            allowed_intervals=[
                SlotInterval(start=0, end=6),
                SlotInterval(start=1, end=7),
            ],
        ),
    ],
    slices=[
        SliceDef(id="train-a", spec="3g.40gb", interval=SlotInterval(start=0, end=2), kind="running"),
        SliceDef(id="train-b", spec="1g.10gb", interval=SlotInterval(start=3, end=3), kind="running"),
        SliceDef(id="idle-1", spec="3g.40gb", interval=SlotInterval(start=4, end=6), kind="idle"),
        SliceDef(id="idle-2", spec="1g.10gb", interval=SlotInterval(start=7, end=7), kind="idle"),
    ],
)


def default_layout_json() -> dict:
    return DEFAULT_LAYOUT.model_dump()


SELFTEST_LAYOUT = LayoutImport(
    slot_count=8,
    specs=[
        SpecDef(name="1g.10gb", allowed_intervals=_single_slots()),
        SpecDef(
            name="2g.20gb",
            allowed_intervals=[
                SlotInterval(start=0, end=1),
                SlotInterval(start=2, end=3),
                SlotInterval(start=4, end=5),
                SlotInterval(start=6, end=7),
            ],
        ),
        SpecDef(name="7g.80gb", allowed_intervals=[SlotInterval(start=0, end=6)]),
    ],
    slices=[
        SliceDef(id="st-run-a", spec="2g.20gb", interval=SlotInterval(start=0, end=1), kind="running"),
        SliceDef(id="st-run-b", spec="2g.20gb", interval=SlotInterval(start=4, end=5), kind="running"),
        SliceDef(id="st-idle-a", spec="1g.10gb", interval=SlotInterval(start=2, end=2), kind="idle"),
        SliceDef(id="st-idle-b", spec="1g.10gb", interval=SlotInterval(start=3, end=3), kind="idle"),
        SliceDef(id="st-idle-c", spec="2g.20gb", interval=SlotInterval(start=6, end=7), kind="idle"),
    ],
)
