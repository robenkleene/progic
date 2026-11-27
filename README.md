# Progic

Progic is a command-line tool for creating and editing Logic Pro projects (`.logicx`) without Logic. It's designed to be driven by LLM agents: every command takes or prints JSON, positions are musical (bars, beats, quarter notes), and errors say exactly which part of the input was wrong.

You build a template in Logic with the tracks, instruments, and plugins you want. Progic then writes the musical content around it: tempo, time signatures, markers, track names, mixer settings, the parameters of plugins built with JUCE, MIDI regions with notes, audio regions playing audio files, and volume and pan automation. It can also add empty software instrument tracks.

## Quick Start for Agents

``` sh
progic inspect Template.logicx --text       # what's in the template
progic edit-format                          # the edit file reference
progic apply Template.logicx edits.json -o Song.logicx
progic inspect Song.logicx --notes          # check the result
```

The loop is always: `inspect` the project, write a JSON edit file, `apply` it to a new output path, then `inspect` the output to confirm. `progic <command> --help` describes each command.

A minimal edit file:

``` json
{
  "tempo": 138,
  "markers": [{"bar": 1, "name": "Intro"}, {"bar": 33, "name": "Drop"}],
  "tracks": [
    {
      "track": "Inst 1",
      "name": "Lead",
      "regions": [
        {
          "bar": 33,
          "length_bars": 4,
          "notes": [
            {"start": 0, "length": 0.5, "pitch": "A4", "velocity": 100},
            {"start": 0.5, "length": 0.5, "pitch": "C5"}
          ]
        }
      ]
    }
  ]
}
```

## Commands

### `progic inspect PROJECT [--notes] [--params] [--text]`

Prints the tempo map, meter map, project end, markers, and every track with its number, name, kind (`instrument`, `audio`, `aux`, ...), volume, pan, plugins, and MIDI and audio regions. It also reports `can_add_audio_regions`, `can_add_tracks`, with `free_instrument_strips` (how many tracks can be added) or `cannot_add_tracks_reason`. JSON by default; `--text` for a compact summary; `--notes` to include every note; `--params` to include the parameters of plugins built with JUCE (marked `"juce": true`).

### `progic apply PROJECT EDITS (-o OUTPUT | --in-place | --dry-run) [--force]`

Applies a JSON edit file (`-` reads it from stdin). With `-o` the project is copied to `OUTPUT` first; with `--in-place` the input is modified; with `--dry-run` nothing is written. All edits are validated in memory before anything is written, so a failed `apply` changes nothing. Prints a JSON summary of the changes.

### `progic plugin-state PROJECT TRACK PLUGIN [-o OUTPUT]`

Prints the state of a plugin built with JUCE (`"juce": true` in `inspect`), the XML its `getStateInformation` saves, as Logic restores it. The track is its name or number, the plugin its name or slot. E.g., to render the plugin offline with the same settings, or compare its settings between projects.

### `progic new TEMPLATE OUTPUT [--force]`

Copies a template to a new project, leaving out Logic's backup folders.

### `progic strip-undo PROJECT (-o OUTPUT | --in-place) [--force]`

Removes the undo history Logic saves in a project. Strip it from templates: their history is only how they were made, and loading patches can leave megabytes of it.

### `progic edit-format`

Prints the full edit file reference. The edit file can set:

- `meter`: one signature (`"4/4"`) or a list of changes by bar
- `tempo`: one BPM or a list of changes by bar and beat
- `end`: the project end marker, where exporting stops
- `markers`: the complete list of markers (replaces existing ones)
- `tracks`: per track, a new `name`, `volume` and `pan` (0-127), plugin `params` (for plugins built with JUCE), `remove` a plugin, replace it with a copy of another track's (`replace_with`), or `add` a copy of another track's effect or MIDI effect, `clear_regions`, new MIDI `regions`, new `audio_regions` playing WAV files, which are copied into the project, and volume and pan `automation`, ramping between points; an entry with `"add": "instrument"` instead of `"track"` adds a software instrument track

### `progic skill [--install [DIR]]`

Prints the agent skill for using progic, or installs it as a Claude Code skill (default `~/.claude/skills/progic/SKILL.md`).

## Units

- Bars and beats are 1-based. A beat is one unit of the time signature denominator (a quarter note in 4/4, an eighth note in 6/8). Fractions are allowed.
- Note `start` and `length` are in quarter notes from the start of their region, independent of the time signature.
- Pitches are MIDI numbers or names with C4 = 60 (middle C). Logic displays MIDI note 60 as C3, so names in Logic's piano roll read one octave lower.
- Volume is Logic's 0-127 fader value (90 is the 0 dB default), or in dB for automation, on the fader's curve of 40 dB per decade of value. Pan is 0-127 with 64 at center.

## Templates

Progic doesn't load instruments or plugins, so a template should contain a track for every sound a song needs, with its instrument and plugins loaded. Unused tracks can be left empty. Templates are kept without their undo history (`progic strip-undo`).

Progic can add software instrument tracks, but they come with an empty channel strip: no instrument or plugins. Logic can't load a mixer channel created from scratch, so an added track uses one of the spare instrument channel strips Logic pre-allocates in a new project. Adding tracks needs a template that:

- has at least 3 software instrument tracks,
- has a software instrument track as the last track created, and
- still has spare instrument strips. Logic leaves about 60 in some projects and none in others, so check with `inspect`.

`progic inspect` reports whether a project meets these (`can_add_tracks`).

To be able to write markers, the template needs a marker track: in Logic, add a marker, delete it, and save. `progic inspect` reports this as `has_marker_track`.

New MIDI regions are cloned from a region already in the project when it has one, and otherwise from a bundled prototype region.

New audio regions are cloned from an audio region already in the project, so a template that will get audio needs one: in Logic, put any audio file on an audio track and save. They play PCM WAV files at the project's sample rate. Anything else, such as AAC or ALAC in a `.caf`, is converted first, e.g., with `afconvert -f WAVE -d LEI24@48000 in.caf out.wav` on a Mac. Progic adds the waveform overview chunk Logic would otherwise add to a WAV when it imports it, since Logic checks a file's size against the size the project stores.

## Installation

Progic needs Python 3.9 or later and has no dependencies.

``` sh
uv tool install git+<repository-url>   # or `uv tool install .` from a checkout
progic skill --install
```

`pipx install` and `pip3 install` work the same way. For development, run it from a checkout with `PYTHONPATH=src python3 -m progic`.

## How It Works

A `.logicx` project is a folder. The project itself is the binary file `Alternatives/000/ProjectData`: a header followed by a flat list of tagged records (tracks, channels, mixer strips, plugin slots, event sequences, regions). Records have no absolute offsets between them, so progic parses the file into records, edits, inserts, or removes records, and writes them back. Parsing and serializing an unmodified file reproduces it byte-for-byte.

The reverse-engineered format, and much of the record-level code, comes from [LogicProFormatWriter](https://github.com/jonkubis/LogicProFormatWriter) (MIT, see `NOTICE`). Its `PROJECTDATA_FORMAT.md` documents the records; comments in progic refer to its sections (e.g. "§16").

The format is undocumented and changes between Logic releases. Most test fixtures were saved by Logic Pro 11.2.2; the `L13_*` fixtures were saved by Logic Pro 13.0, which changed the layout of channel strips and track rows. Adding tracks is implemented for the Logic 11 layout only.

## Layout

- `src/progic/container.py`: `ProjectData` record parsing and serialization
- `src/progic/bundle.py`: the `.logicx` folder, plists, and copying
- `src/progic/timeline.py`: tempo, meter, markers, and bar/tick conversion
- `src/progic/tracks.py`: tracks, channel strips, plugins, renaming, mixer
- `src/progic/plugins.py`: the state of plugins built with JUCE, their parameters, and copying a plugin
- `src/progic/addtrack.py`: adding software instrument tracks
- `src/progic/regions.py`: MIDI regions and notes
- `src/progic/audio.py`: audio regions and the audio files they play
- `src/progic/automation.py`: volume and pan automation
- `src/progic/inspect.py`: the `inspect` summary
- `src/progic/undo.py`: Logic's undo history
- `src/progic/edits.py`: the edit file format and `apply`
- `src/progic/cli.py`: the command-line interface
- `src/progic/data/`: the bundled MIDI region prototype and agent skill
- `scripts/extract_prototype.py`: regenerates the bundled prototype from a Logic project
- `tests/`: unit tests; `tests/fixtures/` holds Logic-saved projects

## Development

``` sh
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

Tests check that every fixture round-trips byte-for-byte, that encoders reproduce what Logic wrote where Logic saved an equivalent project, and that edits read back correctly. They don't open projects in Logic, so changes to how records are written should also be checked by opening the output in Logic.
