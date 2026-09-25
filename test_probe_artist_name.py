#!/usr/bin/env python3
"""
test_probe_artist_name.py
=========================
Tests for probe_artist_name.py, the one-off experiment asking whether the
CD-425M supports the "artist name" text type (info_type 0x02). Nothing is
known about how the real changer answers, so the fake changer here plays
each outcome the probe has to report sensibly: it stores an artist, it
sends nothing back, or it ignores info_type and treats the request as a
disc name (the dangerous one for a write).

Slot 1's baseline is the user's real disc from the v1.12.1 log: genre 0x03
(Alternative Rock), userfiles 0x02.

    python test_probe_artist_name.py -v
"""
import struct
import unittest

import pclink_protocol as proto
from pclink_link import PCLinkRejected, PCLinkTimeout, PCLinkWriteUnconfirmed
from probe_artist_name import Probe

PLACEHOLDER = "\x01"


class _FakeChanger:
    """Answers reads by calling on_frame during send(), as the real link
    does. `artist_mode`: "stores" (keeps an artist per slot, replies with
    info_type 0x02), "silent" (ACKs, no reply), "as_disc_name" (treats
    info_type 0x02 like disc names, for reads and writes), "cd425m" (what
    the real changer did in v1.12.3: no reply to the read, ReadyForData to
    the write request, then the payload refused with EOT)."""

    def __init__(self, artist_mode="stores", no_ready=False):
        self.on_frame = None
        self.artist_mode = artist_mode
        self.no_ready = no_ready
        self.disc = {"name": "Nevermind", "genre": 0x03, "userfiles": 0x02, "artist": None}
        self.sent = []  # (command, data) of every transaction we were sent
        self.fail_genre = False

    def _reply(self, command, data):
        self.on_frame(proto.Frame(command, data, proto.decode_payload(command, data)))

    def _text(self, info_type, text):
        d = self.disc
        self._reply(proto.CMD_TEXT_DATA, proto.encode_text_data(
            slot=1, index=0, text=text or PLACEHOLDER, info_type=info_type,
            genre=d["genre"], userfiles=d["userfiles"]))

    def send(self, command, data, timeout=5.0):
        self.sent.append((command, data))
        action, data_type, slot, _, info_type, _ = struct.unpack("<BBHBBB", data)
        assert action == proto.Action.RETRIEVE_DATA and slot == 1
        d = self.disc
        if data_type == proto.DataType.TEXT_DATA and info_type == proto.InfoType.DISC_NAMES:
            self._text(info_type, d["name"])
        elif data_type == proto.DataType.TEXT_DATA and info_type == proto.InfoType.ARTIST_NAME:
            if self.artist_mode == "stores":
                self._text(info_type, d["artist"])
            elif self.artist_mode == "as_disc_name":
                self._text(proto.InfoType.DISC_NAMES, d["name"])
        elif data_type == proto.DataType.DISC_GENRE:
            if self.fail_genre:
                raise PCLinkTimeout("simulated")
            self._reply(proto.CMD_DISC_GENRE, proto.encode_disc_genre(1, d["genre"]))
        elif data_type == proto.DataType.DISC_USERFILES:
            self._reply(proto.CMD_DISC_USERFILES, proto.encode_disc_userfiles(1, d["userfiles"]))
        else:
            raise AssertionError(f"unexpected read {data_type}")

    def send_write(self, command, data, follow_up_command, follow_up_data, timeout=8.0):
        self.sent.append((command, data))
        if self.no_ready:
            raise PCLinkWriteUnconfirmed("no ReadyForData")
        self.sent.append((follow_up_command, follow_up_data))
        if self.artist_mode == "cd425m":
            raise PCLinkRejected("payload answered with EOT")
        p = proto.decode_text_data(follow_up_data)
        d = self.disc
        # CONFIRMED (v1.5.1, v1.8.2): every TextData write sets these.
        d["genre"], d["userfiles"] = p["genre"], p["userfiles"]
        if self.artist_mode == "stores":
            d["artist"] = p["text"]
        elif self.artist_mode == "as_disc_name":
            d["name"] = p["text"][:25]


def _run(changer, write=None):
    lines = []
    result = Probe(changer, 1, out=lines.append, settle=0).run(write)
    return result, "\n".join(lines)


class TestReadOnly(unittest.TestCase):
    def test_sends_the_artist_request(self):
        changer = _FakeChanger()
        _run(changer)
        artist_reads = [d for c, d in changer.sent if d[4:6] == bytes([0, 0x02])]
        # action 0 (retrieve), data_type 1 (TextData), slot 1, unknown 0, info_type 2, genre 0
        self.assertEqual(artist_reads, [bytes.fromhex("00 01 01 00 00 02 00")])

    def test_read_only_never_writes(self):
        changer = _FakeChanger()
        result, _ = _run(changer)
        self.assertFalse(result["wrote"])
        self.assertTrue(all(d[0] == proto.Action.RETRIEVE_DATA for _, d in changer.sent))

    def test_baseline_from_the_real_disc(self):
        result, text = _run(_FakeChanger())
        self.assertEqual(result["baseline"], {"name": "Nevermind", "genre": 0x03, "userfiles": 0x02})

    def test_reports_artist_reply(self):
        changer = _FakeChanger()
        changer.disc["artist"] = "Nirvana"
        _, text = _run(changer)
        self.assertIn("info_type 0x02, index 0: 'Nirvana'", text)

    def test_reports_placeholder_as_no_text(self):
        _, text = _run(_FakeChanger())
        self.assertIn("info_type 0x02, index 0: (no text / placeholder)", text)

    def test_reports_silence(self):
        _, text = _run(_FakeChanger("silent"))
        self.assertIn("ACK'd the request but sent nothing back", text)

    def test_reports_wrong_info_type(self):
        _, text = _run(_FakeChanger("as_disc_name"))
        self.assertIn("info_type 0x00, not 0x02: 'Nevermind'", text)


class TestWrite(unittest.TestCase):
    def test_write_frames_carry_genre_and_userfiles(self):
        changer = _FakeChanger()
        _run(changer, "Nirvana")
        writes = [(c, d) for c, d in changer.sent if c == proto.CMD_TEXT_DATA or d[0] == proto.Action.WRITE_NAME]
        self.assertEqual(writes, [
            # WRITE_NAME, TextData, slot 1, unknown 0, info_type 2, genre 3
            (proto.CMD_DATA_ACCESS, bytes.fromhex("80 01 01 00 00 02 03")),
            # slot 1, index 0, userfiles 0x02, info_type 2, genre 3, format 0, text
            (proto.CMD_TEXT_DATA, bytes.fromhex("01 00 00 02 02 03 00") + b"Nirvana"),
        ])

    def test_supported_write_reads_back_and_leaves_disc_alone(self):
        result, text = _run(_FakeChanger(), "Nirvana")
        self.assertTrue(result["wrote"])
        self.assertEqual(result["changed"], [])
        self.assertIn("info_type 0x02, index 0: 'Nirvana'", text)
        self.assertIn("Disc name, genre and userfiles unchanged.", text)

    def test_write_that_renames_the_disc_is_flagged(self):
        changer = _FakeChanger("as_disc_name")
        result, text = _run(changer, "Nirvana")
        self.assertEqual(result["changed"], ["name"])
        self.assertIn("The disc name was 'Nevermind'", text)

    def test_no_ready_for_data_reported(self):
        result, text = _run(_FakeChanger(no_ready=True), "Nirvana")
        self.assertFalse(result["wrote"])
        self.assertIn("PCLinkWriteUnconfirmed", text)
        self.assertEqual(result["changed"], [])

    def test_real_cd425m_session(self):
        # v1.12.3 (2026-09-25), slot 1, "The Tragically Hip / Trou", genre
        # 0x03, userfiles 0x02: the artist read got nothing back, the write
        # got ReadyForData and then an EOT for the payload, and the re-read
        # showed nothing changed.
        changer = _FakeChanger("cd425m")
        changer.disc["name"] = "The Tragically Hip / Trou"
        result, text = _run(changer, "Nirvana")
        self.assertFalse(result["wrote"])
        self.assertIn("write failed: PCLinkRejected", text)
        self.assertEqual(result["artist_before"], [])
        self.assertEqual(result["artist_after"], [])
        self.assertEqual(result["changed"], [])
        self.assertEqual(
            [d for c, d in changer.sent if c == proto.CMD_TEXT_DATA],
            [bytes.fromhex("01 00 00 02 02 03 00 4e 69 72 76 61 6e 61")])

    def test_refuses_to_write_without_genre(self):
        changer = _FakeChanger()
        changer.fail_genre = True
        result, text = _run(changer, "Nirvana")
        self.assertFalse(result["wrote"])
        self.assertIn("Not writing", text)
        self.assertFalse(any(d[0] == proto.Action.WRITE_NAME for _, d in changer.sent))


if __name__ == "__main__":
    unittest.main()
