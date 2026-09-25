"""
pclink_link.py
===============
Serial transport for the Kenwood PC-Link protocol. Owns the physical port and
a single dedicated I/O thread (avoids two threads racing on the same serial
read/write). Both directions -- us sending a request, and the changer pushing
a spontaneous event or a reply -- are handled by the same loop, since only
one side transmits an actual frame at a time (the ENQ/ACK handshake is the
arbitration mechanism the protocol itself provides).

Note on reply framing: per the docs' "Reply Framing" section, reply data
(e.g. the changer's handshake identifier, or data requested via
DataAccess) is sent as a bare STX + payload within the SAME transaction as
the request that prompted it -- there is no fresh ENQ for it. Confirmed
against real CD-425M hardware.

Note on EOT: confirmed against real hardware that the CLOSING EOT of a
transaction is sent by US (the initiator of the request), not the changer
-- for a command with no reply data (e.g. DoAction), the changer just sits
there re-ACKing periodically (about every 2s) until it receives our EOT.
So after our request frame is ACK'd, we drain any reply frame(s) that show
up promptly (bare STX, no ENQ -- see above), then send our own EOT and
wait for the changer's ACK of it, closing the transaction ourselves.

Note on the write choreography (send_write() -- CONFIRMED against real
hardware for WRITE_NAME, after a first hypothesis was tried and
disproved): DataAccess's payload has no room for actual content (a name,
a genre, a userfile mask, a program listing), just
action/data_type/slot/info_type/genre, so the real content has to go in
a separate frame.

A first hypothesis -- send that frame as an inline follow-up within the
SAME transaction, right after the changer's ReadyForData reply -- was
tried against a real CD-425M and did NOT work: the changer responded to
the injected frame with an immediate EOT (not ACK), and the disc name it
targeted did not actually change. On reflection this made structural
sense even before the second test confirmed the fix: nowhere else in
this protocol does the PC ever send a second command-frame within a
transaction it opened in response to something the changer said -- every
other multi-frame exchange either has the changer sending several REPLY
frames back (TrackNames/DiscTOC pagination) or the changer opening its
own fresh ENQ-initiated transaction for something it wants to say
(confirmed reply-framing behavior, see above). ReadyForData is a normal,
first-class command (0x09) with its own row in the command table, ACK'd
like any other frame -- nothing marks it as "still mid-conversation, send
more" rather than "conversation over, go ahead and start a new one when
ready."

The revised hypothesis -- ReadyForData means "close this transaction,
then open a NEW one to actually send the data", i.e. two separate,
complete transactions rather than one transaction with two frames -- was
then tried and CONFIRMED: a real write (disc name + 10 track names for
one disc) was independently read back afterward and matched exactly what
had been written. send_write() implements this: sends the DataAccess
request as an ordinary send() (one full transaction, closed normally);
if that transaction's reply included a ReadyForData frame, sends the
actual payload as its OWN separate send() (a second, independent
transaction, fresh ENQ and all); raises PCLinkWriteUnconfirmed if
ReadyForData was never seen at all, without attempting the second send.
Confirmed specifically for Action.WRITE_NAME / CMD_TEXT_DATA; the same
choreography was tried for a standalone SET_DISC_GENRE and failed (see
CHANGELOG.md v1.4.x); WRITE_PROGRAM uses it too, CONFIRMED in v1.8.1. See CHANGELOG.md's v1.2.0 entry and README.md's "Write
choreography" honest-gap entry for the full history of what was tried
and ruled out along the way.

Usage
-----
    link = PCLinkConnection(port="COM5")           # or "/dev/tty.usbserial-XXXX"
    link.on_frame = my_callback                    # called for every decoded frame
    link.open()
    link.handshake()
    link.send(pclink_protocol.CMD_DO_ACTION,
              pclink_protocol.encode_do_action(pclink_protocol.ActionCommand.PLAY_PAUSE))
    ...
    link.close()
"""

from __future__ import annotations

import struct
import threading
import time
import queue
import logging
from typing import Callable, Optional

import serial  # pyserial

import pclink_protocol as proto
from pclink_protocol import Frame

log = logging.getLogger("pclink")

BAUD = 9600
BYTESIZE = serial.EIGHTBITS
PARITY = serial.PARITY_NONE
STOPBITS = serial.STOPBITS_TWO

# Timeouts (seconds). The changer is not fast; these are generous on purpose.
T_ACK = 1.5  # waiting for ACK/NAK after ENQ or after a frame
T_EOT_ACK = 1.5

# How long we wait, after our request frame is ACK'd, to see whether a reply
# is coming before we close the transaction with our own EOT.
#
# Commands documented to actually produce reply data (Handshake's
# identifier; DataAccess's DiscInfo/DiscTOC/TextData/etc, which may need a
# moment if the changer's reading a TOC off the disc) get a generous
# window. Commands documented as NOT producing any reply -- DoAction,
# ChangeDisc, ChangeMode -- get a near-zero window: confirmed on real
# hardware that DoAction never gets an inline reply (a resulting InfoEvent
# arrives later as its own freshly-ENQ'd transaction, not tucked inside
# this one), so there's nothing to wait for, and every millisecond here is
# pure latency sitting in front of whatever comes next -- which matters a
# lot for e.g. getting "Finish Repeatable" out before the changer commits
# to auto-advancing another track.
REPLY_WINDOW_DEFAULT = 0.4
REPLY_WINDOW_BY_COMMAND = {
    proto.CMD_HANDSHAKE: 3.0,
    proto.CMD_DATA_ACCESS: 3.0,  # triggers DiscInfo/DiscTOC/TextData/etc replies
    proto.CMD_DO_ACTION: 0.0,
    proto.CMD_CHANGE_DISC: 0.0,
    proto.CMD_CHANGE_MODE: 0.0,
}

# Per-byte poll timeout used for the read loop. This is also the practical
# floor on how quickly a "give up waiting" deadline (like the reply windows
# above) actually takes effect, since a single blocking ser.read(1) call
# doesn't know about our intended shorter deadline -- it always waits up to
# this long before returning empty-handed. Kept short so a 0.0s reply
# window is actually fast in practice, not just on paper.
T_BYTE = 0.03


class PCLinkError(Exception):
    pass


class PCLinkTimeout(PCLinkError):
    pass


class PCLinkNak(PCLinkError):
    pass


class PCLinkRejected(PCLinkError):
    """The changer answered our frame with EOT instead of ACK: it received
    the frame and refused it. Seen on a real CD-425M every time a write
    payload was wrong, with nothing written: the v1.2.0 inline-follow-up
    write, v1.4.1/v1.4.2's DiscGenre follow-up frames, and v1.12.3's
    artist-name (info_type 0x02) TextData payload. Before v1.12.3 this was
    taken for an ACK, so a refused write looked like it had gone through.
    (Not the same as a frame that is ACK'd and then followed by EOT with no
    reply, e.g. DiscTOC while changing discs -- that's an empty reply.)"""
    pass


class PCLinkWriteUnconfirmed(PCLinkError):
    """Raised by send_write() when the write's outcome can't be confirmed
    -- currently: the changer never replied with ReadyForData to the
    initiating DataAccess request at all, so the actual payload was never
    sent. Distinct from PCLinkNak (an explicit checksum-level rejection)
    and PCLinkTimeout (no response at all) -- this means "the changer
    said something, but not something that confirms it wanted the write
    to proceed." See pclink_link.py's module docstring and send_write()."""
    pass


class _SendRequest:
    def __init__(self, command: int, data: bytes):
        self.command = command
        self.data = data
        self.done = threading.Event()
        self.error: Optional[Exception] = None
        # Set by _do_send after the transaction completes: True if a
        # ReadyForData frame was seen in the reply -- see send_write().
        self.saw_ready_for_data = False


class PCLinkConnection:
    def __init__(self, port: str):
        self.port_name = port
        self.ser: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._out_q: "queue.Queue[_SendRequest]" = queue.Queue()

        # Called (from the IO thread) for every fully decoded frame, whether
        # it's a spontaneous event from the changer or a reply to something
        # we asked for. GUI code should treat this as "arrived on a
        # background thread" and marshal to the UI thread itself.
        self.on_frame: Optional[Callable[[Frame], None]] = None

        # Called with (direction, raw_bytes, note) for a raw packet monitor /
        # debug console. direction is "TX" or "RX".
        self.on_raw: Optional[Callable[[str, bytes, str], None]] = None

    # -- lifecycle ---------------------------------------------------

    def open(self) -> None:
        self.ser = serial.Serial(
            self.port_name,
            baudrate=BAUD,
            bytesize=BYTESIZE,
            parity=PARITY,
            stopbits=STOPBITS,
            timeout=T_BYTE,
        )
        self._stop.clear()
        self._thread = threading.Thread(target=self._io_loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self.ser and self.ser.is_open:
            self.ser.close()

    def handshake(self, timeout: float = 5.0) -> Frame:
        """Send the "I'm PC" handshake and wait for the changer's reply."""
        return self.send(
            proto.CMD_HANDSHAKE, proto.encode_handshake(), timeout=timeout
        )

    # -- sending -------------------------------------------------------

    def send(self, command: int, data: bytes = b"", timeout: float = 5.0) -> None:
        """Queue a frame to be sent on the IO thread and block until the
        send/ack/eot handshake for it has completed (or raise on error)."""
        req = _SendRequest(command, data)
        self._out_q.put(req)
        if not req.done.wait(timeout):
            raise PCLinkTimeout(
                f"Timed out waiting to send {proto.COMMAND_NAMES.get(command, command)}"
            )
        if req.error:
            raise req.error

    def send_write(
        self,
        command: int,
        data: bytes,
        follow_up_command: int,
        follow_up_data: bytes,
        timeout: float = 8.0,
    ) -> None:
        """The DataAccess write choreography -- CONFIRMED against real
        hardware for Action.WRITE_NAME (a write followed by an
        independent read-back matched exactly), after a first hypothesis
        was tried and disproved (see this module's docstring, "Note on
        the write choreography", for the full reasoning). Also used for
        WRITE_PROGRAM, CONFIRMED in v1.8.1.

        `command`/`data` is the initiating DataAccess request
        (action=WRITE_NAME/SET_DISC_GENRE/WRITE_PROGRAM/SET_USERFILES,
        per pclink_protocol.WRITE_ACTION_DATA_TYPE); it's sent as an
        ordinary, complete transaction via send(). If the changer replied
        with ReadyForData during that transaction, `follow_up_command`/
        `follow_up_data` -- the actual TextData/DiscGenre/DiscUserfiles/
        DiscListing payload -- is sent next as its OWN separate
        transaction (a second, independent send()), rather than injected
        into the first one. If ReadyForData was never seen at all, raises
        PCLinkWriteUnconfirmed without attempting the second send --
        meaning nothing was written.

        Raises PCLinkNak/PCLinkTimeout/PCLinkError from either
        transaction (the first, initiating one, or the second, payload
        one), plus PCLinkWriteUnconfirmed if the first transaction never
        got a ReadyForData reply at all. Check the log (with "show raw
        bytes" on) to see which transaction a given failure came from.
        """
        req = _SendRequest(command, data)
        self._out_q.put(req)
        if not req.done.wait(timeout):
            raise PCLinkTimeout(
                f"Timed out waiting to send "
                f"{proto.COMMAND_NAMES.get(command, command)} (write)"
            )
        if req.error:
            raise req.error
        if not req.saw_ready_for_data:
            raise PCLinkWriteUnconfirmed(
                f"Changer never sent ReadyForData in response to the "
                f"{proto.COMMAND_NAMES.get(command, command)} write "
                f"request -- the actual payload was never sent."
            )
        # A SEPARATE transaction for the actual payload -- see this
        # method's docstring / the module docstring for why.
        self.send(follow_up_command, follow_up_data, timeout=timeout)

    # -- internals -------------------------------------------------------

    def _emit_raw(self, direction: str, data: bytes, note: str = "") -> None:
        if self.on_raw:
            try:
                self.on_raw(direction, data, note)
            except Exception:
                log.exception("on_raw callback failed")

    def _emit_frame(self, frame: Frame) -> None:
        if self.on_frame:
            try:
                self.on_frame(frame)
            except Exception:
                log.exception("on_frame callback failed")

    def _read_byte(self) -> Optional[int]:
        b = self.ser.read(1)
        if not b:
            return None
        return b[0]

    def _write(self, data: bytes, note: str = "") -> None:
        self.ser.write(data)
        self._emit_raw("TX", data, note)

    def _io_loop(self) -> None:
        assert self.ser is not None
        while not self._stop.is_set():
            try:
                b = self._read_byte()
                if b is not None:
                    if b == proto.ENQ:
                        self._handle_incoming_transaction()
                    else:
                        # Stray byte outside of a transaction; log & discard.
                        self._emit_raw("RX", bytes([b]), "(unexpected, discarded)")
                    # Give any pending outgoing send a chance right now,
                    # rather than only when the incoming side goes fully
                    # quiet. This matters a lot in practice: e.g. after
                    # "Next Track", the changer keeps streaming InfoEvents
                    # as it auto-advances for as long as it hasn't received
                    # our "Finish Repeatable" -- if we only checked the
                    # outgoing queue on incoming *silence*, a queued Finish
                    # could be starved indefinitely by back-to-back
                    # changer-initiated transactions, letting it skip many
                    # more tracks than the button was actually held for.
                    self._try_send_pending()
                    continue

                self._try_send_pending()
            except Exception as exc:  # keep the IO thread alive no matter what
                log.exception("pclink IO loop error")
                time.sleep(0.1)

    def _try_send_pending(self) -> None:
        try:
            req = self._out_q.get_nowait()
        except queue.Empty:
            return
        self._do_send(req)

    def _do_send(self, req: _SendRequest) -> None:
        try:
            # 1. ENQ, wait for ACK
            self._write(bytes([proto.ENQ]), "ENQ (request to send)")
            if not self._wait_for(proto.ACK, T_ACK):
                raise PCLinkTimeout("No ACK after ENQ")

            # 2. STX + command + payload
            frame_bytes = proto.encode_frame(req.command, req.data)
            self._write(
                frame_bytes,
                f"{proto.COMMAND_NAMES.get(req.command, req.command)} frame",
            )

            # 3. wait for ACK/NAK of the frame
            resp = self._wait_for_any((proto.ACK, proto.NAK), T_ACK)
            if resp == proto.NAK:
                raise PCLinkNak("Changer NAK'd the frame (checksum mismatch?)")
            if resp is None:
                raise PCLinkTimeout("No ACK/NAK after frame")
            # EOT instead of ACK means the changer refused the frame (see
            # PCLinkRejected). The rest of the transaction runs as before,
            # so the bytes on the wire don't change -- the v1.12.3 log shows
            # the changer happy with the next transaction after this -- and
            # the error is raised at the end.
            rejected = resp == proto.EOT

            # 4. Reply data, if any, rides along in THIS SAME transaction:
            # per the docs' "Reply Framing" section, a reply is sent as a
            # bare STX + payload -- no fresh ENQ -- confirmed against real
            # hardware (a CD-425M's handshake reply arrives exactly this
            # way). Drain zero or more such frames.
            reply_window = REPLY_WINDOW_BY_COMMAND.get(req.command, REPLY_WINDOW_DEFAULT)
            # Always allow at least one real poll (T_BYTE long), even for a
            # nominal 0.0s window -- otherwise a byte that's already sitting
            # in the OS's receive buffer could be missed entirely rather
            # than just "not waited for".
            deadline = time.monotonic() + max(reply_window, T_BYTE)
            changer_closed, saw_ready_for_data = self._drain_replies(deadline=deadline)
            req.saw_ready_for_data = saw_ready_for_data

            # 5. WE close the transaction: confirmed against real hardware
            # that for a command with no reply data (e.g. DoAction), the
            # changer just sits there re-sending ACK every ~2s until it
            # gets an EOT from us -- it does not close the transaction on
            # its own. So unless the changer already sent its own EOT
            # during the drain above, we send one now.
            if not changer_closed:
                self._write(bytes([proto.EOT]), "EOT (closing transaction)")
                if not self._wait_for(proto.ACK, T_EOT_ACK):
                    # Not fatal: our request frame was already ACK'd, so the
                    # command itself was received either way. Just note it.
                    log.warning(
                        "No ACK after our EOT for %s (command was still received)",
                        proto.COMMAND_NAMES.get(req.command, req.command),
                    )
            if rejected:
                raise PCLinkRejected(
                    f"Changer answered the "
                    f"{proto.COMMAND_NAMES.get(req.command, req.command)} frame with "
                    f"EOT instead of ACK -- it refused it"
                )

        except Exception as exc:
            req.error = exc
        finally:
            req.done.set()

    def _handle_incoming_transaction(self) -> None:
        """We just read an ENQ from the changer -- act as receiver for a
        changer-initiated transaction (a spontaneous event, e.g. StateEvent).
        Here the changer is the initiator, so per the documented flow
        control, IT sends the closing EOT and we just ACK it."""
        self._emit_raw("RX", bytes([proto.ENQ]), "ENQ (changer wants to send)")
        self._write(bytes([proto.ACK]), "ACK (ready to receive)")
        self._drain_replies(deadline=time.monotonic() + T_ACK, require_eot_from_peer=True)

    def _drain_replies(
        self,
        deadline: float,
        require_eot_from_peer: bool = False,
    ) -> tuple[bool, bool]:
        """Read zero or more messages -- either a bare STX (an inline reply
        riding within our own transaction, e.g. Handshake's identifier) or
        a fresh ENQ-initiated one (e.g. a DataAccess reply that arrives as
        its own transaction, confirmed on real hardware) -- ACKing/NAKing
        each, up until `deadline` (a time.monotonic() timestamp) or until
        the peer sends an EOT. Stray non-framing bytes (e.g. a retried ACK
        from a changer that's waiting on our own closing EOT -- see
        _do_send) are logged and ignored rather than aborting the drain.

        Returns (changer_closed, saw_ready_for_data).

        `changer_closed`: True if the peer sent its own EOT (which we
        ACK'd, closing the transaction) -- the caller should not also send
        an EOT of its own in that case. False if the deadline was reached
        with no EOT seen.

        `require_eot_from_peer`: when True (changer-initiated transactions,
        where the changer is the protocol's initiator and so is documented
        to send the closing EOT itself), a missing EOT is logged as a
        warning rather than treated as an expected, ordinary case.

        `saw_ready_for_data`: True if a ReadyForData frame was seen during
        the drain. Used by send_write() to decide whether to open a
        second, separate transaction to send a write's actual payload --
        an earlier version tried sending that payload as an inline
        follow-up frame right here, mid-drain, but real hardware testing
        showed the changer responding to an injected frame with an
        immediate EOT rather than ACKing it, and the value it targeted
        did not actually change. See this module's docstring ("Note on
        the write choreography") for the full reasoning. This method no
        longer sends anything on the write path itself -- it only reports
        what it saw, and send_write() decides what to do about it.
        """
        saw_ready_for_data = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if require_eot_from_peer:
                    self._emit_raw("RX", b"", "(no EOT from changer -- giving up)")
                return False, saw_ready_for_data
            b = self._wait_read_byte(min(remaining, T_ACK))
            if b is None:
                continue  # short poll elapsed; loop again until deadline
            if b == proto.ENQ:
                # The changer is starting its OWN fresh transaction right
                # now -- e.g. a DataAccess reply (DiscInfo/TextData/etc)
                # that, unlike Handshake's reply, doesn't ride inline as a
                # bare STX. Confirmed on real hardware that at least some
                # replies/events arrive this way even while we're still
                # "in session" from our own request. Handle it exactly like
                # the top-level incoming-transaction path does: ACK the
                # ENQ, then read the STX-framed message that follows.
                self._emit_raw("RX", bytes([b]), "ENQ (changer sending its reply/event)")
                self._write(bytes([proto.ACK]), "ACK (ready to receive)")
                stx = self._wait_read_byte(T_ACK)
                if stx == proto.STX:
                    frame = self._read_and_ack_frame()
                    if frame is not None:
                        self._emit_frame(frame)
                        if frame.command == proto.CMD_READY_FOR_DATA:
                            saw_ready_for_data = True
                else:
                    self._emit_raw(
                        "RX", bytes([stx]) if stx is not None else b"",
                        "(expected STX after ENQ, giving up on this one)",
                    )
                continue  # more frames (another ENQ/STX), or EOT, may follow
            if b == proto.STX:
                frame = self._read_and_ack_frame()
                if frame is not None:
                    self._emit_frame(frame)
                    if frame.command == proto.CMD_READY_FOR_DATA:
                        saw_ready_for_data = True
                continue  # more reply frames, or EOT, may follow
            if b == proto.EOT:
                self._emit_raw("RX", bytes([b]), "EOT")
                self._write(bytes([proto.ACK]), "ACK (transaction complete)")
                return True, saw_ready_for_data
            # Stray byte -- most likely the changer re-sending its ACK while
            # it waits for us to close the transaction (see _do_send). Log
            # it and keep listening rather than aborting the drain.
            self._emit_raw("RX", bytes([b]), "(ignored while draining, likely a retry)")
            continue

    def _read_and_ack_frame(self) -> Optional[Frame]:
        """Assumes STX has just been read. Reads command + length + data +
        checksum, ACKs/NAKs it, and returns the decoded Frame (or None on
        timeout/incomplete/bad-checksum)."""
        command = self._wait_read_byte(T_ACK)
        len_lo = self._wait_read_byte(T_ACK)
        len_hi = self._wait_read_byte(T_ACK)
        if command is None or len_lo is None or len_hi is None:
            return None
        n = len_lo | (len_hi << 8)

        data = bytearray()
        for _ in range(n):
            byte = self._wait_read_byte(T_ACK)
            if byte is None:
                self._write(bytes([proto.NAK]), "NAK (incomplete frame)")
                return None
            data.append(byte)

        checksum = self._wait_read_byte(T_ACK)
        raw_frame = bytes([proto.STX, command, len_lo, len_hi]) + bytes(data) + (
            bytes([checksum]) if checksum is not None else b""
        )
        self._emit_raw("RX", raw_frame, f"{proto.COMMAND_NAMES.get(command, command)} frame")

        expected = proto.compute_checksum(command, bytes(data))
        if checksum is None or checksum != expected:
            self._write(bytes([proto.NAK]), "NAK (bad checksum)")
            return None
        self._write(bytes([proto.ACK]), "ACK (frame ok)")

        payload = proto.decode_payload(command, bytes(data))
        return Frame(command=command, data=bytes(data), payload=payload)

    # -- small read helpers -------------------------------------------------------

    def _wait_read_byte(self, timeout: float) -> Optional[int]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return None
            b = self._read_byte()
            if b is not None:
                return b
        return None

    def _wait_for(self, expected: int, timeout: float) -> bool:
        b = self._wait_read_byte(timeout)
        if b is not None:
            self._emit_raw("RX", bytes([b]), "")
        return b == expected

    def _wait_for_any(self, expected: tuple, timeout: float) -> Optional[int]:
        """Read one byte and return it verbatim (None on timeout), regardless
        of whether it's in `expected` -- the caller decides what to do with
        an unexpected value (e.g. distinguishing ACK from NAK)."""
        b = self._wait_read_byte(timeout)
        if b is not None:
            self._emit_raw("RX", bytes([b]), "")
        return b
