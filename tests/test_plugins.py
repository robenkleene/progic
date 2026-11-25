from __future__ import annotations

import unittest

from progic import edits, plugins
from progic import tracks as T
from progic.bundle import Bundle
from progic.container import ProjectData

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

    def test_errors(self) -> None:
        cases = [
            ({"plugin": "Mania", "params": {"swng": 1}}, 1, "no parameter 'swng' (did you mean 'swing'"),
            ({"plugin": "Nope", "params": {}}, 1, "has no plugin 'Nope' (plugins: Depth (slot 2), Mania (slot 4))"),
            ({"plugin": "E-Piano", "params": {"a": 1}}, 2, "isn't a JUCE plugin"),
            ({"plugin": "Mania", "params": {"swing": "x"}}, 1, "'swing' must be a number"),
            ({"plugin": "Mania", "params": {}, "remove": True}, 1, "give 'params' or 'remove', not both"),
        ]
        for entry, track, message in cases:
            with self.subTest(message), self.assertRaises(edits.EditError) as ctx:
                edits.apply(load("L13_abstract"), {"tracks": [{"track": track, "plugins": [entry]}]})
            self.assertIn(message, str(ctx.exception))
