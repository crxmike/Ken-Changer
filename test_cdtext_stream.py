"""
test_cdtext_stream.py
=====================
Tests for v1.12.4: the endless LongTextData stream a CD-Text disc (slot 4,
"Acoustic Christmas Celebr", DiscInfo format 0x90) sends when its names
are read while it's in the drive. Built from the raw-byte logs of
2026-09-25 (14:12, 14:38, 14:41 sessions).

What's CONFIRMED on real hardware (those logs): the stream itself, and
that the same slot read while another disc is in the drive returns the
stored names as ordinary TextData. What's NOT yet tried on hardware: the
link cutting the stream off and resyncing (pclink_link.
_cut_off_text_stream), and the app changes built on it.
"""

from __future__ import annotations

import threading
import time
import types
import unittest
import unittest.mock

# test_write_feature stubs out pyserial before pclink_link is imported.
from test_write_feature import FakeSerial  # noqa: E402

import library_browser
import pclink_app
import pclink_link as link_mod
import pclink_protocol as proto
from pclink_link import PCLinkConnection, PCLinkTextStream


def _bytes(hex_text: str) -> bytes:
    return bytes.fromhex(hex_text)


def _stream_frame(seq: int, info_type: int = 0) -> bytes:
    """One frame of the stream, byte for byte as logged, e.g. 14:38:52
    `02 fd 09 00 04 00 00 00 00 0b 90 01 01 59` (info_type 0, seq 1)."""
    data = bytes([0x04, 0x00, 0x00, 0x00, info_type, 0x0B, 0x90, seq, 0x01])
    return proto.encode_frame(proto.CMD_LONG_TEXT_DATA, data)


# 14:42:28, slot 4 read with slot 3 in the drive: the stored disc name.
STORED_DISC_NAME = "02 fe 0c 00 04 00 00 00 00 0b 90 54 69 74 6c 65 55"
STORED_TRACK_1 = "02 fe 0d 00 04 00 01 00 01 0b 90 4e 61 6d 65 20 31 82"


class TestDecode(unittest.TestCase):
    def test_logged_frames_match_the_builder(self):
        self.assertEqual(_stream_frame(1, 0), _bytes("02 fd 09 00 04 00 00 00 00 0b 90 01 01 59"))
        self.assertEqual(_stream_frame(0x26, 0), _bytes("02 fd 09 00 04 00 00 00 00 0b 90 26 01 34"))
        self.assertEqual(_stream_frame(1, 1), _bytes("02 fd 09 00 04 00 00 00 01 0b 90 01 01 58"))

    def test_decodes_genre_userfiles_and_seq(self):
        p = proto.decode_long_text_data(_stream_frame(0x26, 1)[4:-1])
        self.assertEqual(p["slot"], 4)
        self.assertEqual(p["track"], 0)
        self.assertEqual(p["userfiles"], 0)
        self.assertEqual(p["info_type"], proto.InfoType.TRACK_NAMES)
        self.assertEqual(p["genre"], 0x0B)  # Folk, as DiscGenre says
        self.assertEqual(p["format"], 0x90)
        self.assertEqual(p["seq"], 0x26)
        self.assertTrue(proto.is_placeholder_text(p["text"]))


class _LinkTestBase(unittest.TestCase):
    def setUp(self):
        self.conn = PCLinkConnection("FAKE")
        self.fake = FakeSerial()
        self.conn.ser = self.fake
        self.frames = []
        self.conn.on_frame = self.frames.append


class TestDetection(_LinkTestBase):
    def test_stream_is_detected_after_five_frames(self):
        for seq in range(1, 8):
            self.fake.feed(_stream_frame(seq))
        with self.assertRaises(link_mod._EndlessTextStream):
            self.conn._drain_replies(deadline=time.monotonic() + 3.0)
        self.assertEqual(len(self.frames), link_mod.TEXT_STREAM_FRAMES)
        # Each of those frames was ACK'd, nothing else sent.
        self.assertEqual(self.fake.sent_bytes(), bytes([proto.ACK]) * link_mod.TEXT_STREAM_FRAMES)

    def test_stored_names_are_not_a_stream(self):
        self.fake.feed(_bytes(STORED_DISC_NAME))
        self.fake.feed(_bytes(STORED_TRACK_1))
        self.fake.feed(bytes([proto.EOT]))
        closed, _ = self.conn._drain_replies(deadline=time.monotonic() + 2.0)
        self.assertTrue(closed)
        self.assertEqual([f.payload["text"] for f in self.frames], ["Title", "Name 1"])

    def test_placeholder_textdata_run_is_not_a_stream(self):
        # v1.6.7: a TrackNames read of a 10-track disc returns indexes 11-20
        # as 0x01 TextData filler. That's an ordinary reply, not the stream.
        for index in range(11, 21):
            data = bytes([0x03, 0x00, index, 0x01, 0x01, 0x17, 0x00, 0x01])
            self.fake.feed(proto.encode_frame(proto.CMD_TEXT_DATA, data))
        self.fake.feed(bytes([proto.EOT]))
        closed, _ = self.conn._drain_replies(deadline=time.monotonic() + 2.0)
        self.assertTrue(closed)
        self.assertEqual(len(self.frames), 10)


class TestCutOff(_LinkTestBase):
    def test_resent_frame_is_skipped_whole_and_changer_eot_acked(self):
        # As logged at 14:12:07: after our EOT the changer re-sends its next
        # frame (slot byte 0x04 in it!) and then sends EOT.
        self.fake.feed(_stream_frame(0x29, 1) + _stream_frame(0x29, 1) + bytes([proto.EOT]))
        self.conn._cut_off_text_stream()
        # Our EOT, then only the ACK of the changer's EOT: the re-sent
        # frames were not ACK'd, and their 0x04 bytes weren't taken for EOT.
        self.assertEqual(self.fake.sent_bytes(), bytes([proto.EOT, proto.ACK]))

    def test_gives_up_when_the_line_goes_quiet(self):
        old = link_mod.T_STREAM_QUIET
        link_mod.T_STREAM_QUIET = 0.2
        try:
            self.fake.feed(_stream_frame(0x29, 1))
            start = time.monotonic()
            self.conn._cut_off_text_stream()
            self.assertLess(time.monotonic() - start, 2.0)
        finally:
            link_mod.T_STREAM_QUIET = old
        self.assertEqual(self.fake.sent_bytes(), bytes([proto.EOT]))


class TestSendEndToEnd(_LinkTestBase):
    """A DiscName read through the IO thread, as at 14:38:50, then the next
    queued read (DiscGenre, 14:39:28's reply) going through normally."""

    def setUp(self):
        super().setUp()
        self.conn._stop.clear()
        self.conn._thread = threading.Thread(target=self.conn._io_loop, daemon=True)
        self.conn._thread.start()

    def tearDown(self):
        self.conn._stop.set()
        self.conn._thread.join(timeout=2)

    def _wait_sent(self, n: int, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.fake.sent_bytes()) < n:
            self.assertLess(time.monotonic(), deadline, "link never sent the expected bytes")
            time.sleep(0.01)

    def _send_in_thread(self, request: bytes) -> dict:
        result = {}

        def run():
            try:
                self.conn.send(proto.CMD_DATA_ACCESS, request, timeout=10.0)
            except Exception as exc:
                result["error"] = exc
        result["thread"] = threading.Thread(target=run, daemon=True)
        result["thread"].start()
        return result

    def _answer_request(self, already_sent: int) -> None:
        """ACK our ENQ, then ACK our 12-byte DataAccess frame."""
        self._wait_sent(already_sent + 1)
        self.fake.feed(bytes([proto.ACK]))
        self._wait_sent(already_sent + 1 + 12)
        self.fake.feed(bytes([proto.ACK]))

    def test_stream_raises_then_link_recovers(self):
        request = proto.encode_data_access(proto.Action.RETRIEVE_DATA, proto.DataType.TEXT_DATA,
                                           slot=4, info_type=proto.InfoType.DISC_NAMES)
        self.assertEqual(proto.encode_frame(proto.CMD_DATA_ACCESS, request),
                         _bytes("02 03 07 00 00 01 04 00 00 00 00 f1"))
        result = self._send_in_thread(request)
        self._answer_request(0)
        self.fake.feed(b"".join(_stream_frame(seq) for seq in range(1, 7)) + bytes([proto.EOT]))
        result["thread"].join(timeout=10)
        self.assertIsInstance(result.get("error"), PCLinkTextStream)

        n = link_mod.TEXT_STREAM_FRAMES
        sent = self.fake.sent_bytes()
        self.assertEqual(sent[0], proto.ENQ)
        self.assertEqual(sent[13:], bytes([proto.ACK]) * n + bytes([proto.EOT, proto.ACK]))

        genre_request = proto.encode_data_access(proto.Action.RETRIEVE_DATA,
                                                 proto.DataType.DISC_GENRE, slot=4)
        result = self._send_in_thread(genre_request)
        self._answer_request(len(sent))
        self.fake.feed(_bytes("02 08 03 00 04 00 0b e6") + bytes([proto.EOT]))
        result["thread"].join(timeout=10)
        self.assertNotIn("error", result)
        self.assertEqual(self.frames[-1].payload["genre"], 0x0B)


class TestAppIgnoresTheStream(unittest.TestCase):
    def _fake_app(self):
        app = types.SimpleNamespace(logged=[], cached=[], formats=[])
        app._log = app.logged.append
        app._cache_name = app.cached.append
        app._note_disc_format = lambda slot, fmt: app.formats.append((slot, fmt))
        return app

    def _frame(self, raw: bytes):
        data = raw[4:-1]
        return proto.Frame(command=raw[1], data=data, payload=proto.decode_payload(raw[1], data))

    def test_stream_frames_not_cached(self):
        app = self._fake_app()
        for seq in (1, 2, 3):
            pclink_app.App._on_frame(app, self._frame(_stream_frame(seq)))
        self.assertEqual(app.cached, [])
        self.assertEqual(sum("not stored names" in line for line in app.logged), 1)

    def test_stored_name_still_cached(self):
        app = self._fake_app()
        pclink_app.App._on_frame(app, self._frame(_bytes(STORED_DISC_NAME)))
        self.assertEqual([p["text"] for p in app.cached], ["Title"])


class TestRescanKeepsUnreadFields(unittest.TestCase):
    # Slot 4 as the saved scan had it before the 14:14:44 rescan.
    SAVED = {"slot": 4, "track_count": 10, "name": "Acoustic Christmas Celebr",
             "genre": "Folk", "userfiles": [], "tracks": {"1": "Silent Night"}}

    def _library(self):
        return {"discs": [dict(self.SAVED)]}

    def test_unread_fields_kept(self):
        # What that rescan read: DiscInfo only, everything else failed.
        fresh = {"slot": 4, "track_count": 10, "name": None, "genre": None,
                 "userfiles": None, "tracks": None}
        merged, kept = library_browser.keep_unread_fields(self._library(), fresh)
        self.assertEqual(merged, self.SAVED)
        self.assertEqual(kept, ["name", "tracks", "genre", "userfiles"])

    def test_read_fields_replace_saved_ones(self):
        fresh = {"slot": 4, "track_count": 10, "name": "Title", "genre": "Folk",
                 "userfiles": [], "tracks": None}
        merged, kept = library_browser.keep_unread_fields(self._library(), fresh)
        self.assertEqual(merged["name"], "Title")
        self.assertEqual(merged["tracks"], {"1": "Silent Night"})
        self.assertEqual(kept, ["tracks"])

    def test_empty_name_is_a_real_read(self):
        fresh = {"slot": 4, "track_count": 10, "name": "", "genre": "Folk",
                 "userfiles": [], "tracks": {}}
        merged, kept = library_browser.keep_unread_fields(self._library(), fresh)
        self.assertEqual((merged["name"], kept), ("", []))

    def test_new_slot_unchanged(self):
        fresh = {"slot": 9, "track_count": 5, "name": None, "genre": None,
                 "userfiles": None, "tracks": None}
        self.assertEqual(library_browser.keep_unread_fields(self._library(), fresh), (fresh, []))


class TestHardwareCutOff1459(_LinkTestBase):
    """v1.12.4's first hardware run (14:59:14-24): after our EOT the changer
    re-sent seq 6 five times, 2s apart, then sent EOT."""

    def test_five_resends_then_eot(self):
        resent = _bytes("02 fd 09 00 04 00 00 00 00 0b 90 06 01 54")
        self.assertEqual(resent, _stream_frame(6))
        self.fake.feed(resent * 5 + bytes([proto.EOT]))
        self.conn._cut_off_text_stream()
        self.assertEqual(self.fake.sent_bytes(), bytes([proto.EOT, proto.ACK]))


class TestSendWaitsForStartedTransaction(_LinkTestBase):
    """v1.12.5: send()'s timeout covers only the wait to START. The 10s
    cut-off outlasted the old flat 5s, the caller retried, and the retry
    started the stream again (14:59:24)."""

    def test_started_request_is_waited_for(self):
        req = link_mod._SendRequest(proto.CMD_DATA_ACCESS, b"")
        req.started.set()
        threading.Timer(0.3, req.done.set).start()
        self.assertTrue(PCLinkConnection._wait_done(req, 0.05))
        self.assertFalse(req.cancelled)

    def test_default_wait_outlasts_a_stream_read(self):
        # v1.12.12, 17:22:27-17:22:39 (2026-09-26): the disc-name read held
        # the line ~12s; the DiscTOC auto-fetch queued behind it gave up
        # after three 5s waits. The default wait now covers a whole stream
        # read: ~2s before the first frame, then the cut-off.
        worst_stream_read = 2.0 + link_mod.T_STREAM_RESYNC_MAX + link_mod.T_ACK * 3
        self.assertGreater(link_mod.T_QUEUE_WAIT, worst_stream_read)

    def test_queued_request_waits_its_turn_by_default(self):
        # Scaled down: the IO thread picks the request up only after 0.3s
        # (as if busy with a stream); the default wait lets it through.
        def io_thread():
            req = self.conn._out_q.get(timeout=1.0)
            time.sleep(0.3)
            req.started.set()
            req.done.set()
        threading.Thread(target=io_thread, daemon=True).start()
        with unittest.mock.patch.object(link_mod, "T_QUEUE_WAIT", 1.0):
            self.conn.send(proto.CMD_DATA_ACCESS, bytes([0]))  # no timeout passed

    def test_unstarted_request_is_cancelled_and_never_sent(self):
        with self.assertRaises(link_mod.PCLinkTimeout):
            self.conn.send(proto.CMD_DATA_ACCESS, bytes([0]), timeout=0.05)  # no IO thread
        self.conn._try_send_pending()
        self.assertEqual(self.fake.sent_bytes(), b"")  # no ENQ for the abandoned request


# 16:35:19-16:35:21, slot 4 in the drive: each track's CD-Text title,
# asked for with the track in DataAccess's track byte.
CDTEXT_TITLES = {1: "Silent Night", 2: "O Holy Night", 3: "We Wish You A Merry Christmas",
                 4: "Joy To The World", 5: "The First Noel", 6: "God Rest Ye Merry Gentlemen",
                 7: "We Three Kings", 8: "Auld Lang Syne", 9: "O Come All Ye Faithful",
                 10: "Hark! The Herald Angels Sing"}


class _FakeLink:
    """Answers DataAccess reads for slot 4. `in_drive` True: as at 16:35
    (track 0 -- disc name, or all track names -- streams; track N gets its
    full CD-Text title as LongTextData; past track 10, nothing). False: the
    stored names, as at 14:58:29. `track_frames` False: the all-tracks read
    gets no frame at all (14:59:28)."""

    def __init__(self, app, in_drive=True, track_frames=True):
        self.app, self.requests = app, []
        self.in_drive, self.track_frames = in_drive, track_frames

    def send(self, command, data, timeout=5.0):
        data_type, track, info_type = data[1], data[4], data[5]  # see encode_data_access
        self.requests.append((data_type, info_type, track))
        if data_type == proto.DataType.DISC_INFO:
            self.app._disc_track_count[4] = 10
        elif data_type == proto.DataType.TEXT_DATA:
            if self.in_drive:
                if track == 0:
                    raise PCLinkTextStream("slot 4 streamed")
                if track in CDTEXT_TITLES:
                    payload = (bytes([0x04, 0x00, track, 0x00, info_type, 0x06, 0x90, 0x01])
                               + CDTEXT_TITLES[track].encode("ascii"))
                    pclink_app.App._cache_name(self.app, proto.decode_long_text_data(payload))
            elif info_type == proto.InfoType.DISC_NAMES:
                self.app._disc_name_cache[4] = "Title"
            elif self.track_frames:
                self.app._track_name_cache[4] = {1: "Name 1"}
        elif data_type == proto.DataType.DISC_GENRE:
            self.app._genre_cache[4] = 0x0B
        elif data_type == proto.DataType.DISC_USERFILES:
            self.app._userfiles_cache[4] = 0


class TestSlotReadAfterStream(unittest.TestCase):
    def _app(self):
        app = types.SimpleNamespace(
            _disc_track_count={}, _disc_name_cache={}, _track_name_cache={},
            _genre_cache={}, _userfiles_cache={}, _text_stream_slots=set(), _cdtext_slots=set(),
            logged=[],
            _current_slot=4, _update_name_labels=lambda: None,
            _refresh_disc_data_from_changer=lambda: None, _note_userfiles=lambda s, u: None)
        app._log = app.logged.append
        for name in ("_retrieve_sync", "_read_track_names_sync", "_read_tracks_one_by_one_sync",
                     "_read_names_sync", "_note_disc_format"):
            setattr(app, name, getattr(pclink_app.App, name).__get__(app))
        return app

    def test_rescan_reads_tracks_one_by_one_after_disc_name_streams(self):
        app = self._app()
        link = _FakeLink(app)
        state = pclink_app.App._read_slot_state_sync(app, link, 4)
        self.assertIsNone(state["name"])  # not read: the saved name is kept
        self.assertEqual(state["tracks"], CDTEXT_TITLES)
        self.assertEqual((state["genre"], state["userfiles"]), (0x0B, 0))
        text_reads = [(it, t) for dt, it, t in link.requests if dt == proto.DataType.TEXT_DATA]
        # Disc name streams, so no all-tracks read, then tracks 1-10.
        self.assertEqual(text_reads, [(proto.InfoType.DISC_NAMES, 0)]
                         + [(proto.InfoType.TRACK_NAMES, n) for n in range(1, 11)])

    def test_full_titles_not_cut_to_25(self):
        app = self._app()
        pclink_app.App._read_slot_state_sync(app, _FakeLink(app), 4)
        self.assertEqual(app._track_name_cache[4][3], "We Wish You A Merry Christmas")

    def test_unknown_track_count_stops_past_last_track(self):
        app = self._app()
        link = _FakeLink(app)
        app._disc_track_count[4] = 99  # "not played since power-on"
        link_send = link.send

        def send(command, data, timeout=5.0):  # DiscInfo keeps saying 99
            if data[1] == proto.DataType.DISC_INFO:
                app._disc_track_count[4] = 99
                return
            link_send(command, data, timeout)
        link.send = send
        app._text_stream_slots.add(4)
        self.assertTrue(pclink_app.App._read_track_names_sync(app, link, 4))
        self.assertEqual(app._track_name_cache[4], CDTEXT_TITLES)
        tracks_asked = [t for dt, it, t in link.requests if dt == proto.DataType.TEXT_DATA]
        self.assertEqual(tracks_asked, list(range(1, 12)))  # 11 got nothing: stop

    def test_all_tracks_read_that_streams_falls_back(self):
        # Get Track Names on its own (no disc-name read first).
        app = self._app()
        link = _FakeLink(app)
        app._disc_track_count[4] = 10
        self.assertTrue(pclink_app.App._read_track_names_sync(app, link, 4))
        tracks_asked = [t for dt, it, t in link.requests if dt == proto.DataType.TEXT_DATA]
        self.assertEqual(tracks_asked, [0] + list(range(1, 11)))
        self.assertEqual(app._track_name_cache[4], CDTEXT_TITLES)

    def test_auto_fetch_shows_no_disc_title(self):
        app = self._app()
        app._disc_name_cache[4] = "stale"
        pclink_app.App._read_names_sync(app, _FakeLink(app), 4, " [auto]")
        self.assertEqual(app._disc_name_cache[4], "")
        self.assertEqual(app._track_name_cache[4][10], "Hark! The Herald Angels Sing")
        self.assertTrue(any("no CD-Text disc title" in l for l in app.logged))

    def test_track_read_with_no_frames_is_not_an_empty_list(self):
        # 14:59:28: TrackNames ACK'd, no frame in the reply window, our EOT.
        app = self._app()
        state = pclink_app.App._read_slot_state_sync(
            app, _FakeLink(app, in_drive=False, track_frames=False), 4)
        self.assertEqual(state["name"], "Title")
        self.assertIsNone(state["tracks"])

    def test_normal_read_unchanged(self):
        # 14:58:29, slot 3 in the drive: one all-tracks read, as before.
        app = self._app()
        link = _FakeLink(app, in_drive=False)
        state = pclink_app.App._read_slot_state_sync(app, link, 4)
        self.assertEqual((state["name"], state["tracks"]), ("Title", {1: "Name 1"}))
        tracks_asked = [t for dt, it, t in link.requests if dt == proto.DataType.TEXT_DATA]
        self.assertEqual(tracks_asked, [0, 0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
