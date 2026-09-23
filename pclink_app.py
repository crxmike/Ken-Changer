#!/usr/bin/env python3
"""
pclink_app.py
=============
A desktop app (Windows/macOS/Linux) for controlling a Kenwood CD-425M (also
works with the CD-4700M / CD-4260M, which share the same command set) over
its PC-Link serial port, via a USB-to-RS232 null-modem adapter.

Run:
    pip install pyserial
    python pclink_app.py

See README.md for wiring notes and a summary of what is/isn't confirmed
against real hardware yet.
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None

import pclink_protocol as proto
from pclink_link import (
    PCLinkConnection, PCLinkError, PCLinkTimeout, PCLinkNak, PCLinkWriteUnconfirmed,
)
from pclink_protocol import Frame
import gnudb_client


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
APP_VERSION = "1.6.7"


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

    def __init__(self):
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

        # Disc Map tab: per-slot occupancy as last reported by DiscInfo
        # (slot -> True if it has a disc, False if empty; a slot with no
        # entry yet hasn't been queried this session). Populated one slot
        # at a time by clicking a cell, or for all 200 slots via "Scan All
        # 200 Slots". Changer-sourced, so cleared on disconnect like the
        # other caches -- see _reset_disc_map / _disconnect.
        self._disc_occupancy: dict[int, bool] = {}
        self._disc_map_cells: dict[int, int] = {}  # slot -> canvas rectangle item id
        self._disc_map_stop = threading.Event()
        self._disc_map_scanning = False

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

        control_tab = ttk.Frame(notebook, padding=4)
        notebook.add(control_tab, text="Control")

        disc_data_tab = ttk.Frame(notebook, padding=4)
        notebook.add(disc_data_tab, text="Disc Data")

        disc_map_tab = ttk.Frame(notebook, padding=4)
        notebook.add(disc_map_tab, text="Disc Map")

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
        dd_scroll.grid(row=5, column=0, sticky="nsew")
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
        if self.link:
            self.link.close()
            self.link = None
        self._current_slot = None
        self._current_track = None
        self._last_state = None
        self._pending_mode = None
        self._disc_name_cache.clear()
        self._track_name_cache.clear()
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
        self.disc_data_summary.configure(text="No current disc known yet.")
        self._reset_disc_map()
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
                self.ui_queue.put(lambda s=slot, o=occupied: self._update_disc_map_cell(s, o))

        elif frame.command == proto.CMD_DISC_TOC:
            self._cache_toc(frame.payload)

        elif frame.command == proto.CMD_DISC_GENRE:
            p = frame.payload
            self._log(f"DiscGenre: slot={p.get('slot')} genre={p.get('genre_name')}")
            self._cache_genre(p)

        elif frame.command in (proto.CMD_TEXT_DATA, proto.CMD_LONG_TEXT_DATA):
            p = frame.payload
            index = p.get("index", p.get("track"))
            text = p.get("text")
            note = "  (placeholder: no title stored)" if proto.is_placeholder_text(text or "") else ""
            self._log(f"Text: slot={p.get('slot')} index={index} -> {text!r}{note}")
            self._cache_name(p)

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
        Get Track Names buttons, which use the slot spinbox)."""
        self._send_bg(
            proto.CMD_DATA_ACCESS,
            proto.encode_data_access(
                proto.Action.RETRIEVE_DATA, proto.DataType.TEXT_DATA, slot=slot,
                info_type=proto.InfoType.DISC_NAMES,
            ),
            f"DataAccess(DiscName, slot={slot}) [auto]",
        )
        self._send_bg(
            proto.CMD_DATA_ACCESS,
            proto.encode_data_access(
                proto.Action.RETRIEVE_DATA, proto.DataType.TEXT_DATA, slot=slot,
                info_type=proto.InfoType.TRACK_NAMES,
            ),
            f"DataAccess(TrackNames, slot={slot}) [auto]",
        )

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

            self.disc_data_summary.configure(
                text=f"Slot {slot} -- {track_count} track(s) known"
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
        except gnudb_client.GnudbError as exc:
            self._log(f"gnudb.org query failed: {exc}")
            return

        if not matches:
            self._log("gnudb.org: no match found for this disc.")
            return

        if len(matches) == 1:
            self._log(f"gnudb.org: found a match -- {matches[0].title}")
            self._gnudb_read_worker(slot, matches[0], hello)
            return

        self._log(f"gnudb.org: found {len(matches)} possible matches -- pick one.")
        self.ui_queue.put(lambda: self._show_gnudb_match_picker(slot, matches, hello))

    def _gnudb_read_worker(self, slot: int, match: "gnudb_client.GnudbMatch", hello: str):
        try:
            disc = gnudb_client.read(match.category, match.discid, hello)
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

    def _show_gnudb_match_picker(self, slot: int, matches: list, hello: str):
        win = tk.Toplevel(self)
        win.title("Select a gnudb.org match")
        win.geometry("520x320")
        ttk.Label(win, text="Multiple possible matches were found -- select one:").pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        listbox = tk.Listbox(win, width=76, height=12)
        for m in matches:
            listbox.insert("end", f"[{m.category}] {m.title}")
        listbox.pack(fill="both", expand=True, padx=8)
        if matches:
            listbox.selection_set(0)

        def on_select():
            selection = listbox.curselection()
            if not selection:
                return
            chosen = matches[selection[0]]
            win.destroy()
            self._log(f"Selected: {chosen.title}")
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

    def _write_to_changer(self):
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
        entries for that history."""
        link = self._require_link()
        if link is None:
            return
        slot = self._disc_data_slot
        if slot is None:
            messagebox.showinfo("Nothing to write", "No current disc loaded yet.")
            return

        items = gather_disc_data_write_items(self._disc_data_rows)
        genre_code = gather_genre_write_item(self.genre_custom_var.get())
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
        )
        if not messagebox.askyesno(
            "Write to Changer",
            f"Write {len(final_items)} value(s) to slot {slot} on the changer?\n\n"
            "This overwrites whatever is currently stored there for each one."
            + genre_warning,
        ):
            return

        self.dd_write_btn.configure(state="disabled")
        self._log(f"Write to Changer: writing {len(final_items)} value(s) to slot {slot}...")
        threading.Thread(
            target=self._write_to_changer_worker, args=(link, slot, final_items), daemon=True
        ).start()

    def _write_to_changer_worker(self, link: PCLinkConnection, slot: int, items: list):
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
        """
        ok = 0
        failed = 0
        wrote_genre = False
        for index, text, info_type, label, genre in items:
            request_data = proto.encode_data_access(
                proto.Action.WRITE_NAME, proto.DataType.TEXT_DATA,
                slot=slot, info_type=info_type,
            )
            follow_up_data = proto.encode_text_data(
                slot=slot, index=index, text=text, info_type=info_type, genre=genre,
            )
            try:
                link.send_write(
                    proto.CMD_DATA_ACCESS, request_data,
                    proto.CMD_TEXT_DATA, follow_up_data,
                )
                suffix = " (genre attached)" if genre else ""
                self._log(f"Write to Changer: {label} -> {text!r} written ok{suffix}.")
                ok += 1
                if genre:
                    wrote_genre = True
            except PCLinkWriteUnconfirmed:
                self._log(
                    f"Write to Changer: {label} -- changer never sent ReadyForData, "
                    "not written."
                )
                failed += 1
            except PCLinkNak:
                self._log(f"Write to Changer: {label} -- changer NAK'd the write.")
                failed += 1
            except PCLinkTimeout:
                self._log(f"Write to Changer: {label} -- timed out.")
                failed += 1
            except PCLinkError as exc:
                self._log(f"Write to Changer: {label} -- error: {exc}")
                failed += 1

        self._log(f"Write to Changer: done -- {ok} written, {failed} failed.")
        if ok:
            # Re-read from the changer so column 1 (and the Genre row)
            # reflect what was actually written, rather than trusting the
            # write locally.
            self._send_bg(
                proto.CMD_DATA_ACCESS,
                proto.encode_data_access(
                    proto.Action.RETRIEVE_DATA, proto.DataType.TEXT_DATA, slot=slot,
                    info_type=proto.InfoType.DISC_NAMES,
                ),
                f"DataAccess(DiscName, slot={slot})",
            )
            self._send_bg(
                proto.CMD_DATA_ACCESS,
                proto.encode_data_access(
                    proto.Action.RETRIEVE_DATA, proto.DataType.TEXT_DATA, slot=slot,
                    info_type=proto.InfoType.TRACK_NAMES,
                ),
                f"DataAccess(TrackNames, slot={slot})",
            )
            if wrote_genre:
                self._send_bg(
                    proto.CMD_DATA_ACCESS,
                    proto.encode_data_access(
                        proto.Action.RETRIEVE_DATA, proto.DataType.DISC_GENRE, slot=slot,
                    ),
                    f"DataAccess(DiscGenre, slot={slot})",
                )
        self.ui_queue.put(lambda: self.dd_write_btn.configure(state="normal"))

    def _copy_column_to_custom(self, source: str):
        var_key = "changer_var" if source == "changer" else "gnudb_var"
        for row in self._disc_data_rows:
            val = row[var_key].get()
            row["custom_var"].set(val if val and val != "-" else "")

        genre_source_var = self.genre_changer_var if source == "changer" else self.genre_gnudb_var
        genre_val = genre_source_var.get()
        # The Custom column's Genre control is a fixed dropdown (only the
        # changer's own enum, per proto.GENRES) -- only copy over a value
        # that's actually one of those names; free-text gnudb genres (e.g.
        # "Alternative") that don't match exactly are left blank rather
        # than silently forced into the nearest-sounding option.
        self.genre_custom_var.set(genre_val if genre_val in proto.GENRE_NAME_TO_CODE else "")

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
        slot = self.slot_var.get()
        self._note_query_slot(slot)
        self._send_bg(
            proto.CMD_DATA_ACCESS,
            proto.encode_data_access(
                proto.Action.RETRIEVE_DATA, proto.DataType.TEXT_DATA, slot=slot,
                info_type=proto.InfoType.TRACK_NAMES,
            ),
            f"DataAccess(TrackNames, slot={slot})",
        )

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

    def _reset_disc_map(self):
        """Changer-sourced state, so cleared on disconnect like the other
        caches (_disc_name_cache etc.) -- what was in slot 17 last session
        isn't necessarily still true this session."""
        self._disc_occupancy.clear()
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
