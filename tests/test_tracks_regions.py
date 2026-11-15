from __future__ import annotations

import unittest

from progic import regions as R
from progic import tracks as T
from progic.bundle import Bundle
from progic.container import ProjectData

from .util import fixture


def load(name: str) -> ProjectData:
    return Bundle(fixture(name)).read()


def reparse(pd: ProjectData) -> ProjectData:
    return ProjectData.parse(pd.serialize())


class TrackTest(unittest.TestCase):
    def test_lists_tracks_with_kinds(self) -> None:
        got = [(t.number, t.name, t.kind) for t in T.tracks(load("mixed_template_2inst"))]
        self.assertEqual(
            got,
            [(1, "Inst 1", "instrument"), (2, "Audio 1", "audio"), (3, "Inst 2", "instrument"), (4, "Inst 3", "instrument")],
        )

    def test_reads_name_logic_wrote(self) -> None:
        self.assertEqual(T.tracks(load("F7_trackname"))[0].name, "ZZTRACKZZ")

    def test_rename_matches_logic(self) -> None:
        pd = load("F0_baseline")
        T.rename(T.find_track(pd, 1), "ZZTRACKZZ")
        logic = load("F7_trackname")
        self.assertEqual(T.find_track(reparse(pd), 1).ivne.raw, T.find_track(logic, 1).ivne.raw)

    def test_rename_odd_and_even_lengths(self) -> None:
        for name in ("Lead", "Pluck", "Super Saw Lead Layer"):
            with self.subTest(name):
                pd = load("mixed_template_2inst")
                T.rename(T.find_track(pd, "Inst 2"), name)
                pd = reparse(pd)
                self.assertEqual(T.find_track(pd, 3).name, name)
                # The UUID link to the channel strip survives the resize.
                self.assertIsNotNone(T.find_track(pd, 3).strip)

    def test_find_track_errors(self) -> None:
        pd = load("mixed_template_2inst")
        with self.assertRaises(T.TrackError):
            T.find_track(pd, "Nope")
        with self.assertRaises(T.TrackError):
            T.find_track(pd, 9)

    def test_set_mixer(self) -> None:
        pd = load("mixed_template_2inst")
        T.set_mixer(T.find_track(pd, 2), volume=100, pan=20)
        t = T.find_track(reparse(pd), 2)
        self.assertEqual((t.volume, t.pan), (100, 20))
        with self.assertRaises(T.TrackError):
            T.set_mixer(t, volume=128)


class RegionTest(unittest.TestCase):
    def test_reads_notes_logic_wrote(self) -> None:
        (region,) = R.midi_regions(load("F4b_midinotes"))
        self.assertEqual((region.track, region.tick, region.length, region.name), (1, 0, 3840, "Inst 1"))
        self.assertEqual(
            region.notes,
            [R.Note(0, 36, 32, 240), R.Note(960, 60, 64, 480), R.Note(1920, 84, 100, 960)],
        )

    def test_note_encoding_matches_logic(self) -> None:
        pd = load("F4b_midinotes")
        (region,) = R.midi_regions(pd)
        seq = pd.first(b"qSvE", region.idx)
        assert seq is not None
        # Logic stores a fine-velocity byte at +0x0a of each note; the encoder
        # writes 0 there (an exact integer velocity), so mask it out.
        def mask(b: bytes) -> bytes:
            out = bytearray(b)
            for i in range(0, len(out) - 16, 32):
                out[i + 0x0A] = 0
            return bytes(out)

        self.assertEqual(mask(R.encode_notes(region.notes)), mask(seq.payload))

    def test_add_region_round_trips(self) -> None:
        pd = load("mixed_template_2inst")
        notes = [R.Note(0, 60, 100, 480), R.Note(480, 64, 90, 480)]
        R.add_midi_region(pd, T.find_track(pd, "Inst 2"), tick=3840 * 4, length=3840 * 2, notes=notes, name="Lead A")
        R.add_midi_region(pd, T.find_track(pd, "Inst 1"), tick=0, length=3840, notes=[R.Note(0, 36, 127, 240)])
        got = R.midi_regions(reparse(pd))
        self.assertEqual(
            [(r.track, r.tick, r.length, r.name, r.notes) for r in got],
            [(1, 0, 3840, "Inst 1", [R.Note(0, 36, 127, 240)]), (3, 15360, 7680, "Lead A", notes)],
        )
        self.assertEqual(len({r.idx for r in got}), 2)

    def test_add_region_uses_project_prototype_when_present(self) -> None:
        pd = load("F22_multimidi")
        R.add_midi_region(pd, T.find_track(pd, 2), tick=3840, length=3840, notes=[R.Note(0, 60, 100, 960)])
        self.assertEqual(len(R.midi_regions(reparse(pd))), 5)

    def test_bar_counter_independent_of_prototype_position(self) -> None:
        def counters(pd: ProjectData) -> dict[int, int]:
            events = R._arrange_events(pd)  # pyright: ignore[reportPrivateUsage]
            assert events is not None
            body = events.payload
            return {R.u32(body, o + 4) - R.REGION_ORIGIN: R.u32(body, o + 0x30) for o in R._placements(body, 0x20)}  # pyright: ignore[reportPrivateUsage]

        pd = load("mixed_template_2inst")
        track = T.find_track(pd, 1)
        # The first add clones the bundled prototype; the second clones the
        # region at bar 5 that the first add created.
        R.add_midi_region(pd, track, tick=3840 * 4, length=3840, notes=[])
        R.add_midi_region(pd, track, tick=0, length=3840, notes=[])
        got = counters(pd)
        self.assertEqual(got[3840 * 4] - got[0], 4 * 0x100)
        self.assertEqual(got[0], R.u32(R.bundled_prototype().event, 0x30))

    def test_rejects_audio_track(self) -> None:
        pd = load("mixed_template_2inst")
        with self.assertRaises(R.RegionError):
            R.add_midi_region(pd, T.find_track(pd, "Audio 1"), tick=0, length=3840, notes=[])

    def test_remove_regions(self) -> None:
        pd = load("F22_multimidi")
        before = len(pd.records)
        self.assertEqual(R.remove_midi_regions(pd, T.find_track(pd, 2)), 1)
        pd = reparse(pd)
        self.assertEqual([r.track for r in R.midi_regions(pd)], [1, 3, 4])
        self.assertEqual(len(pd.records), before - 5)


if __name__ == "__main__":
    unittest.main()
