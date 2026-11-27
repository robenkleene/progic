"""MIDI regions on instrument tracks.

A MIDI region is a group of three records sharing one cluster index (`idx`):
`qeSM` (the region container: name, length, start), `karT`, and `qSvE` (the
notes). The `tSxT` and `lytS` records that can share a region's index belong to
the score style list, not the region. The region is placed on a track by an
80-byte placement event in the arrange event sequence, which Logic keeps sorted
by position:

    +0x00 0x20 (MIDI placement)   +0x04 position (REGION_ORIGIN + tick)
    +0x10 channel idx >> 16       +0x14 track number (u16, 1-based)
    +0x20 region idx >> 16        +0x30 bar counter

New regions are cloned from a prototype region group, taken from the project
itself when it has a MIDI region, otherwise from a bundled prototype extracted
from a Logic-saved project (see `scripts/extract_prototype.py`).

Ported from LogicProFormatWriter (see NOTICE); format in its
`PROJECTDATA_FORMAT.md` §8.5, §10.9, §36.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from importlib import resources

from .container import REC_HEADER_SIZE, SEQ_TAIL, ProjectData, Record, put_u16, put_u32, u16, u32
from .timeline import POS_ORIGIN
from .tracks import ARRANGE_IDX, Track

REGION_ORIGIN = 34560
PLACEMENT_SIZE = 0x50
_MIDI_PLACEMENT = 0x20
_REGION_GROUP_TAGS = (b"qeSM", b"karT", b"qSvE")
_AUTOMATION = b"*Automation"
_IDX_STRIDE = 0x40000
# New regions use cluster indexes from here up, clear of tracks and channels.
_FIRST_NEW_REGION_IDX = 0x1200000

_QESM_NAME = 0x34
# A sequence id, unique among sequences, stored in the region's `qeSM` and in
# the header of its `qSvE`.
_QESM_SEQUENCE_ID = 0x2C
_QSVE_SEQUENCE_ID = 0x0E
# Offsets after the end of the region name field.
_QESM_LENGTH_AFTER_NAME = 0x3C
_QESM_START_AFTER_NAME = 0xE0
_QESM_CHANNEL_AFTER_NAME = 0xCA
# Minus one more than the track's instrument number, the strip's position among
# instrument strips (signed 16-bit).
_QESM_INSTRUMENT_AFTER_NAME = 0xD4
_STRIP_INSTRUMENT_POSITION = 0x2A
_ARRANGE_ROW_HEIGHT = 0x3C
# Format version fields, as (offset, size) per tag. Logic 11 writes `karT`
# version 5 and `qeSM` 0x370, Logic 13 writes 6 and 0x400, and the layouts are
# otherwise the same. Logic upgrades older records when it opens a project,
# which marks the project modified, so clones take the project's versions.
_VERSION_FIELDS = {b"karT": (0x04, 1), b"qeSM": (0x24, 2)}

_NOTE_ON = 0x90


class RegionError(Exception):
    pass


@dataclass(frozen=True)
class Note:
    tick: int
    """Start, in ticks from the region start."""
    pitch: int
    velocity: int
    length: int


@dataclass
class MidiRegion:
    track: int
    tick: int
    """Start, in ticks from bar 1."""
    length: int
    name: str
    idx: int
    notes: list[Note] = field(default_factory=lambda: [])


@dataclass(frozen=True)
class Prototype:
    group: list[tuple[bytes, bytes]]
    event: bytes
    channel: int


# --- reading ----------------------------------------------------------------


def _is_placement(body: bytes, o: int) -> bool:
    return (
        o + PLACEMENT_SIZE <= len(body)
        and body[o] in (0x20, 0x24)
        and body[o + 1:o + 4] == b"\x00\x00\x00"
        and u32(body, o + 4) >= REGION_ORIGIN
        and body[o + 0x17] == 0x89
        and body[o + 0x37] == 0x8A
        and u16(body, o + 0x14) >= 1
    )


def _placements(body: bytes, marker: int) -> list[int]:
    out: list[int] = []
    o = 0
    while o + PLACEMENT_SIZE <= len(body):
        if body[o] == marker and _is_placement(body, o):
            out.append(o)
            o += PLACEMENT_SIZE
        else:
            o += 4
    return out


def _name_end(qesm: bytes) -> int:
    n = u16(qesm, _QESM_NAME)
    return _QESM_NAME + 2 + n + (n & 1)


def _qesm_name(qesm: bytes) -> bytes:
    n = u16(qesm, _QESM_NAME)
    return qesm[_QESM_NAME + 2:_QESM_NAME + 2 + n]


def decode_notes(seq: Record) -> list[Note]:
    body = seq.payload
    out: list[Note] = []
    i = 0
    while i + 16 <= len(body) and body[i:i + 16] != SEQ_TAIL:
        status = body[i]
        if status & 0xF0 == _NOTE_ON:
            out.append(Note(u32(body, i + 4) - POS_ORIGIN, body[i + 0x0C], body[i + 0x0B], u32(body, i + 0x1C)))
            # Extended notes carry extra 16-byte words tagged at +0x07.
            i += 32
            while i + 16 <= len(body) and body[i + 0x07] in (0xA3, 0xA4, 0xA7):
                i += 16
        elif status & 0xF0 in (0x80, 0xA0, 0xB0, 0xD0, 0xE0) or 0x50 <= status <= 0x57:
            i += 16
        else:
            # Unknown event size (e.g. sysex): stop rather than misread.
            break
    return out


def midi_regions(pd: ProjectData) -> list[MidiRegion]:
    qesms = {r.idx: r for r in pd.find(b"qeSM")}
    seqs = {r.idx: r for r in pd.find(b"qSvE")}
    out: list[MidiRegion] = []
    for arrange in pd.find(b"qSvE", ARRANGE_IDX):
        body = arrange.payload
        for o in _placements(body, _MIDI_PLACEMENT):
            idx = (u32(body, o + 0x20) // 4) << 18
            qe = qesms.get(idx)
            if qe is None or _qesm_name(qe.raw) == _AUTOMATION:
                continue
            end = _name_end(qe.raw)
            notes_seq = seqs.get(idx)
            out.append(
                MidiRegion(
                    track=u16(body, o + 0x14),
                    tick=u32(body, o + 4) - REGION_ORIGIN,
                    length=u32(qe.raw, end + _QESM_LENGTH_AFTER_NAME),
                    name=_qesm_name(qe.raw).decode("latin-1", "replace"),
                    idx=idx,
                    notes=decode_notes(notes_seq) if notes_seq else [],
                )
            )
    return sorted(out, key=lambda r: (r.track, r.tick))


# --- prototypes -------------------------------------------------------------


def arrange_height_offset(container: Record) -> int:
    """Offset of the arrange container's height, 0x3c per row (tracks plus
    master); Logic draws only as many rows as fit."""
    return _name_end(container.raw) + 0x0A


def arrange_container(pd: ProjectData) -> Record | None:
    """The real arrange `qeSM`. Some projects also carry a larger decoy
    `qeSM` at the same idx whose height field is always 0; writing next to the
    decoy leaves regions invisible. The real one has a height that is a
    nonzero multiple of the row height."""
    cands = list(pd.find(b"qeSM", ARRANGE_IDX))

    def height(r: Record) -> int:
        o = arrange_height_offset(r)
        return u16(r.raw, o) if o + 2 <= len(r.raw) else 0

    real = [r for r in cands if height(r) and height(r) % _ARRANGE_ROW_HEIGHT == 0]
    pool = real or cands
    return max(pool, key=lambda r: len(r.raw)) if pool else None


# The arrange sequence's length in ticks, after its name. Logic keeps it at
# the project end marker, and doesn't play audio regions that start after it
# (MIDI regions play regardless).
_ARRANGE_LENGTH_AFTER_NAME = 0x3C


def arrange_length(pd: ProjectData) -> int | None:
    container = arrange_container(pd)
    return None if container is None else u32(container.raw, _name_end(container.raw) + _ARRANGE_LENGTH_AFTER_NAME)


def set_arrange_length(pd: ProjectData, tick: int) -> None:
    container = arrange_container(pd)
    if container is None:
        return
    raw = bytearray(container.raw)
    put_u32(raw, _name_end(container.raw) + _ARRANGE_LENGTH_AFTER_NAME, tick)
    container.raw = bytes(raw)


def _arrange_events(pd: ProjectData) -> Record | None:
    container = arrange_container(pd)
    if container is None:
        return None
    start = pd.records.index(container)
    return next(
        (r for r in pd.records[start + 1:] if r.tag == b"qSvE" and r.idx == ARRANGE_IDX),
        None,
    )


def extract_prototype(pd: ProjectData) -> Prototype | None:
    """A region group and placement event to clone, from the first MIDI region
    in `pd`, or `None` if it has none."""
    events = _arrange_events(pd)
    if events is None:
        return None
    body = events.payload
    for o in _placements(body, _MIDI_PLACEMENT):
        idx = (u32(body, o + 0x20) // 4) << 18
        qe = pd.first(b"qeSM", idx)
        if qe is None or _qesm_name(qe.raw) == _AUTOMATION:
            continue
        group = [(r.tag, r.raw) for r in pd.records if r.tag in _REGION_GROUP_TAGS and r.idx == idx]
        channel = u32(qe.raw, _name_end(qe.raw) + _QESM_CHANNEL_AFTER_NAME)
        return Prototype(group, body[o:o + PLACEMENT_SIZE], channel)
    return None


def prototype_to_json(p: Prototype) -> str:
    return json.dumps(
        {
            "group": [[tag.decode("latin-1"), base64.b64encode(raw).decode()] for tag, raw in p.group],
            "event": base64.b64encode(p.event).decode(),
            "channel": p.channel,
        },
        indent=1,
    )


def bundled_prototype() -> Prototype:
    data = json.loads(resources.files("progic").joinpath("data/midi_region_prototype.json").read_text())
    return Prototype(
        [(tag.encode("latin-1"), base64.b64decode(raw)) for tag, raw in data["group"]],
        base64.b64decode(data["event"]),
        data["channel"],
    )


# --- writing ----------------------------------------------------------------


def encode_notes(notes: list[Note]) -> bytes:
    ordered = sorted(notes, key=lambda n: (n.tick, n.pitch))
    body = bytearray()
    for i, n in enumerate(ordered):
        if not 0 <= n.pitch <= 127 or not 1 <= n.velocity <= 127:
            raise RegionError(f"note pitch must be 0-127 and velocity 1-127 (got {n.pitch}, {n.velocity})")
        if n.tick < 0 or n.length <= 0:
            raise RegionError(f"note start must be >= 0 and length > 0 (got {n.tick}, {n.length})")
        e = bytearray(32)
        e[0x00] = _NOTE_ON
        put_u32(e, 0x04, POS_ORIGIN + n.tick)
        e[0x0B] = n.velocity
        e[0x0C] = n.pitch
        # Bit 0x80 at +0x0f marks the last note of the sequence.
        e[0x0F] = 0x01 | (0x80 if i == len(ordered) - 1 else 0x00)
        e[0x10] = 0x40
        e[0x17] = 0x89
        put_u32(e, 0x1C, n.length)
        body += e
    return bytes(body) + SEQ_TAIL


def _free_region_idx(pd: ProjectData) -> int:
    used = {r.idx for r in pd.records}
    idx = _FIRST_NEW_REGION_IDX
    while idx in used:
        idx += _IDX_STRIDE
    return idx


def _free_sequence_id(pd: ProjectData) -> int:
    used = [u16(r.raw, _QSVE_SEQUENCE_ID) for r in pd.find(b"qSvE")]
    used += [u16(r.raw, _QESM_SEQUENCE_ID) for r in pd.find(b"qeSM")]
    return max(used, default=0) + 1


def _rename_qesm(raw: bytes, name: str) -> bytes:
    encoded = name.encode("latin-1", "replace")
    old = _name_end(raw) - _QESM_NAME
    new = bytearray(2) + encoded + (b"\x00" if len(encoded) & 1 else b"")
    put_u16(new, 0, len(encoded))
    out = raw[:_QESM_NAME] + bytes(new) + raw[_QESM_NAME + old:]
    return Record(b"qeSM", out).with_payload(out[REC_HEADER_SIZE:]).raw


def _adopt_versions(pd: ProjectData, tag: bytes, b: bytearray) -> None:
    version = _VERSION_FIELDS.get(tag)
    existing = pd.first(tag)
    if version is None or existing is None:
        return
    off, size = version
    b[off:off + size] = existing.raw[off:off + size]


def add_midi_region(
    pd: ProjectData,
    track: Track,
    *,
    tick: int,
    length: int,
    notes: list[Note],
    name: str | None = None,
    prototype: Prototype | None = None,
) -> MidiRegion:
    """Add a MIDI region on an instrument track. `tick` is the region start
    from bar 1, `length` its length in ticks, and note ticks are relative to the
    region start. Like Logic, the region is named after the track by default."""
    if track.kind != "instrument":
        raise RegionError(f"track {track.name!r} is a {track.kind} track; MIDI regions need an instrument track")
    if tick < 0 or length <= 0:
        raise RegionError("region start must be >= 0 and length > 0")
    proto = prototype or extract_prototype(pd) or bundled_prototype()
    events = _arrange_events(pd)
    if events is None:
        raise RegionError("project has no arrange event sequence")

    name = name or track.name
    idx = _free_region_idx(pd)
    sequence_id = _free_sequence_id(pd)
    clones: list[Record] = []
    for tag, raw in proto.group:
        b = bytearray(raw)
        put_u32(b, 0x08, idx)
        _adopt_versions(pd, tag, b)
        if tag == b"qSvE":
            put_u16(b, _QSVE_SEQUENCE_ID, sequence_id)
            rec = Record(tag, bytes(b)).with_payload(encode_notes(notes))
        elif tag == b"qeSM":
            # The length, start, channel, and instrument number sit at fixed
            # distances after the variable-length name, so they're written after
            # renaming. They're the only references to the track in the group.
            put_u16(b, _QESM_SEQUENCE_ID, sequence_id)
            b = bytearray(_rename_qesm(bytes(b), name))
            end = _name_end(bytes(b))
            put_u32(b, end + _QESM_LENGTH_AFTER_NAME, length)
            put_u32(b, end + _QESM_START_AFTER_NAME, tick)
            put_u32(b, end + _QESM_CHANNEL_AFTER_NAME, track.channel)
            if track.strip is not None:
                position = u16(track.strip.raw, _STRIP_INSTRUMENT_POSITION)
                put_u16(b, end + _QESM_INSTRUMENT_AFTER_NAME, -(position + 1) & 0xFFFF)
            rec = Record(tag, bytes(b))
        else:
            rec = Record(tag, bytes(b))
        clones.append(rec)

    ev = bytearray(proto.event)
    put_u32(ev, 0x04, REGION_ORIGIN + tick)
    put_u32(ev, 0x10, track.channel >> 16)
    put_u16(ev, 0x14, track.number)
    put_u32(ev, 0x20, idx >> 16)
    # +0x30 advances by 0x100 per 3840 ticks of position. The prototype may sit
    # anywhere, so its own offset is removed first.
    proto_tick = u32(proto.event, 0x04) - REGION_ORIGIN
    put_u32(ev, 0x30, u32(proto.event, 0x30) + (tick // 3840 - proto_tick // 3840) * 0x100)

    # Region groups go before the project pool (`OgnS`), like Logic's own.
    at = next((i for i, r in enumerate(pd.records) if r.tag == b"OgnS"), len(pd.records))
    pd.records[at:at] = clones
    body = events.payload
    at = 0
    for o in _placements(body, _MIDI_PLACEMENT):
        if u32(body, o + 0x04) > REGION_ORIGIN + tick:
            at = o
            break
        at = o + PLACEMENT_SIZE
    events.set_payload(body[:at] + bytes(ev) + body[at:])

    return MidiRegion(track.number, tick, length, name, idx, sorted(notes, key=lambda n: (n.tick, n.pitch)))


def remove_midi_regions(pd: ProjectData, track: Track) -> int:
    """Remove every MIDI region on `track`. Returns how many were removed."""
    events = _arrange_events(pd)
    if events is None:
        return 0
    body = events.payload
    doomed: set[int] = set()
    keep = bytearray()
    last = 0
    for o in _placements(body, _MIDI_PLACEMENT):
        idx = (u32(body, o + 0x20) // 4) << 18
        qe = pd.first(b"qeSM", idx)
        if u16(body, o + 0x14) == track.number and qe is not None and _qesm_name(qe.raw) != _AUTOMATION:
            keep += body[last:o]
            last = o + PLACEMENT_SIZE
            doomed.add(idx)
    if not doomed:
        return 0
    keep += body[last:]
    events.set_payload(bytes(keep))
    pd.records = [r for r in pd.records if not (r.tag in _REGION_GROUP_TAGS and r.idx in doomed)]
    return len(doomed)
