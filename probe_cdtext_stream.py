#!/usr/bin/env python3
"""
probe_cdtext_stream.py
======================
A one-off experiment, not part of the app: what does the CD-425M really
send when a CD-Text disc's names are read while that disc is in the drive?

Seen so far (v1.12.4-v1.12.7, slot 4, DiscInfo format 0x90): an endless
run of LongTextData frames, track 0, text 0x01, a `seq` byte counting up.
The app cuts it off after 5 frames, so nobody has seen how it goes on:
does it end by itself, does `seq` wrap after 0xFF, does the track or text
ever change? This probe lets it run, ACKing every frame the way the app
does, for up to --window seconds per read (default 60), then cuts it off
cleanly and reports.

Read-only: it never writes anything. Close the app first -- only one
program can hold the port.

What it reads, in order, for the given slot:
  1. DiscInfo (ordinary read)
  2. disc name  (TextData, info_type 0x00) -- let run
  3. track names (TextData, info_type 0x01) -- let run
  4. genre and userfiles (ordinary reads, also showing the link is clean
     again afterwards)

Every byte sent and received is printed and saved to
probe_cdtext_<slot>_<time>.log, same as the app's "show raw bytes" log.
Note in the log (or tell whoever reads it) whether the disc was in the
drive, and playing or stopped.

Usage:

    python probe_cdtext_stream.py COM3 4
    python probe_cdtext_stream.py COM3 4 --window 120
    python probe_cdtext_stream.py COM3 4 --tracks 1,2,3 --window 15

--tracks N,N,... replaces the two name reads with one track-name read per
track, with the track number in DataAccess's "unknown" byte (the byte
after the slot). The user's own earlier program (KENWOODv2.pde) did this
and got the current track's name back as LongTextData.

Nothing here is confirmed on real hardware yet.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

import serial

import pclink_protocol as proto
from pclink_link import PCLinkConnection, PCLinkError, PCLinkTimeout


class StreamProbe:
    """Runs the reads over `link` (a PCLinkConnection, or a fake in tests).
    Frames arrive through on_frame during send(), as with the real link;
    raw notes through on_raw, which is chained so the caller's raw log
    keeps working."""

    def __init__(self, link, slot: int, out=print, window: float = 60.0,
                 clock=time.monotonic, settle: float = 0.2):
        self.link = link
        self.slot = slot
        self.out = out
        self.window = window
        self.clock = clock
        self.settle = settle  # seconds to let late frames land (0 in tests)
        self._frames: list[tuple[float, proto.Frame]] = []
        self._notes: list[str] = []
        link.on_frame = lambda f: self._frames.append((self.clock(), f))
        previous_raw = getattr(link, "on_raw", None)

        def on_raw(direction, data, note):
            self._notes.append(f"{direction} {note}")
            if previous_raw:
                previous_raw(direction, data, note)
        link.on_raw = on_raw

    def _read(self, data_type: int, info_type: int, what: str, let_run: bool, track: int = 0):
        """One RETRIEVE_DATA. Returns (start time, [(time, frame)], notes,
        error text or None)."""
        self.out(f"\n>> Read {what} (slot {self.slot})"
                 + (f", letting the reply run up to {self.window:.0f}s" if let_run else ""))
        # `track` goes in DataAccess's "unknown" byte (after the slot).
        # The user's own earlier program (KENWOODv2.pde) puts the current
        # track number there to read a CD-Text disc's track name.
        data = proto.encode_data_access(proto.Action.RETRIEVE_DATA, data_type,
                                        slot=self.slot, info_type=info_type, track=track)
        for attempt in range(3):
            self._frames.clear()
            self._notes.clear()
            start = self.clock()
            try:
                if let_run:
                    self.link.send(proto.CMD_DATA_ACCESS, data, reply_window=self.window,
                                   let_stream_run=True)
                else:
                    self.link.send(proto.CMD_DATA_ACCESS, data)
                time.sleep(self.settle)
                return start, list(self._frames), list(self._notes), None
            except PCLinkTimeout:
                self.out("   timed out, retrying")
                time.sleep(self.settle / 2)
            except PCLinkError as exc:
                return start, list(self._frames), list(self._notes), f"{type(exc).__name__}: {exc}"
        return start, list(self._frames), list(self._notes), "timed out 3 times"

    def summarize(self, start: float, records, notes, error) -> dict:
        """What a text read's reply looked like. Printed and returned."""
        texts = [(t, f) for t, f in records
                 if f.command in (proto.CMD_TEXT_DATA, proto.CMD_LONG_TEXT_DATA)]
        others = [f for _, f in records
                  if f.command not in (proto.CMD_TEXT_DATA, proto.CMD_LONG_TEXT_DATA)]
        long_ = [(t, f) for t, f in texts if f.command == proto.CMD_LONG_TEXT_DATA]
        seqs = [f.payload.get("seq") for _, f in long_]
        wraps = sum(1 for a, b in zip(seqs, seqs[1:]) if b is not None and a is not None and b < a)
        gaps = [(a, b) for a, b in zip(seqs, seqs[1:])
                if a is not None and b is not None and b > a and b != a + 1]
        kinds = []
        for _, f in texts:
            p = f.payload
            key = (f.command_name, p.get("index", p.get("track")), p.get("info_type"),
                   "(placeholder)" if proto.is_placeholder_text(p.get("text", "")) else p.get("text"))
            if key not in kinds:
                kinds.append(key)
        cut_off = any("cutting off" in n for n in notes)
        changer_eot = any(n.startswith("RX EOT") for n in notes)
        s = {
            "frames": len(texts),
            "long_text_frames": len(long_),
            "first_frame_after": round(texts[0][0] - start, 2) if texts else None,
            "last_frame_after": round(texts[-1][0] - start, 2) if texts else None,
            "seq_first": seqs[0] if seqs else None,
            "seq_last": seqs[-1] if seqs else None,
            "seq_wraps": wraps,
            "seq_gaps": gaps[:10],
            "distinct": kinds,
            "cut_off_by_us": cut_off,
            "changer_sent_eot": changer_eot,
            "other_frames": [f.command_name for f in others],
            "error": error,
        }
        out = self.out
        if error:
            out(f"   error: {error}")
        if not texts:
            out("   => no text frames at all.")
        else:
            out(f"   => {s['frames']} text frame(s), {s['long_text_frames']} of them LongTextData;"
                f" first after {s['first_frame_after']}s, last after {s['last_frame_after']}s.")
            if seqs:
                out(f"   => seq {s['seq_first']} .. {s['seq_last']}, wrapped {wraps} time(s)"
                    + (f", gaps {gaps[:10]}" if gaps else ", no gaps"))
            out(f"   => {len(kinds)} distinct (frame, track/index, info_type, text):")
            for k in kinds[:30]:
                out(f"        {k}")
            if len(kinds) > 30:
                out(f"        ... and {len(kinds) - 30} more")
        if cut_off:
            out("   => still running when the window ended; we cut it off.")
        elif texts:
            out("   => the reply ended by itself" + (" (changer's EOT)." if changer_eot else "."))
        if others:
            out(f"   => other frames meanwhile: {', '.join(s['other_frames'])}")
        return s

    def run(self, tracks: list[int] | None = None) -> dict:
        result = {}
        self.out(f"=== CD-Text stream probe, slot {self.slot} ===")
        _, recs, _, err = self._read(proto.DataType.DISC_INFO, 0, "disc info", let_run=False)
        info = next((f.payload for _, f in recs if f.command == proto.CMD_DISC_INFO), None)
        result["disc_info"] = info
        if info:
            self.out(f"   tracks={info['track_count']} format=0x{info['format']:02X}")
        elif err:
            self.out(f"   error: {err}")
        if tracks is None:
            reads = [("disc_name", proto.InfoType.DISC_NAMES, "disc name", 0),
                     ("track_names", proto.InfoType.TRACK_NAMES, "track names", 0)]
        else:
            reads = [(f"track_{n}", proto.InfoType.TRACK_NAMES,
                      f"track name, track {n} in DataAccess's 'unknown' byte", n) for n in tracks]
        for key, info_type, what, track in reads:
            result[key] = self.summarize(*self._read(proto.DataType.TEXT_DATA, info_type, what,
                                                     let_run=True, track=track))
        for key, data_type, command, field in (
                ("genre", proto.DataType.DISC_GENRE, proto.CMD_DISC_GENRE, "genre"),
                ("userfiles", proto.DataType.DISC_USERFILES, proto.CMD_DISC_USERFILES, "userfiles")):
            _, recs, _, err = self._read(data_type, 0, key, let_run=False)
            p = next((f.payload for _, f in recs if f.command == command), None)
            result[key] = None if p is None else p[field]
            self.out(f"   {key} = {result[key]!r}" + (f" ({err})" if err else ""))
        self.out("\nDone. Please note whether the disc was in the drive (playing or stopped).")
        return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Watch the CD-425M's LongTextData stream for a CD-Text disc.")
    ap.add_argument("port", help="serial port, e.g. COM3")
    ap.add_argument("slot", type=int, help="slot to probe (1-200)")
    ap.add_argument("--tracks", metavar="N,N,...",
                    help="instead of the disc-name/track-names reads, read these tracks' names "
                         "with the track number in DataAccess's 'unknown' byte, e.g. 1,2,3")
    ap.add_argument("--window", type=float, default=60.0,
                    help="seconds to let each name read's reply run (default 60)")
    args = ap.parse_args(argv)

    log_path = f"probe_cdtext_{args.slot}_{datetime.now():%Y%m%d_%H%M%S}.log"
    log_file = open(log_path, "w", encoding="utf-8")

    def out(line=""):
        stamped = f"[{datetime.now():%H:%M:%S.%f}"[:13] + "] " + line if line else line
        print(stamped)
        log_file.write(stamped + "\n")
        log_file.flush()

    link = PCLinkConnection(args.port)
    link.on_raw = lambda d, data, note: out(f"   [{d}] {data.hex(' ')}  {note}".rstrip())
    try:
        link.open()
        probe = StreamProbe(link, args.slot, out, window=args.window)
        out(f"Handshake on {args.port}...")
        link.handshake(timeout=5.0)
        time.sleep(0.5)
        probe.run([int(n) for n in args.tracks.split(",")] if args.tracks else None)
    except PCLinkError as exc:
        out(f"Stopped: {type(exc).__name__}: {exc}")
        return 1
    except serial.SerialException as exc:
        out(f"Couldn't open {args.port}: {exc}")
        from serial.tools import list_ports  # here, so tests can stub pyserial
        ports = ", ".join(p.device for p in list_ports.comports()) or "none found"
        out(f"Available ports: {ports}")
        return 1
    finally:
        link.close()
        out(f"\nLog saved to {log_path}")
        log_file.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
