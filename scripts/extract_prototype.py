#!/usr/bin/env python3
"""Regenerate `src/progic/data/midi_region_prototype.json`, the region group
cloned for new MIDI regions when a project has none of its own.

The source is a Logic-saved project with an empty MIDI region on an instrument
track (by default `tests/fixtures/F23_av.logicx`, from LogicProFormatWriter).

    scripts/extract_prototype.py [SOURCE.logicx]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from progic.bundle import Bundle  # noqa: E402
from progic.regions import extract_prototype, prototype_to_json  # noqa: E402


def main() -> int:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tests" / "fixtures" / "F23_av.logicx"
    proto = extract_prototype(Bundle(source).read())
    if proto is None:
        print(f"error: {source} has no MIDI region", file=sys.stderr)
        return 1
    out = ROOT / "src" / "progic" / "data" / "midi_region_prototype.json"
    out.write_text(prototype_to_json(proto) + "\n")
    print(f"wrote {out.relative_to(ROOT)} from {source.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
