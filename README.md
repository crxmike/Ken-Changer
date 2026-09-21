# Kenwood PC-Link Control App (CD-425M)

**Status: v1.3.0 -- read/control + TOC/DiscID + Disc Map + writing
disc/track names, all confirmed working against real CD-425M
hardware.** See `CHANGELOG.md` for what that covers and the history of
fixes that got it there. Querying gnudb.org is wired up but its live
round-trip is unconfirmed. Writing genre/program/userfiles
(`SET_DISC_GENRE`/`WRITE_PROGRAM`/`SET_USERFILES`) is defined at the
protocol layer but has no UI yet.

A small desktop app for controlling a Kenwood CD-425M CD changer (also
compatible with the CD-4700M / CD-4260M, which use the same command set)
over its PC-Link serial port.

Built directly from the protocol documented at
`https://juken.sourceforge.net/protocol/` (pages supplied by the user and
transcribed into `pclink_protocol.py`).

## Requirements

- Python 3.9+
- `pyserial`
- `tkinter` (bundled with most Python installs; on some Linux distros install
  separately, e.g. `sudo apt install python3-tk`)
- A **null-modem** USB-to-RS232 serial adapter/cable connected to the
  changer's PC-Link port (TX/RX must be swapped -- a straight-through cable
  will not work).
- Internet access, only if you want to use the Disc Data tab's "Query
  gnudb.org" feature (everything else works fully offline). No extra pip
  package needed for this -- `gnudb_client.py` uses only the standard
  library.

```bash
pip install pyserial
python pclink_app.py
```

## What the app does

- **Connect** to the changer on a chosen serial port (9600 baud, 8 data bits,
  no parity, 2 stop bits -- fixed, per the protocol spec) and perform the
  `Handshake` exchange.
- **Transport controls**: Play/Pause, Stop, Random, Repeat, and four
  press-and-hold buttons -- Fast Forward/Backward (which repeat the action
  every ~300ms while held) and Previous/Next (which send the action once
  on press) -- all of which send "Finish Repeatable" (`0xFFFF`) on release.
  Confirmed against real hardware that Previous/Next need this too, even
  though the source docs' Action Command table only marks FF/FB as
  "(repeatable)".
- **Disc/track selection**: jump to a given disc slot and track.
- **Status display**, updated live from the changer's spontaneous events:
  playback state (Playing/Paused/Stopped/Changing/...), current disc slot,
  track, program number, play mode, repeat on/off, active userfiles, disc
  name, track name, and door open/closed.
- **Table of Contents panel** for the currently-loaded disc: per-track start
  time and length, total disc length, and a computed **DiscID** (the
  classic CDDB1/freedb disc identifier, which gnudb.org's query protocol is
  compatible with) -- auto-fetched whenever the current disc changes, or on
  demand via "Refresh TOC". Unlike disc/track names, TOC data is only ever
  requested for the currently-loaded disc (confirmed against real hardware
  that it isn't available for other slots), so this panel ignores the slot
  spinbox and always tracks what's actually playing.
- **Queries**: on-demand DiscInfo (track count/format) and disc/track name
  text lookups for an arbitrary slot.
- **Disc Data tab**: a row-aligned, three-column view of the current disc's
  data -- Disc Name followed by each Track Name. Column 1 ("From Changer")
  is populated automatically from the same cached data the status panel
  uses. Column 2 ("From gnudb.org") is filled in by querying gnudb.org's
  CDDB-compatible database using the DiscID computed from the current TOC
  -- click "Query gnudb.org" (requires a contact email in the field next to
  it; see "gnudb.org usage" below). Column 3 ("Custom (to write)") is a
  free-text column for values you intend to write back to the changer --
  "Write to Changer" sends every non-empty entry as a `WRITE_NAME` request
  (disc name for row 0, track name for rows 1..N), then re-reads the
  names from the changer so column 1 reflects what's actually there.
  **Confirmed working against real hardware** -- see `CHANGELOG.md`'s
  v1.3.0 entry and "Honest gaps" #11. Column 3's entries are per-slot and
  preserved across disc navigation and data refreshes, so switching discs
  to check something and coming back doesn't lose whatever you were
  typing. "Copy Changer -> Custom" and "Copy gnudb -> Custom" seed
  column 3 from either source as a starting point.
- **Disc Map tab**: a 200-cell grid, one per slot, colored by whether the
  changer reports a disc there or not -- click a cell to query just that
  slot, or "Scan All 200 Slots" to query every slot in sequence (with a
  Stop button, since a full scan takes a while). Built on the same
  DiscInfo query the Control tab's "Get Disc Info" button already uses; a
  slot counts as occupied when DiscInfo's `track_count` is greater than 0.
  Confirmed against real hardware, including a full 200-slot scan.
  Occupancy is changer-sourced, so it's cleared on disconnect like the
  other caches. **Caveat, confirmed by the user**: the changer's own
  memory of what's in each slot can be lost or go stale after a power
  outage, which then makes DiscInfo -- and so the Disc Map -- report
  wrong data. Fixing this requires running "ALL DATA READ" from the
  changer's own front-panel/remote menu; there is no way to trigger or
  detect this over the serial connection, so the Disc Map tab shows a
  standing note about it rather than trying to catch it automatically.
- **Log console** with a "show raw bytes" toggle, so you can see the actual
  ENQ/ACK/STX/EOT byte exchange -- useful both for troubleshooting your
  specific unit and for extending the app later.

## gnudb.org usage

`gnudb_client.py` implements `cddb query` and `cddb read` over gnudb.org's
HTTP CGI protocol (`https://gnudb.org/howtognudb.php`), using only the
Python standard library (`urllib`) -- no extra dependency. A few things
worth knowing:

- **Tries HTTP then falls back to HTTPS.** gnudb.org's documented endpoint
  is plain HTTP on port 80 (`http://gnudb.gnudb.org/~cddb/cddb.cgi`), which
  is what every independent source confirms -- but a timeout there (rather
  than an immediate refusal) is a common symptom of a firewall or ISP that
  deprioritizes/blocks port 80 specifically. gnudb's own "sites" listing
  also mentions the same host on port 443, so `_get()` tries HTTP first
  and automatically retries over HTTPS if that fails. If BOTH fail, the
  error message lists every URL actually attempted, so you can paste one
  into a browser directly to tell a network/firewall problem apart from a
  code problem. On Windows specifically, check whether Windows Defender
  Firewall (or antivirus) prompted to block outbound access the first time
  this app tried to reach the network, and allow it if so.
- **A contact email is required.** gnudb.org's stated policy requires a
  real, contactable email address in every request, not a generic
  placeholder -- enter yours in the Disc Data tab before querying. It's
  kept in memory only; this app never writes it to disk.
- **The app identifies itself as "KenwoodPCLinkController"** plus its
  version, per the policy's request to avoid generic client names. If you
  intend to use this beyond occasional personal testing, gnudb.org asks
  that you email `info@gnudb.org` with your program name and a contact
  address so they know who's accessing the service (see the "GNUDB Access
  Policy" section of the page above).
- **Query result parsing is tolerant of a documentation inconsistency**:
  gnudb.org's own protocol page lists status code `200` for a single exact
  match and `211` for a list of inexact matches in one place, but its own
  worked example uses `210` for a list of exact matches. `gnudb_client.py`
  handles `200` as a single match and treats both `210` and `211` as a
  multi-line list -- verified against the docs' own worked examples
  (Pink Floyd / Bob Dylan / Katie Melua) rather than just the written
  spec, since the spec itself isn't fully self-consistent here.
- **Track numbering**: gnudb's `TTITLE0` is track 1 (0-based index, per
  the documented xmcd standard) -- converted to this app's 1-based track
  numbers when populating column 2. This is unrelated to, and shouldn't be
  confused with, the Kenwood changer's own `index` field in its `TextData`
  replies, which real hardware testing showed already matches the 1-based
  track number directly with no offset (see the TOC/track-name notes
  below) -- two different protocols with two different, coincidentally
  opposite conventions.
- **Multiple matches**: if gnudb.org returns more than one candidate for a
  DiscID (this can genuinely happen -- DiscIDs aren't perfectly unique), a
  small picker window lists them for you to choose from before the full
  entry is fetched.

## Not yet implemented

- **Writing genre/program/userfiles to the changer.** `Action.WRITE_NAME`
  (disc/track names, column 3 of the Disc Data tab) is wired up and
  confirmed -- see "Disc Data tab" above and `CHANGELOG.md`'s v1.3.0
  entry. `Action.SET_DISC_GENRE` / `WRITE_PROGRAM` / `SET_USERFILES`
  share the same `send_write()` plumbing in `pclink_link.py` but have no
  UI yet, and their encoders are untested against real hardware (only
  proven self-consistent via round-trip tests in `test_write_feature.py`).
- **A software fallback for "ALL DATA READ."** That command has to be run
  from the changer's own front-panel/remote menu (see the Disc Map tab's
  caveat above) -- there's no serial equivalent. For a user without a
  remote, the fallback idea (not yet built, deliberately deferred for now)
  would be to physically step through all 200 slots one at a time via
  `ChangeDisc`, pausing for the changer to read each one, as a workaround
  that forces the same recataloging without needing the menu.

## Files

- `pclink_protocol.py` -- pure protocol logic: frame/checksum encoding,
  command byte constants, all documented enumerations (Action, State, Mode,
  Genre, etc.), and payload encoders/decoders for every command page in the
  source documentation. No serial I/O; fully unit-testable on its own.
- `pclink_link.py` -- the serial transport. Owns the port and a single I/O
  thread that implements the ENQ/ACK/EOT flow-control handshake in both
  directions (since the changer can initiate a transaction at any time to
  push an event, not just reply to requests).
- `pclink_app.py` -- the Tkinter GUI tying the above together.
- `gnudb_client.py` -- minimal HTTP client for gnudb.org's CDDB-compatible
  query/read protocol (stdlib `urllib` only, no serial or Tkinter
  dependency; independently testable, and tested against gnudb.org's own
  documented example responses).

## Protocol summary (for reference)

- **Link**: 9600-8-N-2, null-modem cable.
- **Flow control**: `ENQ (0x05)` -> `ACK (0x06)` -> frame -> `ACK`/`NAK` ->
  `EOT (0x04)` -> `ACK`.
- **Frame**: `STX (0x02)` + command byte + 2-byte length (LSB first) + data +
  1-byte checksum.
- **Checksum**: two's-complement negation of the sum of (command byte +
  length bytes + data bytes).
- Multi-byte values are little-endian (LSB first) throughout.

### Command bytes implemented

| Byte | Name | Direction | Notes |
|---|---|---|---|
| 0x00 | Handshake | both | `"I'm PC"` / `"I'm CD-425M"` |
| 0x03 | DataAccess | PC -> changer | request/set info; see below |
| 0x04 | DiscInfo | changer -> PC | track count, format |
| 0x06 | DiscTOC | changer -> PC | per-track start times |
| 0x07 | DiscUserfiles | changer -> PC | userfile bitmask for a disc |
| 0x08 | DiscGenre | changer -> PC | genre for a disc |
| 0x09 | ReadyForData | changer -> PC | changer requesting data (write path) |
| 0x0A | DoAction | PC -> changer | transport control commands |
| 0x0B | ChangeDisc | PC -> changer | jump to disc/track |
| 0x0C | ChangeMode | PC -> changer | switch play mode |
| 0x0D | DiscListing | both | list of slot/track pairs (e.g. programs) |
| 0x12 | InfoEvent | changer -> PC | current slot/track/mode/etc. |
| 0x13 | StateEvent | changer -> PC | Playing/Paused/Stopped/... |
| 0x14 | DiscEvent | changer -> PC | current disc slot |
| 0x15 | DoorEvent | changer -> PC | door open/closed |
| 0xFD | LongTextData | changer -> PC | track/disc name text |
| 0xFE | TextData | changer -> PC | track/disc name text |

## Honest gaps / things to verify against your real hardware

The source documentation is itself a community reverse-engineering effort
and says so in places ("If you have a changer where no identifier is
listed..."). A few spots where this app makes a reasonable inference rather
than transcribing an explicit statement from the docs -- worth confirming
once you're actually talking to the unit (the raw-byte log will show you
exactly what's happening):

1. **Reply framing -- two different shapes.** Confirmed against real
   CD-425M hardware that replies arrive in TWO different ways depending on
   the command: `Handshake`'s identifier rides inline as a bare `STX` +
   payload *within the same transaction* as the request, with no fresh
   `ENQ`; but a `DataAccess` reply (e.g. `DiscName`/`TrackNames` text) can
   instead arrive as the changer starting its OWN fresh `ENQ`-initiated
   transaction, even while we're still "in session" from our own request.
   The app now handles both: `_drain_replies` (run right after our own
   frame is `ACK`'d) recognizes either a bare `STX` or a fresh `ENQ` and
   processes either correctly. (An earlier version only handled the bare
   `STX` case and silently discarded an incoming `ENQ` without acking it --
   which meant the changer's reply transaction died right there, waiting
   for an `ACK` that would never come. This was the cause of the "Disc
   Name only ever shows '-'" bug -- fixed.)
2. **Who sends the closing EOT.** Confirmed against real hardware: for the
   request/reply exchanges this app makes, *we* (the initiator) are
   responsible for sending the closing `EOT`, not the changer. For a
   command with no reply data (e.g. `DoAction`), the changer just sits
   there re-sending its `ACK` roughly every 2 seconds, waiting for our
   `EOT` -- it never closes the transaction on its own. So after draining
   any reply frame(s), the app sends its own `EOT` and waits for the
   changer's `ACK` of it. (This is the opposite of an earlier assumption in
   this codebase that the changer would close things out -- fixed, and
   verified against a real DoAction exchange that was otherwise stuck
   retrying forever.)

   The time the app waits, after a request frame is `ACK`'d, before giving
   up on a reply and sending its own closing `EOT`, is tunable per command
   via `REPLY_WINDOW_BY_COMMAND` in `pclink_link.py` -- currently 3s for
   `Handshake`/`DataAccess` (which do return data) and 0.4s for everything
   else (so transport buttons like Play/Pause stay responsive). If a
   `DataAccess` query for a large TOC needs more time on your unit, or a
   command you expected to be reply-less unexpectedly returns data, adjust
   the relevant entry there.
3. **Outgoing sends could get starved by a burst of incoming events.**
   Confirmed against real hardware with Next/Previous: after sending
   `Next Track`, the changer keeps auto-advancing and streaming a fresh
   `InfoEvent` for every track until it receives `Finish Repeatable`
   (`0xFFFF`). The IO loop originally only checked for a pending outgoing
   send when the incoming side went fully quiet -- so a `Finish` queued
   right after a quick click could sit starved behind several back-to-back
   changer-initiated events, letting it skip multiple tracks instead of
   just one. Fixed: the loop now checks for a pending outgoing send
   immediately after every incoming transaction, not just on incoming
   silence, so `Finish` goes out as soon as the bus is free rather than
   waiting for a gap that the changer wasn't going to give it.
3. **Reply routing for `DataAccess` requests.** The docs describe the
   `DataAccess` request payload and separately describe reply payload shapes
   (`DiscInfo`, `DiscTOC`, `TextData`, etc.), but don't spell out that a
   `DataAccess` request produces exactly one reply frame of the matching
   command byte. That's the natural reading (and is what this app assumes),
   but if your unit sends something different, the log will tell you.
4. **`DiscTOC` isn't available while the changer is physically switching
   discs.** Confirmed on real hardware: a request made while `StateEvent`
   reports `Changing` comes back completely empty (frame `ACK`'d, then an
   immediate `EOT` with no `DiscTOC` reply at all) -- unlike `DiscName`/
   `TrackNames`, which are stored/cataloged data and work fine even
   mid-change. There's a real timing wrinkle too: the `InfoEvent`
   announcing a new slot arrives *before* the `Changing` `StateEvent` does,
   so the slot-change auto-fetch can't reliably know to wait. Handled by
   also retrying automatically whenever the state transitions away from
   `Changing` if a complete TOC (with a lead-out) isn't in hand yet -- see
   `_on_disc_settled`. The TOC panel shows "is changing" while this is in
   progress, and the manual Refresh TOC button declines with an explanation
   rather than sending a request that's known to come back empty.
5. **`DiscTOC` pagination -- handled opportunistically, not explicitly
   requested.** The doc notes "data page (47? TOC values per page)" with a
   question mark in the source itself. There's no `page_num` field in the
   *request* payload (`cd_dataaccess.html`), only in each reply -- matching
   the pattern already confirmed for `TrackNames`, where a single request
   produces multiple reply frames (one per track) within the same
   transaction, not one reply per request. So a single `DataAccess(DiscTOC)`
   request's reply-draining (which already loops to catch multiple `STX`-
   framed frames) will pick up multiple `DiscTOC` pages automatically if the
   changer sends them, and `_cache_toc` merges them by track number. Not yet
   confirmed against a disc with enough tracks to actually span more than
   one page. (One real data point: a confirmed single-page reply had
   `page_num = 1`, not `0` -- page numbering may be 1-based. Doesn't affect
   the merge logic either way, since it only uses `first_track`/`last_track`
   to place entries, not `page_num` itself.)
5. **`toc_time`'s minutes/seconds/frames are BCD-encoded, not plain
   binary.** Confirmed against real hardware: the raw byte `0x49` means 49
   (seconds), not 73. `cd_types.html` doesn't say this explicitly (just
   "byte minutes" / "byte seconds"), but BCD-encoded MSF timecodes are the
   Red Book CD standard, and decoding these bytes as plain integers
   produced impossible values (several `seconds` fields over 59) while BCD
   decoding produced valid, monotonically increasing track start times for
   every entry in a real TOC. Fixed in `_bcd_to_int`/`decode_disc_toc`.
6. **DiscID calculation -- confirmed against real hardware.**
   `calculate_cddb_discid` implements the standard, public CDDB1/freedb
   algorithm (which gnudb.org's query protocol is compatible with). Both
   assumptions it depends on are now confirmed rather than just plausible:
   the formula itself (cross-checked by re-deriving it independently), and
   that `DiscTOC`'s timecodes are absolute Red Book MSF including the
   standard 150-frame/2-second lead-in -- a real disc's track 1 was
   observed starting at exactly `00:02`. Still not cross-checked against an
   actual gnudb.org/freedb lookup for a disc with a known-correct ID (no
   network access in the environment this was built in) -- if you look one
   up and it doesn't match, that's the next thing to investigate.
7. **Text data retrieval for multiple tracks.** `TextData`/`LongTextData`
   appear to be single-item replies (one track or one disc name per frame).
   Requesting "track names" for a whole disc may return one frame per track
   in sequence, or may require iterating `index` yourself -- not explicit in
   the docs. The app currently issues one request and logs whatever comes
   back; extend `_get_track_names` if your unit needs per-track requests.
6. **`DiscEvent`'s slot field is a single byte**, while `slot` is a 2-byte
   `short` everywhere else in the documented payloads. That's exactly what
   the source page says, so it's transcribed as-is rather than "fixed" --
   but it's worth double-checking against real traffic.
7. **No "give me current status" command.** The changer only announces
   `InfoEvent`/`StateEvent`/`DiscEvent`/`DoorEvent` on state *changes* --
   nothing in the docs describes a way to ask "what's currently loaded"
   after connecting. So on a fresh connect, the Disc Name/Track Name/Disc
   (slot)/Track status rows stay blank until something actually changes on
   the changer (you press a transport button, or use it directly) -- a
   manual Get ... lookup only fills the status panel in as a *bootstrap
   guess* when we have no real information yet, and a real event always
   takes priority over it. (Two earlier versions of this got it wrong in
   opposite directions: one let ANY manual lookup override the tracked
   "current slot" -- confirmed on real hardware this made the status panel
   show a disc that wasn't actually playing; the fix after that had
   `ChangeDisc` optimistically set the tracked slot itself, which then
   silently broke the auto-fetch for the new disc entirely, since the
   confirming `InfoEvent`'s "did the slot change?" check found nothing had
   changed as far as it could tell. Fixed for real this time: no command
   sets the tracked slot preemptively -- only a real `InfoEvent`/`DiscEvent`
   does, and the passive Get ... / `ChangeDisc` buttons only ever bootstrap
   it once, when nothing else is known yet.)
8. **Track name/number offset -- reverted.** An earlier version converted
   `TextData`'s `index` field with a `+1` on the theory that it was a
   0-based list position while `InfoEvent`'s `track` field was 1-based.
   Confirmed on real hardware this was wrong and made every track name off
   by one in the other direction. `index` (and `LongTextData`'s `track`
   field) are now used directly with no conversion, matching `InfoEvent`'s
   `track` with no offset.
9. **A send can fail from a one-off bus collision.** Confirmed on real
   hardware: our outgoing `ENQ` can occasionally collide with the changer
   trying to start its own transaction at nearly the same moment -- we read
   back the changer's `ENQ` where we expected our own `ACK`, and the send
   fails outright with a timeout. This is a transient race, not a real
   communication failure (the very next send typically succeeds
   immediately). `_send_bg` now retries up to twice on a timeout before
   giving up; without this, a collision on an auto-fetch (nobody watching
   to manually retry) meant that data just silently never arrived -- this
   was the actual cause of Disc Name never populating automatically in one
   observed session.
10. **No documented error/fault codes.** Beyond the `State` enum and
   `DoorEvent`, the source material doesn't document any explicit
   error-code payload, so the app doesn't surface one.
11. **Writing disc/track names (`Action.WRITE_NAME`) -- confirmed
   against real hardware, including the UI.** `send_write()`'s
   two-transaction choreography (see `pclink_link.py`'s module
   docstring) and `encode_text_data` were confirmed earlier: a disc name
   and ten track names, written this way, were independently read back
   afterward and matched exactly. That confirmation existed before but
   got lost along with the UI wiring due to bad file management --
   recovered and re-recorded in `CHANGELOG.md`'s v1.3.0 entry, along
   with a fresh confirmation of the Disc Data tab's "Write to Changer"
   button itself (`gather_disc_data_write_items` / `_write_to_changer_worker`
   in `pclink_app.py`): 22 writes (disc name + 10 track names, twice --
   once with throwaway test strings, once with real gnudb-sourced
   metadata) against slot 3 on a real CD-425M, all ACK'd, all read back
   afterward with an exact match, every frame's checksum/length
   independently re-verified against `pclink_protocol.py`. No quirks
   surfaced. `SET_DISC_GENRE` / `WRITE_PROGRAM` / `SET_USERFILES` share
   the same `send_write()` plumbing but have no UI and remain untested
   against real hardware beyond their own round-trip encoder tests.
12. **Disc Map -- confirmed against real hardware, including a full
   200-slot scan.** The "occupied = track_count > 0" rule was already
   backed by two of the user's own log traces (one occupied slot, one
   empty slot); the user has since confirmed the whole tab -- individual
   clicks and "Scan All 200 Slots" -- against a real CD-425M. One related
   thing surfaced by the user during that confirmation, not a gap in this
   app's logic but a real hardware behavior to know about: the changer's
   own memory of slot contents can be lost or go stale after a power
   outage, making DiscInfo (and so the Disc Map) report wrong data until
   "ALL DATA READ" is run from the changer's own front-panel/remote menu
   -- no serial equivalent exists, so the app can't trigger or detect this
   itself; see the Disc Map tab's on-screen caveat. Separately, still not
   confirmed: whether a disc that's physically present but blank or
   unreadable would also report `track_count=0` and so look empty on the
   map -- `cd_discinfo.html` doesn't document the `unknown` byte's
   meaning, but in the user's two examples it tracked occupancy too (`1`
   for the occupied slot, `0` for the empty one) and might be the more
   reliable field for that specific case if it ever comes up.

If your real unit's behavior differs from any of the above, turn on "show
raw bytes" in the log and it'll show you exactly what's being exchanged.
