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
stale. As of the last update: v1.10.2 (names, genre, reading and
writing userfiles/program, the gnudb.org lookup, gnudb cover art, and
the Backup tab's export/restore all confirmed; exact gnudb DiscID
matches don't happen on this changer, most likely because gnudb stores
those entries under non-standard IDs -- see CHANGELOG v1.9.2; DiscInfo
reports 99 tracks for discs not played since power-on -- see CHANGELOG
v1.10.1). v1.11.1: the Library tab (browse/search/play every disc) is
confirmed too, and so is v1.12.0's userfile add/remove there (v1.12.1).
v1.12.2: writing an empty program (clearing it) is confirmed.

## Files

- `pclink_protocol.py` -- pure protocol logic, no I/O, independently
  testable.
- `pclink_link.py` -- serial transport, ENQ/ACK/EOT flow control.
- `pclink_app.py` -- the Tkinter GUI.
- `gnudb_client.py` -- gnudb.org HTTP client (stdlib only).
- `album_art.py` -- cover art: gnudb's own `# Cover:` links, then an
  iTunes search (v1.9.1; needs Pillow).
- `library_backup.py` -- Backup tab: backup file format and restore
  planning (v1.10.0).
- `library_browser.py` -- Library tab: search, filters, sorting and the
  last-scan cache, `library_cache.json` (v1.11.0).
- `test_write_feature.py` -- tests for the in-progress write feature.
- `test_genre_feature.py` -- tests for the genre read/write feature
  (v1.4.0).
- `test_mode_feature.py` -- tests for the Play Mode selector
  (`ChangeMode`, v1.6.0).
- `test_userfile_program_view.py` -- tests for the read-only Userfiles &
  Program tab (v1.7.0).
- `test_userfile_program_write.py` -- tests for writing userfiles and
  programs (v1.8.0).
- `test_gnudb_client.py` -- tests for the gnudb.org lookup, network
  mocked (v1.8.3).
- `test_album_art.py` -- tests for the cover art lookup, network mocked
  (v1.9.0).
- `test_library_backup.py` -- tests for library backup/restore against a
  simulated changer (v1.10.0).
- `test_library_browser.py` -- tests for the Library tab (v1.11.0;
  confirmed on real hardware in v1.11.1).
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
- `protocol_reference/KENWOOD_CD-425M_instruction_manual.pdf` -- Kenwood's
  own owner's manual (covers CD-4700M/CD-4260M/CD-425M/DPF-J6030).
  Gitignored and not committed, since it's Kenwood's copyrighted manual,
  so it only exists in the user's local copy. README's "Owner's manual
  notes" summarizes what matters if it's missing. It doesn't document the serial protocol, but it's the authority on what
  the changer's features *do* from the front panel/remote: play modes,
  Best Selection, programs, user files, title limits and ALL DATA READ.
  Check it before guessing at user-visible behavior.

## Working style

- Every fix gets a test before being handed back, even without hardware
  access -- see existing tests for the mocked-data pattern.
- The user tests against real hardware and reports back with raw logs;
  that's the only path to "confirmed."
- Keep `README.md`'s "Honest gaps" section and `CHANGELOG.md` in sync
  with reality as things get confirmed, revert, or newly discovered --
  they're what let a fresh conversation understand the non-obvious
  reasoning without re-deriving it.
