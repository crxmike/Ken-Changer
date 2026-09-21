#!/usr/bin/env python3
"""
test_write_feature.py
======================
Tests for the write-to-changer feature added on top of v1.1.0:
  - pclink_protocol.py's new write-payload encoders (encode_text_data,
    encode_long_text_data, encode_disc_genre, encode_disc_userfiles,
    encode_disc_listing)
  - pclink_link.py's write choreography (send_write() sending the
    DataAccess request and, if ReadyForData is seen, the actual payload
    as a SEPARATE second transaction -- see pclink_link.py's module
    docstring for why, after a first "inline follow-up" hypothesis was
    tried and disproved on real hardware)

Stdlib-only (unittest), matching the rest of this project -- no pytest,
no pyserial required. Run with:

    python3 test_write_feature.py -v

Per this project's established practice ("every fix gets a test before
being handed back, even without hardware access, using synthetic/mocked
data"): these confirm the new code is internally self-consistent (an
encoder's output decodes back to what went in; the link layer sends the
right bytes in the right order when the changer behaves the way this app
assumes it will). Real hardware testing has since confirmed the write
choreography and encode_text_data specifically (a disc name and ten
track names, written this way, were independently read back and matched
exactly -- see CHANGELOG.md's v1.2.0 entry). The other encoders
(encode_disc_genre/encode_disc_userfiles/encode_disc_listing) are NOT
wired to any UI yet and remain untested against real hardware -- these
tests only prove their internal self-consistency. See README.md's
"Honest gaps" entry #15 for the full history.
"""

from __future__ import annotations

import queue
import struct
import sys
import threading
import time
import types
import unittest

# ---------------------------------------------------------------------------
# Stub out `serial` (pyserial) before importing pclink_link, so these tests
# run without the real dependency installed. Only the handful of symbols
# pclink_link.py references at import time / inside PCLinkConnection.open()
# are needed -- open() itself is never called by these tests (they set
# `.ser` directly to a fake), so serial.Serial is never actually invoked.
# ---------------------------------------------------------------------------
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
import pclink_link as link_mod
from pclink_link import PCLinkConnection, PCLinkNak, PCLinkTimeout, PCLinkWriteUnconfirmed
from pclink_app import gather_disc_data_write_items


# ---------------------------------------------------------------------------
# Protocol-layer: write-payload encoders round-trip through their matching
# read-side decoders.
# ---------------------------------------------------------------------------


class TestWriteEncoders(unittest.TestCase):
    def test_text_data_round_trip_disc_name(self):
        payload = proto.encode_text_data(
            slot=5, index=0, text="Test Album",
            info_type=proto.InfoType.DISC_NAMES, genre=0x05,
            fmt=proto.Format.NO_CDTEXT,
        )
        decoded = proto.decode_text_data(payload)
        self.assertEqual(decoded["slot"], 5)
        self.assertEqual(decoded["index"], 0)
        self.assertEqual(decoded["info_type"], proto.InfoType.DISC_NAMES)
        self.assertEqual(decoded["text"], "Test Album")

    def test_text_data_round_trip_track_name(self):
        payload = proto.encode_text_data(
            slot=12, index=7, text="Track Seven",
            info_type=proto.InfoType.TRACK_NAMES,
        )
        decoded = proto.decode_text_data(payload)
        self.assertEqual(decoded["slot"], 12)
        self.assertEqual(decoded["index"], 7)
        self.assertEqual(decoded["info_type"], proto.InfoType.TRACK_NAMES)
        self.assertEqual(decoded["text"], "Track Seven")

    def test_text_data_empty_string(self):
        # Column 3 entries are always non-empty by the time _write_to_changer
        # builds a request (see pclink_app.py), but the encoder itself
        # shouldn't choke on an empty name.
        payload = proto.encode_text_data(slot=1, index=0, text="")
        decoded = proto.decode_text_data(payload)
        self.assertEqual(decoded["text"], "")

    def test_long_text_data_round_trip(self):
        payload = proto.encode_long_text_data(
            slot=3, track=2, text="Long Text Track",
            info_type=proto.InfoType.TRACK_NAMES,
        )
        decoded = proto.decode_long_text_data(payload)
        self.assertEqual(decoded["slot"], 3)
        self.assertEqual(decoded["track"], 2)
        self.assertEqual(decoded["info_type"], proto.InfoType.TRACK_NAMES)
        self.assertEqual(decoded["text"], "Long Text Track")

    def test_disc_genre_round_trip(self):
        payload = proto.encode_disc_genre(slot=9, genre=0x17)  # Rock
        decoded = proto.decode_disc_genre(payload)
        self.assertEqual(decoded["slot"], 9)
        self.assertEqual(decoded["genre"], 0x17)
        self.assertEqual(decoded["genre_name"], "Rock")

    def test_disc_userfiles_round_trip(self):
        payload = proto.encode_disc_userfiles(slot=4, userfiles=0b00010001)
        decoded = proto.decode_disc_userfiles(payload)
        self.assertEqual(decoded["slot"], 4)
        self.assertEqual(decoded["userfiles"], 0b00010001)
        self.assertIn("Userfile #1", decoded["userfile_names"])
        self.assertIn("Userfile #5", decoded["userfile_names"])

    def test_disc_listing_round_trip(self):
        items = [(1, 1), (1, 2), (5, 0xAA)]
        payload = proto.encode_disc_listing(items)
        decoded = proto.decode_disc_listing(payload)
        self.assertEqual(decoded["length"], 3)
        self.assertEqual(
            [(it["slot"], it["track"]) for it in decoded["items"]], items
        )
        self.assertTrue(decoded["items"][2]["all_tracks"])
        self.assertFalse(decoded["items"][0]["all_tracks"])

    def test_disc_listing_empty(self):
        payload = proto.encode_disc_listing([])
        decoded = proto.decode_disc_listing(payload)
        self.assertEqual(decoded["length"], 0)
        self.assertEqual(decoded["items"], [])

    def test_write_action_data_type_covers_all_write_actions(self):
        # Every write Action (everything except RETRIEVE_DATA) must have an
        # entry, or send_write() callers would have nothing to look up.
        write_actions = {
            proto.Action.SET_DISC_GENRE,
            proto.Action.WRITE_PROGRAM,
            proto.Action.SET_USERFILES,
            proto.Action.WRITE_NAME,
        }
        self.assertEqual(set(proto.WRITE_ACTION_DATA_TYPE.keys()), write_actions)
        # And each maps to the same DataType its DATA_TYPE_TO_REPLY_COMMAND
        # entry uses, so the follow-up frame's command byte can be looked
        # up consistently from either table.
        for action, data_type in proto.WRITE_ACTION_DATA_TYPE.items():
            self.assertIn(data_type, proto.DATA_TYPE_TO_REPLY_COMMAND)

    def test_full_frame_checksum_still_valid_for_a_write_payload(self):
        # Sanity check that a write payload behaves like any other payload
        # when put through the normal frame/checksum machinery -- i.e. the
        # new encoders didn't do anything encode_frame can't handle (e.g.
        # an odd length, non-ascii bytes slipping through).
        payload = proto.encode_text_data(slot=1, index=1, text="Ok")
        frame_bytes = proto.encode_frame(proto.CMD_TEXT_DATA, payload)
        # Re-derive the checksum the way a receiver would and confirm it
        # matches the trailing byte encode_frame produced.
        command = frame_bytes[1]
        length = frame_bytes[2] | (frame_bytes[3] << 8)
        data = frame_bytes[4 : 4 + length]
        checksum = frame_bytes[4 + length]
        self.assertEqual(proto.compute_checksum(command, data), checksum)
        self.assertEqual(data, payload)

    def test_decode_ready_for_data_real_hardware_one_byte_shape(self):
        # Real data point from a user's CD-425M log (a WRITE_NAME attempt
        # for slot 3's disc name): the ReadyForData payload was ONE byte
        # (0x01), not the documented short-slot + byte-info_type (3
        # bytes). Must decode without raising -- decode_payload's
        # try/except used to turn this into an opaque {"error": ...} in
        # every log line.
        decoded = proto.decode_ready_for_data(bytes.fromhex("01"))
        self.assertEqual(decoded, {"raw_byte": 0x01})

        # decode_payload (the dispatcher _on_frame actually calls) must
        # also come back clean, not as an error dict, for this exact
        # real-world frame.
        decoded_via_dispatch = proto.decode_payload(proto.CMD_READY_FOR_DATA, bytes.fromhex("01"))
        self.assertNotIn("error", decoded_via_dispatch)
        self.assertEqual(decoded_via_dispatch["raw_byte"], 0x01)

    def test_decode_ready_for_data_documented_three_byte_shape_still_works(self):
        # If a 3-byte ReadyForData ever does show up (a different
        # request, a different unit), the documented shape should still
        # decode the way it always has.
        payload = struct.pack("<HB", 7, proto.InfoType.TRACK_NAMES)
        decoded = proto.decode_ready_for_data(payload)
        self.assertEqual(decoded, {"slot": 7, "info_type": proto.InfoType.TRACK_NAMES})


# ---------------------------------------------------------------------------
# App layer: gather_disc_data_write_items, the pure (no-Tkinter) piece of
# the Disc Data tab's "Write to Changer" button -- picks which rows'
# Custom column entries actually get sent, and what index/info_type each
# one maps to.
# ---------------------------------------------------------------------------


class _FakeVar:
    """Minimal stand-in for a tkinter.StringVar's .get() -- avoids needing
    a real Tk root just to test row-gathering logic."""

    def __init__(self, value: str):
        self._value = value

    def get(self) -> str:
        return self._value


def _fake_row(text: str) -> dict:
    return {"custom_var": _FakeVar(text)}


class TestGatherDiscDataWriteItems(unittest.TestCase):
    def test_row_zero_is_disc_name_others_are_tracks(self):
        rows = [_fake_row("Album X"), _fake_row("Track One"), _fake_row("Track Two")]
        items = gather_disc_data_write_items(rows)
        self.assertEqual(
            items,
            [
                (0, "Album X", proto.InfoType.DISC_NAMES, "Disc Name"),
                (1, "Track One", proto.InfoType.TRACK_NAMES, "Track 1"),
                (2, "Track Two", proto.InfoType.TRACK_NAMES, "Track 2"),
            ],
        )

    def test_blank_and_whitespace_only_rows_are_skipped(self):
        rows = [_fake_row(""), _fake_row("Track One"), _fake_row("   "), _fake_row("Track Three")]
        items = gather_disc_data_write_items(rows)
        self.assertEqual([label for *_rest, label in items], ["Track 1", "Track 3"])

    def test_surrounding_whitespace_is_stripped(self):
        rows = [_fake_row("  Album X  ")]
        items = gather_disc_data_write_items(rows)
        self.assertEqual(items[0][1], "Album X")

    def test_no_rows_gives_no_items(self):
        self.assertEqual(gather_disc_data_write_items([]), [])


# ---------------------------------------------------------------------------
# Link layer: the write choreography inside _drain_replies / send_write.
# ---------------------------------------------------------------------------


def _ready_for_data_frame_bytes(slot: int, info_type: int) -> bytes:
    """Builds the raw bytes for a STX-framed ReadyForData message, as if
    the changer had sent it -- there's no encode_ready_for_data() in
    pclink_protocol.py (ReadyForData is changer -> PC only), so the test
    builds it directly, matching decode_ready_for_data's shape."""
    payload = struct.pack("<HB", slot & 0xFFFF, info_type & 0xFF)
    return proto.encode_frame(proto.CMD_READY_FOR_DATA, payload)


class FakeSerial:
    """A minimal in-memory stand-in for pyserial's Serial, implementing
    just the read/write/is_open surface pclink_link.py actually uses.
    `feed()` queues bytes for the connection to "receive"; `sent_bytes()`
    returns everything the connection has "transmitted" so far."""

    def __init__(self):
        self._to_conn: "queue.Queue[int]" = queue.Queue()
        self._from_conn = bytearray()
        self._lock = threading.Lock()

    def read(self, n: int = 1) -> bytes:
        assert n == 1, "pclink_link only ever reads one byte at a time"
        try:
            b = self._to_conn.get(timeout=0.05)
        except queue.Empty:
            return b""
        return bytes([b])

    def write(self, data: bytes) -> int:
        with self._lock:
            self._from_conn += data
        return len(data)

    @property
    def is_open(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def feed(self, data: bytes) -> None:
        for b in data:
            self._to_conn.put(b)

    def sent_bytes(self) -> bytes:
        with self._lock:
            return bytes(self._from_conn)


class TestDrainRepliesReadyForDataDetection(unittest.TestCase):
    """Exercises _drain_replies directly (synchronously, no IO thread, no
    ENQ/ACK preamble) -- the request frame's own ENQ/ACK/frame/ACK dance
    is already covered by this app's existing hardware-confirmed history
    (see CHANGELOG.md). What's new here is that _drain_replies no longer
    sends anything of its own on the write path -- it only *detects and
    reports* whether a ReadyForData frame showed up, via the
    `saw_ready_for_data` half of its (changer_closed, saw_ready_for_data)
    return value. What to DO about that (open a second transaction) is
    now send_write()'s job -- see TestSendWriteEndToEnd."""

    def setUp(self):
        self.conn = PCLinkConnection("FAKE")
        self.fake = FakeSerial()
        self.conn.ser = self.fake  # bypass open(); no real port needed

    def test_detects_ready_for_data_via_bare_stx(self):
        self.fake.feed(_ready_for_data_frame_bytes(slot=5, info_type=proto.InfoType.DISC_NAMES))
        self.fake.feed(bytes([link_mod.proto.EOT]))

        changer_closed, saw_ready_for_data = self.conn._drain_replies(
            deadline=time.monotonic() + 2.0
        )

        self.assertTrue(changer_closed)
        self.assertTrue(saw_ready_for_data)
        # Nothing beyond ACKing the ReadyForData frame and the EOT should
        # have been transmitted -- this method no longer sends a write
        # payload of its own.
        self.assertEqual(self.fake.sent_bytes(), bytes([link_mod.proto.ACK, link_mod.proto.ACK]))

    def test_detects_ready_for_data_via_fresh_enq(self):
        # Confirmed-elsewhere pattern: a reply can arrive as its own fresh
        # ENQ-initiated transaction rather than riding inline. ReadyForData
        # should be recognized either way.
        self.fake.feed(bytes([link_mod.proto.ENQ]))
        self.fake.feed(_ready_for_data_frame_bytes(slot=5, info_type=proto.InfoType.DISC_NAMES))
        self.fake.feed(bytes([link_mod.proto.EOT]))

        changer_closed, saw_ready_for_data = self.conn._drain_replies(
            deadline=time.monotonic() + 2.0
        )

        self.assertTrue(changer_closed)
        self.assertTrue(saw_ready_for_data)

    def test_no_ready_for_data_when_absent(self):
        # An ordinary reply (e.g. a read's TextData) with no ReadyForData
        # anywhere in it.
        read_payload = proto.encode_text_data(slot=5, index=0, text="Existing Name")
        self.fake.feed(proto.encode_frame(proto.CMD_TEXT_DATA, read_payload))
        self.fake.feed(bytes([link_mod.proto.EOT]))

        received = []
        self.conn.on_frame = received.append

        changer_closed, saw_ready_for_data = self.conn._drain_replies(
            deadline=time.monotonic() + 2.0
        )

        self.assertTrue(changer_closed)
        self.assertFalse(saw_ready_for_data)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].payload["text"], "Existing Name")

    def test_deadline_reached_with_nothing_received(self):
        changer_closed, saw_ready_for_data = self.conn._drain_replies(
            deadline=time.monotonic() + 0.1
        )
        self.assertFalse(changer_closed)
        self.assertFalse(saw_ready_for_data)
        self.assertEqual(self.fake.sent_bytes(), b"")


class TestSendWriteEndToEnd(unittest.TestCase):
    """Integration tests through PCLinkConnection.send_write(), with the
    IO thread actually running. Covers the write choreography CONFIRMED
    against real hardware (a write followed by an independent read-back
    matched exactly): the DataAccess request and the actual payload are
    TWO SEPARATE transactions (fresh ENQ for each), not one transaction
    with an inline follow-up frame -- revised after a first real hardware
    test showed the inline-follow-up approach getting an immediate EOT
    instead of ACK, with the target value not actually changing. See
    pclink_link.py's module docstring for the full reasoning."""

    def setUp(self):
        self.conn = PCLinkConnection("FAKE")
        self.fake = FakeSerial()
        self.conn.ser = self.fake
        self.conn._stop.clear()
        self.conn._thread = threading.Thread(target=self.conn._io_loop, daemon=True)
        self.conn._thread.start()

    def tearDown(self):
        self.conn._stop.set()
        self.conn._thread.join(timeout=2)

    def _wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_send_write_uses_two_separate_transactions(self):
        request_data = proto.encode_data_access(
            proto.Action.WRITE_NAME, proto.DataType.TEXT_DATA, slot=5,
            info_type=proto.InfoType.DISC_NAMES,
        )
        follow_up_data = proto.encode_text_data(slot=5, index=0, text="Album X")
        request_frame = proto.encode_frame(proto.CMD_DATA_ACCESS, request_data)
        follow_up_frame = proto.encode_frame(proto.CMD_TEXT_DATA, follow_up_data)

        errors = []

        def do_call():
            try:
                self.conn.send_write(
                    proto.CMD_DATA_ACCESS, request_data,
                    proto.CMD_TEXT_DATA, follow_up_data,
                    timeout=5.0,
                )
            except Exception as exc:  # noqa: BLE001 - surface to the test thread
                errors.append(exc)

        caller_thread = threading.Thread(target=do_call, daemon=True)
        caller_thread.start()

        # -- Transaction 1: the DataAccess request --
        self.assertTrue(self._wait_until(lambda: len(self.fake.sent_bytes()) >= 1))
        self.assertEqual(self.fake.sent_bytes()[:1], bytes([link_mod.proto.ENQ]))
        self.fake.feed(bytes([link_mod.proto.ACK]))  # ACK of ENQ

        self.assertTrue(
            self._wait_until(lambda: len(self.fake.sent_bytes()) >= 1 + len(request_frame))
        )
        self.fake.feed(bytes([link_mod.proto.ACK]))  # ACK of the request frame

        self.fake.feed(_ready_for_data_frame_bytes(slot=5, info_type=proto.InfoType.DISC_NAMES))
        self.fake.feed(bytes([link_mod.proto.EOT]))  # changer closes transaction 1

        # -- Transaction 2: a FRESH ENQ for the actual payload, only after
        # transaction 1 is fully closed -- this is the crux of the
        # "separate transaction" hypothesis, so check for a genuinely new
        # ENQ byte after transaction 1's own EOT/ACK exchange, not just
        # that the follow-up frame eventually appears somewhere.
        # ENQ + request frame + our ACK of the ReadyForData frame itself
        # (written by _read_and_ack_frame) + our ACK of transaction 1's
        # closing EOT.
        len_before_txn2 = 1 + len(request_frame) + 2
        self.assertTrue(
            self._wait_until(lambda: len(self.fake.sent_bytes()) > len_before_txn2),
            f"transaction 2 never started; got {self.fake.sent_bytes()!r}",
        )
        sent_so_far = self.fake.sent_bytes()
        self.assertEqual(
            sent_so_far[len_before_txn2], link_mod.proto.ENQ,
            f"expected a fresh ENQ opening transaction 2; got {sent_so_far!r}",
        )
        self.fake.feed(bytes([link_mod.proto.ACK]))  # ACK of transaction 2's ENQ

        self.assertTrue(
            self._wait_until(
                lambda: len(self.fake.sent_bytes()) >= len_before_txn2 + 1 + len(follow_up_frame)
            ),
            f"follow-up frame never arrived; got {self.fake.sent_bytes()!r}",
        )
        self.fake.feed(bytes([link_mod.proto.ACK]))  # ACK of the follow-up frame

        # Transaction 2 has no further reply data -- the PC should close
        # it with its own EOT (same confirmed pattern as DoAction etc.).
        self.assertTrue(
            self._wait_until(
                lambda: self.fake.sent_bytes().endswith(bytes([link_mod.proto.EOT]))
            ),
            f"PC never sent its own closing EOT for transaction 2; got {self.fake.sent_bytes()!r}",
        )
        self.fake.feed(bytes([link_mod.proto.ACK]))  # ACK of our EOT

        caller_thread.join(timeout=3)
        self.assertFalse(caller_thread.is_alive(), "send_write() never returned")
        self.assertEqual(errors, [], f"send_write() raised: {errors}")

        sent = self.fake.sent_bytes()
        self.assertIn(request_frame, sent)
        self.assertIn(follow_up_frame, sent)
        self.assertLess(sent.index(request_frame), sent.index(follow_up_frame))

    def test_send_write_raises_unconfirmed_and_sends_nothing_if_no_ready_for_data(self):
        request_data = proto.encode_data_access(
            proto.Action.WRITE_NAME, proto.DataType.TEXT_DATA, slot=5,
            info_type=proto.InfoType.DISC_NAMES,
        )
        follow_up_data = proto.encode_text_data(slot=5, index=0, text="Album X")
        request_frame = proto.encode_frame(proto.CMD_DATA_ACCESS, request_data)
        follow_up_frame = proto.encode_frame(proto.CMD_TEXT_DATA, follow_up_data)

        result = {}

        def do_call():
            try:
                self.conn.send_write(
                    proto.CMD_DATA_ACCESS, request_data,
                    proto.CMD_TEXT_DATA, follow_up_data,
                    timeout=5.0,
                )
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        caller_thread = threading.Thread(target=do_call, daemon=True)
        caller_thread.start()

        self.assertTrue(self._wait_until(lambda: len(self.fake.sent_bytes()) >= 1))
        self.fake.feed(bytes([link_mod.proto.ACK]))  # ACK of ENQ
        self.assertTrue(
            self._wait_until(lambda: len(self.fake.sent_bytes()) >= 1 + len(request_frame))
        )
        self.fake.feed(bytes([link_mod.proto.ACK]))  # ACK of the request frame
        # No ReadyForData -- changer just closes the transaction outright.
        self.fake.feed(bytes([link_mod.proto.EOT]))

        caller_thread.join(timeout=3)
        self.assertFalse(caller_thread.is_alive(), "send_write() never returned")
        self.assertIsInstance(result.get("error"), PCLinkWriteUnconfirmed)
        self.assertNotIn(
            follow_up_frame, self.fake.sent_bytes(),
            "the write payload must never be sent if ReadyForData never arrived",
        )

    def test_send_write_propagates_nak_from_second_transaction(self):
        # If the changer NAKs the actual payload frame (transaction 2),
        # send_write() should propagate PCLinkNak, same as it would for
        # any other rejected frame.
        request_data = proto.encode_data_access(
            proto.Action.WRITE_NAME, proto.DataType.TEXT_DATA, slot=5,
            info_type=proto.InfoType.DISC_NAMES,
        )
        follow_up_data = proto.encode_text_data(slot=5, index=0, text="Album X")
        request_frame = proto.encode_frame(proto.CMD_DATA_ACCESS, request_data)
        follow_up_frame = proto.encode_frame(proto.CMD_TEXT_DATA, follow_up_data)

        result = {}

        def do_call():
            try:
                self.conn.send_write(
                    proto.CMD_DATA_ACCESS, request_data,
                    proto.CMD_TEXT_DATA, follow_up_data,
                    timeout=5.0,
                )
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        caller_thread = threading.Thread(target=do_call, daemon=True)
        caller_thread.start()

        self.assertTrue(self._wait_until(lambda: len(self.fake.sent_bytes()) >= 1))
        self.fake.feed(bytes([link_mod.proto.ACK]))
        self.assertTrue(
            self._wait_until(lambda: len(self.fake.sent_bytes()) >= 1 + len(request_frame))
        )
        self.fake.feed(bytes([link_mod.proto.ACK]))
        self.fake.feed(_ready_for_data_frame_bytes(slot=5, info_type=proto.InfoType.DISC_NAMES))
        self.fake.feed(bytes([link_mod.proto.EOT]))  # closes transaction 1

        len_before_txn2 = 1 + len(request_frame) + 2
        self.assertTrue(
            self._wait_until(lambda: len(self.fake.sent_bytes()) > len_before_txn2)
        )
        self.fake.feed(bytes([link_mod.proto.ACK]))  # ACK of transaction 2's ENQ
        self.assertTrue(
            self._wait_until(
                lambda: len(self.fake.sent_bytes()) >= len_before_txn2 + 1 + len(follow_up_frame)
            )
        )
        self.fake.feed(bytes([link_mod.proto.NAK]))  # changer rejects the payload frame

        caller_thread.join(timeout=3)
        self.assertFalse(caller_thread.is_alive())
        self.assertIsInstance(result.get("error"), PCLinkNak)

    def test_encoders_match_real_hardware_session_bytes(self):
        # Anchors the encoders to the exact bytes seen in a real user's
        # session (slot 3, disc name "TEST NAME") -- a pure sanity check
        # that today's encoders still produce byte-identical frames to
        # what was actually observed on the wire. (This used to be dead
        # code accidentally appended after the previous test's return --
        # it ran, but only as unreachable-looking tail code glommed onto
        # test_send_write_propagates_nak_from_second_transaction rather
        # than as its own named test.)
        request_data = proto.encode_data_access(
            proto.Action.WRITE_NAME, proto.DataType.TEXT_DATA, slot=3,
            info_type=proto.InfoType.DISC_NAMES,
        )
        follow_up_data = proto.encode_text_data(slot=3, index=0, text="TEST NAME")
        self.assertEqual(
            proto.encode_frame(proto.CMD_DATA_ACCESS, request_data).hex(),
            "020307008001030000000072",
        )
        self.assertEqual(
            proto.encode_frame(proto.CMD_TEXT_DATA, follow_up_data).hex(),
            "02fe10000300000000000054455354204e414d456e",
        )
        # And the real ReadyForData reply from that session:
        self.assertEqual(proto.decode_payload(proto.CMD_READY_FOR_DATA, bytes.fromhex("01")), {"raw_byte": 1})


class TestOrdinarySendRegression(unittest.TestCase):
    """Not about the write feature itself -- a guard against the follow_up
    plumbing added to _do_send/_drain_replies having changed behavior for
    every OTHER command (DoAction, ChangeDisc, plain DataAccess reads,
    etc.), which don't pass a follow_up at all. Confirms the
    already-hardware-confirmed "we send our own closing EOT when there's
    no reply data" behavior (see CHANGELOG.md) still holds via the
    ordinary send() path."""

    def setUp(self):
        self.conn = PCLinkConnection("FAKE")
        self.fake = FakeSerial()
        self.conn.ser = self.fake
        self.conn._stop.clear()
        self.conn._thread = threading.Thread(target=self.conn._io_loop, daemon=True)
        self.conn._thread.start()

    def tearDown(self):
        self.conn._stop.set()
        self.conn._thread.join(timeout=2)

    def _wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_do_action_sends_its_own_closing_eot(self):
        data = proto.encode_do_action(proto.ActionCommand.PLAY_PAUSE)
        frame = proto.encode_frame(proto.CMD_DO_ACTION, data)

        errors = []

        def do_call():
            try:
                self.conn.send(proto.CMD_DO_ACTION, data, timeout=5.0)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=do_call, daemon=True)
        t.start()

        self.assertTrue(self._wait_until(lambda: len(self.fake.sent_bytes()) >= 1))
        self.fake.feed(bytes([link_mod.proto.ACK]))  # ACK of ENQ

        self.assertTrue(
            self._wait_until(lambda: len(self.fake.sent_bytes()) >= 1 + len(frame))
        )
        self.fake.feed(bytes([link_mod.proto.ACK]))  # ACK of the frame itself

        # DoAction has no reply data and a 0.0s reply window -- the PC
        # should send its own closing EOT without anything further from us.
        self.assertTrue(
            self._wait_until(
                lambda: self.fake.sent_bytes().endswith(bytes([link_mod.proto.EOT]))
            ),
            f"PC never sent its own closing EOT; got {self.fake.sent_bytes()!r}",
        )
        self.fake.feed(bytes([link_mod.proto.ACK]))  # ACK of our EOT

        t.join(timeout=3)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
