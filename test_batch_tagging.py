#!/usr/bin/env python3
"""
test_batch_tagging.py
=====================
Tests for batch gnudb tagging (v1.13.0): load every unnamed disc, read its
TOC, look it up on gnudb.org, and write the matches the user approves.
CONFIRMED on real hardware (v1.13.1); that run's fixes (tracks 21+,
writing again after a failed check, a disc the changer can't read) are
tested here but not yet retried on hardware.

Stdlib-only (unittest), matching the rest of this project.

What's being tested:
  - Which discs (unnamed_slots), the DiscID from a TOC (the real
    v1.8.3/v1.9.1 TOC frames), what a gnudb entry becomes (plan_target),
    the write plan's checks (plan_write), the read-back check (verify)
    and the request pacing (TestPure).
  - The load worker against a simulated changer that, like the real one,
    answers a TOC read only for the disc in the drive (CONFIRMED, README
    "Honest gaps" #4), with gnudb.org mocked (TestLoadWorker).
  - The write worker: the fresh read, the write (the Backup restore's
    plan, carrying genre and userfiles) and the read-back, plus every
    reason not to write (TestWriteWorker).
  - The review window's glue (TestReviewWindow).
"""

from __future__ import annotations

import os
import struct
import tempfile
import time
import unittest

import test_library_backup as tlb  # installs the fake serial module first
import batch_tagging as bt
import gnudb_client
import library_backup as lb
import pclink_app as app_mod
import pclink_protocol as proto
from gnudb_client import GnudbDisc, GnudbMatch

ROCK, ALT, FOLK = tlb.ROCK, tlb.ALT, tlb.FOLK

# Real DiscTOC frames (test_gnudb_client.py): the Tragically Hip disc from
# the v1.8.3 session (12 tracks, DiscID 930c540c, CONFIRMED to be what the
# app queried) and Rusty (10 tracks) from the v1.9.1 log.
HIP_TOC = tlb._data(
    "02 06 2d 00 01 00 01 00 01 0c 00 02 00 05 00 00 09 37 00 13 21 00 18 29 "
    "00 22 35 00 26 14 00 30 01 00 33 58 00 37 19 00 42 12 00 47 27 00 52 38 00 19")
RUSTY_TOC = tlb._data(
    "02 06 27 00 03 00 01 00 01 0a 00 02 00 03 45 00 05 39 00 09 49 00 13 27 "
    "00 16 43 00 21 10 00 26 46 00 30 43 00 32 30 00 37 02 00 ac")

HIP = GnudbDisc(
    "rock", "920c540c", "The Tragically Hip", "Trouble at the Henhouse", genre="Rock",
    track_titles={1: "Gift Shop", 2: "Springtime in Vienna", 3: "Ahead by a Century",
                  4: "Don’t Wake Daddy", 5: "Flamenco", 6: "700 Ft. Ceiling",
                  7: "Butts Wigglin’", 8: "Apartment Song", 9: "Coconut Cream",
                  10: "Let’s Stay Engaged", 11: "Sherpa", 12: "Put It Off"},
)


# Slot 32 in the first real batch run (v1.13.0): 26 tracks. All 27 writes
# were ACK'd, but the read-back returned the disc name and tracks 1-20 only,
# and track 21 plays with no title on the front panel (v1.13.2).
MC_FACE = GnudbDisc(
    "data", "830ffd80", "MC Face", "Not The Tom Green Show",
    track_titles={1: "Intro", 2: "Not The Green Tom Show", 3: "My Girlfriend Died",
                  4: "Big Googely Eyes", 5: "Prelude - Drug Boy", 6: "Humplik The Baddest",
                  7: "Jiffy Pop", 8: "Prelude - The Stake Out",
                  9: "Bank Robbery (Harvie & J-Roo)", 10: "Stupid Dummies", 11: "A Love Song",
                  12: "Prelude - J-Roo And Face", 13: "J-Roo On The Loose",
                  14: "Prelude - The Laugh You Do", 15: "Devil In The Scope",
                  16: "Rock The Mike Tonight", 17: "Prelude - The Apology",
                  18: "Somethin To Chew On)", 19: "Prelude - Harvie's Babies",
                  20: "Sex Offender", 21: "MC Face On Patrol", 22: "Prelude - Dustin Hoffman",
                  23: "Slaughter Ya Oughta", 24: "Just Hit Me", 25: "Extro",
                  26: "MC Face Goin Solo"},
)


def _toc_entry(payload):
    """A complete _toc_cache entry, as App._cache_toc builds it."""
    toc = proto.decode_disc_toc(payload)
    times = toc["track_times"]
    return {"tracks": {toc["first_track"] + i: t for i, t in enumerate(times[:-1])},
            "leadout": times[-1], "format": toc["format"], "format_name": toc["format_name"]}


def _hip_target(slot=150, count=12, disc=HIP):
    return bt.plan_target(slot, disc, count, app_mod.ascii_fold, app_mod.match_changer_genre)


class TestPure(unittest.TestCase):
    def test_unnamed_slots_from_the_scan(self):
        raw = lb.build_library([
            lb.disc_record(1, 12, "Named", {}, ROCK, 0),
            lb.disc_record(5, 10, "", {}, 0, 0),
            lb.disc_record(9, 99, None, None, None, None),  # name couldn't be read
            lb.disc_record(2, 13, "", {}, 0, 0),
        ], None, None, "1.13.0", "2026-10-01T12:00:00")
        self.assertEqual(bt.unnamed_slots(raw), ([2, 5], [9]))
        self.assertEqual(bt.unnamed_slots(None), ([], []))

    def test_discid_from_a_real_toc(self):
        self.assertEqual(bt.toc_discid(_toc_entry(HIP_TOC))["discid"], "930c540c")

    def test_incomplete_toc_has_no_discid(self):
        entry = _toc_entry(HIP_TOC)
        self.assertIsNone(bt.toc_discid(dict(entry, leadout=None)))
        del entry["tracks"][5]
        self.assertIsNone(bt.toc_discid(entry))
        self.assertIsNone(bt.toc_discid(None))
        self.assertIsNone(bt.toc_discid({"tracks": {}, "leadout": None}))

    def test_disc_name_is_artist_album_cut_to_25(self):
        # The same cut the real changer made (v1.8.3 session).
        self.assertEqual(bt.disc_title(HIP), "The Tragically Hip / Trou")
        self.assertEqual(bt.disc_title(GnudbDisc("misc", "x", "", "Album Only")), "Album Only")

    def test_target_folds_text_and_matches_genre(self):
        target, notes = _hip_target()
        self.assertEqual(target["name"], "The Tragically Hip / Trou")
        self.assertEqual(target["tracks"][4], "Don't Wake Daddy")
        self.assertEqual(len(target["tracks"]), 12)
        self.assertEqual(target["genre"], ROCK)
        self.assertIsNone(target["userfiles"])  # always kept
        self.assertTrue(any("cut to the 25" in n for n in notes))

    def test_unknown_genre_keeps_the_current_one(self):
        disc = GnudbDisc("misc", "x", "A", "B", genre="folk rock", track_titles={1: "One"})
        target, notes = bt.plan_target(3, disc, 1, match_genre=app_mod.match_changer_genre)
        self.assertIsNone(target["genre"])
        self.assertTrue(any("'folk rock' isn't one of the changer's" in n for n in notes))

    def test_extra_and_missing_titles(self):
        disc = GnudbDisc("misc", "x", "A", "B", track_titles={1: "One", 2: "", 4: "Four", 5: "Five"})
        target, notes = bt.plan_target(3, disc, 4)
        self.assertEqual(target["tracks"], {1: "One", 4: "Four"})
        self.assertTrue(any("titles 5-5 aren't written" in n for n in notes))
        self.assertTrue(any("2 track(s): 2, 3" in n for n in notes))

    def _current(self, **kw):
        cur = {"track_count": 12, "name": "", "tracks": {}, "genre": FOLK, "userfiles": 0x05,
               "cdtext": False}
        cur.update(kw)
        return cur

    def test_write_plan_carries_genre_and_keeps_userfiles(self):
        target, _ = _hip_target()
        items, mask, why = bt.plan_write(target, self._current())
        self.assertIsNone(why)
        self.assertEqual(mask, 0x05)
        self.assertEqual(items[0], (0, "The Tragically Hip / Trou", proto.InfoType.DISC_NAMES,
                                    "Disc Name", ROCK))
        self.assertEqual(len(items), 13)
        self.assertTrue(all(it[4] == ROCK for it in items))

    def test_unmatched_genre_writes_the_current_genre(self):
        disc = GnudbDisc("misc", "x", "A", "B", genre="", track_titles={1: "One"})
        target, _ = bt.plan_target(3, disc, 1)
        items, _, _ = bt.plan_write(target, self._current(track_count=1))
        self.assertTrue(all(it[4] == FOLK for it in items))

    def test_write_plan_reasons_not_to_write(self):
        target, _ = _hip_target()
        cases = [
            (self._current(name="Somebody"), "named since the scan"),
            (self._current(name=None), "couldn't be read"),
            (self._current(cdtext=True), "CD-Text"),
            (self._current(track_count=10), "probably a different disc"),
            (self._current(track_count=0), "empty"),
            (self._current(userfiles=None), "couldn't be read"),
        ]
        for current, reason in cases:
            items, _, why = bt.plan_write(target, current)
            self.assertEqual(items, [], current)
            self.assertIn(reason, why)

    def test_unreadable_genre_matters_only_when_its_kept(self):
        target, _ = _hip_target()  # gnudb's "Rock" is written, so no risk
        self.assertIsNone(bt.plan_write(target, self._current(genre=None))[2])
        target["genre"] = None  # gnudb's genre isn't one of the changer's
        items, _, why = bt.plan_write(target, self._current(genre=None))
        self.assertEqual(items, [])
        self.assertIn("couldn't be read", why)

    def test_unknown_count_after_power_on_still_writes(self):
        # DiscInfo says 99 for a disc not played since power-on (v1.10.1);
        # loading it for the TOC should fix that, but if not, no mismatch.
        target, _ = _hip_target()
        items, _, why = bt.plan_write(target, self._current(track_count=99))
        self.assertIsNone(why)
        self.assertEqual(len(items), 13)

    def test_verify_compares_the_first_25_characters(self):
        disc = GnudbDisc("misc", "x", "A", "B", genre="Rock",
                         track_titles={1: "We Wish You A Merry Christmas"})
        target, _ = bt.plan_target(3, disc, 1, match_genre=app_mod.match_changer_genre)
        after = {"name": "A / B", "tracks": {1: "We Wish You A Merry Chris"}, "genre": ROCK}
        self.assertEqual(bt.verify(target, after), [])
        self.assertEqual(bt.verify(target, dict(after, genre=FOLK)), ["genre reads 'Folk'"])
        self.assertEqual(bt.verify(target, dict(after, tracks={})), ["track 1 reads None"])
        self.assertEqual(bt.verify(target, dict(after, name=None)),
                         ["the slot couldn't be read back"])

    def test_tracks_past_20_get_no_title(self):
        # The v1.13.0 run's slot 32: the changer keeps titles 1-20 only.
        target, notes = bt.plan_target(32, MC_FACE, 26)
        self.assertEqual(sorted(target["tracks"]), list(range(1, 21)))
        self.assertIn("The changer keeps titles for tracks 1-20 only: tracks 21-26 get none.",
                      notes)
        self.assertFalse(any("No gnudb title" in n for n in notes))
        after = {"name": "MC Face / Not The Tom Gre", "genre": None,
                 "tracks": {n: t[:25] for n, t in MC_FACE.track_titles.items() if n <= 20}}
        self.assertEqual(bt.verify(target, after), [])
        self.assertEqual(bt.verify(target, dict(after, tracks={**after["tracks"], 20: None})),
                         ["track 20 reads None"])
        _, notes12 = _hip_target()
        self.assertFalse(any("1-20 only" in n for n in notes12))

    def test_a_disc_this_batch_named_can_be_written_again(self):
        target, _ = _hip_target()
        current = dict(self._current(), name="The Tragically Hip / Trou")
        self.assertIn("named since the scan", bt.plan_write(target, current)[2])
        items, _, why = bt.plan_write(target, current, ours="The Tragically Hip / Trou")
        self.assertIsNone(why)
        self.assertEqual(len(items), 12)  # the name already matches
        self.assertIn("named since the scan",
                      bt.plan_write(target, current, ours="Something Else")[2])

    def test_preview_rows(self):
        disc = GnudbDisc("misc", "x", "A", "B", track_titles={2: "Two"})
        target, _ = bt.plan_target(3, disc, 2)
        self.assertEqual(bt.preview_rows(target, FOLK),
                         [("Disc Name", "A / B"), ("Genre", "Folk (kept)"),
                          ("Track 1", "-"), ("Track 2", "Two")])

    def test_pacer_spaces_requests(self):
        now, slept = [100.0], []

        def sleep(s):
            slept.append(s)
            now[0] += s
        pacer = bt.Pacer(2.0, clock=lambda: now[0], sleep=sleep)
        pacer.wait()
        now[0] += 0.5
        pacer.wait()
        now[0] += 5
        pacer.wait()
        self.assertEqual(slept, [1.5])

    def test_rows_and_summary(self):
        d = bt.BatchDisc(4, bt.FOUND, "2 candidate(s)", bt.toc_discid(_toc_entry(HIP_TOC)), 12,
                         [GnudbMatch("rock", "920c540c", "Hip / Trouble", exact=False)])
        self.assertEqual(d.row(), ("4", "930c540c", "To review: 2 candidate(s)", "Hip / Trouble"))
        self.assertEqual(bt.summary([d, bt.BatchDisc(5, bt.NO_MATCH), bt.BatchDisc(6, bt.FOUND)]),
                         "2 to review, 1 no match")
        # Not "1 cD-Text, skipped", as the v1.13.0 run logged it.
        self.assertEqual(bt.summary([bt.BatchDisc(4, bt.CDTEXT), bt.BatchDisc(37, bt.NO_TOC)]),
                         "1 CD-Text, skipped, 1 no TOC")


# -- Against a simulated changer -------------------------------------------

class _SimBatchChanger(tlb._SimChanger):
    """_SimChanger plus ChangeDisc and TOC reads: ChangeDisc puts the disc
    in the drive (InfoEvent, then Changing -> Playing StateEvents), and a
    TOC read is answered only for the disc in the drive -- otherwise the
    changer sends nothing (CONFIRMED, README "Honest gaps" #4)."""

    def __init__(self, app, discs, tocs):
        super().__init__(app, discs)
        self.tocs = tocs  # slot -> real DiscTOC payload (any slot; rewritten)
        self.loaded = None
        self.changes = []
        self.toc_reads = []

    def send(self, command, data, timeout=5.0):
        if command == proto.CMD_CHANGE_DISC:
            slot, track, begin = struct.unpack("<HBB", data)
            self.changes.append((slot, track, begin))
            if slot not in self.discs:
                return  # empty slot: nothing happens
            self.loaded = slot
            disc = self.discs[slot]
            self._reply(proto.CMD_INFO_EVENT, struct.pack(
                "<HBBBBBBB", slot, track, 0, disc["genre"], disc["userfiles"], 0, 0, 0))
            self._reply(proto.CMD_STATE_EVENT, bytes([proto.State.CHANGING]))
            self._reply(proto.CMD_STATE_EVENT, bytes([proto.State.PLAYING]))
            return
        _, data_type, slot, _, _, _ = struct.unpack("<BBHBBB", data)
        if data_type == proto.DataType.DISC_TOC:
            self.toc_reads.append(slot)
            if slot == self.loaded and slot in self.tocs:
                self._reply(proto.CMD_DISC_TOC, struct.pack("<H", slot) + self.tocs[slot][2:])
            return
        super().send(command, data, timeout)


def _discs():
    return {
        3: {"count": 4, "name": "Band / Record", "titles": {1: "Intro"}, "genre": ROCK,
            "userfiles": 0x05},
        150: {"count": 12, "name": "", "titles": {}, "genre": FOLK, "userfiles": 0x02},
        160: {"count": 10, "name": "", "titles": {}, "genre": 0, "userfiles": 0},
    }


class _BatchTestCase(tlb._AppTestCase):
    def setUp(self):
        super().setUp()
        app = self.app
        app._fetch_names_for_slot = lambda slot: None
        app._fetch_genre_for_slot = lambda slot: None
        app.BATCH_TOC_RETRY_AFTER = 0
        app.BATCH_POLL_INTERVAL = 0
        app.BATCH_LOAD_TIMEOUT = 0.3
        app._batch_pacer = bt.Pacer(0)
        app._batch_hello = "user example.com KenwoodPCLinkController 1.13.0"
        self.queries, self.reads = [], []
        self.answers = {}  # discid -> list of matches, or an exception
        self._query, self._read = gnudb_client.query, gnudb_client.read

        def query(discid, offsets, seconds, hello, timeout=None):
            self.queries.append(discid)
            answer = self.answers.get(discid, [])
            if isinstance(answer, Exception):
                raise answer
            return answer

        def read(category, discid, hello, timeout=None):
            self.reads.append((category, discid))
            return HIP
        gnudb_client.query, gnudb_client.read = query, read
        self.sim = _SimBatchChanger(app, _discs(), {150: HIP_TOC, 160: RUSTY_TOC})
        app.link = self.sim

    def tearDown(self):
        gnudb_client.query, gnudb_client.read = self._query, self._read
        super().tearDown()

    def load(self, slots):
        self.app._batch = [bt.BatchDisc(s) for s in slots]
        self.app._batch_loading = True
        self.app._library_begin("test", self.app.lib_progress_label)
        self.app._batch_load_worker(self.sim)
        self.drain_ui_queue()
        return {d.slot: d for d in self.app._batch}


class TestLoadWorker(_BatchTestCase):
    def test_loads_each_disc_and_looks_it_up(self):
        hip = GnudbMatch("rock", "920c540c", "The Tragically Hip / Trouble at the Henhouse",
                         exact=False)
        self.answers["930c540c"] = [hip]
        got = self.load([150, 160])
        self.assertEqual(self.sim.changes, [(150, 1, 1), (160, 1, 1)])
        self.assertEqual(got[150].status, bt.FOUND)
        self.assertEqual(got[150].discid["discid"], "930c540c")
        self.assertEqual(got[150].track_count, 12)
        self.assertEqual(got[150].matches, [hip])
        self.assertEqual(got[150].note, "1 candidate(s), inexact")
        self.assertEqual(got[160].status, bt.NO_MATCH)
        self.assertEqual(got[160].track_count, 10)
        self.assertEqual(self.queries, ["930c540c", got[160].discid["discid"]])
        self.assertFalse(self.app._library_running)
        self.assertFalse(self.app._batch_loading)

    def test_nothing_is_written_while_loading(self):
        self.answers["930c540c"] = [GnudbMatch("rock", "930c540c", "Hip")]
        self.load([150, 160])
        self.assertEqual(self.sim.writes, [])
        self.assertEqual(self.reads, [])  # entries are read for the review only

    def test_disc_already_in_the_drive_isnt_reloaded(self):
        self.app._current_slot = 150
        self.app._last_state = proto.State.PLAYING
        self.app._toc_cache[150] = _toc_entry(HIP_TOC)
        got = self.load([150])
        self.assertEqual(self.sim.changes, [])
        self.assertEqual(got[150].status, bt.NO_MATCH)

    def test_toc_that_never_comes(self):
        self.sim.tocs.pop(160)
        got = self.load([160])
        self.assertEqual(got[160].status, bt.NO_TOC)
        self.assertIn("never came", got[160].note)
        self.assertEqual(self.queries, [])

    def test_slot_that_wont_load(self):
        got = self.load([42])  # empty now
        self.assertEqual(got[42].status, bt.NO_TOC)
        self.assertEqual(self.sim.toc_reads, [])  # never settled on it

    def test_changer_moving_on_from_an_unreadable_disc(self):
        # v1.13.0 run, slot 37 (a damaged disc): ~40s of Changing, then the
        # changer played slot 38 by itself. The batch waited out its 90s.
        self.sim.discs[37] = {"count": 99, "name": "", "titles": {}, "genre": 0, "userfiles": 0}
        self.sim.discs[38] = {"count": 99, "name": "", "titles": {}, "genre": 0, "userfiles": 0}
        self.sim.tocs[38] = RUSTY_TOC
        real_send = self.sim.send

        def send(command, data, timeout=5.0):
            if command == proto.CMD_CHANGE_DISC and struct.unpack("<H", data[:2])[0] == 37:
                self.sim.changes.append((37, 1, 1))
                self.sim._reply(proto.CMD_INFO_EVENT, struct.pack("<HBBBBBBB", 37, 1, 0, 0, 0, 0, 0, 0))
                self.sim._reply(proto.CMD_STATE_EVENT, bytes([proto.State.CHANGING]))
                self.sim.loaded = 38
                self.sim._reply(proto.CMD_INFO_EVENT, struct.pack("<HBBBBBBB", 38, 1, 0, 0, 0, 0, 0, 0))
                self.sim._reply(proto.CMD_STATE_EVENT, bytes([proto.State.PLAYING]))
                return
            real_send(command, data, timeout)
        self.sim.send = send
        self.app.BATCH_LOAD_TIMEOUT = 30
        start = time.monotonic()
        got = self.load([37, 38])
        self.assertLess(time.monotonic() - start, 5)  # didn't wait out the timeout
        self.assertEqual(got[37].status, bt.NO_TOC)
        self.assertIn("went on to slot 38", got[37].note)
        self.assertNotIn(37, self.sim.toc_reads)
        self.assertEqual(got[38].discid, bt.toc_discid(_toc_entry(RUSTY_TOC)))

    def test_cdtext_disc_is_skipped(self):
        cdtext_toc = bytearray(RUSTY_TOC)
        cdtext_toc[3] = proto.Format.SEEN_CDTEXT
        self.sim.tocs[160] = bytes(cdtext_toc)
        got = self.load([160])
        self.assertEqual(got[160].status, bt.CDTEXT)
        self.assertEqual(self.queries, [])

    def test_known_cdtext_disc_isnt_even_loaded(self):
        self.app._cdtext_slots.add(160)
        got = self.load([160])
        self.assertEqual(got[160].status, bt.CDTEXT)
        self.assertEqual(self.sim.changes, [])

    def test_rate_limit_stops_the_lookups_not_the_loading(self):
        self.answers["930c540c"] = gnudb_client.GnudbRateLimited("HTTP 429")
        got = self.load([150, 160])
        self.assertEqual(self.queries, ["930c540c"])
        self.assertEqual(got[150].status, bt.RATE_LIMITED)
        self.assertEqual(got[160].status, bt.RATE_LIMITED)
        self.assertIsNotNone(got[160].discid)  # can be looked up again later
        self.assertEqual(len(self.sim.changes), 2)

    def test_lookup_failure(self):
        self.answers["930c540c"] = gnudb_client.GnudbError("timed out")
        got = self.load([150])
        self.assertEqual((got[150].status, got[150].note), (bt.LOOKUP_FAILED, "timed out"))

    def test_stop(self):
        self.app._batch = [bt.BatchDisc(150), bt.BatchDisc(160)]
        self.app._library_begin("test", self.app.lib_progress_label)
        self.app._library_stop.set()
        self.app._batch_load_worker(self.sim)
        self.drain_ui_queue()
        self.assertEqual([d.status for d in self.app._batch], [bt.STOPPED, bt.STOPPED])
        self.assertEqual(self.sim.changes, [])
        self.assertFalse(self.app._library_running)


class TestWriteWorker(_BatchTestCase):
    def setUp(self):
        super().setUp()
        raw = lb.build_library([
            lb.disc_record(150, 12, "", {}, FOLK, 0x02),
            lb.disc_record(3, 4, "Band / Record", {1: "Intro"}, ROCK, 0x05),
        ], None, None, "1.13.0", "2026-10-01T12:00:00")
        self.app._set_browser_scan(raw)
        self.bd = bt.BatchDisc(150, bt.FOUND, discid={"discid": "930c540c"}, track_count=12)
        self.app._batch = [self.bd]

    def write(self, target):
        self.app._library_begin("test", self.app.lib_progress_label)
        self.app._batch_write_worker(self.sim, self.bd, target)
        self.drain_ui_queue()

    def cut_names_at_25(self):
        """The changer reads back 25 characters of a written track name
        (v1.13.0 run: 'Nothing Left to Make Me W'), so the sim keeps 25."""
        real_send_write = self.sim.send_write

        def send_write(command, data, follow_up_command, follow_up_data, timeout=8.0):
            p = proto.decode_text_data(follow_up_data)
            follow_up_data = proto.encode_text_data(
                p["slot"], p["index"], p["text"][:25], info_type=p["info_type"],
                genre=p["genre"], userfiles=p["userfiles"])
            real_send_write(command, data, follow_up_command, follow_up_data, timeout)
        self.sim.send_write = send_write

    def test_writes_names_and_genre_keeps_userfiles_and_checks(self):
        target, _ = _hip_target()
        self.write(target)
        disc = self.sim.discs[150]
        self.assertEqual(disc["name"], "The Tragically Hip / Trou")
        self.assertEqual(disc["titles"][4], "Don't Wake Daddy")
        self.assertEqual(disc["genre"], ROCK)
        self.assertEqual(disc["userfiles"], 0x02)  # kept
        self.assertEqual(len(self.sim.writes), 13)
        self.assertTrue(all(w["userfiles"] == 0x02 and w["genre"] == ROCK for w in self.sim.writes))
        self.assertEqual(self.bd.status, bt.WRITTEN)
        # The Library's scan follows the write.
        scanned = next(d for d in self.app._browser_raw["discs"] if d["slot"] == 150)
        self.assertEqual(scanned["name"], "The Tragically Hip / Trou")
        self.assertEqual(scanned["genre"], "Rock")
        self.assertFalse(self.app._library_running)

    def test_long_track_title_cut_by_the_changer_still_checks_out(self):
        disc = GnudbDisc("misc", "x", "A", "B", track_titles={1: "We Wish You A Merry Christmas"})
        target, _ = bt.plan_target(150, disc, 12)
        self.cut_names_at_25()
        self.write(target)
        self.assertEqual(self.bd.status, bt.WRITTEN)

    def test_disc_named_since_the_scan_isnt_touched(self):
        self.sim.discs[150]["name"] = "Typed On The Remote"
        target, _ = _hip_target()
        self.write(target)
        self.assertEqual(self.sim.writes, [])
        self.assertEqual(self.bd.status, bt.NOT_WRITTEN)
        self.assertIn("named since the scan", self.bd.note)
        scanned = next(d for d in self.app._browser_raw["discs"] if d["slot"] == 150)
        self.assertEqual(scanned["name"], "Typed On The Remote")

    def test_different_disc_in_the_slot_isnt_touched(self):
        self.sim.discs[150]["count"] = 9
        target, _ = _hip_target()
        self.write(target)
        self.assertEqual(self.sim.writes, [])
        self.assertIn("probably a different disc", self.bd.note)

    def test_unreadable_genre_isnt_risked(self):
        self.sim.fail.add((proto.DataType.DISC_GENRE, 150))
        target, _ = _hip_target(disc=GnudbDisc("misc", "x", "A", "B", genre="folk rock",
                                               track_titles={1: "One"}))
        self.write(target)
        self.assertEqual(self.sim.writes, [])
        self.assertEqual(self.bd.status, bt.NOT_WRITTEN)

    def test_read_back_mismatch_is_reported(self):
        target, _ = _hip_target()
        real_send_write = self.sim.send_write

        def send_write(command, data, follow_up_command, follow_up_data, timeout=8.0):
            real_send_write(command, data, follow_up_command, follow_up_data, timeout)
            self.sim.discs[150]["titles"].pop(12, None)  # track 12 doesn't stick
        self.sim.send_write = send_write
        self.write(target)
        self.assertEqual(self.bd.status, bt.CHECK_FAILED)
        self.assertIn("track 12 reads None", self.bd.note)

    def test_26_track_disc_checks_out_on_tracks_1_to_20(self):
        # The v1.13.0 run's slot 32: "check failed (track 21 reads None ...)".
        self.sim.discs[150]["count"] = 26
        target, _ = bt.plan_target(150, MC_FACE, 26)
        self.cut_names_at_25()
        self.write(target)
        self.assertEqual(len(self.sim.writes), 21)  # name + tracks 1-20
        self.assertEqual(self.bd.status, bt.WRITTEN)

    def test_check_failure_can_be_written_again(self):
        # v1.13.0 run: after slot 32's failed check, approving it again got
        # "it has been named since the scan" -- by the batch's own write.
        target, _ = _hip_target()
        real_send_write = self.sim.send_write
        drop = [True]

        def send_write(command, data, follow_up_command, follow_up_data, timeout=8.0):
            real_send_write(command, data, follow_up_command, follow_up_data, timeout)
            if drop[0]:
                self.sim.discs[150]["titles"].pop(12, None)
        self.sim.send_write = send_write
        self.write(target)
        self.assertEqual(self.bd.status, bt.CHECK_FAILED)
        drop[0] = False
        self.sim.writes.clear()
        self.write(target)
        self.assertEqual(self.bd.status, bt.WRITTEN)
        self.assertEqual([w["index"] for w in self.sim.writes], [12])  # only what differed

    def test_failed_write_is_reported(self):
        target, _ = _hip_target()

        def send_write(*a, **kw):
            raise app_mod.PCLinkError("refused")
        self.sim.send_write = send_write
        self.write(target)
        self.assertEqual(self.bd.status, bt.CHECK_FAILED)
        self.assertEqual(self.bd.note, "13 of 13 write(s) failed")


class TestReviewWindow(_BatchTestCase):
    def setUp(self):
        super().setUp()
        raw = lb.build_library([lb.disc_record(150, 12, "", {}, FOLK, 0x02),
                                lb.disc_record(160, 10, "", {}, 0, 0)],
                               None, None, "1.13.0", "2026-10-01T12:00:00")
        self.app._set_browser_scan(raw)
        self.match = GnudbMatch("rock", "920c540c", "The Tragically Hip / Trouble", exact=False)
        self.app._batch = [
            bt.BatchDisc(150, bt.FOUND, "1 candidate(s)", bt.toc_discid(_toc_entry(HIP_TOC)), 12,
                         [self.match]),
            bt.BatchDisc(160, bt.NO_MATCH, "", bt.toc_discid(_toc_entry(RUSTY_TOC)), 10),
        ]
        self.app._batch_entries[("rock", "920c540c")] = HIP

    def test_opens_on_the_first_disc_to_review_with_a_preview(self):
        self.app._open_batch_window()
        self.drain_ui_queue()
        self.assertEqual(self.app.batch_tree.selection(), ("150",))
        rows = [self.app.batch_preview.item(i, "values")
                for i in self.app.batch_preview.get_children()]
        self.assertEqual(rows[0], ("Disc Name", "The Tragically Hip / Trou"))
        self.assertEqual(rows[1], ("Genre", "Rock"))
        self.assertEqual(len(rows), 14)
        self.assertIn("close match", self.app.batch_notes.cget("text"))
        self.assertEqual(str(self.app.batch_write_btn.cget("state")), "normal")

    def test_no_writing_while_discs_are_loading(self):
        self.app._library_begin("loading", self.app.lib_progress_label)
        self.app._open_batch_window()
        self.drain_ui_queue()
        self.assertEqual(str(self.app.batch_write_btn.cget("state")), "disabled")

    def test_unread_candidate_is_read_for_the_preview(self):
        self.app._batch_entries.clear()
        self.app._open_batch_window()
        self.assertIn("Reading", self.app.batch_notes.cget("text"))
        self.assertEqual(str(self.app.batch_write_btn.cget("state")), "disabled")
        # Let the read thread finish.
        import time
        for _ in range(100):
            if self.reads:
                break
            time.sleep(0.01)
        for _ in range(100):
            self.drain_ui_queue()
            if self.app._batch_target is not None:
                break
            time.sleep(0.01)
        self.assertEqual(self.reads, [("rock", "920c540c")])
        self.assertEqual(self.app._batch_target["name"], "The Tragically Hip / Trou")

    def test_write_then_moves_on(self):
        self.app._batch[1].status = bt.FOUND
        self.app._batch[1].matches = [self.match]
        self.app._open_batch_window()
        self.drain_ui_queue()
        bd = self.app._batch[0]
        target = self.app._batch_target
        self.app._library_begin("test", self.app.lib_progress_label)
        self.app._batch_write_worker(self.sim, bd, target)
        self.drain_ui_queue()
        self.assertEqual(bd.status, bt.WRITTEN)
        self.assertEqual(self.app.batch_tree.selection(), ("160",))
        self.assertIn("Written", self.app.batch_tree.item("150", "values")[2])

    def test_skip_moves_on_and_writes_nothing(self):
        self.app._batch[1].status = bt.FOUND
        self.app._open_batch_window()
        self.drain_ui_queue()
        self.app._batch_skip()
        self.assertEqual(self.app._batch[0].status, bt.SKIPPED)
        self.assertEqual(self.app.batch_tree.selection(), ("160",))
        self.assertEqual(self.sim.writes, [])

    def test_disc_with_no_match_can_be_looked_up_again(self):
        self.app._open_batch_window()
        self.app.batch_tree.selection_set("160")
        self.app._batch_show_disc()
        self.assertEqual(str(self.app.batch_lookup_btn.cget("state")), "normal")
        self.assertEqual(str(self.app.batch_write_btn.cget("state")), "disabled")
        self.assertEqual(str(self.app.batch_skip_btn.cget("state")), "disabled")


class TestStart(_BatchTestCase):
    def setUp(self):
        super().setUp()
        self.asked = []
        self._askyesno = app_mod.messagebox.askyesno
        app_mod.messagebox.askyesno = lambda title, msg, **kw: self.asked.append(msg) or False

    def tearDown(self):
        app_mod.messagebox.askyesno = self._askyesno
        super().tearDown()

    def test_needs_a_scan(self):
        self.app._browser_raw = None
        self.app._start_batch_tagging()
        self.assertIn("Scan the changer first", self.messages[-1])

    def test_nothing_unnamed(self):
        self.app._set_browser_scan(lb.build_library(
            [lb.disc_record(3, 4, "Band", {}, ROCK, 0)], None, None, "1.13.0", "x"))
        self.app._start_batch_tagging()
        self.assertIn("Every disc in the last scan has a name", self.messages[-1])

    def test_lists_the_unnamed_discs_and_does_nothing_if_declined(self):
        self.app._set_browser_scan(lb.build_library(
            [lb.disc_record(3, 4, "Band", {}, ROCK, 0), lb.disc_record(150, 12, "", {}, 0, 0),
             lb.disc_record(9, 5, None, None, None, None)], None, None, "1.13.0", "x"))
        self.app.gnudb_email_var.set("user@example.com")
        self.app._start_batch_tagging()
        self.assertIn("1 disc(s) in the last scan have no name: 150", self.asked[-1])
        self.assertIn("couldn't be read: [9]", self.asked[-1])
        self.assertEqual(self.sim.changes, [])
        self.assertEqual(self.app._batch, [])
        self.assertFalse(self.app._library_running)


if __name__ == "__main__":
    unittest.main()
