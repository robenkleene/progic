"""Tempo, meter (time signature), and marker maps, plus bar/tick conversion.

All positions are 960-PPQ ticks measured from bar 1 (tick 0 = bar 1, beat 1).
Logic stores them with an origin offset (`POS_ORIGIN`) that is added on write.

Ported from LogicProFormatWriter (see NOTICE); format in its
`PROJECTDATA_FORMAT.md` §4 (tempo), §5 (meter), and §6 (markers).
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from .container import SEQ_TAIL, ProjectData, Record, put_u16, put_u32, u16, u32

PPQ = 960
POS_ORIGIN = 38400

# Tempo is `BPM * 10000` in three `gnoS` payload slots plus the tempo track.
_GNOS_TEMPO_OFFS = (0x92, 0xEA, 0x3A6)
# `0x3a6` is the stable initial-tempo slot (`0x92` can hold a playhead value).
_GNOS_TEMPO_INITIAL = 0x3A6

_TEMPO_EVENT = b"\x60\x00\x00\x00"
_TEMPO_ALT_ORIGIN = 7_200_000
_TEMPO_ALT_PER_SEC = 2000

_SIG_EVENT = b"\x30\x00\x00\x00"
_SIG_HEADER_SIZE = 80

_MARKER_EVENT = b"\x12\x00\x00\x00"
_MARKER_RTF_PREFIX = (
    b"{\\rtf1\\ansi\\ansicpg1252\\cocoartf2867\n"
    b"\\cocoatextscaling0\\cocoaplatform0{\\fonttbl\\f0\\fnil\\fcharset0 HelveticaNeue;}\n"
    b"{\\colortbl;\\red255\\green255\\blue255;\\red255\\green255\\blue255;}\n"
    b"{\\*\\expandedcolortbl;;\\cssrgb\\c100000\\c100000\\c100000\\c67000;}\n"
    b"\\pard\\tx560\\tx1120\\tx1680\\tx2240\\tx2800\\tx3360\\tx3920\\tx4480"
    b"\\tx5040\\tx5600\\tx6160\\tx6720\\pardirnatural\\partightenfactor0\n"
    b"\n\\f0\\fs24 \\cf2 "
)
# A marker-name `qSxT` header, constant across Logic's own saves. Used when the
# project has a marker track but no marker names to clone a header from.
_MARKER_NAME_HEADER = bytes.fromhex(
    "7153785401002000000004000000ffffffffffffffff020000000200e401000000000000"
)
_GNOS_MARKER_RANGE = 0x24 + 0x1D0


class TimelineError(Exception):
    pass


@dataclass(frozen=True)
class TempoPoint:
    tick: int
    bpm: float


@dataclass(frozen=True)
class MeterPoint:
    tick: int
    numerator: int
    denominator: int


@dataclass(frozen=True)
class Marker:
    tick: int
    name: str


# --- tempo ------------------------------------------------------------------


def _tempo_seq(pd: ProjectData) -> Record | None:
    return next((r for r in pd.find(b"qSvE") if len(r.raw) > 0x38 and r.payload[:4] == _TEMPO_EVENT), None)


def tempo_map(pd: ProjectData) -> list[TempoPoint]:
    seq = _tempo_seq(pd)
    if seq is None:
        return []
    body = seq.payload
    out: list[TempoPoint] = []
    i = 0
    while i + 0x20 <= len(body) and body[i:i + 4] == _TEMPO_EVENT:
        pos = struct.unpack_from("<Q", body, i + 4)[0]
        out.append(TempoPoint(pos - POS_ORIGIN, u32(body, i + 0x10) / 10000))
        i += 0x20
    return out


def initial_bpm(pd: ProjectData) -> float:
    return u32(pd.song.raw, _GNOS_TEMPO_INITIAL) / 10000


def set_tempo_map(pd: ProjectData, points: list[TempoPoint]) -> None:
    """Replace the tempo map. A point at tick 0 is added if missing."""
    seq = _tempo_seq(pd)
    if seq is None:
        raise TimelineError("project has no tempo track")
    by_tick = {p.tick: p.bpm for p in points}
    items = sorted(by_tick.items())
    if not items:
        raise TimelineError("tempo map needs at least one point")
    if items[0][0] != 0:
        items.insert(0, (0, items[0][1]))
    for _, bpm in items:
        if not 5 <= bpm <= 990:
            raise TimelineError(f"tempo {bpm} BPM is outside Logic's 5-990 range")

    events = bytearray()
    seconds = 0.0
    prev: tuple[int, float] | None = None
    for n, (tick, bpm) in enumerate(items):
        if prev is not None:
            seconds += (tick - prev[0]) / PPQ * 60.0 / prev[1]
        alt = _TEMPO_ALT_ORIGIN + round(seconds * _TEMPO_ALT_PER_SEC)
        events += (
            _TEMPO_EVENT
            + struct.pack("<Q", POS_ORIGIN + tick)
            + b"\x7f\x00\x00" + bytes([0x00 if n == 0 else 0x01])
            + struct.pack("<I", round(bpm * 10000))
            + b"\x00\x00\x40\x88"
            + struct.pack("<I", alt)
            + b"\x00\x00\x00\x00"
        )
        prev = (tick, bpm)
    old = u32(pd.song.raw, _GNOS_TEMPO_INITIAL)
    seq.set_payload(bytes(events) + SEQ_TAIL)

    # The `gnoS` single-tempo slots mirror the first point. Only slots holding
    # the old tempo are rewritten, since `0x92` can hold an unrelated value.
    g = bytearray(pd.song.raw)
    new = round(items[0][1] * 10000)
    for off in _GNOS_TEMPO_OFFS:
        if u32(g, off) == old:
            put_u32(g, off, new)
    pd.song.raw = bytes(g)


# --- meter ------------------------------------------------------------------


def _meter_seq(pd: ProjectData) -> Record | None:
    return next(
        (r for r in pd.find(b"qSvE") if len(r.raw) > 0x24 + _SIG_HEADER_SIZE and r.payload[:4] == _SIG_EVENT),
        None,
    )


def meter_map(pd: ProjectData) -> list[MeterPoint]:
    seq = _meter_seq(pd)
    if seq is None:
        return []
    body = seq.payload
    out = [MeterPoint(0, body[0x0C], 1 << body[0x0B])]
    off = _SIG_HEADER_SIZE
    end = len(body) - len(SEQ_TAIL)
    while off + 48 <= end and body[off:off + 4] == _SIG_EVENT:
        out.append(MeterPoint(u32(body, off + 4) - POS_ORIGIN, body[off + 0x0C], 1 << body[off + 0x0B]))
        off += 48
    return out


def _den_exp(den: int) -> int:
    if den <= 0 or den & (den - 1):
        raise TimelineError(f"time signature denominator {den} is not a power of two")
    return den.bit_length() - 1


def set_meter_map(pd: ProjectData, points: list[MeterPoint]) -> None:
    """Replace the meter map. A 4/4 point at tick 0 is added if missing."""
    seq = _meter_seq(pd)
    if seq is None:
        raise TimelineError("project has no signature track")
    by_tick = {p.tick: (p.numerator, p.denominator) for p in points}
    items = sorted(by_tick.items())
    if not items or items[0][0] != 0:
        items.insert(0, (0, items[0][1] if items else (4, 4)))
    (num0, den0), changes = items[0][1], items[1:]
    header = bytearray(seq.payload[:_SIG_HEADER_SIZE])
    header[0x0B] = _den_exp(den0)
    header[0x0C] = num0
    header[0x0F] = 0x00 if changes else 0x80
    body = bytearray()
    for i, (tick, (num, den)) in enumerate(changes):
        r = bytearray(48)
        r[0:4] = _SIG_EVENT
        put_u32(r, 0x04, POS_ORIGIN + tick)
        r[0x0B] = _den_exp(den)
        r[0x0C] = num
        r[0x0F] = 0x80 if i == len(changes) - 1 else 0x00
        r[0x10:0x14] = _SIG_EVENT
        r[0x17] = 0x88
        r[0x18] = 2 * (i + 1) - 1
        put_u32(r, 0x1C, POS_ORIGIN + tick)
        r[0x27] = 0x88
        body += r
    seq.set_payload(bytes(header) + bytes(body) + SEQ_TAIL)


# --- markers ----------------------------------------------------------------


def _marker_seq(pd: ProjectData) -> Record | None:
    filled = next((r for r in pd.find(b"qSvE") if r.payload[:4] == _MARKER_EVENT), None)
    if filled is not None:
        return filled
    # An empty marker track is a unique 52-byte `qSvE` with subtype 0x16, plus
    # the marker track's metadata `qSxT`. Projects that never had a marker
    # track also carry the empty `qSvE`, so the `qSxT` is what tells them apart.
    if not any(r.tag == b"qSxT" and not _is_marker_name(r) for r in pd.records):
        return None
    empty = [r for r in pd.find(b"qSvE", 0x40000) if len(r.raw) == 52 and u16(r.raw, 6) == 0x16]
    return empty[0] if len(empty) == 1 else None


def has_marker_track(pd: ProjectData) -> bool:
    return _marker_seq(pd) is not None


def _is_marker_name(r: Record) -> bool:
    return r.tag == b"qSxT" and b"{\\rtf1" in r.raw


def markers(pd: ProjectData) -> list[Marker]:
    seq = _marker_seq(pd)
    if seq is None:
        return []
    names: dict[int, str] = {}
    for r in pd.records:
        if _is_marker_name(r):
            m = re.search(rb"\\cf2 (.*?)\}", r.raw, re.S)
            if m:
                names[u16(r.raw, 0x0A)] = m.group(1).decode("latin-1", "replace").strip()
    body = seq.payload
    out: list[Marker] = []
    o = 0
    while o + 48 <= len(body) and body[o:o + 4] == _MARKER_EVENT:
        out.append(Marker(u32(body, o + 4) - POS_ORIGIN, names.get(u32(body, o + 0x10), "")))
        o += 48
    return out


def _marker_name_record(header: bytes, name: str, link: int) -> Record:
    rtf = _MARKER_RTF_PREFIX + _rtf_escape(name) + b"}"
    rtf_off = 0x62
    payload = bytearray(rtf_off) + rtf
    put_u32(payload, 0x00, len(payload))
    put_u32(payload, 0x10, rtf_off)
    put_u32(payload, 0x14, len(payload))
    put_u32(payload, 0x18, 0x13)
    payload[0x1C:0x22] = bytes.fromhex("1b1b2f2f5252")
    hdr = bytearray(header)
    put_u16(hdr, 0x0A, link)
    return Record(b"qSxT", bytes(hdr)).with_payload(bytes(payload))


def _rtf_escape(name: str) -> bytes:
    out = bytearray()
    for ch in name:
        if ch in "\\{}":
            out += b"\\" + ch.encode()
        elif ord(ch) < 0x80:
            out += ch.encode()
        else:
            # `\uN?` is RTF's escape for characters outside the code page.
            out += f"\\u{ord(ch) if ord(ch) < 0x8000 else ord(ch) - 0x10000}?".encode()
    return bytes(out)


def set_markers(pd: ProjectData, new_markers: list[Marker]) -> None:
    """Replace all markers. The project must already have a marker track (in
    Logic: create a marker, delete it, and save)."""
    seq = _marker_seq(pd)
    if seq is None:
        raise TimelineError(
            "project has no marker track; in Logic, add a marker, delete it, and save the template"
        )
    items = sorted(new_markers, key=lambda m: m.tick)
    events = bytearray()
    for k, m in enumerate(items):
        e = bytearray(48)
        e[0:4] = _MARKER_EVENT
        put_u32(e, 0x04, POS_ORIGIN + m.tick)
        # Each marker event links to its name record by `link = (k + 1) * 4`.
        put_u32(e, 0x10, (k + 1) * 4)
        e[0x17] = 0x88
        e[0x1C] = 0x01
        e[0x27] = 0x88
        events += e
    seq.set_payload(bytes(events) + SEQ_TAIL)

    existing = [i for i, r in enumerate(pd.records) if _is_marker_name(r)]
    header = pd.records[existing[0]].header if existing else _MARKER_NAME_HEADER
    name_records = [_marker_name_record(header, m.name, (k + 1) * 4) for k, m in enumerate(items)]
    if existing:
        at = existing[0]
        pd.records = [r for r in pd.records if not _is_marker_name(r)]
        pd.records[at:at] = name_records
    else:
        # New names go right after the marker track's metadata `qSxT`.
        meta = [i for i, r in enumerate(pd.records) if r.tag == b"qSxT" and not _is_marker_name(r)]
        if not meta:
            raise TimelineError("project has no marker metadata record to anchor marker names")
        pd.records[meta[-1] + 1:meta[-1] + 1] = name_records

    if items:
        g = bytearray(pd.song.raw)
        put_u32(g, _GNOS_MARKER_RANGE, POS_ORIGIN)
        put_u32(g, _GNOS_MARKER_RANGE + 8, POS_ORIGIN + items[-1].tick)
        pd.song.raw = bytes(g)


# --- bar/tick conversion ----------------------------------------------------


class TimeMap:
    """Converts between bar/beat positions and ticks using a meter map. Bars
    and beats are 1-based; a beat is one denominator unit (a quarter in 4/4,
    an eighth in 6/8)."""

    def __init__(self, meter: list[MeterPoint]) -> None:
        self.meter = sorted(meter, key=lambda p: p.tick) or [MeterPoint(0, 4, 4)]
        if self.meter[0].tick != 0:
            self.meter.insert(0, MeterPoint(0, 4, 4))

    @staticmethod
    def beat_ticks(denominator: int) -> int:
        return PPQ * 4 // denominator

    def _segments(self) -> list[tuple[int, int, MeterPoint]]:
        """`(start_tick, start_bar, point)` for each meter segment."""
        out: list[tuple[int, int, MeterPoint]] = []
        bar = 1
        for i, p in enumerate(self.meter):
            out.append((p.tick, bar, p))
            if i + 1 < len(self.meter):
                bar_len = p.numerator * self.beat_ticks(p.denominator)
                bar += -(-(self.meter[i + 1].tick - p.tick) // bar_len)
        return out

    def to_tick(self, bar: float, beat: float = 1) -> int:
        """Tick of `bar`/`beat`. Fractional bars and beats are allowed."""
        if bar < 1 or beat < 1:
            raise TimelineError(f"bar and beat are 1-based (got bar {bar}, beat {beat})")
        seg = next(s for s in reversed(self._segments()) if s[1] <= bar)
        start, start_bar, p = seg
        beat_len = self.beat_ticks(p.denominator)
        return round(start + (bar - start_bar) * p.numerator * beat_len + (beat - 1) * beat_len)

    def to_bar_beat(self, tick: int) -> tuple[int, float]:
        seg = next(s for s in reversed(self._segments()) if s[0] <= tick)
        start, start_bar, p = seg
        beat_len = self.beat_ticks(p.denominator)
        bars, rem = divmod(tick - start, p.numerator * beat_len)
        return start_bar + bars, 1 + rem / beat_len

    def bar_ticks(self, bar: int) -> int:
        """Length of `bar` in ticks."""
        seg = next(s for s in reversed(self._segments()) if s[1] <= bar)
        return seg[2].numerator * self.beat_ticks(seg[2].denominator)
