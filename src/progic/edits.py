"""Apply a JSON edit file to a project.

The format is documented in `EDIT_FORMAT` below, which is also what
`progic edit-format` prints. Edits are validated and applied in memory; the
caller writes the project only if everything succeeded.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import addtrack, audio, automation, plugins, regions, timeline, tracks
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
  "end":     {"bar": 9},                     // the project end marker, where
                                             //   exporting stops
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
      "plugins": [                           // set plugin parameters
        {
          "plugin": "Mania",                 // its name, or slot number
          "params": {"swing": 30}            // by id, real values (not 0-1);
                                             //   `progic inspect --params`
        },
        {"plugin": "Chorus", "remove": true},// or remove a plugin
        {"plugin": "Arpeggiator",            // or replace it with a copy of
         "replace_with": {"track": 2,        //   another track's, with its
                          "plugin": "Purpose"}} //   settings
      ],
      "clear_regions": true,                 // delete existing regions first
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
      ],
      "automation": [                        // REPLACES a parameter's automation,
                                             //   ramping between points
        {"param": "volume",                  // "volume" or "pan"
         "points": [{"bar": 1, "db": -60},   // volume in dB (or "value", the
                    {"bar": 9, "db": 0}]},   //   fader's 0-127, 90 = 0 dB)
        {"param": "pan",
         "points": [{"bar": 1, "value": 64}, // pan 0-127, 64 = center
                    {"bar": 5, "beat": 3, "value": 20}]}
      ],
      "audio_regions": [                     // ADD audio regions (audio tracks)
        {
          "bar": 1, "beat": 1,               // region start
          "file": "Ocean.wav",               // a PCM WAV, relative to the edit
                                             //   file; copied into the project
          "until": {"bar": 65},              // optional: repeat the file back
                                             //   to back until here, trimming
                                             //   the last copy; default: once
          "name": "Ocean"                    // optional, default: file name
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

Order of application: meter, tempo, end, markers, then tracks in the order
given.
Bar positions are resolved against the meter after the "meter" edit. Track
references are resolved before any renames, so "track" always uses names
as they were in the input project.

Requirements:
  - "plugins" can set the parameters of plugins built with JUCE, which
    `progic inspect` marks "juce": true. It can remove any plugin, or
    replace it with a copy of another of the same kind (e.g., a MIDI effect
    with a MIDI effect).
  - "markers" needs a project with a marker track (in Logic: add a marker,
    delete it, save). `progic inspect` reports "has_marker_track".
  - "regions" need instrument tracks.
  - "audio_regions" need audio tracks, and a project with an audio region
    to copy (in Logic: put any audio file on an audio track and save). The
    file must be a PCM WAV at the project's sample rate; convert anything
    else first, e.g. `afconvert -f WAVE -d LEI24@48000 in.caf out.wav`.
  - "add" needs a template with at least 3 instrument tracks whose most
    recently created track is an instrument track. `progic inspect` reports
    "can_add_tracks" (with "cannot_add_tracks_reason" if not).
"""

_NOTE_NAMES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_NOTE_RE = re.compile(r"^([A-Ga-g])([#b]?)(-?\d+)$")


class EditError(Exception):
    pass


@dataclass
class Media:
    """The audio files an edit adds, for the caller to copy into the project."""

    base: Path = Path(".")
    """What audio file paths are relative to."""
    taken: set[str] = field(default_factory=lambda: set())
    """File names already in the project's audio folder."""
    files: dict[Path, audio.AudioFile] = field(default_factory=lambda: {})
    prototype: audio.Prototype | None = None
    rate: int | None = None
    """The project's sample rate, which audio files must match."""


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


def _audio_file(media: Media, value: Any, where: str) -> audio.AudioFile:
    if not isinstance(value, str):
        raise EditError(f"{where}: 'file' must be a path to a WAV file")
    path = (media.base / value).resolve()
    if path in media.files:
        return media.files[path]
    name = path.name
    stem, suffix = path.stem, path.suffix
    n = 1
    while name in media.taken:
        name = f"{stem}_{n}{suffix}"
        n += 1
    try:
        f = audio.read_audio_file(path, name)
    except audio.AudioError as e:
        raise EditError(f"{where}: {e}") from e
    if media.rate is not None and f.rate != media.rate:
        raise EditError(
            f"{where}: {path.name} is {f.rate} Hz but the project is {media.rate} Hz, so it would play at the "
            f"wrong speed; convert it first, e.g. `afconvert -f WAVE -d LEI24@{media.rate} '{path.name}' out.wav`"
        )
    media.taken.add(name)
    media.files[path] = f
    return f


def _audio_regions(
    pd: ProjectData, tm: TimeMap, media: Media, track: tracks.Track, obj: dict[str, Any], where: str
) -> int:
    unknown = set(obj) - {"bar", "beat", "file", "until", "name"}
    if unknown:
        raise EditError(f"{where}: unknown keys: {', '.join(sorted(unknown))}")
    start = _tick(tm, obj, where)
    f = _audio_file(media, obj.get("file"), where)
    name = obj.get("name")
    if name is not None and not isinstance(name, str):
        raise EditError(f"{where}: 'name' must be a string")
    tempo = timeline.tempo_map(pd)
    seconds = f.frames / f.rate
    if "until" in obj:
        if not isinstance(obj["until"], dict):
            raise EditError(f"{where}: 'until' must be an object like {{\"bar\": 65}}")
        end = timeline.seconds_at(tempo, _tick(tm, obj["until"], f"{where}.until"))
        if end <= timeline.seconds_at(tempo, start):
            raise EditError(f"{where}: 'until' must be after the region's start")
    else:
        end = timeline.seconds_at(tempo, start) + seconds
    # Copies back to back, each starting where the last one's audio ends, the
    # last trimmed to end at `end`.
    count = 0
    at = timeline.seconds_at(tempo, start)
    while end - at > 1 / f.rate:
        frames = min(f.frames, round((end - at) * f.rate))
        try:
            audio.add_audio_region(
                pd, track, f, tick=round(timeline.tick_at(tempo, at)), frames=frames, name=name,
                prototype=media.prototype,
            )
        except regions.RegionError as e:
            raise EditError(f"{where}: {e}") from e
        at += seconds
        count += 1
    return count


def _automation(pd: ProjectData, tm: TimeMap, track: tracks.Track, value: Any, where: str) -> list[str]:
    new: dict[int, list[automation.Point]] = {}
    for i, lane in enumerate(_changes(value, where)):
        lw = f"{where}[{i}]"
        unknown = set(lane) - {"param", "points"}
        if unknown:
            raise EditError(f"{lw}: unknown keys: {', '.join(sorted(unknown))}")
        name = lane.get("param")
        if name not in automation.PARAMS:
            raise EditError(f"{lw}: 'param' must be one of {', '.join(automation.PARAMS)} (got {name!r})")
        points: list[automation.Point] = []
        for j, p in enumerate(_changes(lane.get("points", []), f"{lw}.points")):
            pw = f"{lw}.points[{j}]"
            unknown = set(p) - {"bar", "beat", "db", "value"}
            if unknown:
                raise EditError(f"{pw}: unknown keys: {', '.join(sorted(unknown))}")
            if ("db" in p) == ("value" in p) or ("db" in p and name != "volume"):
                raise EditError(f"{pw}: give 'value' (0-127)" + (" or 'db'" if name == "volume" else ""))
            v = automation.db_to_value(_number(p, "db", pw)) if "db" in p else _number(p, "value", pw)
            if not 0 <= v <= 127:
                raise EditError(f"{pw}: value must be 0-127 (got {v:g})")
            points.append(automation.Point(_tick(tm, p, pw), v))
        if not points:
            raise EditError(f"{lw}: needs at least one point")
        new[automation.PARAMS[name]] = points
    try:
        automation.set_lanes(pd, track, new)
    except automation.AutomationError as e:
        raise EditError(f"{where}: {e}") from e
    return [str(lane["param"]) for lane in value]


def _plugin(track: tracks.Track, ref: Any, where: str) -> tracks.Plugin:
    if not isinstance(ref, (int, str)) or isinstance(ref, bool):
        raise EditError(f"{where}: 'plugin' must be a plugin's name or slot number")
    try:
        return tracks.find_plugin(track, ref)
    except tracks.TrackError as e:
        raise EditError(f"{where}: {e}") from e


def _apply_plugins(pd: ProjectData, track: tracks.Track, value: Any, where: str) -> list[dict[str, Any]]:
    done: list[dict[str, Any]] = []
    for i, pe in enumerate(_changes(value, where)):
        pw = f"{where}[{i}]"
        unknown = set(pe) - {"plugin", "params", "remove", "replace_with"}
        if unknown:
            raise EditError(f"{pw}: unknown keys: {', '.join(sorted(unknown))}")
        plugin = _plugin(track, pe.get("plugin"), pw)
        assert plugin.record is not None
        if sum(1 for k in ("params", "remove", "replace_with") if pe.get(k)) > 1:
            raise EditError(f"{pw}: give one of 'params', 'remove', or 'replace_with'")
        if "replace_with" in pe:
            source_ref = pe["replace_with"]
            if not isinstance(source_ref, dict) or set(source_ref) != {"track", "plugin"}:
                raise EditError(f"{pw}: 'replace_with' must be like {{\"track\": 2, \"plugin\": \"Purpose\"}}")
            try:
                source_track = tracks.find_track(pd, source_ref["track"])
            except tracks.TrackError as e:
                raise EditError(f"{pw}.replace_with: {e}") from e
            source = _plugin(source_track, source_ref["plugin"], f"{pw}.replace_with")
            assert source.record is not None
            copy = plugins.copied_into(source.record, plugin.record)
            pd.records[pd.records.index(plugin.record)] = copy
            track.plugins[track.plugins.index(plugin)] = dataclasses.replace(source, slot=plugin.slot, record=copy)
            done.append({"plugin": plugin.name, "slot": plugin.slot, "replaced_with": source.name})
            continue
        if pe.get("remove"):
            # The plugin is its slot's record, which Logic loads without
            pd.records.remove(plugin.record)
            track.plugins.remove(plugin)
            done.append({"plugin": plugin.name, "slot": plugin.slot, "removed": True})
            continue
        values = pe.get("params", {})
        if not isinstance(values, dict):
            raise EditError(f"{pw}: 'params' must be an object of parameter ids and values")
        for name in values:
            _number(values, name, f"{pw}.params")
        try:
            plugins.set_params(plugin.record, {k: float(v) for k, v in values.items()})
        except plugins.PluginError as e:
            raise EditError(f"{pw}: {plugin.name or 'plugin'} (slot {plugin.slot}): {e}") from e
        done.append({"plugin": plugin.name, "slot": plugin.slot, "params": len(values)})
    return done


def apply(pd: ProjectData, edits: Any, media: Media | None = None) -> dict[str, Any]:
    """Apply `edits` (parsed JSON) to `pd`. Returns a summary of what changed.
    The audio files it adds are collected in `media`, for the caller to copy
    into the project."""
    media = media or Media()
    # Taken before any regions are cleared, which could remove the only one.
    media.prototype = media.prototype or audio.extract_prototype(pd)
    media.taken |= set(audio.audio_file_names(pd))
    media.rate = media.rate or audio.sample_rate(pd)
    if not isinstance(edits, dict):
        raise EditError("edit file must be a JSON object")
    doc: dict[str, Any] = edits  # type: ignore[assignment]
    unknown = set(doc) - {"meter", "tempo", "end", "markers", "tracks"}
    if unknown:
        raise EditError(f"unknown top-level keys: {', '.join(sorted(unknown))}")
    summary: dict[str, Any] = {}

    if "meter" in doc:
        summary["meter"] = [f"{p.numerator}/{p.denominator}" for p in _apply_meter(pd, doc["meter"])]
    tm = TimeMap(timeline.meter_map(pd))
    if "tempo" in doc:
        summary["tempo"] = [p.bpm for p in _apply_tempo(pd, tm, doc["tempo"])]
    if "end" in doc:
        if not isinstance(doc["end"], dict):
            raise EditError("end must be an object like {\"bar\": 9}")
        try:
            timeline.set_project_end(pd, _tick(tm, doc["end"], "end"))
        except timeline.TimelineError as e:
            raise EditError(f"end: {e}") from e
        summary["end"] = doc["end"]
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
            unknown = set(te) - {
                "track", "add", "name", "volume", "pan", "plugins", "clear_regions", "regions", "audio_regions",
                "automation",
            }
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
                    result["regions_removed"] = regions.remove_midi_regions(pd, track) + audio.remove_audio_regions(
                        pd, track
                    )
                if "volume" in te or "pan" in te:
                    volume = int(_number(te, "volume", where)) if "volume" in te else None
                    pan = int(_number(te, "pan", where)) if "pan" in te else None
                    tracks.set_mixer(track, volume=volume, pan=pan)
                    result["mixer"] = {k: v for k, v in (("volume", volume), ("pan", pan)) if v is not None}
                if "plugins" in te:
                    result["plugins"] = _apply_plugins(pd, track, te["plugins"], f"{where}.plugins")
                if "automation" in te:
                    result["automation"] = _automation(pd, tm, track, te["automation"], f"{where}.automation")
                added = 0
                for j, r in enumerate(_changes(te.get("regions", []), f"{where}.regions")):
                    _region(pd, tm, track, r, f"{where}.regions[{j}]")
                    added += 1
                if added:
                    result["regions_added"] = added
                added = 0
                for j, r in enumerate(_changes(te.get("audio_regions", []), f"{where}.audio_regions")):
                    added += _audio_regions(pd, tm, media, track, r, f"{where}.audio_regions[{j}]")
                if added:
                    result["audio_regions_added"] = added
            except (tracks.TrackError, regions.RegionError, plugins.PluginError) as e:
                raise EditError(f"{where}: {e}") from e
            done.append(result)
        summary["tracks"] = done
    return summary
