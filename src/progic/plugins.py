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

from .container import REC_HEADER_SIZE, Record, put_u32, u32

_STATE_SIZE = 0xBC
_STATE = 0xD0
_JUCE_KEY = "jucePluginState"
_JUCE_MAGIC = b"VC2!"
_PARAM = re.compile(rb'<PARAM id="([^"]*)" value="([^"]*)"/>')


class PluginError(Exception):
    pass


def copied_into(source: Record, target: Record) -> Record:
    """A copy of the plugin in `source`'s slot, with its state, for the slot
    `target` is in (any plugin slot's record, on any track). The copy gets its
    own UUID, the 16 bytes before the slot record's last 4."""
    raw = bytearray(source.raw)
    # The strip and the slot
    raw[0x0C:0x14] = target.raw[0x0C:0x14]
    raw[-20:-4] = uuid.uuid4().bytes
    return Record(source.tag, bytes(raw))


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
