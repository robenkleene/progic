"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__, audio, edits, inspect, plugins, timeline, tracks, undo
from .bundle import Bundle, BundleError
from .container import ProjectData

DESCRIPTION = """\
Create and edit Logic Pro projects (.logicx) without Logic.

Typical workflow:
  progic inspect Template.logicx            # see tracks, plugins, tempo, markers
  progic edit-format                        # the JSON edit file reference
  progic apply Template.logicx edits.json -o Song.logicx
  progic inspect Song.logicx --notes        # check the result

Projects are edited starting from a template made in Logic: instruments
and plugins come from the template's tracks, and progic writes tempo, meter,
markers, track names, mixer settings, and MIDI regions. It can also add
empty software instrument tracks to a suitable template.
"""

INSPECT_HELP = """\
Print a project summary as JSON (or text with --text): tempo map, meter
map, markers, and each track with its number, name, kind (instrument,
audio, aux, ...), volume, pan, plugins, and MIDI regions. Positions are
1-based bars and beats. Use --notes to include every note, and --params
to include the parameters of plugins built with JUCE ("juce": true), which
`apply` can set. It also reports whether tracks can be added to the project
(can_add_tracks)."""

PLUGIN_STATE_HELP = """\
Print the state of a plugin built with JUCE (`inspect` marks them
"juce": true), the XML its getStateInformation saves, as Logic restores it,
e.g., to render the plugin offline with the same settings."""

APPLY_HELP = """\
Apply a JSON edit file to a project. See `progic edit-format` for the
format. Either write to a new project with -o (the input is copied first),
edit the input with --in-place, or only validate with --dry-run. All edits
are validated before anything is written; on error nothing changes. Prints
a JSON summary of the changes."""

SKILL_HELP = """\
Print the agent skill (instructions for an LLM agent using progic), or
install it with --install as a Claude Code skill. The default install
location is ~/.claude/skills/progic/SKILL.md."""

NEW_HELP = """\
Copy a template project to a new path (without Logic's backup folders)."""

STRIP_UNDO_HELP = """\
Remove the undo history Logic saves in a project, e.g., from a template,
whose history is only how it was made. Loading patches can leave megabytes
of it. Write to a new project with -o, or edit the input with --in-place."""


class CliError(Exception):
    pass


def _load(path: str) -> tuple[Bundle, ProjectData]:
    bundle = Bundle(path)
    return bundle, bundle.read()


def _read_edits(path: str) -> Any:
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text()
    except OSError as e:
        raise CliError(f"cannot read edit file: {e}") from e
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise CliError(f"edit file is not valid JSON: {e}") from e


def _sync_metadata(bundle: Bundle, pd: ProjectData) -> None:
    meter = timeline.meter_map(pd)
    values: dict[str, Any] = {"BeatsPerMinute": timeline.initial_bpm(pd)}
    files = audio.audio_file_names(pd)
    if files or "AudioFiles" in bundle.metadata():
        values["AudioFiles"] = [f"{audio.AUDIO_DIR}/{name}" for name in files]
    if meter:
        values["SongSignatureNumerator"] = meter[0].numerator
        values["SongSignatureDenominator"] = meter[0].denominator
    bundle.update_metadata(**values)


def cmd_inspect(args: argparse.Namespace) -> int:
    _, pd = _load(args.project)
    summary = inspect.inspect(pd, notes=args.notes, params=args.params)
    print(inspect.text(summary) if args.text else json.dumps(summary, indent=2))
    return 0


def cmd_plugin_state(args: argparse.Namespace) -> int:
    _, pd = _load(args.project)
    try:
        plugin = tracks.find_plugin(tracks.find_track(pd, args.track), args.plugin)
    except tracks.TrackError as e:
        raise CliError(str(e)) from e
    xml = None if plugin.record is None else plugins.state_xml(plugin.record)
    if xml is None:
        raise CliError(f"{plugin.name or 'the plugin'} isn't a JUCE plugin with its state saved")
    if args.output:
        Path(args.output).write_text(xml)
    else:
        print(xml)
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    Bundle.copy(args.template, args.output, overwrite=args.force)
    print(json.dumps({"created": str(args.output)}))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    if args.output and args.in_place:
        raise CliError("pass only one of -o OUTPUT or --in-place")
    if not (args.output or args.in_place or args.dry_run):
        raise CliError("pass -o OUTPUT, --in-place, or --dry-run")
    edit_doc = _read_edits(args.edits)
    src, pd = _load(args.project)
    media_dir = src.path / "Media" / audio.AUDIO_DIR
    media = edits.Media(
        base=Path.cwd() if args.edits == "-" else Path(args.edits).resolve().parent,
        taken={p.name for p in media_dir.iterdir()} if media_dir.is_dir() else set(),
    )
    summary = edits.apply(pd, edit_doc, media)
    # Parse what we're about to write as a last structural check.
    data = pd.serialize()
    ProjectData.parse(data)
    if args.dry_run:
        print(json.dumps({"dry_run": True, "changes": summary}, indent=2))
        return 0
    dest = Bundle.copy(src.path, args.output, overwrite=args.force) if args.output else src
    dest.project_data_path.write_bytes(data)
    if media.files:
        (dest.path / "Media" / audio.AUDIO_DIR).mkdir(parents=True, exist_ok=True)
        for f in media.files.values():
            (dest.path / "Media" / audio.AUDIO_DIR / f.name).write_bytes(f.data)
    _sync_metadata(dest, pd)
    print(json.dumps({"project": str(dest.path), "changes": summary}, indent=2))
    return 0


def cmd_strip_undo(args: argparse.Namespace) -> int:
    if bool(args.output) == bool(args.in_place):
        raise CliError("pass one of -o OUTPUT or --in-place")
    src, pd = _load(args.project)
    steps = undo.strip_undo(pd)
    data = pd.serialize()
    ProjectData.parse(data)
    dest = Bundle.copy(src.path, args.output, overwrite=args.force) if args.output else src
    dest.project_data_path.write_bytes(data)
    print(json.dumps({"project": str(dest.path), "undo_steps_removed": steps}))
    return 0


def cmd_edit_format(_: argparse.Namespace) -> int:
    print(edits.EDIT_FORMAT, end="")
    return 0


def cmd_skill(args: argparse.Namespace) -> int:
    text = resources.files("progic").joinpath("data/SKILL.md").read_text()
    if args.install is None:
        print(text, end="")
        return 0
    dest = Path(args.install).expanduser() / "SKILL.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    print(json.dumps({"installed": str(dest)}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="progic", description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--version", action="version", version=f"progic {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    raw = argparse.RawDescriptionHelpFormatter

    p = sub.add_parser("inspect", help="summarize a project as JSON", description=INSPECT_HELP, formatter_class=raw)
    p.add_argument("project", help="path to a .logicx project")
    p.add_argument("--notes", action="store_true", help="include every note of every region")
    p.add_argument("--params", action="store_true", help="include the parameters of plugins built with JUCE")
    p.add_argument("--text", action="store_true", help="human-readable text instead of JSON")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("apply", help="apply a JSON edit file", description=APPLY_HELP, formatter_class=raw)
    p.add_argument("project", help="path to a .logicx project")
    p.add_argument("edits", help="path to a JSON edit file, or - for stdin")
    p.add_argument("-o", "--output", help="write the result to this new .logicx path")
    p.add_argument("--in-place", action="store_true", help="modify the input project")
    p.add_argument("--force", action="store_true", help="overwrite OUTPUT if it exists")
    p.add_argument("--dry-run", action="store_true", help="validate and print the changes without writing")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("new", help="copy a template to a new project", description=NEW_HELP, formatter_class=raw)
    p.add_argument("template", help="path to a template .logicx project")
    p.add_argument("output", help="path of the new .logicx project")
    p.add_argument("--force", action="store_true", help="overwrite OUTPUT if it exists")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser(
        "strip-undo", help="remove a project's undo history", description=STRIP_UNDO_HELP, formatter_class=raw
    )
    p.add_argument("project", help="path to a .logicx project")
    p.add_argument("-o", "--output", help="write the result to this new .logicx path")
    p.add_argument("--in-place", action="store_true", help="modify the input project")
    p.add_argument("--force", action="store_true", help="overwrite OUTPUT if it exists")
    p.set_defaults(func=cmd_strip_undo)

    p = sub.add_parser(
        "plugin-state", help="print a JUCE plugin's state XML", description=PLUGIN_STATE_HELP, formatter_class=raw
    )
    p.add_argument("project", help="path to a .logicx project")
    p.add_argument("track", help="the track's name or 1-based number")
    p.add_argument("plugin", help="the plugin's name or slot number")
    p.add_argument("-o", "--output", help="write the XML to OUTPUT instead of printing it")
    p.set_defaults(func=cmd_plugin_state)

    p = sub.add_parser("edit-format", help="print the JSON edit file reference")
    p.set_defaults(func=cmd_edit_format)

    p = sub.add_parser("skill", help="print or install the agent skill", description=SKILL_HELP, formatter_class=raw)
    p.add_argument(
        "--install",
        nargs="?",
        const="~/.claude/skills/progic",
        metavar="DIR",
        help="write SKILL.md into DIR (default ~/.claude/skills/progic)",
    )
    p.set_defaults(func=cmd_skill)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CliError, BundleError, edits.EditError, ValueError) as e:
        print(f"progic: error: {e}", file=sys.stderr)
        return 1
