from __future__ import annotations

import unittest

from progic import edits, plugins
from progic import tracks as T
from progic.bundle import Bundle
from progic.container import REC_HEADER_SIZE, ProjectData, u16, u32

from .util import fixture


def load(name: str) -> ProjectData:
    return Bundle(fixture(name)).read()


def plugin(pd: ProjectData, track: int, name: str) -> T.Plugin:
    (found,) = [p for p in T.find_track(pd, track).plugins if p.name == name]
    return found


class PluginTest(unittest.TestCase):
    def test_reads_juce_params(self) -> None:
        pd = load("L13_abstract")
        mania = plugins.params(plugin(pd, 1, "Mania").record)  # type: ignore[arg-type]
        assert mania is not None
        self.assertEqual((len(mania), mania["track1_enabled"], mania["track1_note"]), (468, 1.0, 36.0))
        # Logic's own instruments have no JUCE state
        self.assertIsNone(plugins.params(plugin(pd, 2, "E-Piano").record))  # type: ignore[arg-type]

    def test_sets_params(self) -> None:
        pd = load("L13_abstract")
        before = plugins.params(plugin(pd, 1, "Mania").record)  # type: ignore[arg-type]
        tail = plugin(pd, 1, "Mania").record.raw[-20:]  # type: ignore[union-attr]
        edits.apply(pd, {"tracks": [{"track": 1, "plugins": [{"plugin": "Mania", "params": {"track2_enabled": 1, "swing": 30}}]}]})
        pd = ProjectData.parse(pd.serialize())
        record = plugin(pd, 1, "Mania").record
        assert record is not None and before is not None
        self.assertEqual(plugins.params(record), {**before, "track2_enabled": 1.0, "swing": 30.0})
        self.assertEqual(record.raw[-20:], tail)
        # The other plugins on the track are as they were
        self.assertEqual(len(plugins.params(plugin(pd, 1, "Depth").record) or {}), 227)  # type: ignore[arg-type]

    def test_by_slot(self) -> None:
        pd = load("L13_abstract")
        edits.apply(pd, {"tracks": [{"track": 2, "plugins": [{"plugin": 4, "params": {"key": 2}}]}]})
        self.assertEqual(plugins.params(plugin(pd, 2, "Purpose").record)["key"], 2.0)  # type: ignore[arg-type,index]

    def test_removes_plugin(self) -> None:
        pd = load("L13_abstract")
        edits.apply(pd, {"tracks": [{"track": 1, "plugins": [{"plugin": "Mania", "remove": True}]}]})
        pd = ProjectData.parse(pd.serialize())
        self.assertEqual([p.name for p in T.find_track(pd, 1).plugins], ["Depth"])
        self.assertEqual([p.name for p in T.find_track(pd, 2).plugins], ["E-Piano", "Purpose"])

    def test_replaces_plugin_with_a_copy(self) -> None:
        pd = load("L13_abstract")
        edits.apply(pd, {"tracks": [{"track": 1, "plugins": [
            {"plugin": "Mania", "replace_with": {"track": 2, "plugin": "Purpose"}},
            {"plugin": "Purpose", "params": {"key": 3}},
        ]}]})
        pd = ProjectData.parse(pd.serialize())
        copy = plugin(pd, 1, "Purpose")
        original = plugin(pd, 2, "Purpose")
        self.assertEqual(copy.slot, 4)
        self.assertEqual([p.name for p in T.find_track(pd, 1).plugins], ["Depth", "Purpose"])
        self.assertEqual(plugins.params(copy.record)["key"], 3.0)  # type: ignore[arg-type,index]
        self.assertEqual(plugins.params(original.record)["key"], 0.0)  # type: ignore[arg-type,index]
        # Its own UUID
        self.assertNotEqual(copy.record.raw[-20:-4], original.record.raw[-20:-4])  # type: ignore[union-attr]

    def test_errors(self) -> None:
        cases = [
            ({"plugin": "Mania", "params": {"swng": 1}}, 1, "no parameter 'swng' (did you mean 'swing'"),
            ({"plugin": "Nope", "params": {}}, 1, "has no plugin 'Nope' (plugins: Depth (slot 2), Mania (slot 4))"),
            ({"plugin": "E-Piano", "params": {"a": 1}}, 2, "isn't a JUCE plugin"),
            ({"plugin": "Mania", "params": {"swing": "x"}}, 1, "'swing' must be a number"),
            ({"plugin": "Mania", "params": {"swing": 1}, "remove": True}, 1, "give one of 'params', 'remove', or 'replace_with'"),
            ({"plugin": "Mania", "replace_with": {"track": 2}}, 1, "replace_with: must be like"),
        ]
        for entry, track, message in cases:
            with self.subTest(message), self.assertRaises(edits.EditError) as ctx:
                edits.apply(load("L13_abstract"), {"tracks": [{"track": track, "plugins": [entry]}]})
            self.assertIn(message, str(ctx.exception))

class AddPluginTest(unittest.TestCase):
    def slots(self, pd: ProjectData, n: int) -> list[tuple[int, tuple[int, int], str]]:
        strip = T.find_track(pd, n).strip_id
        return [
            (u16(r.raw, 0x12), plugins.chain(r), r.raw[REC_HEADER_SIZE + 0x78:REC_HEADER_SIZE + 0x84].split(b"\0")[0].decode("latin-1"))
            for r in pd.find(b"UCuA")
            if u32(r.raw, 0x0C) >> 16 == strip
        ]

    def test_adds_effect_after_inserts(self) -> None:
        pd = load("L13_audio_kits")
        edits.apply(pd, {"tracks": [{"track": 2, "plugins": [{"add": {"track": 22, "plugin": "Phaser"}}]}]})
        pd = ProjectData.parse(pd.serialize())
        got = [(o, c, name) for o, c, name in self.slots(pd, 2) if c[0] in (1, 2)]
        self.assertEqual(got, [(3, (1, 0), "E-Piano"), (4, (1, 1), "Phaser"), (7, (2, 0), "Purpose")])
        self.assertEqual([p.name for p in T.find_track(pd, 2).plugins], ["E-Piano", "Phaser", "Purpose"])

    def test_adds_midi_effect(self) -> None:
        # Logic played this Mania, copied to a drum kit's kick
        pd = load("L13_audio_kits")
        edits.apply(pd, {"tracks": [{"track": 23, "plugins": [{"add": {"track": 1, "plugin": "Mania"}}]}]})
        pd = ProjectData.parse(pd.serialize())
        got = [(o, c, name) for o, c, name in self.slots(pd, 23) if c[0] in (1, 2)]
        self.assertEqual(got, [(3, (1, 0), "Q-Sampler"), (4, (2, 0), "Mania")])
        self.assertIsNotNone(plugins.params(T.find_plugin(T.find_track(pd, 23), "Mania").record))

    def test_renumbers_without_gap(self) -> None:
        # L13_template's inserts and MIDI effect have consecutive ordinals
        pd = load("L13_template")
        edits.apply(pd, {"tracks": [{"track": 1, "plugins": [{"add": {"track": 1, "plugin": "Compressor"}}]}]})
        got = [(o, c) for o, c, _ in self.slots(ProjectData.parse(pd.serialize()), 1)][:6]
        self.assertEqual(got, [(2, (1, 0)), (3, (1, 1)), (4, (1, 2)), (5, (1, 3)), (6, (2, 0)), (8, (5, 0))])

    def test_cannot_add_instrument(self) -> None:
        pd = load("L13_audio_kits")
        with self.assertRaisesRegex(edits.EditError, "only an effect or a MIDI effect"):
            edits.apply(pd, {"tracks": [{"track": 2, "plugins": [{"add": {"track": 1, "plugin": "Depth"}}]}]})
