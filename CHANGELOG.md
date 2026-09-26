# Changelog

## v1.12.12 -- Reads queued behind a CD-Text stream no longer give up

Seen on the real CD-425M (2026-09-26, 17:22 raw-byte log, connecting
with slot 4 in the drive): the disc-name auto-fetch hit the stream and
held the line from 17:22:27 to 17:22:39. The DiscTOC auto-fetch queued
behind it waited 5s to start, timed out, retried twice more the same way
and logged "gave up after 3 attempts", so the TOC wasn't read on
connect. `send()` and `send_write()` now wait up to `T_QUEUE_WAIT` (30s)
by default for a queued request to start, which covers a whole stream
read (~2s before the first frame, then up to 15s of cut-off). Collisions
are unaffected: they fail inside a started transaction, not in the
queue.

**On real hardware** (17:32 log, connecting with slot 4 in the drive):
the DiscTOC retry queued behind the disc-name read and went out right
after it (17:32:36), full TOC with lead-out. But that stream was ended
by the changer's own StateEvent after ~2s, so the wait was short enough
for the old 5s too: the long (~12s) case that failed at 17:22 hasn't
been seen again yet.

Tests: 2 more in `test_cdtext_stream.py`.

## v1.12.11 -- No name writes to a CD-Text disc; its genre and userfiles ride on track 1

From the 2026-09-26 16:32-16:40 raw-byte log (slot 4, the CD-Text disc).
**Seen on the real CD-425M** in that log:
- **A genre write works on a CD-Text disc.** "test name" written as the
  disc name with genre Soundtrack while slot 4 was in the drive
  (16:35:18): the genre changed (Christian -> Soundtrack) and stayed.
- **A written disc name is stored but not used.** "test name" read back
  only while slot 4 was out of the drive (16:38:02, and during Changing
  at 16:38:09); in the drive the disc-name read still streams. The front
  panel still showed "-----" (user report).
- **The stored track names are now the CD-Text titles cut to 25**
  ("We Wish You A Merry Chris"); the "Name 1".."Name 10" written earlier
  are gone. Fits the manual: the changer memorizes CD-Text titles.
- **Setting userfiles on slot 4 was blocked by the app.** Both routes
  re-send the disc name, so they read it first, and in the drive that
  read streams: Library "Userfiles: slot 4 not changed." (16:39:17), and
  the Userfiles & Program tab's "Couldn't read slot 4's current disc
  name" (16:40:12). Nothing was written.
- The format byte: slot 4 reports `0x90` in DiscInfo, DiscTOC and every
  text reply, in the drive or not; slots 1-3 report `0x00` everywhere.

**CONFIRMED on the real CD-425M** (2026-09-26, 17:07-17:09 raw-byte log,
slot 4 in the drive):
- Recognized from the first stream frame on connect (17:07:11), logged
  once.
- **A track-name write sets userfiles.** Userfiles tab, 0x00 -> 0x07
  (17:07:47): `02 fe 13 00 04 00 01 07 01 1a 00 "Silent Night"`. Read
  back 0x07 in every text frame, the InfoEvent and DiscUserfiles; genre
  kept (Soundtrack).
- **Genre on track 1** from the Disc Data tab, Soundtrack -> Country
  (17:09:10): `... 01 07 01 07 00 "Silent Night"`; userfiles kept 0x07.

Also CONFIRMED (17:13-17:18 log and a screenshot):
- The Disc Data tab greys out slot 4's Custom column and says why.
- **The Library's userfile menu** took slot 4 out of #2 (17:13:46,
  0x07 -> 0x05) while it was in the drive: the slot read found no disc
  name, matched the scan by track 1, wrote on track 1 (`... 01 05 01 07
  00 "Silent Night"`) and read back 0x05.
- An Export then Backup restore ran over slot 4 without trouble ("4
  already matched"). Nothing differed there, so a second test followed
  (17:24-17:25 log): genre changed to Easy Listening, then the same
  backup restored. **Restore wrote one value to slot 4, on track 1**
  (`... 01 05 01 07 00 "Silent Night"`: Country, #1+#3), no names, and
  logged "slot 4 verified -- re-read matches the backup". The
  export's "1 disc(s) are missing a value: [4]" is slot 4's unreadable
  disc name, expected for this disc.

**Changes (made before that test):**
- **Recognizing a CD-Text disc** (`_note_disc_format`): format `0x90` in
  DiscInfo (only with a track count above 0: empty slots 100-101 report
  it too), DiscTOC or a disc/track text reply, or a name read that
  streams. `0x00` clears it. A slot read (Library, Backup) now includes
  `cdtext`. The log says "Slot N holds a CD-Text disc..." once.
- **No name writes to it.** The Disc Data tab greys out the Custom column
  and says why; Write to Changer writes nothing but a chosen genre, and
  says the typed names aren't written. Backup restore leaves its names
  alone (it would have written the full CD-Text titles back, cut to 25).
- **Genre and userfiles ride on track 1** (`library_backup.
  state_carrier`): a CD-Text disc re-sends track 1's name, as stored (the
  CD-Text title cut to 25), instead of the disc name, which can't be read
  in the drive. Track 1 reads fine either way. Used by Write to Changer
  (genre), the Userfiles & Program tab, the Library's userfile menu and
  Backup restore. Genre on a track write was already CONFIRMED (v1.5.1:
  a track write used to reset it); userfiles on a track write is
  CONFIRMED by the 17:07 log above.
- The write guard reads what's missing twice when needed: a disc-name
  read that streams shows the disc is CD-Text, and then track 1 is read.
- The Library's userfile check matches a CD-Text disc in the drive (no
  disc name) by track 1's title, compared to 25 characters.

Tests: `test_cdtext_write_block.py` (33), built from the 16:32-16:40 and
17:07-17:09 logs' frames; `test_cdtext_stream.py`'s fakes know the new attributes.

## v1.12.10 -- Full CD-Text track names for a CD-Text disc in the drive

Uses v1.12.9's finding in the app. **CONFIRMED on the real CD-425M**
(2026-09-25, raw-byte log, slot 4 playing):
- Connecting (16:55:59): the disc-name read streamed and was cut off
  (a StateEvent from the changer ended it at once), "Slot 4 is a CD-Text
  disc in the drive with no CD-Text disc title.", then DiscInfo (10
  tracks) and ten per-track reads, each one `LongTextData` frame with the
  full title ("We Wish You A Merry Christmas", "God Rest Ye Merry
  Gentlemen", "Hark! The Herald Angels Sing"), then EOT. About 8s in all,
  ~0.35s per track.
- Rescan (16:56:43): the same, then genre and userfiles; "its name
  couldn't be read, so the saved values were kept". About 9s.

Known gap: the Library/Backup now hold full CD-Text titles for such a
disc, longer than the changer's 25-character stored names. A Backup
restore of that disc would write them (cut to 25) as stored names, which
the changer may later replace anyway (v1.12.7). Ties in with the parked
idea of not writing to CD-Text discs.

- `encode_data_access`'s `unknown` argument is now `track` (0 = all
  tracks / the disc name, N = track N).
- Track names are read through one routine, `_read_track_names_sync`:
  the usual all-tracks read first, and if that streams, **one request per
  track**, 1 to N (N from DiscInfo; with no known count, until a track
  gets no reply, as past the last track). A CD-Text disc in the drive
  answers each with its full CD-Text title as `LongTextData` (not cut to
  25), which `_cache_name` already stores. Everything else still gets the
  single all-tracks read.
- If the disc-name read streams first, the all-tracks read (which would
  stream too, ~5-13s) is skipped and it goes straight to per-track.
- Used by the automatic fetch when a disc becomes current, Get Track
  Names, the re-read after Write to Changer, and Rescan/Backup/Library
  slot reads.
- A disc-name read that streams now logs "Slot N is a CD-Text disc in
  the drive with no CD-Text disc title." and shows the disc name as
  empty (the front panel shows "----"). Rescan still keeps the saved
  disc name in that case (it wasn't read), as in v1.12.4. The link's
  error text says the same instead of "play another disc".
- The trigger is the stream itself, not the `0x90` format byte: only one
  CD-Text disc has been seen, empty slots report `0x90` too, and whether
  a disc is in the drive isn't reliably known when a read starts. The
  price is one stream and cut-off (~5-13s) per read of that disc.

Tests: `TestSlotReadAfterStream` in `test_cdtext_stream.py` (7), with the
16:35 log's titles; `test_genre_feature.py`'s write-worker test stubs the
new re-read.

## v1.12.9 -- DataAccess's "unknown" byte is a track number: full CD-Text track names from the disc

**The lead:** the user's own earlier program (`KENWOODv2.pde`, Processing)
read the current track's name from this CD-Text disc while it played. Its
track-name request puts the track number in the byte after the slot,
which `cd_dataaccess.html` calls "unknown" and this app always sends as
`0x00`: `02 03 07 00 00 01 <slot> 00 <track> 01 00 <cs>`.
`probe_cdtext_stream.py` got `--tracks N,N,...` to do the same (its
encoder reproduces KENWOODv2's disc 1 / track 1 frame byte for byte).

**CONFIRMED on the real CD-425M** (2026-09-25 16:35, raw-byte log, slot 4
in the drive and playing):
- Track 1 (`02 03 07 00 00 01 04 00 01 01 00 ef`): one `LongTextData`
  frame after ~0.25s, `02 fd 14 00 04 00 01 00 01 06 90 01 53 69 6c ...`
  = slot 4, **track 1**, userfiles 0, info_type 1, genre `0x06`, format
  `0x90`, `seq` 1, "Silent Night"; then the changer's EOT. No stream.
- Track 2: "O Holy Night". Track 3: **"We Wish You A Merry Christmas"**
  (29 characters). Track 10: **"Hark! The Herald Angels Sing"** (28). The
  names come from the disc and are **not cut to 25 characters**, unlike
  the stored copy. Each fits in one frame, `seq` 1.
- Track 11 (past the last track): ACK, then the changer's EOT, no frame.
- Genre and userfiles read normally afterwards.

**So the endless stream is what "track 0" gets on this disc:** track 0
is the disc title, the user confirms the disc has none (the front panel
shows "----" for it), and asking for it loops over empty frames instead
of answering. Every read the app sends asks for track 0.

**Also CONFIRMED, same day 16:40, slot 2 in the drive:**
- Slot 1 (no CD-Text, format `0x00`), tracks 1, 2, 12: one **`TextData`**
  frame each with just that track's stored name ("Gift Shop",
  "Springtime in Vienna", "Put It Off"), then EOT. Track 13 (past the
  last): ACK, EOT, no frame.
- Slot 4 (the CD-Text disc, out of the drive), tracks 1 and 3: one
  **`TextData`** frame each from the stored copy, cut to 25 characters
  ("We Wish You A Merry Chris").

**The model this gives** (one CD-Text disc and one ordinary disc seen):
- The byte after the slot in `DataAccess` is a track number. `0` means
  "all": for track names, every stored name in one reply (the index-0
  frame repeating the disc name, as always); for the disc name, the disc
  name. `N` means just track N. Past the last track: no frame.
- Stored names come back as `TextData` (25 characters max). A CD-Text
  disc **in the drive** answers from the disc instead, as
  `LongTextData`, full length. Asked for track 0 when the disc has no
  CD-Text disc title, it loops forever on empty frames (v1.12.8), and a
  "track names, all" read never gets past track 0.
No app change yet besides the version bump.

## v1.12.8 -- probe_cdtext_stream.py: the stream is an endless 70-frame loop with nothing in it

**New probe, run on the real CD-425M** (2026-09-25, 16:09-16:20, after a
power cycle and ALL DATA READ). `probe_cdtext_stream.py` reads slot 4's
DiscInfo, disc name, track names, genre and userfiles, letting each name
read's reply run for 60s (ACKing every frame) instead of cutting it off
after 5. To allow that, `PCLinkConnection.send()` got `reply_window=` and
`let_stream_run=` (probe-only; the app doesn't use them). Three runs:

1. **Slot 4 playing** and 2. **slot 4 in the drive, stopped** -- identical:
   - Disc name and track names: first frame after ~1.8s, then 1050
     `LongTextData` frames in the 60s, all the same except `seq`: slot 4,
     track 0, info_type as requested, userfiles 0, genre `0x06`, format
     `0x90`, text `0x01`.
   - **`seq` runs 1 to 70, then starts again at 1**, with no gaps: 15
     full cycles in each read. Each cycle is ~1.75s of silence (the same
     as the wait before the first frame) then 70 frames ~0.03s apart
     (~3.9s per cycle). It never ended by itself and never carried any
     text. What the 70 counts is unknown.
   - All four cut-offs landed in the silence between cycles: the changer
     sent seq 1 of the next cycle, got no ACK, and went quiet (~3s in
     all, ended by our 2.5s-quiet fallback). Earlier mid-cycle cut-offs
     took up to 10s (v1.12.5).
   - Genre (`0x06`) and userfiles (`0x00`) read normally afterwards.
3. **Another disc (slot 2) playing**: slot 4's stored names came back as
   ordinary `TextData` straight away: disc name `0x01` (empty), track
   names "Silent Night" ... "Hark! The Herald Angels S" (the disc's
   CD-Text, cut to 25 characters).

**CONFIRMED:** over PC-Link, a CD-Text disc in the drive gives no text at
all, playing or stopped: the stream is an endless loop of empty frames,
so there's nothing to wait for, and the app's cut-off after 5 frames
loses nothing. The only way to read its titles is the stored copy, read
while another disc is in the drive.

Tests: `test_probe_cdtext_stream.py` (5). `probe_artist_name.py` and the
new probe now import `serial.tools` only in `main()`, so their tests also
load in a full `unittest discover` run (the old "one import error").

## v1.12.7 -- Next Track on the CD-Text disc: it jumps to the next disc, and its stored names turn into its CD-Text

**Clean test on the real CD-425M** (2026-09-25, raw-byte log,
15:30-15:35), slot 4 (the CD-Text disc, format `0x90`):
1. 15:32:15, slot 4 playing track 6: wrote "Title" and "Name 1"-"Name
   10" (genre now Christian). Every payload ACK'd.
2. 15:33:07, slot 1 in the drive: Rescan of slot 4 read back "Title" and
   "Name 1"-"Name 10". Stored.
3. 15:33:30, ChangeDisc to slot 4: its reads during "Changing" still
   returned "Title" etc.; Playing from 15:33:44.
4. 15:34:10, Next Track from the app (`DoAction` `a0 cf`, then Finish
   Repeatable): InfoEvent slot 4 **track 2**, then Changing, then
   InfoEvent **slot 1 track 1**. The changer went to the next disc
   (slots 1, 3 and 4 are loaded, so after 4 comes 1) instead of track 2.
   Next Track on slot 1 afterwards worked normally (15:34:43).
5. 15:35:04, slot 1 in the drive: Rescan of slot 4 read disc name `0x01`
   and track names "Silent Night" ... "Hark! The Herald Angels S", the
   disc's CD-Text titles. Genre stayed Christian and userfiles `0x00`.

**The first log of the day shows the same thing** (v1.12.4): at 14:15:00
Next Track on slot 4 gave InfoEvent slot 4 track 2, Changing, then slot
1 track 1, and at 14:15:31 slot 4's disc name "Acoustic Christmas
Celebr" was `0x01`. The sessions in between with no Next Track on slot 4
(14:36-14:42, 14:58) kept the written names through reloads and play.

So in both logged cases, **Next Track on this disc sent the changer to
the next disc, and the disc's stored names were replaced by its CD-Text,
with the disc name emptied.** Whether the track change itself triggers
it (so a natural move from track 1 to 2 would too), or only the Next
Track command, or whether the remote does the same, isn't known. Nor is
why the changer leaves the disc. The manual says CD-Text discs can't be
given titles (README's "Owner's manual notes"), which fits the changer
restoring the CD-Text. Not a link or app bug as far as the log shows:
the app sent the same two DoAction frames that work on slot 1.

No code change besides the version bump.

## v1.12.6 -- v1.12.5 CONFIRMED; slot 4's stored names turned into the disc's CD-Text

**v1.12.5 confirmed on the real CD-425M** (2026-09-25, raw-byte log,
15:18-15:20), slot 4 in the drive:
- Connecting (15:18:14): the automatic disc-name read streamed, was cut
  off, and "Skipping slot 4's track names" followed. Genre and TOC read
  normally.
- Rescan (15:18:37): one stream, cut off, track names skipped, genre and
  userfiles read, and "its name, tracks couldn't be read, so the saved
  values were kept". No retries, no second stream, no empty track list.
- A manual Get Track Names (15:19:23): one stream, one error line, no
  retry.
- The cut-off ended three different ways, all handled: the changer
  starting an event (StateEvent ENQ right after one re-sent frame, under
  a second), 3s of quiet after one re-sent frame, and 5 re-sent frames
  then EOT (10s).

**Not understood: slot 4's stored names changed.** At 14:58:30 slot 4
read back "Title" and "Name 1"-"Name 10" (written at 14:38). At 15:20:24,
read with slot 3 in the drive, its disc name was `0x01` (empty) and its
track names were the disc's CD-Text titles: "Silent Night", "O Holy
Night", ... "Hark! The Herald Angels S" (cut to 25 characters), the
titles the front panel shows from the disc. **This proves nothing on
its own:** between 14:59 and 15:18 the user was writing various things
to the disc from the app, and that isn't logged, so an app write may
explain it.

The one case with a complete log is v1.12.4's "lost" disc name: between
14:13:42 and 14:15:31 (the 14:12-14:16 log, no writes in it) slot 4's
disc name went from "Acoustic Christmas Celebr" to `0x01`, while its
track names were these same CD-Text titles throughout. That fits the
changer replacing its stored titles with the disc's CD-Text (which
seems to have no disc title), but it's a single case and the trigger is
unknown.

The owner's manual backs this up (pp. 28-29): titles can be registered
only for non-CD-Text discs ("For discs corresponding to CD-TEXT, a new
title can not be registered"), and a CD-Text disc's own titles are
"memorized" by the changer. ALL DATA READ reads CD-Text into memory; the
manual doesn't say what else does. Over PC-Link the changer accepts the
write, and kept "Title" etc. for at least 20 minutes and several
reloads; whether it keeps it for good isn't known. See README's
"Owner's manual notes".

No code change besides the version bump.

## v1.12.5 -- The stream cut-off works on hardware; three follow-up bugs fixed

**v1.12.4 tested on the real CD-425M** (2026-09-25, raw-byte log, 14:58).
First a Rescan of slot 4 with slot 3 in the drive, and a ChangeDisc to
slot 4 with its reads during "Changing": all stored names came back as
`TextData`, as before. Then a Rescan of slot 4 with slot 4 in the drive:
- **Detection works (CONFIRMED):** 5 stream frames (seq 1-5), then our
  EOT at 14:59:14. The log showed one line for the stream instead of 40.
- **Cut-off works but takes 10s (CONFIRMED):** the changer re-sent the
  frame we didn't ACK (seq 6) 5 times, 2s apart, then sent EOT at
  14:59:24. We ACK'd it, and the next ENQ got an ACK straight away. The
  resent frames were skipped whole, so their slot byte `04` wasn't taken
  for EOT.
- **The Rescan kept the saved disc name (CONFIRMED):** "Rescan: slot 4
  updated, but its name couldn't be read, so the saved values were kept."

**Three bugs the same log shows, fixed here (not yet seen on hardware):**
- **`send()` gave up after a flat 5s,** less than the 10s cut-off. The
  read's caller took that as a timeout and retried twice. The retries
  were queued behind the cut-off, and the first one started the stream
  over at 14:59:24 (its reply began only as our EOT went out, 3s later,
  so the stream wasn't caught, and the old unsynchronized mess followed
  until 14:59:31). Now `send()`'s timeout covers only the wait for the
  request to START. Once started, it waits for the transaction to end,
  which is always bounded. A request whose caller gave up before it
  started is dropped instead of being sent later.
- **A name read that got no frames back counted as "no names".** The
  track-name read at 14:59:28 got no frame before the reply window
  closed, and the Rescan saved slot 4 with an empty track list. It's now
  "couldn't read", so the saved track names are kept.
- **The track-name read ran even after the disc name streamed,** which
  would cost another ~13s of link time for nothing. After a stream, the
  Rescan (and the automatic fetch when a disc becomes current, now one
  read after the other instead of two parallel ones) skips it and logs
  why.

Tests: 6 more in `test_cdtext_stream.py`, replaying the 14:59 cut-off and
reads.

## v1.12.4 -- A CD-Text disc in the drive answers name reads with an endless stream

**Seen on real hardware** (2026-09-25, raw-byte logs, 14:12 to 14:42).
Slot 4 ("Acoustic Christmas Celebr", genre Folk, DiscInfo format `0x90`,
the only disc so far with that format):
- **While slot 4 is in the drive** (playing or stopped), every name read
  (`DataAccess(RETRIEVE, TextData)`, disc names or track names) gets ACK,
  then after 2-3 seconds an endless run of `LongTextData` (`0xFD`) frames,
  e.g. `02 fd 09 00 04 00 00 00 00 0b 90 01 01 59`: slot 4, track 0, text
  `0x01`, and a byte after the format that counts up 01, 02, ... 0x28 in
  about a second. The track never moves on. It happened in all five
  sessions, every time the disc was in the drive.
- **While another disc is in the drive** (or slot 4 is still
  "Changing"), the same reads return slot 4's stored names as ordinary
  `TextData`, e.g. 14:42:28 `02 fe 0c 00 04 00 00 00 00 0b 90 54 69 74 6c
  65 55` ("Title").
- **The front panel shows the disc's own CD-Text** while it plays:
  "Silent Night" on track 1, where the stored name is "Name 1". So the
  changer reads text off this disc, and the stream is probably its
  attempt to send that. `0x90` may mean "CD-Text", but empty slots 100-102
  report it too (v1.7.1), so it stays "Unknown (0x90)".
- **Writes work with the disc in the drive.** "Title" and "Name 1" to
  "Name 10" were written at 14:38:41 (ReadyForData raw byte 1, every
  payload ACK'd) and read back exactly at 14:42:28 with slot 3 in the
  drive. Our frames carry format `0x00`, and the changer kept `0x90`.
- The `LongTextData` layout matches `cd_longtextdata.html`, and its three
  "unknown" bytes are userfiles (`0x00`), genre (`0x0b`) and the counter.
  The decoder now names them `userfiles`, `genre` and `seq`.

**What the stream did to the app:** the 3-second reply window closed
mid-stream and our EOT went out. The changer carried on, re-sent the
frame we never ACK'd, then sent EOT every ~2s (3 times) before going
quiet: 8-10 seconds in which every queued read (genre, TOC, userfiles)
got stream bytes instead of an ACK and failed. The app also stored the
stream's `0x01` as "no disc name", and "Rescan Disc" saved slot 4 to
`library_cache.json` with no name, tracks, genre or userfiles (twice).

**Changes, NOT yet seen on real hardware:**
- `pclink_link.py`: 5 placeholder `LongTextData` frames in a row for the
  same slot/track/info_type count as the stream. The link sends EOT, then
  discards everything without ACKing (skipping re-sent frames whole, since
  their slot byte `0x04` is also EOT) until the changer's EOT, which it
  ACKs, or 2.5s of quiet. The read raises `PCLinkTextStream` ("play
  another disc, then read it again"), which every read path already logs
  as an error. ACKing the changer's EOT is a guess that it ends the
  stream sooner: its repeated EOTs look like it waiting for that ACK.
- `pclink_app.py`: placeholder `LongTextData` frames aren't cached, and
  are logged once per stream rather than per frame.
- "Rescan Disc" (and the Library's userfile changes, which re-read the
  slot the same way) keeps the saved value of any field it couldn't read
  (`library_browser.keep_unread_fields`) and says which.

**Unexplained:** slot 4's stored disc name went from "Acoustic Christmas
Celebr" (14:13:42) to `0x01` (14:15:31) with its track names intact and
no write from the app in between. The suspect was cutting off a
disc-name stream (14:14:43), but three later cut-offs (14:38:53,
14:39:25, 14:41:39) left "Title" intact. Also unknown: whether the stream
ever ends by itself.

Tests: `test_cdtext_stream.py` (14), from these logs' frames.

## v1.12.3 -- The CD-425M has no artist-name field; a refused frame is no longer taken for an ACK

**Experiment: the artist-name text type** (2026-09-25, raw-byte log,
`probe_artist_name.py`). `cd_types.html` lists TextData info_type `0x02`
as "artist name", and `InfoType.ARTIST_NAME` had been defined but never
used. If the changer stored an artist per disc, gnudb results could stop
squeezing "Artist / Album" into the 25-character disc name. Slot 1 ("The
Tragically Hip / Trou", genre `0x03`, userfiles `0x02`, no CD-Text):
- Read: `DataAccess(RETRIEVE_DATA, TextData, info_type 0x02)`
  (`02 03 07 00 00 01 01 00 00 02 00 f2`). ACK, then the changer's EOT, no
  reply frame. Same result before and after the write.
- Write: `WRITE_NAME` with info_type `0x02` and genre `0x03`
  (`02 03 07 00 80 01 01 00 00 02 03 6f`). The changer sent ReadyForData
  (`02 09 01 00 01 f5`, raw byte 1, as for name writes). The TextData
  payload (`02 fe 0e 00 01 00 00 02 02 03 00 4e 69 72 76 61 6e 61 1d`,
  "Nirvana", genre and userfiles carried over) was answered with **EOT
  instead of ACK**, the same refusal seen in v1.2.0 and v1.4.1/v1.4.2.
- Re-read: the disc name, genre and userfiles were unchanged.

**Result:** no artist field on this changer, at least for a non-CD-Text
disc. No artist UI will be built. Untried: a CD-Text disc, in case the
changer reports an artist read from the disc. The probe script stays in
the repo for that.

**Bug found by the same log:** `pclink_link.py` only checked the answer to
a sent frame for NAK or a timeout, so an EOT counted as an ACK. The probe
reported "the changer ACK'd both transactions" for a write the changer
had refused, and the app would have logged such a write as done. Now an
EOT there raises `PCLinkRejected` (a `PCLinkError`, which every write path
already catches and logs as an error). The bytes on the wire don't
change: the transaction still closes with our EOT, as in this log, where
the next request went through normally. The confirmed writes all got a
real ACK, so they aren't affected. The fix is **not yet seen in the app
on real hardware**.

Tests: `test_probe_artist_name.py` (13, one replaying this session) and
`test_send_write_raises_when_payload_answered_with_eot` in
`test_write_feature.py`, which feeds the link the logged bytes.

## v1.12.2 -- Writing an empty program CONFIRMED

**Writing an empty program clears it** (2026-09-24, raw-byte log). This
was the last untried corner of v1.8.0's program write (see v1.8.1).
- First a 3-step program (slot 1 T1, slot 2 T2, slot 3 T3) was written:
  `WRITE_PROGRAM` (`02 03 07 00 20 20 00 00 00 00 00 b6`), ReadyForData
  raw byte 4, then `02 0d 0a 00 03 01 00 01 02 00 02 03 00 03 da`. The
  re-read matched, and as in v1.8.1 the changer went to Program mode
  (`InfoEvent` `mode=3, program=1`), then Changing, then Playing.
- Then the empty write: the same `WRITE_PROGRAM` request, ReadyForData 4
  again, then a zero-length `DiscListing` (`02 0d 01 00 00 f2`). The
  changer ACK'd it, the re-read returned that same empty frame, and an
  `InfoEvent` followed with `program=0, mode=0` (Track Mode).

**New behavior:** clearing the program while it plays **drops the changer
out of Program mode back to Track mode**, like leaving Program mode clears
the program (v1.8.1) but in the other direction. Seen once. The Write
Program dialog said an empty write "may switch to Program mode and start
playing it". For an empty program it now says the changer goes back to
Track mode.

Tests: `TestRealWriteSession.test_empty_program_write_clears_it` in
`test_userfile_program_write.py`, built from this log's frames.

## v1.12.1 -- Library userfiles CONFIRMED; the Userfiles tab uses the saved scan (CONFIRMED)

**v1.12.0 confirmed on the real CD-425M** (2026-09-24, raw-byte log).
Slot 1 was in userfile #2 (`0x02`):
- Add to #3: the slot was re-read first (DiscInfo, name, track names,
  genre, userfiles `0x02`). Then the disc name was re-sent with `0x06`
  and genre `0x03` (`02 fe 20 00 01 00 00 06 00 03 00 ... 31`). The
  changer sent ReadyForData (raw byte 1) and ACK'd. The re-read showed
  `0x06` on DiscUserfiles, the disc name and every track, with the genre
  still Alternative Rock and every name unchanged.
- Take out of #3: the same steps, writing `0x02`
  (`02 fe 20 00 01 00 00 02 00 03 00 ... 35`), and the re-read showed
  `0x02`.
- A ChangeDisc to slot 1 afterward reported `userfiles` 2 in its
  InfoEvent, so the changer's own play state agrees.

**The Userfiles & Program tab ignored the Library's saved scan** (user
report). Its table is built from the userfiles the changer has reported
this session, and those are cleared on disconnect as changer-sourced
state. So after a restart it showed only the loaded disc until a Disc Map
scan plus "Read Userfiles for Known Discs" (or a Library scan) re-read
every disc, even though `library_cache.json` already had them all. Now:
- Any disc the changer hasn't reported this session is filled in from
  the saved scan and marked "*". A note under the table gives the scan's
  date. Userfile names and disc names fall back to the scan the same way.
  What the changer reports always replaces the scan's entry.
- This is display-only. The table's data isn't used for writes, which
  still read the disc's current genre and userfiles from the changer
  first (`_ensure_disc_state`), so a stale scan can't reset them.
- "Read Userfiles for Known Discs" now also reads the saved scan's
  slots, so it works without a Disc Map scan first. Slots the Disc Map
  found empty this session are skipped.

**The Userfiles tab change is CONFIRMED on real hardware too** (same
day, raw-byte log). The user reported it working as expected. The log
shows "Read Userfiles for Known Discs" run straight after connecting,
with no Disc Map scan, reading slots 1-3 from the saved scan
(`02 03 07 00 00 08 01 00 00 00 00 ed`, then slots 2 and 3). The replies
were `0x02`, `0x04` and `0x01`, matching the scan.

Tests: `TestUserfilesTabUsesTheSavedScan` (8, one of them checking the
three logged read frames). A test in `TestRealLibraryTabSession` checks
that adding #3 and taking it out again send exactly the two logged
frames.

## v1.12.0 -- Add a disc to a userfile from the Library tab (CONFIRMED in v1.12.1)

A "Userfiles" menu on the Library tab (also opened by right-clicking a
disc) lists the eight userfiles, with the selected disc's current ones
ticked. Ticking one adds the disc to it; unticking takes the disc out.
It asks for confirmation first.

It sends nothing new. The write is the Userfiles & Program tab's
membership write: the disc name re-sent with the new mask in TextData's
userfiles byte and the disc's genre kept (`plan_userfile_membership_write`,
CONFIRMED v1.8.1). The difference is where the old values come from. The
Library's data is a scan that may be out of date, so the worker first
re-reads the slot from the changer (the same reads as "Rescan Disc") and
builds the write from that, never from the scan. Nothing is written when:
- the slot is empty or can't be read, or its name, genre or userfiles
  can't be read;
- the slot now holds a different disc name than the scan saw. The write
  re-sends the name, so writing the wrong one would rename a disc;
- the disc has no name. The write needs one to re-send, same as on the
  Userfiles & Program tab;
- the disc is already in (or already out of) that userfile.
In each case the Library row is updated from the fresh read and the
reason is shown. After a write, the slot is read again, the row and the
saved scan are updated, and a mismatch is reported.

Like "Rescan Disc", it's only available while the changer scan is shown,
not a backup file, since it updates the saved scan.

Tests (`test_library_browser.py`, 14 new): `TestUserfileChange` for the
decision logic, and `TestLibraryUserfiles` against the simulated changer
(add, remove, declined, disc changed since the scan, no name, already a
member, a write that doesn't stick, the menu's ticks, backup file shown).
One test checks that adding #3 to a disc in #1 and #2 sends exactly the
frame logged in the v1.8.1 real-hardware session (slot 1 -> `0x07`).

## v1.11.1 -- Library tab CONFIRMED on real hardware

The user tried v1.11.0 on the real CD-425M (2026-09-24, raw-byte log) and
reported everything working as expected. What the log shows:
- **Scan Changer**: all 200 slots in about 68 seconds. Slots 1-3 came back
  with their real track counts (12, 13, 10; all had been played since
  power-on), names, genre and userfiles. Slots 4-200 were empty. Then the
  userfile names were read. "Scan done: 3 disc(s)."
- **Play from a track**: `ChangeDisc(slot=1, track=6)`
  (`02 0b 04 00 01 00 06 01 e9`); the changer's `InfoEvent` reported slot
  1, track 6, so the selected track was used.
- **Rescan Disc** on slot 2: one slot's reads, then "Rescan: slot 2
  updated."
- **ChangeDisc(slot=2, track=1)**, then the usual slot-change auto-fetch
  and the TOC read once the disc finished changing. That's what "Load in
  Disc Data Tab" (or Play on a disc) sends; the log can't tell which
  button was used.
- The last scan showing again on the next launch, and opening a backup
  file, don't touch the changer, so they aren't in the log. They're
  covered by the user's report.

**New observation, not understood:** empty slots 100-102 reported
DiscInfo `format` `0x90` (`02 04 05 00 64 00 00 00 90 03`) instead of
`0x00`, with a track count of 0 like every other empty slot. It makes no
difference to the app, which only counts a slot as occupied when its
count is above 0. Its meaning is unknown.

Tests: `TestRealLibraryTabSession` (the logged `0x90` empty-slot frame
stays out of the library; a track double-click sends the logged
`ChangeDisc` frame). No code change besides the version bump.

## v1.11.0 -- Library tab: browse, search and play the whole library (CONFIRMED in v1.11.1)

A new "Library" tab (next to Control) lists every disc in the changer:
slot, disc name, genre, userfiles and track count, with the selected
disc's tracks below. The v1.10.0 entry called the Backup export "a first
step toward the library browser"; this is that browser. It reuses the
export's slot walk, and adds no new serial commands.

Choices made with the user:
- **Where the data comes from.** "Scan Changer" reads all 200 slots, the
  same walk as the export (`_read_all_slots_sync`, split out of
  `_library_export_worker`), plus the userfile names but not the
  program. A Backup tab export refreshes the Library too, since it just
  read the same data. "Open Backup..." shows a `.json` backup, so the
  library can be browsed with no changer connected.
- **The last scan is kept** in `library_cache.json` next to the app
  (gitignored) and shown on the next launch, labeled with when it was
  made, since discs may have moved or been renamed since. It's an
  ordinary backup file, written via a temp file so a crash can't leave
  half of one. Opening a backup never overwrites it, and "Show Last
  Scan" goes back to it.
- **Search** matches every word anywhere in a disc: its name, its genre
  or any track name ("hip gift" finds the Hip through "Gift Shop").
  Matching tracks are highlighted. Genre and userfile dropdowns filter,
  and clicking a column heading sorts (discs missing that value go
  last).
- **Actions.** Play, or a double-click, sends `ChangeDisc` (the
  already-confirmed `begin=True` form) for the selected disc and track.
  "Load in Disc Data Tab" does the same and then switches tabs. The
  Disc Data tab only shows the loaded disc, and the TOC gnudb needs can
  only be read for the loaded disc (CONFIRMED), so the disc has to be
  loaded, which starts it playing. `begin=False` (load without playing)
  has never been tried, so it isn't used. "Rescan Disc" re-reads one slot
  and updates the saved scan.

Known limits:
- A disc not played since power-on shows its track count as "?" (or
  "2?" when two track names are known), because DiscInfo reports 99
  (v1.10.1). Its track pane lists only the named tracks.
- The tab doesn't follow writes made on other tabs. After renaming
  something on the Disc Data or Userfiles tab, use "Rescan Disc".

Also: tests that build the app now pass `library_cache_path` (a temp
file, or `None`), so running them can't overwrite the real cache.

Tests: `test_library_browser.py` (37): search, filters, sorting and rows
on the user's real discs (from the v1.10.x backups and logs), the cache
file, and the tab itself against `test_library_backup`'s simulated
changer (scan, export refresh, reload on the next launch, open a backup,
Play and Load in Disc Data Tab sending the right `ChangeDisc`, Rescan
Disc).

## v1.10.2 -- Backup export and restore CONFIRMED on real hardware

The user tested v1.10.1 on the real CD-425M (2026-09-24, raw-byte log).
The changer had been switched on with slot 1 loaded.

**Export: CONFIRMED.** Slot 1 reported 12 tracks, and slots 2 and 3
reported 99. The summary listed slots 2 and 3 as having an unknown track
count, and the file saved them as `null`, with no track "0". 3 discs
saved; all 200 slots took about 70 seconds.

**Restore: CONFIRMED.** After the export, the user changed two slot 1
track names on the Disc Data tab: track 1 to "Gift Shoppe" and track 7
to "Ass Jigglin". Restore then:
- wrote only those two tracks back ("Gift Shop", "Butts Wigglin"). Each
  write carried genre 3 and userfiles 0x02, and the changer ACK'd both
  through the usual ReadyForData choreography;
- re-read slot 1, which matched the backup ("slot 1 verified"), with
  genre and userfiles unchanged;
- treated slots 2 and 3 (still reporting 99) as already matching, not
  as "different disc", which is the v1.10.1 fix working;
- read the userfile names, found they matched, and wrote none.

Summary: "1 disc(s) written and verified, 2 already matched, 0 skipped,
0 not verified." A second restore right after: "0 disc(s) written and
verified, 3 already matched."

Still untested on hardware: restoring userfile names, restoring the
program, a genre- or userfiles-only change (which re-sends the disc
name), and a slot skipped for a track-count mismatch.

Tests: `TestRealRestoreSession` checks that restore planning produces
the exact two TextData frames from the log.

## v1.10.1 -- Backup: DiscInfo's 99-track placeholder, and a stray track "0"

The user ran the first real export (2026-09-24, raw-byte log), with the
changer freshly switched on and slot 3 loaded. Then they played slots 2
and 1 and exported again. Both exports completed, all 200 slots in about
70 seconds, and saved 3 discs. The log showed two problems.

**1. DiscInfo's track count is only real for discs played since power-on.
CONFIRMED by the user.** Right after power-on, slot 3 (the loaded disc)
reported 10 tracks, but slots 1 and 2 reported **99**
(`02 04 05 00 01 00 01 63 00 92`). After each disc had been played, they
reported 12 and 13 (`... 01 0c 00 e9`), matching their TOCs. The
`unknown` byte was 1 for every occupied slot either way, and empty slots
still report 0, so the Disc Map's occupancy check (count > 0) isn't
affected. v1.10.0 saved the 99, so restoring after the disc had been
played would have skipped it as a "different disc" (99 vs 12). The
reverse case, a backup from a played disc restored right after a
power-on, would have been skipped too.
Fix: 99 now means "unknown" (`library_backup.UNKNOWN_TRACK_COUNT`). It's
saved as `null`, and restore only compares counts when both are known.
The export summary lists discs whose count wasn't known. Downside: right
after power-on, restore can't use the track count to check it has the
right disc. See README "Honest gaps" #19.

Also seen: with a count of 99, a track-names read returns 20 titles,
with placeholders after the real ones (the manual's 20-title limit).
With the real count, it returns exactly that many.

**2. A track-names read starts with an index-0 frame that repeats the
disc name** (`02 fe 20 00 01 00 00 02 01 03 ...`, info_type 1, index 0).
The app's track-name cache stored it as "track 0", so every disc in both
backup files had a `"0"` track. `parse_library` rejects track 0, so
**neither v1.10.0 backup would have restored.** Fix: the export skips
index 0, and parsing ignores a `"0"` track so the two existing files
still load (checked against the user's actual files). The Disc Data tab
only uses tracks 1 and up, so it was never affected.

Tests: 10 more in `test_library_backup.py`, including the logged DiscInfo
(99 and 12) and index-0 frames, and trimmed entries from the user's own
v1.10.0 file. The simulated changer now sends the index-0 frame and can
report 99, like the real one.

## v1.10.0 -- Backup tab: export and restore the library (NOT yet tried on real hardware)

Why: every name, genre and userfile the user has set lives only in the
changer's memory, and the user has seen that memory get lost or go stale
after a power outage (the Disc Map caveat). A file backup is the way
back. It's also a first step toward the library browser and batch gnudb
tagging, which need the same walk over every slot.

**Export** ("Export Library...") walks slots 1-200. For each one it sends
DiscInfo, and for each disc it reads the name, track names, genre and
userfiles. Then it reads the userfile names and the program. It saves
`.json` (a restorable backup) or `.csv` (a catalog with one row per
track), depending on the extension you pick. Before reading a slot it
drops that slot's cached values. So a reply that never arrives shows up
as `null` in the file instead of a stale value from earlier in the
session. If the program editor has unwritten edits, it doesn't re-read
the program, since that would throw them away. Stopping or
disconnecting partway through saves nothing.

**Restore** ("Restore from Backup...") goes disc by disc. It reads the
slot, works out what differs (`library_backup.plan_disc_restore`), writes
only that, then reads the slot again and logs whether it now matches.
Design choices, all from things already confirmed:
- Every `WRITE_NAME` carries the backup's genre and userfile mask, since
  every `TextData` write sets both (v1.5.1, v1.8.1/v1.8.2). If only the
  genre or userfiles differ, the disc name is re-sent to carry them, the
  same way the Userfiles tab does it (v1.8.1).
- It never erases: a name missing from the backup, or `null`, leaves
  the changer's value alone.
- It skips a slot whose DiscInfo track count doesn't match the backup,
  since a different disc has probably moved into that slot.
- Restoring the program is a separate yes/no, because writing a program
  starts it playing (v1.8.1).
- Hand-edited text is folded to ASCII (`ascii_fold`) and disc names are
  cut to the 25 characters the changer keeps (v1.8.5).

Refactor: `_write_to_changer_worker`'s per-item write moved into
`_send_text_write` / `_send_write_logged`, and the blocking read into
`_retrieve_sync`, so the Backup tab reuses them. Log text is unchanged.
DiscInfo's track count is now cached too (`_disc_track_count`).

Tests: `test_library_backup.py` (46). They run against a simulated changer
that answers reads the way the CD-425M does and applies writes the way it
was confirmed to. One test feeds the export frames from real logs (slot
1's disc-name read-back and track 4 from v1.8.3, and v1.7.1's program).
See README "Honest gaps" #19 for what the first real run should look at.

## v1.9.2 -- Cover art CONFIRMED; new DiscID evidence against v1.8.6's rounding theory

The user looked up three discs with v1.9.1 on the real CD-425M against
live gnudb.org (2026-09-23, raw-byte log), then confirmed on screen that
each cover appeared in the Disc Data tab and was the right album.

**Cover art: confirmed, all three from gnudb's own links.**

| Slot | Album | Cover |
|---|---|---|
| 1 | The Tragically Hip / Trouble at the Henhouse | `coverartarchive.org/release/6966ed5b-...` |
| 2 | Limblifter / Bellaclava | `coverartarchive.org/release/e79547b9-...` |
| 3 | Rusty / Fluke | `coverartarchive.org/release/4b6f0c0d-...` |

So real gnudb `read` responses for this user's discs do carry the
`# Cover:` lines (v1.9.1 had only the docs' example to go on). The first
link downloaded and decoded every time. The iTunes fallback wasn't
needed, so it is still untested in the app. The Henhouse release is the
same MusicBrainz release that came top in a hand search for that album
while building v1.9.0.

**DiscIDs: our IDs look right; the v1.8.6 explanation looks wrong.**

| Disc | Ours | Closest gnudb candidate |
|---|---|---|
| Limblifter / Bellaclava (13 tracks) | `ae0b3b0d` | `ae0b3b84` |
| Rusty / Fluke (10 tracks) | `7908ac0a` | `7908ac81` (also `7c08ac81`, `7908ad92`) |
| Trouble at the Henhouse (12 tracks) | `930c540c` | `8e0c548a`, `900c5484`, `900c568e` |

For Limblifter and Rusty, one candidate has exactly our checksum and our
length. Only the last byte differs. In a standard CDDB1 ID that byte is
the track count: ours (`0d` = 13, `0a` = 10) is right, and gnudb's
(`84`, `81`) can't be a track count. v1.8.6 blamed the changer rounding
its whole-second TOC times, but that would change the checksum, and here
it matches. The better explanation is that our IDs are correct and these
gnudb entries, all filed under `data`, are stored under a non-standard
ID, so an exact match can't happen. Henhouse has no such candidate
(every checksum differs), so rounding may still play a part for some
discs. Not established: how gnudb arrives at IDs like `ae0b3b84`; its
protocol page doesn't say.

Either way, the practical conclusion stands: lookups on this changer
come back as inexact matches, and the picker is the normal path. The log
line "no exact match (normal for this changer -- its TOC has whole
seconds only)" now overstates the cause. It's left unchanged for now,
since the behavior it describes is still right.

Tests: `TestRealDiscIdComparison` in `test_gnudb_client.py` (our DiscIDs
from the logged TOC frames, and the checksum/length match). No code
change besides the version bump.

## v1.9.1 -- Cover art from gnudb's own links first; Pillow is now required

The user pointed out that gnudb.org's docs show cover art in `cddb read`
responses. v1.9.0 missed this: I assumed CDDB entries were text only and
didn't check gnudb's docs. The code hid it too, because
`gnudb_client.read()` threw away every `#` line as a comment. gnudb's
documented example has one pair of lines per MusicBrainz release:

```
# Cover: https://coverartarchive.org/release/<mbid>/<id>-500.jpg
# Artid: <mbid>
```

- `read()` now collects the Cover URLs into `GnudbDisc.cover_urls`, in
  order. Everything else it parses is unchanged.
- `album_art.fetch_album_art(disc)` tries those URLs first. They belong
  to the exact entry the user picked, so they're more reliable than a
  search. iTunes (v1.9.0's matching, unchanged) is now only the
  fallback, for an entry with no Cover line or whose images all fail.
  Each failed gnudb link is logged, and the log says where the cover
  came from.
- **Pillow is now a requirement** (`pip install pyserial pillow`), the
  user's choice. Cover Art Archive serves JPEGs, which Tkinter can't show
  on its own. Images are decoded and shrunk to fit 200x200 on the worker
  thread, and shown via `ImageTk`. Without Pillow the app still starts;
  the lookup logs "Pillow isn't installed".
- With Pillow, v1.9.0's undocumented iTunes PNG trick isn't needed. The
  fallback downloads iTunes' ordinary JPEG (600x600, shrunk).

Checked by hand (2026-09-23, not in the app): the Cover Art Archive link
from gnudb's documented example downloaded and displayed in the real app
window (197x200), and the iTunes fallback still found Trouble at the
Henhouse. Whether real gnudb entries for the user's discs carry Cover
lines is still unknown. **Not yet tried in the app.**

Tests: `test_album_art.py` (30): Cover lines parsed from gnudb's
documented read example, the gnudb -> next link -> iTunes order, JPEG
decoding with Pillow, the missing-Pillow message, and the worker's log
lines.

## v1.9.0 -- Cover art on the Disc Data tab after a gnudb read

When a gnudb entry loads, the app now looks up its cover and shows it at
the top right of the Disc Data tab. **Not yet tried in the app.**

gnudb has no images, since CDDB entries are text only. So the new
`album_art.py` searches Apple's iTunes Search API for gnudb's artist and
album. Cover Art Archive (via MusicBrainz) was the other option, but it
serves JPEGs, and Tkinter can't show JPEG without Pillow. iTunes returns
a JPEG artwork URL too, but Apple's image server converts to whatever
format the URL's extension names, so the app asks for
`.../200x200bb.png` and Tk shows it directly, with no new dependency.
That conversion isn't documented, so it could stop working. It worked in
a hand check against the live API (2026-09-23).

Choosing the result: the artist must match, ignoring case, accents,
punctuation, a leading "The" and bracketed suffixes like "(Remastered)".
Then the album must match exactly, or one title must start with the
other (logged as "closest title"). Otherwise the app shows no cover
rather than risk the wrong one. A search on a common title returns other
artists' albums, and a search on an artist returns their other albums.
In the hand check, Trouble at the Henhouse and Highway 61 Revisited were
found. *Dark Side of the Moon* wasn't: iTunes' results have only
tribute albums for it, and those were correctly rejected.

The lookup runs on the gnudb worker thread after the read. A failure is
only logged and never blocks the gnudb data. The cover is kept per slot
for the session, like `_gnudb_cache`, and follows the Disc Data tab when
the slot changes. Only the artist/album text goes to iTunes, not the
contact email.

Tests: `test_album_art.py` (22, network mocked; the iTunes result is
trimmed from the real Henhouse response).

## v1.8.7 -- gnudb genre matching ignores case, hyphens and "&"/"and"

In the user's screenshot, "Copy gnudb -> Custom" left the Custom genre
dropdown blank for gnudb's "folk rock". That's correct, since the
changer has no Folk Rock. But the copy needed an exact, case-sensitive
match, and gnudb genres are free text and often lowercase. So "rock" or
"jazz" would have been left blank too, even though the changer has Rock
and Jazz.

`match_changer_genre()` now matches ignoring case, hyphens vs. spaces,
extra spaces and "&" vs. "and": "rock" -> Rock, "hip-hop" -> Hip Hop,
"rhythm and blues" -> Rhythm & Blues. It still needs the same words:
"folk rock", "Alternative" and "R&B" stay blank rather than being
forced into the nearest-sounding genre, as before. No two changer
genres collide under these rules (tested). This only changes which
dropdown value gets pre-filled; the genre write path itself (confirmed
v1.5.1) is untouched. Not yet tried in the app on real hardware.

Tests: `TestGenreMatch` in `test_gnudb_client.py`.

## v1.8.6 -- Exact gnudb DiscID matches aren't expected on the CD-425M (known limitation)

The user looked up several more discs on the real CD-425M (2026-09-23).
Every one came back with inexact matches only, never an exact one. The
user's conclusion, adopted here: it comes from how the changer reports
the TOC. Every `DiscTOC` time has `frames = 0`, so the changer gives
whole seconds only.

Why that breaks exact matches: the CDDB1 DiscID itself only uses whole
seconds, so a TOC that just dropped the frames would still produce the
right ID. Getting it wrong on every disc means the changer's seconds
aren't the true `floor(frames / 75)` values. Most likely it rounds to
the nearest second, so some track starts come out one second late and
shift the checksum byte. v1.8.5's Henhouse candidates fit this: same
length field, different checksum. The app can't correct for it without
the real frame offsets, which this unit doesn't send. Trying every
+/-1 s combination would mean thousands of queries against a
rate-limited server.

So the inexact-match path (code `211` -> picker, v1.8.3) is the normal
way lookups work on this changer. It has found the right album every
time so far. The only code change is the log line, which now says "no
exact match (normal for this changer -- its TOC has whole seconds
only)" so it doesn't read like an error.

Not established: whether the changer rounds or does something else.
That would need a disc whose real frame-accurate TOC is known (e.g.
ripped on a PC) to compare against the changer's. **Revised in v1.9.2:**
two more discs had a gnudb candidate matching our checksum and length
exactly, which points away from rounding. See v1.9.2.

## v1.8.5 -- ASCII folding CONFIRMED; what gnudb's candidate DiscIDs show

The user retested v1.8.4 on a real CD-425M (2026-09-23, raw-byte log,
slot 1 again):

- **ASCII folding works.** After "Copy gnudb -> Custom", track 4 went
  out as `44 6f 6e 27 74 ...` ("Don't Wake Daddy", `0x27`) and track 10
  as "Let's Stay Engaged". Both read back byte-identical, replacing the
  `Don?t` / `Let?s` that v1.8.3 had stored. All 13 writes ACK'd, and
  genre and userfiles `0x07` were kept again.
- **The candidates' DiscIDs, as logged by v1.8.4:**

  | DiscID | Checksum | Length (s) | Last byte |
  |---|---|---|---|
  | ours `930c540c` | `93` (147) | `0c54` (3156) | `0c` (12 tracks) |
  | `data 8e0c548a` | `8e` (142) | `0c54` (3156) | `8a` |
  | `data 900c5484` | `90` (144) | `0c54` (3156) | `84` |
  | `data 900c568e` | `90` (144) | `0c56` (3158) | `8e` |

  Two candidates share our length exactly, and the third is 2 s longer
  (probably another pressing). None ends in `0c`, though. In a standard
  CDDB1 ID the last byte is the track count, and 132-142 tracks is
  impossible, so these three entries' IDs aren't standard CDDB1 IDs for
  a 12-track disc. All three are also filed under `data` rather than a
  music category. gnudb.org's protocol page doesn't say how IDs like
  these arise. The likely explanation is that gnudb simply has no
  standard-ID entry for this pressing, so no exact match was possible,
  and our ID may well be correct. That's unconfirmed: the checksums
  differ from ours too (142/144 vs 147). The next step is a second, more
  common disc, to see whether our ID ever gets an exact match (code 200
  or 210).
- **The 25-character disc-name warning shows up as intended.** The
  user's screenshot of the v1.8.4 Write to Changer dialog shows: "the
  disc name is 44 characters; the changer keeps only the first 25: 'The
  Tragically Hip / Trou'", matching what the changer then stored. This
  confirms v1.8.3's `disc_name_length_warning`.

No code change (version bump only). Tests:
`TestRealFoldedWriteSession` in `test_gnudb_client.py`.

## v1.8.4 -- gnudb.org round-trip CONFIRMED live; ASCII folding for gnudb text; candidate DiscIDs logged

The user ran v1.8.3 against the live gnudb.org server and a real CD-425M
(2026-09-23, raw-byte log, slot 1, The Tragically Hip's *Trouble at the
Henhouse*):

- **The lookup works end to end.** The query for our DiscID `930c540c`
  (12 tracks, 3156 s) got no exact match but 3 inexact ones (`211`). The
  new v1.8.3 path sent them to the picker instead of loading one. The
  user picked the right album, `read` loaded 12 track names, and "Copy
  gnudb -> Custom" + Write to Changer wrote all 13 values (ACK'd, re-read,
  genre Alternative Rock and userfiles `0x07` kept). v1.8.3's picker and
  inexact-match log line are confirmed. Rate-limit handling wasn't
  exercised (no block this time).
- **The changer cuts a long disc name to 25 characters when writing.**
  The 44-character "The Tragically Hip / Trouble at the Henhouse" went out
  whole (`02 fe 33 00 ...`), was ACK'd, and read back as
  `'The Tragically Hip / Trou'`. That's the manual's p. 28 limit and it
  fits v1.8.3's warning, though the log doesn't show whether the dialog
  said so.
- **Curly apostrophes were stored as `?`.** gnudb's "Don’t Wake Daddy"
  and "Let’s Stay Engaged" use U+2019, which `encode_text_data` turns
  into `?` (`0x3f` in the logged frame). Should be fixed (not yet tested
  on hardware): "Copy gnudb -> Custom" now runs gnudb text through
  `ascii_fold()`, which maps curly quotes, dashes and ellipses to ASCII
  and drops accents (Beyoncé -> Beyonce), so the Custom column shows
  exactly what will be stored. The changer can store a plain apostrophe:
  its own handshake reply is "I'm CD-425M". Re-copying and re-writing
  slot 1 should turn its two `?` back into apostrophes.
- **Our DiscID wasn't an exact match. Cause still unknown.** Every
  `DiscTOC` time on this unit has `frames = 0`: the changer gives whole
  seconds only. The frame offsets we send are therefore only accurate to
  a second. The DiscID itself only uses seconds, so it's right as long as
  the changer truncates rather than rounds. The log didn't show the
  candidates' DiscIDs, so the app now logs each one
  (`gnudb_match_summary`) and shows it in the picker. The next lookup
  will show whether gnudb's ID for the same disc differs from ours (e.g.
  another pressing, or a rounded second).

Tests (`test_gnudb_client.py`, `TestRealLookupSession`, built from this
log's frames): our DiscID from the real TOC, frames always 0, the disc
name frame byte-for-byte and its 25-character read-back, and the track 4
frame with `?` vs. the folded apostrophe. Also `TestAsciiFold` and
`TestMatchSummary`.

## v1.8.3 -- gnudb.org lookup: tests, inexact matches, rate-limit errors, disc-name length warning

Prep for the live gnudb.org test (still UNCONFIRMED; nothing here has
touched the real server). Tests: `test_gnudb_client.py` (32, network
mocked).

- **gnudb tests are now committed.** Earlier docs said the parser was
  checked against gnudb.org's documented example responses, but no test
  file for it was ever committed. `test_gnudb_client.py` covers the
  request (cmd / hello / `proto=6` / User-Agent, HTTP before HTTPS), the
  HTTPS fallback (including the bare `TimeoutError` case from the user's
  earlier traceback), query codes 200/210/211/202 and error codes, and
  `read()` (DTITLE split, `TTITLE0` = track 1, continuation lines). The
  sample responses follow the documented format but weren't captured
  from the real server.
- **A lone inexact match no longer loads by itself.** A single match used
  to be read immediately even if it came from a `211` ("inexact
  matches") reply, which is the server's guess and can be a different
  album. `GnudbMatch` now has `exact`, and only a single exact match
  loads straight away (`gnudb_auto_read_match`). Otherwise the picker
  opens, with a warning when the matches are inexact.
- **Rate limiting is reported as rate limiting.** An HTTP 403/429/503 now
  raises `GnudbRateLimited` (a `GnudbError` subclass) with a "wait and
  try again" message. Any HTTP error status (the server answered) now
  stops at once instead of retrying over HTTPS, so a throttled IP doesn't
  get a second request. Before, a block looked like a generic "Couldn't
  reach gnudb.org". Which status gnudb.org actually sends when it blocks
  is still unknown.
- **Non-CDDB replies get a clear error.** If the body isn't a CDDB
  response (e.g. an HTML block page served as HTTP 200), the error says so
  and quotes the start of it, instead of "query failed: <!DOCTYPE html>".
- **Write to Changer warns about long disc names.** "Copy gnudb -> Custom"
  fills the disc name with "Artist / Album", which is often over the
  manual's 25-character disc title limit (p. 28). Slot 4's disc name had
  already come back cut at exactly 25. The confirmation now says so and
  shows the 25 characters the changer would keep (`disc_name_length_warning`).
  It warns rather than blocks. Track names aren't checked because their
  limit isn't documented.

## v1.8.2 -- Name writes keep a disc's userfiles, CONFIRMED; program auto-play confirmed

The user tested the last open item on a real CD-425M (2026-09-23,
raw-byte log):

- **A name write now keeps a disc's userfiles.** Slot 1 was in #1-#3
  (`0x07`). Writing track 1's name ("Gift Shop") sent
  `02 fe 10 00 01 00 01 07 01 03 00 ...`, carrying userfiles `0x07` and
  genre Alternative Rock. The re-read showed `0x07` on `DiscUserfiles`
  and on every track, with genre unchanged. This confirms v1.8.0's fix.
  Before it, every name write sent `userfiles=0`, which (since the
  changer honors that byte, v1.8.1) would have removed the disc from
  all its userfiles.
- **Writing a program starts playback by itself.** The user confirmed
  they didn't press Play during v1.8.1's program write.
- **`ReadyForData`'s byte was `1` for this track-name write**, where
  v1.8.1's was `0`. So it varies even for the same kind of write, and
  it still doesn't predict success.

No code change (version bump only). Test:
`TestRealWriteSession.test_track_name_write_keeps_a_nonzero_mask`.

## v1.8.1 -- Writing userfiles and programs CONFIRMED on real hardware

The user tested v1.8.0 on a real CD-425M (2026-09-23, raw-byte log) and
reported everything working as expected. Every write was ACK'd, and every
re-read matched what was written:

- **Userfile rename**: #1 "fUCK sHIT" -> "New Name" (`index` 1) and #3
  (unnamed) -> "My List" (`index` 4). Both read back in the right
  place, which confirms the bit indexing on writes too.
- **Program write**: 3 steps (slot 1 T1, slot 2 T5, slot 3 T2). The
  re-read returned the exact frame that was sent
  (`02 0d 0a 00 03 01 00 01 02 00 05 03 00 02 d8`).
- **Disc membership via `TextData`'s `userfiles` byte**: slot 1
  `0x00` -> `0x07` (#1-#3), sent as a re-send of its disc name. The
  re-read showed `userfiles=7` on `DiscUserfiles`, on the disc name and
  on every track. Genre stayed Alternative Rock and no names changed.
  **So the changer does honor that byte on a write**, and the
  standalone `SET_USERFILES` path isn't needed.

**New behavior, seen in this log:**

- **Writing a program puts the changer into Program mode and starts it
  playing.** Right after the write, with nothing else sent, the
  changer reported `InfoEvent` `mode=3, program=1`, then Changing, then
  Playing. Next Track then stepped through the program (step 2 slot 2
  T5, step 3 slot 3 T2). That's the only way found so far to get into
  Program mode from PC-Link, since `ChangeMode(Program)` is ignored
  (v1.6.6). Not yet seen more than once. The write dialog now warns about
  it.
- **Leaving Program mode clears the program.** After
  `ChangeMode(Track)`, the program read came back empty. The owner's
  manual (p. 25, "To clear all tracks") says pressing P.MODE does the
  same thing. It also explains why last session's 11-step program was
  already gone at the start of this one.
- **`ReadyForData`'s byte varies**: `1` for disc-name and userfile-name
  writes, `4` for the program write, and **`0` for a track-name write**
  (earlier sessions recorded `1` for track names too). Every one of
  these writes worked, so the byte isn't a go/no-go signal. Its meaning
  is still unknown.

**Still unconfirmed:** that a name write *keeps* a non-zero mask. Since
the `userfiles` byte is honored, the old `userfiles=0` name writes would
almost certainly have cleared it, which is what v1.8.0's fix prevents.
But this session's only name write ("Gift Shoppe") was on slot 1 while
its mask was still `0x00`, so the fix itself wasn't exercised. Slot 1 is
now `0x07`, so writing any track name there and checking that it stays
`0x07` would settle it. Writing an empty program (clearing it) also
hasn't been tried.

Also seen again: `DiscInfo` for empty slots 100-102 reports format `0x90`
(v1.7.1). Tests: `TestRealWriteSession` in
`test_userfile_program_write.py`, built from this log's frames.

## v1.8.0 -- Writing userfiles and programs (NOT yet tried on real hardware)

The Userfiles & Program tab can now write, not just read. **Nothing here
has been tried on real hardware yet.** Three new writes:

- **Rename a userfile** (select a row, "Rename Selected..."): a
  `WRITE_NAME` through the CONFIRMED name-write path, shaped exactly like
  the CONFIRMED read from v1.7.1: `DataAccess(WRITE_NAME, TextData,
  slot=0, info_type=7)`, then a `TextData` frame with `index` = the
  userfile's **bit**. For the same name, that frame is byte-for-byte what
  the changer sent us on the read (test checks this against the logged
  frame). Names are limited to 25 plain-ASCII characters (manual p. 35).
- **Set which userfiles a disc is in** (slot + 8 checkboxes, "Write
  Disc's Userfiles"): **not** the standalone `Action.SET_USERFILES` +
  `DiscUserfiles` frame. That's the same slot+byte shape as the
  `SET_DISC_GENRE` + `DiscGenre` write real hardware rejected three
  times (v1.4.0-v1.4.2). `TextData` has a `userfiles` byte next to
  `genre`, so following the genre precedent (v1.5.0), the app re-sends
  the disc's current name with the new mask in it, and the disc's
  current genre too, so it isn't reset. It refuses if the disc has no
  name, since there'd be nothing to re-send. **Open question:** whether
  the changer honors that byte on a write. If it ignores it, the
  standalone `SET_USERFILES` path is next.
- **Program editor** (Read Program, then add/remove/reorder steps with
  a slot, track or "All tracks", then "Write Program"):
  `DataAccess(WRITE_PROGRAM, DiscListing, slot=0)`, then a `DiscListing`
  frame. There's no other mechanism for this one. Written from the
  11-step program read in v1.7.1, the frame is byte-identical to the
  changer's own reply. Up to 32 steps (manual p. 24). Writing an empty
  program is allowed, with a warning; what the changer does with it is
  unknown.

Every write re-reads afterward (names, program, or the disc's
userfiles/genre/names), so the tab shows what the changer actually
stored.

**Likely latent bug, fixed without hardware proof:** every name write
so far has sent `TextData`'s `userfiles` byte as `0`. Genre turned out to
be set by *every* `TextData` write (v1.5.1), so this byte may well work
the same way, meaning any Disc Data tab write may have been silently
removing the disc from all its userfiles. Not observed yet (no one has
checked membership after a name write). Now every name write carries the
disc's known mask. If the slot's genre or userfiles haven't been read
yet, the app reads them first, and if they still can't be read, it
writes nothing rather than guess `0` (for genre that's a change: it used
to fall back to `0`). Every name write also re-reads `DiscUserfiles`
afterward, so the log answers the question either way.

**What to look for on hardware** (with "show raw bytes" on):
`ReadyForData`'s `raw_byte` for each new write (it was `1` for every
confirmed `WRITE_NAME` and `8` for every failed `SET_DISC_GENRE`),
whether the follow-up frame gets `ACK` or an immediate `EOT`, and
whether the re-read matches.

Tests: `test_userfile_program_write.py`.

## v1.7.1 -- Userfiles & Program tab CONFIRMED on real hardware; userfile-name lookup fixed

The user tested v1.7.0 on a real CD-425M (2026-09-22, raw-byte log) and
reported it working as expected. All three reads work:

- **Read Program** (`02 03 07 00 00 20 00 00 00 00 00 d6`, slot 0)
  returned one `DiscListing` frame with 11 steps, all on slot 1: tracks
  1, 1, 2, 5, 8, 4, 2, 5, 3, 6, 9. The shape matches
  `cd_disclisting.html` exactly (length byte, then 3-byte slot/track
  items), and nothing was truncated. The user then confirmed those 11
  steps match the program they stored from the remote.
- **Read Userfiles for Known Discs**, after a full Disc Map scan (3
  discs), returned one `DiscUserfiles` frame per slot: slot 1 = `0x00`,
  slot 2 = `0x04` (#3), slot 3 = `0x01` (#1). That matches what
  `InfoEvent`/`TextData` had already reported.
- **Read Userfile Names** (slot 0, `info_type` 7) returned **eight
  `TextData` frames whose `index` was 1, 2, 4, 8, 16, 32, 64, 128**. That
  means the index is the userfile's *bit*, not its number. #1 = "fUCK
  sHIT", #2 = "BALLS FART", and the unnamed ones were a lone `0x01`,
  already filtered as placeholders (v1.6.7).

**Bug fixed:** v1.7.0 looked names up assuming index n = userfile #n.
That's the same thing for #1 and #2 (bits 1 and 2), so this session's
table looked right by coincidence, but a name on #3 (index 4) would have
shown on row #4, and names on #5-#8 would never have shown at all.
`userfile_rows()` now looks each name up by the userfile's bit. Tests:
`TestRealSession` in `test_userfile_program_view.py`, built from the
logged frames, including one that covers the #3 case.

**Noticed during the Disc Map scan, not acted on:** slots 100-102 (empty,
`track_count` 0) came back with `DiscInfo` `format = 0x90` instead of
`0x00`. That doesn't affect occupancy (which keys off `track_count`), and
its meaning is unknown.

## v1.7.0 -- Read-only "Userfiles & Program" tab (NOT yet tried on real hardware)

A new tab showing what's stored in the changer, without changing
anything:

- **Userfiles table**: one row per userfile #1-#8, with its name and the
  discs in it.
  - *Membership* uses the userfile bitmask that's already CONFIRMED
    (v1.6.2: bit n-1 = userfile #n). It's collected from every reply
    that carries one (`InfoEvent`, disc/track `TextData`, and new
    `DiscUserfiles` replies), so it fills in as discs are browsed. The
    **"Read Userfiles for Known Discs"** button sends
    `DataAccess(RETRIEVE, DiscUserfiles, slot)` for each slot known to
    hold a disc (occupied on the Disc Map, or with a disc name read),
    one at a time with the usual collision retry.
  - *Names* come from **"Read Userfile Names"**:
    `DataAccess(RETRIEVE, TextData, slot=0, info_type=7)`. Replies are
    cached by their raw `index`, and the table assumes index n =
    userfile #n. **Unconfirmed**: it could be 0-based, and the log shows
    the raw index of every name received.
- **Program table**: **"Read Program"** sends `DataAccess(RETRIEVE,
  DiscListing, slot=0)` and shows each step's disc and track, with names
  where known. A track of `0xAA` shows as "All tracks".
  **Unconfirmed**: whether slot 0 is right (a program spans discs),
  whether the reply is the program or something else (such as the Best
  list), and its exact shape.

Supporting changes: `decode_disc_listing` now tolerates a frame shorter
than its own `length` byte (it stops at the last complete item and flags
`truncated`) instead of raising on the IO thread.
`LISTING_ALL_TRACKS = 0xAA`. The new pure helpers `userfile_rows()` and
`program_rows()` in `pclink_app.py` are what the tables display. All of
this is cleared on disconnect like the other changer-sourced caches.
Tests: `test_userfile_program_view.py`.

## v1.6.7 -- Random modes can't be set via ChangeMode; the Random button reaches them

The user tried every random variant from the Play Mode dropdown on a real
CD-425M (2026-09-22, raw-byte log). **All five were ACK'd and then
ignored**, while both stopped and playing: Track Random One (`02 0c 02 00
01 00 f1`), Track Random All (`... 02 00 f0`), Music Type Random All
(`... 06 03 e9`), Userfile Random One (`... 08 01 e9`) and Userfile Random
All (`... 09 01 e8`). The plain modes kept working in between.

**The Random button (`DoAction` `0xD4A1`) does reach them.** Each press
moves to the next random option for the current mode, CONFIRMED from the
`InfoEvent` after every press (`proto.RANDOM_BUTTON_CYCLE`):

- Track → Track Random One → Track Random All → Track
- Music Type → Music Type Random All → Music Type
- Userfile → Userfile Random One → Userfile (Userfile Random All, `0x09`,
  never came up)

Also confirmed: **the Repeat button toggles `InfoEvent`'s `repeat`
flag**, on and then off. And **an empty userfile is silently ignored**:
`ChangeMode(Userfile #2)` with no disc tagged #2 produced no mode change,
and the 5-second notice caught it.

**Changed:** all five random variants join Best and Program in
`CHANGE_MODE_UNSUPPORTED`. The dropdown now offers only Track, Music Type
and Userfile modes, the three that work. A gray hint under it says to use
the Random button for random play, and the remote for Best/Program.
Tests: `TestRandomModesSession`.

**Fixed: placeholder track titles.** One `TrackNames` read of slot 3 (10
tracks) returned extra entries for indexes 11-20 whose text was a single
`0x01` byte, apparently unused entries in the changer's per-disc title
table. That's 20 in total, matching the owner's manual's 20-track-title
limit. A second read of the same slot a few minutes later returned only
the 10 real tracks. New `proto.is_placeholder_text()` treats any
non-empty text made only of control characters as "no title".
`_cache_name` now stores those as empty, dropping any cached name for
that track instead of keeping a stale one, and the log line marks them
"(placeholder: no title stored)". The user had shuffled discs between
slots earlier in the session (see next point), which may be related, but
the exact trigger isn't known: the entries appeared on slot 3, which
wasn't one of the moved slots. Tests: `TestPlaceholderTrackTitles` in
`test_write_feature.py`.

**Confirmed: disc data follows the disc, not the slot.** The user moved
the disc from slot 4 to slot 1, and its title, track names and genre
(Alternative Rock) all read back from slot 1. This matches the owner's
manual (p. 27: data is stored per disc ID for up to 210 discs).

## v1.6.6 -- Program mode can't be set over PC-Link either; removed from the dropdown

The user stored a program from the remote and tested on a real CD-425M
(2026-09-22, raw-byte log):

- **Played from the remote, the program works**, and the status panel
  follows it: `InfoEvent` `mode=3` for slot 4 track 1 (`program=1`),
  slot 4 track 2, then slot 2 track 4 (`program=2`). So in Program mode
  the `program` byte is the current step number, as documented. (It's
  also non-zero in Best/Music Type/Userfile modes, so it seems to be a
  general "position in the list" counter.)
- **The app's `ChangeMode(Program)` (`02 0c 02 00 03 00 ef`) was ignored
  every time**: three times while Playing (16:56:05, 16:57:37, 16:57:51)
  and once while Stopped (16:58:11). Each was ACK'd, and then no
  `InfoEvent` followed. `ChangeMode(Track)` sent in between (16:57:41)
  worked normally.

**Conclusion: Program behaves exactly like Best (v1.6.4).** A stored
program plays from the remote and reports the same mode code the app
sends, but `ChangeMode` won't switch into it, playing or stopped. v1.6.1's
"nothing to play" explanation for the first ignored Program request is
now disproved too.

**Changed:** `Mode.PROGRAM` joins `Mode.BEST` in
`CHANGE_MODE_UNSUPPORTED`, so the Play Mode dropdown offers only Track,
Music Type and Userfile modes (all confirmed working). The "didn't take"
notice no longer names a cause. It's kept as a safety net. Tests:
`TestProgramModeUnsupported`.

**Not tried, possible future lead:** `DoAction`'s documented codes
(`0xCBA0` Play/Pause, `0xC9A0` Stop, ...) look like the remote's own key
codes. If the remote's P.MODE and BEST SELECTION keys have codes of their
own, sending those via `DoAction` might start these modes. They aren't
documented, and guessing codes at real hardware wasn't done.

## v1.6.5 -- InfoEvent's "num_tracks" byte CONFIRMED as the disc's genre; Music Type confirmed with two genres

The user re-tagged slot 4 as Alternative Rock and tested on a real CD-425M
(2026-09-22, raw-byte log):

- **At connect, in plain Track Mode**, `InfoEvent` for slot 4 read byte 5
  = `0x03` (`02 12 09 00 04 00 01 00 03 00 ...`). Slot 4 has 12 tracks,
  and its `DiscGenre` is `0x03` (Alternative Rock). Slot 2 (Rock, 13
  tracks) still reads `0x17`. Since no genre was selected in Track Mode,
  **byte 5 is the current disc's genre, not the track count the docs
  call it.** This settles v1.6.1's all-Rock hunch. `decode_info_event`
  now returns it as `genre`/`genre_name` (the `num_tracks` key is gone;
  nothing in the app used it).
- **Music Type → Rock** (`02 0c 02 00 05 17 d6`) played slot 2, a Rock
  disc. **Music Type → Alternative Rock** (`02 0c 02 00 05 03 ea`) played
  slot 4, the Alternative Rock disc. The genre `param` works for more than
  one genre, and `InfoEvent`'s `param` was `0x00` both times.

Also: **Program mode is parked** at the user's request. Setting up a
program from the remote was proving tricky, and it'll be revisited later.
README "Honest gaps" #15 was rewritten into one clean list (the v1.6.4
edit had left the Best note in the middle of the "still open" items).
Tests: `TestTwoGenreSession`.

## v1.6.4 -- Best mode can't be set over PC-Link; removed from the dropdown

The user tested v1.6.3's theory on a real CD-425M (2026-09-22): they
pressed Stop (`StateEvent` Stopping, then Stopped at 16:26:12), then sent
Set Mode → Best (`02 0c 02 00 04 00 ee`) at 16:26:37. It was ACK'd, our
`EOT` was ACK'd, and nothing followed. The 5-second notice fired.

**The "only while stopped" theory is disproved for Best.** Across three
sessions, `ChangeMode(Best)` has been ignored while Playing (twice),
while Stopped (once) and with a Best list stored, even though Best
started from the remote works and reports `mode=4`, the same code we
send. The conclusion: **this unit doesn't honor `ChangeMode` for Best
mode.** (A `param` value the docs don't mention can't be completely ruled
out, but there's no evidence for one: the remote-started Best reported
`param=0`.)

**Changed:** Best is left out of the Play Mode dropdown
(`CHANGE_MODE_UNSUPPORTED` / `change_mode_choices()` in `pclink_app.py`).
The status panel still shows Best when it's started from the remote. The
"didn't take" notice now only mentions the remaining known case, Program
mode with nothing stored. Program is still open: it has only been tried
once, while Playing and with nothing stored. Tests:
`TestBestModeUnsupported`.

## v1.6.3 -- Best mode ignored even with a Best list; "nothing to play" theory disproved

The user retested Best mode on a real CD-425M (2026-09-22, raw-byte log):

1. They started Best playback from the remote. The changer sent
   `InfoEvent` `mode=4` (Best), slot 3 track 3, `program=1` (presumably
   the "BEST01" list position). So `Mode.BEST = 0x04` matches what the
   changer itself reports, and the Best list isn't empty.
2. The app's Set Mode → Track Mode (`02 0c 02 00 00 00 f2`) **worked**:
   `InfoEvent` `mode=0` came back right away. Next Track then moved to
   track 4, which isn't in the Best list.
3. The app's Set Mode → Best Mode (`02 0c 02 00 04 00 ee`, same bytes as
   in v1.6.1) was ACK'd and then **ignored again**: no `InfoEvent`. The
   app's 5-second notice fired.

**What this disproves:** v1.6.1 and the owner's-manual notes guessed
Best was ignored because nothing was stored. The list clearly had
entries here, so that's not it.

**Working theory, NOT confirmed:** Best and Program may only switch while
the changer is stopped. The owner's manual lists "Set the CD player to
stop mode" as the preparation for playing Best Selection (p. 40) and for
programming (p. 24), but not for Music Type or User File. That matches
every attempt so far: Best (twice) and Program (once) were all sent while
Playing and were ignored. Userfile was sent while Playing and worked
anyway, and Music Type was sent while Stopped and worked. The next test
is Stop, then Set Mode → Best.

**Changed:** the "didn't take" log notice no longer claims "nothing to
play". It now says Best/Program were ignored while playing and suggests
pressing Stop first. Tests: `TestBestModeSession` in
`test_mode_feature.py`, built from this log.

## Unreleased -- Kenwood's owner's manual as a local reference

`protocol_reference/KENWOOD_CD-425M_instruction_manual.pdf`, supplied by
the user. It's gitignored rather than committed, since it's Kenwood's
copyrighted manual. It documents front-panel/remote behavior, not the serial
protocol. The findings relevant to this app are in README's new "Owner's
manual notes" section. The main ones:

- **Best mode** needs favorite tracks registered first (up to 32, via the
  remote's BEST SELECTION button). This was offered as the reason
  `ChangeMode(Best)` was ignored in v1.6.1. **v1.6.3 disproved that**:
  it's still ignored with a Best list stored.
- **Title limits**: 25 characters for disc titles and user file names,
  and up to 20 track titles per disc. The app enforces neither yet. A
  real read-back already fits the 25-character figure (slot 4's disc name
  came back as exactly 25 characters).

No code changed.

## v1.6.2 -- Userfile param encoding CONFIRMED: it's a bit, not a number

The user ran the test v1.6.1 asked for, on a real CD-425M (2026-09-22,
raw-byte log):

1. They tagged the current disc (slot 2) as Userfile #3 on the remote.
   The changer sent an unprompted `InfoEvent` with `userfiles=0x04`.
   This confirms the `userfiles` field is the documented bitmask
   (#3 = bit 2 = `0x04`).
2. They used the app's Set Mode with Userfile Mode and #3. The frame was
   `02 0c 02 00 07 04 e7` (param `0x04`), and it was ACK'd. The changer
   replied with `InfoEvent` `mode=7, param=0x04`, then Changing, then
   Playing, on slot 2.

**This settles it.** Read as a plain number, `0x04` would mean Userfile
#4, and slot 2 isn't in #4 (its bitmask is only `0x04`). The changer
played slot 2 and reported the same `0x04` back, so `param` is a bit,
exactly what `proto.userfile_param()` already sends. No code change was
needed. Docs, docstrings and tests (`TestUserfile3Session`) are updated.

Still open from v1.6.1: whether `InfoEvent`'s `num_tracks` byte is
really the disc's genre (this session's disc was Rock again, `0x17`), and
whether Best mode needs something stored before it will switch.

## v1.6.1 -- Play Mode selector: first real-hardware session (partly confirmed)

The user tested v1.6.0 on a real CD-425M (2026-09-22, raw-byte log).

**Confirmed:**
- `ChangeMode` is a reply-less command. Every frame was ACK'd, our
  closing `EOT` was ACK'd, and no reply data came back, matching its
  0.0s entry in `REPLY_WINDOW_BY_COMMAND`. The frames on the wire matched
  the encoder exactly (`02 0c 02 00 05 17 d6` for Music Type/Rock, and so
  on; now in `test_mode_feature.py`'s `TestRealHardwareSession`).
- **Music Type Mode (Rock) works.** The changer sent `InfoEvent` with
  `mode=5` and started playing.
- **Userfile Mode (#1) works.** `InfoEvent` came back with `mode=7,
  param=0x01`, and the changer moved to slot 3, which is tagged
  Userfile #1.
- **Mode changes made on the remote show up correctly.** Picking
  Userfile #2 on the remote gave `InfoEvent` `mode=7, param=0x02`.

**New real-hardware behavior: the changer silently ignores a mode it
can't enter.** Best Mode and Program Mode (no program stored) were both
ACK'd normally, and then nothing happened: no `InfoEvent`, no error.
Whether Best failed for the same reason (nothing stored for it) isn't
known yet. Since the protocol gives no failure signal, `_change_mode`
now records the requested mode, and `_check_mode_took` logs a notice if
no `InfoEvent` reports it within `MODE_CHANGE_TIMEOUT_MS` (5s). Real
switches in this session reported back within a second.

**Docs contradicted: `InfoEvent`'s `param` isn't the genre in Music Type
mode.** While playing in Music Type (Rock), `param` read `0x00`, not
`0x17`. v1.6.0 displayed that as "Unassigned (0x00)", which was
misleading. `describe_mode_param()` now shows only the raw byte in genre
modes.

**Probably also contradicted, not confirmed yet: `InfoEvent`'s
`num_tracks` byte.** It read `0x17` on slots 2, 3 and 4. Their TOCs
show 13, 10 and 12 tracks, and all three are genre Rock (`0x17`, per
`DiscGenre`). So this byte looks like the current disc's genre, not a
track count. It's documented in `decode_info_event` but not renamed,
since every disc seen so far was Rock. The next step is a non-Rock disc.

**Still open: the userfile `param` encoding.** Userfile #1 and #2 encode
to `0x01`/`0x02` whether it's a number or a bit, so this session can't
tell them apart. Userfile #3 is the first that differs (`0x03` as a
number vs `0x04` as a bit).

Also noticed: `InfoEvent`'s `program` byte was `1` in the Music Type and
Userfile modes and `0` in Track mode, although the docs say it's "only
set in program mode". Its meaning is unknown and it isn't used.

## v1.6.0 -- Play Mode selector (ChangeMode), NOT yet tried on real hardware

**What's new:** a "Play mode" row on the Control tab's Transport panel:
a dropdown of all ten `proto.MODE_NAMES` modes plus a "Set Mode" button
that sends `ChangeMode` (`cd_changemode.html`: `byte mode, byte param`).
A Genre dropdown is enabled only for the two Music Type modes and a
Userfile # spinbox (1-8) only for the three Userfile modes; `param` is 0
for everything else. Like `ChangeDisc`, the button doesn't update the
Mode status row itself -- the changer's own `InfoEvent` is what shows
whether the mode actually changed.

Also new: a "Mode Param" status row showing `InfoEvent`'s `param` byte
(`describe_mode_param()`): the genre name in Music Type modes, the
userfile in Userfile modes, and the raw hex byte in every case.

**Open question, deliberately surfaced rather than guessed silently:**
the docs say `param` "is a userfile" in the Userfile modes but not
whether that's a plain number (1-8 / 0-7) or a bit. `proto.userfile_param()`
assumes the bit encoding `InfoEvent`'s `userfiles` field documents
("bit or'd combination of userfiles", so #1 -> `0x01`, #3 -> `0x04`).
The easiest way to settle it: pick a userfile mode from the changer's
own front panel/remote and read the raw byte in the new Mode Param row.
If it doesn't match, only `userfile_param()` needs changing.

**Worth checking on hardware:** that `ChangeMode` gets ACK'd with no
reply data (its reply window is 0.0s in `REPLY_WINDOW_BY_COMMAND`, same
as `ChangeDisc`), that the next `InfoEvent` reports the new mode, and
what the changer does when a mode has nothing to play (for example,
Program mode with no program stored, or a genre with no discs).

Tests: `test_mode_feature.py` (payload layout, frame checksum, per-mode
param handling, `InfoEvent` param description).

## v1.5.2 -- Renamed the app to "Ken Changer"

**What changed:** the app's display name changed from "Kenwood PC-Link
Control App" to "Ken Changer", out of caution around using Kenwood's
trademarked name as this project's own branding. This is cosmetic only
-- no protocol, hardware, or gnudb behavior changed.

Updated: the README title (`README.md`) and the Tkinter window titlebar
(`pclink_app.py`). Left unchanged: the `.gitignore` build artifact
filename (`kenwood-pclink.zip`) and the project folder name, both
deferred for now.

**Deliberately NOT changed:** the `"KenwoodPCLinkController"` string
used in the gnudb.org `hello=` handshake (`pclink_app.py`) and HTTP
User-Agent (`gnudb_client.py`). That string is gnudb.org's whitelisted
client identity, unrelated to the app's own branding, and changing it
risks breaking gnudb access -- see gnudb.org's access policy notes
elsewhere in this file and in `README.md`.

## v1.5.1 -- Fixed: writing a track name was silently resetting genre back to Unassigned

**What happened:** shortly after confirming v1.5.0's genre write worked
(slot 4, genre set to "Rock"), the user wrote a track name ("gift shopp"
for Track 1) in the same session, with the Genre dropdown left as-is
(not touched again). The raw log showed the track name write went out
correctly and succeeded -- but a re-read immediately afterward showed
**both** `DataAccess(DiscGenre)` **and** every track's `TextData` reply
reporting `genre: 0` ("Unassigned") again, even though nothing about
genre was supposed to change in that write.

**Root cause:** genre is a DISC-LEVEL value that the changer sets from
the `genre` byte of **every** `TextData` write it receives, not just the
one for the disc name. v1.5.0's `merge_genre_into_write_items()` only
attached the selected/known genre to the disc-name item and left every
track item's genre byte at the old default of `0` -- which, it turns
out, doesn't mean "leave genre alone," it means "set genre to
Unassigned." So any track-only write (an extremely common case -- most
uses of "Write to Changer" won't touch genre at all) was silently
clobbering whatever genre had been set, including genre set by this same
app moments earlier, or (in principle) genre from any other source.

This actually explains a detail from the v1.5.0 confirmation session
too: after that session's `WRITE_NAME` write, a re-read of `TrackNames`
showed `genre: 23` on every single track, not just the disc name entry
-- at the time this was noted as "the changer apparently echoes the
disc's current genre into every reply." That was half right: it's not
just an echo on *reads*, it's also honored on *writes* -- every
`TextData` write's genre byte actually sets the disc's genre, read
direction included.

**Fix:** `merge_genre_into_write_items()` (in `pclink_app.py`) now
computes a single `effective_genre` for the whole write batch -- the
newly selected genre if the user picked one in the dropdown, otherwise
whatever genre the app already has cached for that slot (`0` only if
truly unknown) -- and attaches that SAME value to every item, disc name
and tracks alike. `_write_to_changer` now also passes the currently-known
genre (`self._genre_cache.get(slot)`) into the merge call, including in
the "can't write genre" fallback path, so even a refused genre change
doesn't fall back to erasing an existing one.

Added regression tests in `test_genre_feature.py`
(`TestMergeGenreIntoWriteItems.test_track_only_write_preserves_existing_genre`
and friends) that reproduce the exact bug (a track-only write with a
known existing genre) and confirm the fix. `_write_to_changer_worker`
itself was already correct -- it just forwards whatever genre each item
carries -- so no change was needed there beyond the test that documents
this (`TestGenreWriteWorkerAttachesGenre`, updated to check the worker
forwards each item's genre unmodified rather than asserting track items
must be 0, which was the actual bug's assumption).

**CONFIRMED as a real bug via real hardware, and the fix is now ALSO
CONFIRMED against real hardware.** The user retested (slot 4): set genre
to "Folk" via the Disc Data tab (Genre dropdown, Disc Name Custom field
left blank so the app reused the known name), then -- in the same
session, Genre dropdown left untouched -- wrote a track name ("Don't
Wake Daddy" for Track 4). The follow-up `TextData` frame for that track
write correctly carried genre byte `0x0b` (Folk), matching the fix, and
an independent re-read afterward showed `genre: 11`/"Folk" consistently
for the disc name and every track -- genre no longer reset to
"Unassigned". This is exactly the reproduction sequence identified when
the bug was found (set genre, then write an unrelated name, then check
genre survived).

## v1.5.0 -- Genre read/write, CONFIRMED against real hardware

**The fourth approach worked.** The user retested v1.4.3's "fold genre
into a disc-name WRITE_NAME write" strategy against a real CD-425M (slot
2, target "Rock", Disc Name Custom column left blank so the app reused
the currently-known name "Limblifter-Bellaclava"):

- Request: `02 03 07 00 80 01 02 00 00 00 00 73` --
  `Action.WRITE_NAME`/`DataType.TEXT_DATA`, slot 2. ACK'd.
- `ReadyForData` came back with `raw_byte: 1` (matching `DataType.
  TEXT_DATA` -- the same value seen in every prior CONFIRMED `WRITE_NAME`
  session, unlike the `raw_byte: 8` seen in all three failed standalone
  `SET_DISC_GENRE` attempts). Transaction 1 closed with the changer's own
  `EOT`.
- Transaction 2 (fresh `ENQ`): `TextData` follow-up frame `02 fe 1c 00 02
  00 00 00 00 17 00 4c 69 6d ...` -- disc name text unchanged, genre byte
  `17` (Rock). ACK'd. The app closed the transaction with its own `EOT`,
  same as every other confirmed `WRITE_NAME` write.
- **Independent re-read afterward confirmed it took**: `DataAccess(DiscName)`
  came back with `'genre': 23, 'genre_name': 'Rock', 'text':
  'Limblifter-Bellaclava'` -- name preserved, genre changed.
  `DataAccess(DiscGenre)` (the standalone read) also came back
  `'genre': 23, 'genre_name': 'Rock'`. And, interestingly,
  `DataAccess(TrackNames)` came back with `genre: 23` on *every single
  track* too, not just the disc name -- consistent with genre being a
  disc-level property that the changer echoes into every `TextData`
  reply for that disc, not something tracked per-request. This isn't a
  bug in this app; nothing was written to the tracks, this is just what
  the changer reports back for a field it apparently always fills with
  the disc's current genre regardless of what was queried.

**Confirmed, end to end**: reading genre (`DataAccess(RETRIEVE_DATA,
DiscGenre)` -> single `CMD_DISC_GENRE` reply, auto-fetched on slot change
and via the manual "Get Genre" button) and writing genre (folded into an
`Action.WRITE_NAME` write via `merge_genre_into_write_items()`, going out
through `send_write()`'s existing confirmed choreography) both work
against a real CD-425M. Updated `pclink_app.py`'s user-facing text
(confirmation dialog, log messages, docstrings) to drop the "unconfirmed"
language now that this is genuinely confirmed, per CLAUDE.md's hard rule.

**What this means for the abandoned standalone `Action.SET_DISC_GENRE`
path (v1.4.0-v1.4.2)**: it's not necessarily *impossible* -- there could
be some other request shape that would make it work -- but three
different real-hardware attempts at it all failed, while the very first
attempt at folding genre into `WRITE_NAME` succeeded, so there's no
reason to keep pursuing it. `build_genre_write_frames()` and the
`Action.SET_DISC_GENRE`/`CMD_DISC_GENRE` write-side plumbing in
`pclink_protocol.py` are left in place, unused, same as
`encode_long_text_data`, in case that ever changes.

**What's still unconfirmed**: `WRITE_PROGRAM`/`SET_USERFILES` (no UI
yet), and gnudb.org's live round-trip.

## v1.4.3 -- Genre write retested again, still doesn't work; abandoned standalone SET_DISC_GENRE, folded into WRITE_NAME instead

**What happened:** the user retested v1.4.2's single-transaction attempt
(slot 2, target "Rock"). The raw log showed the request going out
correctly this time -- `02 03 07 00 10 10 02 00 00 00 17 bd` (genre byte
`17`, checksum `bd`, both correct for Rock) -- the changer ACK'd it, sent
`ReadyForData` (`raw_byte: 8`, same as every prior attempt), and the
transaction closed cleanly with the changer's own `EOT`. No rejection
this time. **But the genre still didn't change** -- the re-read afterward
still showed "Unassigned".

**Why v1.4.2's theory was wrong:** `ReadyForData` literally means "I'm
ready to receive the write payload" -- v1.4.2 deliberately sent nothing
after it (on the theory that genre already fits inside `DataAccess`'s own
request, so no follow-up should be needed). That theory doesn't survive
this result: if the changer already had everything it needed from the
request alone, it wouldn't matter that we sent nothing extra, and the
genre would have changed. It didn't. So a follow-up frame IS needed after
all -- which means v1.4.0's and v1.4.1's follow-up frames weren't
unwanted, they were probably just the wrong *shape*.

**Investigation:** read `cd_types.html` directly (per CLAUDE.md's
instruction for write-side commands) to check whether `pclink_protocol.py`
had a wrong enum value somewhere -- this project's history includes more
than one case of an assumed value turning out wrong on real hardware.
Checked `Action.SET_DISC_GENRE` (0x10), `DataType.DISC_GENRE` (0x10), and
every one of the 29 genre codes against the doc's own tables: all exactly
match `pclink_protocol.py`'s existing `Action`/`DataType`/`GENRES`. Ruled
out.

Re-examined `cd_textdata.html` (already read in the v1.4.2 investigation,
but not acted on): `TextData`'s payload -- the one `Action.WRITE_NAME`
already writes, CONFIRMED working on real hardware -- has its own fields
`slot / index / userfiles / info_type / genre / format / text`. Genre is
already *in there*, right next to the text, in a mechanism that's
already proven to work. This suggests genre isn't meant to be set through
a standalone `Action.SET_DISC_GENRE`/`DiscGenre` write at all -- it's
meant to ride along with a `WRITE_NAME` write for the disc's name.

**What changed:** `_write_to_changer` now folds a selected genre into the
disc-name `WRITE_NAME` write's `genre` field (via the new
`merge_genre_into_write_items()`) instead of sending a separate
`Action.SET_DISC_GENRE` request. If the Disc Name row's Custom column is
blank when a genre is selected, the currently-known disc name (from the
existing read cache) is reused so the name doesn't get wiped out; if no
disc name is known at all for that slot, the app now refuses the genre
write with an explanation rather than risking a blank name. The old
standalone-`SET_DISC_GENRE` code path (`build_genre_write_frames`, the
`Action.SET_DISC_GENRE`/`CMD_DISC_GENRE` write plumbing) is kept in
`pclink_app.py`/`pclink_protocol.py` for reference -- same as
`encode_long_text_data` is kept around unused -- in case a future retry
of that approach is warranted, but it's no longer called by
`_write_to_changer_worker`.

**STILL NOT confirmed working** -- this is the fourth distinct approach
tried for genre and, like the other three, has NOT been tried against
real hardware yet. If it still doesn't work, the remaining leads (roughly
in order of how much is left to check): whether genre can only be set for
the currently-loaded disc (slot 2 happens to be the loaded disc in every
attempt so far, so this hasn't been ruled out); or whether this specific
CD-425M unit simply doesn't support writing genre at all despite the
documented enum existing (the source docs are a community
reverse-engineering effort and don't claim every documented field is
actually writable on every unit).

## v1.4.2 -- Genre write retested, still doesn't work; dropped the follow-up frame entirely

**What happened:** the user retested with v1.4.1's fix in place (same
slot 2, target "Hip Hop"). The raw log confirmed the fix worked as
intended -- the request frame now correctly carried `02 03 07 00 10 10
02 00 00 00 0d c7` (genre byte `0d`, not `00`) -- but the genre **still
didn't change**. The follow-up `DiscGenre` frame (`02 08 03 00 02 00 0d
e6`) was rejected with an immediate `EOT` again, exactly like the first
attempt, ruling out "it was just the genre=0 bug" as the explanation.

**Investigation:** read the actual source docs for the commands involved
(`protocol_reference/cd_discgenre.html`, `cd_dataaccess.html`,
`cd_readyfordata.html`) directly, per CLAUDE.md's instruction to not just
rely on the paraphrase in `pclink_protocol.py`'s comments for write-side
commands. Neither `cd_discgenre.html` nor `cd_dataaccess.html` says
anything about a required write choreography -- `cd_discgenre.html` just
documents the plain `slot + genre` read/write payload shape (which
`encode_disc_genre`/`decode_disc_genre` already implement correctly).
`cd_dataaccess.html` confirms `DataAccess`'s own request payload already
has a dedicated `genre` field.

That's the key difference from `WRITE_NAME`: a disc/track name is
arbitrary-length text that genuinely cannot fit inside `DataAccess`'s
fixed-size request, so a second frame is structurally necessary there.
Genre is a single byte that already fits entirely inside `DataAccess`'s
own payload. The follow-up `DiscGenre` frame being flatly rejected with
an immediate `EOT` -- twice now, in two separate sessions -- is the exact
symptom this project already diagnosed once before, for `WRITE_NAME`'s
first, disproven "send the payload inline, same transaction" hypothesis.
Put together, this points at genre not needing (or wanting) a follow-up
frame at all.

**What changed:** the genre write now goes out as a single ordinary
`DataAccess` transaction (`link.send()`, not `send_write()`) with the
target genre embedded directly in the request's own `genre` field, and
no follow-up frame is sent. `build_genre_write_frames()` still computes
both the request and a `DiscGenre` follow-up payload (kept around in case
a future revision needs it), but `_write_to_changer_worker` now ignores
the follow-up part. Added `test_genre_feature.py::TestGenreWriteIsSingleTransaction`
to lock in "genre write uses `send()`, not `send_write()`" so a future
change doesn't silently revert this without a deliberate decision.

**STILL NOT confirmed working** -- this specific approach has not been
tried against real hardware yet. If it *still* doesn't change the genre,
the next things to check, in rough order of likelihood: (1) whether
`cd_types.html`'s `Genre` enum actually uses different numeric values on
this unit than `pclink_protocol.GENRES` assumes (the same way BCD
timecodes and track-number offsets turned out different from the
documented/assumed shape elsewhere in this project); (2) whether genre
can only be set for a disc that's *currently loaded* (this test used slot
2, which appears to be the loaded/current disc in these sessions, so this
is a lower-probability lead, but worth ruling out); (3) whether a
completely different action/command is actually meant for this (e.g.
maybe genre is meant to be set as part of a `WRITE_NAME`-style
`TextData`/`LongTextData` write, given both share a `genre` field in
their own payloads per `cd_textdata.html`, rather than through
`SET_DISC_GENRE`/`DiscGenre` at all).

## v1.4.1 -- Genre-write bug found by the user's first real-hardware attempt, still NOT confirmed

**What happened:** the user tried writing "Hip Hop" to slot 2 (previously
"Unassigned") and reported back with a raw-byte log (2026-09-21). The
genre did not change. Two things stood out in the log:

1. **A real bug, now fixed.** The initiating
   `DataAccess(Action.SET_DISC_GENRE, DataType.DISC_GENRE)` request frame
   on the wire was `02 03 07 00 10 10 02 00 00 00 00 d4` -- decoding its
   7-byte payload (`action, data_type, slot_lo, slot_hi, unknown,
   info_type, genre`) gives `genre = 0x00`, not `0x0D` (Hip Hop).
   `encode_data_access()` has always had a dedicated `genre` parameter
   for exactly this, but `_write_to_changer_worker`'s call site never
   passed it, so every genre write silently went out as genre=0
   regardless of what was selected in the dropdown. Since the disc's
   genre was already 0 ("Unassigned") before this test, the bug was
   invisible in the before/after comparison -- the write may have
   "succeeded" at writing the wrong value without looking any different
   from a no-op. Fixed: pulled the frame-building into a new
   `build_genre_write_frames(slot, genre_code)` helper (mirroring
   `gather_genre_write_item`) that always includes `genre=genre_code` in
   the request, and added a regression test
   (`test_genre_feature.py::TestBuildGenreWriteFrames`) that decodes the
   actual bytes and would have caught this.
2. **An open question, NOT resolved by this fix.** After the (buggy)
   request was ACK'd and the changer replied with `ReadyForData`
   (`raw_byte: 8` this time, vs. `raw_byte: 1` in the earlier confirmed
   `WRITE_NAME` session -- unclear if that's meaningful), the app opened
   a second transaction and sent the follow-up `DiscGenre` frame (`02 08
   03 00 02 00 0d e6` -- correctly carrying genre=0x0D even though the
   request didn't). The changer responded with an immediate `EOT`
   instead of `ACK`/`NAK` -- the *exact* rejection symptom this project
   already diagnosed once before, for `WRITE_NAME`'s first (disproven)
   "send the payload inline, same transaction" hypothesis (see
   `pclink_link.py`'s module docstring). Unlike a name, genre is a single
   byte that already fits inside `DataAccess`'s own request payload, so
   it's plausible the changer doesn't want *any* follow-up frame for a
   genre write -- but there's only one data point so far, and it's
   confounded by the genre=0 bug in the same request, so this needs
   another real-hardware test with the bug fixed before drawing a
   conclusion. If it still doesn't work with a correct request, the
   next thing to try is dropping the follow-up frame entirely (plain
   `link.send()` of just the DataAccess request, relying on its own
   `genre` field) instead of `send_write()`'s two-transaction dance.

**Still NOT confirmed working** -- per CLAUDE.md's hard rule, this stays
"should work" (now with the confirmed bug fixed) until the user retests
against the real CD-425M and reports back.

## v1.4.0 -- Reading/writing genre, NOT yet confirmed against real hardware

**What's new:** the status panel gets a new "Genre" row, and the Disc
Data tab gets a new "Genre" row above the Disc Name/Track rows (genre is
disc-level only, so it doesn't fit the existing per-track row scheme).

- **Reading**: genre is now auto-fetched the same way disc/track names
  already are -- `DataAccess(RETRIEVE_DATA, DiscGenre)`, alongside a new
  manual "Get Genre" button next to the existing Get Disc Info/TOC/Track
  Names/Disc Name buttons. Expected to come back as a single
  `CMD_DISC_GENRE` reply frame, per the same "one reply per DataAccess
  request" assumption this app already makes for DiscInfo/DiscTOC/
  TextData (README.md's "Honest gaps" #3) -- not specifically re-verified
  for genre.
- **Writing**: the Disc Data tab's Genre row has a dropdown (not free
  text) in the Custom column, listing every value in
  `pclink_protocol.GENRES` plus a blank "don't write a genre" option --
  the changer only understands that fixed enum, unlike names. "Write to
  Changer" now also sends a non-blank genre selection, via
  `Action.SET_DISC_GENRE` through `send_write()`'s existing two-
  transaction choreography (the same one CONFIRMED for `WRITE_NAME` in
  v1.3.0). `encode_disc_genre`/`decode_disc_genre` already existed and
  were already covered by a round-trip test in `test_write_feature.py`;
  what's new is the app-layer glue -- `gather_genre_write_item` (maps the
  dropdown's selected name back to a numeric code via the new
  `pclink_protocol.GENRE_NAME_TO_CODE` reverse lookup) and the write
  worker's new genre branch, both covered by `test_genre_feature.py`.

**NOT confirmed against real hardware yet**, per CLAUDE.md's hard rule --
neither the read nor the write. The choreography is *assumed* to carry
over unchanged from the `WRITE_NAME` confirmation because both share the
identical `send_write()` plumbing and `WRITE_ACTION_DATA_TYPE`/
`DATA_TYPE_TO_REPLY_COMMAND` mappings, but that's an inference, not a
test result -- this app's own history (reply framing, EOT semantics, BCD
timecodes, track-number offsets) is full of plausible-sounding
assumptions that turned out wrong once actually tried against a real
CD-425M. Will be updated here once the user reports back (ideally with
"show raw bytes" on) after trying it.

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
