#!/usr/bin/env python3
"""
test_library_browser.py
=======================
Tests for the Library tab (v1.11.0): browse every disc, search disc and
track names, filter by genre/userfile, play a disc, and keep the last
scan in library_cache.json. CONFIRMED on real hardware (v1.11.1).

Stdlib-only (unittest), matching the rest of this project.

What's being tested:
  - Searching, filtering, sorting and the table rows (library_browser.py),
    on discs from the user's real backups and logs (TestSearchAndFilter,
    TestSortAndRows).
  - The cache file and "Rescan Disc"'s replace (TestCacheFile).
  - The tab itself: a scan against test_library_backup's simulated
    changer, the export refreshing it, the cache loading on the next
    launch, opening a backup file, the filters and search in the table,
    and Play / Load in Disc Data Tab sending ChangeDisc (TestLibraryTab).
  - Adding a disc to a userfile or taking it out (v1.12.0, CONFIRMED in
    v1.12.1): the fresh read first, the write, and every reason not to
    write (TestUserfileChange, TestLibraryUserfiles).
  - The Userfiles & Program tab filling in from the saved scan (v1.12.1,
    CONFIRMED on real hardware; TestUserfilesTabUsesTheSavedScan).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

import test_library_backup as tlb  # installs the fake serial module first
import library_backup as lb
import library_browser as lbr
import pclink_app as app_mod
import pclink_protocol as proto

ROCK, ALT = tlb.ROCK, tlb.ALT

# The user's discs as a v1.11.0 scan would save them: names, genres and
# userfiles from the v1.10.x backups and logs, track names from the
# v1.10.2 restore (slot 1) and the v1.10.0 backup (slot 3). Slots 2 and 3
# hadn't been played since power-on (99 -> null track count).
REAL_RAW = lb.build_library(
    [
        lb.disc_record(1, 12, "The Tragically Hip / Trou",
                       {1: "Gift Shop", 4: "Don't Wake Daddy", 7: "Butts Wigglin", 12: "Put It Off"},
                       ALT, 0x02),
        lb.disc_record(2, 99, "Limblifter-Bellaclava", {1: "Count To 9"}, ROCK, 0x04),
        lb.disc_record(3, 99, "Rusty / Fluke", {1: "Groovy Dead", 10: "Ceiling"}, ROCK, 0x01),
    ],
    {0x01: "New Name", 0x02: "BALLS FART", 0x04: "My List"}, None,
    "1.11.0", "2026-09-24T12:30:00",
)


def _real():
    return lbr.load_library_text(lb.library_to_json(REAL_RAW))


def _slots(discs):
    return [d["slot"] for d in discs]


class TestSearchAndFilter(unittest.TestCase):
    def setUp(self):
        self.discs = _real()["discs"]

    def test_empty_search_shows_everything(self):
        self.assertEqual(_slots(lbr.filter_discs(self.discs, "  ")), [1, 2, 3])

    def test_words_can_come_from_the_name_and_a_track(self):
        self.assertEqual(_slots(lbr.filter_discs(self.discs, "hip gift")), [1])
        self.assertEqual(_slots(lbr.filter_discs(self.discs, "hip ceiling")), [])

    def test_case_insensitive_substring(self):
        self.assertEqual(_slots(lbr.filter_discs(self.discs, "CEIL")), [3])

    def test_genre_is_searchable(self):
        self.assertEqual(_slots(lbr.filter_discs(self.discs, "alternative")), [1])
        self.assertEqual(_slots(lbr.filter_discs(self.discs, "rock")), [1, 2, 3])

    def test_bare_number_matches_the_slot(self):
        self.assertEqual(_slots(lbr.filter_discs(self.discs, "3")), [3])

    def test_matching_tracks_for_highlighting(self):
        hip = self.discs[0]
        self.assertEqual(lbr.matching_tracks(hip, "wigg"), {7})
        self.assertEqual(lbr.matching_tracks(hip, "hip"), set())  # the name isn't a track
        self.assertEqual(lbr.matching_tracks(hip, ""), set())

    def test_genre_filter(self):
        self.assertEqual(_slots(lbr.filter_discs(self.discs, genre=ROCK)), [2, 3])

    def test_userfile_filter_is_by_number(self):
        self.assertEqual(_slots(lbr.filter_discs(self.discs, userfile=2)), [1])  # mask 0x02
        self.assertEqual(_slots(lbr.filter_discs(self.discs, userfile=3)), [2])  # mask 0x04

    def test_filters_and_search_combine(self):
        self.assertEqual(_slots(lbr.filter_discs(self.discs, "groovy", genre=ROCK, userfile=1)), [3])
        self.assertEqual(_slots(lbr.filter_discs(self.discs, "groovy", userfile=2)), [])

    def test_unread_values_dont_match_a_filter(self):
        unread = {"slot": 9, "name": None, "genre": None, "userfiles": None, "tracks": None,
                  "track_count": 5}
        self.assertEqual(lbr.filter_discs([unread], genre=ROCK), [])
        self.assertEqual(lbr.filter_discs([unread], userfile=1), [])
        self.assertEqual(lbr.filter_discs([unread], ""), [unread])
        self.assertEqual(lbr.filter_discs([unread], "x"), [])


class TestSortAndRows(unittest.TestCase):
    def setUp(self):
        self.discs = _real()["discs"]

    def test_sort_by_name_both_ways(self):
        self.assertEqual(_slots(lbr.sort_discs(self.discs, "name")), [2, 3, 1])
        self.assertEqual(_slots(lbr.sort_discs(self.discs, "name", reverse=True)), [1, 3, 2])

    def test_missing_values_go_last_either_way(self):
        # Slots 2 and 3's track counts aren't known.
        self.assertEqual(_slots(lbr.sort_discs(self.discs, "tracks")), [1, 2, 3])
        self.assertEqual(_slots(lbr.sort_discs(self.discs, "tracks", reverse=True)), [1, 2, 3])
        noname = dict(self.discs[0], slot=5, name="")
        self.assertEqual(_slots(lbr.sort_discs([noname, *self.discs], "name")), [2, 3, 1, 5])

    def test_ties_stay_in_slot_order(self):
        self.assertEqual(_slots(lbr.sort_discs(self.discs, "genre")), [1, 2, 3])
        self.assertEqual(_slots(lbr.sort_discs(self.discs, "genre", reverse=True)), [2, 3, 1])

    def test_sort_by_userfiles_and_slot(self):
        self.assertEqual(_slots(lbr.sort_discs(self.discs, "userfiles")), [3, 1, 2])
        self.assertEqual(_slots(lbr.sort_discs(self.discs, "slot", reverse=True)), [3, 2, 1])

    def test_unknown_sort_key(self):
        with self.assertRaises(ValueError):
            lbr.sort_discs(self.discs, "artist")

    def test_disc_rows(self):
        self.assertEqual(lbr.disc_row(self.discs[0]),
                         ("1", "The Tragically Hip / Trou", "Alternative Rock", "#2", "12"))
        # Unknown count: only the named tracks are known.
        self.assertEqual(lbr.disc_row(self.discs[2])[4], "2?")
        unread = {"slot": 9, "name": None, "genre": None, "userfiles": None, "tracks": None,
                  "track_count": None}
        self.assertEqual(lbr.disc_row(unread), ("9", "?", "?", "?", "?"))
        blank = {"slot": 9, "name": "", "genre": 0, "userfiles": 0, "tracks": {}, "track_count": 4}
        self.assertEqual(lbr.disc_row(blank), ("9", "(no name)", "Unassigned", "", "4"))

    def test_track_rows(self):
        rows = lbr.track_rows(self.discs[0])
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[0], (1, "Gift Shop"))
        self.assertEqual(rows[1], (2, ""))
        # Unknown count: just the named ones.
        self.assertEqual(lbr.track_rows(self.discs[2]), [(1, "Groovy Dead"), (10, "Ceiling")])

    def test_filter_choices(self):
        self.assertEqual(lbr.genre_choices(self.discs), [("Alternative Rock", ALT), ("Rock", ROCK)])
        choices = lbr.userfile_choices(_real()["userfile_names"])
        self.assertEqual(len(choices), 8)
        self.assertEqual(choices[1], ("#2 BALLS FART", 2))
        self.assertEqual(choices[7], ("#8", 8))

    def test_summary_and_when(self):
        self.assertEqual(lbr.summary(self.discs, self.discs), "3 disc(s), 7 named track(s)")
        self.assertEqual(lbr.summary(self.discs[:1], self.discs), "1 of 3 disc(s), 4 named track(s)")
        self.assertEqual(lbr.format_when("2026-09-24T12:30:00"), "2026-09-24 12:30")
        self.assertEqual(lbr.format_when(None), "an unknown date")


class TestCacheFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, lbr.CACHE_FILENAME)

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_and_no_temp_file_left(self):
        lbr.save_cache(self.path, REAL_RAW)
        self.assertEqual(os.listdir(self.tmp.name), [lbr.CACHE_FILENAME])
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(lbr.load_library_text(f.read()), _real())

    def test_cache_is_a_restorable_backup(self):
        lbr.save_cache(self.path, REAL_RAW)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(len(lb.parse_library(f.read())["discs"]), 3)

    def test_replace_disc(self):
        new = lb.disc_record(2, 13, "Limblifter / Bellaclava", {1: "Count To 9"}, ROCK, 0x04)
        replaced = lbr.replace_disc(REAL_RAW, new, 2)
        self.assertEqual(replaced["discs"][1]["name"], "Limblifter / Bellaclava")
        self.assertEqual(REAL_RAW["discs"][1]["name"], "Limblifter-Bellaclava")  # not mutated
        self.assertEqual(_slots(lbr.replace_disc(REAL_RAW, None, 2)["discs"]), [1, 3])
        added = lbr.replace_disc(REAL_RAW, lb.disc_record(150, 4, "New", {}, 0, 0), 150)
        self.assertEqual(_slots(added["discs"]), [1, 2, 3, 150])
        self.assertEqual(added["exported_at"], REAL_RAW["exported_at"])


class _TabCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.tmp.name, lbr.CACHE_FILENAME)
        self.app = self._new_app()
        self.sent = []
        self.app._send_bg = lambda command, data, label, retries=2: self.sent.append((command, data))
        self.errors, self.infos = [], []
        self._showerror = app_mod.messagebox.showerror
        self._showinfo = app_mod.messagebox.showinfo
        app_mod.messagebox.showerror = lambda title, msg, **kw: self.errors.append(msg)
        app_mod.messagebox.showinfo = lambda title, msg, **kw: self.infos.append(msg)
        self._askopen = app_mod.filedialog.askopenfilename

    def tearDown(self):
        app_mod.messagebox.showerror = self._showerror
        app_mod.messagebox.showinfo = self._showinfo
        app_mod.filedialog.askopenfilename = self._askopen
        self.app.destroy()
        self.tmp.cleanup()

    def _new_app(self):
        return app_mod.App(library_cache_path=self.cache)

    def drain(self, app=None):
        app = app or self.app
        while not app.ui_queue.empty():
            app.ui_queue.get_nowait()()

    def scan(self, sim):
        self.app.link = sim
        self.app._library_begin("test", self.app.lib_progress_label)
        self.app._library_scan_worker(sim)
        self.drain()

    def rows(self):
        tree = self.app.lib_disc_tree
        return [tree.item(i, "values") for i in tree.get_children()]

    def select(self, slot, track=None):
        self.app.lib_disc_tree.selection_set(str(slot))
        self.app._browser_show_tracks()
        if track is not None:
            self.app.lib_track_tree.selection_set(str(track))


class TestLibraryTab(_TabCase):
    def test_scan_fills_the_table_and_saves_the_cache(self):
        self.scan(tlb._SimChanger(self.app, tlb._library_discs()))
        self.assertEqual([r[0] for r in self.rows()], ["3", "150", "200"])
        self.assertEqual(self.rows()[0], ("3", "Band / Record", "Rock", "#1, #3", "4"))
        self.assertEqual(self.rows()[1][1], "(no name)")
        self.assertIn("changer scan", self.app.lib_source_label.cget("text"))
        self.assertEqual(self.app.lib_progress_label.cget("text"), "Scan done: 3 disc(s).")
        self.assertFalse(self.app._library_running)
        with open(self.cache, encoding="utf-8") as f:
            self.assertEqual(len(lb.parse_library(f.read())["discs"]), 3)

    def test_next_launch_shows_the_saved_scan(self):
        self.scan(tlb._SimChanger(self.app, tlb._library_discs()))
        second = self._new_app()
        try:
            tree = second.lib_disc_tree
            self.assertEqual(list(tree.get_children()), ["3", "150", "200"])
            self.assertEqual(str(second.lib_rescan_btn.cget("state")), "disabled")  # nothing selected
        finally:
            second.destroy()

    def test_corrupt_cache_is_logged_not_fatal(self):
        with open(self.cache, "w", encoding="utf-8") as f:
            f.write("{not json")
        second = self._new_app()
        try:
            self.assertEqual(second.lib_disc_tree.get_children(), ())
            self.assertIsNone(second._browser_raw)
        finally:
            second.destroy()

    def test_stopped_scan_changes_nothing(self):
        lbr.save_cache(self.cache, REAL_RAW)
        self.app._load_library_cache()
        sim = tlb._SimChanger(self.app, tlb._library_discs())
        self.app.link = sim
        self.app._library_begin("test", self.app.lib_progress_label)
        self.app._library_stop.set()
        self.app._library_scan_worker(sim)
        self.drain()
        self.assertEqual([r[0] for r in self.rows()], ["1", "2", "3"])
        with open(self.cache, encoding="utf-8") as f:
            self.assertEqual(json.load(f), REAL_RAW)

    def test_backup_export_refreshes_the_library(self):
        sim = tlb._SimChanger(self.app, tlb._library_discs())
        self.app.link = sim
        self.app._library_begin("test")
        self.app._library_export_worker(sim, os.path.join(self.tmp.name, "export.json"))
        self.drain()
        self.assertEqual([r[0] for r in self.rows()], ["3", "150", "200"])
        self.assertTrue(os.path.exists(self.cache))
        self.assertEqual(len(self.infos), 1)  # the export's usual "Saved ..." message

    def test_search_and_filters_in_the_table(self):
        lbr.save_cache(self.cache, REAL_RAW)
        self.app._load_library_cache()
        self.app.lib_search_var.set("wigg")
        self.assertEqual([r[0] for r in self.rows()], ["1"])
        self.assertEqual(self.app.lib_count_label.cget("text"), "1 of 3 disc(s), 4 named track(s)")
        self.select(1)
        tracks = self.app.lib_track_tree
        self.assertEqual(tracks.item("7", "tags"), ("match",))
        self.assertEqual(tracks.item("1", "tags"), "")
        self.app._browser_clear_filters()
        self.app.lib_userfile_var.set("#3 My List")
        self.app._browser_refresh()
        self.assertEqual([r[0] for r in self.rows()], ["2"])
        self.app.lib_userfile_var.set("All userfiles")
        self.app.lib_genre_var.set("Rock")
        self.app._browser_refresh()
        self.assertEqual([r[0] for r in self.rows()], ["2", "3"])

    def test_selection_survives_a_new_search(self):
        lbr.save_cache(self.cache, REAL_RAW)
        self.app._load_library_cache()
        self.select(1, track=7)
        self.app.lib_search_var.set("hip")
        self.assertEqual(self.app.lib_disc_tree.selection(), ("1",))
        self.assertEqual(self.app.lib_track_tree.selection(), ("7",))

    def test_heading_click_sorts_then_reverses(self):
        lbr.save_cache(self.cache, REAL_RAW)
        self.app._load_library_cache()
        self.app._browser_sort_by("name")
        self.assertEqual([r[0] for r in self.rows()], ["2", "3", "1"])
        self.assertEqual(self.app.lib_disc_tree.heading("name", "text"), "Disc Name ▲")
        self.app._browser_sort_by("name")
        self.assertEqual([r[0] for r in self.rows()], ["1", "3", "2"])
        self.assertEqual(self.app.lib_disc_tree.heading("slot", "text"), "Slot")

    def test_play_sends_change_disc(self):
        lbr.save_cache(self.cache, REAL_RAW)
        self.app._load_library_cache()
        self.select(3)
        self.app._browser_play()
        self.select(1, track=7)
        self.app._browser_play()
        self.app._browser_play(use_track=False)  # double-click on the disc
        self.assertEqual(self.sent, [
            (proto.CMD_CHANGE_DISC, proto.encode_change_disc(3, 1, begin=True)),
            (proto.CMD_CHANGE_DISC, proto.encode_change_disc(1, 7, begin=True)),
            (proto.CMD_CHANGE_DISC, proto.encode_change_disc(1, 1, begin=True)),
        ])

    def test_load_in_disc_data_tab(self):
        lbr.save_cache(self.cache, REAL_RAW)
        self.app._load_library_cache()
        self.select(2)
        self.app._browser_open_in_disc_data()  # not connected
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.errors), 1)
        self.app.link = tlb._SimChanger(self.app)
        self.app._browser_open_in_disc_data()
        self.assertEqual(self.sent, [(proto.CMD_CHANGE_DISC, proto.encode_change_disc(2, 1, begin=True))])
        self.assertEqual(self.app.notebook.select(), str(self.app.disc_data_tab))

    def test_open_backup_then_back_to_the_last_scan(self):
        self.scan(tlb._SimChanger(self.app, tlb._library_discs()))
        backup = os.path.join(self.tmp.name, "old-backup.json")
        with open(backup, "w", encoding="utf-8") as f:
            f.write(lb.library_to_json(REAL_RAW))
        app_mod.filedialog.askopenfilename = lambda **kw: backup
        self.app._open_browser_backup()
        self.assertEqual([r[0] for r in self.rows()], ["1", "2", "3"])
        self.assertIn("old-backup.json", self.app.lib_source_label.cget("text"))
        self.select(1)
        self.assertEqual(str(self.app.lib_rescan_btn.cget("state")), "disabled")
        self.assertEqual(str(self.app.lib_last_scan_btn.cget("state")), "normal")
        self.app._show_last_scan()
        self.assertEqual([r[0] for r in self.rows()], ["3", "150", "200"])
        # Opening a backup never touches the saved scan.
        with open(self.cache, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["discs"]), 3)
            f.seek(0)
            self.assertNotIn("Tragically", f.read())

    def test_bad_backup_file_is_reported(self):
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write('{"format": "something else"}')
        app_mod.filedialog.askopenfilename = lambda **kw: bad
        self.app._open_browser_backup()
        self.assertEqual(self.errors, ["This isn't a Ken Changer library backup."])
        self.assertIsNone(self.app._browser_library)

    def test_rescan_disc_updates_one_slot(self):
        discs = tlb._library_discs()
        sim = tlb._SimChanger(self.app, discs)
        self.scan(sim)
        discs[3]["name"] = "Band / Renamed"
        self.select(3)
        self.assertEqual(str(self.app.lib_rescan_btn.cget("state")), "normal")
        self.app._library_begin("test", self.app.lib_progress_label)
        self.app._library_rescan_worker(sim, 3)
        self.drain()
        self.assertEqual(self.rows()[0][1], "Band / Renamed")
        self.assertEqual(self.app.lib_disc_tree.selection(), ("3",))
        with open(self.cache, encoding="utf-8") as f:
            self.assertIn("Band / Renamed", f.read())

    def test_rescan_of_an_emptied_slot_removes_it(self):
        discs = tlb._library_discs()
        sim = tlb._SimChanger(self.app, discs)
        self.scan(sim)
        del discs[150]
        self.app._library_begin("test", self.app.lib_progress_label)
        self.app._library_rescan_worker(sim, 150)
        self.drain()
        self.assertEqual([r[0] for r in self.rows()], ["3", "200"])

    def test_rescan_that_cant_read_changes_nothing(self):
        discs = tlb._library_discs()
        sim = tlb._SimChanger(self.app, discs)
        self.scan(sim)
        sim.fail.add((proto.DataType.DISC_INFO, 3))
        self.app._library_begin("test", self.app.lib_progress_label)
        self.app._library_rescan_worker(sim, 3)
        self.drain()
        self.assertEqual(self.rows()[0][1], "Band / Record")
        self.assertIn("couldn't read slot 3", self.app.lib_progress_label.cget("text"))


class TestUserfileChange(unittest.TestCase):
    STATE = {"track_count": 4, "name": "Band / Record", "tracks": {}, "genre": ROCK,
             "userfiles": 0x05}

    def change(self, number=2, member=True, scanned="Band / Record", **state):
        return lbr.userfile_change(3, scanned, dict(self.STATE, **state), number, member)

    def test_add_and_remove(self):
        self.assertEqual(self.change(2, True), (0x07, ""))
        self.assertEqual(self.change(1, False), (0x04, ""))
        self.assertEqual(self.change(8, True, userfiles=0), (0x80, ""))

    def test_nothing_to_do(self):
        self.assertEqual(self.change(1, True), (None, "Slot 3 is already in userfile #1."))
        self.assertEqual(self.change(2, False), (None, "Slot 3 isn't in userfile #2."))

    def test_a_different_disc_is_not_written(self):
        mask, why = self.change(name="Someone Else")
        self.assertIsNone(mask)
        self.assertIn("now holds 'Someone Else', not 'Band / Record'", why)

    def test_unreadable_or_empty_slot(self):
        for state, text in (({"track_count": None}, "Couldn't read slot 3"),
                            ({"track_count": 0}, "Slot 3 is empty now"),
                            ({"name": None}, "disc name"),
                            ({"userfiles": None}, "userfiles")):
            mask, why = self.change(**state)
            self.assertIsNone(mask)
            self.assertIn(text, why)


class _RunNow:
    """Stands in for threading.Thread: runs the worker at once."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


class TestLibraryUserfiles(_TabCase):
    def setUp(self):
        super().setUp()
        self.answers = []
        self._askyesno = app_mod.messagebox.askyesno
        self._thread = app_mod.threading.Thread
        app_mod.messagebox.askyesno = lambda title, msg, **kw: (self.answers.append(msg), True)[1]
        app_mod.threading.Thread = _RunNow
        self.discs = tlb._library_discs()
        self.sim = tlb._SimChanger(self.app, self.discs)
        self.scan(self.sim)

    def tearDown(self):
        app_mod.messagebox.askyesno = self._askyesno
        app_mod.threading.Thread = self._thread
        super().tearDown()

    def set_userfile(self, slot, number, member):
        self.select(slot)
        self.app._browser_set_userfile(number, member)
        self.drain()

    def test_add_to_a_userfile(self):
        self.set_userfile(3, 2, True)
        (write,) = self.sim.writes
        self.assertEqual((write["slot"], write["info_type"], write["text"], write["genre"],
                          write["userfiles"]),
                         (3, proto.InfoType.DISC_NAMES, "Band / Record", ROCK, 0x07))
        self.assertEqual(self.rows()[0][3], "#1, #2, #3")
        self.assertEqual(self.app.lib_disc_tree.selection(), ("3",))
        self.assertEqual(self.app.lib_progress_label.cget("text"), "Userfiles: slot 3 added to #2.")
        self.assertEqual(self.infos, [])
        self.assertIn("Add slot 3 (Band / Record) to userfile #2", self.answers[0])
        with open(self.cache, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["discs"][0]["userfiles"], [1, 2, 3])
        self.assertFalse(self.app._library_running)

    def test_take_out_of_a_userfile(self):
        self.set_userfile(200, 8, False)
        self.assertEqual(self.sim.writes[0]["userfiles"], 0x00)
        self.assertEqual(self.discs[200]["genre"], tlb.FOLK)
        self.assertEqual(self.rows()[2][3], "")

    def test_declined_writes_nothing(self):
        app_mod.messagebox.askyesno = lambda title, msg, **kw: False
        self.set_userfile(3, 2, True)
        self.assertEqual(self.sim.writes, [])
        self.assertFalse(self.app._library_running)

    def test_disc_changed_since_the_scan_is_not_written(self):
        self.discs[3]["name"] = "Someone Else"
        self.set_userfile(3, 2, True)
        self.assertEqual(self.sim.writes, [])
        self.assertEqual(self.rows()[0][1], "Someone Else")  # the library caught up
        self.assertIn("now holds 'Someone Else'", self.infos[0])

    def test_disc_with_no_name_is_not_written(self):
        self.set_userfile(150, 1, True)
        self.assertEqual(self.sim.writes, [])
        self.assertIn("no name stored", self.infos[0])

    def test_already_in_it_per_the_changer(self):
        self.discs[3]["userfiles"] = 0x07  # added from the remote since the scan
        self.set_userfile(3, 2, True)
        self.assertEqual(self.sim.writes, [])
        self.assertEqual(self.infos, ["Slot 3 is already in userfile #2."])
        self.assertEqual(self.rows()[0][3], "#1, #2, #3")

    def test_write_that_doesnt_stick_is_reported(self):
        self.sim.send_write = lambda *a, **kw: None  # ACK'd but not stored
        self.set_userfile(3, 2, True)
        self.assertIn("instead of 0x07", self.infos[0])

    def test_menu_ticks_the_discs_userfiles(self):
        self.select(3)
        self.app._browser_fill_userfile_menu()
        self.assertEqual(self.app.lib_userfile_menu.index("end"), 7)
        self.assertEqual([v.get() for v in self.app._browser_userfile_vars],
                         [True, False, True, False, False, False, False, False])

    def test_not_while_a_backup_file_is_shown(self):
        backup = os.path.join(self.tmp.name, "old.json")
        with open(backup, "w", encoding="utf-8") as f:
            f.write(lb.library_to_json(REAL_RAW))
        app_mod.filedialog.askopenfilename = lambda **kw: backup
        self.app._open_browser_backup()
        self.select(1)
        self.assertEqual(str(self.app.lib_userfile_btn.cget("state")), "disabled")
        self.app._browser_set_userfile(3, True)
        self.assertEqual(self.sim.writes, [])

    def test_sends_the_confirmed_membership_frame(self):
        # v1.8.1 on the real CD-425M: slot 1 set to userfiles 0x07 by this
        # exact frame, ACK'd and read back. Adding #3 to a disc in #1 and
        # #2 is the same write.
        self.discs[1] = {"count": 12, "name": "The Tragically Hip / Trou", "titles": {},
                         "genre": 0x03, "userfiles": 0x03}
        self.scan(self.sim)
        sent = []
        write = self.sim.send_write

        def record(command, data, follow_up_command, follow_up_data, timeout=8.0):
            sent.append((data, follow_up_command, follow_up_data))
            write(command, data, follow_up_command, follow_up_data)
        self.sim.send_write = record
        self.set_userfile(1, 3, True)
        ((req, cmd, fu),) = sent
        self.assertEqual(proto.encode_frame(proto.CMD_DATA_ACCESS, req).hex(" "),
                         "02 03 07 00 80 01 01 00 00 00 00 74")
        self.assertEqual(proto.encode_frame(cmd, fu).hex(" "),
                         "02 fe 20 00 01 00 00 07 00 03 00 54 68 65 20 54 72 61 67 69 63 61 6c 6c"
                         " 79 20 48 69 70 20 2f 20 54 72 6f 75 30")


class TestUserfilesTabUsesTheSavedScan(_TabCase):
    """v1.12.1: the Userfiles & Program tab showed nothing until every disc
    was re-read from the changer, though the Library's saved scan had it all
    (user report, 2026-09-24). The scan now fills in the gaps, marked "*"."""

    def load(self):
        lbr.save_cache(self.cache, REAL_RAW)
        self.app._load_library_cache()

    def uf_rows(self):
        return [self.app.uf_tree.item(str(n), "values") for n in range(1, 9)]

    def test_rows_from_the_scan_are_marked(self):
        rows = app_mod.userfile_rows({}, {}, {}, _real())
        self.assertEqual(rows[0], ("#1", "New Name", "3 (Rusty / Fluke)*"))
        self.assertEqual(rows[1], ("#2", "BALLS FART", "1 (The Tragically Hip / Trou)*"))
        self.assertEqual(rows[2], ("#3", "My List", "2 (Limblifter-Bellaclava)*"))
        self.assertEqual(rows[3], ("#4", "-", "-"))

    def test_what_the_changer_reported_wins(self):
        # Slot 1 read this session as 0x06 (#2 and #3), the logged re-read.
        rows = app_mod.userfile_rows({1: 0x06}, {0x04: "Renamed"}, {}, _real())
        self.assertEqual(rows[1][2], "1 (The Tragically Hip / Trou)")
        self.assertEqual(rows[2], ("#3", "Renamed",
                                   "1 (The Tragically Hip / Trou), 2 (Limblifter-Bellaclava)*"))
        self.assertEqual(app_mod.scan_only_slots({1: 0x06}, _real()), {2, 3})

    def test_no_scan_is_unchanged(self):
        self.assertEqual(app_mod.userfile_rows({3: 0x01}, {}, {3: "X"})[0], ("#1", "-", "3 (X)"))

    def test_tab_fills_in_at_launch_and_after_disconnect(self):
        self.load()
        self.assertEqual(self.uf_rows()[1][2], "1 (The Tragically Hip / Trou)*")
        self.assertIn("From the Library tab's scan on 2026-09-24 12:30",
                      self.app.uf_saved_note.cget("text"))
        self.assertEqual(self.app.uf_member_checks[2].cget("text"), "#3 My List")
        self.app._disconnect()
        self.assertEqual(self.uf_rows()[1][2], "1 (The Tragically Hip / Trou)*")

    def test_a_changer_read_replaces_the_scan_entry(self):
        self.load()
        for slot, mask in ((1, 0x02), (2, 0x04), (3, 0x01)):
            self.app._note_userfiles(slot, mask)
        self.drain()
        self.assertNotIn("*", "".join(r[2] for r in self.uf_rows()))
        self.assertEqual(self.app.uf_saved_note.cget("text"), "")

    def test_logged_read_of_the_scans_discs_without_a_disc_map(self):
        # v1.12.1 on the real CD-425M (2026-09-24): straight after
        # connecting, with no Disc Map scan, "Read Userfiles for Known
        # Discs" sent these three frames, and every "*" was replaced.
        self.load()
        sim = tlb._SimChanger(self.app, {
            1: {"count": 12, "name": "The Tragically Hip / Trou", "titles": {}, "genre": ALT,
                "userfiles": 0x02},
            2: {"count": 13, "name": "Limblifter-Bellaclava", "titles": {}, "genre": ROCK,
                "userfiles": 0x04},
            3: {"count": 10, "name": "Rusty / Fluke", "titles": {}, "genre": ROCK,
                "userfiles": 0x01}})
        sent = []
        simulated = sim.send

        def record(command, data, timeout=5.0):
            sent.append(proto.encode_frame(command, data).hex(" "))
            simulated(command, data, timeout)
        sim.send = record
        self.app.link = sim
        thread = app_mod.threading.Thread
        app_mod.threading.Thread = _RunNow
        try:
            self.app._read_userfiles_for_known_discs()
        finally:
            app_mod.threading.Thread = thread
        self.drain()
        self.assertEqual(sent, ["02 03 07 00 00 08 01 00 00 00 00 ed",
                                "02 03 07 00 00 08 02 00 00 00 00 ec",
                                "02 03 07 00 00 08 03 00 00 00 00 eb"])
        self.assertNotIn("*", "".join(r[2] for r in self.uf_rows()))
        self.assertEqual(self.app.uf_saved_note.cget("text"), "")

    def test_known_discs_include_the_scan(self):
        self.load()
        self.assertEqual(self.app._known_disc_slots(), [1, 2, 3])
        self.app._disc_occupancy[2] = False  # the Disc Map found it empty
        self.assertEqual(self.app._known_disc_slots(), [1, 3])


class TestRealLibraryTabSession(_TabCase):
    """Frames from real runs of the Library tab on the CD-425M
    (2026-09-24): an empty slot reporting format 0x90 and the ChangeDisc a
    track double-click sent (v1.11.0), and the userfile writes (v1.12.0)."""

    EMPTY_SLOT_100 = "02 04 05 00 64 00 00 00 90 03"  # count 0, format 0x90
    CHANGE_DISC_1_6 = "02 0b 04 00 01 00 06 01 e9"

    def test_empty_slot_with_format_0x90_is_not_a_disc(self):
        sim = tlb._SimChanger(self.app, tlb._library_discs())
        simulated = sim.send

        def send(command, data, timeout=5.0):
            if data == proto.encode_data_access(proto.Action.RETRIEVE_DATA,
                                                proto.DataType.DISC_INFO, slot=100):
                raw = bytes.fromhex(self.EMPTY_SLOT_100)
                return sim._reply(raw[1], tlb._data(self.EMPTY_SLOT_100))
            return simulated(command, data, timeout)
        sim.send = send
        self.scan(sim)
        self.assertEqual([r[0] for r in self.rows()], ["3", "150", "200"])

    def test_track_double_click_sends_the_logged_change_disc(self):
        lbr.save_cache(self.cache, REAL_RAW)
        self.app._load_library_cache()
        self.select(1, track=6)
        self.app._browser_play(use_track=True)
        frame = bytes.fromhex(self.CHANGE_DISC_1_6)
        self.assertEqual(self.sent, [(frame[1], tlb._data(self.CHANGE_DISC_1_6))])


    def test_logged_add_and_remove_of_userfile_3(self):
        # v1.12.0 on the real CD-425M (2026-09-24): slot 1 was in #2 (0x02).
        # Adding #3 wrote 0x06 and removing it wrote 0x02 again, genre
        # Alternative Rock (0x03) kept; each re-read matched.
        discs = {1: {"count": 12, "name": "The Tragically Hip / Trou", "titles": {},
                     "genre": ALT, "userfiles": 0x02}}
        sim = tlb._SimChanger(self.app, discs)
        self.scan(sim)
        sent = []
        write = sim.send_write

        def record(command, data, follow_up_command, follow_up_data, timeout=8.0):
            sent.append(proto.encode_frame(follow_up_command, follow_up_data).hex(" "))
            write(command, data, follow_up_command, follow_up_data)
        sim.send_write = record
        askyesno, thread = app_mod.messagebox.askyesno, app_mod.threading.Thread
        app_mod.messagebox.askyesno = lambda *a, **kw: True
        app_mod.threading.Thread = _RunNow
        try:
            self.select(1)
            self.app._browser_set_userfile(3, True)
            self.drain()
            self.app._browser_set_userfile(3, False)
            self.drain()
        finally:
            app_mod.messagebox.askyesno, app_mod.threading.Thread = askyesno, thread
        name = "54 68 65 20 54 72 61 67 69 63 61 6c 6c 79 20 48 69 70 20 2f 20 54 72 6f 75"
        self.assertEqual(sent, [f"02 fe 20 00 01 00 00 06 00 03 00 {name} 31",
                                f"02 fe 20 00 01 00 00 02 00 03 00 {name} 35"])
        self.assertEqual(discs[1]["userfiles"], 0x02)
        self.assertEqual(self.app.lib_progress_label.cget("text"), "Userfiles: slot 1 taken out of #3.")

if __name__ == "__main__":
    unittest.main()
