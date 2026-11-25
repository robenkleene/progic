"""Tracks and their channel strips.

A track in the Tracks area is joined across four record types:

    karT (arrange row, idx 0x40000)   -> channel idx at +0x2a
    ivnE (environment channel, idx == channel idx)
                                      -> track name, and a UUID
    OCuA (mixer channel strip, UUID at +0xbd == the ivnE UUID)
                                      -> strip type, fader, pan, strip id
    UCuA (plugin slot, strip id at +0x0c >> 16 == the OCuA strip id)
                                      -> plugin identity and state

Format references are to LogicProFormatWriter's `PROJECTDATA_FORMAT.md`
(§15 OCuA, §16 UCuA, §18 karT, §19 ivnE).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .container import REC_HEADER_SIZE, ProjectData, Record, put_u16, u16, u32

ARRANGE_IDX = 0x40000
MASTER_CHANNEL = 0x500000

_KART_CHANNEL = 0x2A
_IVNE_NAME_LEN = 0xC2
_IVNE_NAME = 0xC4
_IVNE_NAME_FROM_PATCH = 0x84
_IVNE_NAME_FROM_PATCH_BIT = 0x80
_OCUA_UUID = 0xBD
_OCUA_MAGIC = 0x1235

STRIP_TYPES = {
    0x40: "audio",
    0x41: "input",
    0x42: "aux",
    0x43: "instrument",
    0x44: "output",
    0x45: "bus",
    0x46: "master",
    0x49: "input",
    0x4B: "rewire",
    0x4C: "output",
}

_AU_TYPES = {b"aufx", b"aumu", b"aumf", b"augn", b"aumi", b"auol", b"aupn", b"aufc", b"aumx"}


class TrackError(Exception):
    pass


@dataclass
class Plugin:
    slot: int
    name: str
    native: bool
    preset: str
    au_type: str | None = None
    au_subtype: str | None = None
    au_manufacturer: str | None = None
    record: Record | None = field(default=None, repr=False, compare=False)
    """The plugin slot record, which has its state."""


@dataclass
class Track:
    number: int
    """1-based position in the Tracks area (the master row is not counted)."""
    name: str
    kind: str
    channel: int
    ivne: Record
    strip: Record | None
    plugins: list[Plugin] = field(default_factory=lambda: [])

    @property
    def strip_id(self) -> int | None:
        return None if self.strip is None else u32(self.strip.raw, 0x0C) >> 16

    @property
    def volume(self) -> int | None:
        return None if self.strip is None else self.strip.raw[REC_HEADER_SIZE + 0x55]

    @property
    def pan(self) -> int | None:
        return None if self.strip is None else self.strip.raw[REC_HEADER_SIZE + 0x59]


def ivne_name(raw: bytes) -> str:
    n = u16(raw, _IVNE_NAME_LEN)
    return raw[_IVNE_NAME:_IVNE_NAME + n].decode("latin-1", "replace")


def ivne_uuid(raw: bytes) -> bytes:
    """The channel UUID sits a fixed distance after the variable-length name."""
    n = u16(raw, _IVNE_NAME_LEN)
    off = _IVNE_NAME + n + (n & 1) + 0x11F
    return raw[off:off + 16]


def _is_strip(r: Record) -> bool:
    pay = r.raw[REC_HEADER_SIZE:]
    return len(r.raw) >= _OCUA_UUID + 16 and len(pay) > 0x5A and u16(pay, 0x0A) == _OCUA_MAGIC


def strip_uuid_offsets(r: Record) -> tuple[int, int]:
    """Where a strip's channel UUID may be. Up to Logic 12 it is the last 16
    bytes of the strip; Logic 13 appends 32 bytes (the output's UUID and
    padding) after it."""
    return len(r.raw) - 16, len(r.raw) - 48


def strips_by_uuid(pd: ProjectData) -> dict[bytes, Record]:
    """Channel strips keyed by the channel UUID they belong to, for both
    layouts. Mapping both candidate offsets is safe: a random UUID never
    matches by accident."""
    out: dict[bytes, Record] = {}
    for r in pd.find(b"OCuA"):
        if _is_strip(r):
            for off in strip_uuid_offsets(r):
                out.setdefault(r.raw[off:off + 16], r)
    out.pop(bytes(16), None)
    return out


def arrange_rows(pd: ProjectData) -> list[Record]:
    """Populated arrange rows in stream order, excluding the master row (up to
    Logic 12). Logic 13 also has a row for the main output, which `tracks`
    leaves out."""
    return [
        r
        for r in pd.find(b"karT", ARRANGE_IDX)
        if len(r.raw) > _KART_CHANNEL + 4 and u16(r.raw, 0x14) == 0 and u32(r.raw, _KART_CHANNEL) != MASTER_CHANNEL
    ]


def _plugins(pd: ProjectData, strip_id: int) -> list[Plugin]:
    out: list[Plugin] = []
    for r in pd.find(b"UCuA"):
        if u32(r.raw, 0x0C) >> 16 != strip_id:
            continue
        pay = r.raw[REC_HEADER_SIZE:]
        if len(pay) < 0x90:
            continue
        # `GAME` at +0x84 marks a built-in Logic plugin; otherwise an Audio Unit
        # is identified by its byte-reversed type/subtype/manufacturer codes.
        native = pay[0x84:0x88] == b"GAME"
        au_type = pay[0x88:0x8C][::-1]
        if not (native or au_type in _AU_TYPES):
            continue
        raw_name = pay[0x78:0x84].split(b"\x00", 1)[0]
        name = raw_name.decode("latin-1") if all(0x20 <= c < 0x7F for c in raw_name) else ""
        preset = pay[0x0E:].split(b"\x00", 1)[0].decode("latin-1", "replace")

        def code(o: int) -> str:
            return pay[o:o + 4][::-1].decode("latin-1", "replace")

        out.append(
            Plugin(
                slot=u16(r.raw, 0x12),
                name=name,
                native=native,
                preset=preset,
                au_type=None if native else code(0x88),
                au_subtype=None if native else code(0x8C),
                au_manufacturer=None if native else code(0x84),
                record=r,
            )
        )
    return sorted(out, key=lambda p: p.slot)


def tracks(pd: ProjectData) -> list[Track]:
    ivnes = {r.idx: r for r in pd.find(b"ivnE")}
    strips = strips_by_uuid(pd)
    out: list[Track] = []
    for row in arrange_rows(pd):
        channel = u32(row.raw, _KART_CHANNEL)
        ivne = ivnes.get(channel)
        if ivne is None:
            continue
        strip = strips.get(ivne_uuid(ivne.raw))
        kind = STRIP_TYPES.get(strip.raw[REC_HEADER_SIZE + 4], "unknown") if strip else "unknown"
        if kind in ("output", "master"):
            continue
        t = Track(len(out) + 1, ivne_name(ivne.raw), kind, channel, ivne, strip)
        if t.strip_id is not None:
            t.plugins = _plugins(pd, t.strip_id)
        out.append(t)
    return out


def find_track(pd: ProjectData, ref: str | int) -> Track:
    """Look up a track by 1-based number or exact name."""
    all_tracks = tracks(pd)
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        n = int(ref)
        for t in all_tracks:
            if t.number == n:
                return t
        raise TrackError(f"no track number {n} (project has {len(all_tracks)} tracks)")
    matches = [t for t in all_tracks if t.name == ref]
    if not matches:
        names = ", ".join(repr(t.name) for t in all_tracks)
        raise TrackError(f"no track named {ref!r} (tracks: {names})")
    if len(matches) > 1:
        raise TrackError(f"{len(matches)} tracks are named {ref!r}; refer to it by number instead")
    return matches[0]


def find_plugin(track: Track, ref: object) -> Plugin:
    """Look up one of a track's plugins by name or slot number."""
    if isinstance(ref, str) and ref.isdigit():
        ref = int(ref)
    if isinstance(ref, int) and not isinstance(ref, bool):
        found = [p for p in track.plugins if p.slot == ref]
    elif isinstance(ref, str):
        found = [p for p in track.plugins if p.name == ref]
    else:
        raise TrackError("a plugin is its name or slot number")
    if not found:
        have = ", ".join(f"{p.name or '?'} (slot {p.slot})" for p in track.plugins) or "none"
        raise TrackError(f"track {track.name!r} has no plugin {ref!r} (plugins: {have})")
    if len(found) > 1:
        raise TrackError(f"track {track.name!r} has {len(found)} plugins named {ref!r}; use its slot number")
    return found[0]


def rename(track: Track, name: str) -> None:
    """Rename a track. Logic stores the track name on its environment channel
    (`ivnE`), which is what Logic itself changes on a rename."""
    encoded = name.encode("latin-1", "replace")
    if not encoded:
        raise TrackError("track name cannot be empty")
    raw = bytearray(track.ivne.raw)
    n = u16(raw, _IVNE_NAME_LEN)
    # The name field is `[u16 length][bytes][pad to even]`, and everything after
    # it shifts, so the record is rebuilt rather than patched in place.
    field_bytes = bytearray(2) + encoded + (b"\x00" if len(encoded) & 1 else b"")
    put_u16(field_bytes, 0, len(encoded))
    # Bit 0 of +0x51 is set on channels Logic has renamed (bit 1 is a separate
    # flag that is set on every channel but the last).
    raw[0x51] |= 0x01
    # Bit 7 of +0x84 names the track after its patch instead (e.g., "Untitled"
    # for an instrument with no patch), which Logic 13 sets on new instrument
    # channels and clears on a rename
    raw[_IVNE_NAME_FROM_PATCH] &= ~_IVNE_NAME_FROM_PATCH_BIT & 0xFF
    raw[_IVNE_NAME_LEN:_IVNE_NAME + n + (n & 1)] = field_bytes
    track.ivne.raw = Record(track.ivne.tag, bytes(raw)).with_payload(bytes(raw[REC_HEADER_SIZE:])).raw
    track.name = name


def set_mixer(track: Track, *, volume: int | None = None, pan: int | None = None) -> None:
    """Set the strip fader and pan as 0-127 values (volume 90 is Logic's
    default, about 0 dB; pan 64 is center, 0 is hard left, 127 hard right)."""
    if track.strip is None:
        raise TrackError(f"track {track.name!r} has no channel strip")
    raw = bytearray(track.strip.raw)
    if volume is not None:
        if not 0 <= volume <= 127:
            raise TrackError("volume must be 0-127")
        # The fader value at +0x55 is mirrored at +0x77, and both must match.
        raw[REC_HEADER_SIZE + 0x55] = volume
        raw[REC_HEADER_SIZE + 0x77] = volume
    if pan is not None:
        if not 0 <= pan <= 127:
            raise TrackError("pan must be 0-127")
        raw[REC_HEADER_SIZE + 0x59] = pan
    track.strip.raw = bytes(raw)
