# Ken Changer (Kenwood CD-425M Control App)

**Status: v1.12.2 -- read/control + TOC/DiscID + Disc Map + writing
disc/track names + reading/writing genre + reading/writing userfiles
and programs + gnudb.org lookup with cover art, all confirmed working
against real CD-425M hardware and the live gnudb.org server.** See `CHANGELOG.md` for what that covers and the
history of fixes that got it there. Genre writing took four different
approaches to get right -- it goes out folded into a `WRITE_NAME` write
rather than the standalone `Action.SET_DISC_GENRE` action the protocol
docs' own enum suggests, since three real-hardware attempts at that
failed before this one worked -- **and genre turned out to be a
disc-level value that EVERY `WRITE_NAME` write sets, so v1.5.1 fixed
(and CONFIRMED the fix for) a real bug where writing an unrelated track
name was silently resetting genre back to "Unassigned."** See "Honest
gaps" #14 and `CHANGELOG.md`'s v1.4.1-v1.5.1 entries for the full history
if you're extending this. v1.8.0 adds writing userfiles and programs
(see "Honest gaps" #17). v1.6.x adds a play
mode selector (`ChangeMode`), **partly confirmed on real hardware**.
v1.10.0 adds a Backup tab (export the library to a file, and restore
it), **confirmed on real hardware in v1.10.2** (see "Honest gaps" #19).
v1.11.0 adds a Library tab (browse, search and play every disc),
**confirmed on real hardware in v1.11.1** (see "Honest gaps" #20).
v1.12.0 lets the Library tab add a disc to a userfile (or take it out),
**confirmed on real hardware in v1.12.1** (see "Honest gaps" #21).

A small desktop app for controlling a Kenwood CD-425M CD changer (also
compatible with the CD-4700M / CD-4260M, which use the same command set)
over its PC-Link serial port.

Built directly from the protocol documented at
`https://juken.sourceforge.net/protocol/` (pages supplied by the user and
transcribed into `pclink_protocol.py`).

## Requirements

- Python 3.9+
- `pyserial`
- `Pillow` (v1.9.1), for showing cover art on the Disc Data tab. Covers
  are JPEGs, which Tkinter can't show on its own. Without Pillow the app
  still runs; the cover lookup just logs that Pillow is missing.
- `tkinter` (bundled with most Python installs; on some Linux distros install
  separately, e.g. `sudo apt install python3-tk`)
- A **null-modem** USB-to-RS232 serial adapter/cable connected to the
  changer's PC-Link port (TX/RX must be swapped -- a straight-through cable
  will not work).
- Internet access, only if you want to use the Disc Data tab's "Query
  gnudb.org" feature (everything else works fully offline).
  `gnudb_client.py` and `album_art.py` use the standard library for the
  network side.

```bash
pip install pyserial pillow
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
  name, track name, genre, and door open/closed. Genre is fetched the same
  way disc/track names are (auto-fetched on slot change, plus a manual
  "Get Genre" button) -- **confirmed against real hardware**, see
  "Honest gaps" #13.
- **Table of Contents panel** for the currently-loaded disc: per-track start
  time and length, total disc length, and a computed **DiscID** (the
  classic CDDB1/freedb disc identifier, which gnudb.org's query protocol is
  compatible with) -- auto-fetched whenever the current disc changes, or on
  demand via "Refresh TOC". Unlike disc/track names, TOC data is only ever
  requested for the currently-loaded disc (confirmed against real hardware
  that it isn't available for other slots), so this panel ignores the slot
  spinbox and always tracks what's actually playing.
- **Play mode selector** (v1.6.2, **confirmed on real hardware for
  Music Type and Userfile modes**; see "Honest gaps" #15): pick any of the changer's ten play modes (Track, Program,
  Best, Music Type, Userfile, and their random variants) and click "Set
  Mode" to send `ChangeMode`. Music Type modes take a genre and Userfile
  modes take a userfile number (1-8). A "Mode Param" status row shows the
  genre or userfile the changer reports back, plus its raw byte. See
  "Honest gaps" #15.
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
  column 3 from either source as a starting point. The gnudb copy
  fills the genre dropdown only when gnudb's genre spells one of the
  changer's, ignoring case, hyphens and "&"/"and" (v1.8.7). A fixed **Genre**
  row sits above the Disc Name/Track rows (genre is disc-level only --
  there's no per-track genre) -- its Custom column is a dropdown listing
  every value in the changer's fixed genre enum (`pclink_protocol.GENRES`)
  plus a blank "don't write a genre" option, rather than free text, since
  the changer only understands that fixed set. "Write to Changer" sends
  a non-blank genre selection by folding it into the disc name's
  `Action.WRITE_NAME` write (`TextData`'s payload has its own `genre`
  field, per `cd_textdata.html`) -- reusing the currently-known disc name
  if the Custom Disc Name field is left blank, or refusing the genre
  write if no disc name is known at all. Every name write (disc name AND
  tracks) also carries whatever genre is currently known for the disc,
  not just 0, since genre turned out to be a disc-level value that ANY
  `TextData` write sets -- writing a track name used to silently reset
  genre back to "Unassigned" until this was fixed (v1.5.1). **Confirmed
  against real hardware** -- both the write mechanism itself (four
  approaches tried) and the v1.5.1 fix (genre survives an unrelated
  track-name write); see "Honest gaps" #14 for the full history.
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
- **Userfiles & Program tab** (reading v1.7.1, **confirmed on real
  hardware**): a table of userfiles #1-#8 with each one's
  name and the discs in it, plus the stored program (step, disc, track).
  Disc membership fills in automatically from normal traffic.
  "Read Userfiles for Known Discs", "Read Userfile Names" and "Read
  Program" fetch the rest. See "Honest gaps" #16. **Writing (v1.8.1,
  confirmed on real hardware)**: rename a userfile, tick which userfiles
  a disc is in, and edit and write the program (up to 32 steps). Writing
  a program also starts it playing in Program mode, and switching to
  another play mode clears it. See "Honest gaps" #17.
- **Backup tab** (v1.10.0, **export and restore confirmed on real
  hardware in v1.10.2**; see "Honest gaps" #19). "Export Library..." reads all 200 slots (DiscInfo,
  then for each disc its name, track names, genre and userfiles), plus
  the userfile names and the program, and saves them. Save as `.json`
  for a backup you can restore, or `.csv` for a catalog (one row per
  track) to read or print. It also fills in the Disc Map as it goes.
  "Restore from Backup..." writes a `.json` backup back: for each disc it
  reads the slot, writes only what differs, then reads it again and logs
  whether it now matches. It never erases a name the backup doesn't
  have, and it skips a slot whose track count doesn't match the backup,
  since that's probably a different disc. Restoring the program is a
  separate yes/no, because writing a program starts it playing. The file
  format is described at the top of `library_backup.py`.
- **Library tab** (v1.11.0, **confirmed on real hardware in v1.11.1**;
  see "Honest gaps" #20): every disc in one table (slot, name, genre,
  userfiles, track count), with the selected disc's tracks below.
  "Scan Changer" reads all 200 slots, the same walk as the Backup export
  (which refreshes this tab too). The last scan is saved to
  `library_cache.json` next to the app and shown on the next launch,
  labeled with its date. "Open Backup..." browses a `.json` backup
  offline. Search matches words in disc names, genres and track names,
  and highlights matching tracks. The Genre and Userfile dropdowns filter,
  and a click on a column heading sorts. Double-click (or Play) plays a
  disc or track via `ChangeDisc`. "Load in Disc Data Tab" loads the disc
  and switches to that tab, which only shows the loaded disc. "Rescan
  Disc" re-reads one slot. **v1.12.0 (confirmed in v1.12.1, "Honest
  gaps" #21):** the "Userfiles" menu, or a right-click on a disc, adds the
  disc to a userfile or takes it out. The Userfiles & Program tab also
  shows the saved scan's discs (marked "*") until the changer reports
  them this session.
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
  entry is fetched. A single *inexact* match (code `211`, the server's
  guess for an unknown DiscID) also goes to the picker, with a warning,
  rather than loading on its own (v1.8.3).
- **Rate limiting**: gnudb.org throttles IPs that send too many requests.
  An HTTP 403/429/503 reply is logged as "rate-limited" and isn't retried
  over HTTPS, so the server doesn't get a second request (v1.8.3). Wait
  and try again later. An HTML page where a CDDB reply should be (e.g. a
  block page) is reported as such too.
- **Non-ASCII text**: the changer stores plain ASCII only, and anything
  else is written as `?`. "Copy gnudb -> Custom" converts gnudb text to
  the nearest ASCII first (curly quotes -> `'`/`"`, dashes -> `-`, accents
  dropped, v1.8.4). **Confirmed on real hardware (v1.8.5).**
- **Tests**: `test_gnudb_client.py`, with the network mocked, plus frames
  from the first live session. **Live round-trip confirmed (v1.8.3
  session).**
- **Cover art (v1.9.1, CONFIRMED v1.9.2)**: after a gnudb entry loads, the Disc Data tab shows its cover at the top right. A
  `cddb read` response lists the entry's covers as comment lines
  (`# Cover: https://coverartarchive.org/...`), and `album_art.py` uses
  the first one that downloads. If the entry has none, it falls back to
  searching Apple's iTunes Search API for gnudb's artist and album. Only
  the artist and album text go to iTunes, not your contact email. See
  "Honest gaps" #18.

## Owner's manual notes

`protocol_reference/KENWOOD_CD-425M_instruction_manual.pdf` is Kenwood's
own manual for the CD-4700M/CD-4260M/CD-425M/DPF-J6030. It's gitignored
and not included in this repo, since it's Kenwood's copyrighted manual.
If you have your own copy, drop it at that path. It doesn't cover
the serial protocol, but it states several limits and behaviors that
matter to this app. These come from the manual, not from hardware
testing, so treat them as the manufacturer's claims until confirmed:

- **Title length: 25 characters for a disc title** (p. 28) **and for a
  user file name** (p. 35). A real read-back already fits this: slot 4's
  disc name came back as exactly 25 characters (`'The Hip / Trouble at
  the '`). Write to Changer warns (v1.8.3, **confirmed** v1.8.5) when
  the disc name is longer, showing the part the changer will keep. A
  real 44-character write was ACK'd and stored as its first 25.
- **Up to 20 track titles per disc** (p. 28, for titles entered by
  hand). It isn't known yet whether a `WRITE_NAME` for track 21+ is
  rejected, ignored or accepted. The app doesn't enforce this either.
- **Best Selection** (p. 40): a list of up to 32 favorite tracks,
  registered while each one plays. Playing it lists "set the CD player
  to stop mode" as preparation, and so does programming (p. 24). That's
  NOT why `ChangeMode(Best)` is ignored, though: that was tested while
  stopped too, and Best can't be set over PC-Link at all (see "Honest
  gaps" #15). **Programs** (p. 24): up to 32
  disc/track steps, where a step with no track number means the whole
  disc ("ALL"). That matches `DiscListing`'s `0xAA` all-tracks marker in
  `pclink_protocol.py`.
- **User files** (p. 33-36): 8 of them, a disc can be in several, and
  each can be given a name (read back via `InfoType.USERFILE_NAMES`, not
  yet used). Music Type and User File play go through their discs in
  increasing slot order.
- **Stored data is keyed to the disc, not the slot** (p. 27): the changer
  keeps titles, music types and user files for up to 210 discs by each
  CD's own ID, so a disc keeps its data when moved to another slot.
  **Confirmed on real hardware (v1.6.7):** a disc moved from slot 4 to
  slot 1 kept its title, track names and genre.
- **Up to 20 track titles per disc** shows up on the wire too: one read
  of a 10-track disc returned entries 11-20 with a single `0x01` byte as
  their text. The app treats text made only of control characters as "no
  title" (`proto.is_placeholder_text`).
- **The front-panel menu lists 26 music types** (p. 31), while
  `proto.GENRES` has 29 codes: it adds Unassigned, Unknown and Erotic,
  which the manual's list leaves out.
- **ALL DATA READ** (p. 18) is under the remote's MODE menu. **Resetting
  all registered data** (titles, music types, user files, Best
  Selection; p. 41) means holding Stop while plugging the power back in.
  Memory backup lasts at least 3 weeks unplugged (p. 42).

## Not yet implemented

- **The standalone `Action.SET_USERFILES` write.** Not needed: disc
  membership is written through `TextData`'s `userfiles` byte instead,
  confirmed in v1.8.1 (see "Honest gaps" #17).
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
- `protocol_reference/` -- saved copies of the community protocol
  documentation this app was built from. Kenwood's own owner's manual
  (`KENWOOD_CD-425M_instruction_manual.pdf`) can sit there too, but it's
  gitignored and not committed; see "Owner's manual notes" above.
- `gnudb_client.py` -- minimal HTTP client for gnudb.org's CDDB-compatible
  query/read protocol (stdlib `urllib` only, no serial or Tkinter
  dependency; tested in `test_gnudb_client.py` against responses in
  gnudb.org's documented format).
- `album_art.py` -- cover art for a gnudb entry: the entry's own cover
  links first, then an iTunes search. Decodes with Pillow; tested in
  `test_album_art.py`.
- `library_backup.py` -- the Backup tab's file format and restore
  planning (no serial or Tkinter dependency; tested in
  `test_library_backup.py`).
- `library_browser.py` -- the Library tab's search, filters, sorting and
  scan cache (no serial or Tkinter dependency; tested in
  `test_library_browser.py`).

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
   observed starting at exactly `00:02`. **Known limitation: exact
   gnudb matches aren't expected on this changer (v1.8.6).** Every disc
   the user looked up got inexact matches only. The CD-425M's TOC times
   always have `frames = 0` (whole seconds only). v1.8.6 blamed that,
   guessing the changer rounds its seconds. v1.9.2's log points
   elsewhere: for two discs (Limblifter, Rusty), a gnudb candidate
   matched our checksum and length exactly, and only the last byte
   differed. Ours was the right track count; gnudb's (`0x84`, `0x81`)
   couldn't be one. So our IDs look right, and those gnudb entries (all
   in `data`) seem to be stored under non-standard IDs. Henhouse had no
   such candidate, so rounding may still matter for some discs. Either
   way, the inexact-match picker is the normal lookup path, and it has
   found the right album every time so far. See `CHANGELOG.md`
   v1.8.5, v1.8.6 and v1.9.2.
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
   surfaced. `WRITE_PROGRAM` got a UI in v1.8.0 and is
   confirmed (v1.8.1, see #17); `SET_DISC_GENRE` / `SET_USERFILES` share the same
   plumbing but aren't used.
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
13. **Reading genre -- CONFIRMED against real hardware.** The status
   panel's "Genre" row and the Disc Data tab's Genre row are
   auto-fetched/populated the same way disc/track names are
   (`DataAccess(RETRIEVE_DATA, DiscGenre)`, replying with a single
   `CMD_DISC_GENRE` frame -- the same "one reply frame per DataAccess
   request" assumption as gap #3 above, now specifically verified for
   genre too). Exercised successfully across every one of the sessions
   in gap #14 below, both before and after writes.
14. **Writing genre -- CONFIRMED against real hardware, but NOT via the
   standalone action the protocol docs' own enum suggests, and NOT
   without hitting (and then fixing and CONFIRMING the fix for) one more
   real bug along the way (v1.5.1).** Four different approaches were
   tried, in order, against slot 2 on 2026-09-21, before one worked:
   - **Attempt 1 (v1.4.0, standalone `Action.SET_DISC_GENRE`,
     `send_write()`'s two-transaction choreography)**: a real bug -- the
     initiating `DataAccess(SET_DISC_GENRE)` request's own `genre` field
     was left at 0 instead of the target value ("Hip Hop"), because the
     call site never passed it to `encode_data_access()`. Fixed in
     v1.4.1.
   - **Attempt 2 (v1.4.1, same approach, bug fixed)**: the request now
     correctly carried the target genre (`... 10 10 02 00 00 00 0d c7`),
     and the changer still replied with `ReadyForData` -- but the
     follow-up `DiscGenre` frame (transaction 2, the same shape
     CONFIRMED for `WRITE_NAME` in gap #11, just with `CMD_DISC_GENRE`
     instead of `CMD_TEXT_DATA`) was rejected with an immediate `EOT`
     instead of `ACK`/`NAK`. Genre didn't change.
   - **Attempt 3 (v1.4.2, standalone `SET_DISC_GENRE`, NO follow-up
     frame)**: reasoning that genre already fits inside `DataAccess`'s
     own payload so a follow-up shouldn't be needed. The request went
     out correctly, the changer ACK'd it and closed the transaction
     cleanly -- but genre still didn't change, since `ReadyForData`
     means "send the payload now" and nothing was sent. Disproved the
     "no follow-up needed" theory; the two earlier rejections must have
     been about the follow-up frame's *shape*, not that none was wanted.
   - **Attempt 4 (v1.4.3, abandoning the standalone action entirely) --
     WORKED.** Re-reading `cd_types.html` directly ruled out a wrong enum
     value (`Action.SET_DISC_GENRE`, `DataType.DISC_GENRE`, and all 29
     genre codes match the docs exactly). `cd_textdata.html` showed
     `TextData`'s payload -- the one `Action.WRITE_NAME` already writes,
     already CONFIRMED working -- has its own `genre` field alongside
     `text`. Folding genre into a disc-name `WRITE_NAME` write
     (`merge_genre_into_write_items()` in `pclink_app.py`) and sending
     it through the *already-confirmed* `WRITE_NAME` choreography (no
     new shape to guess at) worked on the first try: `ReadyForData` came
     back with `raw_byte: 1` (matching the `WRITE_NAME`-confirmed
     pattern, unlike the `raw_byte: 8` seen in all three failed
     `SET_DISC_GENRE` attempts), the follow-up `TextData` frame was
     ACK'd normally, and an independent re-read afterward showed
     `genre=23/"Rock"` for both the disc name and every track.

   **v1.5.1 bug, found immediately after (real hardware, same session)
   -- fix also CONFIRMED against real hardware:** writing a track name
   shortly after the successful genre write above silently reset genre
   back to "Unassigned" -- both the standalone `DiscGenre` re-read and
   every track's `TextData` re-read came back `genre: 0`. Root cause:
   genre is a DISC-LEVEL value that the changer sets from the `genre`
   byte of **every** `TextData` write it receives, not just the one it
   happens to be attached to -- what looked like the changer "echoing
   the disc's current genre into every reply" in Attempt 4 above was
   actually only half the picture; it also *honors* that byte on every
   *write*. `merge_genre_into_write_items()` was attaching genre only to
   the disc-name item and leaving every track item's genre byte at 0,
   which (it turns out) doesn't mean "don't touch genre" on this unit --
   it means "set genre to Unassigned." Fixed: every item in a write
   batch -- disc name and every track -- now carries the SAME genre byte
   (the newly selected one, or the previously-known one if the dropdown
   wasn't touched, so a plain name-only write can't silently erase an
   existing genre). **Retested (slot 4): set genre to "Folk", then wrote
   an unrelated track name with the dropdown left alone -- the follow-up
   frame correctly carried genre `0x0b`/"Folk", and an independent
   re-read afterward showed "Folk" for the disc name and every track,
   with no reset.**

   If the Disc Name row's Custom column is left blank when writing a
   genre, the currently-known disc name is reused so it isn't wiped out;
   if no disc name is known at all for that slot, the write is refused
   rather than risking a blank name. The old standalone-`SET_DISC_GENRE`
   plumbing (`build_genre_write_frames`, `encode_disc_genre`/
   `decode_disc_genre`) is kept around unused in `pclink_app.py`/
   `pclink_protocol.py`, same spirit as `encode_long_text_data`, in case
   it's ever worth revisiting. The Disc Data tab's Genre row Custom
   column is a dropdown (not free text) listing every value in
   `pclink_protocol.GENRES` -- the changer only understands that fixed
   enum, so free text isn't offered the way it is for names. See
   `CHANGELOG.md`'s v1.4.1-v1.5.1 entries for the full raw-byte detail of
   all four write approaches and the v1.5.1 bug.

15. **Play mode selector (`ChangeMode`) -- CONFIRMED against real
   hardware for Track, Music Type and Userfile modes (v1.6.1-v1.6.7).**
   - `ChangeMode` is ACK'd with no reply data, and the changer reports
     the new mode in its next `InfoEvent`. Mode changes made on the
     remote show up in the status panel too.
   - **Music Type mode** was confirmed with two genres: Rock played slot
     2 (a Rock disc) and Alternative Rock played slot 4 (an Alternative
     Rock disc).
   - **The userfile `param` is a bit, not a number (v1.6.2).** Slot 2 was
     tagged only as #3 (`userfiles=0x04`), and `ChangeMode(Userfile,
     0x04)` played it.
   - **The changer silently ignores some mode changes**: it ACKs the
     request and then sends nothing, with no event and no error. The app
     logs a notice if no `InfoEvent` reports the new mode within 5s.
   - **Best mode can't be set over PC-Link (v1.6.4).** `ChangeMode(Best)`
     was ignored while Playing, while Stopped, and with a Best list
     stored, even though Best started from the remote works and reports
     `mode=4`, the same code the app sent. It's left out of the Play Mode
     dropdown; the status panel still shows it when started from the
     remote.
   - **`InfoEvent`'s byte 5 is the disc's genre, not `num_tracks` as
     documented (v1.6.5).** Slot 2 (Rock, 13 tracks) reads `0x17` and
     slot 4 (Alternative Rock, 12 tracks) reads `0x03`, including in plain
     Track Mode. It's decoded as `genre`/`genre_name`.
   - **`InfoEvent`'s `param` in Music Type mode** reads `0x00`, not the
     selected genre (seen with both Rock and Alternative Rock), so the
     Mode Param row shows only the raw byte there.
   - **Random modes can't be set via `ChangeMode` either (v1.6.7)**, but
     the Random button reaches them. From Track it goes Track → Random
     One → Random All → Track; from Music Type, Music Type ↔ Random All;
     from Userfile, Userfile ↔ Random One. The Repeat button toggles
     `repeat`. The dropdown now offers only Track, Music Type and Userfile
     modes, with an on-screen hint pointing to the Random button and the
     remote.
   - **An empty userfile is silently ignored**, the same way (v1.6.7).
   - **Program mode can't be set over PC-Link either (v1.6.6).** A
     stored program plays from the remote (`mode=3`, with the `program`
     byte stepping 1, 2, ... through it), but the app's
     `ChangeMode(Program)` was ignored four times, both while Playing and
     while Stopped. Like Best, it's left out of the dropdown. **But
     writing a program (v1.8.1) did switch into Program mode and start
     playback**, see #17.

16. **Userfiles & Program tab -- CONFIRMED against real hardware
   (v1.7.1).**
   - **Program**: `DataAccess(DiscListing)` with `slot=0` returns the
     stored program as one `DiscListing` frame, shaped as documented;
     the user checked an 11-step read-back against the program they
     stored, and it matched step for step.
   - **Userfile names**: `TextData` with `info_type=7` and `slot=0`
     returns all eight, **indexed by the userfile's bit** (1, 2, 4 ...
     128), not its number. Unnamed userfiles come back as a lone `0x01`.
     (v1.7.0 assumed index = number; fixed in v1.7.1.)
   - **`DiscUserfiles`**: one frame per requested slot, matching the
     membership `InfoEvent`/`TextData` already carry.

17. **Writing userfiles and programs -- CONFIRMED against real
   hardware (v1.8.1).** All three use `send_write()`'s two-transaction
   choreography, and each re-read matched exactly.
   - **Userfile names**: `WRITE_NAME` with `slot=0`, `info_type=7`, and
     `TextData` `index` = the userfile's bit (#1 -> 1, #3 -> 4).
   - **Disc membership**: the disc's name is re-sent with the new mask in
     `TextData`'s `userfiles` byte, plus its current genre. **The changer
     honors that byte**: slot 1 went `0x00` -> `0x07`, with genre and
     names unchanged. The standalone `SET_USERFILES` + `DiscUserfiles`
     write (the same shape as the genre write rejected in #14) was never
     needed.
   - **Program**: `WRITE_PROGRAM` + `DiscListing`, `slot=0`. **Writing it
     switches the changer into Program mode and starts playback** with
     nothing else sent (the user confirmed they didn't press Play). It's
     the only way into Program mode found from PC-Link (#15).
     **Leaving Program mode clears the program**, as the owner's manual
     says for P.MODE. **Writing an empty program clears it (v1.12.2,
     CONFIRMED)**: a zero-length `DiscListing` (`02 0d 01 00 00 f2`) was
     ACK'd and read back empty. Doing that while the program played
     dropped the changer back to Track mode.
   - **Name writes keep the disc's userfiles (v1.8.2, CONFIRMED)**:
     every name write carries the disc's known mask (read first if
     needed) rather than `0`, which would clear it. A track name written
     on slot 1 (in #1-#3) left it in #1-#3.
   - `ReadyForData`'s byte was `1` or `0` for track-name writes, `1` for
     disc/userfile names and `4` for the program. All proceeded, and its
     meaning is unknown.

18. **Cover art -- CONFIRMED in the app (v1.9.2).** Internet only; it
   never talks to the changer. The user looked up three discs (Trouble at
   the Henhouse, Bellaclava, Fluke) and the right cover appeared for
   each. All three came from the gnudb entry's own `# Cover:` link
   (coverartarchive.org), so real gnudb responses do carry them. Known
   limits:
   - **The iTunes fallback is untested in the app**: none of the three
     needed it. By hand it found Trouble at the Henhouse and Highway 61
     Revisited (2026-09-23).
   - **The iTunes fallback needs the artist to match** (case, accents,
     punctuation, a leading "The" and bracketed suffixes like "(Remastered)" ignored), and so
     must the album, or at least one title must start with the other.
     Otherwise nothing is shown rather than a wrong cover. A
     starts-with match is logged as "closest title".
   - **iTunes' catalog has gaps.** Pink Floyd's *Dark Side of the Moon*
     isn't in its search results at all (only tribute albums), so it only
     gets a cover if its gnudb entry has one.
   - The cover is kept per slot for the session, like the gnudb data.
   - **Replaced in v1.9.1:** v1.9.0 used iTunes only and asked Apple's
     image server for a PNG (an undocumented conversion) so Tkinter could
     show it without Pillow. With Pillow that trick isn't needed; the
     fallback now downloads iTunes' ordinary JPEG.
19. **Library backup and restore -- CONFIRMED on real hardware
   (v1.10.2).** All 200 slots export in about 70 seconds with 3 discs.
   The first export exposed two bugs, fixed in v1.10.1 (see below). With
   v1.10.1, the user hand-edited two track names. Restore wrote back
   exactly those two, verified them on re-read, and treated the unplayed
   (99-track) slots as matching. A second restore wrote nothing. See
   `CHANGELOG.md` v1.10.2. **Still untested:** restoring userfile names,
   restoring the program, a genre- or userfiles-only change (which
   re-sends the disc name), and a slot skipped for a track-count
   mismatch. Notes:
   - **DiscInfo's track count is 99 for any disc not played since the
     changer was switched on (CONFIRMED).** Only discs played since
     power-on report a real count. Backups save 99 as `null`, and
     restore only compares counts when both are known. So **right after
     power-on, restore can't use the track count to check it has the
     right disc.** Playing each disc once first gets that check back.
   - **A track-names read starts with an index-0 frame repeating the
     disc name (CONFIRMED).** v1.10.0 backed it up as track "0", which
     broke restore; v1.10.1 skips it and ignores it in older files.
   - **A disc with no name stored.** The export assumes the changer sends a
     placeholder (`'\x01'`), which becomes `""` in the file. If it sends
     nothing at all, the name shows up as `null` ("couldn't read") instead.
   - **Restore's track-count check** is the only thing stopping a backup
     being written onto a different disc that moved into the slot. Two
     different discs with the same number of tracks would pass it, and it
     does nothing when either count is unknown (see above).
   - **Tracks 21+** (the manual's 20-title limit, see "Owner's manual
     notes") are backed up if the changer returns them, and restore
     writes them. It's still unknown what the changer does with those.
20. **Library tab -- CONFIRMED on real hardware (v1.11.1).** It sends
   nothing new: the scan is the export's slot walk (#19) plus the
   userfile-name read (#16), and Play is `ChangeDisc`. In the first real
   run, the scan found all 3 discs with their names, genres, userfiles
   and track counts (about 68 seconds for 200 slots). Playing track 6
   of slot 1 started exactly that track, Rescan Disc re-read slot 2, and
   the user reported the rest (Disc Data tab, the scan reloading on the
   next launch) working as expected. See `CHANGELOG.md` v1.11.1.
   - **DiscInfo `format` `0x90` on some empty slots** (100-102 in that
     run; every other empty slot says `0x00`). Meaning unknown; the
     track count is still 0, so they're correctly shown as empty.
   Known limits:
   - **Track counts after power-on**: a disc not played since power-on
     shows "?" (or "2?" with two named tracks), and only its named tracks
     are listed (DiscInfo says 99, see #19).
   - **It doesn't follow edits made on other tabs.** Use "Rescan Disc"
     after renaming or re-tagging a disc.
   - **The saved scan can go stale** (discs moved, or renamed from the
     remote). The tab shows the scan's date; "Scan Changer" refreshes it.
21. **Adding a disc to a userfile from the Library tab -- CONFIRMED on
   real hardware (v1.12.1).** It uses the confirmed membership write
   (#17: the disc name re-sent with the new userfile mask and the disc's
   genre). Because the Library's data may be stale, the slot is read
   fresh from the changer first, and the write is built from that read.
   Nothing is written if the slot now holds a different disc name, has
   no name, or can't be read. In the first real run, adding slot 1 to #3
   (`0x02` -> `0x06`) and taking it out again (`0x06` -> `0x02`) both
   read back exactly, with the genre and every name unchanged.
   - Since v1.12.1 (**confirmed on real hardware**), the Userfiles &
     Program tab fills in discs the changer hasn't reported this session
     from the Library's saved scan, marked "*". That is display only;
     writes still read the disc from the changer first. "Read Userfiles
     for Known Discs" reads the scan's slots too, so it works without a
     Disc Map scan.
   Known limit: a disc with no name can't be added (same as the
   Userfiles & Program tab); name it on the Disc Data tab first.
22. **No artist field on the CD-425M, as far as tested (v1.12.3, real
   hardware).** `cd_types.html` lists TextData info_type `0x02` as
   "artist name". `probe_artist_name.py` tried it on slot 1 (a
   non-CD-Text disc with a user-entered name). Reading it got no reply
   frame at all: ACK, then the changer's EOT. Writing it (with the disc's
   genre and userfiles) got ReadyForData, but the changer refused the
   TextData payload with EOT instead of ACK. Nothing changed: no artist
   on re-read, and the disc name, genre and userfiles stayed the same.
   So gnudb results still go in as "Artist / Album" in the 25-character
   disc name. Untried: a disc with CD-Text (format `0x13`), in case the
   changer reports an artist read from the disc itself.
   - The same run showed a link-layer blind spot, fixed in v1.12.3 but
     **not yet seen in the app on real hardware**: a frame answered with
     EOT instead of ACK was taken for an ACK, so a refused write looked
     successful. It now raises `PCLinkRejected`, and the app logs the
     write as an error.

If your real unit's behavior differs from any of the above, turn on "show
raw bytes" in the log and it'll show you exactly what's being exchanged.
