#!/usr/bin/env python3
"""
test_genre_feature.py
======================
Tests for reading/writing DiscGenre (v1.4.0-v1.5.1): the status panel's
"Genre" row, the Disc Data tab's Genre row (with its Custom-column
dropdown), and getting the selected genre out to the changer.

Stdlib-only (unittest), matching the rest of this project. Per this
project's established practice ("every fix gets a test before being
handed back, even without hardware access"): these confirm the new code
is internally self-consistent. proto.GENRES/encode_disc_genre/
decode_disc_genre were already covered by test_write_feature.py's
round-trip test before any UI existed for them; what's tested here is
the app-layer glue (gather_genre_write_item, GENRE_NAME_TO_CODE,
merge_genre_into_write_items) that the Disc Data tab's Genre row and
"Write to Changer" button use.

History (2026-09-21, slot 2) -- three real-hardware attempts at a
STANDALONE Action.SET_DISC_GENRE write all failed, each a different way:
  - v1.4.0: forgot to put the target genre in the initiating DataAccess
    request's own `genre` field at all -- fixed in v1.4.1 (see
    TestBuildGenreWriteFrames).
  - v1.4.1 (bug fixed): the follow-up DiscGenre frame (send_write()'s
    two-transaction choreography, confirmed for WRITE_NAME) got rejected
    with an immediate EOT instead of ACK/NAK.
  - v1.4.2 (no follow-up frame at all, on the theory the changer didn't
    want one): the transaction closed cleanly, but of course nothing was
    written, since ReadyForData means "send the payload now" and nothing
    was sent.

v1.4.3 changed approach entirely: cd_textdata.html shows TextData's own
payload (the one WRITE_NAME already uses, CONFIRMED working) has its own
`genre` field alongside `text`. Genre now rides along with a disc-name
WRITE_NAME write instead of going out via the standalone
Action.SET_DISC_GENRE path -- see TestMergeGenreIntoWriteItems and
TestGenreWriteWorkerAttachesGenre.

**v1.5.0: this approach is CONFIRMED against real hardware** -- writing
"Rock" to slot 2 (disc name left blank in the Custom column, so the app
reused the currently-known name) went out exactly like an ordinary
already-confirmed WRITE_NAME write, and an independent re-read afterward
showed genre=23/"Rock" for the disc name and every track. See
CHANGELOG.md's v1.5.0 entry for the full raw-byte detail. Reading genre
(DataAccess(RETRIEVE_DATA, DiscGenre) -> a single CMD_DISC_GENRE reply)
has also now been exercised successfully many times across every one of
these sessions.

**v1.5.1 bug, found and fixed, CONFIRMED against real hardware**: right
after v1.5.0's success, writing a plain track name (Genre dropdown left
untouched) silently reset the just-set genre back to "Unassigned" --
genre turned out to be a disc-level value that EVERY TextData write
sets, not just the disc-name one. Fixed in merge_genre_into_write_items()
(every item in a write batch now carries the same genre byte -- see
TestMergeGenreIntoWriteItems) and confirmed fixed in a follow-up
real-hardware session: setting genre to "Folk" and then writing a track
name in the same session left genre at "Folk" throughout, instead of
resetting to "Unassigned".
"""

from __future__ import annotations

import sys
import types
import unittest

if "serial" not in sys.modules:
    fake_serial_module = types.ModuleType("serial")
    fake_serial_module.EIGHTBITS = 8
    fake_serial_module.PARITY_NONE = "N"
    fake_serial_module.STOPBITS_TWO = 2

    class _UnusedSerial:
        def __init__(self, *a, **k):
            raise AssertionError("real serial.Serial should never be constructed in tests")

    fake_serial_module.Serial = _UnusedSerial
    sys.modules["serial"] = fake_serial_module

import pclink_protocol as proto
import pclink_app as app_mod
from pclink_app import build_genre_write_frames, gather_genre_write_item, merge_genre_into_write_items


class TestGenreNameToCode(unittest.TestCase):
    def test_reverse_map_covers_every_genre(self):
        self.assertEqual(len(proto.GENRE_NAME_TO_CODE), len(proto.GENRES))
        for code, name in proto.GENRES.items():
            self.assertEqual(proto.GENRE_NAME_TO_CODE[name], code)

    def test_known_values(self):
        self.assertEqual(proto.GENRE_NAME_TO_CODE["Rock"], 0x17)
        self.assertEqual(proto.GENRE_NAME_TO_CODE["Unassigned"], 0x00)
        self.assertEqual(proto.GENRE_NAME_TO_CODE["World Music"], 0x1C)


class TestGatherGenreWriteItem(unittest.TestCase):
    def test_blank_selection_means_dont_write(self):
        self.assertIsNone(gather_genre_write_item(""))
        self.assertIsNone(gather_genre_write_item("   "))
        self.assertIsNone(gather_genre_write_item(None))

    def test_valid_genre_name_maps_to_its_code(self):
        self.assertEqual(gather_genre_write_item("Rock"), 0x17)
        self.assertEqual(gather_genre_write_item("Classical"), 0x05)

    def test_unassigned_is_a_real_choice_not_treated_as_blank(self):
        # 0x00 is a legitimate genre value ("Unassigned") -- distinct from
        # an empty selection, which means "don't write a genre at all".
        self.assertEqual(gather_genre_write_item("Unassigned"), 0x00)

    def test_unrecognized_text_returns_none(self):
        self.assertIsNone(gather_genre_write_item("Not A Real Genre"))

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(gather_genre_write_item("  Jazz  "), 0x0E)


class TestBuildGenreWriteFrames(unittest.TestCase):
    """Regression test for a real bug caught via real-hardware testing
    (2026-09-21 session, slot 2, CD-425M): _write_to_changer_worker built
    the initiating DataAccess(SET_DISC_GENRE) request without passing
    `genre=genre_code` into encode_data_access, so it always went out
    with genre=0 ("Unassigned") no matter what was selected in the
    dropdown -- the raw log showed the request frame's genre byte as
    0x00 while the follow-up DiscGenre frame correctly carried 0x0D (Hip
    Hop). See CHANGELOG.md's v1.4.1 entry."""

    def test_request_frame_carries_the_target_genre(self):
        request_data, _ = build_genre_write_frames(slot=2, genre_code=0x0D)
        # encode_data_access's payload is
        # <BBHBBB> = action, data_type, slot_lo, slot_hi, unknown, info_type, genre
        self.assertEqual(request_data[0], proto.Action.SET_DISC_GENRE)
        self.assertEqual(request_data[1], proto.DataType.DISC_GENRE)
        self.assertEqual(request_data[6], 0x0D, "request_data's genre byte must match genre_code")

    def test_follow_up_frame_also_carries_the_target_genre(self):
        _, follow_up_data = build_genre_write_frames(slot=2, genre_code=0x0D)
        decoded = proto.decode_disc_genre(follow_up_data)
        self.assertEqual(decoded["slot"], 2)
        self.assertEqual(decoded["genre"], 0x0D)

    def test_matches_the_exact_bytes_from_the_real_hardware_session(self):
        # Anchors to the corrected version of the exact request seen on
        # the wire in the 2026-09-21 session (slot 2, genre 0x0D/Hip Hop)
        # -- that session's actual bytes had genre=0x00 at this position
        # (the bug); this confirms the fix produces 0x0D instead, with
        # everything else in the frame unchanged.
        request_data, follow_up_data = build_genre_write_frames(slot=2, genre_code=0x0D)
        request_frame = proto.encode_frame(proto.CMD_DATA_ACCESS, request_data)
        follow_up_frame = proto.encode_frame(proto.CMD_DISC_GENRE, follow_up_data)
        # Byte-for-byte identical to what was observed on the wire except
        # the genre byte (the payload's last byte) and the trailing
        # checksum, which changes because the genre byte changed. The
        # real session's (buggy) request frame was
        # 0203070010100200000000d4 -- genre byte 0x00 where 0x0d now
        # appears, and checksum d4 where c7 now appears.
        self.assertEqual(request_frame.hex(), "020307001010020000000dc7")
        self.assertEqual(follow_up_frame.hex(), "0208030002000de6")


class _FakeLink:
    """Stand-in for PCLinkConnection, recording calls instead of touching
    a real (or even fake) serial port -- _write_to_changer_worker only
    ever calls .send()/.send_write() on whatever link it's given, so this
    is enough to observe which one it picks for the genre write."""

    def __init__(self):
        self.send_calls = []
        self.send_write_calls = []

    def send(self, command, data, timeout=5.0):
        self.send_calls.append((command, data))

    def send_write(self, command, data, follow_up_command, follow_up_data, timeout=8.0):
        self.send_write_calls.append((command, data, follow_up_command, follow_up_data))


class TestMergeGenreIntoWriteItems(unittest.TestCase):
    """v1.4.3: genre now rides along with WRITE_NAME writes (see the
    module docstring for why) instead of a standalone
    Action.SET_DISC_GENRE write. merge_genre_into_write_items() is the
    pure function that decides how.

    v1.5.1 regression tests (real-hardware bug, 2026-09-21): genre turns
    out to be a DISC-LEVEL value that EVERY TextData write sets, not just
    the one it's attached to -- writing a track name with its follow-up
    frame's genre byte left at the old default of 0 silently reset a
    just-written "Rock" genre back to "Unassigned". So every item in the
    final list must carry the SAME genre byte -- the newly selected one,
    or (when no new genre was selected) the previously-known one, so an
    ordinary name-only write can't accidentally erase an existing genre."""

    def test_no_genre_selected_every_item_gets_the_current_known_genre(self):
        # Not 0 -- see test_track_only_write_preserves_existing_genre for
        # why defaulting to 0 here was the actual v1.5.0 bug.
        items = [
            (0, "Album X", proto.InfoType.DISC_NAMES, "Disc Name"),
            (1, "Track One", proto.InfoType.TRACK_NAMES, "Track 1"),
        ]
        final, error = merge_genre_into_write_items(items, None, "Old Name", 0x17)
        self.assertIsNone(error)
        self.assertEqual(
            final,
            [
                (0, "Album X", proto.InfoType.DISC_NAMES, "Disc Name", 0x17),
                (1, "Track One", proto.InfoType.TRACK_NAMES, "Track 1", 0x17),
            ],
        )

    def test_no_genre_selected_and_none_known_defaults_to_zero(self):
        items = [(0, "Album X", proto.InfoType.DISC_NAMES, "Disc Name")]
        final, error = merge_genre_into_write_items(items, None, "Old Name", None)
        self.assertIsNone(error)
        self.assertEqual(final, [(0, "Album X", proto.InfoType.DISC_NAMES, "Disc Name", 0)])

    def test_track_only_write_preserves_existing_genre(self):
        # The actual v1.5.1 bug, reproduced: writing only a track name
        # (no genre selected in the dropdown) must NOT reset a disc's
        # already-known genre back to 0/Unassigned.
        items = [(1, "gift shopp", proto.InfoType.TRACK_NAMES, "Track 1")]
        final, error = merge_genre_into_write_items(items, None, "The Hip", 0x17)
        self.assertIsNone(error)
        self.assertEqual(
            final, [(1, "gift shopp", proto.InfoType.TRACK_NAMES, "Track 1", 0x17)]
        )

    def test_genre_selected_attaches_to_every_item_not_just_disc_name(self):
        items = [
            (0, "Album X", proto.InfoType.DISC_NAMES, "Disc Name"),
            (1, "Track One", proto.InfoType.TRACK_NAMES, "Track 1"),
        ]
        final, error = merge_genre_into_write_items(items, 0x17, "Old Name", 0x00)
        self.assertIsNone(error)
        self.assertEqual(
            final,
            [
                (0, "Album X", proto.InfoType.DISC_NAMES, "Disc Name", 0x17),
                (1, "Track One", proto.InfoType.TRACK_NAMES, "Track 1", 0x17),
            ],
        )

    def test_genre_with_no_disc_name_item_falls_back_to_current_name(self):
        # Custom column's Disc Name row was left blank, but the app
        # already knows the current disc name from a prior read -- reuse
        # it rather than risk blanking the name.
        items = [(1, "Track One", proto.InfoType.TRACK_NAMES, "Track 1")]
        final, error = merge_genre_into_write_items(items, 0x0D, "Existing Name", 0x00)
        self.assertIsNone(error)
        self.assertEqual(
            final,
            [
                (0, "Existing Name", proto.InfoType.DISC_NAMES, "Disc Name (genre only)", 0x0D),
                (1, "Track One", proto.InfoType.TRACK_NAMES, "Track 1", 0x0D),
            ],
        )

    def test_genre_with_no_disc_name_item_and_no_known_name_refuses(self):
        items = [(1, "Track One", proto.InfoType.TRACK_NAMES, "Track 1")]
        final, error = merge_genre_into_write_items(items, 0x0D, None, 0x00)
        self.assertIsNotNone(error)
        # The track item should still be reported so the caller can
        # choose to proceed with just that, dropping only the genre part
        # -- but it should still carry the requested genre code (the
        # caller falls back to a None genre_code separately if it wants
        # to drop the genre change entirely; this function's job is just
        # to say "can't attach a NEW genre without a disc name").
        self.assertEqual(final, [(1, "Track One", proto.InfoType.TRACK_NAMES, "Track 1", 0x0D)])

    def test_genre_with_no_disc_name_item_and_empty_string_name_refuses(self):
        # An empty string (vs. None) must be treated the same as "no name
        # known" -- both are falsy and neither is safe to write.
        items = []
        final, error = merge_genre_into_write_items(items, 0x0D, "", 0x00)
        self.assertIsNotNone(error)
        self.assertEqual(final, [])


class TestGenreWriteWorkerAttachesGenre(unittest.TestCase):
    """Verifies _write_to_changer_worker actually threads the per-item
    genre through to encode_text_data's follow-up frame -- i.e. that the
    wiring between merge_genre_into_write_items()'s output and the real
    send_write() call is correct, not just that the pure function itself
    computes the right tuples."""

    def setUp(self):
        self.app = app_mod.App(library_cache_path=None)
        self.link = _FakeLink()
        # Re-reads after a successful write happen via self._send_bg (its
        # own background thread) -- irrelevant to what this test checks
        # and racy to observe from the test thread, so stub it out.
        self.app._send_bg = lambda *a, **kw: None

    def tearDown(self):
        self.app.destroy()

    def test_disc_name_and_track_writes_both_carry_the_items_genre(self):
        # Realistic input from merge_genre_into_write_items(): every item
        # shares the same genre byte (see its v1.5.1 fix -- a track item
        # with genre=0 here would reset the disc's genre on real hardware,
        # so the worker must faithfully forward whatever genre each item
        # was given, not silently zero out non-disc-name items itself).
        items = [
            (0, "Album X", proto.InfoType.DISC_NAMES, "Disc Name", 0x17),
            (1, "Track One", proto.InfoType.TRACK_NAMES, "Track 1", 0x17),
        ]
        self.app._write_to_changer_worker(self.link, slot=2, items=items)

        self.assertEqual(len(self.link.send_write_calls), 2)
        disc_name_call = self.link.send_write_calls[0]
        command, request_data, follow_up_command, follow_up_data = disc_name_call
        self.assertEqual(command, proto.CMD_DATA_ACCESS)
        self.assertEqual(follow_up_command, proto.CMD_TEXT_DATA)
        decoded = proto.decode_text_data(follow_up_data)
        self.assertEqual(decoded["text"], "Album X")
        self.assertEqual(decoded["genre"], 0x17)

        track_call = self.link.send_write_calls[1]
        decoded_track = proto.decode_text_data(track_call[3])
        self.assertEqual(decoded_track["text"], "Track One")
        self.assertEqual(
            decoded_track["genre"], 0x17,
            "the worker must forward each item's own genre byte as-is, "
            "not zero it out for track items",
        )

        # No standalone SET_DISC_GENRE/DiscGenre write should be sent at all.
        for _cmd, data, *_rest in self.link.send_write_calls:
            action = data[0]
            self.assertNotEqual(action, proto.Action.SET_DISC_GENRE)
        self.assertEqual(self.link.send_calls, [])


class TestSetDiscGenreWriteActionWiring(unittest.TestCase):
    """Sanity checks on the pieces _write_to_changer_worker composes for
    the Genre row -- that DataType.DISC_GENRE's reply command really is
    CMD_DISC_GENRE (used both as the read-side reply and as send_write()'s
    follow_up_command for the write), and that a full request/follow-up
    frame pair encodes and decodes consistently, the same sanity check
    test_write_feature.py already does for WRITE_NAME."""

    def test_disc_genre_is_its_own_reply_command(self):
        self.assertEqual(
            proto.DATA_TYPE_TO_REPLY_COMMAND[proto.DataType.DISC_GENRE],
            proto.CMD_DISC_GENRE,
        )
        self.assertEqual(
            proto.WRITE_ACTION_DATA_TYPE[proto.Action.SET_DISC_GENRE],
            proto.DataType.DISC_GENRE,
        )

    def test_request_and_follow_up_frames_round_trip(self):
        slot = 9
        genre_code = proto.GENRE_NAME_TO_CODE["Rock"]
        request_data = proto.encode_data_access(
            proto.Action.SET_DISC_GENRE, proto.DataType.DISC_GENRE, slot=slot,
        )
        follow_up_data = proto.encode_disc_genre(slot=slot, genre=genre_code)

        request_frame = proto.encode_frame(proto.CMD_DATA_ACCESS, request_data)
        follow_up_frame = proto.encode_frame(proto.CMD_DISC_GENRE, follow_up_data)

        decoded_follow_up = proto.decode_disc_genre(follow_up_data)
        self.assertEqual(decoded_follow_up["slot"], slot)
        self.assertEqual(decoded_follow_up["genre"], genre_code)
        self.assertEqual(decoded_follow_up["genre_name"], "Rock")

        # Both frames should carry a valid checksum, same sanity check
        # test_write_feature.py does for the WRITE_NAME write path.
        for frame_bytes in (request_frame, follow_up_frame):
            command = frame_bytes[1]
            length = frame_bytes[2] | (frame_bytes[3] << 8)
            data = frame_bytes[4 : 4 + length]
            checksum = frame_bytes[4 + length]
            self.assertEqual(proto.compute_checksum(command, data), checksum)


if __name__ == "__main__":
    unittest.main(verbosity=2)
