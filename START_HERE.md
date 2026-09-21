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

## In progress / next up

- **`SET_DISC_GENRE` / `WRITE_PROGRAM` / `SET_USERFILES`** share
  `send_write()`'s plumbing with the now-confirmed `WRITE_NAME` path but
  have no UI yet, and their encoders are untested against real hardware
  beyond round-trip self-consistency (`test_write_feature.py`). Natural
  next step now that `WRITE_NAME`'s choreography and the UI pattern for
  it are both proven out.
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
