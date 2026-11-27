"""Logic's undo history, which it saves in the project.

Each undo step is an `lESU` record (e.g., "Create Track" or "Load Patch",
with what's needed to undo it, a few kilobytes to hundreds), and a `ktSU`
record ends with the number of steps, twice. A template's history is only
how it was made, and loading a patch can leave hundreds of kilobytes of it,
so templates are kept without it.
"""

from __future__ import annotations

from .container import ProjectData, Record, put_u32

_STEP = b"lESU"
_STACK = b"ktSU"


def strip_undo(pd: ProjectData) -> int:
    """Remove the undo history, leaving an empty undo stack. Returns how many
    steps were removed."""
    steps = sum(1 for r in pd.records if r.tag == _STEP)
    pd.records = [r for r in pd.records if r.tag != _STEP]
    for i, r in enumerate(pd.records):
        if r.tag == _STACK:
            raw = bytearray(r.raw)
            put_u32(raw, len(raw) - 8, 0)
            put_u32(raw, len(raw) - 4, 0)
            pd.records[i] = Record(r.tag, bytes(raw))
    return steps
