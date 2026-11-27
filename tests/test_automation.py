from __future__ import annotations

import unittest

from progic import automation as AU
from progic import edits as E
from progic import tracks as T
from progic.bundle import Bundle
from progic.container import ProjectData, u32

from .util import fixture


def load(name: str) -> ProjectData:
    return Bundle(fixture(name)).read()


def reparse(pd: ProjectData) -> ProjectData:
    return ProjectData.parse(pd.serialize())


def rounded(lanes: dict[int, list[AU.Point]]) -> dict[int, list[tuple[int, float]]]:
    return {k: [(p.tick, round(p.value, 2)) for p in v] for k, v in lanes.items()}


class AutomationTest(unittest.TestCase):
    def test_reads_points_logic_wrote(self) -> None:
        # Logic 13, with volume 0, -20, and 0 dB on track 1, and pan on track 2
        pd = load("L13_automation")
        self.assertEqual(rounded(AU.lanes(pd, T.find_track(pd, 1))), {7: [(0, 90.0), (15440, 27.81), (30800, 90.0)]})
        self.assertEqual(rounded(AU.lanes(pd, T.find_track(pd, 2))), {10: [(0, 63.11), (15360, 126.37)]})
        self.assertEqual(AU.lanes(pd, T.find_track(pd, 3)), {})

    def test_writes_ramps_as_logic_does(self) -> None:
        # The same points give Logic's events, interpolated points included, to
        # the last bit of their values
        logic = load("L13_automation")
        pd = load("L13_template")
        for n in (1, 2):
            with self.subTest(track=n):
                AU.set_lanes(pd, T.find_track(pd, n), AU.lanes(logic, T.find_track(logic, n)))
                ours = AU._container(reparse(pd), T.find_track(pd, n))[1].payload
                theirs = AU._container(logic, T.find_track(logic, n))[1].payload
                self.assertEqual(len(ours), len(theirs))
                for o in range(0, len(ours) - 16, 16):
                    self.assertEqual(ours[o], theirs[o])
                    self.assertEqual(ours[o + 4:o + 8], theirs[o + 4:o + 8])
                    # Logic's own steps drift a little from a straight ramp,
                    # under a hundredth of a step
                    self.assertLessEqual(abs(u32(ours, o + 8) - u32(theirs, o + 8)), 2**24 // 100)
                    self.assertEqual(ours[o + 12], theirs[o + 12])
                    # Logic marks the point that was selected
                    self.assertEqual(ours[o + 15], theirs[o + 15] & 0x7F)

    def test_db(self) -> None:
        self.assertEqual(AU.db_to_value(0), 90)
        self.assertAlmostEqual(AU.db_to_value(-20), 28.46, places=2)
        self.assertEqual(AU.db_to_value(12), 127)
        self.assertAlmostEqual(AU.value_to_db(127), 5.98, places=2)

    def test_keeps_other_params(self) -> None:
        pd = load("L13_automation")
        track = T.find_track(pd, 1)
        AU.set_lanes(pd, track, {10: [AU.Point(0, 0)]})
        self.assertEqual(sorted(AU.lanes(reparse(pd), track)), [7, 10])


class EditTest(unittest.TestCase):
    def test_edit(self) -> None:
        pd = load("L13_template")
        E.apply(
            pd,
            {"tracks": [{"track": 3, "automation": [
                {"param": "volume", "points": [{"bar": 1, "db": -60}, {"bar": 3, "db": 0}]},
                {"param": "pan", "points": [{"bar": 2, "beat": 3, "value": 20}]},
            ]}]},
        )
        got = rounded(AU.lanes(reparse(pd), T.find_track(pd, 3)))
        self.assertEqual(got, {7: [(0, 2.85), (7680, 90.0)], 10: [(5760, 20.0)]})

    def test_errors(self) -> None:
        pd = load("L13_template")
        for lane in (
            {"param": "tempo", "points": [{"bar": 1, "value": 1}]},
            {"param": "pan", "points": [{"bar": 1, "db": 0}]},
            {"param": "volume", "points": [{"bar": 1, "value": 200}]},
            {"param": "volume", "points": []},
        ):
            with self.subTest(lane=lane), self.assertRaisesRegex(E.EditError, r"tracks\[0\]\.automation\[0\]"):
                E.apply(pd, {"tracks": [{"track": 1, "automation": [lane]}]})


if __name__ == "__main__":
    unittest.main()
