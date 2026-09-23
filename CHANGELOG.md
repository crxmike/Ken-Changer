# Changelog

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
