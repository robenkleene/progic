---
name: progic
description: Create and edit Logic Pro projects (.logicx) with the `progic` CLI, e.g., setting up a song from a template with tempo, markers, track names, mixer settings, and MIDI parts. Use when asked to make, arrange, or modify a Logic Pro project or write MIDI into one.
---

# Progic

`progic` edits Logic Pro projects without Logic. It works from a template made in Logic: the template supplies tracks, instruments, and plugins, and progic writes tempo, time signatures, markers, track names, volume and pan, and MIDI regions.

## Workflow

1. Inspect the template: `progic inspect Template.logicx --text` (or without `--text` for JSON). Note each track's number, name, and kind, the plugins loaded on it, and `has_marker_track`.
2. Read the edit format once per session: `progic edit-format`.
3. Write an edit file (JSON) and apply it to a new path: `progic apply Template.logicx edits.json -o Song.logicx`. Use `--dry-run` to validate first.
4. Check the result: `progic inspect Song.logicx --notes`.
5. To revise, apply a further edit file to the output with `--in-place`, or regenerate from the template.

Never overwrite the user's template; always write to a new path with `-o` unless told otherwise.

## Rules

- Positions are 1-based bars and beats. Note `start` and `length` are quarter notes from the region start.
- Pitch names use C4 = 60 (middle C). Logic shows MIDI 60 as C3; mention this if you tell the user note names.
- Only existing tracks can be edited. Choose tracks by their instrument and plugins from `inspect` (e.g. put a bass line on the track with a bass patch), and rename them to match their role. If the template lacks a track you need, tell the user rather than putting the part on an unsuitable track.
- MIDI regions only go on `instrument` tracks.
- `markers` replaces all markers and needs `has_marker_track: true`.
- `tempo` and `meter` replace the whole map.
- `clear_regions: true` on a track removes its MIDI regions before adding new ones; use it when regenerating a part.
- Errors name the failing field (e.g. `tracks[1].regions[0]: ...`); fix that field and re-run. A failed `apply` writes nothing.

## Writing Parts

- Lay out the song with markers first (intro, build, breakdown, drop, outro) and place regions to match.
- Write full notes for each region; there are no loops or pattern repeats, so repeat a phrase by writing another region or repeating notes within one.
- Keep velocities musical (roughly 70-115) and vary them; quantized notes with identical velocities sound mechanical.
- Default region length is the notes rounded up to whole bars; set `length_bars` to make a region longer than its notes.
