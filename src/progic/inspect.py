"""A JSON-friendly summary of a project, in musical units.

Positions are 1-based bars and beats (a beat is one unit of the time
signature denominator). Note starts and lengths inside a region are in
quarter notes from the region start, which keeps them independent of meter.
"""

from __future__ import annotations

from typing import Any

from . import addtrack, regions, timeline, tracks
from .container import ProjectData
from .timeline import PPQ, TimeMap


def _round(x: float) -> float | int:
    r = round(x, 4)
    return int(r) if r == int(r) else r


def position(tm: TimeMap, tick: int) -> dict[str, float | int]:
    bar, beat = tm.to_bar_beat(tick)
    return {"bar": bar, "beat": _round(beat)}


def _length_bars(tm: TimeMap, tick: int, length: int) -> float | int:
    """Length in bars, counting each bar at its own time signature."""
    bars = 0.0
    pos = tick
    end = tick + length
    while pos < end:
        bar, _ = tm.to_bar_beat(pos)
        bar_len = tm.bar_ticks(bar)
        bar_end = tm.to_tick(bar) + bar_len
        step = min(end, bar_end) - pos
        bars += step / bar_len
        pos += step
    return _round(bars)


def plugin_json(p: tracks.Plugin) -> dict[str, Any]:
    out: dict[str, Any] = {"slot": p.slot, "name": p.name}
    if p.native:
        out["native"] = True
    else:
        out["audio_unit"] = f"{p.au_type}/{p.au_subtype}/{p.au_manufacturer}"
    if p.preset:
        out["preset"] = p.preset
    return out


def note_json(n: regions.Note) -> dict[str, Any]:
    return {"start": _round(n.tick / PPQ), "length": _round(n.length / PPQ), "pitch": n.pitch, "velocity": n.velocity}


def inspect(pd: ProjectData, *, notes: bool = False) -> dict[str, Any]:
    meter = timeline.meter_map(pd)
    tm = TimeMap(meter)
    by_track: dict[int, list[regions.MidiRegion]] = {}
    for r in regions.midi_regions(pd):
        by_track.setdefault(r.track, []).append(r)

    track_list: list[dict[str, Any]] = []
    for t in tracks.tracks(pd):
        entry: dict[str, Any] = {"number": t.number, "name": t.name, "kind": t.kind}
        if t.strip is not None:
            entry["volume"] = t.volume
            entry["pan"] = t.pan
        entry["plugins"] = [plugin_json(p) for p in t.plugins]
        region_list: list[dict[str, Any]] = []
        for r in by_track.get(t.number, []):
            region: dict[str, Any] = {
                "name": r.name,
                **position(tm, r.tick),
                "length_bars": _length_bars(tm, r.tick, r.length),
            }
            if notes:
                region["notes"] = [note_json(n) for n in r.notes]
            else:
                region["note_count"] = len(r.notes)
            region_list.append(region)
        entry["regions"] = region_list
        track_list.append(entry)

    cannot_add = addtrack.can_add_instrument(pd)
    summary: dict[str, Any] = {
        "tempo": [{**position(tm, p.tick), "bpm": _round(p.bpm)} for p in timeline.tempo_map(pd)],
        "meter": [{**position(tm, p.tick), "signature": f"{p.numerator}/{p.denominator}"} for p in meter],
        "end": position(tm, timeline.project_end(pd)),
        "has_marker_track": timeline.has_marker_track(pd),
        "markers": [{**position(tm, m.tick), "name": m.name} for m in timeline.markers(pd)],
        "can_add_tracks": cannot_add is None,
        "tracks": track_list,
    }
    if cannot_add is None:
        summary["free_instrument_strips"] = addtrack.free_instrument_strips(pd)
    else:
        summary["cannot_add_tracks_reason"] = cannot_add
    return summary


def text(summary: dict[str, Any]) -> str:
    """A compact human-readable rendering of `inspect` output."""
    lines: list[str] = []
    tempo = ", ".join(f"{p['bpm']} BPM @ bar {p['bar']}" for p in summary["tempo"])
    meter = ", ".join(f"{p['signature']} @ bar {p['bar']}" for p in summary["meter"])
    lines.append(f"Tempo: {tempo}")
    lines.append(f"Meter: {meter}")
    lines.append(f"End: bar {summary['end']['bar']} beat {summary['end']['beat']}")
    if summary["has_marker_track"]:
        markers = ", ".join(f"{m['name']!r} @ bar {m['bar']}" for m in summary["markers"]) or "none"
        lines.append(f"Markers: {markers}")
    else:
        lines.append("Markers: no marker track (markers can't be added)")
    if summary["can_add_tracks"]:
        lines.append(f"Adding tracks: yes, up to {summary['free_instrument_strips']} instrument tracks")
    else:
        lines.append(f"Adding tracks: no ({summary['cannot_add_tracks_reason']})")
    lines.append("Tracks:")
    for t in summary["tracks"]:
        mix = f" vol {t['volume']} pan {t['pan']}" if "volume" in t else ""
        lines.append(f"  {t['number']}. {t['name']} [{t['kind']}]{mix}")
        for p in t["plugins"]:
            lines.append(f"       plugin slot {p['slot']}: {p['name'] or p.get('audio_unit', '?')}")
        for r in t["regions"]:
            count = len(r["notes"]) if "notes" in r else r["note_count"]
            lines.append(
                f"       region {r['name']!r} @ bar {r['bar']} beat {r['beat']}, {r['length_bars']} bars, {count} notes"
            )
    return "\n".join(lines)
