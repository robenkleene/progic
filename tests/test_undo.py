from __future__ import annotations

import unittest

from progic import undo
from progic.bundle import Bundle
from progic.container import ProjectData, u32

from .util import fixture


class UndoTest(unittest.TestCase):
    def test_strips_history(self) -> None:
        pd = Bundle(fixture("L13_template")).read()
        self.assertEqual(undo.strip_undo(pd), 20)
        pd = ProjectData.parse(pd.serialize())
        self.assertEqual([r for r in pd.records if r.tag == b"lESU"], [])
        (stack,) = [r for r in pd.records if r.tag == b"ktSU"]
        self.assertEqual((u32(stack.raw, len(stack.raw) - 8), u32(stack.raw, len(stack.raw) - 4)), (0, 0))

    def test_project_without_history(self) -> None:
        pd = Bundle(fixture("F0_baseline")).read()
        self.assertEqual(undo.strip_undo(pd), 0)
        self.assertEqual(pd.serialize(), Bundle(fixture("F0_baseline")).read().serialize())


if __name__ == "__main__":
    unittest.main()
