"""Audio regions on audio tracks, and the audio files they play.

An audio region is two records sharing a region index (`idx`, a multiple of
0x40000, counted separately from other records' indexes): `lFuA`, the audio
file it plays, and `gRuA`, the region (its length and name). Like a MIDI
region, it's placed on a track by an 80-byte event in the arrange event
sequence:

    +0x00 0x24 (audio placement)  +0x04 position (REGION_ORIGIN + tick)
    +0x10 channel idx >> 16       +0x14 track number (u16, 1-based)
    +0x2c region idx >> 16

`lFuA` holds the file name (UTF-16) and a format descriptor, found by its
container tag (`EVAW` for WAVE, `FFIA` for AIFF, `ffac` for CAF), with fields
at fixed distances before and after the tag. Logic compares the file size it
stores with the file's, and drops a region whose file changed from the Project
Audio list, so a WAV is given the waveform overview chunk (`LGWV`) Logic would
otherwise add to it, which would change its size.

New regions are clones of a region already in the project, with a new region
index and UUID. Logic rebuilds its audio pool and object registry from the
records when it opens the project.

Ported from LogicProFormatWriter (see NOTICE); format in its
`PROJECTDATA_FORMAT.md` §8, §8.1, §8.9, and §10.7.
"""

from __future__ import annotations

import array
import os
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .container import REC_HEADER_SIZE, ProjectData, Record, put_u16, put_u32, u16, u32
from .regions import PLACEMENT_SIZE, REGION_ORIGIN, RegionError, _arrange_events, _is_placement
from .tracks import Track

AUDIO_DIR = "Audio Files"

_AUDIO_PLACEMENT = 0x24
_IDX_STRIDE = 0x40000
_EVENT_CHANNEL = 0x10
_EVENT_TRACK = 0x14
_EVENT_REGION = 0x2C

# `gRuA` payload offsets, and its 16-byte UUID, record-relative. The name is
# `[u16 length][latin-1 name][pad to even]`, and the record is sized to fit it.
_GRUA_LENGTH = 0x16
_GRUA_NAME = 0x4A
_GRUA_UUID = 0xCA

# `lFuA` payload offsets: the file name's length in UTF-16 units, then the name.
_LFUA_NAME_LEN = 0x08
_LFUA_NAME = 0x0A

# `lFuA` fields relative to the format descriptor's container tag.
_CONTAINER_TAGS = (b"EVAW", b"FFIA", b"ffac")
_D_FIRST_SAMPLE = 0x08
_D_FRAMES = 0x0C
_D_RATE = 0x14
_D_CHANNELS = 0x18
_D_BITS = 0x1A
_D_OVERVIEW_SIZE = 0x4A
_D_SIZE = -0x32
_D_CHANNEL_BYTE = -0x38
_D_FLOAT_BYTE = -0x3D
_D_PATH = -0x13E
_D_PATH_END = -0x62
_D_CODEC = -0x142
_D_TYPE_FLAGS = -0x1C3
_TYPE_COMPRESSED = 0x10
_WAVE_FLOAT_CODEC = 3

# The project's sample rate, by a byte at `gnoS` payload +0xdf and a value at
# +0x11e (repeated at +0x120), for the rates seen in Logic's own projects.
_GNOS_RATE_BYTE = 0x24 + 0xDF
_GNOS_RATE_VALUE = 0x24 + 0x11E
_GNOS_RATES = {(0x00, 0xE970): 44100, (0x16, 0xF8AC): 48000}

# Seconds from Logic's epoch (1904) to the Unix epoch, for the overview chunk.
_MAC_EPOCH = 2_082_844_800
_OVERVIEW_BIN = 256


class AudioError(Exception):
    pass


@dataclass(frozen=True)
class AudioFile:
    """An audio file to copy into a project, as Logic will find it."""

    source: Path
    name: str
    """Its file name in the project's `Audio Files` folder."""
    data: bytes
    """Its contents, with the waveform overview Logic expects."""
    frames: int
    rate: int
    channels: int
    bits: int
    is_float: bool
    first_sample: int
    """The byte offset of its first sample."""
    overview_size: int
    """The size of its waveform overview chunk's body."""


@dataclass(frozen=True)
class Prototype:
    """A region to clone: its `lFuA` and `gRuA` records and its placement event."""

    lfua: bytes
    grua: bytes
    event: bytes


@dataclass
class AudioRegion:
    track: int
    tick: int
    """Start, in ticks from bar 1."""
    frames: int
    """Length, in sample frames of its file."""
    rate: int
    """Its file's sample rate."""
    name: str
    file: str
    idx: int


# --- audio files ------------------------------------------------------------


def _chunks(data: bytes, start: int) -> list[tuple[bytes, int, int]]:
    """A RIFF file's chunks from `start`, as (id, body offset, body size)."""
    out: list[tuple[bytes, int, int]] = []
    o = start
    while o + 8 <= len(data):
        cid = data[o:o + 4]
        size = u32(data, o + 4)
        out.append((cid, o + 8, size))
        o += 8 + size + (size & 1)
    return out


def _overview(body: bytes, channels: int, bits: int, is_float: bool, frames: int) -> bytes:
    """An `LGWV` chunk body: the frame count, a timestamp, then each 256-frame
    bin's peak across channels as a pair of signed bytes, minus and plus its
    largest absolute sample. Logic only uses the peaks to draw the waveform;
    its size is what has to match."""
    if is_float:
        samples = array.array("f", body[:frames * channels * 4])
        if sys.byteorder == "big":
            samples.byteswap()
        peaks = array.array("b", (max(-128, min(127, int(s * 128))) for s in samples))
    else:
        # The top byte of each little-endian sample is the sample scaled to 8 bits.
        width = bits // 8
        peaks = array.array("b", body[width - 1:frames * channels * width:width])
    out = bytearray(struct.pack("<II", frames, int(time.time()) + _MAC_EPOCH))
    step = _OVERVIEW_BIN * channels
    for o in range(0, len(peaks), step):
        part = peaks[o:o + step]
        peak = min(127, max(-min(part), max(part)))
        out += struct.pack("<bb", -peak, peak)
    return bytes(out)


def read_audio_file(path: Path | str, name: str | None = None) -> AudioFile:
    """Read a PCM WAV file for copying into a project. Other formats (e.g., AAC
    or ALAC in a CAF) are converted first, on a Mac with `afconvert`."""
    source = Path(path)
    try:
        data = source.read_bytes()
    except OSError as e:
        raise AudioError(f"cannot read {source}: {e}") from e
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioError(
            f"{source.name} isn't a WAV file; convert it first, e.g. "
            f"`afconvert -f WAVE -d LEI24@48000 '{source.name}' out.wav` on a Mac"
        )
    chunks = _chunks(data, 12)
    fmt = next(((o, n) for cid, o, n in chunks if cid == b"fmt "), None)
    body = next(((o, n) for cid, o, n in chunks if cid == b"data"), None)
    if fmt is None or body is None:
        raise AudioError(f"{source.name} has no format or data chunk")
    fo = fmt[0]
    tag, channels, rate = u16(data, fo), u16(data, fo + 2), u32(data, fo + 4)
    block, bits = u16(data, fo + 12), u16(data, fo + 14)
    if tag == 0xFFFE and fmt[1] >= 40:
        tag = u16(data, fo + 24)
    is_float = tag == _WAVE_FLOAT_CODEC
    if tag not in (1, _WAVE_FLOAT_CODEC) or (is_float and bits != 32) or (not is_float and bits not in (16, 24, 32)):
        raise AudioError(
            f"{source.name} is not 16, 24, or 32-bit PCM or 32-bit float; convert it first, e.g. "
            f"`afconvert -f WAVE -d LEI24 '{source.name}' out.wav` on a Mac"
        )
    frames = body[1] // block
    overview = next((n for cid, _, n in chunks if cid == b"LGWV"), None)
    if overview is None:
        lgwv = _overview(data[body[0]:body[0] + body[1]], channels, bits, is_float, frames)
        tail = b"LGWV" + struct.pack("<I", len(lgwv)) + lgwv + (b"\x00" if len(lgwv) & 1 else b"")
        # A chunk only starts on an even offset.
        pad = b"\x00" if len(data) & 1 else b""
        data = bytearray(data + pad + tail)
        put_u32(data, 4, len(data) - 8)
        data = bytes(data)
        overview = len(lgwv)
    return AudioFile(source, name or source.name, data, frames, rate, channels, bits, is_float, body[0], overview)


# --- reading ----------------------------------------------------------------


def sample_rate(pd: ProjectData) -> int | None:
    """The project's sample rate, or `None` if it isn't one Progic knows."""
    g = pd.song.raw
    return _GNOS_RATES.get((g[_GNOS_RATE_BYTE], u16(g, _GNOS_RATE_VALUE)))


def _region_records(pd: ProjectData) -> dict[int, dict[bytes, Record]]:
    out: dict[int, dict[bytes, Record]] = {}
    for r in pd.records:
        if r.tag in (b"gRuA", b"lFuA") and r.idx % _IDX_STRIDE == 0:
            out.setdefault(r.idx, {}).setdefault(r.tag, r)
    return out


def _file_name(lfua: Record) -> str:
    p = lfua.payload
    return p[_LFUA_NAME:_LFUA_NAME + u16(p, _LFUA_NAME_LEN) * 2].decode("utf-16-le", "replace")


def _region_name(grua: Record) -> str:
    p = grua.payload
    return p[_GRUA_NAME + 2:_GRUA_NAME + 2 + u16(p, _GRUA_NAME)].decode("latin-1", "replace")


def _descriptor(raw: bytes | bytearray) -> int | None:
    """The offset of an `lFuA`'s container tag, past its file name."""
    start = REC_HEADER_SIZE + _LFUA_NAME + u16(raw, REC_HEADER_SIZE + _LFUA_NAME_LEN) * 2
    found = [o for o in (raw.find(t, start) for t in _CONTAINER_TAGS) if o >= 0]
    return min(found) if found else None


def _audio_placements(body: bytes) -> list[int]:
    out: list[int] = []
    o = 0
    while o + PLACEMENT_SIZE <= len(body):
        if body[o] == _AUDIO_PLACEMENT and _is_placement(body, o):
            out.append(o)
            o += PLACEMENT_SIZE
        else:
            o += 4
    return out


def audio_regions(pd: ProjectData) -> list[AudioRegion]:
    events = _arrange_events(pd)
    if events is None:
        return []
    groups = _region_records(pd)
    body = events.payload
    out: list[AudioRegion] = []
    for o in _audio_placements(body):
        idx = (u32(body, o + _EVENT_REGION) // 4) * _IDX_STRIDE
        group = groups.get(idx, {})
        grua, lfua = group.get(b"gRuA"), group.get(b"lFuA")
        if grua is None or lfua is None:
            continue
        d = _descriptor(lfua.raw)
        out.append(
            AudioRegion(
                track=u16(body, o + _EVENT_TRACK),
                tick=u32(body, o + 4) - REGION_ORIGIN,
                frames=u32(grua.payload, _GRUA_LENGTH),
                rate=0 if d is None else u32(lfua.raw, d + _D_RATE),
                name=_region_name(grua),
                file=_file_name(lfua),
                idx=idx,
            )
        )
    return sorted(out, key=lambda r: (r.track, r.tick))


def audio_file_names(pd: ProjectData) -> list[str]:
    """The files the project's audio regions play, in region order."""
    seen: list[str] = []
    for group in sorted(_region_records(pd).items()):
        lfua = group[1].get(b"lFuA")
        if lfua is not None and _file_name(lfua) not in seen:
            seen.append(_file_name(lfua))
    return seen


# --- writing ----------------------------------------------------------------


def extract_prototype(pd: ProjectData) -> Prototype | None:
    """A region in `pd` to clone, or `None` if it has none, preferring one
    that plays a WAV, whose descriptor needs the fewest changes."""
    events = _arrange_events(pd)
    if events is None:
        return None
    groups = _region_records(pd)
    body = events.payload
    found: list[Prototype] = []
    for o in _audio_placements(body):
        idx = (u32(body, o + _EVENT_REGION) // 4) * _IDX_STRIDE
        group = groups.get(idx, {})
        if b"gRuA" in group and b"lFuA" in group:
            found.append(Prototype(group[b"lFuA"].raw, group[b"gRuA"].raw, body[o:o + PLACEMENT_SIZE]))
    wav = [p for p in found if (d := _descriptor(p.lfua)) is not None and p.lfua[d:d + 4] == b"EVAW"]
    return (wav or found or [None])[0]


def _rename_file(raw: bytes, name: str) -> bytes:
    old = u16(raw, REC_HEADER_SIZE + _LFUA_NAME_LEN)
    start = REC_HEADER_SIZE + _LFUA_NAME
    out = bytearray(raw[:start]) + name.encode("utf-16-le") + raw[start + old * 2:]
    put_u16(out, REC_HEADER_SIZE + _LFUA_NAME_LEN, len(name))
    return Record(b"lFuA", bytes(out)).with_payload(bytes(out[REC_HEADER_SIZE:])).raw


def _describe_file(raw: bytes, f: AudioFile) -> bytes:
    """`raw` with its descriptor describing `f`, a PCM WAV, as Logic writes it."""
    b = bytearray(raw)
    d = _descriptor(b)
    if d is None or d + _D_PATH < REC_HEADER_SIZE or d + _D_OVERVIEW_SIZE + 4 > len(b):
        raise AudioError("the project's audio region has an unknown file descriptor")
    b[d:d + 4] = b"EVAW"
    put_u32(b, d + 0x04, 0)
    put_u32(b, d + _D_FIRST_SAMPLE, f.first_sample)
    put_u32(b, d + _D_FRAMES, f.frames)
    put_u32(b, d + _D_RATE, f.rate)
    put_u16(b, d + _D_CHANNELS, f.channels)
    put_u16(b, d + _D_BITS, f.bits)
    put_u32(b, d + _D_OVERVIEW_SIZE, f.overview_size)
    put_u32(b, d + _D_SIZE, len(f.data))
    b[d + _D_CHANNEL_BYTE] = f.channels
    b[d + _D_FLOAT_BYTE] = 2 if f.is_float else 0
    put_u32(b, d + _D_CODEC, _WAVE_FLOAT_CODEC if f.is_float else 0)
    b[d + _D_TYPE_FLAGS] &= ~_TYPE_COMPRESSED & 0xFF
    # Logic finds the file relative to the project's own media folder.
    b[d + _D_PATH:d + _D_PATH_END] = bytes(_D_PATH_END - _D_PATH)
    path = AUDIO_DIR.encode("latin-1") + b"\x00"
    b[d + _D_PATH:d + _D_PATH + len(path)] = path
    return bytes(b)


def _rename_region(raw: bytes, name: str) -> bytes:
    o = REC_HEADER_SIZE + _GRUA_NAME
    old = u16(raw, o)
    encoded = name.encode("latin-1", "replace")
    field_ = struct.pack("<H", len(encoded)) + encoded + (b"\x00" if len(encoded) & 1 else b"")
    out = raw[:o] + field_ + raw[o + 2 + old + (old & 1):]
    return Record(b"gRuA", out).with_payload(out[REC_HEADER_SIZE:]).raw


def _free_region_idx(pd: ProjectData) -> int:
    used = [r.idx for r in pd.records if r.tag in (b"gRuA", b"lFuA")]
    return max(used, default=-_IDX_STRIDE) + _IDX_STRIDE


def add_audio_region(
    pd: ProjectData,
    track: Track,
    f: AudioFile,
    *,
    tick: int,
    frames: int | None = None,
    name: str | None = None,
    prototype: Prototype | None = None,
) -> AudioRegion:
    """Add a region on an audio track playing `f` from its start, at `tick`
    from bar 1, `frames` long (the whole file by default). The region is named
    after the file by default. It's cloned from `prototype`, by default the
    project's own first audio region."""
    if track.kind != "audio":
        raise RegionError(f"track {track.name!r} is a {track.kind} track; audio regions need an audio track")
    if tick < 0:
        raise RegionError("region start must be >= 0")
    frames = f.frames if frames is None else frames
    if not 0 < frames <= f.frames:
        raise RegionError(f"region length must be 1-{f.frames} frames (got {frames})")
    proto = prototype or extract_prototype(pd)
    if proto is None:
        raise RegionError("the project has no audio region to copy; add one to an audio track in the template")
    events = _arrange_events(pd)
    if events is None:
        raise RegionError("project has no arrange event sequence")

    name = name or Path(f.name).stem
    idx = _free_region_idx(pd)
    lfua = bytearray(_describe_file(_rename_file(proto.lfua, f.name), f))
    put_u32(lfua, 0x08, idx)
    grua = bytearray(_rename_region(proto.grua, name))
    put_u32(grua, 0x08, idx)
    put_u32(grua, REC_HEADER_SIZE + _GRUA_LENGTH, frames)
    # A new UUID, keeping the session part in the middle.
    grua[_GRUA_UUID:_GRUA_UUID + 4] = os.urandom(4)
    grua[_GRUA_UUID + 8:_GRUA_UUID + 16] = os.urandom(8)

    # After the other regions' records, or before the project pool (`OgnS`).
    existing = [i for i, r in enumerate(pd.records) if r.tag in (b"gRuA", b"lFuA")]
    at = existing[-1] + 1 if existing else next(
        (i for i, r in enumerate(pd.records) if r.tag == b"OgnS"), len(pd.records)
    )
    pd.records[at:at] = [Record(b"lFuA", bytes(lfua)), Record(b"gRuA", bytes(grua))]

    ev = bytearray(proto.event)
    put_u32(ev, 0x04, REGION_ORIGIN + tick)
    put_u32(ev, _EVENT_CHANNEL, track.channel >> 16)
    put_u16(ev, _EVENT_TRACK, track.number)
    put_u32(ev, _EVENT_REGION, idx >> 16)
    # Logic keeps MIDI and audio placements together, sorted by position.
    body = events.payload
    at = 0
    o = 0
    while o + PLACEMENT_SIZE <= len(body):
        if _is_placement(body, o):
            if u32(body, o + 4) > REGION_ORIGIN + tick:
                break
            o += PLACEMENT_SIZE
            at = o
        else:
            o += 4
    events.set_payload(body[:at] + bytes(ev) + body[at:])
    return AudioRegion(track.number, tick, frames, f.rate, name, f.name, idx)


def remove_audio_regions(pd: ProjectData, track: Track) -> int:
    """Remove every audio region on `track`, and the file references only they
    used. Returns how many were removed. The files stay in the project folder."""
    events = _arrange_events(pd)
    if events is None:
        return 0
    body = events.payload
    removed: list[int] = []
    keep = bytearray()
    last = 0
    for o in _audio_placements(body):
        if u16(body, o + _EVENT_TRACK) == track.number:
            keep += body[last:o]
            last = o + PLACEMENT_SIZE
            removed.append((u32(body, o + _EVENT_REGION) // 4) * _IDX_STRIDE)
    if not removed:
        return 0
    keep += body[last:]
    events.set_payload(bytes(keep))
    still = {(u32(keep, o + _EVENT_REGION) // 4) * _IDX_STRIDE for o in _audio_placements(bytes(keep))}
    doomed = set(removed) - still
    pd.records = [r for r in pd.records if not (r.tag in (b"gRuA", b"lFuA") and r.idx in doomed)]
    return len(removed)
