from __future__ import annotations

import math
import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from progic import audio as A
from progic import edits as E
from progic import tracks as T
from progic.bundle import Bundle
from progic.container import ProjectData, put_u32

from .util import fixture


def load(name: str) -> ProjectData:
    return Bundle(fixture(name)).read()


def reparse(pd: ProjectData) -> ProjectData:
    return ProjectData.parse(pd.serialize())


def logic_wav(name: str) -> Path:
    return fixture("F23_av") / "Media" / "Audio Files" / name


class AudioTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.dir)

    def tone(self, name: str = "tone.wav", seconds: float = 3, rate: int = 44100) -> Path:
        path = self.dir / name
        with wave.open(str(path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(
                b"".join(struct.pack("<hh", int(8000 * math.sin(i / 20)), 0) for i in range(int(rate * seconds)))
            )
        return path


class ReadTest(AudioTestCase):
    def test_reads_regions_logic_wrote(self) -> None:
        got = [(r.track, r.tick, r.frames, r.name, r.file) for r in A.audio_regions(load("F23_av"))]
        self.assertEqual(
            got, [(5, 0, 9546, "047", "047.wav"), (6, 0, 9546, "048", "048.wav"), (7, 0, 11150, "049", "049.wav")]
        )

    def test_describes_files_as_logic_does(self) -> None:
        # Describing the file each region plays reproduces Logic's descriptor
        pd = load("F23_av")
        for lfua in pd.find(b"lFuA"):
            name = A._file_name(lfua)
            with self.subTest(name):
                self.assertEqual(A._describe_file(lfua.raw, A.read_audio_file(logic_wav(name))), lfua.raw)

    def test_adds_overview_logic_would(self) -> None:
        # Without its overview chunk, a file gets one the size Logic's was
        data = logic_wav("049.wav").read_bytes()
        chunks = {cid: (o, n) for cid, o, n in A._chunks(data, 12)}
        start, size = chunks[b"LGWV"]
        bare = bytearray(data[:start - 8])
        put_u32(bare, 4, len(bare) - 8)
        (self.dir / "049.wav").write_bytes(bare)
        f = A.read_audio_file(self.dir / "049.wav")
        self.assertEqual(len(f.data), len(data))
        self.assertEqual(f.overview_size, size)

    def test_rejects_other_formats(self) -> None:
        (self.dir / "x.caf").write_bytes(b"caff" + bytes(64))
        with self.assertRaisesRegex(A.AudioError, "afconvert"):
            A.read_audio_file(self.dir / "x.caf")


class WriteTest(AudioTestCase):
    def test_adds_region(self) -> None:
        pd = load("F23_av")
        f = A.read_audio_file(self.tone())
        A.add_audio_region(pd, T.find_track(pd, 6), f, tick=3840)
        got = [(r.track, r.tick, r.frames, r.name, r.file) for r in A.audio_regions(reparse(pd)) if r.track == 6]
        self.assertEqual(got, [(6, 0, 9546, "048", "048.wav"), (6, 3840, 132300, "tone", "tone.wav")])

    def test_needs_audio_track(self) -> None:
        pd = load("F23_av")
        with self.assertRaises(A.RegionError):
            A.add_audio_region(pd, T.find_track(pd, 1), A.read_audio_file(self.tone()), tick=0)

    def test_removes_regions(self) -> None:
        pd = load("F23_av")
        self.assertEqual(A.remove_audio_regions(pd, T.find_track(pd, 6)), 1)
        pd = reparse(pd)
        self.assertEqual([r.track for r in A.audio_regions(pd)], [5, 7])
        self.assertEqual(A.audio_file_names(pd), ["047.wav", "049.wav"])


class EditTest(AudioTestCase):
    def apply(self, doc: object) -> tuple[ProjectData, E.Media]:
        pd = load("F23_av")
        media = E.Media(base=self.dir, taken={"047.wav", "048.wav", "049.wav"})
        E.apply(pd, doc, media)
        return reparse(pd), media

    def test_repeats_until(self) -> None:
        self.tone(seconds=3)
        pd, media = self.apply(
            {"tracks": [{"track": 6, "clear_regions": True,
                         "audio_regions": [{"bar": 5, "file": "tone.wav", "until": {"bar": 9}}]}]}
        )
        # 3 s is 1.5 bars at 120 BPM: two whole copies, then 2 s of the third
        got = [(r.tick, r.frames) for r in A.audio_regions(pd) if r.track == 6]
        self.assertEqual(got, [(15360, 132300), (21120, 132300), (26880, 88200)])
        self.assertEqual([f.name for f in media.files.values()], ["tone.wav"])

    def test_clear_keeps_prototype(self) -> None:
        # Clearing every region still leaves one to copy
        self.tone()
        doc = {"tracks": [{"track": n, "clear_regions": True} for n in (5, 6, 7)]}
        doc["tracks"][0]["audio_regions"] = [{"bar": 1, "file": "tone.wav"}]
        pd, _ = self.apply(doc)
        self.assertEqual([(r.track, r.file) for r in A.audio_regions(pd)], [(5, "tone.wav")])

    def test_renames_taken_file(self) -> None:
        self.tone("047.wav")
        _, media = self.apply({"tracks": [{"track": 5, "audio_regions": [{"bar": 2, "file": "047.wav"}]}]})
        self.assertEqual([f.name for f in media.files.values()], ["047_1.wav"])

    def test_rejects_other_rates(self) -> None:
        self.tone(rate=48000)
        with self.assertRaisesRegex(E.EditError, "48000 Hz but the project is 44100"):
            self.apply({"tracks": [{"track": 5, "audio_regions": [{"bar": 1, "file": "tone.wav"}]}]})

    def test_errors_name_the_region(self) -> None:
        with self.assertRaisesRegex(E.EditError, r"tracks\[0\]\.audio_regions\[0\]"):
            self.apply({"tracks": [{"track": 5, "audio_regions": [{"bar": 1, "file": "missing.wav"}]}]})


if __name__ == "__main__":
    unittest.main()
