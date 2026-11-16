"""Apply a JSON edit file to a project.

The format is documented in `EDIT_FORMAT` below, which is also what
`progic edit-format` prints. Edits are validated and applied in memory; the
caller writes the project only if everything succeeded.
"""

from __future__ import annotations

import re
from typing import Any

from . import addtrack, regions, timeline, tracks
from .container import ProjectData
from .timeline import PPQ, MeterPoint, TimeMap

EDIT_FORMAT = """\
EDIT FILE FORMAT (JSON)

Every top-level key is optional. Positions are 1-based: bar 1 beat 1 is the
start of the song. A beat is one unit of the time signature denominator
(a quarter note in 4/4, an eighth note in 6/8). Fractional bars and beats
are allowed.

{
  "meter":   "4/4",                          // or a list of changes:
             // [{"bar": 1, "signature": "4/4"}, {"bar": 33, "signature": "7/8"}]
  "tempo":   138,                            // or a list of changes:
             // [{"bar": 1, "bpm": 138}, {"bar": 65, "beat": 3, "bpm": 140}]
  "markers": [                               // REPLACES all markers
    {"bar": 1, "name": "Intro"},
    {"bar": 17, "name": "Breakdown"}
  ],
  "tracks": [                                // edits to tracks, in order
    {
      "track": "Inst 1",                     // current name, or 1-based number
                                             //   (or "add", below)
      "name": "Lead",                        // rename
      "volume": 100,                         // fader, 0-127 (90 = 0 dB default)
      "pan": 64,                             // 0 = left, 64 = center, 127 = right
      "clear_regions": true,                 // delete existing MIDI regions first
      "regions": [                           // ADD MIDI regions (instrument tracks)
        {
          "bar": 33, "beat": 1,              // region start
          "length_bars": 8,                  // optional, default: notes rounded
                                             //   up to whole bars (min 1 bar)
          "name": "Lead A",                  // optional, default: track name
          "notes": [
            // start/length in QUARTER NOTES from the region start
            // pitch: MIDI number or name; names use C4 = 60 (middle C),
            //   which Logic displays as C3
            {"start": 0, "length": 0.5, "pitch": "A4", "velocity": 100},
            {"start": 0.5, "length": 0.5, "pitch": 72, "velocity": 90}
          ]
        }
      ]
    },
    {
      "add": "instrument",                   // ADD a software instrument track
      "name": "Arp",                         // required for added tracks
      "regions": [ ... ]                     // plus any other keys above
    }
  ]
}

Added tracks go after the last track, in the order given. Their channel
strip is empty: no instrument or plugins are loaded, so the user has to
choose a sound for them in Logic.

Order of application: meter, tempo, markers, then tracks in the order given.
Bar positions are resolved against the meter after the "meter" edit. Track
references are resolved before any renames, so "track" always uses names
as they were in the input project.

Requirements:
  - "markers" needs a project with a marker track (in Logic: add a marker,
    delete it, save). `progic inspect` reports "has_marker_track".
  - "regions" need instrument tracks.
  - "add" needs a template with at least 3 instrument tracks whose most
    recently created track is an instrument track. `progic inspect` reports
    "can_add_tracks" (with "cannot_add_tracks_reason" if not).
"""

_NOTE_NAMES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_NOTE_RE = re.compile(r"^([A-Ga-g])([#b]?)(-?\d+)$")


class EditError(Exception):
    pass


def parse_pitch(value: Any) -> int:
    if isinstance(value, bool):
        raise EditError(f"invalid pitch {value!r}")
    if isinstance(value, int):
        pitch = value
    elif isinstance(value, str):
        m = _NOTE_RE.match(value.strip())
        if not m:
            raise EditError(f"invalid pitch {value!r} (use a MIDI number or a name like 'C4', 'F#3', 'Bb2')")
        letter, accidental, octave = m.groups()
        pitch = _NOTE_NAMES[letter.upper()] + {"#": 1, "b": -1, "": 0}[accidental] + (int(octave) + 1) * 12
    else:
        raise EditError(f"invalid pitch {value!r}")
    if not 0 <= pitch <= 127:
        raise EditError(f"pitch {value!r} is outside MIDI range 0-127")
    return pitch


def _number(obj: dict[str, Any], key: str, where: str, default: float | None = None) -> float:
    value = obj.get(key, default)
    if value is None:
        raise EditError(f"{where}: missing {key!r}")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EditError(f"{where}: {key!r} must be a number (got {value!r})")
    return float(value)


def _signature(value: Any, where: str) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"\d+/\d+", value):
        raise EditError(f"{where}: signature must look like '4/4' (got {value!r})")
    num, den = (int(x) for x in value.split("/"))
    if not 1 <= num <= 99:
        raise EditError(f"{where}: numerator must be 1-99")
    return num, den


def _tick(tm: TimeMap, obj: dict[str, Any], where: str) -> int:
    try:
        return tm.to_tick(_number(obj, "bar", where), _number(obj, "beat", where, 1))
    except timeline.TimelineError as e:
        raise EditError(f"{where}: {e}") from e


def _changes(value: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise EditError(f"{where} must be a list of objects")
    return value  # type: ignore[return-value]


def _apply_meter(pd: ProjectData, value: Any) -> list[MeterPoint]:
    if isinstance(value, str):
        num, den = _signature(value, "meter")
        points = [MeterPoint(0, num, den)]
    else:
        # Bars in meter changes are resolved progressively, since each change
        # moves the bar lines after it.
        points: list[MeterPoint] = []
        items = sorted(_changes(value, "meter"), key=lambda c: _number(c, "bar", "meter"))
        for i, change in enumerate(items):
            where = f"meter[{i}]"
            num, den = _signature(change.get("signature"), where)
            if "beat" in change and change["beat"] != 1:
                raise EditError(f"{where}: meter changes must fall on beat 1")
            bar = _number(change, "bar", where)
            if bar != int(bar):
                raise EditError(f"{where}: meter changes must fall on a whole bar")
            if not points and bar != 1:
                points.append(MeterPoint(0, 4, 4))
            tick = TimeMap(points).to_tick(bar) if points else 0
            points.append(MeterPoint(tick, num, den))
    try:
        timeline.set_meter_map(pd, points)
    except timeline.TimelineError as e:
        raise EditError(f"meter: {e}") from e
    return points


def _apply_tempo(pd: ProjectData, tm: TimeMap, value: Any) -> list[timeline.TempoPoint]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        points = [timeline.TempoPoint(0, float(value))]
    else:
        points = [
            timeline.TempoPoint(_tick(tm, c, f"tempo[{i}]"), _number(c, "bpm", f"tempo[{i}]"))
            for i, c in enumerate(_changes(value, "tempo"))
        ]
    try:
        timeline.set_tempo_map(pd, points)
    except timeline.TimelineError as e:
        raise EditError(f"tempo: {e}") from e
    return points


def _apply_markers(pd: ProjectData, tm: TimeMap, value: Any) -> int:
    new: list[timeline.Marker] = []
    for i, m in enumerate(_changes(value, "markers")):
        name = m.get("name")
        if not isinstance(name, str):
            raise EditError(f"markers[{i}]: 'name' must be a string")
        new.append(timeline.Marker(_tick(tm, m, f"markers[{i}]"), name))
    try:
        timeline.set_markers(pd, new)
    except timeline.TimelineError as e:
        raise EditError(f"markers: {e}") from e
    return len(new)


def _region(pd: ProjectData, tm: TimeMap, track: tracks.Track, obj: dict[str, Any], where: str) -> regions.MidiRegion:
    start = _tick(tm, obj, where)
    notes: list[regions.Note] = []
    for j, n in enumerate(obj.get("notes", [])):
        nw = f"{where}.notes[{j}]"
        if not isinstance(n, dict):
            raise EditError(f"{nw} must be an object")
        note: dict[str, Any] = n  # type: ignore[assignment]
        notes.append(
            regions.Note(
                tick=round(_number(note, "start", nw) * PPQ),
                pitch=parse_pitch(note.get("pitch")),
                velocity=int(_number(note, "velocity", nw, 100)),
                length=round(_number(note, "length", nw) * PPQ),
            )
        )
    if "length_bars" in obj:
        bars = _number(obj, "length_bars", where)
        start_bar, start_beat = tm.to_bar_beat(start)
        length = tm.to_tick(start_bar + bars, start_beat) - start
    else:
        end = start + max((n.tick + n.length for n in notes), default=0)
        bar, beat = tm.to_bar_beat(end)
        length = max(tm.to_tick(bar + (1 if beat > 1 else 0)) - start, tm.bar_ticks(tm.to_bar_beat(start)[0]))
    name = obj.get("name")
    if name is not None and not isinstance(name, str):
        raise EditError(f"{where}: 'name' must be a string")
    try:
        return regions.add_midi_region(pd, track, tick=start, length=length, notes=notes, name=name)
    except regions.RegionError as e:
        raise EditError(f"{where}: {e}") from e


def apply(pd: ProjectData, edits: Any) -> dict[str, Any]:
    """Apply `edits` (parsed JSON) to `pd`. Returns a summary of what changed."""
    if not isinstance(edits, dict):
        raise EditError("edit file must be a JSON object")
    doc: dict[str, Any] = edits  # type: ignore[assignment]
    unknown = set(doc) - {"meter", "tempo", "markers", "tracks"}
    if unknown:
        raise EditError(f"unknown top-level keys: {', '.join(sorted(unknown))}")
    summary: dict[str, Any] = {}

    if "meter" in doc:
        summary["meter"] = [f"{p.numerator}/{p.denominator}" for p in _apply_meter(pd, doc["meter"])]
    tm = TimeMap(timeline.meter_map(pd))
    if "tempo" in doc:
        summary["tempo"] = [p.bpm for p in _apply_tempo(pd, tm, doc["tempo"])]
    if "markers" in doc:
        summary["markers"] = _apply_markers(pd, tm, doc["markers"])

    if "tracks" in doc:
        track_edits = _changes(doc["tracks"], "tracks")
        # Resolve every reference up front so renames can't change what a
        # later edit refers to.
        # Added tracks go at the end, so existing track numbers don't change.
        targets: list[tracks.Track | None] = []
        for i, te in enumerate(track_edits):
            where = f"tracks[{i}]"
            unknown = set(te) - {"track", "add", "name", "volume", "pan", "clear_regions", "regions"}
            if unknown:
                raise EditError(f"{where}: unknown keys: {', '.join(sorted(unknown))}")
            if ("track" in te) == ("add" in te):
                raise EditError(f"{where}: give exactly one of 'track' (an existing track) or 'add'")
            if "add" not in te:
                try:
                    targets.append(tracks.find_track(pd, te["track"]))
                except tracks.TrackError as e:
                    raise EditError(f"{where}: {e}") from e
                continue
            if te["add"] != "instrument":
                raise EditError(f"{where}: 'add' must be \"instrument\" (got {te['add']!r})")
            if not isinstance(te.get("name"), str):
                raise EditError(f"{where}: an added track needs a 'name'")
            if te.get("clear_regions"):
                raise EditError(f"{where}: an added track has no regions to clear")
            targets.append(None)
        done: list[dict[str, Any]] = []
        for i, (te, target) in enumerate(zip(track_edits, targets)):
            where = f"tracks[{i}]"
            try:
                if target is None:
                    track = addtrack.add_instrument_track(pd)
                    result: dict[str, Any] = {"track": track.number, "added": "instrument"}
                else:
                    # Look the track up again: earlier edits may have replaced records.
                    track = tracks.find_track(pd, target.number)
                    result = {"track": track.number}
                # Renaming first means new regions default to the new name.
                if "name" in te:
                    if not isinstance(te["name"], str):
                        raise EditError(f"{where}: 'name' must be a string")
                    tracks.rename(track, te["name"])
                    result["renamed"] = te["name"]
                if te.get("clear_regions"):
                    result["regions_removed"] = regions.remove_midi_regions(pd, track)
                if "volume" in te or "pan" in te:
                    volume = int(_number(te, "volume", where)) if "volume" in te else None
                    pan = int(_number(te, "pan", where)) if "pan" in te else None
                    tracks.set_mixer(track, volume=volume, pan=pan)
                    result["mixer"] = {k: v for k, v in (("volume", volume), ("pan", pan)) if v is not None}
                added = 0
                for j, r in enumerate(_changes(te.get("regions", []), f"{where}.regions")):
                    _region(pd, tm, track, r, f"{where}.regions[{j}]")
                    added += 1
                if added:
                    result["regions_added"] = added
            except (tracks.TrackError, regions.RegionError) as e:
                raise EditError(f"{where}: {e}") from e
            done.append(result)
        summary["tracks"] = done
    return summary
