#!/usr/bin/env python3
"""
test_userfile_program_write.py
==============================
Tests for writing userfiles and programs from the Userfiles & Program tab
(v1.8.0). CONFIRMED on real hardware (v1.8.1, 2026-09-23 --
TestRealWriteSession): userfile renames, disc membership via TextData's
userfiles byte, and program writes all read back exactly as written.
v1.8.2: a track-name write on a disc in #1-#3 kept its mask (0x07).

Stdlib-only (unittest), matching the rest of this project.

What's being tested:
  - Renaming a userfile: WRITE_NAME with slot 0, info_type 7 and index =
    the userfile's BIT. The follow-up TextData frame is byte-identical to
    the changer's own read reply for the same name (TestUserfileNameWrite).
  - Setting a disc's userfiles: re-sending the disc name with the new
    mask in TextData's userfiles byte, keeping the disc's genre
    (TestMembershipPlan, TestWorkerCarriesUserfiles).
  - Every plain name write now carries the disc's KNOWN mask instead of 0,
    reading genre/userfiles first if they aren't known
    (TestNameWriteKeepsUserfiles).
  - Program: WRITE_PROGRAM + a DiscListing follow-up identical to the
    changer's own program read reply (TestProgramWrite), plus the editor
    (TestProgramEditor).
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

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
from pclink_app import (
    build_program_write, build_userfile_name_write, missing_disc_state,
    plan_userfile_membership_write, program_items_from_steps, program_steps_from_items,
    userfile_flags_from_mask, userfile_mask_from_flags, validate_title,
)


def _data(frame_hex):
    """Payload bytes of a logged frame (strip STX, cmd, 2 length bytes, checksum)."""
    return bytes.fromhex(frame_hex)[4:-1]


# Real frames from the v1.7.1 session (2026-09-22), see test_userfile_program_view.py.
USERFILE_1_NAME_FRAME = "02 fe 10 00 00 00 01 00 07 00 00 66 55 43 4b 20 73 48 49 54 29"
PROGRAM_FRAME = ("02 0d 22 00 0b 01 00 01 01 00 01 01 00 02 01 00 05 01 00 08 01 00 04"
                 " 01 00 02 01 00 05 01 00 03 01 00 06 01 00 09 8d")
PROGRAM_STEPS = [(1, 1), (1, 1), (1, 2), (1, 5), (1, 8), (1, 4),
                 (1, 2), (1, 5), (1, 3), (1, 6), (1, 9)]


class _FakeLink:
    def __init__(self, on_send=None):
        self.send_calls = []
        self.send_write_calls = []
        self.on_send = on_send

    def send(self, command, data, timeout=5.0):
        self.send_calls.append((command, data))
        if self.on_send:
            self.on_send(command, data)

    def send_write(self, command, data, follow_up_command, follow_up_data, timeout=8.0):
        self.send_write_calls.append((command, data, follow_up_command, follow_up_data))


class _FakeThread:
    """Records what a threading.Thread would have run, without running it."""
    started = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        _FakeThread.started.append(self)


class TestValidateTitle(unittest.TestCase):
    def test_limits(self):
        self.assertEqual(validate_title("  Road Trip  ", 25), "Road Trip")
        self.assertEqual(validate_title("x" * 25, 25), "x" * 25)
        for bad in ("", "   ", "x" * 26, "Café", "tab\there"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_title(bad, 25)


class TestUserfileNameWrite(unittest.TestCase):
    def test_request_is_write_name_slot0_info_type_7(self):
        request, _ = build_userfile_name_write(3, "Road Trip")
        self.assertEqual(request, bytes([0x80, 0x01, 0x00, 0x00, 0x00, 0x07, 0x00]))

    def test_follow_up_is_byte_identical_to_the_changers_own_reply(self):
        # Writing userfile #1's current name should produce exactly the
        # TextData frame the changer sent back when we read it.
        _, follow_up = build_userfile_name_write(1, "fUCK sHIT")
        self.assertEqual(proto.encode_frame(proto.CMD_TEXT_DATA, follow_up),
                         bytes.fromhex(USERFILE_1_NAME_FRAME))

    def test_index_is_the_userfile_bit_not_its_number(self):
        for number in range(1, 9):
            _, follow_up = build_userfile_name_write(number, "N")
            p = proto.decode_text_data(follow_up)
            self.assertEqual(p["index"], 1 << (number - 1))
            self.assertEqual((p["slot"], p["userfiles"], p["info_type"], p["genre"]), (0, 0, 7, 0))

    def test_rejects_bad_number_or_name(self):
        for number, name in ((0, "A"), (9, "A"), (1, ""), (1, "x" * 26)):
            with self.assertRaises(ValueError):
                build_userfile_name_write(number, name)


class TestMaskHelpers(unittest.TestCase):
    def test_round_trip(self):
        for mask in (0x00, 0x01, 0x04, 0x81, 0xFF):
            self.assertEqual(userfile_mask_from_flags(userfile_flags_from_mask(mask)), mask)

    def test_number_3_is_bit_0x04(self):
        # CONFIRMED v1.6.2: userfile #3 is 0x04.
        self.assertEqual(userfile_mask_from_flags([False, False, True]), 0x04)


class TestMembershipPlan(unittest.TestCase):
    def test_resends_disc_name_with_its_genre(self):
        items, error = plan_userfile_membership_write("Limblifter-Bellaclava", 0x17)
        self.assertIsNone(error)
        self.assertEqual(items, [(0, "Limblifter-Bellaclava", proto.InfoType.DISC_NAMES,
                                  "Disc Name (userfiles only)", 0x17)])

    def test_unassigned_genre_is_known_not_missing(self):
        items, error = plan_userfile_membership_write("X", 0)
        self.assertIsNone(error)
        self.assertEqual(items[0][4], 0)

    def test_refuses_without_a_name_or_genre(self):
        for name, genre in ((None, 0x17), ("", 0x17), ("X", None)):
            items, error = plan_userfile_membership_write(name, genre)
            self.assertEqual(items, [])
            self.assertIsNotNone(error)


class TestMissingDiscState(unittest.TestCase):
    def test_reports_what_isnt_cached(self):
        self.assertEqual(missing_disc_state(2, {}, {}, {}), ["genre", "userfiles"])
        self.assertEqual(missing_disc_state(2, {}, {}, {}, need_name=True),
                         ["disc name", "genre", "userfiles"])
        # A known-empty name ("" = no title stored) counts as read.
        self.assertEqual(missing_disc_state(2, {2: ""}, {2: 0}, {2: 0}, need_name=True), [])


class TestProgramWrite(unittest.TestCase):
    def test_request_frame(self):
        request, _ = build_program_write([(1, 1)])
        self.assertEqual(proto.encode_frame(proto.CMD_DATA_ACCESS, request),
                         bytes.fromhex("02 03 07 00 20 20 00 00 00 00 00 b6"))

    def test_follow_up_is_byte_identical_to_the_changers_program_reply(self):
        _, follow_up = build_program_write(PROGRAM_STEPS)
        self.assertEqual(proto.encode_frame(proto.CMD_DISC_LISTING, follow_up),
                         bytes.fromhex(PROGRAM_FRAME))

    def test_all_tracks_and_empty(self):
        _, follow_up = build_program_write([(7, proto.LISTING_ALL_TRACKS)])
        self.assertEqual(follow_up, bytes([1, 7, 0, 0xAA]))
        _, follow_up = build_program_write([])
        self.assertEqual(follow_up, bytes([0]))

    def test_limits(self):
        build_program_write([(200, 99)] * 32)
        for steps in ([(1, 1)] * 33, [(0, 1)], [(201, 1)], [(1, 0)], [(1, 100)]):
            with self.assertRaises(ValueError, msg=repr(steps[:1])):
                build_program_write(steps)

    def test_steps_and_items_round_trip(self):
        items = proto.decode_disc_listing(_data(PROGRAM_FRAME))["items"]
        steps = program_steps_from_items(items)
        self.assertEqual(steps, PROGRAM_STEPS)
        self.assertEqual(program_items_from_steps(steps), items)


class _AppTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app_mod.App(library_cache_path=None)
        self.app._send_bg = lambda *a, **kw: None
        _FakeThread.started = []

    def tearDown(self):
        self.app.destroy()

    def drain_ui_queue(self):
        """Run whatever background work queued for the UI thread."""
        while not self.app.ui_queue.empty():
            self.app.ui_queue.get_nowait()()


class TestWorkerCarriesUserfiles(_AppTestCase):
    def test_every_item_gets_the_given_mask(self):
        link = _FakeLink()
        items = [
            (0, "Album X", proto.InfoType.DISC_NAMES, "Disc Name", 0x17),
            (1, "Track One", proto.InfoType.TRACK_NAMES, "Track 1", 0x17),
        ]
        self.app._write_to_changer_worker(link, slot=2, items=items, userfiles=0x04)
        decoded = [proto.decode_text_data(call[3]) for call in link.send_write_calls]
        self.assertEqual([d["userfiles"] for d in decoded], [0x04, 0x04])
        self.assertEqual([d["genre"] for d in decoded], [0x17, 0x17])

    def test_rereads_userfiles_after_a_write(self):
        reads = []
        self.app._send_bg = lambda command, data, label, **kw: reads.append(data)
        self.app._write_to_changer_worker(
            _FakeLink(), slot=2, items=[(0, "A", proto.InfoType.DISC_NAMES, "Disc Name", 0)])
        self.assertIn(proto.encode_data_access(proto.Action.RETRIEVE_DATA,
                                               proto.DataType.DISC_USERFILES, slot=2), reads)


class TestNameWriteKeepsUserfiles(_AppTestCase):
    """The Disc Data tab's Write to Changer, end to end up to the worker."""

    def _prime(self, slot=2, genre=0x17, userfiles=0x04):
        self.app.link = _FakeLink()
        self.app._disc_data_slot = slot
        self.app._disc_name_cache[slot] = "Limblifter-Bellaclava"
        if genre is not None:
            self.app._genre_cache[slot] = genre
        if userfiles is not None:
            self.app._userfiles_cache[slot] = userfiles
        self.app._set_disc_data_row_count(slot, 2)
        self.app._disc_data_rows[1]["custom_var"].set("Hostess")

    @mock.patch.object(app_mod.messagebox, "askyesno", return_value=True)
    def test_known_mask_is_passed_to_the_worker(self, _ask):
        self._prime()
        with mock.patch.object(app_mod.threading, "Thread", _FakeThread):
            self.app._write_to_changer()
        (thread,) = _FakeThread.started
        self.assertEqual(thread.target, self.app._write_to_changer_worker)
        self.assertEqual(thread.kwargs["userfiles"], 0x04)

    @mock.patch.object(app_mod.messagebox, "askyesno", return_value=True)
    def test_unknown_state_is_read_first_then_the_write_goes_ahead(self, _ask):
        self._prime(genre=None, userfiles=None)
        with mock.patch.object(app_mod.threading, "Thread", _FakeThread):
            self.app._write_to_changer()
            (prefetch,) = _FakeThread.started
            # Simulate the changer answering both reads (as _on_frame would).
            self.app.link.on_send = lambda cmd, data: (
                self.app._genre_cache.__setitem__(2, 0x0B) if data[1] == proto.DataType.DISC_GENRE
                else self.app._userfiles_cache.__setitem__(2, 0x01))
            prefetch.target()
            self.assertEqual([d[1] for _c, d in self.app.link.send_calls],
                             [proto.DataType.DISC_GENRE, proto.DataType.DISC_USERFILES])
            self.drain_ui_queue()  # runs the retry, as the UI thread would
        writer = _FakeThread.started[-1]
        self.assertEqual(writer.target, self.app._write_to_changer_worker)
        self.assertEqual(writer.kwargs["userfiles"], 0x01)
        self.assertEqual(writer.args[2][0][4], 0x0B)  # genre kept too

    @mock.patch.object(app_mod.messagebox, "showerror")
    @mock.patch.object(app_mod.messagebox, "askyesno", return_value=True)
    def test_refuses_if_state_still_unknown_after_reading(self, ask, showerror):
        self._prime(userfiles=None)
        with mock.patch.object(app_mod.threading, "Thread", _FakeThread):
            self.app._write_to_changer(prefetched=True)
        self.assertEqual(_FakeThread.started, [])
        showerror.assert_called_once()
        ask.assert_not_called()


class TestMembershipWriteFlow(_AppTestCase):
    @mock.patch.object(app_mod.messagebox, "askyesno", return_value=True)
    def test_ticked_boxes_become_the_mask_on_a_disc_name_write(self, _ask):
        self.app.link = _FakeLink()
        self.app._disc_name_cache[3] = "Rusty / Fluke"
        self.app._genre_cache[3] = 0x17
        self.app._userfiles_cache[3] = 0x01
        self.app.uf_member_slot_var.set(3)
        for i, on in enumerate([True, False, True]):
            self.app.uf_member_vars[i].set(on)
        with mock.patch.object(app_mod.threading, "Thread", _FakeThread):
            self.app._write_userfile_membership()
        (thread,) = _FakeThread.started
        self.assertEqual(thread.kwargs["userfiles"], 0x05)
        link = _FakeLink()
        thread.target(link, *thread.args[1:], **thread.kwargs)
        (call,) = link.send_write_calls
        self.assertEqual(call[1], proto.encode_data_access(
            proto.Action.WRITE_NAME, proto.DataType.TEXT_DATA, slot=3, info_type=proto.InfoType.DISC_NAMES))
        p = proto.decode_text_data(call[3])
        self.assertEqual((p["slot"], p["index"], p["text"], p["genre"], p["userfiles"]),
                         (3, 0, "Rusty / Fluke", 0x17, 0x05))

    @mock.patch.object(app_mod.messagebox, "showinfo")
    def test_unnamed_disc_is_refused(self, showinfo):
        self.app.link = _FakeLink()
        self.app._disc_name_cache[3] = ""
        self.app._genre_cache[3] = 0
        self.app._userfiles_cache[3] = 0
        self.app.uf_member_slot_var.set(3)
        with mock.patch.object(app_mod.threading, "Thread", _FakeThread):
            self.app._write_userfile_membership()
        self.assertEqual(_FakeThread.started, [])
        showinfo.assert_called_once()


class TestProgramEditor(_AppTestCase):
    def test_read_then_edit_then_build(self):
        self.app._on_frame(proto.Frame(proto.CMD_DISC_LISTING, _data(PROGRAM_FRAME),
                                       proto.decode_disc_listing(_data(PROGRAM_FRAME))))
        self.drain_ui_queue()
        self.assertEqual(self.app._program_draft, PROGRAM_STEPS)
        self.assertFalse(self.app._program_draft_dirty)

        self.app.prog_tree.selection_set("0")
        self.app.prog_slot_var.set(4)
        self.app.prog_all_var.set(True)
        self.app._prog_add_step()  # inserted after step 1
        self.assertEqual(self.app._program_draft[1], (4, proto.LISTING_ALL_TRACKS))
        self.assertTrue(self.app._program_draft_dirty)

        self.app._prog_move(-1)
        self.assertEqual(self.app._program_draft[:2], [(4, proto.LISTING_ALL_TRACKS), (1, 1)])
        self.app._prog_remove_step()
        self.assertEqual(self.app._program_draft, PROGRAM_STEPS)

    @mock.patch.object(app_mod.messagebox, "showinfo")
    def test_add_stops_at_32(self, showinfo):
        self.app._program_draft = [(1, 1)] * 32
        self.app._prog_add_step()
        self.assertEqual(len(self.app._program_draft), 32)
        showinfo.assert_called_once()

    @mock.patch.object(app_mod.messagebox, "askyesno", return_value=True)
    def test_write_sends_write_program_then_disc_listing(self, _ask):
        self.app.link = _FakeLink()
        self.app._program_draft = list(PROGRAM_STEPS)
        with mock.patch.object(app_mod.threading, "Thread", _FakeThread):
            self.app._write_program()
            _FakeThread.started[0].target()
        (call,) = self.app.link.send_write_calls
        self.assertEqual(call[0], proto.CMD_DATA_ACCESS)
        self.assertEqual(call[1][:2], bytes([proto.Action.WRITE_PROGRAM, proto.DataType.DISC_LISTING]))
        self.assertEqual(call[2], proto.CMD_DISC_LISTING)
        self.assertEqual(proto.encode_frame(call[2], call[3]), bytes.fromhex(PROGRAM_FRAME))


class TestRealWriteSession(_AppTestCase):
    """Exact frames from the user's v1.8.0 test on a real CD-425M
    (2026-09-23). Every write below was ACK'd and read back matching."""

    def _wire(self, command, data):
        return proto.encode_frame(command, data).hex(" ")

    def test_userfile_renames(self):
        for number, name, request, follow_up in (
            (1, "New Name", "02 03 07 00 80 01 00 00 00 07 00 6e",
             "02 fe 0f 00 00 00 01 00 07 00 00 4e 65 77 20 4e 61 6d 65 20"),
            (3, "My List", "02 03 07 00 80 01 00 00 00 07 00 6e",
             "02 fe 0e 00 00 00 04 00 07 00 00 4d 79 20 4c 69 73 74 67"),
        ):
            req, fu = build_userfile_name_write(number, name)
            self.assertEqual(self._wire(proto.CMD_DATA_ACCESS, req), request)
            self.assertEqual(self._wire(proto.CMD_TEXT_DATA, fu), follow_up)

    def test_program_write(self):
        req, fu = build_program_write([(1, 1), (2, 5), (3, 2)])
        self.assertEqual(self._wire(proto.CMD_DATA_ACCESS, req), "02 03 07 00 20 20 00 00 00 00 00 b6")
        # The changer's re-read afterward returned this exact frame.
        self.assertEqual(self._wire(proto.CMD_DISC_LISTING, fu),
                         "02 0d 0a 00 03 01 00 01 02 00 05 03 00 02 d8")

    def test_membership_write_slot1_to_0x07(self):
        link = _FakeLink()
        items, _ = plan_userfile_membership_write("The Tragically Hip / Trou", 0x03)
        self.app._write_to_changer_worker(link, slot=1, items=items, userfiles=0x07)
        ((_, req, cmd, fu),) = link.send_write_calls
        self.assertEqual(self._wire(proto.CMD_DATA_ACCESS, req), "02 03 07 00 80 01 01 00 00 00 00 74")
        self.assertEqual(self._wire(cmd, fu),
                         "02 fe 20 00 01 00 00 07 00 03 00 54 68 65 20 54 72 61 67 69 63 61 6c 6c"
                         " 79 20 48 69 70 20 2f 20 54 72 6f 75 30")

    def test_track_name_write_carries_mask_and_genre(self):
        link = _FakeLink()
        self.app._write_to_changer_worker(
            link, slot=1, items=[(1, "Gift Shoppe", proto.InfoType.TRACK_NAMES, "Track 1", 0x03)],
            userfiles=0x00)
        ((_, req, cmd, fu),) = link.send_write_calls
        self.assertEqual(self._wire(proto.CMD_DATA_ACCESS, req), "02 03 07 00 80 01 01 00 00 01 00 73")
        self.assertEqual(self._wire(cmd, fu),
                         "02 fe 12 00 01 00 01 00 01 03 00 47 69 66 74 20 53 68 6f 70 70 65 d1")

    def test_track_name_write_keeps_a_nonzero_mask(self):
        # v1.8.2: slot 1 in #1-#3 (0x07). The write carried 0x07, and the
        # re-read showed 0x07 on DiscUserfiles and every track.
        link = _FakeLink()
        self.app._write_to_changer_worker(
            link, slot=1, items=[(1, "Gift Shop", proto.InfoType.TRACK_NAMES, "Track 1", 0x03)],
            userfiles=0x07)
        ((_, _req, cmd, fu),) = link.send_write_calls
        self.assertEqual(self._wire(cmd, fu),
                         "02 fe 10 00 01 00 01 07 01 03 00 47 69 66 74 20 53 68 6f 70 a1")

    def test_empty_program_write_clears_it(self):
        # v1.12.2 (2026-09-24): a 3-step program was written, then an empty
        # one. Both got ReadyForData 4 and were ACK'd; the re-read after the
        # empty write came back empty, and the InfoEvent went program=0,
        # Track Mode.
        req, fu = build_program_write([(1, 1), (2, 2), (3, 3)])
        self.assertEqual(self._wire(proto.CMD_DATA_ACCESS, req), "02 03 07 00 20 20 00 00 00 00 00 b6")
        self.assertEqual(self._wire(proto.CMD_DISC_LISTING, fu),
                         "02 0d 0a 00 03 01 00 01 02 00 02 03 00 03 da")
        req, fu = build_program_write([])
        self.assertEqual(self._wire(proto.CMD_DATA_ACCESS, req), "02 03 07 00 20 20 00 00 00 00 00 b6")
        self.assertEqual(self._wire(proto.CMD_DISC_LISTING, fu), "02 0d 01 00 00 f2")

        reply = "02 0d 01 00 00 f2"
        self.assertEqual(proto.decode_disc_listing(_data(reply))["items"], [])
        self.app._program_draft = [(1, 1), (2, 2), (3, 3)]
        self.app._on_frame(proto.Frame(proto.CMD_DISC_LISTING, _data(reply),
                                       proto.decode_disc_listing(_data(reply))))
        self.drain_ui_queue()
        self.assertEqual(self.app._program_draft, [])

        info = proto.decode_info_event(_data("02 12 09 00 01 00 01 00 03 02 00 00 00 de"))
        self.assertEqual((info["program"], info["mode_name"]), (0, "Track Mode"))

    def test_ready_for_data_bytes_seen(self):
        # Seen: 1 and 0 for track-name WRITE_NAMEs, 1 for disc-name and
        # userfile-name ones, 4 for WRITE_PROGRAM. Meaning unknown; all
        # proceeded.
        for frame, raw in (("02 09 01 00 01 f5", 1), ("02 09 01 00 00 f6", 0), ("02 09 01 00 04 f2", 4)):
            self.assertEqual(proto.decode_ready_for_data(_data(frame)), {"raw_byte": raw})


if __name__ == "__main__":
    unittest.main(verbosity=2)
