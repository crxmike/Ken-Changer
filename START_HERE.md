# Start Here

This is a Python desktop app for controlling a Kenwood CD-425M CD changer
over its serial PC-Link port, plus a gnudb.org lookup and (soon) a
write-back feature. Read this file first, then `README.md` for full
details and `CHANGELOG.md` for the history of hardware-confirmed fixes.

## Current status

**v1.3.0** -- read/control, TOC/DiscID, the Disc Map (200-slot
occupancy grid + full-scan), and writing disc/track names
(`Action.WRITE_NAME`, via the Disc Data tab's "Write to Changer" button)
are all working and confirmed against real hardware. The write feature's
confirmation had existed before but got lost to bad file management
along with the UI wiring that used it; both are now recovered/rebuilt
and reconfirmed -- see `CHANGELOG.md`'s v1.3.0 entry for the details (22
writes against a real CD-425M, all read back matching, no quirks).
gnudb.org querying is CONFIRMED live (v1.8.3 session; see v1.8.4 in
`CHANGELOG.md`). See `CHANGELOG.md` for the full
confirmed list.

**v1.5.0/v1.5.1** -- reading/writing disc genre is CONFIRMED against real
hardware: a "Genre" row in the status panel (auto-fetched + a manual
"Get Genre" button) and a "Genre" row in the Disc Data tab (Custom
column is a dropdown of `pclink_protocol.GENRES`, not free text). It
took four different approaches to get the write working -- three
standalone-`Action.SET_DISC_GENRE` attempts (v1.4.0-v1.4.2) all failed
in three different ways (see `CHANGELOG.md`'s v1.4.1/v1.4.2/v1.5.0
entries for the byte-level detail); what actually works (v1.4.3, proven
on real hardware in v1.5.0) is folding genre into a `WRITE_NAME` write
instead, since `cd_textdata.html` shows `TextData`'s payload already has
its own `genre` field, and that write mechanism was already confirmed
working for names.

**v1.5.1 fixed (and CONFIRMED the fix for) a real bug found right after
v1.5.0's confirmation**: genre turns out to be a DISC-LEVEL value that
the changer sets from the `genre` byte of *every* `TextData` write, not
just the disc-name one -- so writing a plain track name (leaving the
Genre dropdown untouched) was silently resetting genre back to
"Unassigned" every time. Fixed: every name write (disc name and tracks
alike) now carries whatever genre is currently known for the disc, not
0. **Retested (slot 4): set genre to "Folk", then wrote an unrelated
track name with the dropdown left alone -- genre stayed "Folk"
throughout, no reset.** If the Disc Name Custom field is left blank when
writing a genre, the app reuses the currently-known name so it isn't
wiped out; if no name is known at all, it refuses rather than risk
blanking it.

Reading genre has worked cleanly in every session so far. See
`CHANGELOG.md`'s v1.4.1-v1.5.1 entries and README.md's "Honest gaps"
#13/#14 for the full detail.

**v1.6.0-v1.6.7** -- a Play Mode selector (`ChangeMode`) on the Control
tab, plus a "Mode Param" status row. **Partly confirmed on real
hardware.** Track, Music Type and Userfile modes switch correctly. The
changer silently ignores some mode changes, so the app logs a notice when
a mode doesn't take. **Best and Program can't be set over PC-Link at
all** (CONFIRMED v1.6.4/v1.6.6: ignored while playing, while stopped, and
with a list stored, although both work from the remote), and **neither
can any random variant** (v1.6.7), though the Random button reaches
those. So the dropdown offers only Track, Music Type and Userfile.
The userfile param is CONFIRMED as a bit (#3 -> `0x04`, v1.6.2). See
`CHANGELOG.md`'s v1.6.1/v1.6.2 entries and README "Honest gaps" #15.

**v1.7.0/v1.7.1** -- a read-only "Userfiles & Program" tab (userfile
names + which discs are in each, and the stored program). **CONFIRMED
on real hardware**; userfile names turned out to be keyed by userfile
bit, not number (fixed in v1.7.1). See `CHANGELOG.md` and README
"Honest gaps" #16.

**v1.8.0-v1.8.2** -- writing on the Userfiles & Program tab: rename a
userfile, set which userfiles a disc is in, and a program editor.
**CONFIRMED on real hardware.** Disc membership rides in a disc-name
`WRITE_NAME` (`TextData`'s `userfiles` byte, which the changer honors),
following the genre precedent. Writing a program also starts it playing
in Program mode, and leaving Program mode clears the program. Writing
an empty program clears it too, and drops a playing program back to
Track mode (v1.12.2, confirmed). See
`CHANGELOG.md`'s v1.8.1 entry and README "Honest gaps" #17.

Name writes keep a disc's userfiles too (v1.8.2, confirmed).

**v1.10.0** -- a Backup tab: export the whole library (names, genre,
userfiles, userfile names, program) to a `.json` backup or `.csv`
catalog, and restore a `.json` backup (only writes what differs,
re-reads each slot to check it). Export has run on the real changer; it
exposed two bugs, fixed in **v1.10.1**: DiscInfo reports 99 tracks for
discs not played since power-on (CONFIRMED; now treated as unknown), and a
stray track "0" (the disc name repeated) that stopped backups restoring.
**v1.10.2: export and restore CONFIRMED on real hardware.** Two
hand-edited track names were restored and verified, and a second restore
wrote nothing. See `CHANGELOG.md`'s v1.10.0-v1.10.2 entries and README
"Honest gaps" #19.

**v1.11.0** -- a Library tab: every disc in one searchable, sortable
table, with its tracks below. It comes from a changer scan (the Backup
export's slot walk), and the last scan is kept in `library_cache.json`.
It can also show a `.json` backup file offline. Play/double-click sends
`ChangeDisc`, and "Load in Disc Data Tab" loads the disc and switches
tabs. **v1.11.1: CONFIRMED on real hardware.** See README "Honest gaps"
#20 and `CHANGELOG.md` v1.11.1.

**v1.12.0** -- the Library tab can add a disc to a userfile or take it
out (the "Userfiles" menu, or a right-click on a disc). It uses the
confirmed membership write, after re-reading the slot so a stale scan
can't rename a disc. **v1.12.1: CONFIRMED on real hardware.** v1.12.1
also fills in the Userfiles & Program tab from the Library's saved scan
(marked "*") until the changer reports each disc. See README "Honest
gaps" #21.

## In progress / next up

- **A CD-Text disc in the drive answers name reads with an endless
  `LongTextData` stream (v1.12.4, real hardware).** Slot 4, format
  `0x90`; its front panel shows the disc's own CD-Text. Read with another
  disc in the drive, its stored names come back fine, and writes work
  either way. The app's handling of the stream is **confirmed**
  (v1.12.5/v1.12.6: one stream per read, cut off, the track-name read
  skipped, saved names kept). **Found (v1.12.7, two logged cases):**
  Next Track while slot 4 plays sends the changer to the next disc, and
  slot 4's stored names are replaced by its CD-Text (disc name emptied).
  Open: whether a natural track change or the remote does the same;
  then whether the app should warn before writing names to a `0x90`
  disc. The stream itself is settled (v1.12.8,
  `probe_cdtext_stream.py`): it never ends and never carries text. It's
  what asking for "track 0" (the disc title, which this disc lacks) gets.
  **v1.12.9 (confirmed):** `DataAccess`'s "unknown" byte is a track
  number; with it, the disc in the drive returns each track's full
  CD-Text title. Next: try the byte on a non-CD-Text disc and on the
  CD-Text disc out of the drive, then use it in the app. See README "Honest gaps" #23.

- **Artist name (info_type 0x02): not supported (v1.12.3, real
  hardware).** A read gets no reply, and a write's payload is refused
  with EOT. gnudb results stay "Artist / Album" in the disc name. Only
  untried case: a CD-Text disc (`probe_artist_name.py COM3 <slot>`).
  The same log led to a link fix: a frame answered with EOT now raises
  `PCLinkRejected` instead of counting as an ACK (not yet seen in the
  app on real hardware). See README "Honest gaps" #22.

- **Library userfiles and the Userfiles tab's saved-scan rows: confirmed
  (v1.12.1).**

- **Library tab: confirmed (v1.11.1).** Possible follow-ups: have
  it follow writes made on other tabs (it needs "Rescan Disc" today),
  and batch gnudb tagging, which the v1.10.0 entry mentions alongside
  it.

- **Backup tab: confirmed (v1.10.2).** Untried corners: restoring
  userfile names, restoring the program, a genre- or userfiles-only
  change, and a track-count-mismatch skip.
- Nothing open on userfiles/programs. Untried corner: writing an empty
  program (to clear it).
- **gnudb.org: live round-trip CONFIRMED** (v1.8.3 session: query ->
  inexact-match picker -> read -> write to the changer). Still open:
  - Exact DiscID matches: a known limitation, not a bug (v1.8.6).
    Every disc tried got inexact matches only. The picker is the normal
    path. v1.8.6 blamed the changer's whole-second TOC, but v1.9.2's log
    suggests our IDs are right and gnudb stores some entries under
    non-standard IDs (same checksum and length, different last byte).
    Nothing further planned.
  - ASCII folding of gnudb text: CONFIRMED (v1.8.5).
  - The 25-character disc-name warning: CONFIRMED (v1.8.5 screenshot).
  - Rate-limit handling (HTTP 403/429/503) hasn't met a real block yet.
- **Cover art after a gnudb read: CONFIRMED (v1.9.2)** (Disc Data tab,
  top right). Comes from the gnudb entry's own `# Cover:` links, with an
  iTunes search by artist/album as the fallback (`album_art.py`). Needs
  Pillow. Three discs, all the right cover, all from gnudb; the iTunes
  fallback hasn't been needed in the app yet. See README "Honest
  gaps" #18.
- **Deferred for now: a software fallback for "ALL DATA READ"** for users
  without a remote (see the caveat in `CHANGELOG.md`'s v1.2.0 entry). The
  idea, not yet implemented on purpose (explicitly held off per the
  user): physically step through all 200 slots one at a time via
  `ChangeDisc`, pausing for the changer to read each one, as a workaround
  that forces the same recataloging the menu command does. Worth
  revisiting once there's a concrete need for it.

## Files

- `pclink_protocol.py` -- pure protocol logic (framing, checksums, all
  enums, payload encode/decode). No I/O; independently testable.
- `pclink_link.py` -- serial transport, ENQ/ACK/EOT flow control.
- `pclink_app.py` -- the Tkinter GUI.
- `gnudb_client.py` -- gnudb.org HTTP client (stdlib only).
- `album_art.py` -- cover art: gnudb's own links, then iTunes (v1.9.1;
  needs Pillow).
- `library_backup.py` -- Backup tab file format and restore planning
  (v1.10.0).
- `library_browser.py` -- Library tab search, filters, sorting and the
  scan cache (v1.11.0).
- `test_library_browser.py` -- tests for the Library tab, against the
  Backup tests' simulated changer plus the user's real disc data and
  frames from the first real run; confirmed on real hardware (v1.11.1).
- `test_library_backup.py` -- tests for the Backup tab, against a
  simulated changer plus frames from real logs; export and restore
  confirmed on real hardware (v1.10.2).
- `test_write_feature.py` -- tests for the write-to-changer feature
  (names, confirmed; genre/program/userfiles encoders, round-trip only).
- `test_mode_feature.py` -- tests for the v1.6.0 Play Mode selector
  (`ChangeMode`); not yet confirmed on real hardware.
- `test_userfile_program_view.py` -- tests for the read-only Userfiles
  & Program tab; confirmed on real hardware (v1.7.1).
- `test_userfile_program_write.py` -- tests for writing userfiles and
  programs; confirmed on real hardware (v1.8.1).
- `test_gnudb_client.py` -- tests for the gnudb.org lookup (network
  mocked, plus frames from the first live session); live round-trip
  confirmed (v1.8.4).
- `test_album_art.py` -- tests for the cover art lookup (network mocked);
  confirmed in the app (v1.9.2).
- `test_genre_feature.py` -- tests for the genre read/write feature;
  confirmed against real hardware as of v1.5.0 (writes fold into a
  `WRITE_NAME` write rather than a standalone action), including a
  v1.5.1 fix (also confirmed) for a bug where writing a track name reset
  genre -- see `CHANGELOG.md`.
- `protocol_reference/` -- the original source documentation this whole
  project was built from (`https://juken.sourceforge.net/protocol/`,
  saved copies of the CD-changer-relevant pages; the DVD-changer pages and
  page-layout scaffolding weren't included, they're not relevant here).
  `pclink_protocol.py`'s comments paraphrase these, but for anything
  involving the write-side commands (`cd_dataaccess.html`,
  `cd_readyfordata.html`, `cd_discgenre.html`, `cd_disclisting.html`,
  `cd_discuserfiles.html`, `cd_textdata.html`/`cd_longtextdata.html`) --
  the next planned feature -- it's worth reading the originals directly
  rather than only the paraphrase, especially since the docs don't
  actually specify the write-side choreography (e.g. whether the changer
  requests the data via `ReadyForData` before accepting a write, or the PC
  just sends it) -- that'll need figuring out against real hardware either
  way, same as everything else so far.
  Also in there, locally only (gitignored, not committed, because it's
  Kenwood's copyrighted manual): `KENWOOD_CD-425M_instruction_manual.pdf`,
  Kenwood's own owner's manual. It says nothing about the serial protocol, but it's the
  reference for what each front-panel/remote feature does (play modes,
  Best Selection, programs, user files, title length limits, ALL DATA
  READ) -- see README's "Owner's manual notes".
- `README.md` -- full documentation, setup, protocol summary, and an
  "Honest gaps" section listing every place where behavior was inferred
  vs. confirmed against real hardware.
- `CHANGELOG.md` -- chronological history of every hardware-confirmed fix,
  with the reasoning behind each one. Written specifically so a fresh
  conversation (or a fresh person) can understand *why* the code does
  several non-obvious things, not just *what* it does.

## Working style established on this project (worth continuing)

- **Nothing gets marked "confirmed" without real hardware evidence.**
  Several early assumptions (reply framing, EOT semantics, BCD encoding of
  TOC timecodes, track-numbering offsets) turned out to be wrong when
  tested against the actual CD-425M, sometimes more than once in different
  directions. The `README.md` "Honest gaps" section and `CHANGELOG.md`
  exist to keep this distinction clear and prevent re-breaking something
  that was already fixed once.
- **Every fix gets a test before being handed back**, even without access
  to the real hardware -- using synthetic/mocked data built from the
  user's actual logged bytes wherever possible, not just abstract unit
  tests.
- **The user tests against real hardware and reports back with raw logs**
  ("show raw bytes" is a toggle in the app's log console) -- those logs
  are how most of the real protocol quirks in `CHANGELOG.md` were
  actually discovered, often contradicting the source protocol
  documentation (`https://juken.sourceforge.net/protocol/`, which is
  itself a community reverse-engineering effort and says so).
