---
name: progic
description: Create and edit Logic Pro projects (.logicx) with the `progic` CLI, e.g., setting up a song from a template with tempo, markers, tracks, mixer settings, and MIDI parts. Use when asked to make, arrange, or modify a Logic Pro project or write MIDI into one.
---

# Progic

`progic` edits Logic Pro projects without Logic. It works from a template made in Logic: the template's tracks supply instruments and plugins, and progic writes tempo, time signatures, markers, track names, volume and pan, the parameters of plugins built with JUCE, and MIDI regions. It can also add empty software instrument tracks.

## Workflow

1. Inspect the template: `progic inspect Template.logicx --text` (or without `--text` for JSON). Note each track's number, name, and kind, the plugins loaded on it, `has_marker_track`, and `can_add_tracks`.
2. Read the edit format once per session: `progic edit-format`.
3. Write an edit file (JSON) and apply it to a new path: `progic apply Template.logicx edits.json -o Song.logicx`. Use `--dry-run` to validate first.
4. Check the result: `progic inspect Song.logicx --notes`.
5. To revise, apply a further edit file to the output with `--in-place`, or regenerate from the template.

Never overwrite the user's template; always write to a new path with `-o` unless told otherwise.

## Rules

- Positions are 1-based bars and beats. Note `start` and `length` are quarter notes from the region start.
- Pitch names use C4 = 60 (middle C). Logic shows MIDI 60 as C3; mention this if you tell the user note names.
- Choose tracks by their instrument and plugins from `inspect` (e.g. put a bass line on the track with a bass patch), and rename them to match their role.
- If the template lacks a track you need and `can_add_tracks` is true, add one with `{"add": "instrument", "name": ...}`. Added tracks have no instrument loaded, so tell the user which added tracks need a sound and what kind. Prefer an existing track with a suitable sound over adding one.
- MIDI regions only go on `instrument` tracks.
- Plugins built with JUCE (`"juce": true` in `inspect`) have their parameters set with a track's `plugins`, by id with real values (not 0-1). `progic inspect --params` lists the ids and current values. Unknown ids are an error, with the closest ids suggested. `{"plugin": ..., "remove": true}` removes a plugin, e.g., a MIDI effect in front of an instrument, so the track's regions play the instrument directly. `{"plugin": ..., "replace_with": {"track": ..., "plugin": ...}}` replaces a plugin with a copy of another track's, settings and all, e.g., to put a MIDI effect in front of another track's instrument. Replace a plugin with one of the same kind.
- `markers` replaces all markers and needs `has_marker_track: true`.
- `tempo` and `meter` replace the whole map.
- `end` moves the project end marker, where exporting stops (bar 129 in a new project). Set it to the end of the song so exports aren't padded with the rest.
- `clear_regions: true` on a track removes its MIDI regions before adding new ones; use it when regenerating a part.
- Errors name the failing field (e.g. `tracks[1].regions[0]: ...`); fix that field and re-run. A failed `apply` writes nothing.

## Writing Parts

- Lay out the song with markers first (intro, build, breakdown, drop, outro) and place regions to match.
- Write full notes for each region; there are no loops or pattern repeats, so repeat a phrase by writing another region or repeating notes within one.
- Keep velocities musical (roughly 70-115) and vary them; quantized notes with identical velocities sound mechanical.
- Default region length is the notes rounded up to whole bars; set `length_bars` to make a region longer than its notes.
