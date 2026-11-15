from __future__ import annotations

import unittest

from progic.bundle import Bundle
from progic.container import ProjectData

from .util import all_fixtures


class RoundTripTest(unittest.TestCase):
    def test_every_fixture_round_trips_byte_for_byte(self) -> None:
        fixtures = all_fixtures()
        self.assertTrue(fixtures)
        for path in fixtures:
            with self.subTest(path.name):
                data = Bundle(path).project_data_path.read_bytes()
                self.assertEqual(ProjectData.parse(data).serialize(), data)

    def test_rejects_non_project_data(self) -> None:
        with self.assertRaises(ValueError):
            ProjectData.parse(b"not a project")


if __name__ == "__main__":
    unittest.main()
