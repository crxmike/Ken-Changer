"""
pclink_protocol.py
===================
Low-level implementation of Kenwood's "PC-Link" serial protocol, as documented at
https://juken.sourceforge.net/protocol/ (saved copy, 2026).

Covers the Kenwood CD-4700M, CD-4260M, CD-425M CD changers. The DV-5050M /
DV-5900M DVD changers share the same low-level framing but a different (only
partially documented) command set, which is not implemented here.

Physical link
-------------
9600 baud, 8 data bits, no parity, 2 stop bits, over a null-modem
(TX/RX swapped) RS-232 cable.

Flow control
------------
A form of ENQ/ACK handshaking (not XON/XOFF):

    initiator -> ENQ (0x05)
    receiver  -> ACK (0x06)                 (receiver ready)
    initiator -> STX + command + payload    (the actual frame)
    receiver  -> ACK (0x06) / NAK (0x15)    (frame received ok / checksum bad)
    initiator -> EOT (0x04)
    receiver  -> ACK (0x06)

Either side (PC or changer) can be the initiator -- the changer initiates
whenever it has a spontaneous event to report (StateEvent, InfoEvent,
DiscEvent, DoorEvent, or a reply to a request we made).

Payload format
--------------
    [ 2 bytes: data length "n", LSB first ]
    [ n bytes: data                       ]
    [ 1 byte:  checksum                   ]

Checksum = bitwise NOT of ( (command byte + length bytes + data bytes summed) - 1 ),
low 8 bits. Implemented below as a direct two's-complement negation, which is
algebraically the same thing.

Everything below the "Wire framing" section is a straight transcription of the
documented payload shapes. Anywhere the source site left a field named
"unknown", this code preserves it as a raw placeholder rather than guessing.
"""

from __future__ import annotations

import struct
import threading
import time
import queue
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger("pclink")

# --------------------------------------------------------------------------
# Control bytes
# --------------------------------------------------------------------------
ENQ = 0x05
ACK = 0x06
NAK = 0x15
EOT = 0x04
STX = 0x02

# --------------------------------------------------------------------------
# Command bytes (source: contents.html)
# --------------------------------------------------------------------------
CMD_HANDSHAKE = 0x00
CMD_DATA_ACCESS = 0x03
CMD_DISC_INFO = 0x04
CMD_DISC_TOC = 0x06
CMD_DISC_USERFILES = 0x07
CMD_DISC_GENRE = 0x08
CMD_READY_FOR_DATA = 0x09
CMD_DO_ACTION = 0x0A
CMD_CHANGE_DISC = 0x0B
CMD_CHANGE_MODE = 0x0C
CMD_DISC_LISTING = 0x0D
CMD_INFO_EVENT = 0x12
CMD_STATE_EVENT = 0x13
CMD_DISC_EVENT = 0x14
CMD_DOOR_EVENT = 0x15
CMD_LONG_TEXT_DATA = 0xFD
CMD_TEXT_DATA = 0xFE

COMMAND_NAMES = {
    CMD_HANDSHAKE: "Handshake",
    CMD_DATA_ACCESS: "DataAccess",
    CMD_DISC_INFO: "DiscInfo",
    CMD_DISC_TOC: "DiscTOC",
    CMD_DISC_USERFILES: "DiscUserfiles",
    CMD_DISC_GENRE: "DiscGenre",
    CMD_READY_FOR_DATA: "ReadyForData",
    CMD_DO_ACTION: "DoAction",
    CMD_CHANGE_DISC: "ChangeDisc",
    CMD_CHANGE_MODE: "ChangeMode",
    CMD_DISC_LISTING: "DiscListing",
    CMD_INFO_EVENT: "InfoEvent",
    CMD_STATE_EVENT: "StateEvent",
    CMD_DISC_EVENT: "DiscEvent",
    CMD_DOOR_EVENT: "DoorEvent",
    CMD_LONG_TEXT_DATA: "LongTextData",
    CMD_TEXT_DATA: "TextData",
}

# --------------------------------------------------------------------------
# Enumerations (source: cd_types.html)
# --------------------------------------------------------------------------


class Action:
    """DataAccess.action -- what to do with the referenced data."""

    RETRIEVE_DATA = 0x00
    SET_DISC_GENRE = 0x10
    WRITE_PROGRAM = 0x20
    SET_USERFILES = 0x40
    WRITE_NAME = 0x80


class DataType:
    """DataAccess.data_type / ReadyForData's implied data type."""

    READY_FOR_DATA = 0x00
    TEXT_DATA = 0x01
    DISC_INFO = 0x02
    DISC_TOC = 0x04
    DISC_USERFILES = 0x08
    DISC_GENRE = 0x10
    DISC_LISTING = 0x20


DATA_TYPE_TO_REPLY_COMMAND = {
    DataType.TEXT_DATA: CMD_TEXT_DATA,  # (or CMD_LONG_TEXT_DATA, see notes)
    DataType.DISC_INFO: CMD_DISC_INFO,
    DataType.DISC_TOC: CMD_DISC_TOC,
    DataType.DISC_USERFILES: CMD_DISC_USERFILES,
    DataType.DISC_GENRE: CMD_DISC_GENRE,
    DataType.DISC_LISTING: CMD_DISC_LISTING,
}


class InfoType:
    """Sub-type of TextData/LongTextData, used when data_type == TEXT_DATA."""

    DISC_NAMES = 0x00
    TRACK_NAMES = 0x01
    ARTIST_NAME = 0x02
    USERFILE_NAMES = 0x07


GENRES = {
    0x00: "Unassigned",
    0x01: "Unknown",
    0x02: "Adult Contemporary",
    0x03: "Alternative Rock",
    0x04: "Children's",
    0x05: "Classical",
    0x06: "Christian",
    0x07: "Country",
    0x08: "Dance",
    0x09: "Easy Listening",
    0x0A: "Erotic",
    0x0B: "Folk",
    0x0C: "Gospel",
    0x0D: "Hip Hop",
    0x0E: "Jazz",
    0x0F: "Latin",
    0x10: "Musical",
    0x11: "New Age",
    0x12: "Opera",
    0x13: "Operetta",
    0x14: "Pop",
    0x15: "Rap",
    0x16: "Reggae",
    0x17: "Rock",
    0x18: "Rhythm & Blues",
    0x19: "Sound Effects",
    0x1A: "Soundtrack",
    0x1B: "Spoken Word",
    0x1C: "World Music",
}

# Reverse lookup for the Disc Data tab's Genre dropdown (Custom column):
# maps a genre display name back to its numeric code. GENRES has no
# duplicate names, so this is a clean 1:1 inverse.
GENRE_NAME_TO_CODE = {name: code for code, name in GENRES.items()}


class Format:
    NO_CDTEXT = 0x00
    HAS_CDTEXT = 0x13


def format_name(value: int) -> str:
    return {Format.NO_CDTEXT: "No CD-Text", Format.HAS_CDTEXT: "Has CD-Text"}.get(
        value, f"Unknown (0x{value:02X})"
    )


USERFILE_BITS = {1 << i: f"Userfile #{i + 1}" for i in range(8)}


def userfile_list(bitmask: int) -> list[str]:
    return [name for bit, name in USERFILE_BITS.items() if bitmask & bit]


class Mode:
    TRACK = 0x00
    TRACK_RANDOM_ONE = 0x01
    TRACK_RANDOM_ALL = 0x02
    PROGRAM = 0x03
    BEST = 0x04
    GENRE = 0x05
    GENRE_RANDOM_ALL = 0x06
    USERFILE = 0x07
    USERFILE_RANDOM_ONE = 0x08
    USERFILE_RANDOM_ALL = 0x09


MODE_NAMES = {
    Mode.TRACK: "Track Mode",
    Mode.TRACK_RANDOM_ONE: "Track Mode (Random One)",
    Mode.TRACK_RANDOM_ALL: "Track Mode (Random All)",
    Mode.PROGRAM: "Program Mode",
    Mode.BEST: "Best Mode",
    Mode.GENRE: "Music Type Mode",
    Mode.GENRE_RANDOM_ALL: "Music Type Mode (Random All)",
    Mode.USERFILE: "Userfile Mode",
    Mode.USERFILE_RANDOM_ONE: "Userfile Mode (Random One)",
    Mode.USERFILE_RANDOM_ALL: "Userfile Mode (Random All)",
}


# Per cd_changemode.html / cd_infoevent.html: ChangeMode's (and
# InfoEvent's) `param` byte is a genre in the Music Type modes and a
# userfile in the Userfile modes; unused otherwise.
GENRE_MODES = frozenset({Mode.GENRE, Mode.GENRE_RANDOM_ALL})
USERFILE_MODES = frozenset({Mode.USERFILE, Mode.USERFILE_RANDOM_ONE, Mode.USERFILE_RANDOM_ALL})


def userfile_param(number: int) -> int:
    """ChangeMode's `param` for userfile #number (1..8).

    The docs only say param "is a userfile", not whether that's a
    number or a bit. It's a bit, the same encoding as InfoEvent's
    `userfiles` field (USERFILE_BITS): userfile #1 -> 0x01, #8 -> 0x80.
    CONFIRMED on real hardware (v1.6.2): with slot 2 tagged only as
    userfile #3 (InfoEvent userfiles=0x04), ChangeMode(Userfile, 0x04)
    was accepted, reported back as param=0x04, and played slot 2. Read
    as a number, 0x04 would have been userfile #4, which slot 2 isn't in.
    """
    if not 1 <= number <= 8:
        raise ValueError(f"userfile number must be 1..8, got {number}")
    return 1 << (number - 1)


def describe_mode_param(mode: int, param: int) -> str:
    """Human-readable InfoEvent `param`, always including the raw byte
    (the userfile encoding in particular is unconfirmed -- see
    userfile_param).

    Genre modes deliberately show only the raw byte: on real hardware
    (2026-09-22) a ChangeMode(Music Type, Rock=0x17) that the changer
    accepted came back in InfoEvent with param=0x00, not 0x17 -- so
    InfoEvent's param isn't the selected genre, despite the docs, and
    naming it would show a misleading "Unassigned".
    """
    if mode in USERFILE_MODES:
        names = userfile_list(param)
        return f"{', '.join(names) or 'none'} (0x{param:02X})"
    return f"0x{param:02X}"


class State:
    STOPPED = 0x40
    STOPPING = 0x50
    CHANGING = 0x60
    PLAYING = 0x70
    PAUSED = 0x80
    FAST_FWD = 0x90
    FAST_BACK = 0xA0


STATE_NAMES = {
    State.STOPPED: "Stopped",
    State.STOPPING: "Stopping",
    State.CHANGING: "Changing",
    State.PLAYING: "Playing",
    State.PAUSED: "Paused",
    State.FAST_FWD: "Fast Forwarding",
    State.FAST_BACK: "Fast Backwarding",
}


class ActionCommand:
    """DoAction.action -- 16-bit codes, sent little-endian (LSB first)."""

    FAST_BACKWARD = 0x06A0  # repeatable: keep sending while held
    FAST_FORWARD = 0x07A0  # repeatable: keep sending while held
    REPEAT_MODE = 0xCCA0
    STOP = 0xC9A0
    PLAY_PAUSE = 0xCBA0
    PREVIOUS_TRACK = 0xCEA0
    NEXT_TRACK = 0xCFA0
    RANDOM_MODE = 0xD4A1
    FINISH_REPEATABLE = 0xFFFF  # cancels FAST_FORWARD / FAST_BACKWARD


ACTION_COMMAND_NAMES = {
    ActionCommand.FAST_BACKWARD: "Fast Backward",
    ActionCommand.FAST_FORWARD: "Fast Forward",
    ActionCommand.REPEAT_MODE: "Repeat Mode",
    ActionCommand.STOP: "Stop",
    ActionCommand.PLAY_PAUSE: "Play/Pause",
    ActionCommand.PREVIOUS_TRACK: "Previous Track",
    ActionCommand.NEXT_TRACK: "Next Track",
    ActionCommand.RANDOM_MODE: "Random Mode",
    ActionCommand.FINISH_REPEATABLE: "Finish Repeatable",
}


# --------------------------------------------------------------------------
# Wire framing
# --------------------------------------------------------------------------


def compute_checksum(command: int, data: bytes) -> int:
    """checksum = NOT( sum(command, len_lo, len_hi, data) - 1 ), low byte.

    Algebraically identical to a two's-complement negation of the sum, which
    is how it's implemented (avoids any ambiguity about intermediate
    wraparound and matches the described procedure exactly for the inputs
    the protocol actually uses).
    """
    n = len(data)
    total = command + (n & 0xFF) + ((n >> 8) & 0xFF) + sum(data)
    return (~(total - 1)) & 0xFF


def encode_frame(command: int, data: bytes = b"") -> bytes:
    """Build the STX + command + length + data + checksum frame bytes
    (NOT including the surrounding ENQ/ACK/EOT flow-control bytes)."""
    n = len(data)
    chk = compute_checksum(command, data)
    return bytes([STX, command]) + struct.pack("<H", n) + data + bytes([chk])


# --------------------------------------------------------------------------
# Payload encoders (requests we send)
# --------------------------------------------------------------------------


def encode_handshake(identifier: str = "I'm PC") -> bytes:
    return identifier.encode("ascii", errors="replace")


def encode_do_action(action_command: int) -> bytes:
    return struct.pack("<H", action_command & 0xFFFF)


def encode_change_disc(slot: int, track: int = 0, begin: bool = True) -> bytes:
    return struct.pack("<HBB", slot & 0xFFFF, track & 0xFF, 1 if begin else 0)


def encode_change_mode(mode: int, param: int = 0) -> bytes:
    return struct.pack("<BB", mode & 0xFF, param & 0xFF)


def encode_data_access(
    action: int,
    data_type: int,
    slot: int = 0,
    info_type: int = 0,
    genre: int = 0,
    unknown: int = 0,
) -> bytes:
    """action/data_type/slot/unknown/info_type/genre, per cd_dataaccess.html."""
    return struct.pack(
        "<BBHBBB",
        action & 0xFF,
        data_type & 0xFF,
        slot & 0xFFFF,
        unknown & 0xFF,
        info_type & 0xFF,
        genre & 0xFF,
    )


# --------------------------------------------------------------------------
# Write-payload encoders
# --------------------------------------------------------------------------
#
# These build the actual data frame that gets sent AFTER a DataAccess
# write-action request, once the changer asks for it (via ReadyForData)
# by opening a SEPARATE, second transaction -- see pclink_link.py's
# send_write() and CHANGELOG.md's v1.2.0 entry for the full choreography
# history (a first "inline follow-up" hypothesis was tried and disproved
# before landing on this one). DataAccess itself only carries
# action/data_type/slot/info_type/genre -- no room for the actual name
# text, genre assignment, userfile mask, or program listing -- so the
# real content rides in this second frame, using the same payload shape
# the corresponding *read* reply already uses (cd_textdata.html /
# cd_discgenre.html / cd_discuserfiles.html / cd_disclisting.html).
#
# encode_text_data (WRITE_NAME) is CONFIRMED against real hardware: a
# write followed by an independent read-back matched exactly, for both a
# disc name and ten track names. The others (encode_disc_genre/
# encode_disc_userfiles/encode_disc_listing) mirror their read-side
# decoders field-for-field and round-trip through them (see
# test_write_feature.py), but that only proves internal self-consistency
# -- they're not wired up to any UI yet, so they remain untested against
# real hardware.
#
# WRITE_ACTION_DATA_TYPE maps each write Action to the DataType value that
# goes in the *initiating* DataAccess request (mirrors DATA_TYPE_TO_REPLY_
# COMMAND's reply-command mapping above, and cd_dataaccess.html's own
# statement that data_type means "type of data sent/received" -- for a
# write, that's still the type of the data being written).

WRITE_ACTION_DATA_TYPE = {
    Action.WRITE_NAME: DataType.TEXT_DATA,
    Action.SET_DISC_GENRE: DataType.DISC_GENRE,
    Action.SET_USERFILES: DataType.DISC_USERFILES,
    Action.WRITE_PROGRAM: DataType.DISC_LISTING,
}


def encode_text_data(
    slot: int,
    index: int,
    text: str,
    userfiles: int = 0,
    info_type: int = InfoType.DISC_NAMES,
    genre: int = 0,
    fmt: int = Format.NO_CDTEXT,
) -> bytes:
    """Write payload for Action.WRITE_NAME (data_type=TEXT_DATA), per
    cd_textdata.html: slot, index (track number, or 0 for the disc name
    itself -- matches how `index` is already used on the read side, see
    decode_text_data / _cache_name in pclink_app.py), userfiles,
    info_type, genre, format, then the name text with no explicit length
    prefix (the frame's own length field covers it, same assumption
    decode_text_data already makes when reading a reply). This is the
    frame the app sends as CMD_TEXT_DATA, as its own separate transaction,
    once the changer's ReadyForData asks for it -- see pclink_link.py.

    CONFIRMED against real hardware: a disc name and ten track names
    written this way were independently read back afterward and matched
    exactly.

    Default info_type is DISC_NAMES; pass InfoType.TRACK_NAMES with the
    track number as `index` to write a track name instead."""
    return (
        struct.pack(
            "<HBBBBB",
            slot & 0xFFFF,
            index & 0xFF,
            userfiles & 0xFF,
            info_type & 0xFF,
            genre & 0xFF,
            fmt & 0xFF,
        )
        + text.encode("ascii", errors="replace")
    )


def encode_long_text_data(
    slot: int,
    track: int,
    text: str,
    info_type: int = InfoType.DISC_NAMES,
    fmt: int = Format.NO_CDTEXT,
) -> bytes:
    """Alternate write payload for a WRITE_NAME transaction, shaped like
    cd_longtextdata.html (CMD_LONG_TEXT_DATA / 0xFD) instead of TextData.
    The three fields the source docs leave as bare "unknown" are sent as
    0x00. NOT what real hardware turned out to need: encode_text_data/
    CMD_TEXT_DATA is the confirmed-working shape for writing names (see
    WRITE_ACTION_DATA_TYPE and pclink_app.py's write button); this is
    kept around unused in case a different unit's write path ever needs
    the Long shape instead."""
    return (
        struct.pack(
            "<HBBBBBB",
            slot & 0xFFFF,
            track & 0xFF,
            0,
            info_type & 0xFF,
            0,
            fmt & 0xFF,
            0,
        )
        + text.encode("ascii", errors="replace")
    )


def encode_disc_genre(slot: int, genre: int) -> bytes:
    """Write payload for Action.SET_DISC_GENRE, per cd_discgenre.html --
    same shape decode_disc_genre reads. Not yet wired up to the UI (the
    Disc Data tab has no genre picker); the encoder exists for whoever
    adds that control next."""
    return struct.pack("<HB", slot & 0xFFFF, genre & 0xFF)


def encode_disc_userfiles(slot: int, userfiles: int) -> bytes:
    """Write payload for Action.SET_USERFILES, per cd_discuserfiles.html.
    `userfiles` is the same bit-or'd mask decode_disc_userfiles reads (see
    USERFILE_BITS). Not yet wired up to the UI."""
    return struct.pack("<HB", slot & 0xFFFF, userfiles & 0xFF)


def encode_disc_listing(items: list[tuple[int, int]]) -> bytes:
    """Write payload for Action.WRITE_PROGRAM, per cd_disclisting.html: a
    length byte followed by that many slot_track pairs (short slot, byte
    track -- cd_types.html's "slot_track" type). A track value of 0xAA
    means "all tracks for the disc", per that doc's own note. Not yet
    wired up to the UI (no program editor exists yet)."""
    out = bytearray([len(items) & 0xFF])
    for slot, track in items:
        out += struct.pack("<HB", slot & 0xFFFF, track & 0xFF)
    return bytes(out)


# --------------------------------------------------------------------------
# Payload decoders (replies / spontaneous events we receive)
# --------------------------------------------------------------------------


def _read_cstr(data: bytes, offset: int) -> str:
    """Text fields aren't explicitly length-prefixed in the docs; treat the
    remainder of the payload as a (possibly NUL-terminated) ASCII string."""
    raw = data[offset:]
    if b"\x00" in raw:
        raw = raw.split(b"\x00", 1)[0]
    return raw.decode("ascii", errors="replace")


def decode_handshake(data: bytes) -> dict:
    return {"identifier": data.decode("ascii", errors="replace")}


def decode_disc_info(data: bytes) -> dict:
    slot, unknown, track_count, fmt = struct.unpack("<HBBB", data[:5])
    return {
        "slot": slot,
        "unknown": unknown,
        "track_count": track_count,
        "format": fmt,
        "format_name": format_name(fmt),
    }


def _bcd_to_int(byte: int) -> int:
    """Decode a byte as two binary-coded-decimal digits (high nibble = tens,
    low nibble = ones). Confirmed against real hardware: toc_time's
    minutes/seconds/frames fields are BCD, not plain binary -- e.g. the
    raw byte 0x49 means 49, not 73. This isn't stated in cd_types.html
    (which just says "byte minutes" / "byte seconds"), but BCD-encoded MSF
    timecodes are the Red Book CD standard, and decoding these bytes as
    plain integers produces impossible values (seconds > 59) while BCD
    decoding produces valid, monotonically increasing track start times."""
    return (byte >> 4) * 10 + (byte & 0x0F)


def decode_disc_toc(data: bytes) -> dict:
    slot, page_num, fmt, first_track, last_track = struct.unpack("<HBBBB", data[:6])
    offset = 6
    n_entries = last_track - first_track + 2 if last_track >= first_track else 0
    times = []
    for _ in range(max(n_entries, 0)):
        if offset + 3 > len(data):
            break
        minutes, seconds, frames = struct.unpack("<BBB", data[offset : offset + 3])
        times.append({
            "minutes": _bcd_to_int(minutes),
            "seconds": _bcd_to_int(seconds),
            "frames": _bcd_to_int(frames),
        })
        offset += 3
    return {
        "slot": slot,
        "page_num": page_num,
        "format": fmt,
        "format_name": format_name(fmt),
        "first_track": first_track,
        "last_track": last_track,
        "track_times": times,
    }


# --------------------------------------------------------------------------
# CDDB / gnudb.org disc ID
# --------------------------------------------------------------------------
#
# DiscTOC's per-track "toc_time" entries are Red Book MSF timecodes
# (minutes:seconds:frames, 75 frames/second) -- exactly the input the
# classic CDDB1 disc-ID algorithm needs. Per cd_disctoc.html, a DiscTOC
# reply carries (last_track - first_track + 2) toc_time entries: one per
# track PLUS one extra trailing entry. That extra entry lines up exactly
# with what the CDDB algorithm calls the "lead-out" (the disc's total
# playable length marker) -- assumed here since the docs don't name it
# explicitly, but the count matches perfectly and this is the standard
# shape of a Red Book TOC. Not yet confirmed by cross-checking a computed
# ID against a known gnudb.org lookup.
#
# gnudb.org speaks the same "CDDB1"/freedb query protocol; a disc ID
# computed this way is what you'd send as the `hexadecimal disc ID`
# argument to its query command.


def _toc_time_to_frames(t: dict) -> int:
    """MSF -> absolute frame count (75 frames/second)."""
    return (t["minutes"] * 60 + t["seconds"]) * 75 + t["frames"]


def _cddb_digit_sum(n: int) -> int:
    total = 0
    while n > 0:
        total += n % 10
        n //= 10
    return total


def calculate_cddb_discid(track_times: list) -> dict | None:
    """track_times: the full list of toc_time entries from a DiscTOC reply
    (tracks, in order, followed by the trailing lead-out entry -- i.e.
    exactly what decode_disc_toc's "track_times" field contains once all
    pages of a disc's TOC have been collected). Returns None if there
    isn't at least one track plus the trailing lead-out entry to work with.

    Returns a dict with:
      - discid: the 8-hex-digit CDDB1 disc ID string
      - num_tracks, total_seconds: the other two query parameters gnudb.org
        needs (alongside each track's frame offset -- see track_offsets)
      - track_offsets: each track's starting frame offset (75 frames/sec),
        the per-track values a CDDB-style query also needs to send
    """
    if len(track_times) < 2:
        return None
    track_offsets = [_toc_time_to_frames(t) for t in track_times[:-1]]
    leadout_frames = _toc_time_to_frames(track_times[-1])

    checksum = sum(_cddb_digit_sum(off // 75) for off in track_offsets) % 0xFF
    total_seconds = (leadout_frames // 75) - (track_offsets[0] // 75)
    n_tracks = len(track_offsets)

    discid_value = (checksum << 24) | (total_seconds << 8) | n_tracks
    return {
        "discid": f"{discid_value:08x}",
        "num_tracks": n_tracks,
        "total_seconds": total_seconds,
        "track_offsets": track_offsets,
        "leadout_frames": leadout_frames,
    }


def decode_disc_userfiles(data: bytes) -> dict:
    slot, userfiles = struct.unpack("<HB", data[:3])
    return {"slot": slot, "userfiles": userfiles, "userfile_names": userfile_list(userfiles)}


def decode_disc_genre(data: bytes) -> dict:
    slot, genre = struct.unpack("<HB", data[:3])
    return {"slot": slot, "genre": genre, "genre_name": GENRES.get(genre, f"0x{genre:02X}")}


def decode_ready_for_data(data: bytes) -> dict:
    """Per cd_readyfordata.html: short slot + byte info_type (3 bytes).
    NOT what real hardware actually sends, though -- confirmed on a real
    CD-425M across many WRITE_NAME attempts (disc name and every track
    name, both DISC_NAMES and TRACK_NAMES info_type requests): the
    ReadyForData payload was exactly ONE byte, always `0x01`, not the
    documented three. It doesn't vary with slot or info_type, so it reads
    as a bare "ready" flag rather than an echo of anything in the
    request. Handled leniently here rather than erroring out
    (decode_payload's try/except would otherwise turn every occurrence
    into an opaque {"error": ...} in the log). Reported as `raw_byte`
    rather than assumed to always be 1; a documented-shape (3-byte)
    ReadyForData is still decoded the documented way if one ever shows
    up."""
    if len(data) >= 3:
        slot, info_type = struct.unpack("<HB", data[:3])
        return {"slot": slot, "info_type": info_type}
    if len(data) == 1:
        return {"raw_byte": data[0]}
    return {"raw": data.hex(" ")}


def decode_disc_listing(data: bytes) -> dict:
    length = data[0]
    items = []
    offset = 1
    for _ in range(length):
        slot, track = struct.unpack("<HB", data[offset : offset + 3])
        items.append({"slot": slot, "track": track, "all_tracks": track == 0xAA})
        offset += 3
    return {"length": length, "items": items}


def decode_info_event(data: bytes) -> dict:
    # `userfiles` is CONFIRMED as the documented bitmask (v1.6.2): tagging
    # slot 2 as userfile #3 on the remote made it read 0x04.
    # Byte 5 is documented as `num_tracks`, but it's the current disc's
    # GENRE -- CONFIRMED on real hardware (v1.6.5): slot 2 (Rock, 13
    # tracks) reads 0x17 and slot 4 (Alternative Rock, 12 tracks) reads
    # 0x03, including in plain Track Mode where no genre was selected.
    # Always matches that slot's DiscGenre reply.
    slot, track, program, genre, userfiles, param, mode, repeat = struct.unpack(
        "<HBBBBBBB", data[:9]
    )
    return {
        "slot": slot,
        "track": track,
        "program": program,
        "genre": genre,
        "genre_name": GENRES.get(genre, f"0x{genre:02X}"),
        "userfiles": userfiles,
        "userfile_names": userfile_list(userfiles),
        "param": param,
        "param_desc": describe_mode_param(mode, param),
        "mode": mode,
        "mode_name": MODE_NAMES.get(mode, f"0x{mode:02X}"),
        "repeat": bool(repeat),
    }


def decode_state_event(data: bytes) -> dict:
    state = data[0]
    return {"state": state, "state_name": STATE_NAMES.get(state, f"0x{state:02X}")}


def decode_disc_event(data: bytes) -> dict:
    # Documented as a single byte slot (unlike the "short slot" used
    # elsewhere) -- kept exactly as specified.
    return {"slot": data[0]}


def decode_door_event(data: bytes) -> dict:
    return {"door_open": bool(data[0])}


def decode_text_data(data: bytes) -> dict:
    slot, index, userfiles, info_type, genre, fmt = struct.unpack("<HBBBBB", data[:7])
    text = _read_cstr(data, 7)
    return {
        "slot": slot,
        "index": index,
        "userfiles": userfiles,
        "userfile_names": userfile_list(userfiles),
        "info_type": info_type,
        "genre": genre,
        "genre_name": GENRES.get(genre, f"0x{genre:02X}"),
        "format": fmt,
        "format_name": format_name(fmt),
        "text": text,
    }


def decode_long_text_data(data: bytes) -> dict:
    slot, track, _u1, info_type, _u2, fmt, _u3 = struct.unpack("<HBBBBBB", data[:8])
    text = _read_cstr(data, 8)
    return {
        "slot": slot,
        "track": track,
        "info_type": info_type,
        "format": fmt,
        "format_name": format_name(fmt),
        "text": text,
    }


DECODERS: dict[int, Callable[[bytes], dict]] = {
    CMD_HANDSHAKE: decode_handshake,
    CMD_DISC_INFO: decode_disc_info,
    CMD_DISC_TOC: decode_disc_toc,
    CMD_DISC_USERFILES: decode_disc_userfiles,
    CMD_DISC_GENRE: decode_disc_genre,
    CMD_READY_FOR_DATA: decode_ready_for_data,
    CMD_DISC_LISTING: decode_disc_listing,
    CMD_INFO_EVENT: decode_info_event,
    CMD_STATE_EVENT: decode_state_event,
    CMD_DISC_EVENT: decode_disc_event,
    CMD_DOOR_EVENT: decode_door_event,
    CMD_TEXT_DATA: decode_text_data,
    CMD_LONG_TEXT_DATA: decode_long_text_data,
}


def decode_payload(command: int, data: bytes) -> dict:
    decoder = DECODERS.get(command)
    if decoder is None:
        return {"raw": data.hex(" ")}
    try:
        return decoder(data)
    except struct.error as exc:
        return {"error": f"short/malformed payload: {exc}", "raw": data.hex(" ")}


@dataclass
class Frame:
    command: int
    data: bytes
    payload: dict = field(default_factory=dict)

    @property
    def command_name(self) -> str:
        return COMMAND_NAMES.get(self.command, f"0x{self.command:02X}")

    def __repr__(self) -> str:
        return f"Frame({self.command_name}, {self.payload})"
