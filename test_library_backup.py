#!/usr/bin/env python3
"""
test_library_backup.py
======================
Tests for the Backup tab (v1.10.0): exporting the changer's library to a
.json backup / .csv catalog, and restoring a backup. NOT yet tried on
real hardware.

Stdlib-only (unittest), matching the rest of this project.

What's being tested:
  - The file format: records, JSON round trip, CSV catalog, and parsing a
    (possibly hand-edited) file back in (TestFileFormat, TestParseErrors).
  - Restore planning: write only what differs, carry the genre and
    userfile mask on every write, and skip a slot that looks like a
    different disc (TestPlanDiscRestore, TestPlanUserfileNames).
  - The export and restore workers end to end against a simulated changer
    (_SimChanger) that answers reads with frames shaped like the real
    changer's and applies writes the way the real one was CONFIRMED to
    (every TextData write sets the disc's genre and userfiles, v1.5.1 /
    v1.8.1) (TestExportWorker, TestRestoreWorker).
  - Export fed the exact frames from real CD-425M logs (TestRealFrames).
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
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

import library_backup as lb
import pclink_app as app_mod
import pclink_protocol as proto
from pclink_link import PCLinkTimeout

ROCK = proto.GENRE_NAME_TO_CODE["Rock"]
FOLK = proto.GENRE_NAME_TO_CODE["Folk"]
ALT = proto.GENRE_NAME_TO_CODE["Alternative Rock"]
PLACEHOLDER = "\x01"  # what the changer sends for "no title stored" (v1.6.7)


def _data(frame_hex):
    """Payload bytes of a logged frame (strip STX, cmd, 2 length bytes, checksum)."""
    return bytes.fromhex(frame_hex)[4:-1]


# -- File format ----------------------------------------------------------

def _sample_library():
    discs = [
        lb.disc_record(7, 3, "Artist / Album", {1: "One", 2: "Two"}, ROCK, 0x05),
        lb.disc_record(2, 2, "", {}, 0, 0),
    ]
    program = [{"slot": 7, "track": 2, "all_tracks": False},
               {"slot": 2, "track": proto.LISTING_ALL_TRACKS, "all_tracks": True}]
    return lb.build_library(discs, {0x01: "Road Trip", 0x04: "Jazz"}, program,
                            "1.10.0", "2026-09-24T12:00:00")


class TestFileFormat(unittest.TestCase):
    def test_record_is_readable(self):
        rec = lb.disc_record(7, 3, "Artist / Album", {2: "Two", 1: "One", 3: ""}, ROCK, 0x05)
        self.assertEqual(rec, {
            "slot": 7, "track_count": 3, "name": "Artist / Album", "genre": "Rock",
            "userfiles": [1, 3], "tracks": {"1": "One", "2": "Two"},
        })

    def test_unread_values_are_null(self):
        rec = lb.disc_record(7, 3, None, None, None, None)
        self.assertIsNone(rec["name"])
        self.assertIsNone(rec["tracks"])
        self.assertIsNone(rec["genre"])
        self.assertIsNone(rec["userfiles"])

    def test_library_sorted_and_keyed_by_userfile_number(self):
        lib = _sample_library()
        self.assertEqual([d["slot"] for d in lib["discs"]], [2, 7])
        # Names are keyed by BIT in the changer (v1.7.1); #3 is bit 0x04.
        self.assertEqual(lib["userfile_names"], {"1": "Road Trip", "3": "Jazz"})
        self.assertEqual(lib["program"], [{"slot": 7, "track": 2}, {"slot": 2, "track": "all"}])
        self.assertEqual(lib["format"], lb.FORMAT)

    def test_json_round_trip(self):
        parsed = lb.parse_library(lb.library_to_json(_sample_library()))
        disc7 = parsed["discs"][1]
        self.assertEqual(disc7, {"slot": 7, "track_count": 3, "name": "Artist / Album",
                                 "genre": ROCK, "userfiles": 0x05, "tracks": {1: "One", 2: "Two"}})
        self.assertEqual(parsed["userfile_names"], {1: "Road Trip", 3: "Jazz"})
        self.assertEqual(parsed["program"], [(7, 2), (2, proto.LISTING_ALL_TRACKS)])
        self.assertEqual(parsed["exported_at"], "2026-09-24T12:00:00")

    def test_nulls_survive_the_round_trip(self):
        lib = lb.build_library([lb.disc_record(4, 9, None, None, None, None)], None, None, "x", "y")
        parsed = lb.parse_library(lb.library_to_json(lib))
        self.assertEqual(parsed["discs"][0], {"slot": 4, "track_count": 9, "name": None,
                                              "genre": None, "userfiles": None, "tracks": None})
        self.assertEqual(parsed["userfile_names"], {})
        self.assertIsNone(parsed["program"])

    def test_csv_one_row_per_track(self):
        rows = lb.library_to_csv(_sample_library()).splitlines()
        self.assertEqual(rows[0], "Slot,Disc Name,Genre,Userfiles,Tracks,Track,Track Name")
        self.assertEqual(rows[1], "2,,Unassigned,,2,,")  # no track names: one row
        self.assertEqual(rows[2], "7,Artist / Album,Rock,#1 Road Trip; #3 Jazz,3,1,One")
        self.assertEqual(rows[3], "7,Artist / Album,Rock,#1 Road Trip; #3 Jazz,3,2,Two")
        self.assertEqual(len(rows), 4)

    def test_hand_edited_text_is_folded_and_disc_name_cut_to_25(self):
        lib = _sample_library()
        lib["discs"][1]["name"] = "Beyoncé / A Very Long Album Title Indeed"
        lib["discs"][1]["tracks"]["1"] = "Don’t"
        parsed = lb.parse_library(json.dumps(lib), fold=app_mod.ascii_fold)
        self.assertEqual(parsed["discs"][1]["name"], "Beyonce / A Very Long Alb")
        self.assertEqual(parsed["discs"][1]["tracks"][1], "Don't")


class TestParseErrors(unittest.TestCase):
    def _bad(self, mutate):
        lib = _sample_library()
        mutate(lib)
        with self.assertRaises(lb.LibraryError):
            lb.parse_library(json.dumps(lib))

    def test_not_json(self):
        with self.assertRaises(lb.LibraryError):
            lb.parse_library("{nope")

    def test_wrong_format(self):
        self._bad(lambda lib: lib.update(format="something-else"))

    def test_future_version(self):
        self._bad(lambda lib: lib.update(format_version=2))

    def test_unknown_genre(self):
        self._bad(lambda lib: lib["discs"][0].update(genre="Polka"))

    def test_duplicate_slot(self):
        self._bad(lambda lib: lib["discs"][0].update(slot=7))

    def test_slot_out_of_range(self):
        self._bad(lambda lib: lib["discs"][0].update(slot=201))

    def test_bad_userfile_number(self):
        self._bad(lambda lib: lib["discs"][0].update(userfiles=[9]))

    def test_bad_track_key(self):
        self._bad(lambda lib: lib["discs"][1]["tracks"].update({"x": "y"}))

    def test_bad_program_step(self):
        self._bad(lambda lib: lib["program"].append({"slot": 1}))


# -- Restore planning -----------------------------------------------------

def _saved(**kw):
    disc = {"slot": 5, "track_count": 3, "name": "Album", "genre": ROCK, "userfiles": 0x02,
            "tracks": {1: "A", 2: "B"}}
    disc.update(kw)
    return disc


def _current(**kw):
    state = {"track_count": 3, "name": "Album", "genre": ROCK, "userfiles": 0x02,
             "tracks": {1: "A", 2: "B"}}
    state.update(kw)
    return state


class TestPlanDiscRestore(unittest.TestCase):
    def test_already_matches(self):
        self.assertEqual(lb.plan_disc_restore(_saved(), _current()), ([], 0x02, None))

    def test_after_power_loss_everything_is_rewritten(self):
        items, mask, reason = lb.plan_disc_restore(
            _saved(), _current(name="", tracks={}, genre=0, userfiles=0))
        self.assertIsNone(reason)
        self.assertEqual(mask, 0x02)
        self.assertEqual(items, [
            (0, "Album", proto.InfoType.DISC_NAMES, "Disc Name", ROCK),
            (1, "A", proto.InfoType.TRACK_NAMES, "Track 1", ROCK),
            (2, "B", proto.InfoType.TRACK_NAMES, "Track 2", ROCK),
        ])

    def test_only_differences_are_written(self):
        items, _, _ = lb.plan_disc_restore(_saved(), _current(tracks={1: "A", 2: "Other"}))
        self.assertEqual([i[:2] for i in items], [(2, "B")])

    def test_every_item_carries_the_saved_genre(self):
        # Every TextData write sets the disc's genre (CONFIRMED v1.5.1).
        items, _, _ = lb.plan_disc_restore(_saved(), _current(name="", genre=FOLK))
        self.assertTrue(items)
        self.assertEqual({i[4] for i in items}, {ROCK})

    def test_nothing_is_erased(self):
        # The changer has a name and a track 3 title the backup lacks.
        items, _, _ = lb.plan_disc_restore(
            _saved(name=None, tracks={}), _current(name="Kept", tracks={3: "Kept too"}))
        self.assertEqual(items, [])

    def test_genre_only_change_rides_on_the_disc_name(self):
        items, mask, _ = lb.plan_disc_restore(_saved(), _current(genre=FOLK, userfiles=0))
        self.assertEqual(items, [(0, "Album", proto.InfoType.DISC_NAMES,
                                  "Disc Name (genre/userfiles)", ROCK)])
        self.assertEqual(mask, 0x02)

    def test_genre_change_uses_current_name_when_backup_has_none(self):
        items, _, _ = lb.plan_disc_restore(_saved(name=None, tracks={}), _current(genre=FOLK))
        self.assertEqual(items[0][:2], (0, "Album"))

    def test_genre_change_with_no_name_anywhere_is_skipped(self):
        items, _, reason = lb.plan_disc_restore(_saved(name="", tracks={}), _current(name="", genre=FOLK))
        self.assertEqual(items, [])
        self.assertIn("no name", reason)

    def test_unsaved_genre_and_userfiles_keep_the_current_ones(self):
        items, mask, _ = lb.plan_disc_restore(_saved(genre=None, userfiles=None),
                                              _current(name="", genre=FOLK, userfiles=0x80))
        self.assertEqual(items[0][4], FOLK)
        self.assertEqual(mask, 0x80)

    def test_unknown_genre_everywhere_is_skipped(self):
        _, _, reason = lb.plan_disc_restore(_saved(genre=None), _current(genre=None))
        self.assertIn("reset", reason)

    def test_different_disc_is_skipped(self):
        items, _, reason = lb.plan_disc_restore(_saved(), _current(track_count=11))
        self.assertEqual(items, [])
        self.assertIn("different disc", reason)

    def test_empty_slot_is_skipped(self):
        _, _, reason = lb.plan_disc_restore(_saved(), _current(track_count=0))
        self.assertIn("empty", reason)

    def test_unreadable_slot_is_skipped(self):
        _, _, reason = lb.plan_disc_restore(_saved(), _current(track_count=None))
        self.assertIn("couldn't read", reason)

    def test_tracks_past_the_disc_are_ignored(self):
        items, _, _ = lb.plan_disc_restore(_saved(tracks={1: "A", 2: "B", 9: "Nine"}), _current())
        self.assertEqual(items, [])


class TestPlanUserfileNames(unittest.TestCase):
    def test_only_differing_names(self):
        saved = {1: "Road Trip", 3: "Jazz", 8: "Last"}
        current_by_bit = {0x01: "Road Trip", 0x04: "Old"}
        self.assertEqual(lb.plan_userfile_names_restore(saved, current_by_bit),
                         [(3, "Jazz"), (8, "Last")])


# -- v1.10.1: the first real export (2026-09-24) ----------------------------

# Two discs from the user's first real backup file, as v1.10.0 wrote it:
# the disc name repeated as track "0", and slot 1 (not played since
# power-on) with track_count 99.
V1_10_0_FILE = {
    "format": "ken-changer-library", "format_version": 1, "app_version": "1.10.0",
    "exported_at": "2026-09-24T08:46:23",
    "userfile_names": {"1": "New Name", "2": "BALLS FART", "3": "My List"},
    "program": [],
    "discs": [
        {"slot": 1, "track_count": 99, "name": "The Tragically Hip / Trou",
         "genre": "Alternative Rock", "userfiles": [2],
         "tracks": {"0": "The Tragically Hip / Trou", "1": "Gift Shoppe", "12": "Put It Off"}},
        {"slot": 3, "track_count": 10, "name": "Rusty / Fluke", "genre": "Rock",
         "userfiles": [1], "tracks": {"0": "Rusty / Fluke", "1": "Groovy Dead", "10": "Ceiling"}},
    ],
}


class TestUnknownTrackCountAndTrackZero(unittest.TestCase):
    def test_99_is_saved_as_unknown(self):
        self.assertIsNone(lb.disc_record(1, 99, "X", {}, 0, 0)["track_count"])
        self.assertEqual(lb.disc_record(1, 12, "X", {}, 0, 0)["track_count"], 12)

    def test_track_zero_is_not_a_track(self):
        rec = lb.disc_record(1, 12, "Album", {0: "Album", 1: "One"}, 0, 0)
        self.assertEqual(rec["tracks"], {"1": "One"})

    def test_v1_10_0_file_still_restores(self):
        parsed = lb.parse_library(json.dumps(V1_10_0_FILE))
        slot1, slot3 = parsed["discs"]
        self.assertIsNone(slot1["track_count"])
        self.assertEqual(slot1["tracks"], {1: "Gift Shoppe", 12: "Put It Off"})
        self.assertEqual(slot3["track_count"], 10)
        self.assertEqual(slot3["tracks"], {1: "Groovy Dead", 10: "Ceiling"})

    def test_unknown_count_in_backup_is_not_a_mismatch(self):
        _, _, reason = lb.plan_disc_restore(_saved(track_count=None), _current(track_count=12))
        self.assertIsNone(reason)

    def test_unknown_count_now_is_not_a_mismatch(self):
        items, _, reason = lb.plan_disc_restore(
            _saved(track_count=12, tracks={1: "A", 12: "Last"}), _current(track_count=99, tracks={}))
        self.assertIsNone(reason)
        # Without a real count, no saved track is filtered out.
        self.assertEqual([i[0] for i in items], [1, 12])

    def test_known_counts_that_differ_still_skip(self):
        _, _, reason = lb.plan_disc_restore(_saved(track_count=13), _current(track_count=12))
        self.assertIn("different disc", reason)


# -- Workers against a simulated changer ----------------------------------

class _SimChanger:
    """A fake PCLinkConnection that answers DataAccess reads the way the
    CD-425M does -- by feeding reply frames to app._on_frame during send(),
    as the real link does -- and applies writes the way the real one was
    CONFIRMED to (the TextData write's genre and userfiles bytes become the
    disc's)."""

    def __init__(self, app, discs=None, userfile_names=None, program=None):
        self.app = app
        # slot -> {"count", "name", "titles": {n: text}, "genre", "userfiles"}
        self.discs = discs or {}
        self.userfile_names = userfile_names or {}  # bit -> name
        self.program = program or []
        self.fail = set()  # (data_type, slot) reads that time out
        self.writes = []  # decoded follow-up payloads

    def _reply(self, command, data):
        self.app._on_frame(proto.Frame(command, data, proto.decode_payload(command, data)))

    def _text(self, slot, index, info_type, text, disc=None):
        disc = disc or {"genre": 0, "userfiles": 0}
        self._reply(proto.CMD_TEXT_DATA, proto.encode_text_data(
            slot=slot, index=index, text=text or PLACEHOLDER, info_type=info_type,
            genre=disc["genre"], userfiles=disc["userfiles"]))

    def send(self, command, data, timeout=5.0):
        if command != proto.CMD_DATA_ACCESS:
            raise AssertionError(f"unexpected plain send of 0x{command:02X}")
        action, data_type, slot, _, info_type, _ = struct.unpack("<BBHBBB", data)
        assert action == proto.Action.RETRIEVE_DATA
        if (data_type, slot) in self.fail:
            raise PCLinkTimeout("simulated")
        disc = self.discs.get(slot)
        if data_type == proto.DataType.DISC_INFO:
            # "reported": what DiscInfo says, e.g. 99 for a disc not played
            # since power-on (CONFIRMED v1.10.1).
            count = disc.get("reported", disc["count"]) if disc else 0
            self._reply(proto.CMD_DISC_INFO, struct.pack("<HBBB", slot, 1 if count else 0, count, 0))
        elif data_type == proto.DataType.TEXT_DATA and info_type == proto.InfoType.USERFILE_NAMES:
            for n in range(8):
                self._text(0, 1 << n, info_type, self.userfile_names.get(1 << n))
        elif data_type == proto.DataType.TEXT_DATA and info_type == proto.InfoType.DISC_NAMES:
            self._text(slot, 0, info_type, disc["name"], disc)
        elif data_type == proto.DataType.TEXT_DATA and info_type == proto.InfoType.TRACK_NAMES:
            # Like the real changer (v1.10.0 log): index 0 repeats the disc
            # name, then one frame per title, 20 of them when it reports 99.
            self._text(slot, 0, info_type, disc["name"], disc)
            reported = disc.get("reported", disc["count"])
            for n in range(1, min(reported, 20) + 1):
                self._text(slot, n, info_type, disc["titles"].get(n), disc)
        elif data_type == proto.DataType.DISC_GENRE:
            self._reply(proto.CMD_DISC_GENRE, proto.encode_disc_genre(slot, disc["genre"]))
        elif data_type == proto.DataType.DISC_USERFILES:
            self._reply(proto.CMD_DISC_USERFILES, proto.encode_disc_userfiles(slot, disc["userfiles"]))
        elif data_type == proto.DataType.DISC_LISTING:
            self._reply(proto.CMD_DISC_LISTING, proto.encode_disc_listing(self.program))
        else:
            raise AssertionError(f"unexpected read {data_type}")

    def send_write(self, command, data, follow_up_command, follow_up_data, timeout=8.0):
        if follow_up_command == proto.CMD_DISC_LISTING:
            self.program = [(i["slot"], i["track"])
                            for i in proto.decode_disc_listing(follow_up_data)["items"]]
            self.writes.append(("program", self.program))
            return
        p = proto.decode_text_data(follow_up_data)
        self.writes.append(p)
        if p["info_type"] == proto.InfoType.USERFILE_NAMES:
            self.userfile_names[p["index"]] = p["text"]
            return
        disc = self.discs[p["slot"]]
        disc["genre"], disc["userfiles"] = p["genre"], p["userfiles"]
        if p["info_type"] == proto.InfoType.DISC_NAMES:
            disc["name"] = p["text"][:25]  # CONFIRMED: the changer keeps the first 25 (v1.8.5)
        else:
            disc["titles"][p["index"]] = p["text"]


def _library_discs():
    return {
        3: {"count": 4, "name": "Band / Record", "titles": {1: "Intro", 4: "Outro"},
            "genre": ROCK, "userfiles": 0x05},
        150: {"count": 12, "name": "", "titles": {}, "genre": 0, "userfiles": 0},
        200: {"count": 2, "name": "Last Slot", "titles": {1: "x", 2: "y"},
              "genre": FOLK, "userfiles": 0x80},
    }


class _AppTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app_mod.App()
        self.app._send_bg = lambda *a, **kw: None
        self.tmp = tempfile.TemporaryDirectory()
        self.messages = []
        self._showinfo = app_mod.messagebox.showinfo
        app_mod.messagebox.showinfo = lambda title, msg, **kw: self.messages.append(msg)

    def tearDown(self):
        app_mod.messagebox.showinfo = self._showinfo
        self.app.destroy()
        self.tmp.cleanup()

    def drain_ui_queue(self):
        while not self.app.ui_queue.empty():
            self.app.ui_queue.get_nowait()()

    def export(self, sim, name="lib.json"):
        path = os.path.join(self.tmp.name, name)
        self.app.link = sim
        self.app._library_begin("test")
        self.app._library_export_worker(sim, path)
        self.drain_ui_queue()
        with open(path, encoding="utf-8") as f:
            return f.read()


class TestExportWorker(_AppTestCase):
    def test_exports_every_disc_userfile_name_and_program(self):
        sim = _SimChanger(self.app, _library_discs(), {0x01: "Road Trip", 0x04: "Jazz"},
                          [(3, 1), (200, proto.LISTING_ALL_TRACKS)])
        lib = json.loads(self.export(sim))
        self.assertEqual(lib["discs"], [
            {"slot": 3, "track_count": 4, "name": "Band / Record", "genre": "Rock",
             "userfiles": [1, 3], "tracks": {"1": "Intro", "4": "Outro"}},
            {"slot": 150, "track_count": 12, "name": "", "genre": "Unassigned",
             "userfiles": [], "tracks": {}},
            {"slot": 200, "track_count": 2, "name": "Last Slot", "genre": "Folk",
             "userfiles": [8], "tracks": {"1": "x", "2": "y"}},
        ])
        self.assertEqual(lib["userfile_names"], {"1": "Road Trip", "3": "Jazz"})
        self.assertEqual(lib["program"], [{"slot": 3, "track": 1}, {"slot": 200, "track": "all"}])
        self.assertEqual(lib["app_version"], app_mod.APP_VERSION)
        self.assertIn("Saved 3 disc(s)", self.messages[-1])
        self.assertFalse(self.app._library_running)

    def test_unplayed_disc_exports_null_count_and_no_track_zero(self):
        discs = _library_discs()
        discs[3]["reported"] = 99  # not played since power-on
        lib = json.loads(self.export(_SimChanger(self.app, discs)))
        disc3 = lib["discs"][0]
        self.assertIsNone(disc3["track_count"])
        self.assertEqual(disc3["tracks"], {"1": "Intro", "4": "Outro"})
        self.assertIn("haven't been played since the changer was switched on", self.messages[-1])
        self.assertNotIn("0", lib["discs"][2]["tracks"])

    def test_export_also_fills_the_disc_map(self):
        self.export(_SimChanger(self.app, _library_discs()))
        self.assertEqual(len(self.app._disc_occupancy), 200)
        self.assertEqual(sorted(s for s, o in self.app._disc_occupancy.items() if o), [3, 150, 200])

    def test_csv_extension_writes_a_catalog(self):
        text = self.export(_SimChanger(self.app, _library_discs()), "lib.csv")
        self.assertTrue(text.startswith("Slot,Disc Name,"))
        self.assertIn("3,Band / Record,Rock,#1; #3,4,4,Outro", text)

    def test_unreadable_value_is_null_not_stale(self):
        sim = _SimChanger(self.app, _library_discs())
        self.app._genre_cache[3] = FOLK  # stale value from earlier in the session
        sim.fail.add((proto.DataType.DISC_GENRE, 3))
        lib = json.loads(self.export(sim))
        self.assertIsNone(lib["discs"][0]["genre"])
        self.assertIn("missing a value", self.messages[-1])

    def test_unreadable_slot_is_reported(self):
        sim = _SimChanger(self.app, _library_discs())
        sim.fail.add((proto.DataType.DISC_INFO, 42))
        self.export(sim)
        self.assertIn("couldn't be read: [42]", self.messages[-1])

    def test_unwritten_program_edits_are_not_discarded(self):
        sim = _SimChanger(self.app, _library_discs(), program=[(3, 1)])
        self.app._program_items = [{"slot": 200, "track": 2, "all_tracks": False}]
        self.app._program_draft = [(3, 3), (3, 4)]
        self.app._program_draft_dirty = True
        lib = json.loads(self.export(sim))
        self.assertEqual(lib["program"], [{"slot": 200, "track": 2}])
        self.assertEqual(self.app._program_draft, [(3, 3), (3, 4)])

    def test_stop_saves_nothing(self):
        sim = _SimChanger(self.app, _library_discs())
        path = os.path.join(self.tmp.name, "stopped.json")
        self.app._library_begin("test")
        self.app._library_stop.set()
        self.app.link = sim
        self.app._library_export_worker(sim, path)
        self.drain_ui_queue()
        self.assertFalse(os.path.exists(path))
        self.assertFalse(self.app._library_running)

    def test_disconnect_stops_the_export(self):
        sim = _SimChanger(self.app, _library_discs())
        self.app._library_begin("test")
        self.app.link = None  # disconnected
        self.app._library_export_worker(sim, os.path.join(self.tmp.name, "x.json"))
        self.drain_ui_queue()
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "x.json")))


class TestRestoreWorker(_AppTestCase):
    def backup(self, **sim_kw):
        return self.backup_from(_library_discs(), **sim_kw)

    def backup_from(self, discs, **sim_kw):
        sim = _SimChanger(self.app, discs, **sim_kw)
        return lb.parse_library(self.export(sim), fold=app_mod.ascii_fold)

    def restore(self, sim, library, restore_program=False):
        self.app.link = sim
        self.app._library_begin("test")
        self.app._library_restore_worker(sim, library, restore_program)
        self.drain_ui_queue()

    def test_restores_a_wiped_changer_and_verifies(self):
        library = self.backup(userfile_names={0x04: "Jazz"})
        # Power outage: the changer lost every name, genre and userfile.
        wiped = {s: {"count": d["count"], "name": "", "titles": {}, "genre": 0, "userfiles": 0}
                 for s, d in _library_discs().items()}
        sim = _SimChanger(self.app, wiped)
        self.restore(sim, library)
        self.assertEqual(sim.discs, _library_discs())
        self.assertEqual(sim.userfile_names, {0x04: "Jazz"})
        # Slot 150 had nothing but genre/userfiles 0, which the wipe matches.
        self.assertIn("2 disc(s) written and verified, 1 already matched, 0 skipped, 0 not verified",
                      self.messages[-1])
        self.assertIn("Userfile names: 1 written and verified", self.messages[-1])

    def test_every_write_carries_the_discs_genre_and_mask(self):
        library = self.backup()
        wiped = {3: {"count": 4, "name": "", "titles": {}, "genre": 0, "userfiles": 0}}
        library["discs"] = [d for d in library["discs"] if d["slot"] == 3]
        sim = _SimChanger(self.app, wiped)
        self.restore(sim, library)
        self.assertEqual(len(sim.writes), 3)  # disc name + 2 titled tracks
        self.assertEqual({(w["genre"], w["userfiles"]) for w in sim.writes}, {(ROCK, 0x05)})

    def test_backup_from_power_on_restores_once_the_disc_is_played(self):
        discs = _library_discs()
        for d in discs.values():
            d["reported"] = 99
        library = self.backup_from(discs)
        wiped = {s: {"count": d["count"], "name": "", "titles": {}, "genre": 0, "userfiles": 0}
                 for s, d in _library_discs().items()}
        sim = _SimChanger(self.app, wiped)  # counts known now
        self.restore(sim, library)
        self.assertEqual(sim.discs, _library_discs())
        self.assertIn("0 skipped", self.messages[-1])

    def test_restore_right_after_power_on(self):
        library = self.backup()
        wiped = {s: {"count": d["count"], "reported": 99, "name": "", "titles": {},
                     "genre": 0, "userfiles": 0} for s, d in _library_discs().items()}
        sim = _SimChanger(self.app, wiped)
        self.restore(sim, library)
        self.assertEqual(sim.discs[3]["titles"], {1: "Intro", 4: "Outro"})
        self.assertIn("0 skipped, 0 not verified", self.messages[-1])

    def test_second_restore_writes_nothing(self):
        library = self.backup()
        sim = _SimChanger(self.app, _library_discs())
        self.restore(sim, library)
        self.assertEqual(sim.writes, [])
        self.assertIn("0 disc(s) written and verified, 3 already matched", self.messages[-1])

    def test_swapped_disc_is_left_alone(self):
        library = self.backup()
        discs = _library_discs()
        discs[3] = {"count": 9, "name": "Someone Else", "titles": {}, "genre": 0, "userfiles": 0}
        sim = _SimChanger(self.app, discs)
        self.restore(sim, library)
        self.assertEqual(sim.discs[3]["name"], "Someone Else")
        self.assertIn("Skipped slots: [3]", self.messages[-1])

    def test_a_write_that_doesnt_stick_is_reported(self):
        library = self.backup()
        discs = _library_discs()
        discs[200]["titles"] = {}
        sim = _SimChanger(self.app, discs)
        sim.send_write = lambda *a, **kw: None  # ACK'd but nothing stored
        self.restore(sim, library)
        self.assertIn("Not verified: [200]", self.messages[-1])

    def test_program_only_when_asked(self):
        library = self.backup(program=[(3, 2), (200, proto.LISTING_ALL_TRACKS)])
        sim = _SimChanger(self.app, _library_discs(), program=[])
        self.restore(sim, library, restore_program=False)
        self.assertEqual(sim.program, [])
        self.restore(sim, library, restore_program=True)
        self.assertEqual(sim.program, [(3, 2), (200, proto.LISTING_ALL_TRACKS)])
        self.assertIn("Program: 2 step(s) written and verified", self.messages[-1])


class TestRealRestoreSession(unittest.TestCase):
    """CONFIRMED on real hardware (v1.10.2, 2026-09-24): slot 1 exported
    with "Gift Shop" / "Butts Wigglin", both edited by hand, then Restore
    wrote exactly these two frames, re-read the slot as matching, and a
    second restore wrote nothing."""

    WRITES = [
        "02 fe 10 00 01 00 01 02 01 03 00 47 69 66 74 20 53 68 6f 70 a6",
        "02 fe 14 00 01 00 07 02 01 03 00 42 75 74 74 73 20 57 69 67 67 6c 69 6e dd",
    ]

    def test_plan_produces_the_logged_frames(self):
        saved = {"slot": 1, "track_count": 12, "name": "The Tragically Hip / Trou",
                 "genre": ALT, "userfiles": 0x02,
                 "tracks": {1: "Gift Shop", 7: "Butts Wigglin", 12: "Put It Off"}}
        current = {"track_count": 12, "name": "The Tragically Hip / Trou", "genre": ALT,
                   "userfiles": 0x02,
                   "tracks": {1: "Gift Shoppe", 7: "Ass Jigglin", 12: "Put It Off"}}
        items, mask, reason = lb.plan_disc_restore(saved, current)
        self.assertIsNone(reason)
        frames = [proto.encode_frame(proto.CMD_TEXT_DATA, proto.encode_text_data(
            slot=1, index=i, text=t, info_type=it, genre=g, userfiles=mask)).hex(" ")
            for i, t, it, _, g in items]
        self.assertEqual(frames, self.WRITES)
        # And once written, nothing is left to do (the second restore).
        current["tracks"].update({1: "Gift Shop", 7: "Butts Wigglin"})
        self.assertEqual(lb.plan_disc_restore(saved, current), ([], 0x02, None))

    def test_unplayed_slots_matched_instead_of_skipped(self):
        # Slots 2 and 3 reported 99 tracks during that restore.
        saved = {"slot": 2, "track_count": None, "name": "Limblifter-Bellaclava",
                 "genre": ROCK, "userfiles": 0x04, "tracks": {1: "Count To 9"}}
        current = dict(saved, track_count=99)
        self.assertEqual(lb.plan_disc_restore(saved, current), ([], 0x04, None))


class TestRealFrames(_AppTestCase):
    """Export fed frames logged from the real CD-425M: the v1.8.3 session's
    slot 1 (disc-name read-back and track 4 write, whose TextData payload
    is shaped exactly like a read reply), v1.7.1's program, and from the
    first real export (v1.10.0, 2026-09-24) slot 1's DiscInfo right after
    power-on (99 tracks) and once played (12), plus the index-0 frame that
    starts a track-names reply."""

    DISC_NAME_READBACK = (
        "02 fe 20 00 01 00 00 07 00 03 00 54 68 65 20 54 72 61 67 69 63 61 6c 6c 79 20 "
        "48 69 70 20 2f 20 54 72 6f 75 30"
    )
    TRACK4 = "02 fe 17 00 01 00 04 07 01 03 00 44 6f 6e 27 74 20 57 61 6b 65 20 44 61 64 64 79 71"
    PROGRAM_FRAME = ("02 0d 22 00 0b 01 00 01 01 00 01 01 00 02 01 00 05 01 00 08 01 00 04"
                     " 01 00 02 01 00 05 01 00 03 01 00 06 01 00 09 8d")
    DISC_INFO_99 = "02 04 05 00 01 00 01 63 00 92"
    DISC_INFO_12 = "02 04 05 00 01 00 01 0c 00 e9"
    TRACK_NAMES_INDEX_0 = ("02 fe 20 00 01 00 00 02 01 03 00 54 68 65 20 54 72 61 67 69 63 61 6c 6c"
                           " 79 20 48 69 70 20 2f 20 54 72 6f 75 34")

    def test_export_of_real_frames(self):
        lib = self._export_real(self.DISC_INFO_12)
        self.assertEqual(lib["discs"], [{
            "slot": 1, "track_count": 12, "name": "The Tragically Hip / Trou",
            "genre": "Alternative Rock", "userfiles": [1, 2, 3], "tracks": {"4": "Don't Wake Daddy"},
        }])
        self.assertEqual(len(lib["program"]), 11)
        self.assertEqual(lib["program"][0], {"slot": 1, "track": 1})

    def test_export_of_real_frames_right_after_power_on(self):
        lib = self._export_real(self.DISC_INFO_99)
        self.assertIsNone(lib["discs"][0]["track_count"])
        self.assertEqual(lib["discs"][0]["tracks"], {"4": "Don't Wake Daddy"})

    def _export_real(self, disc_info):
        sim = _SimChanger(self.app, {1: {"count": 12, "name": "", "titles": {}, "genre": ALT,
                                         "userfiles": 0x07}})
        real = {
            (proto.DataType.DISC_INFO, 0): [disc_info],
            (proto.DataType.TEXT_DATA, proto.InfoType.DISC_NAMES): [self.DISC_NAME_READBACK],
            (proto.DataType.TEXT_DATA, proto.InfoType.TRACK_NAMES): [self.TRACK_NAMES_INDEX_0,
                                                                      self.TRACK4],
            (proto.DataType.DISC_LISTING, 0): [self.PROGRAM_FRAME],
        }
        simulated = sim.send

        def send(command, data, timeout=5.0):
            _, data_type, slot, _, info_type, _ = struct.unpack("<BBHBBB", data)
            frames = real.get((data_type, info_type)) if slot in (0, 1) else None
            if frames is None:
                return simulated(command, data, timeout)
            for frame in frames:
                raw = bytes.fromhex(frame)
                sim._reply(raw[1], _data(frame))
        sim.send = send
        return json.loads(self.export(sim))


if __name__ == "__main__":
    unittest.main()
