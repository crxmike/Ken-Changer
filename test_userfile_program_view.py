#!/usr/bin/env python3
"""
test_userfile_program_view.py
=============================
Tests for the read-only "Userfiles & Program" tab (v1.7.0).

Stdlib-only (unittest), matching the rest of this project.

CONFIRMED on real hardware (v1.7.1, 2026-09-22 log -- TestRealSession):
  - DataAccess(RETRIEVE, DiscListing, slot=0) returns the stored program
    as a single DiscListing frame.
  - DataAccess(RETRIEVE, TextData, slot=0, info_type=7) returns all 8
    userfile names, each TextData's `index` being the userfile's BIT
    (1, 2, 4 ... 128), not its number; unnamed ones are a lone 0x01.
  - DataAccess(RETRIEVE, DiscUserfiles, slot) returns one DiscUserfiles
    frame per slot.
"""

import types
import unittest

import pclink_protocol as proto
import pclink_app
from pclink_app import program_rows, userfile_rows


def _data(frame_hex):
    """Payload bytes of a logged frame (strip STX, cmd, 2 length bytes, checksum)."""
    return bytes.fromhex(frame_hex)[4:-1]


class TestRequests(unittest.TestCase):
    def test_program_request(self):
        data = proto.encode_data_access(proto.Action.RETRIEVE_DATA, proto.DataType.DISC_LISTING, slot=0)
        self.assertEqual(data, bytes([0x00, 0x20, 0x00, 0x00, 0x00, 0x00, 0x00]))

    def test_userfile_names_request(self):
        data = proto.encode_data_access(
            proto.Action.RETRIEVE_DATA, proto.DataType.TEXT_DATA, slot=0,
            info_type=proto.InfoType.USERFILE_NAMES,
        )
        self.assertEqual(data, bytes([0x00, 0x01, 0x00, 0x00, 0x00, 0x07, 0x00]))

    def test_disc_userfiles_request(self):
        data = proto.encode_data_access(proto.Action.RETRIEVE_DATA, proto.DataType.DISC_USERFILES, slot=3)
        self.assertEqual(data, bytes([0x00, 0x08, 0x03, 0x00, 0x00, 0x00, 0x00]))


class TestDecodeDiscListing(unittest.TestCase):
    def test_program_shaped_like_the_one_the_user_built(self):
        # slot 4 T1, slot 4 T2, slot 2 T4 (the program from v1.6.6's session)
        data = proto.encode_disc_listing([(4, 1), (4, 2), (2, 4)])
        p = proto.decode_disc_listing(data)
        self.assertEqual([(i["slot"], i["track"]) for i in p["items"]], [(4, 1), (4, 2), (2, 4)])
        self.assertFalse(p["truncated"])

    def test_all_tracks_marker(self):
        p = proto.decode_disc_listing(proto.encode_disc_listing([(7, 0xAA)]))
        self.assertTrue(p["items"][0]["all_tracks"])

    def test_empty_and_truncated_frames_dont_raise(self):
        self.assertEqual(proto.decode_disc_listing(b"")["items"], [])
        self.assertEqual(proto.decode_disc_listing(bytes([0]))["items"], [])
        p = proto.decode_disc_listing(bytes([3, 4, 0, 1, 4, 0]))  # says 3, has 1.67
        self.assertEqual(len(p["items"]), 1)
        self.assertTrue(p["truncated"])


class TestUserfileRows(unittest.TestCase):
    def test_membership_from_logged_bitmasks(self):
        # From real logs: slot 2 -> 0x04 (#3), slot 3 -> 0x01 (#1), slot 1 -> 0x00.
        cache = {
            2: proto.decode_info_event(_data("02 12 09 00 02 00 01 00 17 04 00 00 00 c7"))["userfiles"],
            3: proto.decode_info_event(_data("02 12 09 00 03 00 01 01 17 01 01 07 00 c0"))["userfiles"],
            1: 0x00,
        }
        names = {2: "Limblifter-Bellaclava", 3: "Rusty / Fluke"}
        rows = userfile_rows(cache, {}, names)
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0], ("#1", "-", "3 (Rusty / Fluke)"))
        self.assertEqual(rows[2], ("#3", "-", "2 (Limblifter-Bellaclava)"))
        self.assertEqual(rows[1], ("#2", "-", "-"))

    def test_disc_in_several_userfiles(self):
        rows = userfile_rows({5: 0b10000001}, {0x01: "Dad", 0x80: "Kids"}, {})
        self.assertEqual(rows[0], ("#1", "Dad", "5"))
        self.assertEqual(rows[7], ("#8", "Kids", "5"))


class TestProgramRows(unittest.TestCase):
    def test_rows_use_known_names(self):
        items = proto.decode_disc_listing(proto.encode_disc_listing([(2, 4), (3, 0xAA)]))["items"]
        rows = program_rows(items, {2: "Limblifter-Bellaclava"}, {2: {4: "Hostess"}})
        self.assertEqual(rows, [(1, "2 (Limblifter-Bellaclava)", "4 (Hostess)"),
                                (2, "3", "All tracks")])


class TestCachingFromTraffic(unittest.TestCase):
    """_cache_name / _note_userfiles, driven by real logged TextData frames."""

    @staticmethod
    def _fake_app():
        app = types.SimpleNamespace(
            _disc_name_cache={}, _track_name_cache={}, _userfiles_cache={},
            _userfile_name_cache={}, _current_slot=None, ui_queue=types.SimpleNamespace(put=lambda f: None),
            _update_name_labels=lambda: None, _refresh_disc_data_from_changer=lambda: None,
            uf_tree=object(), _refresh_userfiles_view=lambda: None,
        )
        app._note_userfiles = lambda s, u: pclink_app.App._note_userfiles(app, s, u)
        return app

    def test_track_name_reply_records_the_discs_userfiles(self):
        app = self._fake_app()
        frame = "02 fe 0e 00 03 00 03 01 01 17 00 57 61 6b 65 20 4d 65 7b"  # slot 3 'Wake Me', userfiles 0x01
        pclink_app.App._cache_name(app, proto.decode_text_data(_data(frame)))
        self.assertEqual(app._userfiles_cache, {3: 0x01})
        self.assertEqual(app._track_name_cache[3][3], "Wake Me")

    def test_userfile_name_reply_goes_to_its_own_cache(self):
        app = self._fake_app()
        # Synthetic (not yet seen on hardware): info_type 7, index 2, "Dad".
        data = bytes([0x00, 0x00, 0x02, 0x00, 0x07, 0x00, 0x00]) + b"Dad\x00"
        pclink_app.App._cache_name(app, proto.decode_text_data(data))
        self.assertEqual(app._userfile_name_cache, {2: "Dad"})
        self.assertEqual(app._userfiles_cache, {})
        self.assertEqual(app._disc_name_cache, {})


class TestRealSession(unittest.TestCase):
    """Exact frames from the user's v1.7.0 test on a real CD-425M."""

    NAME_FRAMES = [
        "02 fe 10 00 00 00 01 00 07 00 00 66 55 43 4b 20 73 48 49 54 29",
        "02 fe 11 00 00 00 02 00 07 00 00 42 41 4c 4c 53 20 46 41 52 54 2d",
        "02 fe 08 00 00 00 04 00 07 00 00 01 ee",
        "02 fe 08 00 00 00 08 00 07 00 00 01 ea",
        "02 fe 08 00 00 00 10 00 07 00 00 01 e2",
        "02 fe 08 00 00 00 20 00 07 00 00 01 d2",
        "02 fe 08 00 00 00 40 00 07 00 00 01 b2",
        "02 fe 08 00 00 00 80 00 07 00 00 01 72",
    ]
    PROGRAM_FRAME = ("02 0d 22 00 0b 01 00 01 01 00 01 01 00 02 01 00 05 01 00 08 01 00 04"
                     " 01 00 02 01 00 05 01 00 03 01 00 06 01 00 09 8d")

    def test_requests_match_the_wire(self):
        cases = [
            (proto.encode_data_access(proto.Action.RETRIEVE_DATA, proto.DataType.TEXT_DATA, slot=0,
                                      info_type=proto.InfoType.USERFILE_NAMES),
             "02 03 07 00 00 01 00 00 00 07 00 ee"),
            (proto.encode_data_access(proto.Action.RETRIEVE_DATA, proto.DataType.DISC_LISTING, slot=0),
             "02 03 07 00 00 20 00 00 00 00 00 d6"),
            (proto.encode_data_access(proto.Action.RETRIEVE_DATA, proto.DataType.DISC_USERFILES, slot=2),
             "02 03 07 00 00 08 02 00 00 00 00 ec"),
        ]
        for data, wire in cases:
            self.assertEqual(proto.encode_frame(proto.CMD_DATA_ACCESS, data), bytes.fromhex(wire))

    def test_userfile_names_are_indexed_by_bit(self):
        app = TestCachingFromTraffic._fake_app()
        for frame in self.NAME_FRAMES:
            pclink_app.App._cache_name(app, proto.decode_text_data(_data(frame)))
        self.assertEqual(app._userfile_name_cache, {1: "fUCK sHIT", 2: "BALLS FART"})
        rows = userfile_rows({}, app._userfile_name_cache, {})
        self.assertEqual([r[1] for r in rows], ["fUCK sHIT", "BALLS FART"] + ["-"] * 6)

    def test_bit_indexing_puts_userfile_3_on_row_3(self):
        # What the old "index n = #n" lookup got wrong: #3's name is index 4.
        rows = userfile_rows({}, {0x04: "Road Trip"}, {})
        self.assertEqual(rows[2][1], "Road Trip")
        self.assertEqual(rows[3][1], "-")

    def test_program_frame(self):
        p = proto.decode_disc_listing(_data(self.PROGRAM_FRAME))
        self.assertEqual(p["length"], 11)
        self.assertFalse(p["truncated"])
        self.assertEqual([(i["slot"], i["track"]) for i in p["items"]],
                         [(1, 1), (1, 1), (1, 2), (1, 5), (1, 8), (1, 4),
                          (1, 2), (1, 5), (1, 3), (1, 6), (1, 9)])

    def test_disc_userfiles_frames(self):
        frames = ("02 07 03 00 01 00 00 f5", "02 07 03 00 02 00 04 f0", "02 07 03 00 03 00 01 f2")
        got = {}
        for f in frames:
            p = proto.decode_disc_userfiles(_data(f))
            got[p["slot"]] = p["userfiles"]
        self.assertEqual(got, {1: 0x00, 2: 0x04, 3: 0x01})


if __name__ == "__main__":
    unittest.main(verbosity=2)
