"""Byte-level framing of Logic's `ProjectData` file.

`ProjectData` is a 24-byte root header followed by a flat list of records. Each
record is a 36-byte header (a reversed FourCC tag, an index, and a payload size)
followed by its payload. There are no absolute offsets between records, so
records can grow, shrink, be inserted, or be removed as long as the root length
is recomputed on write.

Ported from LogicProFormatWriter (see NOTICE). The format is documented in that
project's `PROJECTDATA_FORMAT.md` §2.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator

MAGIC = b"\x23\x47\xc0\xab"
ROOT_HEADER_SIZE = 0x18
REC_HEADER_SIZE = 0x24
REC_SIZE_OFF = 0x1C
REC_IDX_OFF = 0x08

# Event sequences (`qSvE`) end with this 16-byte trailer.
SEQ_TAIL = bytes.fromhex("f1000000ffffff3f0000000000000000")


def u16(b: bytes | bytearray, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def u32(b: bytes | bytearray, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def put_u16(b: bytearray, o: int, v: int) -> None:
    struct.pack_into("<H", b, o, v)


def put_u32(b: bytearray, o: int, v: int) -> None:
    struct.pack_into("<I", b, o, v)


class Record:
    """One record: `tag` is the 4-byte tag as stored (e.g. `b"karT"`), `raw`
    is the whole record including its 36-byte header."""

    __slots__ = ("tag", "raw")

    def __init__(self, tag: bytes, raw: bytes) -> None:
        self.tag = tag
        self.raw = raw

    @property
    def idx(self) -> int:
        return u32(self.raw, REC_IDX_OFF)

    @property
    def header(self) -> bytes:
        return self.raw[:REC_HEADER_SIZE]

    @property
    def payload(self) -> bytes:
        return self.raw[REC_HEADER_SIZE:REC_HEADER_SIZE + u32(self.raw, REC_SIZE_OFF)]

    def with_payload(self, payload: bytes) -> Record:
        """A copy of this record with `payload` replaced and the size fixed."""
        hdr = bytearray(self.header)
        put_u32(hdr, REC_SIZE_OFF, len(payload))
        return Record(self.tag, bytes(hdr) + payload)

    def set_payload(self, payload: bytes) -> None:
        self.raw = self.with_payload(payload).raw

    def __repr__(self) -> str:
        return f"<Record {self.tag.decode('latin-1')!r} idx=0x{self.idx:x} {len(self.raw)}B>"


class ProjectData:
    """A parsed `ProjectData` file. `parse(data).serialize() == data` holds
    byte-for-byte for any file Logic writes."""

    def __init__(self, root_header: bytes, records: list[Record]) -> None:
        self.root_header = root_header
        self.records = records

    @classmethod
    def parse(cls, data: bytes) -> ProjectData:
        if data[:4] != MAGIC:
            raise ValueError(f"not a ProjectData file (magic {data[:4].hex()})")
        end = ROOT_HEADER_SIZE + u32(data, 0x10)
        if end != len(data):
            raise ValueError(f"root length points to 0x{end:x}, file is 0x{len(data):x} bytes")
        records: list[Record] = []
        pos = ROOT_HEADER_SIZE
        while pos < end:
            tag = data[pos:pos + 4]
            if tag == MAGIC:
                size = ROOT_HEADER_SIZE + u32(data, pos + 0x10)
            else:
                # A printable tag is the desync check: if the size math drifts,
                # the next "tag" lands on arbitrary bytes.
                if not all(0x20 <= c < 0x7F for c in tag):
                    raise ValueError(f"desync: non-printable tag {tag.hex()} at 0x{pos:x}")
                size = REC_HEADER_SIZE + u32(data, pos + REC_SIZE_OFF)
            if size < REC_HEADER_SIZE or pos + size > end:
                raise ValueError(f"bad size {size} for {tag!r} at 0x{pos:x}")
            records.append(Record(tag, data[pos:pos + size]))
            pos += size
        return cls(data[:ROOT_HEADER_SIZE], records)

    def serialize(self) -> bytes:
        body = b"".join(r.raw for r in self.records)
        hdr = bytearray(self.root_header)
        put_u32(hdr, 0x10, len(body))
        return bytes(hdr) + body

    def find(self, tag: bytes, idx: int | None = None) -> Iterator[Record]:
        for r in self.records:
            if r.tag == tag and (idx is None or r.idx == idx):
                yield r

    def first(self, tag: bytes, idx: int | None = None) -> Record | None:
        return next(self.find(tag, idx), None)

    @property
    def song(self) -> Record:
        """The root `gnoS` (Song) record, always first."""
        return self.records[0]
