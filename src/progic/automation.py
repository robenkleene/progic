"""Track automation: volume and pan changing over time.

Each track's channel has an automation container, a `qeSM` named
`*Automation` whose channel field (at the same distance after its name as a
MIDI region's) is the track's channel, and a `qSvE` with the same index
holding its points as 16-byte events, sorted by position:

    +0x00 0x50                    +0x01 position's fraction of a tick (24-bit)
    +0x04 position (POS_ORIGIN + tick)
    +0x08 value, 8.24 fixed point (the fader's 0-127 for volume, 90 = 0 dB)
    +0x0c parameter (MIDI controller numbers: 7 volume, 10 pan)
    +0x0f flags: 0x01 a point, 0x40 added for a point Logic interpolated
          between two, 0x80 added for a selected point

Logic saves a ramp between two points with the points it interpolated: for
volume every 15 ticks, and for pan, whose values are whole numbers, wherever
the value has changed by one more, so ramps are written the same way.

Decoded from a project Logic 13 saved with volume and pan automation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .container import SEQ_TAIL, ProjectData, Record, put_u32, u32
from .regions import _QESM_CHANNEL_AFTER_NAME, _name_end, _qesm_name
from .timeline import POS_ORIGIN
from .tracks import Track

_AUTOMATION = b"*Automation"
_EVENT = 0x50
_EVENT_SIZE = 16
_POINT = 0x01
_INTERPOLATED = 0x40
_STEP = 15
# Parameters interpolated by steps of value rather than of time
_VALUE_STEP = {10: 1.0}

PARAMS = {"volume": 7, "pan": 10}

# Container fields Logic sets once it holds automation, after the name.
_HAS_AUTOMATION = ((0x7B, 84), (0xC0, 1))
_SHOWN_PARAM_AFTER_NAME = 0xAC
_SHOWN_PARAM = {7: 24, 10: 28}

# The fader's value for 0 dB, and its curve: 40 dB per decade of value.
_UNITY = 90


class AutomationError(Exception):
    pass


@dataclass(frozen=True)
class Point:
    tick: int
    value: float
    """The parameter's raw value, 0-127."""


def db_to_value(db: float) -> float:
    """The fader value for `db` (90 is 0 dB, 127 about +6 dB)."""
    return min(127.0, _UNITY * 10 ** (db / 40))


def value_to_db(value: float) -> float:
    return -math.inf if value <= 0 else 40 * math.log10(value / _UNITY)


def _container(pd: ProjectData, track: Track) -> tuple[Record, Record]:
    for q in pd.find(b"qeSM"):
        if _qesm_name(q.raw) == _AUTOMATION and u32(q.raw, _name_end(q.raw) + _QESM_CHANNEL_AFTER_NAME) == track.channel:
            seq = pd.first(b"qSvE", q.idx)
            if seq is not None:
                return q, seq
    raise AutomationError(f"track {track.name!r} has no automation container")


def lanes(pd: ProjectData, track: Track) -> dict[int, list[Point]]:
    """The points of each parameter automated on `track`, without the ones
    Logic interpolated."""
    try:
        _, seq = _container(pd, track)
    except AutomationError:
        return {}
    body = seq.payload
    out: dict[int, list[Point]] = {}
    for o in range(0, len(body) - _EVENT_SIZE + 1, _EVENT_SIZE):
        if body[o] != _EVENT or body[o + 0x0F] & _INTERPOLATED:
            continue
        tick = u32(body, o + 4) - POS_ORIGIN + int.from_bytes(body[o + 1:o + 4], "little") / 2**24
        out.setdefault(body[o + 0x0C], []).append(Point(round(tick), u32(body, o + 8) / 2**24))
    return out


def _event(tick: float, value: float, param: int, flags: int) -> bytes:
    e = bytearray(_EVENT_SIZE)
    e[0] = _EVENT
    whole = math.floor(tick)
    e[1:4] = round((tick - whole) * 2**24).to_bytes(4, "little")[:3]
    put_u32(e, 4, POS_ORIGIN + whole)
    put_u32(e, 8, round(max(0.0, min(127.0, value)) * 2**24) & 0xFFFFFFFF)
    e[0x0C] = param
    e[0x0F] = flags
    return bytes(e)


def set_lanes(pd: ProjectData, track: Track, new: dict[int, list[Point]]) -> None:
    """Replace `track`'s automation of each parameter in `new` with its
    points, ramping linearly between them. Other parameters are kept."""
    q, seq = _container(pd, track)
    current = lanes(pd, track)
    current.update(new)
    events: list[tuple[float, int, bytes]] = []
    for param, points in current.items():
        points = sorted(points, key=lambda p: p.tick)
        for a, b in zip(points, points[1:] + [None]):
            events.append((a.tick, param, _event(a.tick, a.value, param, _POINT)))
            if b is None or b.value == a.value or b.tick == a.tick:
                continue
            slope = (b.value - a.value) / (b.tick - a.tick)
            step = _VALUE_STEP[param] / abs(slope) if param in _VALUE_STEP else _STEP
            t = a.tick + step
            while t < b.tick:
                events.append((t, param, _event(t, a.value + slope * (t - a.tick), param, _POINT | _INTERPOLATED)))
                t += step
    events.sort(key=lambda e: (e[0], e[1]))
    seq.set_payload(b"".join(e for _, _, e in events) + SEQ_TAIL)

    raw = bytearray(q.raw)
    end = _name_end(q.raw)
    for off, value in _HAS_AUTOMATION:
        raw[end + off] = value if events else 0
    shown = next(iter(current), None)
    raw[end + _SHOWN_PARAM_AFTER_NAME] = _SHOWN_PARAM.get(shown, 0) if events else 0
    q.raw = bytes(raw)
