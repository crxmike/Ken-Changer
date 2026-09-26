#!/usr/bin/env python3
"""
pclink_app.py
=============
A desktop app (Windows/macOS/Linux) for controlling a Kenwood CD-425M (also
works with the CD-4700M / CD-4260M, which share the same command set) over
its PC-Link serial port, via a USB-to-RS232 null-modem adapter.

Run:
    pip install pyserial pillow
    python pclink_app.py

See README.md for wiring notes and a summary of what is/isn't confirmed
against real hardware yet.
"""

from __future__ import annotations

import datetime
import json
import os
import queue
import threading
import time
import unicodedata
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None

import pclink_protocol as proto
from pclink_link import (
    PCLinkConnection, PCLinkError, PCLinkTimeout, PCLinkNak, PCLinkWriteUnconfirmed,
    PCLinkTextStream,
)
from pclink_protocol import Frame
import gnudb_client
import album_art
import library_backup
import library_browser


MODE_CHANGE_TIMEOUT_MS = 5000  # how long to wait for an InfoEvent after Set Mode
REPEAT_INTERVAL = 0.3  # seconds between repeated FF/FB DoAction sends while held

# v1.3.0 -- writing disc/track names (Action.WRITE_NAME) via the Disc
# Data tab's "Write to Changer" button, confirmed against real hardware
# (see CHANGELOG.md).
# v1.4.0 -- reading DiscGenre into the status panel + Disc Data tab, and
# writing it back via the same "Write to Changer" button (Action.
# SET_DISC_GENRE). NOT yet confirmed against real hardware -- per
# CLAUDE.md's hard rule, this stays "should work" until the user reports
# back with a raw-byte log. WRITE_PROGRAM/SET_USERFILES still share the
# same send_write() plumbing but have no UI yet. gnudb.org querying is
# wired up but its live round-trip is also still unconfirmed -- see
# README.md's "Honest gaps" section.
# v1.4.1 -- fixed a real bug caught by the user's first real-hardware
# genre-write attempt: the initiating DataAccess(SET_DISC_GENRE) request
# went out with its own `genre` field left at 0 ("Unassigned") instead of
# the target value, because the call site never passed genre=genre_code.
# See build_genre_write_frames() and CHANGELOG.md. STILL not confirmed
# working -- the same log also showed the follow-up DiscGenre frame
# getting an immediate EOT instead of ACK, an open question this fix
# doesn't address; needs another real-hardware test.
# v1.4.2 -- the user retested v1.4.1's fix: the request now correctly
# carried the target genre, but the genre still didn't change, and the
# follow-up DiscGenre frame was rejected with an immediate EOT again
# (twice in a row now). Switched the genre write to a single ordinary
# DataAccess transaction with NO follow-up frame -- since genre already
# fits entirely inside DataAccess's own payload, unlike a name's
# arbitrary text. NOT yet tried against real hardware.
# v1.4.3 -- v1.4.2's single-transaction attempt was tried on real
# hardware: the request went out correctly (genre byte verified on the
# wire), the changer ACK'd it and sent ReadyForData same as before, and
# the transaction closed cleanly -- but genre STILL didn't change. Since
# ReadyForData means "send me the payload now" and we deliberately sent
# nothing, this doesn't mean genre doesn't need a follow-up frame -- it
# confirms it does, and the two rejected follow-up attempts in v1.4.0/
# v1.4.1 must have had the wrong shape, not been unwanted altogether.
# Read cd_types.html directly: Action.SET_DISC_GENRE/DataType.DISC_GENRE
# and every proto.GENRES code already match the docs exactly, ruling out
# a wrong-enum-value explanation. New lead instead: cd_textdata.html
# shows TextData's payload (the one WRITE_NAME already uses, confirmed
# working) has its own `genre` field alongside `text`. Genre writing is
# now folded into a disc-name WRITE_NAME write (see
# merge_genre_into_write_items()) instead of the standalone
# Action.SET_DISC_GENRE path (three separate real-hardware attempts at
# that have now failed in three different ways). NOT yet tried.
# v1.5.0 -- v1.4.3's approach (genre folded into a disc-name WRITE_NAME
# write) CONFIRMED against real hardware: writing genre "Rock" to slot 2
# (via the Disc Data tab's Genre row, Custom Disc Name left blank so the
# app reused the currently-known name) went out exactly like an ordinary
# confirmed WRITE_NAME write, and an independent re-read afterward showed
# genre=23/"Rock" for the disc name AND every track (the changer's
# TextData replies apparently always echo the disc's current genre in
# every reply, not just the one it was written through, which is
# consistent with genre being a disc-level, not per-track, property).
# The standalone Action.SET_DISC_GENRE path (v1.4.0-v1.4.2) never
# worked; genre now always writes through Action.WRITE_NAME. See
# CHANGELOG.md's v1.5.0 entry for the full four-attempt history.
# v1.5.1 -- real bug, found by the user writing a track name shortly
# after a genre write: writing ANY TextData (a track name, not just the
# disc name) with its follow-up frame's genre byte left at the old
# default of 0 SILENTLY RESET the disc's just-written genre ("Rock")
# back to "Unassigned". Confirms genre is a disc-level value that EVERY
# TextData write sets on this unit, not just the one it happened to be
# attached to. Fixed: merge_genre_into_write_items() now attaches the
# SAME genre byte -- the newly selected one, or the previously-known one
# if the user didn't touch the Genre dropdown -- to every write item
# (disc name AND every track), so an ordinary name-only write can no
# longer accidentally erase an existing genre. CONFIRMED against real
# hardware: writing a new genre ("Folk") then a track name in the same
# session, genre stayed "Folk" throughout -- see CHANGELOG.md.
# v1.6.0 -- Play Mode selector on the Control tab (ChangeMode), plus a
# "Mode Param" status row showing InfoEvent's raw param byte. NOT yet
# tried against real hardware -- see build_change_mode_request().
# v1.6.1 -- first real-hardware session: Music Type and Userfile #1
# switched modes; Best and Program (nothing stored) were ACK'd then
# silently ignored, so Set Mode now logs a notice if no InfoEvent reports
# the new mode within MODE_CHANGE_TIMEOUT_MS. InfoEvent's param came back
# 0x00 in Music Type (Rock) mode, so the Mode Param row no longer names a
# genre from it. Userfile number-vs-bit encoding still open (#1/#2 are
# the same byte either way).
# v1.6.2 -- userfile param encoding CONFIRMED as a bit (#3 -> 0x04) on
# real hardware. No code change; docs/tests only.
# v1.6.3 -- Best mode is ignored by ChangeMode even with a Best list
# stored (remote-started Best played fine), so "nothing to play" isn't
# the reason. Reworded the didn't-take notice; working theory (from the
# owner's manual) is that Best/Program only switch while stopped.
# v1.6.4 -- stop theory disproved: Best was ignored while Stopped too.
# Best is now left out of the Play Mode dropdown (CHANGE_MODE_UNSUPPORTED).
# v1.6.5 -- InfoEvent's documented `num_tracks` byte CONFIRMED to be the
# disc's genre (non-Rock disc read 0x03 = Alternative Rock); decoded as
# `genre` now. Music Type mode confirmed with two genres.
# v1.6.6 -- Program mode CONFIRMED not settable via ChangeMode either (a
# stored program plays from the remote, but 4 app requests -- playing and
# stopped -- were all ignored). Left out of the dropdown like Best.
# v1.6.7 -- all five random variants CONFIRMED ignored via ChangeMode too;
# the Random button reaches them instead. Dropdown is now Track / Music
# Type / Userfile only, with an on-screen hint. Placeholder track titles
# (text that's only control characters, e.g. '\x01') are no longer cached
# as names.
# v1.7.0 -- read-only "Userfiles & Program" tab: userfile names + which
# discs are in each userfile, and the stored program (DiscListing). NOT
# yet tried against real hardware.
# v1.7.1 -- confirmed on real hardware; fixed userfile names being looked
# up by userfile number when the changer keys them by userfile BIT.
# v1.8.0 -- writing on the Userfiles & Program tab: rename a userfile
# (WRITE_NAME, info_type 7), set a disc's userfiles (folded into a
# disc-name WRITE_NAME via TextData's userfiles byte, like genre), and a
# program editor (WRITE_PROGRAM + DiscListing). Every name write now also
# carries the disc's known userfile mask instead of 0, reading genre/
# userfiles first if they aren't known.
# v1.8.1 -- all three writes CONFIRMED on real hardware. Writing a
# program also puts the changer into Program mode and starts it playing;
# switching back to Track mode clears the program (as the manual says).
# v1.8.2 -- CONFIRMED that name writes keep a disc's userfiles (a track
# name written on a disc in #1-#3 left it in #1-#3). Docs/tests only.
# v1.8.3 -- gnudb.org lookup hardening, NOT yet tried against the live
# server: a lone inexact (211) match goes to the picker instead of loading
# silently; HTTP 403/429/503 is reported as rate-limited (and isn't retried
# over HTTPS); an HTML/non-CDDB body gets a clear error; Write to Changer
# warns when the disc name is over 25 characters. Tests:
# test_gnudb_client.py.
# v1.8.4 -- the gnudb.org round-trip is CONFIRMED live (v1.8.3 session:
# no exact match for our DiscID 930c540c, 3 inexact candidates, picked
# one, read 12 tracks, wrote them to the changer). "Copy gnudb -> Custom"
# now folds text to ASCII (curly apostrophes had been stored as '?'), and
# the log lists each candidate's DiscID so ours can be compared.
# v1.8.5 -- ASCII folding CONFIRMED on real hardware (tracks 4 and 10 of
# slot 1 now store a plain apostrophe). Candidate DiscIDs logged: none is
# a standard CDDB1 ID for this 12-track disc. Docs/tests only.
# v1.8.6 -- exact gnudb DiscID matches aren't expected on this changer
# (user tested several discs: all inexact). The TOC's frames are always
# 0, so the inexact-match log line now says that's normal.
# v1.8.7 -- "Copy gnudb -> Custom" matches gnudb's genre to the changer's
# genre list ignoring case, hyphens and '&'/'and' ("rock" -> Rock,
# "hip-hop" -> Hip Hop). Different words still stay blank.
# v1.9.0 -- cover art on the Disc Data tab after a gnudb read, looked up
# on iTunes by gnudb's artist/album (album_art.py). Internet-only, never
# touches the changer. Not yet tried in the app.
# v1.9.1 -- the cover now comes from the gnudb entry's own "# Cover:"
# links (coverartarchive.org) first, with iTunes as the fallback. Needs
# Pillow for the JPEGs. Not yet tried in the app.
# v1.9.2 -- cover art CONFIRMED on screen (3 discs, all from gnudb's own
# links). Docs/tests only, plus DiscID evidence against v1.8.6's rounding
# theory (see CHANGELOG.md).
# v1.10.0 -- Backup tab: export every slot's names, genre and userfiles
# (plus userfile names and the program) to a .json backup or .csv catalog,
# and restore a .json backup, writing only what differs and re-reading each
# slot to check it (library_backup.py). Reads and writes use only paths
# already CONFIRMED; the 200-slot walk and the restore itself are NOT yet
# tried on real hardware.
# v1.10.1 -- first real export: works, but DiscInfo reports 99 tracks for
# every disc not played since power-on (CONFIRMED), which v1.10.0 saved
# as-is and restore would have read as "different disc". 99 is now
# "unknown" (null in the file, never a mismatch). Also, a track-names
# read starts with an index-0 frame repeating the disc name; that went
# into backups as track "0", which restore rejected. Both fixed, and
# v1.10.0 files still load. The fixes aren't tried on real hardware yet.
# v1.10.2 -- export and restore CONFIRMED on real hardware (v1.10.1): two
# hand-edited track names were written back and verified, unplayed (99)
# slots matched instead of being skipped, and a second restore wrote
# nothing. Docs/tests only.
# v1.11.0 -- a Library tab: browse every disc (from a changer scan, the
# Backup export, or a backup file), search disc/track names, filter by
# genre/userfile, and play a disc or open it on the Disc Data tab. The
# last scan is kept in library_cache.json.
# v1.11.1 -- the Library tab CONFIRMED on real hardware (scan, play a
# track, rescan a disc). Docs/tests only.
# v1.12.0 -- the Library tab's "Userfiles" menu (and right-click): add a
# disc to a userfile or take it out, using the confirmed membership
# write, after a fresh read of the slot.
# v1.12.1 -- v1.12.0 CONFIRMED on real hardware (add #3 to slot 1 and
# take it out again). The Userfiles & Program tab's table is filled in
# from the Library's saved scan (marked "*") for discs the changer hasn't
# reported this session, and "Read Userfiles for Known Discs" uses the
# scan's slots too. CONFIRMED on real hardware.
# v1.12.2 -- CONFIRMED that writing an empty program clears it (and drops
# the changer from Program mode back to Track mode). The Write Program
# dialog now says that for an empty program. Docs/tests otherwise.
# v1.12.3 -- the artist-name text type (info_type 0x02) isn't supported
# on the CD-425M (probe_artist_name.py, real hardware). pclink_link.py now
# raises PCLinkRejected when the changer answers a frame with EOT instead
# of ACK; before, a refused write was logged as done.
# v1.12.11 -- no name writes to a CD-Text disc (its titles come from the
# disc); its genre and userfiles ride on a re-sent track 1 name instead of
# the disc name, which can't be read while it's in the drive.
APP_VERSION = "1.12.11"

# The Library tab's last changer scan (v1.11.0), next to the app.
LIBRARY_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  library_browser.CACHE_FILENAME)


def gather_disc_data_write_items(disc_data_rows: list) -> list:
    """Pure helper (no Tkinter/link dependency, so it's directly
    unit-testable -- see test_write_feature.py): picks out every
    non-empty "Custom" entry from the Disc Data tab's rows and maps it to
    what _write_to_changer needs to send. Row 0 is always the disc name
    (InfoType.DISC_NAMES, index 0); rows 1..N are track names
    (InfoType.TRACK_NAMES, index = track number) -- matching the read
    side's index convention (see decode_text_data / _cache_name).
    Returns a list of (index, text, info_type, label) tuples."""
    items = []
    for i, row in enumerate(disc_data_rows):
        text = row["custom_var"].get().strip()
        if not text:
            continue
        if i == 0:
            items.append((0, text, proto.InfoType.DISC_NAMES, "Disc Name"))
        else:
            items.append((i, text, proto.InfoType.TRACK_NAMES, f"Track {i}"))
    return items


# Owner's manual p. 28. Real hardware already showed it: slot 4's disc name
# read back cut off at exactly 25 characters ('The Hip / Trouble at the ').
# Track-name limits aren't documented, so only the disc name is checked.
DISC_TITLE_MAX = 25


def disc_name_length_warning(write_items: list) -> str:
    """A note for the Write to Changer confirmation when the disc name
    (index 0) is longer than DISC_TITLE_MAX -- e.g. a gnudb.org
    "Artist / Album" copied into Custom. Warns rather than blocks, since
    the changer just keeps the first 25 characters. Takes
    gather_disc_data_write_items()-shaped items (merged 5-tuples work too).
    Returns "" when there's nothing to warn about."""
    for item in write_items:
        index, text = item[0], item[1]
        if index == 0 and len(text) > DISC_TITLE_MAX:
            return (
                f"\n\nNote: the disc name is {len(text)} characters; the changer keeps "
                f"only the first {DISC_TITLE_MAX}: {text[:DISC_TITLE_MAX]!r}. "
                f"Shorten it in the Custom column first if you'd rather choose the cut."
            )
    return ""


# Typographic punctuation gnudb.org entries often use, mapped to the plain
# ASCII the changer can store. Without this, encode_text_data turns each
# one into '?': a real v1.8.3 write of gnudb's 'Don’t Wake Daddy' read
# back as 'Don?t Wake Daddy'. The changer does store a plain apostrophe
# (0x27): its own handshake reply is "I'm CD-425M".
_ASCII_PUNCTUATION = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    "…": "...", " ": " ", "×": "x",
}


def ascii_fold(text: str) -> str:
    """Closest plain-ASCII version of `text` for writing to the changer:
    curly quotes/dashes/ellipsis become ASCII punctuation and accented
    letters lose their accents ('Beyoncé' -> 'Beyonce'). Anything still
    not ASCII becomes '?', the same as encode_text_data would do anyway."""
    text = "".join(_ASCII_PUNCTUATION.get(c, c) for c in text)
    text = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )
    return text.encode("ascii", errors="replace").decode("ascii")


def gnudb_match_summary(matches: list) -> str:
    """One log line per candidate with its category and DiscID, so a real
    session shows how the server's DiscIDs compare with ours."""
    return "; ".join(
        f"[{m.category} {m.discid}{'' if m.exact else ', inexact'}] {m.title}" for m in matches
    )


def gnudb_auto_read_match(matches: list):
    """The match to load straight away, or None to show the picker. Only a
    single EXACT match is loaded without asking; a lone inexact (code 211)
    match is the server's guess and can be a different album, so the user
    confirms it in the picker first."""
    if len(matches) == 1 and matches[0].exact:
        return matches[0]
    return None


def build_genre_write_frames(slot: int, genre_code: int) -> tuple[bytes, bytes]:
    """Builds the (request_data, follow_up_data) byte pairs for a
    standalone Action.SET_DISC_GENRE write -- kept around (same spirit as
    encode_long_text_data in pclink_protocol.py: a disproven-on-this-unit
    approach preserved for reference/future retry) but NO LONGER used by
    _write_to_changer_worker as of v1.4.3. History: three real-hardware
    attempts at a standalone genre write all failed, in three different
    ways -- v1.4.0 forgot to put the genre in the request at all; v1.4.1
    fixed that but the follow-up DiscGenre frame got rejected with an
    immediate EOT; v1.4.2 tried dropping the follow-up frame entirely,
    but then nothing was sent after ReadyForData so nothing was written.
    v1.4.3 instead folds genre into a WRITE_NAME/TextData write (see
    merge_genre_into_write_items()), since cd_textdata.html shows
    TextData's own payload already has a genre field, and that write path
    is the one CONFIRMED working. See CHANGELOG.md's v1.4.1-v1.4.3
    entries for the full history."""
    request_data = proto.encode_data_access(
        proto.Action.SET_DISC_GENRE, proto.DataType.DISC_GENRE, slot=slot, genre=genre_code,
    )
    follow_up_data = proto.encode_disc_genre(slot=slot, genre=genre_code)
    return request_data, follow_up_data


def _genre_key(text: str) -> str:
    """Spelling-insensitive key: case, hyphens/spaces and '&' vs 'and'."""
    words = (text or "").lower().replace("-", " ").replace("&", " and ").split()
    return " ".join(words)


_GENRE_BY_KEY = {_genre_key(name): name for name in proto.GENRE_NAME_TO_CODE}


def match_changer_genre(text: str) -> str:
    """The changer genre name (a key of proto.GENRE_NAME_TO_CODE) that
    `text` spells, or "" if none. gnudb genres are free text and often
    lowercase ("rock", "hip-hop"), so this ignores case, hyphens vs spaces
    and '&' vs 'and' (v1.8.7). It's still an exact match on the words:
    "folk rock" or "Alternative" don't match anything and stay blank
    rather than being forced into the nearest-sounding genre."""
    return _GENRE_BY_KEY.get(_genre_key(text), "")


def gather_genre_write_item(genre_custom_text: str) -> int | None:
    """Pure helper (unit-testable, same pattern as
    gather_disc_data_write_items): maps whatever's selected in the Disc
    Data tab's Genre row Custom dropdown back to a numeric genre code via
    proto.GENRE_NAME_TO_CODE. Returns None for a blank selection (meaning
    "don't write a genre") or unrecognized text -- distinct from actually
    choosing the valid "Unassigned" (0x00) genre value, which returns
    0x00, not None."""
    text = (genre_custom_text or "").strip()
    if not text:
        return None
    return proto.GENRE_NAME_TO_CODE.get(text)


def merge_genre_into_write_items(
    items: list, genre_code: int | None, current_disc_name: str | None,
    current_genre: int | None = None,
) -> tuple[list, str | None]:
    """v1.4.3: folds a selected genre into disc-name/track-name write
    items instead of sending it via a separate Action.SET_DISC_GENRE
    write -- see build_genre_write_frames' and CHANGELOG.md's
    v1.4.3/v1.5.0/v1.5.1 entries for why (cd_textdata.html shows
    TextData's own payload already has a `genre` field, and that's the
    one write path CONFIRMED working on real hardware; three earlier
    attempts at a standalone genre write all failed).

    v1.5.1, also real-hardware-confirmed: genre turns out to be a
    DISC-LEVEL value that EVERY TextData write sets, not just the one
    for the disc name -- confirmed the hard way when writing a track
    name (with its follow-up frame's genre byte left at the old default
    of 0) silently reset a just-written "Rock" genre back to
    "Unassigned". So every item in the final list -- disc name AND every
    track -- now carries the SAME `genre` byte: the newly selected
    `genre_code` if the user picked one, else whatever `current_genre`
    the app already knows for this slot (so a plain name-only write
    doesn't erase an existing genre), else 0 if neither is known.

    `items` is gather_disc_data_write_items()'s output: (index, text,
    info_type, label) tuples. `current_genre` should be the app's cached
    DiscGenre reading for this slot (self._genre_cache.get(slot)), used
    only when genre_code is None. Returns (final_items, error):
    final_items is a list of (index, text, info_type, label, genre)
    5-tuples. `error` is None on success, or a user-facing message when
    genre_code is not None but there's no disc name (neither a custom
    entry nor a currently-known one) to attach a write to at all --
    writing genre with a blank/empty name would risk clobbering the disc
    name, so this refuses rather than guessing."""
    effective_genre = genre_code if genre_code is not None else (current_genre or 0)

    if genre_code is None:
        return [(*it, effective_genre) for it in items], None

    has_disc_name_item = any(
        index == 0 and info_type == proto.InfoType.DISC_NAMES
        for index, _text, info_type, _label in items
    )
    if not has_disc_name_item:
        if not current_disc_name:
            return [(*it, effective_genre) for it in items], (
                "Can't write genre: it has to ride along with a disc-name "
                "write (see README.md's \"Honest gaps\" #14), and no disc "
                "name is known for this slot yet. "
                "Either type one into the Disc Name row's Custom field, or "
                "read the disc name first (Refresh from Changer / Get Disc "
                "Name)."
            )
        items = [(0, current_disc_name, proto.InfoType.DISC_NAMES, "Disc Name (genre only)")] + items

    return [(*it, effective_genre) for it in items], None


MODE_NAME_TO_CODE = {name: code for code, name in proto.MODE_NAMES.items()}

# Modes the changer won't switch to via ChangeMode, so the Play Mode
# dropdown leaves them out. Both CONFIRMED on real hardware: every request
# was ACK'd then ignored -- while playing, while stopped, and with a list
# stored -- although starting the same mode from the remote works and
# reports the same mode code we send.
#   Best (v1.6.4): remote-started Best reports mode=4.
#   Program (v1.6.6): remote-started program reports mode=3, with the
#   InfoEvent `program` byte stepping 1, 2, ... through the program.
#   Random variants (v1.6.7): all five ignored via ChangeMode, but the
#   Random button (DoAction RANDOM_MODE) reaches them -- see
#   RANDOM_BUTTON_CYCLE in pclink_protocol.py.
CHANGE_MODE_UNSUPPORTED = frozenset({
    proto.Mode.BEST,
    proto.Mode.PROGRAM,
    proto.Mode.TRACK_RANDOM_ONE,
    proto.Mode.TRACK_RANDOM_ALL,
    proto.Mode.GENRE_RANDOM_ALL,
    proto.Mode.USERFILE_RANDOM_ONE,
    proto.Mode.USERFILE_RANDOM_ALL,
})


def change_mode_choices() -> list[str]:
    """Mode names offered in the Control tab's Play Mode dropdown."""
    return [proto.MODE_NAMES[code] for code in sorted(proto.MODE_NAMES)
            if code not in CHANGE_MODE_UNSUPPORTED]


def build_change_mode_request(mode_name: str, genre_name: str, userfile_number: int) -> tuple[bytes, str]:
    """Turn the Control tab's Play Mode row into a ChangeMode payload + log
    label. `genre_name` is only used for the Music Type modes and
    `userfile_number` (1..8) only for the Userfile modes; param is 0
    otherwise. Raises ValueError for a selection that can't be sent.

    Confirmed on real hardware for Music Type and Userfile modes,
    including the userfile bit encoding (v1.6.1/v1.6.2). A mode the
    changer can't enter is silently ignored -- see _check_mode_took.
    """
    mode = MODE_NAME_TO_CODE.get(mode_name)
    if mode is None:
        raise ValueError(f"Unknown play mode {mode_name!r}.")
    if mode in proto.GENRE_MODES:
        param = proto.GENRE_NAME_TO_CODE.get(genre_name.strip())
        if param is None:
            raise ValueError("Pick a genre for Music Type mode.")
        detail = f", genre={genre_name.strip()}"
    elif mode in proto.USERFILE_MODES:
        param = proto.userfile_param(userfile_number)
        detail = f", userfile=#{userfile_number}"
    else:
        param = 0
        detail = ""
    return proto.encode_change_mode(mode, param), f"ChangeMode({mode_name}{detail}, param=0x{param:02X})"


USERFILE_COUNT = 8


def _disc_label(slot: int, disc_names: dict) -> str:
    name = disc_names.get(slot)
    return f"{slot} ({name})" if name else str(slot)


def scan_only_slots(userfiles_by_slot: dict, saved: dict | None) -> set[int]:
    """Slots whose userfiles are known only from the Library's saved scan
    (v1.12.1), not from the changer this session."""
    if not saved:
        return set()
    return {d["slot"] for d in saved["discs"]
            if d.get("userfiles") is not None and d["slot"] not in userfiles_by_slot}


def userfile_rows(userfiles_by_slot: dict, names_by_index: dict, disc_names: dict,
                  saved: dict | None = None) -> list[tuple]:
    """Rows for the Userfiles & Program tab's userfile table: one per
    userfile #1..#8 -> (number, name, discs). Membership comes from each
    slot's userfile bitmask (bit n-1 = userfile #n, CONFIRMED v1.6.2).
    Names are keyed by the same BIT, not the userfile number: CONFIRMED on
    real hardware (v1.7.1) -- a userfile-names read returned indexes 1, 2,
    4, 8, 16, 32, 64, 128, so userfile #3's name is index 4.

    `saved` (v1.12.1) is the Library's last scan (parsed). It fills in
    slots, disc names and userfile names the changer hasn't reported this
    session, so the table isn't empty until every disc is re-read. Slots
    from it are marked "*", since they may be out of date. What the changer
    reported always wins."""
    masks, names, saved_uf_names = {}, {}, {}
    if saved:
        for d in saved["discs"]:
            if d.get("userfiles") is not None:
                masks[d["slot"]] = d["userfiles"]
            if d.get("name"):
                names[d["slot"]] = d["name"]
        saved_uf_names = {1 << (n - 1): name for n, name in (saved.get("userfile_names") or {}).items()}
    from_scan = scan_only_slots(userfiles_by_slot, saved)
    masks.update(userfiles_by_slot)
    names.update(disc_names)
    rows = []
    for n in range(1, USERFILE_COUNT + 1):
        bit = 1 << (n - 1)
        slots = sorted(s for s, mask in masks.items() if mask & bit)
        discs = ", ".join(_disc_label(s, names) + ("*" if s in from_scan else "")
                          for s in slots) or "-"
        rows.append((f"#{n}", names_by_index.get(bit) or saved_uf_names.get(bit) or "-", discs))
    return rows


def program_rows(items: list, disc_names: dict, track_names: dict) -> list[tuple]:
    """Rows for the program table: (step, disc, track) from decoded
    DiscListing items. A track of 0xAA means the whole disc, per
    cd_disclisting.html (the owner's manual's "ALL", p. 24)."""
    rows = []
    for step, item in enumerate(items, start=1):
        slot, track = item["slot"], item["track"]
        if item.get("all_tracks"):
            track_text = "All tracks"
        else:
            name = track_names.get(slot, {}).get(track)
            track_text = f"{track} ({name})" if name else str(track)
        rows.append((step, _disc_label(slot, disc_names), track_text))
    return rows


# -- Writing userfiles & programs (v1.8.0, CONFIRMED on real hardware v1.8.1) --

USERFILE_NAME_MAX = 25  # owner's manual p. 35
PROGRAM_MAX_STEPS = 32  # owner's manual p. 24
SLOT_MIN, SLOT_MAX = 1, 200
TRACK_MIN, TRACK_MAX = 1, 99


def validate_title(text: str, max_len: int) -> str:
    """Strip and check a name the changer will store. Raises ValueError for
    blank, too-long or non-printable-ASCII text (encode_text_data would
    otherwise silently turn non-ASCII into '?')."""
    text = (text or "").strip()
    if not text:
        raise ValueError("The name can't be blank.")
    if len(text) > max_len:
        raise ValueError(f"The changer stores at most {max_len} characters ({len(text)} given).")
    if any(not (0x20 <= ord(c) < 0x7F) for c in text):
        raise ValueError("Only plain ASCII letters, digits and punctuation can be stored.")
    return text


def build_userfile_name_write(number: int, name: str) -> tuple[bytes, bytes]:
    """(request_data, follow_up_data) for naming userfile #`number` (1..8).
    A WRITE_NAME, the CONFIRMED name-write path, shaped exactly like the
    CONFIRMED read (v1.7.1): slot 0, info_type 7, and `index` = the
    userfile's BIT, with the userfiles/genre/format bytes 0 as in the
    changer's own replies. Raises ValueError for a bad number or name."""
    if not 1 <= number <= USERFILE_COUNT:
        raise ValueError(f"Userfile number must be 1-{USERFILE_COUNT}.")
    name = validate_title(name, USERFILE_NAME_MAX)
    request_data = proto.encode_data_access(
        proto.Action.WRITE_NAME, proto.DataType.TEXT_DATA, slot=0,
        info_type=proto.InfoType.USERFILE_NAMES,
    )
    follow_up_data = proto.encode_text_data(
        slot=0, index=proto.userfile_param(number), text=name,
        info_type=proto.InfoType.USERFILE_NAMES,
    )
    return request_data, follow_up_data


def userfile_mask_from_flags(flags: list) -> int:
    """[#1 checked, #2 checked, ...] -> bitmask (bit n-1 = #n)."""
    return sum(1 << i for i, on in enumerate(flags) if on)


def userfile_flags_from_mask(mask: int) -> list[bool]:
    return [bool(mask & (1 << i)) for i in range(USERFILE_COUNT)]


def plan_userfile_membership_write(disc_name: str | None, genre: int | None, cdtext: bool = False,
                                   track1: str | None = None) -> tuple[list, str | None]:
    """Which discs a disc belongs to is set by re-sending its disc name
    with the new mask in TextData's `userfiles` byte, the same way genre
    rides along (v1.5.0/v1.5.1), rather than via the standalone
    Action.SET_USERFILES + DiscUserfiles frame. That standalone shape is
    the same slot+byte shape as the SET_DISC_GENRE + DiscGenre write that
    real hardware rejected three times. CONFIRMED (v1.8.1): the changer
    honors TextData's userfiles byte on a write (slot 1 0x00 -> 0x07,
    read back on the disc name and every track, genre unchanged).

    The disc name and genre must both be known, since the write re-sends
    the one and would reset the other (genre is set by every TextData
    write, CONFIRMED v1.5.1). Returns (items, error) where items is in
    _write_to_changer_worker's (index, text, info_type, label, genre)
    form; the mask itself goes in the worker's `userfiles` argument.

    v1.12.11: a CD-Text disc re-sends track 1's name instead (see
    library_backup.state_carrier), CONFIRMED on real hardware."""
    if cdtext:
        if not track1:
            return [], (
                "Can't set userfiles: on a CD-Text disc they're written by re-sending "
                "track 1's name, and it couldn't be read."
            )
    elif not disc_name:
        return [], (
            "Can't set userfiles: they're written by re-sending the disc's "
            "name, and this disc has no name stored (or it couldn't be read). "
            "Name the disc on the Disc Data tab first."
        )
    if genre is None:
        return [], (
            "Can't set userfiles: the disc's current genre couldn't be read, "
            "and the write would reset it."
        )
    item, _ = library_backup.state_carrier(cdtext, disc_name, track1, genre)
    return [item[:3] + (item[3].replace("genre/userfiles", "userfiles only"), genre)], None


def plan_cdtext_disc_data_write(name_items: list, genre_code: int | None,
                                track1: str | None) -> tuple[list, str | None]:
    """Write to Changer on a CD-Text disc (v1.12.11): its titles come from
    the disc (owner's manual: a CD-Text disc can't be given a title; a
    written disc name is stored but the front panel still shows "-----",
    real hardware), so typed names are never written. A selected genre is
    written by re-sending track 1's name (library_backup.state_carrier).
    Returns (items, message): message explains what isn't written, or why
    nothing can be."""
    skipped = ("This is a CD-Text disc: its titles come from the disc itself, so the "
               "names in the Custom column aren't written." if name_items else None)
    if genre_code is None:
        return [], skipped or "Nothing to write: only the genre can be changed on a CD-Text disc."
    item, error = library_backup.state_carrier(True, None, track1, genre_code)
    if error:
        return [], "Can't write genre: " + error + "."
    return [item[:3] + ("Track 1 (genre)", genre_code)], skipped


def build_program_write(steps: list[tuple[int, int]]) -> tuple[bytes, bytes]:
    """(request_data, follow_up_data) for Action.WRITE_PROGRAM: a
    DataAccess(WRITE_PROGRAM, DiscListing, slot=0) request (slot 0, like
    the CONFIRMED program read) then a DiscListing payload of (slot,
    track) steps, track = proto.LISTING_ALL_TRACKS for a whole disc.
    CONFIRMED on real hardware (v1.8.1; ReadyForData raw_byte 4). Writing
    a program also switched the changer into Program mode and started it
    playing. Raises ValueError for more than 32
    steps or an out-of-range slot/track."""
    if len(steps) > PROGRAM_MAX_STEPS:
        raise ValueError(f"A program holds at most {PROGRAM_MAX_STEPS} steps ({len(steps)} given).")
    for n, (slot, track) in enumerate(steps, start=1):
        if not SLOT_MIN <= slot <= SLOT_MAX:
            raise ValueError(f"Step {n}: disc slot {slot} is out of range {SLOT_MIN}-{SLOT_MAX}.")
        if track != proto.LISTING_ALL_TRACKS and not TRACK_MIN <= track <= TRACK_MAX:
            raise ValueError(f"Step {n}: track {track} is out of range {TRACK_MIN}-{TRACK_MAX}.")
    request_data = proto.encode_data_access(
        proto.Action.WRITE_PROGRAM, proto.DataType.DISC_LISTING, slot=0,
    )
    return request_data, proto.encode_disc_listing(steps)


def program_steps_from_items(items: list) -> list[tuple[int, int]]:
    """Decoded DiscListing items -> editable (slot, track) steps."""
    return [(i["slot"], i["track"]) for i in items]


def program_items_from_steps(steps: list[tuple[int, int]]) -> list[dict]:
    """(slot, track) steps -> the item dicts program_rows() displays."""
    return [{"slot": s, "track": t, "all_tracks": t == proto.LISTING_ALL_TRACKS} for s, t in steps]


def missing_disc_state(slot: int, disc_names: dict, genres: dict, userfiles: dict,
                       need_name: bool = False, track_names: dict | None = None,
                       cdtext: bool = False) -> list[str]:
    """What a TextData write to `slot` needs but the app hasn't read yet.
    Every TextData write sets the disc's genre (CONFIRMED v1.5.1) and, it's
    assumed by analogy, its userfile mask, so writing without knowing
    them risks resetting them to 0. `need_name`: the write re-sends a name
    to carry them -- the disc name, or track 1's on a CD-Text disc
    (v1.12.11, library_backup.state_carrier)."""
    missing = []
    if need_name and cdtext:
        if not (track_names or {}).get(slot, {}).get(1):
            missing.append("track 1's name")
    elif need_name and slot not in disc_names:
        missing.append("disc name")
    if slot not in genres:
        missing.append("genre")
    if slot not in userfiles:
        missing.append("userfiles")
    return missing


class ScrollableFrame(ttk.Frame):
    """A vertically-scrollable container. Put widgets in `.inner` (an
    ordinary ttk.Frame) the same way you would in any other frame; this
    class just handles the canvas/scrollbar plumbing Tkinter needs for
    scrolling to work, and keeps the inner frame's width in sync with the
    visible area."""

    def __init__(self, parent, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.vscroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)

        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vscroll.set)
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.vscroll.pack(side="right", fill="y")

    def _on_canvas_resize(self, event):
        self.canvas.itemconfig(self._window, width=event.width)


class App(tk.Tk):
    # -- Disc Map tab (grid of the 200 slots, colored by occupancy) --------
    DISC_MAP_SLOT_COUNT = 200
    DISC_MAP_COLS = 20
    DISC_MAP_CELL_SIZE = 26
    DISC_MAP_CELL_PAD = 3
    DISC_MAP_COLOR_UNKNOWN = "#e8e8e8"   # not queried yet this session
    DISC_MAP_COLOR_EMPTY = "#9e9e9e"     # queried, DiscInfo says no disc
    DISC_MAP_COLOR_OCCUPIED = "#4caf50"  # queried, DiscInfo says a disc is there

    # -- Library tab (v1.11.0): disc table columns, keyed by sort key ------
    LIBRARY_COLUMN_TITLES = {"slot": "Slot", "name": "Disc Name", "genre": "Genre",
                             "userfiles": "Userfiles", "tracks": "Tracks"}

    def __init__(self, library_cache_path: str | None = LIBRARY_CACHE_PATH):
        super().__init__()
        self.title(f"Ken Changer - CD-425M (v{APP_VERSION})")
        self.geometry("950x780")

        self.link: PCLinkConnection | None = None
        self.ui_queue: "queue.Queue[callable]" = queue.Queue()

        self._ff_fb_thread: threading.Thread | None = None
        self._ff_fb_stop = threading.Event()

        # Live "now playing" tracking, kept separate from the slot/track
        # spinboxes (which are for navigation, not necessarily what's
        # currently loaded). Populated from InfoEvent/DiscEvent, and used
        # to auto-fetch + display the current disc/track name.
        self._current_slot: int | None = None
        self._current_track: int | None = None
        self._disc_name_cache: dict[int, str] = {}          # slot -> name
        # Slots whose last name read hit the endless LongTextData stream
        # (v1.12.4, pclink_link.PCLinkTextStream), so the track-name read
        # right after it is skipped rather than streaming again.
        self._text_stream_slots: set[int] = set()
        # Slots holding a CD-Text disc (v1.12.11, see _note_disc_format):
        # their names aren't written, and genre/userfiles ride on track 1.
        self._cdtext_slots: set[int] = set()
        self._track_name_cache: dict[int, dict[int, str]] = {}  # slot -> {track: name}
        self._genre_cache: dict[int, int] = {}               # slot -> genre code
        # slot -> {"tracks": {track_num: {minutes,seconds,frames}},
        #          "leadout": {minutes,seconds,frames} or None,
        #          "format": int}
        # Accumulated across however many DiscTOC reply frames (pages) show
        # up for one request -- see _on_frame's CMD_DISC_TOC handling.
        self._toc_cache: dict[int, dict] = {}
        # The changer's last-reported playback state. Confirmed on real
        # hardware: a DiscTOC request made while this is CHANGING returns
        # empty (immediate EOT, no data) -- the changer needs to finish
        # physically loading the disc and reading its TOC first. Tracked so
        # we can (a) show a clearer status message and (b) automatically
        # retry the TOC fetch once the state moves past CHANGING.
        self._last_state: int | None = None

        # Disc Data tab: per-slot custom text the user has typed into
        # column 3 (preserved across disc navigation and data refreshes --
        # rebuilding the row widgets would otherwise silently discard
        # whatever they were in the middle of typing).
        self._custom_data_cache: dict[int, dict[int, str]] = {}  # slot -> {row_index: text}
        self._custom_genre_cache: dict[int, str] = {}  # slot -> genre name chosen in the dropdown
        self._disc_data_rows: list[dict] = []  # row widgets/vars for the currently-shown slot
        self._disc_data_slot: int | None = None  # which slot _disc_data_rows corresponds to

        # gnudb.org lookups: kept independent of the serial connection
        # (it's internet data tied to the disc, not the changer session),
        # so unlike the changer-sourced caches this is NOT cleared on
        # disconnect.
        self._gnudb_cache: dict[int, gnudb_client.GnudbDisc] = {}  # slot -> GnudbDisc
        # Cover art found after a gnudb read (v1.9.0), same lifetime as
        # _gnudb_cache. None = looked up, nothing matched.
        self._album_art_cache: dict[int, album_art.AlbumArt | None] = {}
        self._album_art_photo = None  # keeps the shown tk.PhotoImage alive

        # Disc Map tab: per-slot occupancy as last reported by DiscInfo
        # (slot -> True if it has a disc, False if empty; a slot with no
        # entry yet hasn't been queried this session). Populated one slot
        # at a time by clicking a cell, or for all 200 slots via "Scan All
        # 200 Slots". Changer-sourced, so cleared on disconnect like the
        # other caches -- see _reset_disc_map / _disconnect.
        self._disc_occupancy: dict[int, bool] = {}
        self._disc_track_count: dict[int, int] = {}  # slot -> DiscInfo track_count (v1.10.0)
        self._disc_map_cells: dict[int, int] = {}  # slot -> canvas rectangle item id
        self._disc_map_stop = threading.Event()
        self._disc_map_scanning = False

        # Userfiles & Program tab (v1.7.0, read-only). Changer-sourced, so
        # cleared on disconnect. Userfile membership (slot -> bitmask) is
        # collected from every InfoEvent / TextData / DiscUserfiles reply
        # that carries one, not just from explicit reads.
        self._userfiles_cache: dict[int, int] = {}
        self._userfile_name_cache: dict[int, str] = {}  # TextData index (userfile BIT) -> name
        self._program_items: list | None = None  # decoded DiscListing items, None = not read yet
        # v1.8.0: the program editor's (slot, track) steps. Replaced by each
        # program read; "dirty" once edited and not yet written.
        self._program_draft: list[tuple[int, int]] = []
        self._program_draft_dirty = False
        self._userfile_scan_running = False

        # Backup tab (v1.10.0): one export or restore at a time.
        self._library_running = False
        self._library_stop = threading.Event()
        self._library_label = None  # the progress label of the tab that started it

        # Library tab (v1.11.0), see library_browser.py. _browser_raw is the
        # last changer scan in backup form (what library_cache.json holds;
        # None = no scan yet); _browser_library is what's shown, parsed --
        # that scan, or a backup file opened with "Open Backup...".
        self.library_cache_path = library_cache_path  # None: don't save or load
        self._browser_raw: dict | None = None
        # The saved scan, parsed (v1.12.1): also fills in the Userfiles &
        # Program tab's table. Never a backup file opened for browsing.
        self._browser_scan: dict | None = None
        self._browser_library: dict | None = None
        self._browser_file: str | None = None  # the backup file shown, if not the scan
        self._browser_sort = ("slot", False)
        self._browser_genres: dict[str, int] = {}     # filter label -> genre code
        self._browser_userfiles: dict[str, int] = {}  # filter label -> userfile number
        self._browser_tracks_slot: int | None = None  # the disc the track pane shows

        # Mode code from the last "Set Mode" click, until an InfoEvent
        # reports it. Confirmed on real hardware: the changer ACKs a
        # ChangeMode it won't honor (Best and Program, always -- see
        # CHANGE_MODE_UNSUPPORTED) and then just sends nothing -- no
        # InfoEvent, no error -- so the only way to notice is that the mode
        # never shows up. Kept as a safety net for anything else it ignores.
        self._pending_mode: int | None = None

        self._build_ui()
        self._poll_ui_queue()
        self._refresh_ports()
        self._load_library_cache()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="Serial port:").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=30, state="readonly")
        self.port_combo.pack(side="left", padx=4)
        ttk.Button(top, text="Refresh", command=self._refresh_ports).pack(side="left")

        self.connect_btn = ttk.Button(top, text="Connect", command=self._toggle_connect)
        self.connect_btn.pack(side="left", padx=8)

        self.conn_status = ttk.Label(top, text="Disconnected", foreground="red")
        self.conn_status.pack(side="left", padx=8)

        ttk.Label(top, text="9600 8-N-2, null-modem cable required").pack(side="right")

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)

        notebook = ttk.Notebook(body)
        notebook.pack(fill="both", expand=True)
        self.notebook = notebook

        control_tab = ttk.Frame(notebook, padding=4)
        notebook.add(control_tab, text="Control")

        library_tab = ttk.Frame(notebook, padding=4)
        notebook.add(library_tab, text="Library")

        disc_data_tab = ttk.Frame(notebook, padding=4)
        notebook.add(disc_data_tab, text="Disc Data")
        self.disc_data_tab = disc_data_tab

        disc_map_tab = ttk.Frame(notebook, padding=4)
        notebook.add(disc_map_tab, text="Disc Map")

        uf_prog_tab = ttk.Frame(notebook, padding=4)
        notebook.add(uf_prog_tab, text="Userfiles & Program")

        backup_tab = ttk.Frame(notebook, padding=4)
        notebook.add(backup_tab, text="Backup")

        control_tab.columnconfigure(0, weight=1)
        control_tab.columnconfigure(1, weight=1)
        control_tab.rowconfigure(1, weight=1)  # TOC panel

        # -- Status panel --------------------------------------------------
        status_frame = ttk.LabelFrame(control_tab, text="Status", padding=8)
        status_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4), pady=4)

        self.status_labels = {}
        for i, key in enumerate([
            "State", "Disc (slot)", "Disc Name", "Genre", "Track", "Track Name",
            "Program", "Mode", "Mode Param", "Repeat", "Door", "Userfiles",
        ]):
            ttk.Label(status_frame, text=key + ":").grid(row=i, column=0, sticky="w")
            lbl = ttk.Label(status_frame, text="-", font=("", 10, "bold"), wraplength=220)
            lbl.grid(row=i, column=1, sticky="w", padx=6)
            self.status_labels[key] = lbl

        # -- Transport controls -----------------------------------------------------
        transport_frame = ttk.LabelFrame(control_tab, text="Transport", padding=8)
        transport_frame.grid(row=0, column=1, sticky="nsew", padx=(4, 0), pady=4)

        btn_grid = ttk.Frame(transport_frame)
        btn_grid.pack()

        def action_btn(parent, text, action_code, col, row):
            b = ttk.Button(parent, text=text, width=12,
                            command=lambda: self._send_action(action_code, text))
            b.grid(row=row, column=col, padx=3, pady=3)
            return b

        def press_release_btn(parent, text, action_code, col, row):
            """For buttons the changer treats as a hold: send the action on
            press, and send Finish Repeatable (0xFFFF) on release. Confirmed
            necessary for Previous/Next as well as FF/FB, not just the two
            marked "(repeatable)" in the source docs' Action Command table."""
            b = ttk.Button(parent, text=text, width=12)
            b.grid(row=row, column=col, padx=3, pady=3)
            b.bind("<ButtonPress-1>", lambda e: self._send_action(action_code, text))
            b.bind("<ButtonRelease-1>", lambda e: self._release_hold())
            return b

        press_release_btn(btn_grid, "Previous", proto.ActionCommand.PREVIOUS_TRACK, 0, 0)
        action_btn(btn_grid, "Play / Pause", proto.ActionCommand.PLAY_PAUSE, 1, 0)
        press_release_btn(btn_grid, "Next", proto.ActionCommand.NEXT_TRACK, 2, 0)

        action_btn(btn_grid, "Stop", proto.ActionCommand.STOP, 1, 1)

        ff_btn = ttk.Button(btn_grid, text="<< FB (hold)", width=12)
        ff_btn.grid(row=2, column=0, padx=3, pady=3)
        ff_btn.bind("<ButtonPress-1>", lambda e: self._start_repeat(proto.ActionCommand.FAST_BACKWARD))
        ff_btn.bind("<ButtonRelease-1>", lambda e: self._release_hold())

        fw_btn = ttk.Button(btn_grid, text="FF >> (hold)", width=12)
        fw_btn.grid(row=2, column=2, padx=3, pady=3)
        fw_btn.bind("<ButtonPress-1>", lambda e: self._start_repeat(proto.ActionCommand.FAST_FORWARD))
        fw_btn.bind("<ButtonRelease-1>", lambda e: self._release_hold())

        action_btn(btn_grid, "Random", proto.ActionCommand.RANDOM_MODE, 0, 3)
        action_btn(btn_grid, "Repeat", proto.ActionCommand.REPEAT_MODE, 2, 3)

        ttk.Separator(transport_frame).pack(fill="x", pady=8)

        goto_frame = ttk.Frame(transport_frame)
        goto_frame.pack(fill="x")
        ttk.Label(goto_frame, text="Disc (slot):").grid(row=0, column=0, sticky="w")
        self.slot_var = tk.IntVar(value=1)
        ttk.Spinbox(goto_frame, from_=1, to=200, textvariable=self.slot_var, width=6).grid(row=0, column=1, padx=4)
        ttk.Label(goto_frame, text="Track:").grid(row=0, column=2, sticky="w")
        self.track_var = tk.IntVar(value=1)
        ttk.Spinbox(goto_frame, from_=1, to=99, textvariable=self.track_var, width=6).grid(row=0, column=3, padx=4)
        ttk.Button(goto_frame, text="Change Disc / Play", command=self._change_disc).grid(row=0, column=4, padx=8)

        # Play Mode (ChangeMode). The genre / userfile pickers only apply
        # to the Music Type / Userfile modes, so they're disabled otherwise.
        mode_frame = ttk.Frame(transport_frame)
        mode_frame.pack(fill="x", pady=(8, 0))
        ttk.Label(mode_frame, text="Play mode:").grid(row=0, column=0, sticky="w")
        self.mode_var = tk.StringVar(value=proto.MODE_NAMES[proto.Mode.TRACK])
        mode_combo = ttk.Combobox(
            mode_frame, textvariable=self.mode_var, state="readonly", width=28,
            values=change_mode_choices(),
        )
        mode_combo.grid(row=0, column=1, columnspan=3, sticky="w", padx=4)
        mode_combo.bind("<<ComboboxSelected>>", lambda e: self._update_mode_param_widgets())
        ttk.Button(mode_frame, text="Set Mode", command=self._change_mode).grid(row=0, column=4, padx=8)
        ttk.Label(mode_frame, text="Genre:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.mode_genre_var = tk.StringVar(value="")
        self.mode_genre_combo = ttk.Combobox(
            mode_frame, textvariable=self.mode_genre_var, state="readonly", width=20,
            values=[proto.GENRES[code] for code in sorted(proto.GENRES)],
        )
        self.mode_genre_combo.grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Label(mode_frame, text="Userfile #:").grid(row=1, column=2, sticky="w", pady=(4, 0))
        self.mode_userfile_var = tk.IntVar(value=1)
        self.mode_userfile_spin = ttk.Spinbox(
            mode_frame, from_=1, to=8, textvariable=self.mode_userfile_var, width=4, state="readonly",
        )
        self.mode_userfile_spin.grid(row=1, column=3, sticky="w", padx=4, pady=(4, 0))
        ttk.Label(
            mode_frame, foreground="gray",
            text="Random: set a mode, then use the Random button. Best/Program: remote only.",
        ).grid(row=2, column=0, columnspan=5, sticky="w", pady=(4, 0))
        self._update_mode_param_widgets()

        query_frame = ttk.Frame(transport_frame)
        query_frame.pack(fill="x", pady=(8, 0))
        ttk.Button(query_frame, text="Get Disc Info", command=self._get_disc_info).pack(side="left", padx=2)
        ttk.Button(query_frame, text="Refresh TOC", command=self._get_disc_toc).pack(side="left", padx=2)
        ttk.Button(query_frame, text="Get Track Names", command=self._get_track_names).pack(side="left", padx=2)
        ttk.Button(query_frame, text="Get Disc Name", command=self._get_disc_name).pack(side="left", padx=2)
        ttk.Button(query_frame, text="Get Genre", command=self._get_genre).pack(side="left", padx=2)

        # -- Table of Contents -------------------------------------------------------
        # The changer only exposes TOC data for the currently-loaded disc
        # (unlike disc/track names, which can be queried for any slot), so
        # this panel always tracks _current_slot rather than the slot
        # spinbox above.
        toc_frame = ttk.LabelFrame(control_tab, text="Table of Contents (current disc)", padding=8)
        toc_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=4)

        toc_header = ttk.Frame(toc_frame)
        toc_header.pack(fill="x")
        self.toc_summary_label = ttk.Label(toc_header, text="No TOC loaded yet.")
        self.toc_summary_label.pack(side="left")
        self.discid_label = ttk.Label(toc_header, text="", font=("", 10, "bold"))
        self.discid_label.pack(side="right")

        self.toc_tree = ttk.Treeview(
            toc_frame, columns=("track", "start", "length"), show="headings", height=6,
        )
        self.toc_tree.heading("track", text="Track")
        self.toc_tree.heading("start", text="Start")
        self.toc_tree.heading("length", text="Length")
        self.toc_tree.column("track", width=60, anchor="center")
        self.toc_tree.column("start", width=100, anchor="center")
        self.toc_tree.column("length", width=100, anchor="center")
        self.toc_tree.pack(fill="both", expand=True, pady=(4, 0))

        # -- Disc Data tab -------------------------------------------------------
        # Three side-by-side lists, row-aligned (row 0 = Disc Name, rows
        # 1..N = Track 1..N): what the changer reports, what a gnudb.org
        # lookup reports (not wired up yet -- UI only for now), and a
        # blank/editable column for custom values to eventually write back
        # to the changer (also not wired up yet). Only ever shows the
        # current disc, same as the TOC panel.
        disc_data_tab.columnconfigure(0, weight=1)

        dd_toolbar = ttk.Frame(disc_data_tab)
        dd_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Button(dd_toolbar, text="Refresh from Changer",
                   command=self._refresh_disc_data_from_changer).pack(side="left", padx=2)
        ttk.Button(dd_toolbar, text="Query gnudb.org",
                   command=self._query_gnudb).pack(side="left", padx=2)
        ttk.Button(dd_toolbar, text="Copy Changer -> Custom",
                   command=lambda: self._copy_column_to_custom("changer")).pack(side="left", padx=2)
        ttk.Button(dd_toolbar, text="Copy gnudb -> Custom",
                   command=lambda: self._copy_column_to_custom("gnudb")).pack(side="left", padx=2)
        ttk.Button(dd_toolbar, text="Clear Custom",
                   command=self._clear_custom_data).pack(side="left", padx=2)
        self.dd_write_btn = ttk.Button(dd_toolbar, text="Write to Changer",
                   command=self._write_to_changer)
        self.dd_write_btn.pack(side="right", padx=2)

        # gnudb.org's usage policy requires a real, contactable email in
        # every request (not a generic default) -- see gnudb_client.py.
        # Stored in memory only, never written to disk by this app.
        dd_email_row = ttk.Frame(disc_data_tab)
        dd_email_row.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(dd_email_row, text="Contact email (required by gnudb.org):").pack(side="left")
        self.gnudb_email_var = tk.StringVar(value="")
        ttk.Entry(dd_email_row, textvariable=self.gnudb_email_var, width=30).pack(
            side="left", padx=(4, 0)
        )

        # Cover art (v1.9.0): fetched from iTunes after a gnudb read, since
        # gnudb has no images -- see album_art.py. Sits to the right of the
        # toolbar/email/summary/genre rows.
        self.album_art_label = ttk.Label(
            disc_data_tab, text="No cover art", anchor="center", justify="center",
        )
        self.album_art_label.grid(row=0, column=1, rowspan=5, sticky="ne", padx=(8, 2))

        self.disc_data_summary = ttk.Label(disc_data_tab, text="No current disc known yet.")
        self.disc_data_summary.grid(row=2, column=0, sticky="w", pady=(0, 4))

        dd_header = ttk.Frame(disc_data_tab)
        dd_header.grid(row=3, column=0, sticky="new")

        for col, (text, width) in enumerate([
            ("", 10), ("From Changer", 34), ("From gnudb.org", 34), ("Custom (to write)", 34),
        ]):
            ttk.Label(dd_header, text=text, font=("", 9, "bold"), width=width, anchor="w").grid(
                row=0, column=col, sticky="w", padx=(2, 8)
            )

        # Genre row: disc-level only (unlike Disc Name/Track Name, there's
        # no per-track genre), so it's a fixed row of its own rather than
        # part of the scrollable Disc Name/Track rows below -- those get
        # torn down and rebuilt when the track count changes, which would
        # otherwise fight with this row's own slot-keyed state. Custom
        # column is a dropdown (not free text) listing every value in
        # proto.GENRES, per the changer's fixed genre enum -- plus a blank
        # entry meaning "don't write a genre", distinct from actually
        # choosing the valid "Unassigned" value. Reading/writing genre via
        # DataAccess(DiscGenre)/Action.SET_DISC_GENRE is wired up but NOT
        # yet confirmed against real hardware -- see README.md's "Honest
        # gaps" and CHANGELOG.md.
        dd_genre_row = ttk.Frame(disc_data_tab)
        dd_genre_row.grid(row=4, column=0, sticky="new", pady=(2, 4))
        ttk.Label(dd_genre_row, text="Genre", width=10, anchor="w").grid(
            row=0, column=0, sticky="w", padx=(2, 8)
        )
        self.genre_changer_var = tk.StringVar(value="-")
        ttk.Label(dd_genre_row, textvariable=self.genre_changer_var, width=34, anchor="w").grid(
            row=0, column=1, sticky="w", padx=(2, 8)
        )
        self.genre_gnudb_var = tk.StringVar(value="-")
        ttk.Label(dd_genre_row, textvariable=self.genre_gnudb_var, width=34, anchor="w").grid(
            row=0, column=2, sticky="w", padx=(2, 8)
        )
        self.genre_custom_var = tk.StringVar(value="")
        genre_dropdown_values = [""] + [proto.GENRES[code] for code in sorted(proto.GENRES)]
        self.genre_custom_combo = ttk.Combobox(
            dd_genre_row, textvariable=self.genre_custom_var, values=genre_dropdown_values,
            state="readonly", width=32,
        )
        self.genre_custom_combo.grid(row=0, column=3, sticky="we", padx=(2, 8))

        # placeholder row for the header/genre row; the scrollable
        # Disc Name/Track rows area starts below both. Rebuilt as row 5 so
        # they stay fixed while the track rows scroll.
        disc_data_tab.rowconfigure(5, weight=1)

        dd_scroll = ScrollableFrame(disc_data_tab)
        dd_scroll.grid(row=5, column=0, columnspan=2, sticky="nsew")
        self.disc_data_rows_frame = dd_scroll.inner
        for col, width in [(0, 10), (1, 34), (2, 34), (3, 34)]:
            self.disc_data_rows_frame.columnconfigure(col, minsize=width * 7)

        # -- Disc Map tab -------------------------------------------------------
        # A 200-slot grid, colored by whether DiscInfo reports a disc in
        # that slot or not. Confirmed against real hardware, including a
        # full 200-slot "Scan All" run -- see CHANGELOG.md.
        #
        # Caveat confirmed by the user: the changer's own memory of what's
        # in each slot can be lost or go stale after a power outage, which
        # then makes DiscInfo's answers wrong until the user runs "ALL
        # DATA READ" from the changer's own front-panel/remote menu -- see
        # dm_caveat below. There's no equivalent command over the serial
        # protocol, so this app can't trigger or detect that on its own.
        dm_intro = (
            "Which of the changer's 200 slots have a disc, based on "
            "DiscInfo (the same query as \"Get Disc Info\" on the Control "
            "tab, looped over every slot). A slot is shown as occupied "
            "when DiscInfo's track_count is greater than 0."
        )
        ttk.Label(disc_map_tab, text=dm_intro, wraplength=860, justify="left").pack(
            anchor="w", pady=(0, 4)
        )

        dm_caveat = (
            "\u26a0 If this looks wrong (a slot you know has a disc shows "
            "empty, or vice versa), the changer's own memory of what's in "
            "each slot may have gone stale -- this can happen after a "
            "power outage. Fix: run \"ALL DATA READ\" from the changer's "
            "own front-panel/remote menu (requires the remote; there's no "
            "way to trigger this over the serial connection) before "
            "re-scanning here."
        )
        ttk.Label(
            disc_map_tab, text=dm_caveat, wraplength=860, justify="left",
            foreground="#a05a00",
        ).pack(anchor="w", pady=(0, 6))

        dm_toolbar = ttk.Frame(disc_map_tab)
        dm_toolbar.pack(fill="x", pady=(0, 4))
        self.disc_map_scan_btn = ttk.Button(
            dm_toolbar, text="Scan All 200 Slots", command=self._start_disc_map_scan
        )
        self.disc_map_scan_btn.pack(side="left", padx=2)
        self.disc_map_stop_btn = ttk.Button(
            dm_toolbar, text="Stop Scan", command=self._stop_disc_map_scan, state="disabled"
        )
        self.disc_map_stop_btn.pack(side="left", padx=2)
        self.disc_map_progress_label = ttk.Label(dm_toolbar, text="Not scanned yet.")
        self.disc_map_progress_label.pack(side="left", padx=12)

        dm_legend = ttk.Frame(disc_map_tab)
        dm_legend.pack(fill="x", pady=(0, 6))
        for color, text in [
            (self.DISC_MAP_COLOR_OCCUPIED, "Has a disc"),
            (self.DISC_MAP_COLOR_EMPTY, "Empty"),
            (self.DISC_MAP_COLOR_UNKNOWN, "Not queried yet"),
        ]:
            swatch = tk.Canvas(dm_legend, width=14, height=14, highlightthickness=1,
                                highlightbackground="#666666")
            swatch.create_rectangle(1, 1, 13, 13, fill=color, outline="")
            swatch.pack(side="left", padx=(0, 3))
            ttk.Label(dm_legend, text=text).pack(side="left", padx=(0, 12))
        ttk.Label(dm_legend, text="(click any slot to query just that one)").pack(side="left")

        dm_canvas_frame = ttk.Frame(disc_map_tab)
        dm_canvas_frame.pack(fill="both", expand=True)
        cell = self.DISC_MAP_CELL_SIZE
        pad = self.DISC_MAP_CELL_PAD
        cols = self.DISC_MAP_COLS
        rows = (self.DISC_MAP_SLOT_COUNT + cols - 1) // cols
        canvas_width = pad + cols * (cell + pad)
        canvas_height = pad + rows * (cell + pad)
        self.disc_map_canvas = tk.Canvas(
            dm_canvas_frame, width=canvas_width, height=canvas_height,
            background="white", highlightthickness=0,
        )
        self.disc_map_canvas.pack(anchor="w")

        for slot in range(1, self.DISC_MAP_SLOT_COUNT + 1):
            idx = slot - 1
            row, col = divmod(idx, cols)
            x0 = pad + col * (cell + pad)
            y0 = pad + row * (cell + pad)
            x1, y1 = x0 + cell, y0 + cell
            rect = self.disc_map_canvas.create_rectangle(
                x0, y0, x1, y1, fill=self.DISC_MAP_COLOR_UNKNOWN, outline="#888888",
            )
            label = self.disc_map_canvas.create_text(
                (x0 + x1) / 2, (y0 + y1) / 2, text=str(slot), font=("", 7),
            )
            self.disc_map_canvas.tag_bind(
                rect, "<Button-1>", lambda e, s=slot: self._disc_map_click(s)
            )
            self.disc_map_canvas.tag_bind(
                label, "<Button-1>", lambda e, s=slot: self._disc_map_click(s)
            )
            self._disc_map_cells[slot] = rect

        # -- Userfiles & Program tab (read-only v1.7.0, writes v1.8.0) --------
        ttk.Label(
            uf_prog_tab, foreground="gray",
            text="Writing a program starts it playing in Program mode; switching "
                 "to another play mode clears the program.",
        ).pack(anchor="w")

        uf_frame = ttk.LabelFrame(uf_prog_tab, text="Userfiles", padding=8)
        uf_frame.pack(fill="both", expand=True, pady=4)
        uf_toolbar = ttk.Frame(uf_frame)
        uf_toolbar.pack(fill="x")
        ttk.Button(uf_toolbar, text="Read Userfile Names",
                   command=self._read_userfile_names).pack(side="left", padx=2)
        self.uf_scan_btn = ttk.Button(uf_toolbar, text="Read Userfiles for Known Discs",
                                      command=self._read_userfiles_for_known_discs)
        self.uf_scan_btn.pack(side="left", padx=2)
        self.uf_rename_btn = ttk.Button(uf_toolbar, text="Rename Selected...",
                                        command=self._rename_userfile)
        self.uf_rename_btn.pack(side="left", padx=2)
        self.uf_progress_label = ttk.Label(uf_toolbar, text="")
        self.uf_progress_label.pack(side="left", padx=8)
        self.uf_tree = ttk.Treeview(uf_frame, columns=("uf", "name", "discs"),
                                    show="headings", height=8, selectmode="browse")
        for col, text, width in (("uf", "Userfile", 70), ("name", "Name", 180), ("discs", "Discs", 480)):
            self.uf_tree.heading(col, text=text)
            self.uf_tree.column(col, width=width, anchor="w", stretch=(col == "discs"))
        self.uf_tree.pack(fill="both", expand=True, pady=(4, 0))
        self.uf_saved_note = ttk.Label(uf_frame, text="", foreground="gray")
        self.uf_saved_note.pack(anchor="w")

        # Per-disc membership editor: load a slot's mask, tick boxes, write.
        member_frame = ttk.Frame(uf_frame)
        member_frame.pack(fill="x", pady=(6, 0))
        ttk.Label(member_frame, text="Disc slot:").grid(row=0, column=0, sticky="w")
        self.uf_member_slot_var = tk.IntVar(value=1)
        ttk.Spinbox(member_frame, from_=SLOT_MIN, to=SLOT_MAX, width=5,
                    textvariable=self.uf_member_slot_var).grid(row=0, column=1, padx=4)
        ttk.Button(member_frame, text="Current Disc",
                   command=self._uf_member_use_current).grid(row=0, column=2, padx=2)
        ttk.Button(member_frame, text="Load",
                   command=self._uf_member_load).grid(row=0, column=3, padx=2)
        self.uf_member_write_btn = ttk.Button(member_frame, text="Write Disc's Userfiles",
                                              command=self._write_userfile_membership)
        self.uf_member_write_btn.grid(row=0, column=4, padx=2)
        self.uf_member_label = ttk.Label(member_frame, text="", foreground="gray")
        self.uf_member_label.grid(row=0, column=5, sticky="w", padx=8)
        checks = ttk.Frame(member_frame)
        checks.grid(row=1, column=0, columnspan=6, sticky="w", pady=(4, 0))
        self.uf_member_vars = [tk.BooleanVar(value=False) for _ in range(USERFILE_COUNT)]
        self.uf_member_checks = []
        for i, var in enumerate(self.uf_member_vars):
            cb = ttk.Checkbutton(checks, text=f"#{i + 1}", variable=var)
            cb.grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 12))
            self.uf_member_checks.append(cb)

        prog_frame = ttk.LabelFrame(uf_prog_tab, text="Program", padding=8)
        prog_frame.pack(fill="both", expand=True, pady=4)
        prog_toolbar = ttk.Frame(prog_frame)
        prog_toolbar.pack(fill="x")
        ttk.Button(prog_toolbar, text="Read Program", command=self._read_program).pack(side="left", padx=2)
        self.prog_write_btn = ttk.Button(prog_toolbar, text="Write Program", command=self._write_program)
        self.prog_write_btn.pack(side="left", padx=2)
        self.prog_summary_label = ttk.Label(prog_toolbar, text="Not read yet.")
        self.prog_summary_label.pack(side="left", padx=8)

        prog_edit = ttk.Frame(prog_frame)
        prog_edit.pack(fill="x", pady=(4, 0))
        ttk.Label(prog_edit, text="Disc slot:").pack(side="left")
        self.prog_slot_var = tk.IntVar(value=1)
        ttk.Spinbox(prog_edit, from_=SLOT_MIN, to=SLOT_MAX, width=5,
                    textvariable=self.prog_slot_var).pack(side="left", padx=4)
        ttk.Label(prog_edit, text="Track:").pack(side="left")
        self.prog_track_var = tk.IntVar(value=1)
        self.prog_track_spin = ttk.Spinbox(prog_edit, from_=TRACK_MIN, to=TRACK_MAX, width=4,
                                           textvariable=self.prog_track_var)
        self.prog_track_spin.pack(side="left", padx=4)
        self.prog_all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            prog_edit, text="All tracks", variable=self.prog_all_var,
            command=lambda: self.prog_track_spin.configure(
                state="disabled" if self.prog_all_var.get() else "normal"),
        ).pack(side="left", padx=4)
        for text, cmd in (("Add Step", self._prog_add_step), ("Remove", self._prog_remove_step),
                          ("Up", lambda: self._prog_move(-1)), ("Down", lambda: self._prog_move(1)),
                          ("Clear", self._prog_clear)):
            ttk.Button(prog_edit, text=text, command=cmd).pack(side="left", padx=2)

        self.prog_tree = ttk.Treeview(prog_frame, columns=("step", "disc", "track"),
                                      show="headings", height=8, selectmode="browse")
        for col, text, width in (("step", "Step", 60), ("disc", "Disc", 300), ("track", "Track", 300)):
            self.prog_tree.heading(col, text=text)
            self.prog_tree.column(col, width=width, anchor="w", stretch=(col != "step"))
        self.prog_tree.pack(fill="both", expand=True, pady=(4, 0))
        self._refresh_userfiles_view()

        # -- Backup tab (v1.10.0) ---------------------------------------------
        ttk.Label(
            backup_tab, wraplength=860, justify="left",
            text="Export reads every slot (disc names, track names, genre, userfiles), plus "
                 "the userfile names and the program, and saves them to a file. Save as "
                 ".json for a backup you can restore, or .csv for a catalog to read or print. "
                 "It reads all 200 slots, so it takes a while.",
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            backup_tab, wraplength=860, justify="left", foreground="#a05a00",
            text="Restore writes a .json backup back to the changer. It only writes what "
                 "differs, never erases a name the backup doesn't have, and skips any slot "
                 "whose track count doesn't match the backup (probably a different disc; only "
                 "checked when the changer knows the count, i.e. the disc has been played "
                 "since power-on). "
                 "Each slot is re-read afterward to check it.",
        ).pack(anchor="w", pady=(0, 6))
        backup_toolbar = ttk.Frame(backup_tab)
        backup_toolbar.pack(fill="x")
        self.backup_export_btn = ttk.Button(backup_toolbar, text="Export Library...",
                                            command=self._start_library_export)
        self.backup_export_btn.pack(side="left", padx=2)
        self.backup_restore_btn = ttk.Button(backup_toolbar, text="Restore from Backup...",
                                             command=self._start_library_restore)
        self.backup_restore_btn.pack(side="left", padx=2)
        self.backup_stop_btn = ttk.Button(backup_toolbar, text="Stop", state="disabled",
                                          command=self._library_stop.set)
        self.backup_stop_btn.pack(side="left", padx=2)
        self.backup_progress_label = ttk.Label(backup_toolbar, text="")
        self.backup_progress_label.pack(side="left", padx=12)

        self._build_library_tab(library_tab)

        # -- Log console (outside the notebook -- visible on every tab) ---------
        log_frame = ttk.LabelFrame(body, text="Log", padding=4)
        log_frame.pack(fill="both", expand=False, pady=(4, 0))

        self.raw_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(log_frame, text="Show raw bytes (ENQ/ACK/EOT etc.)",
                         variable=self.raw_var).pack(anchor="w")

        text_frame = ttk.Frame(log_frame)
        text_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(text_frame, height=16, wrap="none", state="disabled",
                                  font=("Consolas", 9))
        yscroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=yscroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")

    def _build_library_tab(self, tab):
        """Library tab (v1.11.0): a disc table with search and filters, and
        the selected disc's tracks below it. See library_browser.py."""
        ttk.Label(
            tab, wraplength=860, justify="left", foreground="gray",
            text="Every disc in the changer. \"Scan Changer\" reads all 200 slots (the same "
                 "walk as the Backup tab's export, so it takes about a minute), and is kept "
                 "for next time. Search matches disc names, genres and track names. "
                 "Double-click a disc or track to play it; right-click a disc (or use "
                 "\"Userfiles\") to add it to a userfile or take it out.",
        ).pack(anchor="w", pady=(0, 4))

        toolbar = ttk.Frame(tab)
        toolbar.pack(fill="x")
        self.lib_scan_btn = ttk.Button(toolbar, text="Scan Changer", command=self._start_library_scan)
        self.lib_scan_btn.pack(side="left", padx=2)
        ttk.Button(toolbar, text="Open Backup...", command=self._open_browser_backup).pack(side="left", padx=2)
        self.lib_last_scan_btn = ttk.Button(toolbar, text="Show Last Scan", state="disabled",
                                            command=self._show_last_scan)
        self.lib_last_scan_btn.pack(side="left", padx=2)
        self.lib_stop_btn = ttk.Button(toolbar, text="Stop", state="disabled",
                                       command=self._library_stop.set)
        self.lib_stop_btn.pack(side="left", padx=2)
        self.lib_progress_label = ttk.Label(toolbar, text="")
        self.lib_progress_label.pack(side="left", padx=12)

        self.lib_source_label = ttk.Label(tab, text="Nothing scanned yet.", foreground="#a05a00",
                                          wraplength=860, justify="left")
        self.lib_source_label.pack(anchor="w", pady=(4, 4))

        filters = ttk.Frame(tab)
        filters.pack(fill="x")
        ttk.Label(filters, text="Search:").pack(side="left")
        self.lib_search_var = tk.StringVar()
        self.lib_search_var.trace_add("write", lambda *a: self._browser_refresh())
        ttk.Entry(filters, textvariable=self.lib_search_var, width=28).pack(side="left", padx=4)
        ttk.Label(filters, text="Genre:").pack(side="left", padx=(8, 0))
        self.lib_genre_var = tk.StringVar(value="All genres")
        self.lib_genre_combo = ttk.Combobox(filters, textvariable=self.lib_genre_var, width=20,
                                            state="readonly", values=["All genres"])
        self.lib_genre_combo.pack(side="left", padx=4)
        self.lib_genre_combo.bind("<<ComboboxSelected>>", lambda e: self._browser_refresh())
        ttk.Label(filters, text="Userfile:").pack(side="left", padx=(8, 0))
        self.lib_userfile_var = tk.StringVar(value="All userfiles")
        self.lib_userfile_combo = ttk.Combobox(filters, textvariable=self.lib_userfile_var, width=20,
                                               state="readonly", values=["All userfiles"])
        self.lib_userfile_combo.pack(side="left", padx=4)
        self.lib_userfile_combo.bind("<<ComboboxSelected>>", lambda e: self._browser_refresh())
        ttk.Button(filters, text="Clear", command=self._browser_clear_filters).pack(side="left", padx=4)
        self.lib_count_label = ttk.Label(filters, text="")
        self.lib_count_label.pack(side="left", padx=8)

        actions = ttk.Frame(tab)
        actions.pack(fill="x", pady=(4, 0))
        self.lib_play_btn = ttk.Button(actions, text="Play", command=self._browser_play)
        self.lib_play_btn.pack(side="left", padx=2)
        self.lib_edit_btn = ttk.Button(actions, text="Load in Disc Data Tab",
                                       command=self._browser_open_in_disc_data)
        self.lib_edit_btn.pack(side="left", padx=2)
        self.lib_rescan_btn = ttk.Button(actions, text="Rescan Disc", command=self._browser_rescan_disc)
        self.lib_rescan_btn.pack(side="left", padx=2)
        # v1.12.0: tick a userfile to add the disc to it, untick to take it
        # out. Also on a right-click on a disc.
        self.lib_userfile_btn = ttk.Menubutton(actions, text="Userfiles")
        self.lib_userfile_menu = tk.Menu(self.lib_userfile_btn, tearoff=False,
                                         postcommand=self._browser_fill_userfile_menu)
        self.lib_userfile_btn.configure(menu=self.lib_userfile_menu)
        self.lib_userfile_btn.pack(side="left", padx=2)
        self._browser_userfile_vars = [tk.BooleanVar(value=False) for _ in range(USERFILE_COUNT)]
        ttk.Label(
            actions, foreground="gray",
            text="The Disc Data tab only shows the loaded disc, so this loads it (and starts it playing).",
        ).pack(side="left", padx=8)

        panes = ttk.PanedWindow(tab, orient="vertical")
        panes.pack(fill="both", expand=True, pady=(4, 0))

        disc_frame = ttk.Frame(panes)
        self.lib_disc_tree = ttk.Treeview(disc_frame, columns=library_browser.SORT_KEYS,
                                          show="headings", height=5, selectmode="browse")
        for col, width in (("slot", 50), ("name", 300), ("genre", 150), ("userfiles", 130),
                           ("tracks", 60)):
            self.lib_disc_tree.heading(col, text=self.LIBRARY_COLUMN_TITLES[col],
                                       command=lambda c=col: self._browser_sort_by(c))
            self.lib_disc_tree.column(col, width=width, anchor="w", stretch=(col == "name"))
        disc_scroll = ttk.Scrollbar(disc_frame, orient="vertical", command=self.lib_disc_tree.yview)
        self.lib_disc_tree.configure(yscrollcommand=disc_scroll.set)
        self.lib_disc_tree.pack(side="left", fill="both", expand=True)
        disc_scroll.pack(side="right", fill="y")
        self.lib_disc_tree.bind("<<TreeviewSelect>>", lambda e: self._browser_show_tracks())
        self.lib_disc_tree.bind("<Double-1>", lambda e: self._browser_double_click(e, use_track=False))
        self.lib_disc_tree.bind("<Button-3>", self._browser_context_menu)
        panes.add(disc_frame, weight=3)

        track_frame = ttk.Frame(panes)
        self.lib_track_tree = ttk.Treeview(track_frame, columns=("track", "title"),
                                           show="headings", height=3, selectmode="browse")
        self.lib_track_tree.heading("track", text="Track")
        self.lib_track_tree.heading("title", text="Track Name")
        self.lib_track_tree.column("track", width=50, anchor="w", stretch=False)
        self.lib_track_tree.column("title", width=500, anchor="w", stretch=True)
        self.lib_track_tree.tag_configure("match", background="#fff3b0")
        track_scroll = ttk.Scrollbar(track_frame, orient="vertical", command=self.lib_track_tree.yview)
        self.lib_track_tree.configure(yscrollcommand=track_scroll.set)
        self.lib_track_tree.pack(side="left", fill="both", expand=True)
        track_scroll.pack(side="right", fill="y")
        self.lib_track_tree.bind("<Double-1>", lambda e: self._browser_double_click(e, use_track=True))
        panes.add(track_frame, weight=2)
        self._update_browser_buttons()

    # ------------------------------------------------------------------
    # UI-thread-safe logging / status updates
    # ------------------------------------------------------------------

    def _poll_ui_queue(self):
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.after(50, self._poll_ui_queue)

    def _log(self, msg: str):
        self.ui_queue.put(lambda: self._log_now(msg))

    def _log_now(self, msg: str):
        self.log_text.configure(state="normal")
        ts = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {msg}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_status(self, key: str, value: str):
        self.ui_queue.put(lambda: self.status_labels[key].configure(text=value))

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    def _refresh_ports(self):
        ports = []
        if list_ports is not None:
            ports = [p.device for p in list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def _toggle_connect(self):
        if self.link is None:
            self._connect()
        else:
            self._disconnect()

    def _connect(self):
        port = self.port_var.get()
        if not port:
            messagebox.showerror("No port selected", "Choose a serial port first.")
            return
        self.link = PCLinkConnection(port)
        self.link.on_frame = self._on_frame  # background thread callback
        self.link.on_raw = self._on_raw
        try:
            self.link.open()
        except Exception as exc:
            messagebox.showerror("Could not open port", str(exc))
            self.link = None
            return

        self.conn_status.configure(text="Connecting...", foreground="orange")
        self.connect_btn.configure(text="Disconnect")

        def do_handshake():
            try:
                self.link.handshake(timeout=5.0)
                self._log("Handshake sent (\"I'm PC\"). Waiting for changer identifier...")
                self.ui_queue.put(lambda: self.conn_status.configure(
                    text=f"Connected: {port}", foreground="green"))
            except PCLinkError as exc:
                self._log(f"Handshake failed: {exc}")
                self.ui_queue.put(lambda: self.conn_status.configure(
                    text="Handshake failed", foreground="red"))

        threading.Thread(target=do_handshake, daemon=True).start()

    def _disconnect(self):
        self._release_hold()
        self._library_stop.set()
        if self.link:
            self.link.close()
            self.link = None
        self._current_slot = None
        self._current_track = None
        self._last_state = None
        self._pending_mode = None
        self._disc_name_cache.clear()
        self._track_name_cache.clear()
        self._cdtext_slots.clear()
        self._genre_cache.clear()
        self._toc_cache.clear()
        for key in ("Disc Name", "Genre", "Track Name"):
            self.status_labels[key].configure(text="-")
        self.toc_tree.delete(*self.toc_tree.get_children())
        self.toc_summary_label.configure(text="No TOC loaded yet.")
        self.discid_label.configure(text="")
        self._custom_data_cache.clear()
        self._custom_genre_cache.clear()
        self.genre_changer_var.set("-")
        self.genre_gnudb_var.set("-")
        self.genre_custom_var.set("")
        for w in self.disc_data_rows_frame.winfo_children():
            w.destroy()
        self._disc_data_rows = []
        self._disc_data_slot = None
        self._show_album_art(None)
        self.disc_data_summary.configure(text="No current disc known yet.")
        self._reset_disc_map()
        self._userfiles_cache.clear()
        self._userfile_name_cache.clear()
        self._program_items = None
        self.uf_progress_label.configure(text="")
        self._refresh_userfiles_view()
        self._refresh_program_view()
        self.conn_status.configure(text="Disconnected", foreground="red")
        self.connect_btn.configure(text="Connect")

    # ------------------------------------------------------------------
    # Frame / raw callbacks (called on the link's IO thread)
    # ------------------------------------------------------------------

    def _on_raw(self, direction: str, data: bytes, note: str):
        if not self.raw_var.get():
            return
        hexstr = data.hex(" ")
        self._log(f"{direction}  {hexstr}   {note}")

    def _on_frame(self, frame: Frame):
        self._log(f"RX {frame.command_name}: {frame.payload}")

        if frame.command == proto.CMD_HANDSHAKE:
            ident = frame.payload.get("identifier", "?")
            self._log(f"Changer identified itself as: {ident!r}")

        elif frame.command == proto.CMD_STATE_EVENT:
            state = frame.payload.get("state")
            self._set_status("State", frame.payload.get("state_name", "-"))
            was_changing = self._last_state == proto.State.CHANGING
            self._last_state = state
            if was_changing and state != proto.State.CHANGING:
                self._on_disc_settled()
            else:
                # Refresh the TOC panel even when nothing else triggers it,
                # so e.g. entering Changing shows the "still changing"
                # message right away instead of only on the next unrelated
                # event. Harmless when a full TOC is already displayed --
                # _update_toc_display only uses the state for its message
                # in the "no data yet" case.
                self._update_toc_display()

        elif frame.command == proto.CMD_INFO_EVENT:
            p = frame.payload
            self._set_status("Disc (slot)", str(p.get("slot", "-")))
            self._set_status("Track", str(p.get("track", "-")))
            self._set_status("Program", str(p.get("program", "-")))
            self._set_status("Mode", p.get("mode_name", "-"))
            self._set_status("Mode Param", p.get("param_desc", "-"))
            if p.get("mode") == self._pending_mode:
                self._pending_mode = None
            self._set_status("Repeat", "On" if p.get("repeat") else "Off")
            self._set_status("Userfiles", ", ".join(p.get("userfile_names", [])) or "-")
            self._note_userfiles(p.get("slot"), p.get("userfiles"))
            self._note_current_position(p.get("slot"), p.get("track"))

        elif frame.command == proto.CMD_DISC_EVENT:
            slot = frame.payload.get("slot")
            self._set_status("Disc (slot)", str(slot if slot is not None else "-"))
            self._note_current_position(slot, self._current_track)

        elif frame.command == proto.CMD_DOOR_EVENT:
            self._set_status("Door", "OPEN" if frame.payload.get("door_open") else "Closed")

        elif frame.command == proto.CMD_DISC_INFO:
            p = frame.payload
            self._log(
                f"DiscInfo: slot={p.get('slot')} tracks={p.get('track_count')} "
                f"format={p.get('format_name')}"
            )
            slot = p.get("slot")
            if slot is not None:
                # track_count == 0 is the only signal DiscInfo gives for
                # "no disc here" -- confirmed against real hardware for one
                # occupied slot (track_count=12) and one empty slot
                # (track_count=0); the payload's "unknown" byte tracked the
                # same thing in both those examples (1 vs 0) but isn't used
                # here. Not yet confirmed whether a disc that's physically
                # present but blank/unreadable would also read as
                # track_count=0 and so look empty on the Disc Map -- if
                # that turns out to happen on real hardware, "unknown"
                # might be the better field to key off instead.
                occupied = (p.get("track_count") or 0) > 0
                self._disc_occupancy[slot] = occupied
                self._disc_track_count[slot] = p.get("track_count") or 0
                # Empty slots 100-101 report 0x90 too (v1.7.1): not a disc.
                self._note_disc_format(slot, p.get("format") if occupied else proto.Format.NO_CDTEXT)
                self.ui_queue.put(lambda s=slot, o=occupied: self._update_disc_map_cell(s, o))

        elif frame.command == proto.CMD_DISC_TOC:
            self._note_disc_format(frame.payload.get("slot"), frame.payload.get("format"))
            self._cache_toc(frame.payload)

        elif frame.command == proto.CMD_DISC_GENRE:
            p = frame.payload
            self._log(f"DiscGenre: slot={p.get('slot')} genre={p.get('genre_name')}")
            self._cache_genre(p)

        elif frame.command == proto.CMD_DISC_USERFILES:
            p = frame.payload
            self._log(
                f"DiscUserfiles: slot={p.get('slot')} "
                f"userfiles=0x{p.get('userfiles', 0):02X} {p.get('userfile_names')}"
            )
            self._note_userfiles(p.get("slot"), p.get("userfiles"))

        elif frame.command == proto.CMD_DISC_LISTING:
            p = frame.payload
            self._log(
                f"DiscListing: {p.get('length')} item(s)"
                f"{' (TRUNCATED frame)' if p.get('truncated') else ''}: "
                + ", ".join(f"slot {i['slot']}/track {'ALL' if i['all_tracks'] else i['track']}"
                            for i in p.get("items", []))
            )
            self._program_items = p.get("items", [])
            self._program_draft = program_steps_from_items(self._program_items)
            self._program_draft_dirty = False
            self.ui_queue.put(self._refresh_program_view)

        elif frame.command in (proto.CMD_TEXT_DATA, proto.CMD_LONG_TEXT_DATA):
            p = frame.payload
            index = p.get("index", p.get("track"))
            text = p.get("text")
            if p.get("info_type") in (proto.InfoType.DISC_NAMES, proto.InfoType.TRACK_NAMES):
                self._note_disc_format(p.get("slot"), p.get("format"))
            if frame.command == proto.CMD_LONG_TEXT_DATA and proto.is_placeholder_text(text or ""):
                # The endless stream a disc sends while it's in the drive
                # (v1.12.4, see pclink_link.PCLinkTextStream). It says
                # nothing about the stored names, so it isn't cached --
                # before v1.12.4 it blanked the disc name in the app.
                if p.get("seq") == 1:
                    self._log(f"LongTextData: slot={p.get('slot')} sending empty text "
                              f"frames (seq counting up) -- not stored names")
                return
            note = "  (placeholder: no title stored)" if proto.is_placeholder_text(text or "") else ""
            self._log(f"Text: slot={p.get('slot')} index={index} -> {text!r}{note}")
            self._cache_name(p)

    def _note_disc_format(self, slot, fmt):
        """Record whether `slot` holds a CD-Text disc, from a reply's format
        byte (v1.12.11): 0x90 counts as CD-Text (see proto.Format.
        SEEN_CDTEXT), 0x00 as not. Other values are left alone. Safe to
        call from the IO thread."""
        if slot is None or fmt not in (proto.Format.SEEN_CDTEXT, proto.Format.NO_CDTEXT):
            return
        cdtext = fmt == proto.Format.SEEN_CDTEXT
        if cdtext == (slot in self._cdtext_slots):
            return
        if cdtext:
            self._cdtext_slots.add(slot)
            self._log(f"Slot {slot} holds a CD-Text disc: its titles come from the disc, "
                      f"so the app won't write names to it.")
        else:
            self._cdtext_slots.discard(slot)
        if slot == self._current_slot:
            self._refresh_disc_data_from_changer()

    def _note_current_position(self, slot, track):
        """Called (on the IO thread) whenever InfoEvent/DiscEvent tells us
        the current slot/track. Auto-fetches disc/track names and the TOC
        the first time we see a given slot, and refreshes the displayed
        labels either way."""
        slot_changed = slot is not None and slot != self._current_slot
        if slot is not None:
            self._current_slot = slot
        if track is not None:
            self._current_track = track

        if slot_changed:
            self._log(f"Now on disc slot {slot} -- auto-fetching disc/track names, genre, and TOC")
            self._fetch_names_for_slot(slot)
            self._fetch_genre_for_slot(slot)
            self._fetch_toc_for_slot(slot)

        self._update_name_labels()
        self._update_genre_label()
        self._update_toc_display()
        self._refresh_disc_data_from_changer()

    def _cache_name(self, text_payload: dict):
        slot = text_payload.get("slot")
        info_type = text_payload.get("info_type")
        text = text_payload.get("text", "")
        if slot is None:
            return
        # Filler entries (e.g. '\x01' for unused track-title slots, v1.6.7)
        # mean "no title": store them as empty so they never show up as a
        # name, and so they overwrite rather than leave a stale real name.
        if proto.is_placeholder_text(text):
            text = ""

        if info_type == proto.InfoType.USERFILE_NAMES:
            index = text_payload.get("index", text_payload.get("track"))
            if index is not None:
                if text:
                    self._userfile_name_cache[index] = text
                else:
                    self._userfile_name_cache.pop(index, None)
                self.ui_queue.put(self._refresh_userfiles_view)
            return

        if info_type in (proto.InfoType.DISC_NAMES, proto.InfoType.TRACK_NAMES):
            # Every disc/track TextData reply also carries the disc's
            # userfile bitmask -- free membership data for the Userfiles view.
            self._note_userfiles(slot, text_payload.get("userfiles"))

        if info_type == proto.InfoType.DISC_NAMES:
            self._disc_name_cache[slot] = text
        elif info_type == proto.InfoType.TRACK_NAMES:
            # "index" (TextData) and "track" (LongTextData) are both used
            # directly as the track number, matching InfoEvent's "track"
            # field with no offset. Confirmed on real hardware: an earlier
            # version applied a +1 conversion here on the theory that
            # "index" was 0-based while InfoEvent's "track" was 1-based --
            # that was wrong and made every track name off by one. No
            # conversion needed.
            track = text_payload.get("index", text_payload.get("track"))
            if track is not None:
                tracks = self._track_name_cache.setdefault(slot, {})
                if text:
                    tracks[track] = text
                else:
                    tracks.pop(track, None)
        else:
            return  # artist name / userfile names etc. -- not shown in status panel

        self._update_name_labels()
        if slot == self._current_slot:
            self._refresh_disc_data_from_changer()

    def _note_userfiles(self, slot, userfiles):
        """Record a slot's userfile bitmask from any reply that carries one.
        Safe to call from the IO thread; the view refreshes via ui_queue."""
        if slot is None or userfiles is None:
            return
        if self._userfiles_cache.get(slot) != userfiles:
            self._userfiles_cache[slot] = userfiles
            if hasattr(self, "uf_tree"):
                self.ui_queue.put(self._refresh_userfiles_view)

    def _update_name_labels(self):
        slot, track = self._current_slot, self._current_track
        disc_name = self._disc_name_cache.get(slot) if slot is not None else None
        track_name = None
        if slot is not None and track is not None:
            track_name = self._track_name_cache.get(slot, {}).get(track)
        self._set_status("Disc Name", disc_name if disc_name else "-")
        self._set_status("Track Name", track_name if track_name else "-")

    def _cache_genre(self, payload: dict):
        slot = payload.get("slot")
        genre = payload.get("genre")
        if slot is None or genre is None:
            return
        self._genre_cache[slot] = genre
        self._update_genre_label()
        if slot == self._current_slot:
            self._refresh_disc_data_from_changer()

    def _update_genre_label(self):
        slot = self._current_slot
        genre = self._genre_cache.get(slot) if slot is not None else None
        name = proto.GENRES.get(genre, f"0x{genre:02X}") if genre is not None else None
        self._set_status("Genre", name if name else "-")

    def _fetch_genre_for_slot(self, slot: int):
        """Auto-triggered DiscGenre fetch, alongside _fetch_names_for_slot.
        Same DataAccess/RETRIEVE_DATA convention already used for
        DiscInfo/DiscTOC/TextData -- a request is assumed to produce
        exactly one reply of the matching command byte (CMD_DISC_GENRE
        here), per README.md's "Honest gaps" #3. Not yet confirmed against
        real hardware specifically for DiscGenre."""
        self._send_bg(
            proto.CMD_DATA_ACCESS,
            proto.encode_data_access(
                proto.Action.RETRIEVE_DATA, proto.DataType.DISC_GENRE, slot=slot,
            ),
            f"DataAccess(DiscGenre, slot={slot}) [auto]",
        )

    def _fetch_names_for_slot(self, slot: int):
        """Auto-triggered fetch (as opposed to the manual Get Disc Name /
        Get Track Names buttons, which use the slot spinbox). The two reads
        run one after the other, so the track names can be skipped when
        the disc name hit the endless LongTextData stream (v1.12.4): each
        stream costs about 13s of link time on real hardware."""
        link = self._require_link()
        if link is None:
            return

        threading.Thread(target=self._read_names_sync, args=(link, slot, " [auto]"),
                         daemon=True).start()

    # ------------------------------------------------------------------
    # Table of Contents / DiscID
    # ------------------------------------------------------------------

    def _fetch_toc_for_slot(self, slot: int, label_suffix: str = " [auto]"):
        """The changer only exposes TOC data for the currently-loaded disc,
        so unlike names this is never requested for an arbitrary slot --
        always the current one. Resets any previously-accumulated TOC data
        for this slot first, so a re-fetch (e.g. via the Refresh TOC
        button) starts clean rather than merging against a possibly-stale
        earlier read.

        Confirmed on real hardware: a request made while the changer is
        still physically loading the disc (StateEvent == Changing) comes
        back completely empty -- ACK'd frame, immediate EOT, no DiscTOC
        reply at all. Not an error exactly, just "not ready yet". Calling
        this while Changing isn't incorrect, just possibly wasted -- see
        _on_disc_settled for the automatic retry once the state clears."""
        self._toc_cache[slot] = {"tracks": {}, "leadout": None, "format": None, "format_name": None}
        self._send_bg(
            proto.CMD_DATA_ACCESS,
            proto.encode_data_access(proto.Action.RETRIEVE_DATA, proto.DataType.DISC_TOC, slot=slot),
            f"DataAccess(DiscTOC, slot={slot}){label_suffix}",
        )
        self._update_toc_display()

    def _on_disc_settled(self):
        """Called when StateEvent transitions away from Changing. If we
        don't yet have a complete TOC (with a lead-out) for the current
        slot -- e.g. because the slot-change auto-fetch fired before we
        knew we were still changing, which real hardware traces show can
        happen since InfoEvent's new-slot notice arrives before the
        Changing StateEvent does -- (re)fetch it now that the disc has
        actually settled."""
        slot = self._current_slot
        if slot is None:
            return
        entry = self._toc_cache.get(slot)
        have_leadout = entry is not None and entry.get("leadout") is not None
        if not have_leadout:
            self._log(f"Disc slot {slot} finished changing -- fetching TOC")
            self._fetch_toc_for_slot(slot, label_suffix=" [auto, post-change]")

    def _cache_toc(self, p: dict):
        """Accumulates one DiscTOC reply frame into self._toc_cache. A
        single request can produce more than one reply frame (observed
        with TrackNames returning one frame per track; DiscTOC's payload
        format similarly supports paged replies -- see cd_disctoc.html's
        "data page" field), so this merges rather than overwrites."""
        slot = p.get("slot")
        first_track = p.get("first_track")
        last_track = p.get("last_track")
        times = p.get("track_times", [])
        if slot is None or first_track is None or last_track is None or not times:
            return

        entry = self._toc_cache.setdefault(
            slot, {"tracks": {}, "leadout": None, "format": None, "format_name": None}
        )
        entry["format"] = p.get("format", entry["format"])
        entry["format_name"] = p.get("format_name", entry["format_name"])

        # Per cd_disctoc.html: a page carries (last_track-first_track+2)
        # toc_time entries -- one per track in [first_track, last_track],
        # plus one trailing entry. That trailing entry is the disc's
        # lead-out if this is the last (or only) page; if more pages
        # follow, later data simply overwrites it with the correct value.
        n_tracks_this_page = last_track - first_track + 1
        for i, t in enumerate(times):
            if i < n_tracks_this_page:
                entry["tracks"][first_track + i] = t
            else:
                entry["leadout"] = t

        self._log(
            f"DiscTOC: slot={slot} page tracks {first_track}-{last_track} "
            f"({len(entry['tracks'])} known so far"
            + (", leadout known" if entry["leadout"] else "")
            + ")"
        )

        if slot == self._current_slot:
            self._update_toc_display()
            self._refresh_disc_data_from_changer()

    def _update_toc_display(self):
        slot = self._current_slot
        entry = self._toc_cache.get(slot) if slot is not None else None

        self.ui_queue.put(lambda: self.toc_tree.delete(*self.toc_tree.get_children()))

        if not entry or not entry["tracks"]:
            if slot is None:
                msg = "No TOC loaded yet."
            elif self._last_state == proto.State.CHANGING:
                msg = f"Slot {slot} is changing -- TOC will load automatically once it settles."
            else:
                msg = f"No TOC loaded yet for slot {slot}."
            self.ui_queue.put(lambda: self.toc_summary_label.configure(text=msg))
            self.ui_queue.put(lambda: self.discid_label.configure(text=""))
            return

        track_nums = sorted(entry["tracks"].keys())
        track_times_ordered = [entry["tracks"][n] for n in track_nums]
        leadout = entry["leadout"]

        def fmt_mmss(t):
            return f"{t['minutes']:02d}:{t['seconds']:02d}"

        rows = []
        for idx, n in enumerate(track_nums):
            start = track_times_ordered[idx]
            if idx + 1 < len(track_times_ordered):
                next_start = track_times_ordered[idx + 1]
            elif leadout is not None:
                next_start = leadout
            else:
                next_start = None
            if next_start is not None:
                start_secs = start["minutes"] * 60 + start["seconds"]
                next_secs = next_start["minutes"] * 60 + next_start["seconds"]
                length_secs = max(next_secs - start_secs, 0)
                length_str = f"{length_secs // 60:02d}:{length_secs % 60:02d}"
            else:
                length_str = "-"
            rows.append((n, fmt_mmss(start), length_str))

        def populate():
            for row in rows:
                self.toc_tree.insert("", "end", values=row)

        self.ui_queue.put(populate)

        fmt_name = entry.get("format_name") or ""
        if leadout is not None:
            total_str = fmt_mmss(leadout)
            summary = f"Slot {slot} - {len(track_nums)} tracks - total {total_str}"
        else:
            summary = f"Slot {slot} - {len(track_nums)} tracks known so far (waiting for more data)"
        if fmt_name:
            summary += f"  ({fmt_name})"
        self.ui_queue.put(lambda: self.toc_summary_label.configure(text=summary))

        # DiscID needs a contiguous run of tracks from the first known
        # track through the last, plus the lead-out -- otherwise (e.g. only
        # some pages have arrived so far) it wouldn't be accurate yet.
        is_contiguous = track_nums == list(range(track_nums[0], track_nums[-1] + 1))
        if leadout is not None and is_contiguous:
            result = proto.calculate_cddb_discid(track_times_ordered + [leadout])
            if result:
                text = f"DiscID: {result['discid']}"
                self.ui_queue.put(lambda: self.discid_label.configure(text=text))
                return
        self.ui_queue.put(lambda: self.discid_label.configure(text=""))

    # ------------------------------------------------------------------
    # Disc Data tab. Column 3 ("Custom") writes back via Action.WRITE_NAME
    # for the Disc Name/Track rows, and the Genre row's dropdown folds
    # into that same write (see merge_genre_into_write_items()) rather
    # than a separate action -- both CONFIRMED against real hardware; see
    # pclink_link.py's send_write(), pclink_protocol.py's
    # encode_text_data, and CHANGELOG.md's v1.3.0/v1.5.0 entries.
    # ------------------------------------------------------------------

    def _track_count_for_slot(self, slot: int) -> int:
        """Best available track count for a slot: prefer the TOC (the
        authoritative source, when we have one), fall back to whatever
        track names we've seen, else 0 (just the Disc Name row)."""
        toc = self._toc_cache.get(slot)
        if toc and toc.get("tracks"):
            return max(toc["tracks"].keys())
        names = self._track_name_cache.get(slot)
        if names:
            return max(names.keys())
        return 0

    def _save_custom_data(self):
        """Snapshot whatever the user has typed into column 3 right now,
        keyed by the slot the rows currently represent, so it survives a
        slot switch and rebuild."""
        if self._disc_data_slot is None:
            return
        saved = {}
        for i, row in enumerate(self._disc_data_rows):
            text = row["custom_var"].get()
            if text:
                saved[i] = text
        if saved:
            self._custom_data_cache[self._disc_data_slot] = saved
        else:
            self._custom_data_cache.pop(self._disc_data_slot, None)

    def _save_custom_genre(self):
        """Same idea as _save_custom_data, for the Genre row's dropdown
        (which lives outside _disc_data_rows -- see the Genre row's
        comment in _build_ui)."""
        if self._disc_data_slot is None:
            return
        text = self.genre_custom_var.get()
        if text:
            self._custom_genre_cache[self._disc_data_slot] = text
        else:
            self._custom_genre_cache.pop(self._disc_data_slot, None)

    def _add_disc_data_row(self, index: int):
        row_var = tk.StringVar(value="")
        changer_var = tk.StringVar(value="-")
        gnudb_var = tk.StringVar(value="-")
        custom_var = tk.StringVar(value="")

        ttk.Label(self.disc_data_rows_frame, textvariable=row_var, anchor="w").grid(
            row=index, column=0, sticky="w", padx=(2, 8), pady=1
        )
        ttk.Label(self.disc_data_rows_frame, textvariable=changer_var, anchor="w").grid(
            row=index, column=1, sticky="w", padx=(2, 8), pady=1
        )
        ttk.Label(self.disc_data_rows_frame, textvariable=gnudb_var, anchor="w").grid(
            row=index, column=2, sticky="w", padx=(2, 8), pady=1
        )
        entry = ttk.Entry(self.disc_data_rows_frame, textvariable=custom_var)
        entry.grid(row=index, column=3, sticky="we", padx=(2, 8), pady=1)

        saved = self._custom_data_cache.get(self._disc_data_slot, {}).get(index)
        if saved:
            custom_var.set(saved)

        self._disc_data_rows.append({
            "row_var": row_var, "changer_var": changer_var,
            "gnudb_var": gnudb_var, "custom_var": custom_var, "entry": entry,
        })

    def _set_disc_data_row_count(self, slot: int, track_count: int):
        """Ensures exactly track_count+1 rows exist (row 0 = Disc Name,
        rows 1..N = tracks) for the given slot. Only tears down and rebuilds
        when the slot itself changes (saving any in-progress custom text
        first); growing the row count for the SAME slot just adds the
        missing rows, so existing Entry widgets (and whatever the user is
        mid-typing into them) are left alone."""
        if slot != self._disc_data_slot:
            self._save_custom_data()
            self._save_custom_genre()
            for w in self.disc_data_rows_frame.winfo_children():
                w.destroy()
            self._disc_data_rows = []
            self._disc_data_slot = slot
            self.genre_custom_var.set(self._custom_genre_cache.get(slot, ""))
            self._show_album_art(slot)

        while len(self._disc_data_rows) < track_count + 1:
            self._add_disc_data_row(len(self._disc_data_rows))

    def _refresh_disc_data_from_changer(self):
        """Populates column 1 (and the row labels) from whatever disc/track
        name data is already cached for the current slot -- the same data
        the status panel and Get Track Names/Get Disc Name buttons use.
        Auto-called whenever that data changes; the toolbar button is just
        a manual way to force the same refresh."""
        slot = self._current_slot

        def update():
            if slot is None:
                self.disc_data_summary.configure(text="No current disc known yet.")
                self.genre_changer_var.set("-")
                return
            track_count = self._track_count_for_slot(slot)
            self._set_disc_data_row_count(slot, track_count)

            self._disc_data_rows[0]["row_var"].set("Disc Name")
            disc_name = self._disc_name_cache.get(slot)
            self._disc_data_rows[0]["changer_var"].set(disc_name if disc_name else "-")

            genre = self._genre_cache.get(slot)
            self.genre_changer_var.set(
                proto.GENRES.get(genre, f"0x{genre:02X}") if genre is not None else "-"
            )

            names = self._track_name_cache.get(slot, {})
            for i in range(1, track_count + 1):
                self._disc_data_rows[i]["row_var"].set(f"Track {i}")
                self._disc_data_rows[i]["changer_var"].set(names.get(i, "-"))

            # A CD-Text disc's titles come from the disc (v1.12.11): no
            # name edits, only the genre.
            cdtext = slot in self._cdtext_slots
            for row in self._disc_data_rows:
                row["entry"].configure(state="disabled" if cdtext else "normal")
            note = (" -- CD-Text disc: its titles come from the disc and can't be changed "
                    "(the genre can)" if cdtext else "")
            self.disc_data_summary.configure(
                text=f"Slot {slot} -- {track_count} track(s) known{note}"
            )

        self.ui_queue.put(update)

    def _query_gnudb(self):
        """Computes a DiscID from the current disc's TOC (same calculation
        the TOC panel already shows) and queries gnudb.org for matching
        entries. Runs the actual network I/O on a background thread so it
        can't freeze the UI; results come back through self.ui_queue like
        everything else that touches widgets from a non-main thread."""
        slot = self._current_slot
        if slot is None:
            self._log("Query gnudb.org: no current disc known yet.")
            return

        toc = self._toc_cache.get(slot)
        if not toc or not toc.get("tracks") or toc.get("leadout") is None:
            self._log(
                "Query gnudb.org: TOC isn't fully loaded for this disc yet "
                "-- wait for it to finish (or click Refresh TOC)."
            )
            return

        track_nums = sorted(toc["tracks"].keys())
        is_contiguous = track_nums == list(range(track_nums[0], track_nums[-1] + 1))
        if not is_contiguous:
            self._log("Query gnudb.org: TOC data has gaps -- can't compute a reliable DiscID yet.")
            return

        track_times_ordered = [toc["tracks"][n] for n in track_nums]
        result = proto.calculate_cddb_discid(track_times_ordered + [toc["leadout"]])
        if not result:
            self._log("Query gnudb.org: couldn't compute a DiscID from the current TOC data.")
            return

        email = self.gnudb_email_var.get().strip()
        if "@" not in email:
            self._log(
                "Query gnudb.org: enter a contact email first -- gnudb.org's "
                "usage policy requires a real one in every request."
            )
            return

        hello = gnudb_client.build_hello(email, "KenwoodPCLinkController", APP_VERSION)
        self._log(f"Querying gnudb.org for DiscID {result['discid']}...")
        threading.Thread(
            target=self._gnudb_query_worker, args=(slot, result, hello), daemon=True
        ).start()

    def _gnudb_query_worker(self, slot: int, discid_result: dict, hello: str):
        try:
            matches = gnudb_client.query(
                discid_result["discid"],
                discid_result["track_offsets"],
                discid_result["total_seconds"],
                hello,
            )
        except gnudb_client.GnudbRateLimited as exc:
            self._log(f"gnudb.org query: rate-limited. {exc}")
            return
        except gnudb_client.GnudbError as exc:
            self._log(f"gnudb.org query failed: {exc}")
            return

        if not matches:
            self._log("gnudb.org: no match found for this disc.")
            return

        self._log(
            f"gnudb.org candidates for our DiscID {discid_result['discid']}: "
            + gnudb_match_summary(matches)
        )
        auto = gnudb_auto_read_match(matches)
        if auto is not None:
            self._log(f"gnudb.org: found an exact match -- {auto.title}")
            self._gnudb_read_worker(slot, auto, hello)
            return

        if not matches[0].exact:
            # The normal case on the CD-425M: its TOC gives whole seconds
            # only (frames always 0), so our DiscID rarely matches exactly
            # (v1.8.6, seen on every disc the user tried).
            self._log(
                f"gnudb.org: no exact match (normal for this changer -- its TOC "
                f"has whole seconds only); {len(matches)} close match(es) -- "
                "check it's the right album before picking."
            )
        else:
            self._log(f"gnudb.org: found {len(matches)} possible matches -- pick one.")
        self.ui_queue.put(lambda: self._show_gnudb_match_picker(slot, matches, hello))

    def _gnudb_read_worker(self, slot: int, match: "gnudb_client.GnudbMatch", hello: str):
        try:
            disc = gnudb_client.read(match.category, match.discid, hello)
        except gnudb_client.GnudbRateLimited as exc:
            self._log(f"gnudb.org read: rate-limited. {exc}")
            return
        except gnudb_client.GnudbError as exc:
            self._log(f"gnudb.org read failed: {exc}")
            return
        self._gnudb_cache[slot] = disc
        self._log(
            f"gnudb.org: loaded '{disc.artist} / {disc.album}' "
            f"({len(disc.track_titles)} track name(s))"
        )
        if slot == self._current_slot:
            self._update_disc_data_gnudb_column()
        self._album_art_worker(slot, disc)

    def _album_art_worker(self, slot: int, disc: "gnudb_client.GnudbDisc"):
        """Looks up cover art for a gnudb entry (on the gnudb worker
        thread, after the read): the entry's own "# Cover:" links first,
        then iTunes (album_art.fetch_album_art). A failure only gets
        logged: the art is a nice-to-have and never blocks the gnudb data
        itself."""
        gnudb_errors = []
        try:
            art = album_art.fetch_album_art(disc, errors=gnudb_errors)
        except album_art.AlbumArtError as exc:
            art, failure = None, exc
        else:
            failure = None
        for err in gnudb_errors:
            self._log(f"Cover art: gnudb's cover link failed: {err}")
        if failure is not None:
            self._log(f"Cover art lookup failed: {failure}")
            return
        self._album_art_cache[slot] = art
        if art is None:
            self._log(
                f"Cover art: none found (gnudb entry has {len(disc.cover_urls)} cover "
                f"link(s); no iTunes match for '{disc.artist} / {disc.album}')."
            )
        elif art.source == "gnudb":
            self._log(f"Cover art: from the gnudb entry ({art.url})")
        else:
            self._log(
                f"Cover art: gnudb entry has no usable cover; using iTunes' "
                f"'{art.artist} / {art.album}'"
                + ("" if art.exact else " (closest title, check it's the right album)")
            )
        self.ui_queue.put(lambda: self._show_album_art(self._disc_data_slot))

    def _show_album_art(self, slot):
        """Shows the cached cover for `slot` in the Disc Data tab, or a
        placeholder. UI thread only."""
        art = self._album_art_cache.get(slot) if slot is not None else None
        if art is None:
            self._album_art_photo = None
            text = "No cover art" if slot not in self._album_art_cache else "No cover found"
            self.album_art_label.configure(image="", text=text)
            return
        try:
            from PIL import ImageTk
            photo = ImageTk.PhotoImage(art.image, master=self)
        except (ImportError, tk.TclError) as exc:
            self._log(f"Cover art: couldn't display the image: {exc}")
            self.album_art_label.configure(image="", text="No cover art")
            return
        self._album_art_photo = photo
        self.album_art_label.configure(image=photo, text="")

    def _show_gnudb_match_picker(self, slot: int, matches: list, hello: str):
        win = tk.Toplevel(self)
        win.title("Select a gnudb.org match")
        win.geometry("520x320")
        prompt = (
            "Multiple possible matches were found -- select one:"
            if all(m.exact for m in matches) else
            "No exact match -- these are gnudb.org's closest guesses and may be\n"
            "a different album or pressing. Select one only if it's right:"
        )
        ttk.Label(win, text=prompt, justify="left").pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        listbox = tk.Listbox(win, width=76, height=12)
        for m in matches:
            listbox.insert("end", f"[{m.category} {m.discid}] {m.title}")
        listbox.pack(fill="both", expand=True, padx=8)
        if matches:
            listbox.selection_set(0)

        def on_select():
            selection = listbox.curselection()
            if not selection:
                return
            chosen = matches[selection[0]]
            win.destroy()
            self._log(f"Selected: [{chosen.category} {chosen.discid}] {chosen.title}")
            threading.Thread(
                target=self._gnudb_read_worker, args=(slot, chosen, hello), daemon=True
            ).start()

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=8, pady=8)
        ttk.Button(btns, text="Select", command=on_select).pack(side="left")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="left", padx=4)

    def _update_disc_data_gnudb_column(self):
        """Populates column 2 ("From gnudb.org") from a completed lookup.
        Only touches rows that already exist for the current slot -- if the
        row count doesn't match yet (e.g. gnudb reports more/fewer tracks
        than the changer's TOC did), extends via _set_disc_data_row_count
        the same way the changer-data refresh does, so nothing is silently
        dropped."""
        slot = self._current_slot
        disc = self._gnudb_cache.get(slot) if slot is not None else None
        if disc is None:
            return

        def update():
            track_count = max(
                self._track_count_for_slot(slot),
                max(disc.track_titles.keys(), default=0),
            )
            self._set_disc_data_row_count(slot, track_count)

            title = f"{disc.artist} / {disc.album}" if disc.artist else disc.album
            self._disc_data_rows[0]["gnudb_var"].set(title if title else "-")
            self.genre_gnudb_var.set(disc.genre if disc.genre else "-")
            for i in range(1, track_count + 1):
                self._disc_data_rows[i]["gnudb_var"].set(disc.track_titles.get(i, "-"))

        self.ui_queue.put(update)

    def _write_to_changer(self, prefetched: bool = False):
        """Writes every non-empty column-3 ("Custom") entry back to the
        changer for the slot currently shown in the Disc Data tab, via
        Action.WRITE_NAME -- confirmed against real hardware for names
        (see pclink_link.py's send_write() / pclink_protocol.py's
        encode_text_data). Row 0 is the disc name (InfoType.DISC_NAMES,
        index 0); rows 1..N are track names (InfoType.TRACK_NAMES, index =
        track number), matching the read side's index convention.

        If the Genre row's dropdown has a selection, it's folded into
        every name write's `genre` field (see
        merge_genre_into_write_items()) rather than sent via a separate
        action -- CONFIRMED against real hardware (v1.5.0-v1.5.1, see
        CHANGELOG.md): writing "Rock" then "Folk" to slot 2/4 this way was
        independently read back afterward and matched, and (v1.5.1) a
        plain track-name-only write afterward no longer resets genre back
        to "Unassigned" the way it did before that fix. Three earlier
        real-hardware attempts at a standalone Action.SET_DISC_GENRE
        write (v1.4.0-v1.4.2) all failed in three different ways before
        this approach was tried -- see CHANGELOG.md's v1.4.1-v1.5.1
        entries for that history.

        v1.8.0: every write also carries the disc's known userfile mask
        (TextData has a `userfiles` byte too, and it's assumed to behave
        like genre). If the slot's genre or userfiles haven't been read
        yet, they're read first; if they still can't be, nothing is
        written rather than risk resetting them."""
        link = self._require_link()
        if link is None:
            return
        slot = self._disc_data_slot
        if slot is None:
            messagebox.showinfo("Nothing to write", "No current disc loaded yet.")
            return
        genre_code = gather_genre_write_item(self.genre_custom_var.get())
        items = gather_disc_data_write_items(self._disc_data_rows)
        cdtext = slot in self._cdtext_slots
        if cdtext and genre_code is None:
            # Nothing it can write (names only): say so without reading.
            self._write_cdtext_disc_data(link, slot, items, genre_code)
            return
        if not self._ensure_disc_state(link, slot, self._write_to_changer, prefetched,
                                       self.dd_write_btn, need_name=cdtext):
            return

        if cdtext:
            self._write_cdtext_disc_data(link, slot, items, genre_code)
            return
        current_disc_name = self._disc_name_cache.get(slot)
        current_genre = self._genre_cache.get(slot)
        final_items, genre_error = merge_genre_into_write_items(
            items, genre_code, current_disc_name, current_genre,
        )

        if genre_error:
            messagebox.showinfo("Can't write genre", genre_error)
            if not items:
                return
            # Still preserve whatever genre is already known, even though
            # the requested genre CHANGE couldn't be applied -- every
            # TextData write sets the disc's genre (see
            # merge_genre_into_write_items), so falling back to 0 here
            # would silently erase an existing genre while just trying to
            # write names.
            final_items, _ = merge_genre_into_write_items(items, None, current_disc_name, current_genre)
            genre_code = None

        if not final_items:
            messagebox.showinfo("Nothing to write", "No custom values entered in column 3.")
            return

        genre_warning = (
            f"\n\nNote: genre will be written by re-sending the disc name "
            f"({final_items[0][1]!r}) with the genre attached."
            if genre_code is not None else ""
        ) + disc_name_length_warning(final_items)
        if not messagebox.askyesno(
            "Write to Changer",
            f"Write {len(final_items)} value(s) to slot {slot} on the changer?\n\n"
            "This overwrites whatever is currently stored there for each one."
            + genre_warning,
        ):
            return

        self.dd_write_btn.configure(state="disabled")
        userfiles = self._userfiles_cache[slot]
        self._log(
            f"Write to Changer: writing {len(final_items)} value(s) to slot {slot} "
            f"(keeping userfiles=0x{userfiles:02X})..."
        )
        threading.Thread(
            target=self._write_to_changer_worker, args=(link, slot, final_items),
            kwargs={"userfiles": userfiles}, daemon=True,
        ).start()

    def _write_cdtext_disc_data(self, link: PCLinkConnection, slot: int, name_items: list,
                                genre_code: int | None):
        """Write to Changer for a CD-Text disc (v1.12.11): only the genre,
        on a re-sent track 1 (see plan_cdtext_disc_data_write)."""
        items, message = plan_cdtext_disc_data_write(
            name_items, genre_code, self._track_name_cache.get(slot, {}).get(1))
        if not items:
            messagebox.showinfo("Write to Changer", message)
            return
        userfiles = self._userfiles_cache[slot]
        if not messagebox.askyesno(
            "Write to Changer",
            (message + "\n\n" if message else "")
            + f"Write genre {proto.GENRES.get(genre_code, genre_code)!r} to slot {slot}?\n\n"
            f"It's written by re-sending track 1's name ({items[0][1]!r}) with the genre "
            "attached, since this CD-Text disc's own disc title can't be read or set.",
        ):
            return
        self.dd_write_btn.configure(state="disabled")
        self._log(f"Write to Changer: slot {slot} is a CD-Text disc -- writing genre only, "
                  f"on track 1 (keeping userfiles=0x{userfiles:02X})...")
        threading.Thread(
            target=self._write_to_changer_worker, args=(link, slot, items),
            kwargs={"userfiles": userfiles}, daemon=True,
        ).start()

    def _ensure_disc_state(self, link: PCLinkConnection, slot: int, retry, prefetched: bool,
                           button, need_name: bool = False) -> bool:
        """Guard for a TextData write to `slot`: True if the slot's genre and
        userfiles (and disc name, if `need_name`) are all known. Otherwise,
        on the first call, reads what's missing on a background thread and
        then calls `retry(prefetched=True)` on the UI thread, returning
        False; on that second call, shows an error if something still
        couldn't be read, and returns False. The name needed is track 1's
        on a CD-Text disc (v1.12.11), which a disc-name read that streams
        can reveal, so the reads go round a second time if the list
        changes."""
        def still_missing():
            return missing_disc_state(slot, self._disc_name_cache, self._genre_cache,
                                      self._userfiles_cache, need_name=need_name,
                                      track_names=self._track_name_cache,
                                      cdtext=slot in self._cdtext_slots)
        missing = still_missing()
        if not missing:
            return True
        if prefetched:
            messagebox.showerror(
                "Can't write",
                f"Couldn't read slot {slot}'s current {', '.join(missing)}. Every name "
                "write re-sends the disc's genre and userfiles, so writing without "
                "knowing them could reset them. Nothing was written.",
            )
            return False
        self._log(f"Write: reading slot {slot}'s {', '.join(missing)} first, so the write keeps them...")
        button.configure(state="disabled")

        def work():
            self._read_disc_state_sync(link, slot, missing)
            again = still_missing()
            if again and again != missing:
                self._read_disc_state_sync(link, slot, again)

            def done():
                button.configure(state="normal")
                retry(prefetched=True)
            self.ui_queue.put(done)

        threading.Thread(target=work, daemon=True).start()
        return False

    def _read_disc_state_sync(self, link: PCLinkConnection, slot: int, what: list[str]):
        """Background thread: blocking reads of the listed disc state. The
        reply frames arrive (and are cached by _on_frame) during each
        send()'s own drain, so the caches are current once this returns."""
        requests = {
            "disc name": (proto.DataType.TEXT_DATA, proto.InfoType.DISC_NAMES, 0),
            "track 1's name": (proto.DataType.TEXT_DATA, proto.InfoType.TRACK_NAMES, 1),
            "genre": (proto.DataType.DISC_GENRE, 0, 0),
            "userfiles": (proto.DataType.DISC_USERFILES, 0, 0),
        }
        for item in what:
            data_type, info_type, track = requests[item]
            self._retrieve_sync(link, data_type, slot, info_type, item, track=track)

    def _retrieve_sync(self, link: PCLinkConnection, data_type: int, slot: int,
                       info_type: int = 0, what: str = "data", track: int = 0) -> bool:
        """Background thread: one blocking RETRIEVE_DATA request, retrying a
        timeout twice (likely a bus collision, see _send_bg). Replies are
        cached by _on_frame before this returns. False if it never went
        through. `track` 0 = all tracks (see encode_data_access)."""
        data = proto.encode_data_access(proto.Action.RETRIEVE_DATA, data_type,
                                        slot=slot, info_type=info_type, track=track)
        self._text_stream_slots.discard(slot)
        for attempt in range(3):
            try:
                link.send(proto.CMD_DATA_ACCESS, data)
                return True
            except PCLinkTimeout:
                time.sleep(0.1)
            except PCLinkTextStream as exc:
                self._text_stream_slots.add(slot)
                self._note_disc_format(slot, proto.Format.SEEN_CDTEXT)
                self._log(f"Reading slot {slot}'s {what} failed: {exc}")
                return False
            except PCLinkError as exc:
                self._log(f"Reading slot {slot}'s {what} failed: {exc}")
                return False
        self._log(f"Reading slot {slot}'s {what} timed out.")
        return False

    def _read_track_names_sync(self, link: PCLinkConnection, slot: int) -> bool:
        """Background thread: read a slot's track names into the cache.
        The usual one-request read (track 0 = all) first -- unless the disc
        name read just streamed -- and, if that streams, one request per
        track instead (v1.12.10). A CD-Text disc in the drive with no
        CD-Text disc title streams on "all", but answers each track with
        its full CD-Text title (CONFIRMED v1.12.9). False if the names
        couldn't be read."""
        if slot not in self._text_stream_slots:
            if self._retrieve_sync(link, proto.DataType.TEXT_DATA, slot,
                                   proto.InfoType.TRACK_NAMES, "track names"):
                return True
            if slot not in self._text_stream_slots:
                return False
        return self._read_tracks_one_by_one_sync(link, slot)

    def _read_tracks_one_by_one_sync(self, link: PCLinkConnection, slot: int) -> bool:
        """Background thread: one TrackNames request per track (the track in
        DataAccess's track byte), tracks 1..N with N from DiscInfo. With no
        known count, stops at the first track that gets no name (past the
        last track, the changer sends no frame -- CONFIRMED v1.12.9)."""
        count = library_backup.known_track_count(self._disc_track_count.get(slot))
        if not count:
            self._retrieve_sync(link, proto.DataType.DISC_INFO, slot, what="disc info")
            count = library_backup.known_track_count(self._disc_track_count.get(slot))
        self._log(f"Slot {slot}: reading track names one track at a time "
                  f"({count or 'unknown number of'} tracks)...")
        self._track_name_cache[slot] = {}
        for n in range(1, (count or 99) + 1):
            if not self._retrieve_sync(link, proto.DataType.TEXT_DATA, slot,
                                       proto.InfoType.TRACK_NAMES, f"track {n}'s name", track=n):
                return False
            if not count and n not in self._track_name_cache.get(slot, {}):
                break
        self._update_name_labels()
        if slot == self._current_slot:
            self._refresh_disc_data_from_changer()
        return True

    def _read_names_sync(self, link: PCLinkConnection, slot: int, label: str = "") -> None:
        """Background thread: disc name, then track names (see
        _read_track_names_sync). A disc-name read that streams means a
        CD-Text disc in the drive with no CD-Text disc title (the front
        panel shows "----"), so the disc name is shown as empty."""
        if self._retrieve_sync(link, proto.DataType.TEXT_DATA, slot,
                               proto.InfoType.DISC_NAMES, "disc name"):
            self._log(f"TX DataAccess(DiscName, slot={slot}){label} - sent ok")
        elif slot in self._text_stream_slots:
            self._log(f"Slot {slot} is a CD-Text disc in the drive with no CD-Text disc title.")
            self._disc_name_cache[slot] = ""
            self._update_name_labels()
        if self._read_track_names_sync(link, slot):
            self._log(f"TX DataAccess(TrackNames, slot={slot}){label} - sent ok")

    def _write_to_changer_worker(self, link: PCLinkConnection, slot: int, items: list,
                                 userfiles: int = 0, on_done=None):
        """Runs on its own background thread -- one send_write() per item,
        same one-at-a-time approach as the Disc Map scan worker
        (PCLinkConnection already serializes sends through its own IO
        thread regardless, but going one at a time here keeps the log
        readable and lets one bad item not block the rest).

        Each item is (index, text, info_type, label, genre) --
        merge_genre_into_write_items()'s output. `genre` is 0 ("don't
        care") for every item except, when a genre was selected in the
        Disc Data tab's Genre row, the disc-name item -- which carries it
        alongside the text, per cd_textdata.html's documented TextData
        payload shape (slot/index/userfiles/info_type/genre/format/text).

        `userfiles` (v1.8.0) is the disc's userfile mask, sent in every
        item's `userfiles` byte: the currently-known mask for a plain name
        write, or the new one for a userfile-membership write. `on_done`
        runs on the UI thread afterward (default: re-enable the Disc Data
        tab's write button).
        """
        ok = 0
        failed = 0
        wrote_genre = False
        for item in items:
            if self._send_text_write(link, slot, item, userfiles, "Write to Changer"):
                ok += 1
                if item[4]:
                    wrote_genre = True
            else:
                failed += 1

        self._log(f"Write to Changer: done -- {ok} written, {failed} failed.")
        if ok:
            # Re-read from the changer so column 1 (and the Genre row)
            # reflect what was actually written, rather than trusting the
            # write locally.
            self._read_names_sync(link, slot)
            if wrote_genre:
                self._send_bg(
                    proto.CMD_DATA_ACCESS,
                    proto.encode_data_access(
                        proto.Action.RETRIEVE_DATA, proto.DataType.DISC_GENRE, slot=slot,
                    ),
                    f"DataAccess(DiscGenre, slot={slot})",
                )
            # Always re-read userfiles too: that's what shows whether the
            # changer honors (or ignores) TextData's userfiles byte.
            self._send_bg(
                proto.CMD_DATA_ACCESS,
                proto.encode_data_access(
                    proto.Action.RETRIEVE_DATA, proto.DataType.DISC_USERFILES, slot=slot,
                ),
                f"DataAccess(DiscUserfiles, slot={slot})",
            )
        self.ui_queue.put(on_done or (lambda: self.dd_write_btn.configure(state="normal")))

    def _send_text_write(self, link: PCLinkConnection, slot: int, item: tuple, userfiles: int,
                         log_prefix: str) -> bool:
        """One WRITE_NAME (the CONFIRMED name-write path) for an
        (index, text, info_type, label, genre) item, logged. True if the
        changer took it."""
        index, text, info_type, label, genre = item
        request_data = proto.encode_data_access(
            proto.Action.WRITE_NAME, proto.DataType.TEXT_DATA,
            slot=slot, info_type=info_type,
        )
        follow_up_data = proto.encode_text_data(
            slot=slot, index=index, text=text, info_type=info_type, genre=genre,
            userfiles=userfiles,
        )
        suffix = " (genre attached)" if genre else ""
        return self._send_write_logged(
            link, f"{log_prefix}: {label}", request_data, proto.CMD_TEXT_DATA, follow_up_data,
            ok_note=f" -> {text!r} written ok{suffix}.",
        )

    def _send_write_logged(self, link: PCLinkConnection, label: str, request_data: bytes,
                           follow_up_command: int, follow_up_data: bytes,
                           ok_note: str = ": written ok.") -> bool:
        """One blocking send_write(), with the outcome logged. True if the
        changer took it."""
        try:
            link.send_write(proto.CMD_DATA_ACCESS, request_data, follow_up_command, follow_up_data)
            self._log(f"{label}{ok_note}")
            return True
        except PCLinkWriteUnconfirmed:
            self._log(f"{label} -- changer never sent ReadyForData, not written.")
        except PCLinkNak:
            self._log(f"{label} -- changer NAK'd the write.")
        except PCLinkTimeout:
            self._log(f"{label} -- timed out.")
        except PCLinkError as exc:
            self._log(f"{label} -- error: {exc}")
        return False

    def _copy_column_to_custom(self, source: str):
        var_key = "changer_var" if source == "changer" else "gnudb_var"
        for row in self._disc_data_rows:
            val = row[var_key].get()
            if source == "gnudb":
                val = ascii_fold(val)  # curly quotes etc. would be stored as '?'
            row["custom_var"].set(val if val and val != "-" else "")

        genre_source_var = self.genre_changer_var if source == "changer" else self.genre_gnudb_var
        genre_val = genre_source_var.get()
        # The Custom column's Genre control is a fixed dropdown (only the
        # changer's own enum, per proto.GENRES) -- see match_changer_genre.
        self.genre_custom_var.set(match_changer_genre(genre_val))

    def _clear_custom_data(self):
        for row in self._disc_data_rows:
            row["custom_var"].set("")
        if self._disc_data_slot is not None:
            self._custom_data_cache.pop(self._disc_data_slot, None)
        self.genre_custom_var.set("")
        if self._disc_data_slot is not None:
            self._custom_genre_cache.pop(self._disc_data_slot, None)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _require_link(self) -> PCLinkConnection | None:
        if self.link is None:
            messagebox.showerror("Not connected", "Connect to the changer first.")
            return None
        return self.link

    def _send_bg(self, command: int, data: bytes, label: str, retries: int = 2):
        link = self._require_link()
        if link is None:
            return

        def do_send():
            attempt = 0
            while True:
                try:
                    link.send(command, data)
                    self._log(f"TX {label} - sent ok")
                    return
                except PCLinkNak:
                    # A real rejection (bad checksum on the changer's end),
                    # not a transient race -- retrying blindly isn't
                    # obviously helpful, so don't.
                    self._log(f"TX {label} - changer NAK'd the frame")
                    return
                except PCLinkTimeout:
                    # Confirmed on real hardware: this can happen when our
                    # outgoing ENQ collides with the changer trying to
                    # start its OWN transaction at nearly the same moment
                    # (we read back the changer's ENQ where we expected our
                    # ACK, and the send fails outright) -- a one-off race,
                    # not a real communication failure. Retrying almost
                    # always succeeds immediately after. Without this, a
                    # collision on an auto-fetch (nobody watching to retry
                    # by hand) meant that data just silently never arrived.
                    attempt += 1
                    if attempt > retries:
                        self._log(
                            f"TX {label} - timed out waiting for response "
                            f"(gave up after {attempt} attempts)"
                        )
                        return
                    self._log(
                        f"TX {label} - timed out (possible bus collision), "
                        f"retrying ({attempt}/{retries})..."
                    )
                    time.sleep(0.1)
                except PCLinkError as exc:
                    self._log(f"TX {label} - error: {exc}")
                    return

        threading.Thread(target=do_send, daemon=True).start()

    def _send_action(self, action_code: int, label: str):
        name = proto.ACTION_COMMAND_NAMES.get(action_code, label)
        self._send_bg(proto.CMD_DO_ACTION, proto.encode_do_action(action_code), f"DoAction({name})")

    def _start_repeat(self, action_code: int):
        self._release_hold()
        self._ff_fb_stop.clear()

        def loop():
            while not self._ff_fb_stop.is_set():
                self._send_action(action_code, "repeat")
                self._ff_fb_stop.wait(REPEAT_INTERVAL)

        self._ff_fb_thread = threading.Thread(target=loop, daemon=True)
        self._ff_fb_thread.start()

    def _release_hold(self):
        """Called on release of any press-and-hold transport button (FF/FB,
        which repeat while held, and Previous/Next, which don't repeat but
        still require the same Finish Repeatable close-out). Confirmed on
        real hardware that Previous/Next need this too, not just the two
        actions marked "(repeatable)" in the source docs' table."""
        self._ff_fb_stop.set()
        if self._ff_fb_thread and self._ff_fb_thread.is_alive():
            self._ff_fb_thread.join(timeout=1)
        self._ff_fb_thread = None
        if self.link is not None:
            self._send_action(proto.ActionCommand.FINISH_REPEATABLE, "Finish Repeatable")

    def _note_query_slot(self, slot: int, track: int | None = None):
        """Called from the passive Get ... lookup buttons. Only used to
        BOOTSTRAP the "current slot" tracking when we have no real
        information yet (e.g. right after connecting, before any
        InfoEvent/DiscEvent has arrived) -- so a first manual lookup at
        least shows up in the status panel instead of being cached but
        never displayed. Once we know the real current slot from an actual
        event, a lookup of some OTHER slot (just checking what's on a
        different disc) must NOT override it -- confirmed this was
        misleading the status panel into showing a slot that wasn't
        actually playing."""
        if self._current_slot is not None:
            return
        self._current_slot = slot
        self._current_track = track
        self._update_name_labels()

    def _change_disc(self):
        slot = self.slot_var.get()
        track = self.track_var.get()
        # Don't optimistically set _current_slot here: confirmed on real
        # hardware that doing so breaks the auto-fetch entirely. The real
        # InfoEvent that follows a successful ChangeDisc checks "did the
        # slot change?" by comparing against _current_slot -- if we'd
        # already (optimistically) set it to the target slot ourselves,
        # that check sees no change and never fires the auto-fetch for the
        # new disc's name/track names, leaving the status panel stuck on
        # "-" even though the disc did actually change. Just let the real
        # event drive it, the same way Next/Previous/etc already do
        # correctly. _note_query_slot only bootstraps the very first,
        # otherwise-unknown slot (a genuinely fresh connect), so it can't
        # cause the same problem in normal use.
        self._send_change_disc(slot, track)

    def _send_change_disc(self, slot: int, track: int):
        self._note_query_slot(slot, track)
        self._send_bg(
            proto.CMD_CHANGE_DISC,
            proto.encode_change_disc(slot, track, begin=True),
            f"ChangeDisc(slot={slot}, track={track})",
        )

    def _update_mode_param_widgets(self):
        mode = MODE_NAME_TO_CODE.get(self.mode_var.get())
        self.mode_genre_combo.configure(state="readonly" if mode in proto.GENRE_MODES else "disabled")
        self.mode_userfile_spin.configure(state="readonly" if mode in proto.USERFILE_MODES else "disabled")

    def _change_mode(self):
        try:
            payload, label = build_change_mode_request(
                self.mode_var.get(), self.mode_genre_var.get(), self.mode_userfile_var.get(),
            )
        except ValueError as exc:
            messagebox.showerror("Can't change mode", str(exc))
            return
        # Like ChangeDisc, don't touch the Mode status row here -- the
        # changer's own InfoEvent is what confirms the mode actually took.
        mode = MODE_NAME_TO_CODE[self.mode_var.get()]
        self._pending_mode = mode
        self._send_bg(proto.CMD_CHANGE_MODE, payload, label)
        self.after(MODE_CHANGE_TIMEOUT_MS, lambda: self._check_mode_took(mode))

    def _check_mode_took(self, mode: int):
        if self._pending_mode != mode:
            return  # took, or superseded by a later Set Mode click
        self._pending_mode = None
        self._log(
            f"{proto.MODE_NAMES[mode]} didn't take -- the changer accepted the "
            f"command but never reported the new mode."
        )

    def _get_disc_info(self):
        slot = self.slot_var.get()
        self._note_query_slot(slot)
        self._send_bg(
            proto.CMD_DATA_ACCESS,
            proto.encode_data_access(proto.Action.RETRIEVE_DATA, proto.DataType.DISC_INFO, slot=slot),
            f"DataAccess(DiscInfo, slot={slot})",
        )

    def _get_disc_toc(self):
        # TOC is only valid for the currently-loaded disc (confirmed by the
        # user against real hardware) -- unlike the other Get ... buttons,
        # this deliberately ignores the slot spinbox.
        if self._current_slot is None:
            self._log("Refresh TOC: no current disc known yet -- change/play a disc first.")
            return
        if self._last_state == proto.State.CHANGING:
            self._log(
                "Refresh TOC: disc is still changing -- it'll be fetched "
                "automatically once it settles, no need to retry by hand."
            )
            return
        self._fetch_toc_for_slot(self._current_slot, label_suffix="")

    def _get_track_names(self):
        link = self._require_link()
        if link is None:
            return
        slot = self.slot_var.get()
        self._note_query_slot(slot)

        def work():
            self._text_stream_slots.discard(slot)
            if self._read_track_names_sync(link, slot):
                self._log(f"TX DataAccess(TrackNames, slot={slot}) - sent ok")
        threading.Thread(target=work, daemon=True).start()

    def _get_disc_name(self):
        slot = self.slot_var.get()
        self._note_query_slot(slot)
        self._send_bg(
            proto.CMD_DATA_ACCESS,
            proto.encode_data_access(
                proto.Action.RETRIEVE_DATA, proto.DataType.TEXT_DATA, slot=slot,
                info_type=proto.InfoType.DISC_NAMES,
            ),
            f"DataAccess(DiscName, slot={slot})",
        )

    def _get_genre(self):
        slot = self.slot_var.get()
        self._note_query_slot(slot)
        self._send_bg(
            proto.CMD_DATA_ACCESS,
            proto.encode_data_access(
                proto.Action.RETRIEVE_DATA, proto.DataType.DISC_GENRE, slot=slot,
            ),
            f"DataAccess(DiscGenre, slot={slot})",
        )

    # ------------------------------------------------------------------
    # Disc Map
    # ------------------------------------------------------------------

    def _disc_map_click(self, slot: int):
        """Query a single slot's DiscInfo by clicking its cell. Declines
        while a full scan is running rather than interleaving with it --
        the two would both land on the same serialized send queue in
        PCLinkConnection so nothing would actually break, but the
        progress/log output would be confusing to follow."""
        if self._disc_map_scanning:
            self._log("Disc Map: a scan is already running -- stop it first to query a slot by hand.")
            return
        self._note_query_slot(slot)
        self._send_bg(
            proto.CMD_DATA_ACCESS,
            proto.encode_data_access(proto.Action.RETRIEVE_DATA, proto.DataType.DISC_INFO, slot=slot),
            f"DataAccess(DiscInfo, slot={slot}) [Disc Map]",
        )

    def _start_disc_map_scan(self):
        link = self._require_link()
        if link is None:
            return
        if self._disc_map_scanning:
            return
        self._disc_map_scanning = True
        self._disc_map_stop.clear()
        self.disc_map_scan_btn.configure(state="disabled")
        self.disc_map_stop_btn.configure(state="normal")
        self.disc_map_progress_label.configure(
            text=f"Scanning slot 1/{self.DISC_MAP_SLOT_COUNT}..."
        )
        self._log(f"Disc Map: starting a scan of all {self.DISC_MAP_SLOT_COUNT} slots...")
        threading.Thread(target=self._disc_map_scan_worker, args=(link,), daemon=True).start()

    def _stop_disc_map_scan(self):
        self._disc_map_stop.set()

    def _disc_map_scan_worker(self, link: PCLinkConnection):
        """Runs on its own background thread. Sends DiscInfo requests one
        slot at a time via link.send() directly (rather than the
        fire-and-forget _send_bg, which would spin up 200 threads at
        once) -- PCLinkConnection already serializes every send through
        its own IO thread regardless, but going one at a time here makes
        it straightforward to report progress and to stop cleanly
        partway through. Retries a timeout up to twice, same as
        _send_bg's collision-retry logic, since the same one-off bus
        collision that can affect any other send applies here too."""
        stopped_early = False
        for slot in range(1, self.DISC_MAP_SLOT_COUNT + 1):
            if self._disc_map_stop.is_set():
                stopped_early = True
                break
            self.ui_queue.put(
                lambda s=slot: self.disc_map_progress_label.configure(
                    text=f"Scanning slot {s}/{self.DISC_MAP_SLOT_COUNT}..."
                )
            )
            attempt = 0
            while True:
                try:
                    link.send(
                        proto.CMD_DATA_ACCESS,
                        proto.encode_data_access(
                            proto.Action.RETRIEVE_DATA, proto.DataType.DISC_INFO, slot=slot
                        ),
                    )
                    break
                except PCLinkNak:
                    self._log(f"Disc Map scan: slot {slot} NAK'd -- leaving it unmarked.")
                    break
                except PCLinkTimeout:
                    attempt += 1
                    if attempt > 2:
                        self._log(f"Disc Map scan: slot {slot} timed out -- leaving it unmarked.")
                        break
                    time.sleep(0.1)
                except PCLinkError as exc:
                    self._log(f"Disc Map scan: slot {slot} error: {exc} -- leaving it unmarked.")
                    break
        self.ui_queue.put(lambda: self._on_disc_map_scan_done(stopped_early))

    def _on_disc_map_scan_done(self, stopped_early: bool):
        self._disc_map_scanning = False
        self.disc_map_scan_btn.configure(state="normal")
        self.disc_map_stop_btn.configure(state="disabled")
        occupied = sum(1 for v in self._disc_occupancy.values() if v)
        queried = len(self._disc_occupancy)
        if stopped_early:
            self.disc_map_progress_label.configure(
                text=f"Stopped -- {queried}/{self.DISC_MAP_SLOT_COUNT} queried, {occupied} have a disc."
            )
            self._log("Disc Map: scan stopped.")
        else:
            self.disc_map_progress_label.configure(
                text=f"Done -- {occupied}/{self.DISC_MAP_SLOT_COUNT} slots have a disc."
            )
            self._log(f"Disc Map: scan complete -- {occupied}/{self.DISC_MAP_SLOT_COUNT} slots have a disc.")

    def _update_disc_map_cell(self, slot: int, occupied: bool):
        rect = self._disc_map_cells.get(slot)
        if rect is None:
            return
        color = self.DISC_MAP_COLOR_OCCUPIED if occupied else self.DISC_MAP_COLOR_EMPTY
        self.disc_map_canvas.itemconfig(rect, fill=color)

    # ------------------------------------------------------------------
    # Userfiles & Program tab (read-only, v1.7.0)
    # ------------------------------------------------------------------

    def _refresh_userfiles_view(self):
        selected = self.uf_tree.selection()
        self.uf_tree.delete(*self.uf_tree.get_children())
        saved = self._browser_scan
        rows = userfile_rows(self._userfiles_cache, self._userfile_name_cache,
                             self._disc_name_cache, saved)
        for n, row in enumerate(rows, start=1):
            self.uf_tree.insert("", "end", iid=str(n), values=row)
        if selected and self.uf_tree.exists(selected[0]):
            self.uf_tree.selection_set(selected[0])
        if scan_only_slots(self._userfiles_cache, saved):
            when = library_browser.format_when(saved.get("exported_at"))
            self.uf_saved_note.configure(
                text=f"* From the Library tab's scan on {when}, not read from the changer yet "
                     "this session. \"Read Userfiles for Known Discs\" refreshes them.")
        else:
            self.uf_saved_note.configure(text="")
        for n, cb in enumerate(self.uf_member_checks, start=1):
            cb.configure(text=f"#{n} {rows[n - 1][1]}" if rows[n - 1][1] != "-" else f"#{n}")

    def _refresh_program_view(self):
        selected = self.prog_tree.selection()
        self.prog_tree.delete(*self.prog_tree.get_children())
        rows = program_rows(program_items_from_steps(self._program_draft),
                            self._disc_name_cache, self._track_name_cache)
        for i, row in enumerate(rows):
            self.prog_tree.insert("", "end", iid=str(i), values=row)
        if selected and self.prog_tree.exists(selected[0]):
            self.prog_tree.selection_set(selected[0])
        if self._program_draft_dirty:
            text = f"{len(rows)} step(s) -- edited, not written yet."
        elif self._program_items is None:
            text = "Not read yet."
        else:
            text = f"{len(rows)} step(s)." if rows else "No program stored."
        self.prog_summary_label.configure(text=text)

    # -- Writing (v1.8.0, CONFIRMED on real hardware v1.8.1) --------------

    def _write_single_bg(self, link: PCLinkConnection, label: str, request_data: bytes,
                         follow_up_command: int, follow_up_data: bytes,
                         reread: tuple[bytes, str], button):
        """One send_write() on a background thread, logged like the Disc
        Data tab's writes, then a re-read so the view shows what the changer
        actually stored rather than what we sent."""
        button.configure(state="disabled")

        def work():
            try:
                link.send_write(proto.CMD_DATA_ACCESS, request_data, follow_up_command, follow_up_data)
                self._log(f"{label}: written ok -- re-reading to check.")
                self._send_bg(proto.CMD_DATA_ACCESS, reread[0], reread[1])
            except PCLinkWriteUnconfirmed:
                self._log(f"{label}: changer never sent ReadyForData, not written.")
            except PCLinkNak:
                self._log(f"{label}: changer NAK'd the write.")
            except PCLinkTimeout:
                self._log(f"{label}: timed out.")
            except PCLinkError as exc:
                self._log(f"{label}: error: {exc}")
            self.ui_queue.put(lambda: button.configure(state="normal"))

        threading.Thread(target=work, daemon=True).start()

    def _rename_userfile(self):
        link = self._require_link()
        if link is None:
            return
        selected = self.uf_tree.selection()
        if not selected:
            messagebox.showinfo("Rename userfile", "Select a userfile in the table first.")
            return
        number = int(selected[0])
        current = self._userfile_name_cache.get(proto.userfile_param(number), "")
        name = simpledialog.askstring(
            "Rename userfile",
            f"New name for userfile #{number} (up to {USERFILE_NAME_MAX} characters):",
            initialvalue=current, parent=self,
        )
        if name is None:
            return
        try:
            request_data, follow_up_data = build_userfile_name_write(number, name)
        except ValueError as exc:
            messagebox.showerror("Rename userfile", str(exc))
            return
        self._write_single_bg(
            link, f"Rename userfile #{number} -> {name.strip()!r}",
            request_data, proto.CMD_TEXT_DATA, follow_up_data,
            (proto.encode_data_access(proto.Action.RETRIEVE_DATA, proto.DataType.TEXT_DATA, slot=0,
                                      info_type=proto.InfoType.USERFILE_NAMES),
             "DataAccess(UserfileNames)"),
            self.uf_rename_btn,
        )

    def _uf_member_slot(self) -> int | None:
        try:
            slot = int(self.uf_member_slot_var.get())
        except (tk.TclError, ValueError):
            slot = None
        if slot is None or not SLOT_MIN <= slot <= SLOT_MAX:
            messagebox.showerror("Disc slot", f"Enter a disc slot from {SLOT_MIN} to {SLOT_MAX}.")
            return None
        return slot

    def _uf_member_use_current(self):
        if self._current_slot is None:
            messagebox.showinfo("Current disc", "The current disc isn't known yet.")
            return
        self.uf_member_slot_var.set(self._current_slot)
        self._uf_member_load()

    def _uf_member_show(self, slot: int):
        """Tick the boxes from the cached mask for `slot` (UI thread)."""
        mask = self._userfiles_cache.get(slot)
        name = self._disc_name_cache.get(slot)
        if mask is None:
            self.uf_member_label.configure(text=f"Slot {slot}: couldn't read its userfiles.")
            return
        for var, on in zip(self.uf_member_vars, userfile_flags_from_mask(mask)):
            var.set(on)
        self.uf_member_label.configure(
            text=f"Slot {slot}{f' ({name})' if name else ''}: userfiles=0x{mask:02X}"
        )

    def _uf_member_load(self):
        slot = self._uf_member_slot()
        if slot is None:
            return
        if slot in self._userfiles_cache:
            self._uf_member_show(slot)
            return
        link = self._require_link()
        if link is None:
            return
        self.uf_member_label.configure(text=f"Reading slot {slot}...")

        def work():
            self._read_disc_state_sync(link, slot, ["userfiles"])
            self.ui_queue.put(lambda: self._uf_member_show(slot))

        threading.Thread(target=work, daemon=True).start()

    def _write_userfile_membership(self, prefetched: bool = False):
        """Set which userfiles the chosen disc is in by re-sending its disc
        name with the new mask (see plan_userfile_membership_write)."""
        link = self._require_link()
        if link is None:
            return
        slot = self._uf_member_slot()
        if slot is None:
            return
        if not self._ensure_disc_state(link, slot, self._write_userfile_membership, prefetched,
                                       self.uf_member_write_btn, need_name=True):
            return
        mask = userfile_mask_from_flags([v.get() for v in self.uf_member_vars])
        cdtext = slot in self._cdtext_slots
        items, error = plan_userfile_membership_write(
            self._disc_name_cache.get(slot), self._genre_cache.get(slot), cdtext,
            self._track_name_cache.get(slot, {}).get(1))
        if error:
            messagebox.showinfo("Can't set userfiles", error)
            return
        old = self._userfiles_cache[slot]
        carrier = ("track 1's name, since this CD-Text disc's disc title can't be read"
                   if cdtext else "the disc name")
        if not messagebox.askyesno(
            "Write Disc's Userfiles",
            f"Set slot {slot}'s userfiles to {', '.join(proto.userfile_list(mask)) or 'none'} "
            f"(0x{old:02X} -> 0x{mask:02X})?\n\n"
            f"This re-sends {carrier} ({items[0][1]!r}) with the new userfiles attached.",
        ):
            return
        self.uf_member_write_btn.configure(state="disabled")
        self._log(f"Userfiles: writing slot {slot} userfiles=0x{mask:02X} (was 0x{old:02X})...")

        def done():
            self.uf_member_write_btn.configure(state="normal")
            self.dd_write_btn.configure(state="normal")

        threading.Thread(
            target=self._write_to_changer_worker, args=(link, slot, items),
            kwargs={"userfiles": mask, "on_done": done}, daemon=True,
        ).start()

    def _prog_selected_index(self) -> int | None:
        selected = self.prog_tree.selection()
        return int(selected[0]) if selected else None

    def _prog_edited(self, select: int | None = None):
        self._program_draft_dirty = True
        self._refresh_program_view()
        if select is not None and self.prog_tree.exists(str(select)):
            self.prog_tree.selection_set(str(select))
            self.prog_tree.see(str(select))

    def _prog_add_step(self):
        if len(self._program_draft) >= PROGRAM_MAX_STEPS:
            messagebox.showinfo("Program full", f"A program holds at most {PROGRAM_MAX_STEPS} steps.")
            return
        try:
            slot = int(self.prog_slot_var.get())
            track = proto.LISTING_ALL_TRACKS if self.prog_all_var.get() else int(self.prog_track_var.get())
            build_program_write([(slot, track)])  # range check only
        except (tk.TclError, ValueError) as exc:
            messagebox.showerror("Add step", str(exc) or "Enter a disc slot and track number.")
            return
        # Insert after the selected step, else at the end.
        index = self._prog_selected_index()
        pos = len(self._program_draft) if index is None else index + 1
        self._program_draft.insert(pos, (slot, track))
        self._prog_edited(select=pos)

    def _prog_remove_step(self):
        index = self._prog_selected_index()
        if index is None:
            return
        del self._program_draft[index]
        self._prog_edited(select=min(index, len(self._program_draft) - 1))

    def _prog_move(self, delta: int):
        index = self._prog_selected_index()
        if index is None:
            return
        target = index + delta
        if not 0 <= target < len(self._program_draft):
            return
        steps = self._program_draft
        steps[index], steps[target] = steps[target], steps[index]
        self._prog_edited(select=target)

    def _prog_clear(self):
        if self._program_draft:
            self._program_draft = []
            self._prog_edited()

    def _write_program(self):
        link = self._require_link()
        if link is None:
            return
        steps = list(self._program_draft)
        try:
            request_data, follow_up_data = build_program_write(steps)
        except ValueError as exc:
            messagebox.showerror("Write Program", str(exc))
            return
        if steps:
            what = f"this {len(steps)}-step program"
            effect = "and the changer may switch to Program mode and start playing it."
        else:
            # v1.12.2, CONFIRMED: clears it, and a changer in Program mode
            # drops back to Track mode.
            what = "an EMPTY program (clearing it)"
            effect = "and if the changer is in Program mode it goes back to Track mode."
        if not messagebox.askyesno(
            "Write Program",
            f"Write {what} to the changer?\n\nThis replaces the program stored there, {effect}",
        ):
            return
        self._write_single_bg(
            link, f"Write Program ({len(steps)} step(s))",
            request_data, proto.CMD_DISC_LISTING, follow_up_data,
            (proto.encode_data_access(proto.Action.RETRIEVE_DATA, proto.DataType.DISC_LISTING, slot=0),
             "DataAccess(DiscListing / program)"),
            self.prog_write_btn,
        )

    def _read_userfile_names(self):
        # slot=0 -- CONFIRMED (v1.7.1): returns all 8 names, one TextData
        # per userfile, indexed by userfile bit; unnamed ones are '\x01'.
        self._send_bg(
            proto.CMD_DATA_ACCESS,
            proto.encode_data_access(
                proto.Action.RETRIEVE_DATA, proto.DataType.TEXT_DATA, slot=0,
                info_type=proto.InfoType.USERFILE_NAMES,
            ),
            "DataAccess(UserfileNames)",
        )

    def _read_program(self):
        # slot=0 -- CONFIRMED (v1.7.1): returns the stored program as one
        # DiscListing frame.
        self._send_bg(
            proto.CMD_DATA_ACCESS,
            proto.encode_data_access(proto.Action.RETRIEVE_DATA, proto.DataType.DISC_LISTING, slot=0),
            "DataAccess(DiscListing / program)",
        )

    def _known_disc_slots(self) -> list[int]:
        """Slots the app knows hold a disc: occupied on the Disc Map, with
        a disc name read back, or (v1.12.1) in the Library's saved scan,
        unless the Disc Map found them empty this session."""
        known = {s for s, occupied in self._disc_occupancy.items() if occupied}
        known |= set(self._disc_name_cache)
        if self._browser_scan:
            known |= {d["slot"] for d in self._browser_scan["discs"]
                      if self._disc_occupancy.get(d["slot"]) is not False}
        return sorted(known)

    def _read_userfiles_for_known_discs(self):
        link = self._require_link()
        if link is None or self._userfile_scan_running:
            return
        slots = self._known_disc_slots()
        if not slots:
            messagebox.showinfo(
                "No discs known yet",
                "The app doesn't know which slots hold discs yet. Run "
                "\"Scan All 200 Slots\" on the Disc Map tab first.",
            )
            return
        self._userfile_scan_running = True
        self.uf_scan_btn.configure(state="disabled")
        threading.Thread(target=self._userfile_scan_worker, args=(link, slots), daemon=True).start()

    def _userfile_scan_worker(self, link: PCLinkConnection, slots: list[int]):
        """One DataAccess(DiscUserfiles) per known slot, sent one at a time
        with the same collision retry as the Disc Map scan."""
        for i, slot in enumerate(slots, start=1):
            self.ui_queue.put(
                lambda i=i, s=slot: self.uf_progress_label.configure(
                    text=f"Reading slot {s} ({i}/{len(slots)})..."
                )
            )
            attempt = 0
            while True:
                try:
                    link.send(
                        proto.CMD_DATA_ACCESS,
                        proto.encode_data_access(
                            proto.Action.RETRIEVE_DATA, proto.DataType.DISC_USERFILES, slot=slot
                        ),
                    )
                    break
                except PCLinkTimeout:
                    attempt += 1
                    if attempt > 2:
                        self._log(f"Userfiles read: slot {slot} timed out -- skipped.")
                        break
                    time.sleep(0.1)
                except PCLinkError as exc:
                    self._log(f"Userfiles read: slot {slot} error: {exc} -- skipped.")
                    break
        self.ui_queue.put(self._on_userfile_scan_done)

    def _on_userfile_scan_done(self):
        self._userfile_scan_running = False
        self.uf_scan_btn.configure(state="normal")
        self.uf_progress_label.configure(text="Done.")
        self._refresh_userfiles_view()

    # ------------------------------------------------------------------
    # Backup tab (v1.10.0) -- see library_backup.py
    # ------------------------------------------------------------------

    def _read_slot_state_sync(self, link: PCLinkConnection, slot: int) -> dict:
        """Background thread: read everything a backup holds for one slot.
        The slot's cached values are dropped first, so a reply that never
        comes shows up as None rather than as a stale value. Keys match
        a parsed backup disc (see library_backup.plan_disc_restore)."""
        state = {"track_count": None, "name": None, "tracks": None, "genre": None, "userfiles": None,
                 "cdtext": False}
        self._disc_track_count.pop(slot, None)
        if not self._retrieve_sync(link, proto.DataType.DISC_INFO, slot, what="disc info"):
            return state
        state["track_count"] = self._disc_track_count.get(slot)
        if not state["track_count"]:
            return state
        # DiscInfo's format byte (v1.12.11, _note_disc_format); the name
        # reads below can still add it (the stream).
        state["cdtext"] = slot in self._cdtext_slots
        self._disc_name_cache.pop(slot, None)
        self._track_name_cache.pop(slot, None)
        self._genre_cache.pop(slot, None)
        self._userfiles_cache.pop(slot, None)
        if self._retrieve_sync(link, proto.DataType.TEXT_DATA, slot, proto.InfoType.DISC_NAMES,
                               "disc name"):
            state["name"] = self._disc_name_cache.get(slot)
        if self._read_track_names_sync(link, slot):
            # No TrackNames frame at all (the read closed with no reply)
            # means not read, not "no names": v1.12.4's hardware test
            # saved slot 4 with an empty track list that way.
            tracks = self._track_name_cache.get(slot)
            state["tracks"] = None if tracks is None else dict(tracks)
        if self._retrieve_sync(link, proto.DataType.DISC_GENRE, slot, what="genre"):
            state["genre"] = self._genre_cache.get(slot)
        if self._retrieve_sync(link, proto.DataType.DISC_USERFILES, slot, what="userfiles"):
            state["userfiles"] = self._userfiles_cache.get(slot)
        state["cdtext"] = slot in self._cdtext_slots
        return state

    def _library_can_start(self) -> PCLinkConnection | None:
        link = self._require_link()
        if link is None or self._library_running:
            return None
        if self._disc_map_scanning or self._userfile_scan_running:
            messagebox.showinfo("Busy", "Wait for the Disc Map or Userfiles scan to finish first.")
            return None
        return link

    def _library_begin(self, text: str, label=None):
        """Start an export, restore or Library scan. `label` is the
        progress label of the tab it was started from (default: Backup)."""
        self._library_running = True
        self._library_stop.clear()
        self._library_label = label or self.backup_progress_label
        for btn in (self.backup_export_btn, self.backup_restore_btn, self.lib_scan_btn,
                    self.lib_rescan_btn, self.lib_userfile_btn):
            btn.configure(state="disabled")
        self.backup_stop_btn.configure(state="normal")
        self.lib_stop_btn.configure(state="normal")
        self._library_label.configure(text=text)

    def _library_progress(self, text: str):
        label = self._library_label
        self.ui_queue.put(lambda: label.configure(text=text))

    def _library_finish(self, text: str, message: str | None = None):
        """Background thread: hand the end of an export/restore to the UI."""
        self._log(text)

        label = self._library_label

        def done():
            self._library_running = False
            for btn in (self.backup_export_btn, self.backup_restore_btn, self.lib_scan_btn):
                btn.configure(state="normal")
            self.backup_stop_btn.configure(state="disabled")
            self.lib_stop_btn.configure(state="disabled")
            self._update_browser_buttons()
            label.configure(text=text)
            if message:
                messagebox.showinfo("Library" if label is self.lib_progress_label else "Backup", message)
        self.ui_queue.put(done)

    def _start_library_export(self):
        link = self._library_can_start()
        if link is None:
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Export Library", defaultextension=".json",
            initialfile=f"changer-library-{datetime.date.today().isoformat()}.json",
            filetypes=[("Backup, restorable (*.json)", "*.json"), ("Catalog (*.csv)", "*.csv")],
        )
        if not path:
            return
        self._library_begin("Starting export...")
        self._log(f"Export: reading all {self.DISC_MAP_SLOT_COUNT} slots, then saving to {path}")
        threading.Thread(target=self._library_export_worker, args=(link, path), daemon=True).start()

    def _read_all_slots_sync(self, link: PCLinkConnection) -> tuple[list, list] | None:
        """Background thread: every slot's backup record (the export and
        the Library scan). Returns (disc records, unreadable slots), or
        None if stopped or disconnected partway."""
        discs, unreadable = [], []
        for slot in range(1, self.DISC_MAP_SLOT_COUNT + 1):
            if self._library_stop.is_set() or self.link is not link:
                return None
            self._library_progress(
                f"Reading slot {slot}/{self.DISC_MAP_SLOT_COUNT} ({len(discs)} disc(s) so far)...")
            state = self._read_slot_state_sync(link, slot)
            if state["track_count"] is None:
                unreadable.append(slot)
            elif state["track_count"]:
                discs.append(library_backup.disc_record(slot, **state))
        return discs, unreadable

    def _read_userfile_names_sync(self, link: PCLinkConnection) -> dict[int, str] | None:
        """Background thread: the userfile names keyed by bit, or None."""
        self._userfile_name_cache.clear()
        if self._retrieve_sync(link, proto.DataType.TEXT_DATA, 0, proto.InfoType.USERFILE_NAMES,
                               "userfile names"):
            return dict(self._userfile_name_cache)
        return None

    def _library_export_worker(self, link: PCLinkConnection, path: str):
        result = self._read_all_slots_sync(link)
        if result is None:
            self._library_finish("Export stopped -- nothing was saved.")
            return
        discs, unreadable = result

        self._library_progress("Reading userfile names and the program...")
        names = self._read_userfile_names_sync(link)
        if self._program_draft_dirty:
            # Reading the program replaces the editor's draft.
            self._log("Export: the program editor has unwritten edits, so the program wasn't "
                      "re-read; saving the last program read instead.")
            program = self._program_items
        else:
            self._program_items = None
            program = None
            if self._retrieve_sync(link, proto.DataType.DISC_LISTING, 0, what="program"):
                program = self._program_items

        library = library_backup.build_library(
            discs, names, program, APP_VERSION,
            datetime.datetime.now().isoformat(timespec="seconds"),
        )
        self.ui_queue.put(lambda: self._set_browser_scan(library))
        as_csv = path.lower().endswith(".csv")
        text = library_backup.library_to_csv(library) if as_csv else library_backup.library_to_json(library)
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        except OSError as exc:
            self._library_finish(f"Export: couldn't save {path}: {exc}", f"Couldn't save the file:\n{exc}")
            return

        unknown_counts = [d["slot"] for d in discs if d["track_count"] is None]
        incomplete = [d["slot"] for d in discs
                      if any(d[k] is None for k in ("name", "tracks", "genre", "userfiles"))]
        notes = []
        if unreadable:
            notes.append(f"{len(unreadable)} slot(s) couldn't be read: {unreadable}")
        if unknown_counts:
            notes.append(f"{len(unknown_counts)} disc(s) haven't been played since the changer "
                         f"was switched on, so their track count isn't known yet (null in the "
                         f"file, and restore can't use it to check the disc): {unknown_counts}")
        if incomplete:
            notes.append(f"{len(incomplete)} disc(s) are missing a value (null in the file): {incomplete}")
        if names is None:
            notes.append("the userfile names couldn't be read")
        if program is None:
            notes.append("the program couldn't be read")
        for note in notes:
            self._log(f"Export: {note}.")
        self._library_finish(
            f"Export: saved {len(discs)} disc(s) to {path}.",
            f"Saved {len(discs)} disc(s) to {path}."
            + ("\n\nNote: " + "; ".join(notes) + ". See the log." if notes else ""),
        )

    def _start_library_restore(self):
        link = self._library_can_start()
        if link is None:
            return
        path = filedialog.askopenfilename(
            parent=self, title="Restore from Backup",
            filetypes=[("Library backup (*.json)", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                library = library_backup.parse_library(f.read(), fold=ascii_fold)
        except (OSError, UnicodeDecodeError, library_backup.LibraryError) as exc:
            messagebox.showerror("Restore from Backup", str(exc))
            return
        discs, names, program = library["discs"], library["userfile_names"], library["program"]
        if not messagebox.askyesno(
            "Restore from Backup",
            f"Restore {len(discs)} disc(s) and {len(names)} userfile name(s) from the backup "
            f"made {library['exported_at'] or '(date unknown)'}?\n\n"
            "Only values that differ from the changer are written, nothing is erased, and "
            "slots whose track count doesn't match the backup are skipped (when the "
            "count is known).",
        ):
            return
        restore_program = bool(program) and messagebox.askyesno(
            "Restore the program too?",
            f"The backup also has a {len(program)}-step program. Write it too?\n\n"
            "Writing a program makes the changer switch to Program mode and start playing it.",
        )
        self._library_begin("Starting restore...")
        self._log(f"Restore: from {path} -- {len(discs)} disc(s)"
                  f"{', plus the program' if restore_program else ''}.")
        threading.Thread(target=self._library_restore_worker,
                         args=(link, library, restore_program), daemon=True).start()

    def _library_restore_worker(self, link: PCLinkConnection, library: dict, restore_program: bool):
        """For each backed-up disc: read the slot, write what differs
        (library_backup.plan_disc_restore), then read it again and check
        nothing still differs."""
        verified, unchanged, skipped, failed = 0, 0, [], []
        discs = library["discs"]
        for i, saved in enumerate(discs, start=1):
            if self._library_stop.is_set() or self.link is not link:
                self._library_stop.set()
                break
            slot = saved["slot"]
            self._library_progress(f"Restoring slot {slot} ({i}/{len(discs)})...")
            items, mask, reason = library_backup.plan_disc_restore(
                saved, self._read_slot_state_sync(link, slot))
            if reason:
                self._log(f"Restore: slot {slot} skipped -- {reason}.")
                skipped.append(slot)
                continue
            if not items:
                unchanged += 1
                continue
            self._log(f"Restore: slot {slot} -- writing {len(items)} value(s) "
                      f"(genre={items[0][4]}, userfiles=0x{mask:02X})...")
            for item in items:
                self._send_text_write(link, slot, item, mask, f"Restore slot {slot}")
            left, _, why = library_backup.plan_disc_restore(saved, self._read_slot_state_sync(link, slot))
            if why or left:
                detail = why or "still different: " + ", ".join(item[3] for item in left)
                self._log(f"Restore: slot {slot} NOT verified after writing -- {detail}.")
                failed.append(slot)
            else:
                self._log(f"Restore: slot {slot} verified -- re-read matches the backup.")
                verified += 1

        names_note = ""
        if library["userfile_names"] and not self._library_stop.is_set():
            names_note = self._restore_userfile_names_sync(link, library["userfile_names"])
        program_note = ""
        if restore_program and not self._library_stop.is_set():
            program_note = self._restore_program_sync(link, library["program"])

        stopped = " (stopped early)" if self._library_stop.is_set() else ""
        summary = (f"Restore{stopped}: {verified} disc(s) written and verified, {unchanged} already "
                   f"matched, {len(skipped)} skipped, {len(failed)} not verified.")
        details = []
        if skipped:
            details.append(f"Skipped slots: {skipped}")
        if failed:
            details.append(f"Not verified: {failed}")
        details += [n for n in (names_note, program_note) if n]
        self._library_finish(summary, summary + ("\n\n" + "\n".join(details) if details else "")
                             + "\n\nSee the log for details.")

    def _restore_userfile_names_sync(self, link: PCLinkConnection, saved: dict[int, str]) -> str:
        self._library_progress("Restoring userfile names...")
        self._userfile_name_cache.clear()
        if not self._retrieve_sync(link, proto.DataType.TEXT_DATA, 0, proto.InfoType.USERFILE_NAMES,
                                   "userfile names"):
            return "Userfile names: couldn't read the current ones, so none were written."
        todo = library_backup.plan_userfile_names_restore(saved, self._userfile_name_cache)
        for number, name in todo:
            try:
                request_data, follow_up_data = build_userfile_name_write(number, name)
            except ValueError as exc:
                self._log(f"Restore: userfile #{number} name {name!r} not written -- {exc}")
                continue
            self._send_write_logged(link, f"Restore: userfile #{number} -> {name!r}",
                                    request_data, proto.CMD_TEXT_DATA, follow_up_data)
        if not todo:
            return "Userfile names: already matched."
        self._retrieve_sync(link, proto.DataType.TEXT_DATA, 0, proto.InfoType.USERFILE_NAMES,
                            "userfile names")
        left = library_backup.plan_userfile_names_restore(saved, self._userfile_name_cache)
        if left:
            return f"Userfile names: {len(left)} still differ after writing: {[n for n, _ in left]}"
        return f"Userfile names: {len(todo)} written and verified."

    def _restore_program_sync(self, link: PCLinkConnection, steps: list[tuple[int, int]]) -> str:
        self._library_progress("Restoring the program...")
        try:
            request_data, follow_up_data = build_program_write(steps)
        except ValueError as exc:
            self._log(f"Restore: program not written -- {exc}")
            return f"Program: not written ({exc})."
        if not self._send_write_logged(link, f"Restore: program ({len(steps)} step(s))",
                                       request_data, proto.CMD_DISC_LISTING, follow_up_data):
            return "Program: the write failed."
        self._program_items = None
        self._retrieve_sync(link, proto.DataType.DISC_LISTING, 0, what="program")
        if self._program_items is not None and program_steps_from_items(self._program_items) == steps:
            return f"Program: {len(steps)} step(s) written and verified."
        return "Program: written, but the re-read didn't match."

    # ------------------------------------------------------------------
    # Library tab (v1.11.0) -- see library_browser.py
    # ------------------------------------------------------------------

    def _load_library_cache(self):
        """Show the last changer scan, saved by an earlier session."""
        path = self.library_cache_path
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            parsed = library_browser.load_library_text(text)
        except (OSError, UnicodeDecodeError, library_backup.LibraryError) as exc:
            self._log(f"Library: couldn't read the saved scan {path}: {exc}")
            return
        self._browser_raw = json.loads(text)
        self._browser_scan = parsed
        self._browser_file = None
        self._show_browser_library(parsed)
        self._refresh_userfiles_view()

    def _set_browser_scan(self, raw: dict):
        """UI thread: show a fresh changer scan (or export) and save it for
        next time."""
        self._browser_raw = raw
        self._browser_scan = library_browser.load_library_text(library_backup.library_to_json(raw))
        self._browser_file = None
        if self.library_cache_path:
            try:
                library_browser.save_cache(self.library_cache_path, raw)
            except OSError as exc:
                self._log(f"Library: couldn't save the scan to {self.library_cache_path}: {exc}")
        self._show_last_scan()
        self._refresh_userfiles_view()

    def _show_last_scan(self):
        if self._browser_scan is not None:
            self._browser_file = None
            self._show_browser_library(self._browser_scan)

    def _open_browser_backup(self):
        path = filedialog.askopenfilename(
            parent=self, title="Open Backup",
            filetypes=[("Library backup (*.json)", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                parsed = library_browser.load_library_text(f.read())
        except (OSError, UnicodeDecodeError, library_backup.LibraryError) as exc:
            messagebox.showerror("Open Backup", str(exc))
            return
        self._browser_file = path
        self._show_browser_library(parsed)

    def _show_browser_library(self, parsed: dict):
        self._browser_library = parsed
        when = library_browser.format_when(parsed.get("exported_at"))
        if self._browser_file:
            source = (f"From the backup file {os.path.basename(self._browser_file)}, made {when}. "
                      "Slots may hold different discs now.")
        else:
            source = (f"From a changer scan on {when}. Discs moved, added or renamed since "
                      "then won't show until you scan again.")
        self.lib_source_label.configure(text=source)

        self._browser_genres = dict(library_browser.genre_choices(parsed["discs"]))
        self._browser_userfiles = dict(library_browser.userfile_choices(parsed["userfile_names"]))
        self.lib_genre_combo.configure(values=["All genres", *self._browser_genres])
        self.lib_userfile_combo.configure(values=["All userfiles", *self._browser_userfiles])
        if self.lib_genre_var.get() not in self._browser_genres:
            self.lib_genre_var.set("All genres")
        if self.lib_userfile_var.get() not in self._browser_userfiles:
            self.lib_userfile_var.set("All userfiles")
        self._browser_refresh()

    def _browser_clear_filters(self):
        self.lib_genre_var.set("All genres")
        self.lib_userfile_var.set("All userfiles")
        self.lib_search_var.set("")
        self._browser_refresh()

    def _browser_sort_by(self, key: str):
        current, reverse = self._browser_sort
        self._browser_sort = (key, not reverse if key == current else False)
        self._browser_refresh()

    def _browser_refresh(self):
        """Refill the disc table from the library, search and filters,
        keeping the selected disc selected if it's still shown."""
        selected = self.lib_disc_tree.selection()
        self.lib_disc_tree.delete(*self.lib_disc_tree.get_children())
        if self._browser_library is None:
            self.lib_count_label.configure(text="")
            self._browser_show_tracks()
            return
        discs = self._browser_library["discs"]
        shown = library_browser.filter_discs(
            discs, self.lib_search_var.get(),
            genre=self._browser_genres.get(self.lib_genre_var.get()),
            userfile=self._browser_userfiles.get(self.lib_userfile_var.get()),
        )
        key, reverse = self._browser_sort
        for disc in library_browser.sort_discs(shown, key, reverse):
            self.lib_disc_tree.insert("", "end", iid=str(disc["slot"]),
                                      values=library_browser.disc_row(disc))
        for col, text in self.LIBRARY_COLUMN_TITLES.items():
            arrow = (" ▼" if reverse else " ▲") if col == key else ""
            self.lib_disc_tree.heading(col, text=text + arrow)
        self.lib_count_label.configure(text=library_browser.summary(shown, discs))
        if selected and self.lib_disc_tree.exists(selected[0]):
            self.lib_disc_tree.selection_set(selected[0])
            self.lib_disc_tree.see(selected[0])
        # Also refreshes the track highlights for a new search, since
        # re-selecting the same disc doesn't fire <<TreeviewSelect>>.
        self._browser_show_tracks()

    def _browser_selected_disc(self) -> dict | None:
        selected = self.lib_disc_tree.selection()
        if not selected or self._browser_library is None:
            return None
        slot = int(selected[0])
        return next((d for d in self._browser_library["discs"] if d["slot"] == slot), None)

    def _browser_show_tracks(self):
        disc = self._browser_selected_disc()
        query = self.lib_search_var.get()
        same_disc = disc is not None and disc["slot"] == self._browser_tracks_slot
        keep = self.lib_track_tree.selection() if same_disc else ()
        self.lib_track_tree.delete(*self.lib_track_tree.get_children())
        self._browser_tracks_slot = None if disc is None else disc["slot"]
        if disc is not None:
            matches = library_browser.matching_tracks(disc, query)
            for n, title in library_browser.track_rows(disc):
                self.lib_track_tree.insert("", "end", iid=str(n), values=(n, title),
                                           tags=("match",) if n in matches else ())
            if keep and self.lib_track_tree.exists(keep[0]):
                self.lib_track_tree.selection_set(keep[0])
        self._update_browser_buttons()

    def _update_browser_buttons(self):
        has_disc = self._browser_selected_disc() is not None
        for btn in (self.lib_play_btn, self.lib_edit_btn):
            btn.configure(state="normal" if has_disc else "disabled")
        # Rescan and Userfiles update the saved scan, so not while a backup
        # file is shown.
        can_rescan = (has_disc and self._browser_file is None and self._browser_raw is not None
                      and not self._library_running)
        for btn in (self.lib_rescan_btn, self.lib_userfile_btn):
            btn.configure(state="normal" if can_rescan else "disabled")
        self.lib_last_scan_btn.configure(
            state="normal" if self._browser_file and self._browser_raw is not None else "disabled")

    def _browser_target(self, use_track: bool | None = None) -> tuple[int, int] | None:
        """(slot, track) to play: the selected disc, at the selected track
        (use_track None/True) or track 1 (use_track False)."""
        disc = self._browser_selected_disc()
        if disc is None:
            return None
        track = 1
        selected = self.lib_track_tree.selection()
        if use_track is not False and selected:
            track = int(selected[0])
        return disc["slot"], track

    def _browser_double_click(self, event, use_track: bool):
        tree = self.lib_track_tree if use_track else self.lib_disc_tree
        if tree.identify_region(event.x, event.y) == "cell":
            self._browser_play(use_track)

    def _browser_play(self, use_track: bool | None = None):
        target = self._browser_target(use_track)
        if target is not None:
            self._send_change_disc(*target)

    def _browser_open_in_disc_data(self):
        """The Disc Data tab follows the loaded disc (and its TOC, which is
        only readable for the loaded disc, CONFIRMED), so load it first."""
        target = self._browser_target()
        if target is None or self._require_link() is None:
            return
        self._send_change_disc(*target)
        self.notebook.select(self.disc_data_tab)

    def _start_library_scan(self):
        link = self._library_can_start()
        if link is None:
            return
        self._library_begin("Starting scan...", self.lib_progress_label)
        self._log(f"Library: scanning all {self.DISC_MAP_SLOT_COUNT} slots.")
        threading.Thread(target=self._library_scan_worker, args=(link,), daemon=True).start()

    def _library_scan_worker(self, link: PCLinkConnection):
        result = self._read_all_slots_sync(link)
        if result is None:
            self._library_finish("Scan stopped -- the library wasn't changed.")
            return
        discs, unreadable = result
        self._library_progress("Reading userfile names...")
        names = self._read_userfile_names_sync(link)
        library = library_backup.build_library(
            discs, names, None, APP_VERSION, datetime.datetime.now().isoformat(timespec="seconds"))
        self.ui_queue.put(lambda: self._set_browser_scan(library))
        if unreadable:
            self._log(f"Library: {len(unreadable)} slot(s) couldn't be read: {unreadable}")
        unknown = [d["slot"] for d in discs if d["track_count"] is None]
        if unknown:
            self._log(f"Library: {len(unknown)} disc(s) haven't been played since the changer "
                      f"was switched on, so their track count isn't known: {unknown}")
        self._library_finish(f"Scan done: {len(discs)} disc(s)"
                             + (f", {len(unreadable)} slot(s) unreadable." if unreadable else "."))

    def _browser_rescan_disc(self):
        disc = self._browser_selected_disc()
        if disc is None or self._browser_raw is None or self._browser_file:
            return
        link = self._library_can_start()
        if link is None:
            return
        slot = disc["slot"]
        self._library_begin(f"Rescanning slot {slot}...", self.lib_progress_label)
        threading.Thread(target=self._library_rescan_worker, args=(link, slot), daemon=True).start()

    def _library_rescan_worker(self, link: PCLinkConnection, slot: int):
        state = self._read_slot_state_sync(link, slot)
        if state["track_count"] is None:
            self._library_finish(f"Rescan: couldn't read slot {slot}; the library wasn't changed.")
            return
        unread = [f for f in library_browser.RESCAN_FIELDS if state[f] is None]
        self._browser_update_slot(slot, state)
        if not state["track_count"]:
            self._library_finish(f"Rescan: slot {slot} is empty now; removed from the library.")
        elif unread:
            self._library_finish(f"Rescan: slot {slot} updated, but its {', '.join(unread)} "
                                 f"couldn't be read, so the saved values were kept.")
        else:
            self._library_finish(f"Rescan: slot {slot} updated.")

    def _browser_update_slot(self, slot: int, state: dict):
        """Background thread: put a fresh read of one slot into the saved
        scan (removing the disc if the slot is empty now). An unreadable
        slot leaves the scan alone, and so does a field that couldn't be
        read (v1.12.4, see library_browser.keep_unread_fields)."""
        if state["track_count"] is None:
            return
        record = library_backup.disc_record(slot, **state) if state["track_count"] else None

        def done():
            if self._browser_raw is not None:
                new = record
                if new is not None:
                    new, _kept = library_browser.keep_unread_fields(self._browser_raw, new)
                self._set_browser_scan(library_browser.replace_disc(self._browser_raw, new, slot))
        self.ui_queue.put(done)

    def _browser_fill_userfile_menu(self):
        """Tick the userfiles the selected disc is in, as of the scan."""
        menu = self.lib_userfile_menu
        menu.delete(0, "end")
        disc = self._browser_selected_disc()
        if disc is None:
            return
        mask = disc.get("userfiles") or 0
        names = self._browser_library["userfile_names"]
        for n, var in enumerate(self._browser_userfile_vars, start=1):
            var.set(bool(mask & (1 << (n - 1))))
            menu.add_checkbutton(label=library_browser.userfile_label(n, names), variable=var,
                                 command=lambda n=n, v=var: self._browser_set_userfile(n, v.get()))

    def _browser_context_menu(self, event):
        row = self.lib_disc_tree.identify_row(event.y)
        if not row:
            return
        self.lib_disc_tree.selection_set(row)
        self._browser_show_tracks()
        if str(self.lib_userfile_btn.cget("state")) == "disabled":
            return
        self._browser_fill_userfile_menu()
        try:
            self.lib_userfile_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.lib_userfile_menu.grab_release()

    def _browser_set_userfile(self, number: int, member: bool):
        """Add the selected disc to userfile #`number` (member True), or
        take it out. The same write as the Userfiles & Program tab (the
        disc name re-sent with the new mask, CONFIRMED v1.8.1), but the
        slot is read fresh first, since the scan may be out of date."""
        disc = self._browser_selected_disc()
        if disc is None or self._browser_raw is None or self._browser_file:
            return
        link = self._library_can_start()
        if link is None:
            return
        slot = disc["slot"]
        scanned_disc = next((d for d in self._browser_raw["discs"] if d["slot"] == slot), {})
        scanned = scanned_disc.get("name")
        userfile = library_browser.userfile_label(number, self._browser_library["userfile_names"])
        which = f"slot {slot} ({scanned})" if scanned else f"slot {slot}"
        question = (f"Add {which} to userfile {userfile}?" if member else
                    f"Take {which} out of userfile {userfile}?")
        if not messagebox.askyesno(
            "Userfiles",
            question + "\n\nThis re-sends the disc's name (track 1's, on a CD-Text disc) with its "
            "new userfiles attached, the same write as the Userfiles & Program tab. The disc's "
            "names, genre and userfiles "
            "are read from the changer first, and nothing is written if the slot doesn't hold "
            "the disc from the scan.",
        ):
            return
        self._library_begin(f"Userfiles: reading slot {slot}...", self.lib_progress_label)
        threading.Thread(target=self._library_userfile_worker,
                         args=(link, slot, scanned, number, member, scanned_disc.get("tracks")),
                         daemon=True).start()

    def _library_userfile_worker(self, link: PCLinkConnection, slot: int, scanned_name: str | None,
                                 number: int, member: bool, scanned_tracks: dict | None = None):
        state = self._read_slot_state_sync(link, slot)
        mask, why_not = library_browser.userfile_change(slot, scanned_name, state, number, member,
                                                        scanned_tracks)
        if mask is not None:
            items, error = plan_userfile_membership_write(
                state["name"], state["genre"], state["cdtext"],
                (state["tracks"] or {}).get(1))
            why_not = error or ""
        if why_not:
            self._browser_update_slot(slot, state)
            self._library_finish(f"Userfiles: slot {slot} not changed.", why_not)
            return

        old = state["userfiles"]
        self._log(f"Library: writing slot {slot} userfiles=0x{mask:02X} (was 0x{old:02X})...")
        if not self._send_text_write(link, slot, items[0], mask, "Library"):
            self._library_finish(f"Userfiles: the write to slot {slot} failed.",
                                 f"The write to slot {slot} failed; see the log.")
            return
        after = self._read_slot_state_sync(link, slot)
        self._browser_update_slot(slot, after)
        done = (f"Userfiles: slot {slot} added to #{number}." if member else
                f"Userfiles: slot {slot} taken out of #{number}.")
        if after["userfiles"] == mask:
            self._library_finish(done)
            return
        seen = "nothing" if after["userfiles"] is None else f"0x{after['userfiles']:02X}"
        self._library_finish(
            f"Userfiles: slot {slot} written, but the re-read didn't show it.",
            f"The write to slot {slot} went through, but reading it back gave userfiles "
            f"{seen} instead of 0x{mask:02X}. See the log.")

    def _reset_disc_map(self):
        """Changer-sourced state, so cleared on disconnect like the other
        caches (_disc_name_cache etc.) -- what was in slot 17 last session
        isn't necessarily still true this session."""
        self._disc_occupancy.clear()
        self._disc_track_count.clear()
        self._disc_map_stop.set()
        self._disc_map_scanning = False
        if hasattr(self, "disc_map_scan_btn"):
            self.disc_map_scan_btn.configure(state="normal")
            self.disc_map_stop_btn.configure(state="disabled")
            self.disc_map_progress_label.configure(text="Not scanned yet.")
        for rect in self._disc_map_cells.values():
            self.disc_map_canvas.itemconfig(rect, fill=self.DISC_MAP_COLOR_UNKNOWN)

    def on_close(self):
        self._disconnect()
        self.destroy()


def main():
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
