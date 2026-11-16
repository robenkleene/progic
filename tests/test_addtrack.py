from __future__ import annotations

import hashlib
import unittest

from progic import addtrack, edits, regions, tracks
from progic.bundle import Bundle
from progic.container import ProjectData, u16, u32
from progic.tracks import TrackError

from .util import fixture


def _load(name: str) -> ProjectData:
    return Bundle(fixture(name)).read()


class AddInstrumentTrackTest(unittest.TestCase):
    def test_matches_reference_implementation(self) -> None:
        # Hash of LogicProFormatWriter's `activate_instrument_track` run three
        # times on this fixture with `IdGen(1)`, `IdGen(2)`, `IdGen(3)`, which
        # draw ids in the same order as `seeded_random`.
        pd = _load("mixed_template_2inst")
        for seed in (1, 2, 3):
            addtrack.add_instrument_track(pd, rand=addtrack.seeded_random(seed))
        self.assertEqual(
            hashlib.sha256(pd.serialize()).hexdigest(),
            "8150e0cd65511609befe8c1b9ffdfe4fb4b53c94ee0754e8c889193a9021a9de",
        )

    def test_added_track_is_linked(self) -> None:
        pd = _load("mixed_template_2inst")
        free = addtrack.free_instrument_strips(pd)
        added = addtrack.add_instrument_track(pd, "Arp", rand=addtrack.seeded_random(7))
        pd = ProjectData.parse(pd.serialize())
        all_tracks = tracks.tracks(pd)
        self.assertEqual([t.name for t in all_tracks][-1], "Arp")
        new = all_tracks[-1]
        self.assertEqual(new.number, 5)
        self.assertEqual(new.kind, "instrument")
        self.assertEqual(new.channel, added.channel)
        self.assertEqual(addtrack.free_instrument_strips(pd), free - 1)
        song = pd.song.raw
        self.assertEqual(u32(song, 0x80), new.channel)
        self.assertEqual(u32(song, 0xF4) >> 16, 5)
        # Logic draws only as many arrange rows as the container height allows.
        container = regions.arrange_container(pd)
        assert container is not None
        self.assertEqual(u16(container.raw, regions.arrange_height_offset(container)), 0x3C * 6)
        # Exactly one channel is flagged as the last.
        last = [r for r in pd.find(b"ivnE") if r.raw[0x74] == 1]
        self.assertEqual([r.idx for r in last], [new.channel])

    def test_regions_on_added_track(self) -> None:
        pd = _load("mixed_template_2inst")
        track = addtrack.add_instrument_track(pd, "Arp", rand=addtrack.seeded_random(1))
        notes = [regions.Note(0, 57, 100, 480)]
        regions.add_midi_region(pd, track, tick=3840, length=3840, notes=notes)
        pd = ProjectData.parse(pd.serialize())
        found = [r for r in regions.midi_regions(pd) if r.track == track.number]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].notes, notes)

    def test_refuses_unsuitable_templates(self) -> None:
        # Fewer than 3 instrument tracks.
        pd = _load("mixed_template")
        self.assertIn("at least 3 instrument tracks", addtrack.can_add_instrument(pd) or "")
        # The newest channel is an audio track.
        pd = _load("F23_av")
        self.assertIn("most recently created", addtrack.can_add_instrument(pd) or "")
        with self.assertRaises(TrackError):
            addtrack.add_instrument_track(pd)
        # Logic 13 projects.
        pd = _load("L13_template")
        self.assertIn("Logic 13", addtrack.can_add_instrument(pd) or "")


class AddTrackEditTest(unittest.TestCase):
    def test_add_in_edit_file(self) -> None:
        pd = _load("mixed_template_2inst")
        summary = edits.apply(
            pd,
            {
                "tracks": [
                    {"track": "Inst 1", "name": "Keys"},
                    {"add": "instrument", "name": "Arp", "volume": 80, "regions": [{"bar": 2, "notes": []}]},
                    {"add": "instrument", "name": "Sub"},
                ]
            },
        )
        self.assertEqual([t["track"] for t in summary["tracks"]], [1, 5, 6])
        pd = ProjectData.parse(pd.serialize())
        all_tracks = tracks.tracks(pd)
        self.assertEqual([t.name for t in all_tracks], ["Keys", "Audio 1", "Inst 2", "Inst 3", "Arp", "Sub"])
        self.assertEqual(all_tracks[4].volume, 80)
        self.assertEqual([r.track for r in regions.midi_regions(pd)], [5])

    def test_add_errors(self) -> None:
        cases = [
            ({"add": "instrument"}, "needs a 'name'"),
            ({"add": "audio", "name": "X"}, "must be \"instrument\""),
            ({"add": "instrument", "track": 1, "name": "X"}, "exactly one of"),
            ({"add": "instrument", "name": "X", "clear_regions": True}, "no regions to clear"),
        ]
        for edit, message in cases:
            with self.subTest(edit=edit):
                with self.assertRaises(edits.EditError) as cm:
                    edits.apply(_load("mixed_template_2inst"), {"tracks": [edit]})
                self.assertIn(message, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
