from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from progic import edits, timeline
from progic.bundle import Bundle
from progic.cli import main
from progic.container import ProjectData
from progic.inspect import inspect

from .util import fixture

EDITS: dict[str, Any] = {
    "meter": "4/4",
    "tempo": [{"bar": 1, "bpm": 138}, {"bar": 65, "bpm": 140}],
    "markers": [{"bar": 1, "name": "Intro"}, {"bar": 33, "name": "Drop"}],
    "tracks": [
        {
            "track": "Inst 2",
            "name": "Lead",
            "volume": 100,
            "regions": [
                {
                    "bar": 33,
                    "length_bars": 4,
                    "notes": [
                        {"start": 0, "length": 0.5, "pitch": "A4", "velocity": 100},
                        {"start": 0.5, "length": 0.5, "pitch": 72},
                    ],
                }
            ],
        },
        {"track": 1, "name": "Bass", "pan": 40},
    ],
}


def load(name: str) -> ProjectData:
    return Bundle(fixture(name)).read()


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class PitchTest(unittest.TestCase):
    def test_names(self) -> None:
        self.assertEqual(edits.parse_pitch("C4"), 60)
        self.assertEqual(edits.parse_pitch("A4"), 69)
        self.assertEqual(edits.parse_pitch("F#3"), 54)
        self.assertEqual(edits.parse_pitch("Bb2"), 46)
        self.assertEqual(edits.parse_pitch("C-1"), 0)
        self.assertEqual(edits.parse_pitch(64), 64)

    def test_invalid(self) -> None:
        for bad in ("H2", "C", 128, -1, True, 1.5):
            with self.subTest(bad), self.assertRaises(edits.EditError):
                edits.parse_pitch(bad)


class ApplyTest(unittest.TestCase):
    def test_apply_then_inspect(self) -> None:
        pd = load("mixed_template_2inst")
        edits.apply(pd, EDITS)
        summary = inspect(ProjectData.parse(pd.serialize()), notes=True)
        self.assertEqual(summary["tempo"], [{"bar": 1, "beat": 1, "bpm": 138}, {"bar": 65, "beat": 1, "bpm": 140}])
        self.assertEqual([m["name"] for m in summary["markers"]], ["Intro", "Drop"])
        t1, _, t3, _ = summary["tracks"]
        self.assertEqual((t1["name"], t1["pan"]), ("Bass", 40))
        self.assertEqual((t3["name"], t3["volume"]), ("Lead", 100))
        (region,) = t3["regions"]
        self.assertEqual((region["name"], region["bar"], region["length_bars"]), ("Lead", 33, 4))
        self.assertEqual(
            region["notes"],
            [
                {"start": 0, "length": 0.5, "pitch": 69, "velocity": 100},
                {"start": 0.5, "length": 0.5, "pitch": 72, "velocity": 100},
            ],
        )

    def test_default_region_length_rounds_up_to_bars(self) -> None:
        pd = load("mixed_template_2inst")
        doc = {"tracks": [{"track": 1, "regions": [{"bar": 2, "notes": [{"start": 4.5, "length": 1, "pitch": 40}]}]}]}
        edits.apply(pd, doc)
        (region,) = inspect(pd)["tracks"][0]["regions"]
        self.assertEqual(region["length_bars"], 2)

    def test_meter_changes_shift_bar_positions(self) -> None:
        pd = load("mixed_template_2inst")
        doc = {
            "meter": [{"bar": 1, "signature": "4/4"}, {"bar": 3, "signature": "3/4"}],
            "tracks": [{"track": 1, "regions": [{"bar": 4, "length_bars": 1, "notes": []}]}],
        }
        edits.apply(pd, doc)
        summary = inspect(ProjectData.parse(pd.serialize()))
        self.assertEqual(summary["meter"][1], {"bar": 3, "beat": 1, "signature": "3/4"})
        self.assertEqual(summary["tracks"][0]["regions"][0]["bar"], 4)

    def test_project_end(self) -> None:
        # A new project ends at bar 129
        self.assertEqual(inspect(load("L13_abstract"))["end"], {"bar": 129, "beat": 1})
        pd = load("L13_abstract")
        edits.apply(pd, {"meter": "3/4", "end": {"bar": 9}})
        self.assertEqual(inspect(ProjectData.parse(pd.serialize()))["end"], {"bar": 9, "beat": 1})
        self.assertEqual(timeline.project_end(pd), 8 * 3 * 960)

    def test_clear_regions(self) -> None:
        pd = load("F22_multimidi")
        edits.apply(pd, {"tracks": [{"track": 2, "clear_regions": True}]})
        self.assertEqual(inspect(pd)["tracks"][1]["regions"], [])

    def test_errors_name_the_location(self) -> None:
        cases: list[tuple[Any, str]] = [
            ({"tempo": "fast"}, "tempo"),
            ({"bogus": 1}, "unknown top-level keys: bogus"),
            ({"tracks": [{"track": "Nope"}]}, "tracks[0]: no track named 'Nope'"),
            ({"tracks": [{"track": 2, "regions": [{"bar": 1, "notes": []}]}]}, "tracks[0].regions[0]: track 'Audio 1'"),
            ({"tracks": [{"track": 1, "regions": [{"bar": 0}]}]}, "tracks[0].regions[0]: bar and beat are 1-based"),
            ({"meter": "4/3"}, "meter: time signature denominator 3"),
            ({"tracks": [{"track": 1, "colour": "red"}]}, "tracks[0]: unknown keys: colour"),
            ({"end": 9}, "end must be an object"),
            ({"end": {"bar": 1}}, "end: the project end must be after bar 1"),
        ]
        for doc, message in cases:
            with self.subTest(message), self.assertRaises(edits.EditError) as ctx:
                edits.apply(load("mixed_template_2inst"), doc)
            self.assertIn(message, str(ctx.exception))

    def test_markers_without_marker_track(self) -> None:
        with self.assertRaises(edits.EditError) as ctx:
            edits.apply(load("F0_baseline"), {"markers": [{"bar": 1, "name": "A"}]})
        self.assertIn("no marker track", str(ctx.exception))


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def test_apply_writes_new_project(self) -> None:
        edit_path = self.tmp / "edits.json"
        edit_path.write_text(json.dumps(EDITS))
        out = self.tmp / "Song.logicx"
        code, stdout, _ = run("apply", str(fixture("mixed_template_2inst")), str(edit_path), "-o", str(out))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["project"], str(out))
        self.assertEqual(Bundle(out).metadata()["BeatsPerMinute"], 138)
        code, stdout, _ = run("inspect", str(out))
        self.assertEqual(json.loads(stdout)["tracks"][2]["name"], "Lead")

    def test_apply_errors_leave_nothing_behind(self) -> None:
        edit_path = self.tmp / "edits.json"
        edit_path.write_text(json.dumps({"tracks": [{"track": "Nope"}]}))
        out = self.tmp / "Song.logicx"
        code, _, stderr = run("apply", str(fixture("mixed_template_2inst")), str(edit_path), "-o", str(out))
        self.assertEqual(code, 1)
        self.assertIn("no track named 'Nope'", stderr)
        self.assertFalse(out.exists())

    def test_apply_requires_output_choice(self) -> None:
        edit_path = self.tmp / "edits.json"
        edit_path.write_text("{}")
        code, _, stderr = run("apply", str(fixture("F0_baseline")), str(edit_path))
        self.assertEqual(code, 1)
        self.assertIn("-o OUTPUT, --in-place, or --dry-run", stderr)
        code, _, stderr = run("apply", str(fixture("F0_baseline")), str(edit_path), "-o", "x", "--in-place")
        self.assertEqual(code, 1)
        self.assertIn("only one of", stderr)

    def test_dry_run_needs_no_output(self) -> None:
        edit_path = self.tmp / "edits.json"
        edit_path.write_text(json.dumps({"tempo": 90}))
        code, stdout, _ = run("apply", str(fixture("F0_baseline")), str(edit_path), "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)["dry_run"])

    def test_dry_run_writes_nothing(self) -> None:
        project = self.tmp / "P.logicx"
        shutil.copytree(fixture("F0_baseline"), project)
        before = Bundle(project).project_data_path.read_bytes()
        edit_path = self.tmp / "edits.json"
        edit_path.write_text(json.dumps({"tempo": 90}))
        code, stdout, _ = run("apply", str(project), str(edit_path), "--in-place", "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)["dry_run"])
        self.assertEqual(Bundle(project).project_data_path.read_bytes(), before)

    def test_new_copies_template_without_backups(self) -> None:
        template = self.tmp / "T.logicx"
        shutil.copytree(fixture("F0_baseline"), template)
        (template / "Alternatives" / "000" / "Project File Backups").mkdir()
        code, _, _ = run("new", str(template), str(self.tmp / "New.logicx"))
        self.assertEqual(code, 0)
        self.assertFalse((self.tmp / "New.logicx" / "Alternatives" / "000" / "Project File Backups").exists())

    def test_inspect_text_and_edit_format(self) -> None:
        code, stdout, _ = run("inspect", str(fixture("F22_multimidi")), "--text")
        self.assertEqual(code, 0)
        self.assertIn("1. Inst 1 [instrument]", stdout)
        code, stdout, _ = run("edit-format")
        self.assertIn("EDIT FILE FORMAT", stdout)

    def test_skill_print_and_install(self) -> None:
        code, stdout, _ = run("skill")
        self.assertEqual(code, 0)
        self.assertTrue(stdout.startswith("---\nname: progic\n"))
        code, _, _ = run("skill", "--install", str(self.tmp / "skills" / "progic"))
        self.assertEqual(code, 0)
        self.assertEqual((self.tmp / "skills" / "progic" / "SKILL.md").read_text(), stdout)


if __name__ == "__main__":
    unittest.main()
