from __future__ import annotations

import unittest

from progic import timeline as t
from progic.bundle import Bundle
from progic.container import ProjectData

from .util import fixture


def load(name: str) -> ProjectData:
    return Bundle(fixture(name)).read()


class TempoTest(unittest.TestCase):
    def test_reads_tempo_map(self) -> None:
        self.assertEqual(
            t.tempo_map(load("F9_tempometer")),
            [t.TempoPoint(0, 120.0), t.TempoPoint(3840, 132.0), t.TempoPoint(7680, 96.0), t.TempoPoint(15360, 150.0)],
        )

    def test_rewriting_logic_tempo_map_is_byte_identical(self) -> None:
        pd = load("F9_tempometer")
        before = pd.serialize()
        t.set_tempo_map(pd, t.tempo_map(pd))
        self.assertEqual(pd.serialize(), before)

    def test_rewriting_tempo_map_preserves_values(self) -> None:
        # F2 differs from the encoder only in per-event flag bytes that vary
        # between Logic saves, so compare values rather than bytes.
        pd = load("F2_tempomap")
        points = t.tempo_map(pd)
        t.set_tempo_map(pd, points)
        self.assertEqual(t.tempo_map(ProjectData.parse(pd.serialize())), points)

    def test_set_tempo_updates_initial_bpm(self) -> None:
        pd = load("F0_baseline")
        t.set_tempo_map(pd, [t.TempoPoint(0, 138.0), t.TempoPoint(3840 * 16, 140.0)])
        pd = ProjectData.parse(pd.serialize())
        self.assertEqual(t.initial_bpm(pd), 138.0)
        self.assertEqual(t.tempo_map(pd), [t.TempoPoint(0, 138.0), t.TempoPoint(61440, 140.0)])

    def test_matches_logic_single_tempo_change(self) -> None:
        pd = load("F0_baseline")
        t.set_tempo_map(pd, [t.TempoPoint(0, 137.5)])
        self.assertEqual(t.initial_bpm(pd), t.initial_bpm(load("F1_tempo")))


class MeterTest(unittest.TestCase):
    def test_rewriting_logic_meter_map_is_byte_identical(self) -> None:
        pd = load("F10_sigs")
        before = pd.serialize()
        t.set_meter_map(pd, t.meter_map(pd))
        self.assertEqual(pd.serialize(), before)

    def test_rewriting_meter_map_preserves_values(self) -> None:
        pd = load("F9_tempometer")
        points = t.meter_map(pd)
        t.set_meter_map(pd, points)
        self.assertEqual(t.meter_map(ProjectData.parse(pd.serialize())), points)

    def test_rejects_bad_denominator(self) -> None:
        with self.assertRaises(t.TimelineError):
            t.set_meter_map(load("F0_baseline"), [t.MeterPoint(0, 4, 3)])


class MarkerTest(unittest.TestCase):
    def test_reads_markers(self) -> None:
        self.assertEqual(
            t.markers(load("F11_markers")),
            [t.Marker(3840, "MARKERONE"), t.Marker(11520, "MARKERTWO"), t.Marker(19200, "MARKERTHREE")],
        )

    def test_writing_logic_markers_onto_empty_track_matches_logic(self) -> None:
        logic = load("F11_markers")
        pd = load("F13_settled_or_not")
        t.set_markers(pd, t.markers(logic))
        self.assertEqual(t.markers(ProjectData.parse(pd.serialize())), t.markers(logic))

    def test_project_without_marker_track_is_detected(self) -> None:
        pd = load("F0_baseline")
        self.assertFalse(t.has_marker_track(pd))
        with self.assertRaises(t.TimelineError):
            t.set_markers(pd, [t.Marker(0, "Intro")])

    def test_escapes_rtf_characters(self) -> None:
        pd = load("F13_settled_or_not")
        t.set_markers(pd, [t.Marker(0, "Drop {B}")])
        self.assertEqual(t.markers(ProjectData.parse(pd.serialize()))[0].tick, 0)


class TimeMapTest(unittest.TestCase):
    def test_four_four(self) -> None:
        tm = t.TimeMap([t.MeterPoint(0, 4, 4)])
        self.assertEqual(tm.to_tick(1), 0)
        self.assertEqual(tm.to_tick(17), 16 * 3840)
        self.assertEqual(tm.to_tick(2, 3), 3840 + 2 * 960)
        self.assertEqual(tm.to_bar_beat(3840 + 960), (2, 2.0))

    def test_meter_changes(self) -> None:
        # Bar 1 is 4/4, bar 2 is 3/4 (2880 ticks), then 7/8 (3360 ticks) from bar 3.
        tm = t.TimeMap(t.meter_map(load("F9_tempometer")))
        self.assertEqual(tm.to_tick(2), 3840)
        self.assertEqual(tm.to_tick(3), 3840 + 2880)
        self.assertEqual(tm.to_tick(5), 3840 + 2880 + 2 * 3360)
        self.assertEqual(tm.to_tick(3, 2), 3840 + 2880 + 480)
        self.assertEqual(tm.bar_ticks(4), 3360)
        self.assertEqual(tm.to_bar_beat(3840 + 2880 + 3360), (4, 1.0))


if __name__ == "__main__":
    unittest.main()
