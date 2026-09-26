"""
test_cdtext_write_block.py
==========================
Tests for v1.12.11: no name writes to a CD-Text disc, and its genre and
userfiles carried on a re-sent track 1 name instead of the disc name.

Built from the raw-byte log of 2026-09-26 16:32-16:40 (slot 4, the
CD-Text disc). What that log CONFIRMS: a genre write on a disc-name write
works while slot 4 is in the drive (Christian -> Soundtrack, 16:35:18); the
written disc name ("test name") is stored but only reads back while slot 4
is out of the drive, and the front panel still shows "-----"; the stored
track names are the CD-Text titles cut to 25; and both userfile writes
stopped because the disc name couldn't be read (16:39:17, 16:40:12). The
17:07-17:09 log then CONFIRMED v1.12.11 itself: a track 1 write set slot 4's
userfiles (0x00 -> 0x07) and, separately, its genre (TestConfirmedOnHardware).

Run with:
    python test_cdtext_write_block.py -v
"""

from __future__ import annotations

import unittest
from unittest import mock

# Stubs out pyserial and gives the fake link/thread used by the write tests.
from test_userfile_program_write import _FakeLink, _FakeThread

import library_backup
import library_browser
import pclink_app as app_mod
import pclink_protocol as proto
from pclink_app import (missing_disc_state, plan_cdtext_disc_data_write,
                        plan_userfile_membership_write)
from pclink_link import PCLinkTextStream

T = proto.InfoType
SOUNDTRACK, CHRISTIAN = 0x1A, 0x06


def _frame(hex_text: str) -> proto.Frame:
    raw = bytes.fromhex(hex_text)
    data = raw[4:-1]
    return proto.Frame(command=raw[1], data=data, payload=proto.decode_payload(raw[1], data))


# All from the 16:32-16:40 log.
DISCINFO_SLOT4 = "02 04 05 00 04 00 01 0a 90 58"          # 10 tracks, 0x90
DISCINFO_SLOT3 = "02 04 05 00 03 00 01 63 00 90"          # 99 tracks, 0x00
DISCINFO_SLOT100 = "02 04 05 00 64 00 00 00 90 03"        # empty, 0x90
STORED_NAME_SLOT4 = "02 fe 10 00 04 00 00 00 00 1a 90 74 65 73 74 20 6e 61 6d 65 c3"
STORED_TRACK1_SLOT4 = ("02 fe 13 00 04 00 01 00 01 1a 90 53 69 6c 65 6e 74 20 4e 69 67 68 "
                       "74 b6")
LONG_TRACK1_SLOT4 = ("02 fd 14 00 04 00 01 00 01 1a 90 01 53 69 6c 65 6e 74 20 4e 69 67 68 "
                     "74 b5")
STORED_NAME_SLOT3 = "02 fe 14 00 03 00 00 01 00 17 00 52 75 73 74 79 20 2f 20 46 6c 75 6b 65 46"
USERFILE_NAME_1 = "02 fe 0f 00 00 00 01 00 07 00 00 4e 65 77 20 4e 61 6d 65 20"
STREAM_FRAME = "02 fd 09 00 04 00 00 00 00 1a 90 01 01 4a"
TOC_SLOT4 = ("02 06 27 00 04 00 01 90 01 0a 00 02 00 03 32 00 08 36 00 10 48 00 12 51 00 15 "
             "30 00 18 39 00 21 54 00 24 22 00 27 16 00 30 37 00 0e")
TOC_SLOT3 = ("02 06 27 00 03 00 01 00 01 0a 00 02 00 03 45 00 05 39 00 09 49 00 13 27 00 16 "
             "43 00 21 10 00 26 46 00 30 43 00 32 30 00 37 02 00 ac")
GENRE_SLOT4 = "02 08 03 00 04 00 1a d7"
USERFILES_SLOT4 = "02 07 03 00 04 00 00 f2"


class _AppCase(unittest.TestCase):
    def setUp(self):
        self.app = app_mod.App(library_cache_path=None)
        self.app._send_bg = lambda *a, **kw: None
        _FakeThread.started = []

    def tearDown(self):
        self.app.destroy()

    def drain(self):
        while not self.app.ui_queue.empty():
            self.app.ui_queue.get_nowait()()


class TestRecognizingACdTextDisc(_AppCase):
    def feed(self, *frames):
        for f in frames:
            self.app._on_frame(_frame(f))

    def test_discinfo_0x90_with_tracks(self):
        self.feed(DISCINFO_SLOT4, DISCINFO_SLOT3)
        self.assertEqual(self.app._cdtext_slots, {4})

    def test_empty_slot_reporting_0x90_isnt_one(self):
        self.feed(DISCINFO_SLOT100)
        self.assertEqual(self.app._cdtext_slots, set())

    def test_stored_names_and_toc_say_so_too(self):
        # 16:38:02: slot 4 read with slot 3 in the drive, as TextData.
        self.feed(STORED_NAME_SLOT4)
        self.assertEqual(self.app._cdtext_slots, {4})
        self.app._cdtext_slots.clear()
        self.feed(TOC_SLOT4, TOC_SLOT3)
        self.assertEqual(self.app._cdtext_slots, {4})

    def test_stream_frames_say_so(self):
        self.feed(STREAM_FRAME)
        self.assertEqual(self.app._cdtext_slots, {4})

    def test_a_0x00_reply_clears_it(self):
        # A different disc put in the slot.
        self.app._cdtext_slots.add(3)
        self.feed(STORED_NAME_SLOT3)
        self.assertEqual(self.app._cdtext_slots, set())

    def test_userfile_names_are_not_a_disc(self):
        self.app._cdtext_slots.add(0)
        self.feed(USERFILE_NAME_1)
        self.assertEqual(self.app._cdtext_slots, {0})


class TestCarrier(unittest.TestCase):
    def test_track1_write_bytes_match_the_stored_copy(self):
        # What slot 4's genre/userfiles write sends: track 1's stored
        # TextData (16:38:03) with our format byte, 0x00, in place of 0x90.
        item, error = library_backup.state_carrier(True, None, "Silent Night", SOUNDTRACK)
        self.assertIsNone(error)
        index, text, info_type, _label, genre = item
        sent = proto.encode_text_data(slot=4, index=index, text=text, info_type=info_type,
                                      genre=genre, userfiles=0x00)
        stored = bytearray(_frame(STORED_TRACK1_SLOT4).data)
        stored[6] = proto.Format.NO_CDTEXT
        self.assertEqual(sent, bytes(stored))

    def test_full_cdtext_title_is_cut_to_the_stored_25(self):
        item, _ = library_backup.state_carrier(True, None, "We Wish You A Merry Christmas", 0)
        self.assertEqual(item[1], "We Wish You A Merry Chris")  # as stored, 16:38:03

    def test_no_track1_name(self):
        item, error = library_backup.state_carrier(True, "test name", None, 0)
        self.assertIsNone(item)
        self.assertIn("track 1", error)

    def test_other_discs_still_use_the_disc_name(self):
        item, _ = library_backup.state_carrier(False, "Rusty / Fluke", "Groovy Dead", 0x17)
        self.assertEqual(item[:3], (0, "Rusty / Fluke", T.DISC_NAMES))


class TestPlans(unittest.TestCase):
    NAMES = [(0, "Acoustic Christmas", T.DISC_NAMES, "Disc Name"),
             (1, "Name 1", T.TRACK_NAMES, "Track 1")]

    def test_names_are_never_written(self):
        items, message = plan_cdtext_disc_data_write(self.NAMES, None, "Silent Night")
        self.assertEqual(items, [])
        self.assertIn("CD-Text", message)

    def test_genre_rides_on_track1_and_names_are_dropped(self):
        items, message = plan_cdtext_disc_data_write(self.NAMES, SOUNDTRACK, "Silent Night")
        self.assertEqual(items, [(1, "Silent Night", T.TRACK_NAMES, "Track 1 (genre)", SOUNDTRACK)])
        self.assertIn("aren't written", message)
        items, message = plan_cdtext_disc_data_write([], SOUNDTRACK, "Silent Night")
        self.assertIsNone(message)

    def test_genre_without_track1(self):
        items, message = plan_cdtext_disc_data_write([], SOUNDTRACK, None)
        self.assertEqual(items, [])
        self.assertIn("track 1", message)

    def test_userfiles_on_a_cdtext_disc(self):
        items, error = plan_userfile_membership_write(None, SOUNDTRACK, True, "Silent Night")
        self.assertIsNone(error)
        self.assertEqual(items, [(1, "Silent Night", T.TRACK_NAMES, "Track 1 (userfiles only)",
                                  SOUNDTRACK)])
        _, error = plan_userfile_membership_write("test name", SOUNDTRACK, True, None)
        self.assertIn("track 1", error)

    def test_userfiles_on_other_discs_unchanged(self):
        items, _ = plan_userfile_membership_write("Rusty / Fluke", 0x17)
        self.assertEqual(items, [(0, "Rusty / Fluke", T.DISC_NAMES, "Disc Name (userfiles only)",
                                  0x17)])
        _, error = plan_userfile_membership_write(None, 0x17)
        self.assertIn("no name stored", error)

    def test_missing_state_asks_for_track1(self):
        self.assertEqual(missing_disc_state(4, {}, {4: 0}, {4: 0}, need_name=True,
                                            track_names={}, cdtext=True), ["track 1's name"])
        self.assertEqual(missing_disc_state(4, {}, {4: 0}, {4: 0}, need_name=True,
                                            track_names={4: {1: "Silent Night"}}, cdtext=True), [])
        self.assertEqual(missing_disc_state(4, {}, {4: 0}, {4: 0}, need_name=True), ["disc name"])


class TestLibraryUserfileCheck(unittest.TestCase):
    # 16:39:17: slot 4 read while in the drive.
    STATE = {"track_count": 10, "name": None, "tracks": {1: "Silent Night", 2: "O Holy Night"},
             "genre": SOUNDTRACK, "userfiles": 0x00, "cdtext": True}

    def test_recognized_by_track1(self):
        mask, why = library_browser.userfile_change(4, None, self.STATE, 1, True,
                                                    {"1": "Silent Night"})
        self.assertEqual((mask, why), (0x01, ""))

    def test_cut_and_full_title_match(self):
        state = dict(self.STATE, tracks={1: "We Wish You A Merry Christmas"})
        mask, _ = library_browser.userfile_change(4, None, state, 2, True,
                                                  {"1": "We Wish You A Merry Chris"})
        self.assertEqual(mask, 0x02)

    def test_different_disc(self):
        mask, why = library_browser.userfile_change(4, None, self.STATE, 1, True,
                                                    {"1": "Groovy Dead"})
        self.assertIsNone(mask)
        self.assertIn("track 1", why)

    def test_other_disc_without_a_name_still_refused(self):
        mask, why = library_browser.userfile_change(4, None, dict(self.STATE, cdtext=False), 1,
                                                    True, {"1": "Silent Night"})
        self.assertIsNone(mask)
        self.assertIn("disc name", why)


class TestRestore(unittest.TestCase):
    SAVED = {"slot": 4, "track_count": 10, "name": "Acoustic Christmas Celebr",
             "tracks": {1: "Name 1", 2: "Name 2"}, "genre": CHRISTIAN, "userfiles": 0x00}
    CURRENT = {"track_count": 10, "name": None, "tracks": {1: "Silent Night", 2: "O Holy Night"},
               "genre": SOUNDTRACK, "userfiles": 0x00, "cdtext": True}

    def test_names_left_alone_genre_on_track1(self):
        items, mask, reason = library_backup.plan_disc_restore(self.SAVED, self.CURRENT)
        self.assertIsNone(reason)
        self.assertEqual(mask, 0x00)
        self.assertEqual(items, [(1, "Silent Night", T.TRACK_NAMES, "Track 1 (genre/userfiles)",
                                  CHRISTIAN)])

    def test_verified_when_genre_and_userfiles_match(self):
        items, _, reason = library_backup.plan_disc_restore(
            self.SAVED, dict(self.CURRENT, genre=CHRISTIAN))
        self.assertEqual((items, reason), ([], None))

    def test_same_backup_on_a_non_cdtext_disc_writes_names(self):
        items, _, _ = library_backup.plan_disc_restore(self.SAVED, dict(self.CURRENT, cdtext=False))
        self.assertEqual([i[3] for i in items], ["Disc Name", "Track 1", "Track 2"])

    def test_disc_record_takes_the_flag(self):
        rec = library_backup.disc_record(4, 10, None, {1: "Silent Night"}, SOUNDTRACK, 0, cdtext=True)
        self.assertNotIn("cdtext", rec)


class TestDiscDataTab(_AppCase):
    def show(self, slot=4):
        self.app._current_slot = slot
        self.app._disc_track_count[slot] = 2
        self.app._refresh_disc_data_from_changer()
        self.drain()

    def test_custom_entries_greyed_out(self):
        self.app._cdtext_slots.add(4)
        self.show()
        self.assertEqual({str(r["entry"].cget("state")) for r in self.app._disc_data_rows},
                         {"disabled"})
        self.assertIn("CD-Text", self.app.disc_data_summary.cget("text"))
        self.app._cdtext_slots.clear()
        self.show()
        self.assertEqual({str(r["entry"].cget("state")) for r in self.app._disc_data_rows},
                         {"normal"})

    def prime(self, genre_choice=""):
        self.app.link = _FakeLink()
        self.app._cdtext_slots.add(4)
        self.app._disc_data_slot = 4
        self.app._genre_cache[4] = CHRISTIAN
        self.app._userfiles_cache[4] = 0x00
        self.app._track_name_cache[4] = {1: "Silent Night"}
        self.app._set_disc_data_row_count(4, 2)
        self.app._disc_data_rows[0]["custom_var"].set("test name")
        self.app.genre_custom_var.set(genre_choice)

    @mock.patch.object(app_mod.messagebox, "askyesno")
    @mock.patch.object(app_mod.messagebox, "showinfo")
    def test_names_only_writes_nothing(self, showinfo, ask):
        self.prime()
        with mock.patch.object(app_mod.threading, "Thread", _FakeThread):
            self.app._write_to_changer()
        self.assertEqual(_FakeThread.started, [])
        ask.assert_not_called()
        self.assertIn("CD-Text", showinfo.call_args[0][1])

    @mock.patch.object(app_mod.messagebox, "askyesno", return_value=True)
    def test_genre_goes_on_track1(self, _ask):
        self.prime("Soundtrack")
        with mock.patch.object(app_mod.threading, "Thread", _FakeThread):
            self.app._write_to_changer()
        (writer,) = _FakeThread.started
        self.assertEqual(writer.target, self.app._write_to_changer_worker)
        self.assertEqual(writer.args[2],
                         [(1, "Silent Night", T.TRACK_NAMES, "Track 1 (genre)", SOUNDTRACK)])
        self.assertEqual(writer.kwargs["userfiles"], 0x00)

        link = _FakeLink()
        self.app._write_to_changer_worker(link, 4, writer.args[2], userfiles=0x00)
        (_cmd, request, _fcmd, follow_up), = link.send_write_calls
        self.assertEqual(request, proto.encode_data_access(
            proto.Action.WRITE_NAME, proto.DataType.TEXT_DATA, slot=4, info_type=T.TRACK_NAMES))
        sent = proto.decode_text_data(follow_up)
        self.assertEqual((sent["index"], sent["text"], sent["genre"]), (1, "Silent Night", SOUNDTRACK))


class TestUserfilesTabInTheDrive(_AppCase):
    """16:40:12: the Userfiles tab's write on slot 4, in the drive, with its
    disc name unknown. Before v1.12.11 it stopped there."""

    @mock.patch.object(app_mod.messagebox, "askyesno", return_value=True)
    def test_reads_track1_after_the_stream_and_writes_on_it(self, _ask):
        app = self.app

        def on_send(_command, data):
            if data[1] == proto.DataType.TEXT_DATA and data[4] == 0:
                raise PCLinkTextStream("slot 4 streamed")
            if data[1] == proto.DataType.TEXT_DATA and data[4] == 1:
                app._on_frame(_frame(LONG_TRACK1_SLOT4))
        app.link = _FakeLink(on_send)
        app._genre_cache[4] = SOUNDTRACK
        app._userfiles_cache[4] = 0x00
        app.uf_member_slot_var.set(4)
        app.uf_member_vars[0].set(True)
        with mock.patch.object(app_mod.threading, "Thread", _FakeThread):
            app._write_userfile_membership()
            (prefetch,) = _FakeThread.started
            prefetch.target()
            asked = [(d[1], d[4], d[5]) for _c, d in app.link.send_calls]
            self.assertEqual(asked, [(proto.DataType.TEXT_DATA, 0, T.DISC_NAMES),
                                     (proto.DataType.TEXT_DATA, 1, T.TRACK_NAMES)])
            self.drain()
        writer = _FakeThread.started[-1]
        self.assertEqual(writer.target, app._write_to_changer_worker)
        self.assertEqual(writer.args[2], [(1, "Silent Night", T.TRACK_NAMES,
                                           "Track 1 (userfiles only)", SOUNDTRACK)])
        self.assertEqual(writer.kwargs["userfiles"], 0x01)


class TestConfirmedOnHardware(unittest.TestCase):
    """The 17:07-17:09 log (v1.12.11 on the real CD-425M, slot 4 in the
    drive): the frames the app sent, and what the changer read back."""

    # 17:07:47, Userfiles tab: userfiles 0x00 -> 0x07 on track 1.
    USERFILES_TX = "02 fe 13 00 04 00 01 07 01 1a 00 53 69 6c 65 6e 74 20 4e 69 67 68 74 3f"
    # 17:09:10, Disc Data tab: genre Soundtrack -> Country on track 1.
    GENRE_TX = "02 fe 13 00 04 00 01 07 01 07 00 53 69 6c 65 6e 74 20 4e 69 67 68 74 52"
    USERFILES_AFTER = "02 07 03 00 04 00 07 eb"          # 17:08:05
    GENRE_AFTER = "02 08 03 00 04 00 07 ea"              # 17:09:18
    USERFILES_AFTER_GENRE = "02 07 03 00 04 00 07 eb"    # 17:09:18

    def test_userfiles_write_frame(self):
        (item,), _ = plan_userfile_membership_write(None, SOUNDTRACK, True, "Silent Night")
        index, text, info_type, _label, genre = item
        sent = proto.encode_text_data(slot=4, index=index, text=text, info_type=info_type,
                                      genre=genre, userfiles=0x07)
        self.assertEqual(sent, _frame(self.USERFILES_TX).data)
        self.assertEqual(_frame(self.USERFILES_AFTER).payload["userfiles"], 0x07)

    def test_library_userfile_frame(self):
        # 17:13:46, Library menu: slot 4 taken out of #2 (0x07 -> 0x05),
        # matched to the scan by track 1 (no readable disc name in the drive).
        state = {"track_count": 10, "name": None, "tracks": {1: "Silent Night"},
                 "genre": 0x07, "userfiles": 0x07, "cdtext": True}
        mask, why = library_browser.userfile_change(4, None, state, 2, False, {"1": "Silent Night"})
        self.assertEqual((mask, why), (0x05, ""))
        (item,), _ = plan_userfile_membership_write(None, 0x07, True, "Silent Night")
        sent = proto.encode_text_data(slot=4, index=item[0], text=item[1], info_type=item[2],
                                      genre=item[4], userfiles=mask)
        self.assertEqual(sent, _frame("02 fe 13 00 04 00 01 05 01 07 00 53 69 6c 65 6e 74 20 4e "
                                      "69 67 68 74 54").data)
        self.assertEqual(_frame("02 07 03 00 04 00 05 ed").payload["userfiles"], 0x05)  # 17:14:05

    def test_restore_frame(self):
        # 17:25:13: restore of the 17:16 backup (Country, #1+#3) after the
        # genre was changed to Easy Listening (0x09) at 17:24:10. One write,
        # on track 1; no names; then "verified".
        saved = {"slot": 4, "track_count": 10, "name": None,
                 "tracks": {1: "Silent Night", 3: "We Wish You A Merry Christmas"},
                 "genre": 0x07, "userfiles": 0x05}
        current = {"track_count": 10, "name": None,
                   "tracks": {1: "Silent Night", 3: "We Wish You A Merry Christmas"},
                   "genre": 0x09, "userfiles": 0x05, "cdtext": True}
        (item,), mask, reason = library_backup.plan_disc_restore(saved, current)
        self.assertIsNone(reason)
        sent = proto.encode_text_data(slot=4, index=item[0], text=item[1], info_type=item[2],
                                      genre=item[4], userfiles=mask)
        self.assertEqual(sent, _frame("02 fe 13 00 04 00 01 05 01 07 00 53 69 6c 65 6e 74 20 4e "
                                      "69 67 68 74 54").data)
        after = dict(current, genre=_frame("02 08 03 00 04 00 07 ea").payload["genre"])  # 17:25:34
        self.assertEqual(library_backup.plan_disc_restore(saved, after), ([], 0x05, None))

    def test_genre_write_frame(self):
        (item,), _ = plan_cdtext_disc_data_write([], 0x07, "Silent Night")
        index, text, info_type, _label, genre = item
        sent = proto.encode_text_data(slot=4, index=index, text=text, info_type=info_type,
                                      genre=genre, userfiles=0x07)
        self.assertEqual(sent, _frame(self.GENRE_TX).data)
        self.assertEqual(_frame(self.GENRE_AFTER).payload["genre"], 0x07)
        self.assertEqual(_frame(self.USERFILES_AFTER_GENRE).payload["userfiles"], 0x07)


class TestSlotReadInTheDrive(_AppCase):
    """A Library/Backup slot read of slot 4 in the drive carries `cdtext`,
    and the Library's userfile check then goes ahead on track 1."""

    def test_state_and_userfile_change(self):
        app = self.app

        def on_send(_command, data):
            data_type, track = data[1], data[4]
            if data_type == proto.DataType.DISC_INFO:
                app._on_frame(_frame(DISCINFO_SLOT4))
            elif data_type == proto.DataType.TEXT_DATA and track == 0:
                raise PCLinkTextStream("slot 4 streamed")
            elif data_type == proto.DataType.TEXT_DATA and track == 1:
                app._on_frame(_frame(LONG_TRACK1_SLOT4))
            elif data_type == proto.DataType.DISC_GENRE:
                app._on_frame(_frame(GENRE_SLOT4))
            elif data_type == proto.DataType.DISC_USERFILES:
                app._on_frame(_frame(USERFILES_SLOT4))
        state = app._read_slot_state_sync(_FakeLink(on_send), 4)
        self.assertTrue(state["cdtext"])
        self.assertIsNone(state["name"])
        self.assertEqual(state["tracks"], {1: "Silent Night"})
        mask, why = library_browser.userfile_change(4, None, state, 1, True, {"1": "Silent Night"})
        self.assertEqual((mask, why), (0x01, ""))
        items, error = plan_userfile_membership_write(state["name"], state["genre"],
                                                      state["cdtext"], state["tracks"].get(1))
        self.assertIsNone(error)
        self.assertEqual(items[0][:3], (1, "Silent Night", T.TRACK_NAMES))


if __name__ == "__main__":
    unittest.main()
