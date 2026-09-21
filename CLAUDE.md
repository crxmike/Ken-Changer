# CLAUDE.md

Read `START_HERE.md` first, then `README.md` and `CHANGELOG.md`, before
making any changes. This file is the Claude Code-specific supplement to
those three -- it won't repeat their content, just point to it and record
the working rules that need to survive the move from claude.ai Projects.

## Hard rule: hardware confirmation

**Never mark something as hardware-confirmed without the user actually
testing it against the real CD-425M.** This is the single most important
rule carried over from this project's prior work. Concretely:

- If you fix something, write a test for it (synthetic/mocked data built
  from the user's actual logged bytes wherever possible -- see existing
  tests for the pattern), but do NOT describe the fix as "confirmed" or
  "working" in `README.md` / `CHANGELOG.md` until the user reports back
  that it worked on real hardware.
- Language matters: "should fix X, per the logs you shared" is correct
  before testing; "fixed" / "confirmed" / "working" is only correct after.
- When the user reports back, ask for or use the raw byte log ("show raw
  bytes" toggle in the app's log console) -- that's how most of the real
  protocol quirks so far were actually discovered, and several early
  assumptions (reply framing, EOT semantics, BCD timecodes, track-number
  offsets) turned out wrong, sometimes more than once in different
  directions. Don't re-trust a plausible-sounding assumption just because
  it matches the source docs -- the source docs are themselves a
  community reverse-engineering effort and say so.
- Update `CHANGELOG.md` (newest first, with the reasoning) and README's
  "Honest gaps" section once something is actually confirmed or
  reverted -- that history is what lets a fresh session understand *why*
  the code does non-obvious things, not just what it does.

## Current status / where to pick up

See `START_HERE.md`'s "Current status" and "In progress / next up"
sections for the live state -- don't duplicate it here since it'll go
stale. As of the last update: v1.2.0+ (read/control, TOC/DiscID, Disc Map
all hardware-confirmed); gnudb.org querying wired up but live round-trip
unconfirmed; writing to the changer (`DataAccess` + `WRITE_NAME` /
`SET_DISC_GENRE` / `WRITE_PROGRAM` / `SET_USERFILES`) is the next planned
feature.

## Files

- `pclink_protocol.py` -- pure protocol logic, no I/O, independently
  testable.
- `pclink_link.py` -- serial transport, ENQ/ACK/EOT flow control.
- `pclink_app.py` -- the Tkinter GUI.
- `gnudb_client.py` -- gnudb.org HTTP client (stdlib only).
- `test_write_feature.py` -- tests for the in-progress write feature.
- `test_genre_feature.py` -- tests for the genre read/write feature
  (v1.4.0).
- `protocol_reference/` -- original source docs
  (`https://juken.sourceforge.net/protocol/`) this project was built
  from. For anything touching write-side commands
  (`cd_dataaccess.html`, `cd_readyfordata.html`, `cd_discgenre.html`,
  `cd_disclisting.html`, `cd_discuserfiles.html`, `cd_textdata.html` /
  `cd_longtextdata.html`), read these directly rather than relying only
  on the paraphrase in `pclink_protocol.py`'s comments -- the docs don't
  specify the write-side choreography (e.g. whether the changer requests
  data via `ReadyForData` before accepting a write, or the PC just sends
  it), so that'll need figuring out against real hardware regardless.

## Working style

- Every fix gets a test before being handed back, even without hardware
  access -- see existing tests for the mocked-data pattern.
- The user tests against real hardware and reports back with raw logs;
  that's the only path to "confirmed."
- Keep `README.md`'s "Honest gaps" section and `CHANGELOG.md` in sync
  with reality as things get confirmed, revert, or newly discovered --
  they're what let a fresh conversation understand the non-obvious
  reasoning without re-deriving it.
