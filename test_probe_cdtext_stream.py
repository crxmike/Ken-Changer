#!/usr/bin/env python3
"""
test_probe_cdtext_stream.py
===========================
Tests for probe_cdtext_stream.py (the read-only probe that lets a CD-Text
disc's LongTextData stream run) and the link option it uses,
PCLinkConnection.send(..., reply_window=, let_stream_run=True).

Frames are built like the ones logged from slot 4 on 2026-09-25, e.g.
`02 fd 09 00 04 00 00 00 00 0b 90 01 01 59`. How the stream goes on past
~40 frames is exactly what the probe is for, so the fake changer here plays
the possible outcomes: it wraps seq and keeps going, it ends with EOT, or
the disc isn't in the drive and the stored names come back as TextData.

    python test_probe_cdtext_stream.py -v
"""
from __future__ import annotations

import threading
import time
import unittest

from test_write_feature import FakeSerial  # stubs pyserial if needed

import pclink_link as link_mod
import pclink_protocol as proto
from pclink_link import PCLinkConnection
from probe_cdtext_stream import StreamProbe


def _stream_frame_data(seq: int, info_type: int = 0) -> bytes:
    return bytes([0x04, 0x00, 0x00, 0x00, info_type, 0x0B, 0x90, seq & 0xFF, 0x01])


class TestLetStreamRun(unittest.TestCase):
    """The link option, through the real IO thread and a fake port."""

    def setUp(self):
        self.conn = PCLinkConnection("FAKE")
        self.fake = FakeSerial()
        self.conn.ser = self.fake
        self.frames = []
        self.conn.on_frame = self.frames.append
        self.conn._stop.clear()
        self.conn._thread = threading.Thread(target=self.conn._io_loop, daemon=True)
        self.conn._thread.start()
        self._old_quiet = link_mod.T_STREAM_QUIET
        link_mod.T_STREAM_QUIET = 0.3

    def tearDown(self):
        link_mod.T_STREAM_QUIET = self._old_quiet
        self.conn._stop.set()
        self.conn._thread.join(timeout=2)

    def _wait_sent(self, n):
        deadline = time.monotonic() + 3
        while len(self.fake.sent_bytes()) < n:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)

    def test_frames_past_five_are_acked_then_cut_off_at_window_end(self):
        request = proto.encode_data_access(proto.Action.RETRIEVE_DATA, proto.DataType.TEXT_DATA,
                                           slot=4, info_type=proto.InfoType.DISC_NAMES)
        errors = []

        def run():
            try:
                self.conn.send(proto.CMD_DATA_ACCESS, request, reply_window=0.8,
                               let_stream_run=True)
            except Exception as exc:
                errors.append(exc)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self._wait_sent(1)
        self.fake.feed(bytes([proto.ACK]))
        self._wait_sent(13)
        self.fake.feed(bytes([proto.ACK]))
        for seq in range(1, 9):
            self.fake.feed(proto.encode_frame(proto.CMD_LONG_TEXT_DATA, _stream_frame_data(seq)))
        t.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual([f.payload["seq"] for f in self.frames], list(range(1, 9)))
        # Every frame ACK'd (no cut-off at 5), then our cut-off EOT.
        self.assertEqual(self.fake.sent_bytes()[13:], bytes([proto.ACK]) * 8 + bytes([proto.EOT]))


class _FakeLink:
    """Plays the changer at the send() level, as test_probe_artist_name's
    fake does. `mode`: "stream_wraps" (300 LongTextData frames, seq wrapping
    after 0xFF, still running at the window's end), "stream_ends" (40
    frames then the changer's EOT), "stored" (the disc isn't in the drive:
    stored names as TextData)."""

    def __init__(self, mode):
        self.mode = mode
        self.on_frame = None
        self.on_raw = None
        self.calls = []
        self.unknown_bytes = []
        self.now = 0.0

    def clock(self):
        return self.now

    def _frame(self, command, data):
        self.now += 0.02
        self.on_frame(proto.Frame(command, data, proto.decode_payload(command, data)))

    def send(self, command, data, timeout=5.0, reply_window=None, let_stream_run=False):
        self.calls.append((data[1], data[5], reply_window, let_stream_run))
        self.unknown_bytes.append(data[4])
        data_type, info_type = data[1], data[5]
        self.now += 0.5
        if data_type == proto.DataType.DISC_INFO:
            self._frame(proto.CMD_DISC_INFO, bytes([0x04, 0x00, 0x01, 0x0A, 0x90]))
        elif data_type == proto.DataType.DISC_GENRE:
            self._frame(proto.CMD_DISC_GENRE, bytes([0x04, 0x00, 0x0B]))
        elif data_type == proto.DataType.DISC_USERFILES:
            self._frame(proto.CMD_DISC_USERFILES, bytes([0x04, 0x00, 0x00]))
        elif self.mode == "per_track":
            # Hypothetical, from KENWOODv2.pde: the track in the "unknown"
            # byte gets that track's CD-Text name as LongTextData.
            track = data[4]
            name = {1: "Silent Night", 2: "O Holy Night"}.get(track, "")
            self._frame(proto.CMD_LONG_TEXT_DATA,
                        bytes([0x04, 0x00, track, 0x00, info_type, 0x06, 0x90, 0x01])
                        + name.encode("ascii"))
            self.on_raw("RX", b"", "EOT")
        elif self.mode == "stored":
            names = ["Title"] if info_type == 0 else ["Title", "Name 1", "Name 2"]
            for i, name in enumerate(names):
                self._frame(proto.CMD_TEXT_DATA, proto.encode_text_data(
                    slot=4, index=i, text=name, info_type=info_type, genre=0x0B, fmt=0x90))
            self.on_raw("RX", b"\x04", "EOT")
        else:
            self.now += 1.5  # the ~2s before the stream starts
            count = 300 if self.mode == "stream_wraps" else 40
            for seq in range(1, count + 1):
                self._frame(proto.CMD_LONG_TEXT_DATA, _stream_frame_data(seq, info_type))
            if self.mode == "stream_wraps":
                self.on_raw("TX", b"\x04", "EOT (cutting off endless LongTextData stream)")
            else:
                self.on_raw("RX", b"\x04", "EOT (changer ending the stream)")


class TestStreamProbe(unittest.TestCase):
    def _run(self, mode):
        link = _FakeLink(mode)
        lines = []
        probe = StreamProbe(link, 4, out=lines.append, window=60.0, clock=link.clock, settle=0)
        return probe.run(), link, lines

    def test_name_reads_let_run_other_reads_normal(self):
        _, link, _ = self._run("stream_wraps")
        let_run = [(dt, it) for dt, it, _w, run in link.calls if run]
        self.assertEqual(let_run, [(proto.DataType.TEXT_DATA, 0), (proto.DataType.TEXT_DATA, 1)])
        self.assertTrue(all(w == 60.0 for _dt, _it, w, run in link.calls if run))

    def test_wrapping_stream_reported(self):
        result, _, lines = self._run("stream_wraps")
        s = result["disc_name"]
        self.assertEqual(s["long_text_frames"], 300)
        self.assertEqual(s["seq_wraps"], 1)
        self.assertEqual(s["seq_gaps"], [])
        self.assertEqual(s["distinct"], [("LongTextData", 0, 0, "(placeholder)")])
        self.assertTrue(s["cut_off_by_us"])
        self.assertEqual(s["first_frame_after"], 2.02)
        self.assertTrue(any("still running when the window ended" in l for l in lines))
        self.assertEqual((result["genre"], result["userfiles"]), (0x0B, 0))

    def test_stream_that_ends_reported(self):
        result, _, lines = self._run("stream_ends")
        s = result["track_names"]
        self.assertEqual((s["seq_first"], s["seq_last"]), (1, 40))
        self.assertFalse(s["cut_off_by_us"])
        self.assertTrue(s["changer_sent_eot"])
        self.assertTrue(any("ended by itself (changer's EOT)" in l for l in lines))

    def test_tracks_option_puts_track_in_unknown_byte(self):
        link = _FakeLink("per_track")
        lines = []
        probe = StreamProbe(link, 4, out=lines.append, window=15.0, clock=link.clock, settle=0)
        result = probe.run([1, 2])
        # DiscInfo 0, track 1, track 2, genre 0, userfiles 0.
        self.assertEqual(link.unknown_bytes, [0, 1, 2, 0, 0])
        self.assertEqual(result["track_1"]["distinct"], [("LongTextData", 1, 1, "Silent Night")])
        self.assertEqual(result["track_2"]["distinct"], [("LongTextData", 2, 1, "O Holy Night")])
        self.assertNotIn("disc_name", result)

    def test_get_track_name_frame_matches_kenwoodv2(self):
        # KENWOODv2.pde's get_trackName for disc 1, track 1.
        data = proto.encode_data_access(proto.Action.RETRIEVE_DATA, proto.DataType.TEXT_DATA,
                                        slot=1, info_type=proto.InfoType.TRACK_NAMES, track=1)
        self.assertEqual(proto.encode_frame(proto.CMD_DATA_ACCESS, data),
                         bytes([0x02, 0x03, 0x07, 0x00, 0x00, 0x01, 0x01, 0x00, 0x01, 0x01, 0x00, 0xF2]))

    def test_stored_names_reported(self):
        result, _, _ = self._run("stored")
        s = result["track_names"]
        self.assertEqual(s["long_text_frames"], 0)
        self.assertEqual([k[3] for k in s["distinct"]], ["Title", "Name 1", "Name 2"])
        self.assertEqual(result["disc_info"]["format"], 0x90)


if __name__ == "__main__":
    unittest.main(verbosity=2)
