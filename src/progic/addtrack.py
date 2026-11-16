"""Adding software instrument tracks.

Logic can't load a track whose mixer channel was created from nothing (that
re-indexes the whole mixer), but a project Logic created fresh carries a pool
of unused instrument channel strips (`OCuA` with a zero UUID). Adding a track
clones the newest instrument channel (`ivnE`), binds it to a free strip, and
adds the arrange row and the bookkeeping Logic expects. Logic regenerates the
`gnoS` object registry on load, so only its counters are updated.

This is LogicProFormatWriter's Logic-validated "light" instrument op
(`activate_instrument_track`, `PROJECTDATA_FORMAT.md` §10.9.1). It needs a
template that:

- has at least 3 instrument tracks (the shared instrument records are in
  their final form from the third instrument on),
- has an instrument track as its most recently created channel, and
- still has free instrument strips (Logic pre-allocates them in a new project).
"""

from __future__ import annotations

import os
import random
from typing import Callable

from . import regions, tracks
from .container import REC_HEADER_SIZE, REC_SIZE_OFF, ProjectData, Record, put_u32, u16, u32
from .tracks import ARRANGE_IDX, MASTER_CHANNEL, Track, TrackError

_IDX_STRIDE = 0x40000

# gnoS (Song) counters.
_GNOS_MAX_CHANNEL = 0x80
_GNOS_TRACK_COUNT = 0xF4
_GNOS_TRACK_COUNT_HI = 0xF8

# ivnE (environment channel).
_IVNE_IDX2 = 0x34
_IVNE_FLAGS = 0x51
_IVNE_IS_LAST = 0x74
_IVNE_LINK = 0x76
_IVNE_NAME_LEN = 0xC2
_IVNE_NAME = 0xC4
_IVNE_UUID_AFTER_NAME = 0x11F
# Master and output channels whose link byte moves with every new channel.
_BUS_CHANNELS = (0x4C0000, 0x500000, 0x540000)

# OCuA (channel strip).
_OCUA_ACTIVE = (0x3C, 0x3D)
_OCUA_CONFIG = 0x70
_OCUA_SEQ = 0x82
_OCUA_UUID = 0xBD
_INSTRUMENT_CONFIG = b"\x29\xf5"

# karT arrange rows (idx 0x40000, 93 bytes) and per-slot Track objects (idx 0x80000).
_KART_SIZE = 93
_KART_BLOCK = 0x10
_KART_CHANNEL = 0x2A
_KART_ID = 0x3C
_KART_NEWEST = 0x4C
_TRACK_IDX = 0x80000
_TRACK_RANK = 0x12
_TRACK_SLOT = 0x2C
# Master and output rows hold the top ranks and never move.
_SYSTEM_RANKS = 3

_ROW_HEIGHT = 0x3C

# MneG (Session Player state) has a JSON dictionary keyed by arrange row id.
_MNEG_DICT = b'drummerModelTrackStates":{'
_MNEG_LENGTHS = (0x24, 0x40)
_MNEG_ENTRY = (
    '{"keepDrumKitWhenChangingDrummer":false,"selectedCharacterIdentifier":'
    '"Acoustic Drummer - Pop Rock","isUsingProducerKit":false,"stateVersion":3,'
    '"keepSettingsWhenChangingDrummer":false,'
    '"selectedPersistentCharacterTypeIdentifier":"Type_AcousticDrummerV2",'
    '"parametersWhereChangedAfterCharacterRecall":false}'
)

RandomBytes = Callable[[int], bytes]


def seeded_random(seed: int) -> RandomBytes:
    """Deterministic ids for tests."""
    rng = random.Random(seed)
    return lambda n: bytes(rng.randrange(256) for _ in range(n))


def _new_id(rand: RandomBytes) -> bytes:
    # Logic's ids are time-UUID shaped with this constant in bytes 4-7.
    return rand(4) + b"\x00\x80\x00\x00" + rand(8)


def _uuid_string(id16: bytes) -> str:
    h = id16.hex().upper()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _set(rec: Record, edit: Callable[[bytearray], None]) -> None:
    raw = bytearray(rec.raw)
    edit(raw)
    rec.raw = bytes(raw)


def _strips(pd: ProjectData) -> list[Record]:
    return [r for r in pd.find(b"OCuA") if len(r.raw) >= _OCUA_UUID + 16]


def _is_instrument_strip(r: Record) -> bool:
    return r.raw[_OCUA_CONFIG:_OCUA_CONFIG + 2] == _INSTRUMENT_CONFIG


def free_instrument_strips(pd: ProjectData) -> int:
    """How many more instrument tracks this project can take."""
    return sum(1 for r in _strips(pd) if _is_instrument_strip(r) and r.raw[_OCUA_UUID:_OCUA_UUID + 16] == bytes(16))


def _instrument_channels(pd: ProjectData) -> dict[int, Record]:
    by_uuid = {r.raw[_OCUA_UUID:_OCUA_UUID + 16]: r for r in _strips(pd)}
    out: dict[int, Record] = {}
    for r in pd.find(b"ivnE"):
        strip = by_uuid.get(tracks.ivne_uuid(r.raw))
        if strip is not None and _is_instrument_strip(strip):
            out[r.idx] = r
    return out


def can_add_instrument(pd: ProjectData) -> str | None:
    """None if an instrument track can be added, else the reason it can't."""
    # Logic 13 track rows are a byte longer, and its projects have no spare
    # strips, so this method doesn't apply to them.
    if any(len(r.raw) != _KART_SIZE for r in tracks.arrange_rows(pd)):
        return "adding tracks to projects saved by Logic 13 isn't supported yet"
    song = pd.song
    newest = u32(song.raw, _GNOS_MAX_CHANNEL)
    count = sum(1 for t in tracks.tracks(pd) if t.kind == "instrument")
    if count < 3:
        return f"adding tracks needs a template with at least 3 instrument tracks (it has {count})"
    if newest not in _instrument_channels(pd):
        return "adding tracks needs a template whose most recently created track is a software instrument track"
    if not free_instrument_strips(pd):
        return "the template has no free instrument channel strips left"
    return None


def _add_arrange_row(pd: ProjectData, ordinal: int, channel: int, row_id: bytes) -> None:
    rows = [r for r in pd.find(b"karT", ARRANGE_IDX) if len(r.raw) == _KART_SIZE]
    master = next(r for r in rows if u32(r.raw, _KART_CHANNEL) == MASTER_CHANNEL)
    template = rows[0]

    def block(n: int) -> bytes:
        return bytes([0xFF, 0xFF, n & 0xFF, 0, 0, 0, 0x02, 0])

    for r in rows:
        if r is not master:
            _set(r, lambda b: b.__setitem__(_KART_NEWEST, 0))
    raw = bytearray(template.raw)
    raw[_KART_BLOCK:_KART_BLOCK + 8] = block(ordinal)
    put_u32(raw, _KART_CHANNEL, channel)
    raw[_KART_ID:_KART_ID + 16] = row_id
    raw[_KART_NEWEST] = 0x20
    _set(master, lambda b: b.__setitem__(slice(_KART_BLOCK, _KART_BLOCK + 8), block(ordinal + 1)))
    pd.records.insert(pd.records.index(master), Record(b"karT", bytes(raw)))


def _rerank(pd: ProjectData, slot: int) -> None:
    """The new slot takes the highest track rank and the other tracks and free
    slots move down one. Logic reads the Track objects in stream order, so they
    are re-sorted by rank."""
    positions = [i for i, r in enumerate(pd.records) if r.tag == b"karT" and r.idx == _TRACK_IDX and len(r.raw) == _KART_SIZE]
    rows = [pd.records[i] for i in positions]
    if len(rows) <= _SYSTEM_RANKS:
        raise TrackError("unexpected track list (too few Track objects)")
    by_rank = sorted(rows, key=lambda r: r.raw[_TRACK_RANK], reverse=True)
    system = {id(r) for r in by_rank[:_SYSTEM_RANKS]}
    top = by_rank[_SYSTEM_RANKS].raw[_TRACK_RANK]
    for r in rows:
        if u32(r.raw, _TRACK_SLOT) == slot:
            _set(r, lambda b: b.__setitem__(_TRACK_RANK, top))
        elif id(r) not in system:
            _set(r, lambda b: b.__setitem__(_TRACK_RANK, (b[_TRACK_RANK] - 1) & 0xFF))
    for i, r in zip(positions, sorted(rows, key=lambda r: r.raw[_TRACK_RANK])):
        pd.records[i] = r


def _set_arrange_height(pd: ProjectData) -> None:
    container = regions.arrange_container(pd)
    if container is None:
        raise TrackError("project has no arrange container")
    off = regions.arrange_height_offset(container)
    rows = len(tracks.arrange_rows(pd)) + 1
    _set(container, lambda b: b.__setitem__(slice(off, off + 2), ((_ROW_HEIGHT * rows) & 0xFFFF).to_bytes(2, "little")))


def _add_session_player_state(pd: ProjectData, row_id: bytes) -> None:
    mneg = pd.first(b"MneG", 0)
    if mneg is None:
        return
    pos = mneg.raw.find(_MNEG_DICT)
    if pos < 0:
        return
    at = pos + len(_MNEG_DICT)
    entry = f'"{_uuid_string(row_id)}":{_MNEG_ENTRY},'.encode("latin-1")
    raw = bytearray(mneg.raw[:at]) + entry + mneg.raw[at:]
    for off in _MNEG_LENGTHS:
        put_u32(raw, off, u32(raw, off) + len(entry))
    put_u32(raw, REC_SIZE_OFF, len(raw) - REC_HEADER_SIZE)
    mneg.raw = bytes(raw)


def add_instrument_track(pd: ProjectData, name: str | None = None, *, rand: RandomBytes = os.urandom) -> Track:
    """Add an empty software instrument track after the last track. Its channel
    strip is a fresh one (no instrument or plugins loaded)."""
    reason = can_add_instrument(pd)
    if reason:
        raise TrackError(reason)
    song = pd.song
    count = u32(song.raw, _GNOS_TRACK_COUNT) >> 16
    newest = u32(song.raw, _GNOS_MAX_CHANNEL)
    channel = newest + _IDX_STRIDE
    slot = channel >> 16

    def counters(b: bytearray) -> None:
        put_u32(b, _GNOS_MAX_CHANNEL, channel)
        put_u32(b, _GNOS_TRACK_COUNT, (count + 1) << 16)
        put_u32(b, _GNOS_TRACK_COUNT_HI, ((count + 1) << 16) | 1)

    _set(song, counters)
    channel_id = _new_id(rand)
    row_id = _new_id(rand)

    # Clone the newest instrument channel. Only the index, link, UUID, and
    # last-channel flag change; bumping the counters after the name leaves the
    # strip hollow in Logic.
    prev = _instrument_channels(pd)[newest]
    new = bytearray(prev.raw)
    put_u32(new, 0x08, channel)
    put_u32(new, _IVNE_IDX2, slot)
    new[_IVNE_LINK] = (prev.raw[_IVNE_LINK] + 0x42) & 0xFF
    n = u16(new, _IVNE_NAME_LEN)
    uuid_off = _IVNE_NAME + n + (n & 1) + _IVNE_UUID_AFTER_NAME
    new[uuid_off:uuid_off + 16] = channel_id
    new[_IVNE_IS_LAST] = 0x01

    def demote(b: bytearray) -> None:
        b[_IVNE_IS_LAST] = 0x00
        b[_IVNE_FLAGS] = (b[_IVNE_FLAGS] + 0x02) & 0xFF

    _set(prev, demote)
    pd.records.insert(pd.records.index(prev) + 1, Record(b"ivnE", bytes(new)))
    for bus in _BUS_CHANNELS:
        r = pd.first(b"ivnE", bus)
        if r is not None:
            _set(r, lambda b: b.__setitem__(_IVNE_LINK, (b[_IVNE_LINK] + 0x42) & 0xFF))

    strip = next(
        r for r in _strips(pd) if _is_instrument_strip(r) and r.raw[_OCUA_UUID:_OCUA_UUID + 16] == bytes(16)
    )

    def activate(b: bytearray) -> None:
        for off in _OCUA_ACTIVE:
            b[off] = 0x01
        b[_OCUA_SEQ] = 0x00
        b[_OCUA_UUID:_OCUA_UUID + 16] = channel_id

    _set(strip, activate)
    _add_arrange_row(pd, count, channel, row_id)
    _rerank(pd, slot)
    _set_arrange_height(pd)
    _add_session_player_state(pd, row_id)

    track = next(t for t in tracks.tracks(pd) if t.channel == channel)
    if name is not None:
        tracks.rename(track, name)
    return track
