"""The state of an Audio Unit plugin built with JUCE, and its parameters.

An Audio Unit's plugin slot (`UCuA`, see `tracks`) holds the state Logic
restores it from, the plugin's `fullState`: a binary property list at +0xd0,
its size (padded to 4 bytes) at +0xbc, then 20 more bytes. A JUCE plugin keeps
its own state under `jucePluginState` there, which is all it restores from:
JUCE's binary XML (`AudioProcessor::copyXmlToBinary`), the magic `VC2!`, the
XML's length (u32), the XML, and a NUL. A plugin that saves its parameters
with `AudioProcessorValueTreeState` writes each as `<PARAM id="..."
value="..."/>`, with its real value rather than a normalized one.

The plist's `data` holds Logic's own copy of the parameters, normalized,
which JUCE ignores on restore, so it's left as it is.
"""

from __future__ import annotations

import difflib
import plistlib
import re
import struct
import uuid

from .container import REC_HEADER_SIZE, Record, put_u16, put_u32, u16, u32

_STATE_SIZE = 0xBC
_STATE = 0xD0
_JUCE_KEY = "jucePluginState"
_JUCE_MAGIC = b"VC2!"
_PARAM = re.compile(rb'<PARAM id="([^"]*)" value="([^"]*)"/>')


class PluginError(Exception):
    pass


# A slot record's strip id (high 16 bits) and its ordinal among the strip's
# records, in the header, and in the payload, its chain and its position in it.
_STRIP_AND_ORDINAL = slice(0x0C, 0x14)
_ORDINAL = 0x12
_CHAIN = REC_HEADER_SIZE + 0x04
_POSITION = REC_HEADER_SIZE + 0x06
# The chains: the channel's audio, the instrument first on an instrument
# channel then the inserts, and the MIDI effects. Logic's other records for a
# strip are in chains of their own (e.g., 5 and 7).
AUDIO_CHAIN = 1
MIDI_CHAIN = 2


def copied_into(source: Record, target: Record) -> Record:
    """A copy of the plugin in `source`'s slot, with its state, for the slot
    `target` is in (any plugin slot's record, on any track). The copy gets its
    own UUID, the 16 bytes before the slot record's last 4."""
    raw = bytearray(source.raw)
    raw[_STRIP_AND_ORDINAL] = target.raw[_STRIP_AND_ORDINAL]
    raw[_CHAIN:_POSITION + 2] = target.raw[_CHAIN:_POSITION + 2]
    raw[-20:-4] = uuid.uuid4().bytes
    return Record(source.tag, bytes(raw))


def chain(record: Record) -> tuple[int, int]:
    """A slot record's chain and its position in it."""
    return u16(record.raw, _CHAIN), u16(record.raw, _POSITION)


def added_to(records: list[Record], strip_id: int, source: Record) -> list[Record]:
    """`records` with a copy of the plugin in `source`'s slot added to the
    strip `strip_id`, after the plugins already in the same chain: an effect
    after the inserts, a MIDI effect after the MIDI effects. Slot records are
    kept in order of their ordinal, so later ones are renumbered if there's
    no gap before them."""
    kind = u16(source.raw, _CHAIN)
    if kind not in (AUDIO_CHAIN, MIDI_CHAIN):
        raise PluginError("only an effect or a MIDI effect can be added")
    mine = [i for i, r in enumerate(records) if r.tag == b"UCuA" and u32(r.raw, 0x0C) >> 16 == strip_id]
    if not mine:
        raise PluginError("the track's channel strip has no plugin slots to add to")
    same = [i for i in mine if u16(records[i].raw, _CHAIN) == kind]
    # After the same chain, or before the first record of a later one
    before = [i for i in mine if u16(records[i].raw, _CHAIN) < kind]
    after = same[-1] if same else (before[-1] if before else None)
    at = after + 1 if after is not None else mine[0]
    ordinal = u16(records[after].raw, _ORDINAL) + 1 if after is not None else 0
    out = list(records)
    later = [i for i in mine if i >= at]
    if later and u16(out[later[0]].raw, _ORDINAL) <= ordinal:
        for i in later:
            b = bytearray(out[i].raw)
            put_u16(b, _ORDINAL, u16(b, _ORDINAL) + 1)
            out[i] = Record(out[i].tag, bytes(b))
    raw = bytearray(source.raw)
    put_u32(raw, 0x0C, (strip_id << 16) | (u32(raw, 0x0C) & 0xFFFF))
    put_u16(raw, _ORDINAL, ordinal)
    put_u16(raw, _CHAIN, kind)
    put_u16(raw, _POSITION, len(same))
    raw[-20:-4] = uuid.uuid4().bytes
    out.insert(at, Record(source.tag, bytes(raw)))
    return out


def _state_range(raw: bytes) -> tuple[int, int] | None:
    """Where the state's plist is in a plugin slot record, if it has one."""
    if len(raw) < _STATE + 8 or raw[_STATE:_STATE + 8] != b"bplist00":
        return None
    size = u32(raw, _STATE_SIZE)
    if _STATE + size > len(raw):
        return None
    return _STATE, _STATE + size


def _state(raw: bytes) -> dict | None:
    where = _state_range(raw)
    if where is None:
        return None
    start, end = where
    # The size is padded, and a plist's trailer is its last 32 bytes, so the
    # padding is removed first
    data = raw[start:end]
    for pad in range(4):
        try:
            state = plistlib.loads(data[:len(data) - pad])
        except Exception:
            continue
        return state if isinstance(state, dict) else None
    return None


def _juce_xml(state: dict) -> bytes | None:
    blob = state.get(_JUCE_KEY)
    if not isinstance(blob, bytes) or blob[:4] != _JUCE_MAGIC:
        return None
    n = struct.unpack_from("<I", blob, 4)[0]
    return blob[8:8 + n]


def state_xml(record: Record) -> str | None:
    """A JUCE plugin's state, the XML its `getStateInformation` saves, or
    `None` for any other plugin."""
    state = _state(record.raw)
    xml = None if state is None else _juce_xml(state)
    return None if xml is None else xml.decode("utf-8")


def params(record: Record) -> dict[str, float] | None:
    """A JUCE plugin's parameters, by id, or `None` for any other plugin."""
    state = _state(record.raw)
    xml = None if state is None else _juce_xml(state)
    if xml is None:
        return None
    return {k.decode(): float(v) for k, v in _PARAM.findall(xml)}


def set_params(record: Record, values: dict[str, float]) -> None:
    """Sets a JUCE plugin's parameters in its state."""
    raw = record.raw
    state = _state(raw)
    xml = None if state is None else _juce_xml(state)
    if state is None or xml is None:
        raise PluginError("it isn't a JUCE plugin with its state saved")
    known = {k.decode() for k, _ in _PARAM.findall(xml)}
    for name in values:
        if name not in known:
            close = difflib.get_close_matches(name, known, n=3)
            hint = f" (did you mean {', '.join(repr(c) for c in close)}?)" if close else ""
            raise PluginError(f"no parameter {name!r}{hint}")

    def replace(m: re.Match[bytes]) -> bytes:
        name = m.group(1).decode()
        if name not in values:
            return m.group(0)
        return b'<PARAM id="%s" value="%s"/>' % (m.group(1), repr(float(values[name])).encode())

    xml = _PARAM.sub(replace, xml)
    state[_JUCE_KEY] = _JUCE_MAGIC + struct.pack("<I", len(xml)) + xml + b"\x00"
    plist = plistlib.dumps(state, fmt=plistlib.FMT_BINARY, sort_keys=False)
    plist += bytes(-len(plist) % 4)
    start, end = _state_range(raw)  # type: ignore[misc]
    new = bytearray(raw[:start] + plist + raw[end:])
    put_u32(new, _STATE_SIZE, len(plist))
    record.set_payload(bytes(new[REC_HEADER_SIZE:]))
