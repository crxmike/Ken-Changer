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
gnudb.org querying is wired up but its live round-trip is still
unconfirmed -- see "In progress" below. See `CHANGELOG.md` for the full
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

**v1.6.0-v1.6.2** -- a Play Mode selector (`ChangeMode`) on the Control
tab, plus a "Mode Param" status row. **Partly confirmed on real
hardware.** Music Type and Userfile modes switch correctly. The changer
silently ignores a mode it can't enter (seen with Best, and Program with
nothing stored), so the app logs a notice when a mode doesn't take.
The userfile param is CONFIRMED as a bit (#3 -> `0x04`, v1.6.2). See
`CHANGELOG.md`'s v1.6.1/v1.6.2 entries and README "Honest gaps" #15.

## In progress / next up

- **Play Mode selector leftovers.** Play a non-Rock disc to check
  whether `InfoEvent`'s `num_tracks` byte is really the disc's genre,
  and find out whether Best mode needs something stored first.
- **`WRITE_PROGRAM` / `SET_USERFILES`** share `send_write()`'s plumbing
  with the now-confirmed `WRITE_NAME` path but have no UI yet, and their
  encoders are untested against real hardware beyond round-trip
  self-consistency (`test_write_feature.py`). The now-confirmed genre
  write (piggybacking on `WRITE_NAME` rather than a standalone action)
  is a useful precedent if `WRITE_PROGRAM`/`SET_USERFILES` run into
  similar trouble -- worth checking whether either of those also has a
  simpler existing mechanism it could ride along with, before assuming
  the standalone action is the right path.
- **gnudb.org querying just got wired up** (query -> read -> populate the
  Disc Data tab's "From gnudb.org" column) but hasn't been confirmed
  against a live server response yet -- the user's IP got rate-limited by
  gnudb.org during testing and is waiting for that to clear. The parsing
  logic itself IS verified, against gnudb.org's own documented example
  responses (see `gnudb_client.py` and the tests described in
  `CHANGELOG.md`) -- what's unverified is only the live round-trip.
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
- `test_write_feature.py` -- tests for the write-to-changer feature
  (names, confirmed; genre/program/userfiles encoders, round-trip only).
- `test_mode_feature.py` -- tests for the v1.6.0 Play Mode selector
  (`ChangeMode`); not yet confirmed on real hardware.
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
