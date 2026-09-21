# Changelog

## v1.3.0 -- Write to Changer (names), CONFIRMED against real hardware

**Recovered context:** the write choreography for `Action.WRITE_NAME`
(`pclink_link.py`'s `send_write()`, `pclink_protocol.py`'s
`encode_text_data`) was already implemented and confirmed against real
hardware in earlier work on this project -- a disc name and ten track
names, written this way, were independently read back afterward and
matched exactly. That confirmation never made it into this changelog
before those changes got lost to bad file management; recording it here
from what survived (the code's own docstrings, and `test_write_feature.py`).

**What's new and now also confirmed:** the Disc Data tab's "Write to
Changer" button, which had regressed to a placeholder
(`_write_to_changer_placeholder`, logging "not implemented yet") despite
that confirmed backend sitting right there unused, is wired up again
(`_write_to_changer` / `_write_to_changer_worker` in `pclink_app.py`) --
for every non-empty entry in column 3 ("Custom"), it sends a
`WRITE_NAME` request for that row (disc name for row 0,
`InfoType.DISC_NAMES`; track name for rows 1..N, `InfoType.TRACK_NAMES`)
via `send_write()`, then re-reads the disc name/track names from the
changer afterward so column 1 reflects what's actually there rather than
what the app assumes it wrote. Confirms with a dialog before sending
(irreversible on the changer) and logs a per-item result (written / NAK'd
/ unconfirmed / timed out).

**Confirmed against a real CD-425M** (slot 3, disc name + all 10 track
names, in two full passes -- first overwriting with throwaway test
strings, then with real gnudb-sourced metadata including a disc name
containing `/`): all 22 writes ACK'd, all 22 read back afterward with an
exact text match, and every frame's checksum/length independently
re-verified against `pclink_protocol.py`'s own `compute_checksum`/
`encode_frame`/`decode_text_data`. Transaction shape matched what
`send_write()` already assumed: the changer closes transaction 1 with
its own `EOT` right after `ReadyForData`, and the PC closes transaction
2 itself once the payload's ACK'd (no reply data expected there). No
quirks surfaced this round.

Only `Action.WRITE_NAME` (disc/track names) is wired up and confirmed;
`SET_DISC_GENRE`/`WRITE_PROGRAM`/`SET_USERFILES` share the same
`send_write()` plumbing but still have no UI and remain untested against
real hardware beyond their own round-trip encoder tests.

## v1.2.0 -- Disc Map

Confirmed working against real hardware, including a full 200-slot scan:

- New "Disc Map" tab: a 200-cell grid, one per slot, colored by whether
  the changer reports a disc there or not. Click a cell to query just
  that slot, or "Scan All 200 Slots" to query every slot in sequence
  (with a Stop button and live progress). Built on the same `DiscInfo`
  query the existing "Get Disc Info" button already used -- a slot counts
  as occupied when `track_count > 0`, based on two of the user's own log
  traces (one occupied slot, one empty slot) before this was extended to
  a full scan and confirmed end to end.

### Real-hardware behavior worth knowing about (not a bug, not fixed --
just something the app can't do anything about over serial)

- **The changer's own memory of slot contents can go stale after a power
  outage**, making `DiscInfo` (and so the Disc Map) report wrong data.
  The fix is "ALL DATA READ", run from the changer's own front-panel/
  remote menu -- there's no equivalent command over the serial protocol,
  so this app can't trigger or detect it. The Disc Map tab shows a
  standing on-screen note about this. A software fallback for users
  without a remote (stepping through all 200 slots via `ChangeDisc` to
  force the same recataloging) was proposed but deliberately deferred --
  see README.md's "Not yet implemented".

## v1.1.0 -- TOC / DiscID / Disc Data editor scaffold

Built on top of the v1.0.0 read/control milestone. Confirmed working
against real hardware:

- Table of Contents panel for the current disc: per-track start time and
  length, total disc length, auto-fetched on disc change
- DiscID calculation (classic CDDB1/freedb algorithm, gnudb.org-compatible),
  verified against real TOC data end to end
- Correct handling of the changer's "still physically changing discs"
  window -- TOC requests made too early come back empty, confirmed on
  hardware; now retried automatically once the state settles instead of
  requiring a manual re-check
- Disc Data tab: a row-aligned, three-column (Changer / gnudb.org / Custom)
  editor for the current disc's Disc Name + Track Names. Interface only for
  now -- the gnudb.org query and write-to-changer pieces are the next
  milestone. Custom column entries are preserved per-slot across disc
  navigation and data refreshes.

### Fixes along the way (newest first)

- **`toc_time`'s minutes/seconds/frames are BCD-encoded, not plain
  binary.** Confirmed on real hardware: the raw byte `0x49` means 49
  (seconds), not 73. Decoding as plain integers produced several
  impossible values (seconds over 59); BCD decoding produced valid,
  monotonically increasing track start times for every entry. This also
  happened to confirm track 1 genuinely starts at the standard `00:02`
  Red Book lead-in, which the DiscID algorithm depends on.
- **`DiscTOC` isn't available while the changer is still switching
  discs.** A request made while `StateEvent` reports `Changing` comes back
  completely empty. There's a real timing wrinkle: the `InfoEvent`
  announcing a new slot arrives *before* the `Changing` `StateEvent` does,
  so a naive "check the state before fetching" approach doesn't reliably
  work. Fixed by also retrying automatically whenever the state
  transitions away from `Changing`, rather than only trying once at
  slot-change time.

## v1.0.0 -- Read/Control milestone

Confirmed working against real Kenwood CD-425M hardware:

- Handshake (`"I'm PC"` / `"I'm CD-425M"`)
- Transport controls: Play/Pause, Stop, Previous/Next, Fast Forward/Backward
  (hold-to-repeat), Random, Repeat -- with correct "Finish Repeatable"
  close-out semantics for the hold-style buttons
- Disc/track navigation (`ChangeDisc`)
- Live status display (state, disc slot, track, door) driven entirely by
  the changer's own spontaneous events
- Auto-populating Disc Name / Track Name in the status panel, kept in sync
  as the track or disc changes
- Manual lookups (Get Disc Info / Disc TOC / Track Names / Disc Name)

Everything above went through real hardware testing and multiple rounds of
fixes based on actual protocol traces. The rest of this changelog is that
history, newest first -- useful if a future change needs to understand *why*
the code does something that looks unusual.

### Fixes along the way

- **Track-name/number offset reverted.** A "fix" that converted `TextData`'s
  `index` field with `+1` (on the theory it was 0-based while `InfoEvent`'s
  `track` was 1-based) was itself wrong -- confirmed on hardware it made
  every track name off by one in the other direction. Reverted: `index` is
  used directly, no conversion.
- **`Change Disc` was silently breaking its own auto-fetch.** Optimistically
  setting the tracked "current slot" before the real `InfoEvent` arrived
  meant the "did the slot change?" check saw no change when the event
  showed up, so the new disc's name/track names never got fetched. Fixed:
  only a real `InfoEvent`/`DiscEvent` sets the tracked slot; manual actions
  only bootstrap it once, when nothing is known yet.
- **A manual lookup of an unrelated disc was hijacking the status panel.**
  An earlier version let any Get button's slot override the tracked
  "current slot" -- so checking a disc that wasn't playing made the status
  panel lie about what was actually loaded. Fixed: passive lookups only
  ever bootstrap the tracked slot when it's still unknown.
- **`DataAccess` replies can arrive via a fresh `ENQ`, not just inline.**
  The reply-draining logic only recognized a bare `STX` (how `Handshake`'s
  reply arrives). A `DataAccess` reply instead starting its own
  `ENQ`-initiated transaction was being read and silently discarded without
  ever being ACK'd -- which was the actual cause of Disc Name sometimes
  never populating. Fixed: the drain logic now handles both shapes.
- **Outgoing sends were needlessly slow.** `DoAction`/`ChangeDisc`/
  `ChangeMode` were waiting up to 0.4s "just in case" a reply showed up,
  even though none of them ever do -- confirmed on hardware. That wait sat
  directly in front of time-sensitive sends like "Finish Repeatable" after
  Next/Previous. Cut to near-zero for those commands, and reduced the
  per-byte read-poll floor from 0.25s to 0.03s so a short deadline is
  actually short in practice.
- **A queued send could be starved by a burst of incoming events.** The IO
  loop only checked for a pending outgoing send when the incoming side went
  fully quiet -- so e.g. "Finish Repeatable" queued right after a quick
  Next-Track click could sit behind several changer-initiated events in a
  row (the changer auto-advances tracks until it gets `Finish`). Fixed: the
  loop checks for a pending send immediately after every incoming
  transaction, not just on silence.
- **We were never sending the closing `EOT` ourselves.** Confirmed on real
  hardware: for a command with no reply data, the changer just re-sends its
  `ACK` roughly every 2s waiting for our `EOT` -- it doesn't close the
  transaction on its own. Fixed: after draining any reply data, we send our
  own closing `EOT`.
- **Previous/Next need the same "Finish Repeatable" close-out as FF/FB**,
  even though the source docs only mark FF/FB as "(repeatable)". Confirmed
  on hardware and implemented as press-and-hold buttons.
- **A changer's reply can arrive as a bare `STX` with no `ENQ`,** riding
  within the same transaction as our request (confirmed via the Handshake
  reply) -- rather than the fresh `ENQ`-initiated transaction originally
  assumed.
- **The originally-assumed EOT/ACK round-trip on every single frame** turned
  out not to match real hardware behavior at all; the actual framing rules
  were derived empirically over several rounds against a real changer, not
  purely from the source documentation (which describes the general shape
  correctly but leaves out these specifics).

### Known limitations (see README.md for full details)

- No documented "give me current status" command -- the status panel can't
  populate until something changes or you manually look it up.
- `DiscTOC` pagination not implemented (only page 0 requested).
- No error/fault code handling beyond `State` and `DoorEvent`.
- Writing (`Action = SET_DISC_GENRE / WRITE_PROGRAM / SET_USERFILES /
  WRITE_NAME`) is defined in the protocol layer but not wired up in the UI
  -- this is the natural next milestone.
