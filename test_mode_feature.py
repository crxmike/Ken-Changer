#!/usr/bin/env python3
"""
test_mode_feature.py
====================
Tests for the Control tab's Play Mode selector (v1.6.0): ChangeMode
(cd_changemode.html) plus InfoEvent's `param` byte in the status panel.

Stdlib-only (unittest), matching the rest of this project.

Real-hardware status (2026-09-22, see CHANGELOG.md v1.6.1/v1.6.2):
ChangeMode is confirmed for Music Type and Userfile modes, including
the userfile param being a BIT (#3 -> 0x04), not a number. A mode the
changer can't enter (Best; Program with nothing stored) is ACK'd and
then silently ignored. TestRealHardwareSession and TestUserfile3Session
use the logged bytes.
"""

import unittest

import pclink_protocol as proto
from pclink_app import (
    CHANGE_MODE_UNSUPPORTED, MODE_NAME_TO_CODE, build_change_mode_request,
    change_mode_choices,
)


class TestEncodeChangeMode(unittest.TestCase):
    def test_payload_is_mode_then_param(self):
        self.assertEqual(proto.encode_change_mode(proto.Mode.GENRE, 0x17), bytes([0x05, 0x17]))

    def test_full_frame_checksum(self):
        # 0C + 02 00 + 03 00 = 0x11 -> checksum 0xEF
        frame = proto.encode_frame(proto.CMD_CHANGE_MODE, proto.encode_change_mode(proto.Mode.PROGRAM))
        self.assertEqual(frame, bytes([0x02, 0x0C, 0x02, 0x00, 0x03, 0x00, 0xEF]))


class TestModeNameToCode(unittest.TestCase):
    def test_covers_every_mode(self):
        self.assertEqual(sorted(MODE_NAME_TO_CODE.values()), sorted(proto.MODE_NAMES))


class TestUserfileParam(unittest.TestCase):
    def test_bit_encoding(self):
        self.assertEqual(proto.userfile_param(1), 0x01)
        self.assertEqual(proto.userfile_param(3), 0x04)
        self.assertEqual(proto.userfile_param(8), 0x80)

    def test_matches_infoevent_userfile_bits(self):
        for n in range(1, 9):
            self.assertEqual(proto.userfile_list(proto.userfile_param(n)), [f"Userfile #{n}"])

    def test_out_of_range_rejected(self):
        for bad in (0, 9, -1):
            with self.assertRaises(ValueError):
                proto.userfile_param(bad)


class TestBuildChangeModeRequest(unittest.TestCase):
    def test_plain_mode_sends_zero_param(self):
        payload, label = build_change_mode_request("Track Mode", "Rock", 3)
        self.assertEqual(payload, bytes([proto.Mode.TRACK, 0x00]))
        self.assertIn("param=0x00", label)

    def test_every_non_param_mode_ignores_pickers(self):
        for code, name in proto.MODE_NAMES.items():
            if code in proto.GENRE_MODES or code in proto.USERFILE_MODES:
                continue
            payload, _ = build_change_mode_request(name, "Folk", 5)
            self.assertEqual(payload, bytes([code, 0x00]))

    def test_genre_modes_carry_the_genre_code(self):
        for code in proto.GENRE_MODES:
            payload, label = build_change_mode_request(proto.MODE_NAMES[code], "Rock", 1)
            self.assertEqual(payload, bytes([code, 0x17]))
            self.assertIn("genre=Rock", label)

    def test_genre_mode_without_genre_refused(self):
        with self.assertRaises(ValueError):
            build_change_mode_request("Music Type Mode", "", 1)

    def test_userfile_modes_carry_the_userfile_bit(self):
        for code in proto.USERFILE_MODES:
            payload, label = build_change_mode_request(proto.MODE_NAMES[code], "", 4)
            self.assertEqual(payload, bytes([code, 0x08]))
            self.assertIn("userfile=#4", label)

    def test_unknown_mode_refused(self):
        with self.assertRaises(ValueError):
            build_change_mode_request("Party Mode", "", 1)


class TestInfoEventParamDescription(unittest.TestCase):
    def _info(self, mode, param):
        # slot=2, track=1, program=0, genre=0x0A, userfiles=0, param, mode, repeat=0
        return proto.decode_info_event(bytes([0x02, 0x00, 0x01, 0x00, 0x0A, 0x00, param, mode, 0x00]))

    def test_genre_mode_shows_raw_byte_only(self):
        # Real hardware reported param=0x00 while in Music Type (Rock) mode,
        # so naming it ("Unassigned") would be misleading.
        p = self._info(proto.Mode.GENRE, 0x00)
        self.assertEqual(p["param_desc"], "0x00")

    def test_userfile_mode_names_the_userfile_and_shows_raw(self):
        p = self._info(proto.Mode.USERFILE, 0x04)
        self.assertEqual(p["param_desc"], "Userfile #3 (0x04)")

    def test_other_modes_show_raw_byte_only(self):
        p = self._info(proto.Mode.TRACK, 0x00)
        self.assertEqual(p["param_desc"], "0x00")


class TestRealHardwareSession(unittest.TestCase):
    """Exact bytes from the user's 2026-09-22 session on a real CD-425M."""

    def test_change_mode_frames_match_what_went_out(self):
        cases = [
            ("Best Mode", "", 1, "02 0c 02 00 04 00 ee"),
            ("Music Type Mode", "Rock", 1, "02 0c 02 00 05 17 d6"),
            ("Userfile Mode", "", 1, "02 0c 02 00 07 01 ea"),
            ("Program Mode", "", 1, "02 0c 02 00 03 00 ef"),
        ]
        for mode_name, genre, userfile, wire in cases:
            payload, _ = build_change_mode_request(mode_name, genre, userfile)
            self.assertEqual(proto.encode_frame(proto.CMD_CHANGE_MODE, payload), bytes.fromhex(wire))

    def test_infoevent_after_music_type_rock(self):
        p = proto.decode_info_event(bytes.fromhex("02 00 01 01 17 00 00 05 00"))
        self.assertEqual(p["mode"], proto.Mode.GENRE)
        self.assertEqual(p["param"], 0x00)  # NOT the selected genre (0x17)
        self.assertEqual(p["param_desc"], "0x00")

    def test_infoevent_after_userfile_1(self):
        p = proto.decode_info_event(bytes.fromhex("03 00 01 01 17 01 01 07 00"))
        self.assertEqual(p["mode"], proto.Mode.USERFILE)
        self.assertEqual(p["param_desc"], "Userfile #1 (0x01)")

    def test_infoevent_after_userfile_2_from_remote(self):
        p = proto.decode_info_event(bytes.fromhex("04 00 01 01 17 02 02 07 00"))
        self.assertEqual(p["param_desc"], "Userfile #2 (0x02)")

    def test_genre_byte_on_all_rock_discs(self):
        # Slots 2/3/4 have 13/10/12 tracks (from their TOCs) and were all
        # genre Rock (0x17) this session; byte 5 read 0x17 for all three.
        for wire in ("02 00 01 00 17 00 00 00 00", "03 00 01 01 17 01 01 07 00",
                     "04 00 01 01 17 02 02 07 00"):
            self.assertEqual(proto.decode_info_event(bytes.fromhex(wire))["genre"], 0x17)


class TestUserfile3Session(unittest.TestCase):
    """Second real-hardware session (2026-09-22): the one that settled the
    userfile encoding, since #3 is the first number where number (0x03)
    and bit (0x04) differ."""

    def test_tagging_slot_2_as_userfile_3_reads_back_as_bit_0x04(self):
        p = proto.decode_info_event(bytes.fromhex("02 00 01 00 17 04 00 00 00"))
        self.assertEqual(p["userfiles"], 0x04)
        self.assertEqual(p["userfile_names"], ["Userfile #3"])

    def test_change_mode_userfile_3_frame_matches_the_wire(self):
        payload, _ = build_change_mode_request("Userfile Mode", "", 3)
        self.assertEqual(proto.encode_frame(proto.CMD_CHANGE_MODE, payload),
                         bytes.fromhex("02 0c 02 00 07 04 e7"))

    def test_changer_reported_back_the_same_param(self):
        p = proto.decode_info_event(bytes.fromhex("02 00 01 01 17 04 04 07 00"))
        self.assertEqual((p["mode"], p["param"]), (proto.Mode.USERFILE, 0x04))
        self.assertEqual(p["param_desc"], "Userfile #3 (0x04)")


class TestBestModeSession(unittest.TestCase):
    """Third real-hardware session (2026-09-22): Best mode started from the
    remote, then Track Mode and Best Mode sent from the app."""

    def test_remote_started_best_mode_reports_mode_4(self):
        # Confirms Mode.BEST (0x04) matches what the changer itself reports;
        # program=1 is presumably the position in the Best list ("BEST01").
        p = proto.decode_info_event(bytes.fromhex("03 00 03 01 17 01 00 04 00"))
        self.assertEqual(p["mode"], proto.Mode.BEST)
        self.assertEqual((p["slot"], p["track"], p["program"]), (3, 3, 1))

    def test_change_mode_track_from_app_took(self):
        payload, _ = build_change_mode_request("Track Mode", "", 1)
        self.assertEqual(proto.encode_frame(proto.CMD_CHANGE_MODE, payload),
                         bytes.fromhex("02 0c 02 00 00 00 f2"))
        p = proto.decode_info_event(bytes.fromhex("03 00 03 00 17 01 00 00 00"))
        self.assertEqual(p["mode"], proto.Mode.TRACK)

    def test_ignored_best_request_was_correctly_encoded(self):
        # Same bytes as session 1's ignored Best request -- the frame isn't
        # the problem; the changer just didn't act on it while playing.
        payload, _ = build_change_mode_request("Best Mode", "", 1)
        self.assertEqual(proto.encode_frame(proto.CMD_CHANGE_MODE, payload),
                         bytes.fromhex("02 0c 02 00 04 00 ee"))


class TestBestModeUnsupported(unittest.TestCase):
    """Fourth real-hardware session (2026-09-22): Stop (StateEvent Stopped),
    then ChangeMode(Best) -- still ACK'd and ignored, disproving the
    "only while stopped" theory from v1.6.3."""

    def test_stopped_state_before_the_request(self):
        self.assertEqual(proto.decode_state_event(bytes([0x40]))["state"], proto.State.STOPPED)

    def test_best_left_out_of_the_dropdown(self):
        self.assertIn(proto.Mode.BEST, CHANGE_MODE_UNSUPPORTED)
        self.assertNotIn("Best Mode", change_mode_choices())

    def test_every_other_mode_still_offered(self):
        expected = [proto.MODE_NAMES[c] for c in sorted(proto.MODE_NAMES)
                    if c not in (proto.Mode.BEST, proto.Mode.PROGRAM)]
        self.assertEqual(change_mode_choices(), expected)

    def test_best_still_decoded_when_started_from_the_remote(self):
        p = proto.decode_info_event(bytes.fromhex("03 00 03 01 17 01 00 04 00"))
        self.assertEqual(p["mode_name"], "Best Mode")


class TestTwoGenreSession(unittest.TestCase):
    """Fifth real-hardware session (2026-09-22): slot 4 re-tagged
    Alternative Rock (0x03), slot 2 still Rock (0x17). Settled that
    InfoEvent byte 5 is the disc's genre, and exercised Music Type mode
    with two different genres."""

    def test_genre_byte_in_track_mode_is_the_discs_genre(self):
        # At connect, plain Track Mode (no genre selected): slot 4 reads
        # 0x03 -- its DiscGenre -- and it has 12 tracks, not 3.
        p = proto.decode_info_event(bytes.fromhex("04 00 01 00 03 00 00 00 00"))
        self.assertEqual(p["mode"], proto.Mode.TRACK)
        self.assertEqual((p["genre"], p["genre_name"]), (0x03, "Alternative Rock"))
        self.assertNotIn("num_tracks", p)

    def test_music_type_rock_played_the_rock_disc(self):
        payload, _ = build_change_mode_request("Music Type Mode", "Rock", 1)
        self.assertEqual(proto.encode_frame(proto.CMD_CHANGE_MODE, payload),
                         bytes.fromhex("02 0c 02 00 05 17 d6"))
        p = proto.decode_info_event(bytes.fromhex("02 00 01 01 17 04 00 05 00"))
        self.assertEqual((p["slot"], p["mode"], p["genre_name"]), (2, proto.Mode.GENRE, "Rock"))

    def test_music_type_alt_rock_played_the_alt_rock_disc(self):
        payload, _ = build_change_mode_request("Music Type Mode", "Alternative Rock", 1)
        self.assertEqual(proto.encode_frame(proto.CMD_CHANGE_MODE, payload),
                         bytes.fromhex("02 0c 02 00 05 03 ea"))
        p = proto.decode_info_event(bytes.fromhex("04 00 01 01 03 00 00 05 00"))
        self.assertEqual((p["slot"], p["mode"], p["genre_name"]), (4, proto.Mode.GENRE, "Alternative Rock"))
        self.assertEqual(p["param"], 0x00)  # still not the selected genre


class TestProgramModeUnsupported(unittest.TestCase):
    """Sixth real-hardware session (2026-09-22): a program (slot 4 T1,
    slot 4 T2, slot 2 T4) stored and played from the remote; the app's
    ChangeMode(Program) was ignored 4 times, playing and stopped."""

    def test_program_left_out_of_the_dropdown(self):
        self.assertIn(proto.Mode.PROGRAM, CHANGE_MODE_UNSUPPORTED)
        self.assertNotIn("Program Mode", change_mode_choices())

    def test_ignored_program_request_was_correctly_encoded(self):
        payload, _ = build_change_mode_request("Program Mode", "", 1)
        self.assertEqual(proto.encode_frame(proto.CMD_CHANGE_MODE, payload),
                         bytes.fromhex("02 0c 02 00 03 00 ef"))

    def test_remote_started_program_steps_through_the_program_byte(self):
        steps = [("04 00 01 01 03 00 00 03 00", 4, 1, 1),
                 ("02 00 04 02 17 04 00 03 00", 2, 4, 2)]
        for wire, slot, track, step in steps:
            p = proto.decode_info_event(bytes.fromhex(wire))
            self.assertEqual(p["mode"], proto.Mode.PROGRAM)
            self.assertEqual((p["slot"], p["track"], p["program"]), (slot, track, step))

    def test_track_mode_from_the_app_still_worked_in_between(self):
        p = proto.decode_info_event(bytes.fromhex("02 00 04 00 17 04 00 00 00"))
        self.assertEqual((p["mode"], p["program"]), (proto.Mode.TRACK, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
