#!/usr/bin/env python3
"""
probe_artist_name.py
====================
A one-off experiment, not part of the app: does the CD-425M support the
"artist name" text type (InfoType.ARTIST_NAME, info_type 0x02)?

cd_types.html lists info_type 0x02 = "artist name" alongside disc names
(0x00), track names (0x01) and userfile names (0x07), but nothing in this
project has ever sent it, and the source docs are a community
reverse-engineering effort covering several Kenwood changers. If the
CD-425M stores an artist per disc, gnudb results could go in as artist +
album instead of "Artist / Album" squeezed into the 25-character disc name.

What it does (close the app first -- only one program can hold the port):

  1. Reads the slot's disc name, genre and userfiles (the baseline).
  2. Asks for the slot's artist name: DataAccess(RETRIEVE_DATA, TEXT_DATA,
     info_type=0x02) and reports every frame that comes back.
  3. Only with --write TEXT: sends a WRITE_NAME with info_type 0x02, then
     reads everything again and compares against the baseline.

The write carries the disc's current genre and userfiles in TextData's
genre/userfiles bytes, because the changer takes both from EVERY TextData
write (CONFIRMED v1.5.1 and v1.8.2) -- sending 0 would reset them. It
can't protect the disc name, though: if the changer ignores info_type 0x02
and treats this as a disc-name write, the disc name gets replaced. The
script prints the original name so it can be put back from the Disc Data
tab.

Every byte sent and received is printed and saved to
probe_artist_<slot>_<time>.log, same as the app's "show raw bytes" log.

Usage:

    python probe_artist_name.py COM3 1
    python probe_artist_name.py COM3 1 --write "The Beatles"

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


ARTIST = proto.InfoType.ARTIST_NAME


class Probe:
    """Runs the reads/write over `link` (a PCLinkConnection, or a fake in
    tests). Frames arrive through on_frame during send(), as with the real
    link, and are collected per request."""

    def __init__(self, link, slot: int, out=print, settle: float = 0.2):
        self.link = link
        self.settle = settle  # seconds to let late frames land (0 in tests)
        self.slot = slot
        self.out = out
        self._frames: list[proto.Frame] = []
        link.on_frame = self._frames.append

    # -- low level ------------------------------------------------------

    def _read(self, data_type: int, info_type: int = 0, what: str = "") -> list | None:
        """One RETRIEVE_DATA request; the frames it produced, or None if it
        never went through."""
        self.out(f"\n>> Read {what} (slot {self.slot})")
        data = proto.encode_data_access(proto.Action.RETRIEVE_DATA, data_type,
                                        slot=self.slot, info_type=info_type)
        for _ in range(3):
            self._frames.clear()
            try:
                self.link.send(proto.CMD_DATA_ACCESS, data)
                time.sleep(self.settle)
                frames = list(self._frames)
                for f in frames:
                    self.out(f"   reply: {f!r}")
                if not frames:
                    self.out("   (no reply frame)")
                return frames
            except PCLinkTimeout:
                self.out("   timed out, retrying")
                time.sleep(self.settle / 2)
            except PCLinkError as exc:
                self.out(f"   failed: {exc}")
                return None
        self.out("   timed out 3 times")
        return None

    @staticmethod
    def _first(frames, command, info_type=None):
        for f in frames or []:
            if f.command == command and (info_type is None or f.payload.get("info_type") == info_type):
                return f.payload
        return None

    def read_state(self) -> dict:
        """Disc name, genre, userfiles (None where a read gave nothing)."""
        state = {"name": None, "genre": None, "userfiles": None}
        p = self._first(self._read(proto.DataType.TEXT_DATA, proto.InfoType.DISC_NAMES, "disc name"),
                        proto.CMD_TEXT_DATA, proto.InfoType.DISC_NAMES)
        if p:
            state["name"] = "" if proto.is_placeholder_text(p["text"]) else p["text"]
        p = self._first(self._read(proto.DataType.DISC_GENRE, what="genre"), proto.CMD_DISC_GENRE)
        if p:
            state["genre"] = p["genre"]
        p = self._first(self._read(proto.DataType.DISC_USERFILES, what="userfiles"), proto.CMD_DISC_USERFILES)
        if p:
            state["userfiles"] = p["userfiles"]
        return state

    def read_artist(self) -> list | None:
        frames = self._read(proto.DataType.TEXT_DATA, ARTIST, "artist name (info_type 0x02)")
        self.describe_artist_reply(frames)
        return frames

    def describe_artist_reply(self, frames):
        if frames is None:
            self.out("   => the request didn't go through (see above).")
            return
        if not frames:
            self.out("   => the changer ACK'd the request but sent nothing back.")
            return
        for f in frames:
            if f.command in (proto.CMD_TEXT_DATA, proto.CMD_LONG_TEXT_DATA):
                p = f.payload
                text = p.get("text", "")
                shown = "(no text / placeholder)" if not text or proto.is_placeholder_text(text) else repr(text)
                it = p.get("info_type")
                if it == ARTIST:
                    self.out(f"   => {f.command_name} with info_type 0x02, index "
                             f"{p.get('index', p.get('track'))}: {shown}")
                else:
                    self.out(f"   => {f.command_name} but info_type 0x{it:02X}, not 0x02: {shown}")
            else:
                self.out(f"   => a {f.command_name} frame, not TextData")

    # -- the experiment --------------------------------------------------

    def run(self, write_text: str | None = None, index: int = 0) -> dict:
        result = {"baseline": None, "artist_before": None, "wrote": False,
                  "artist_after": None, "after": None, "changed": []}
        self.out(f"=== Artist-name probe, slot {self.slot} ===")
        base = self.read_state()
        result["baseline"] = base
        self.out(f"\nBaseline: name={base['name']!r} genre={base['genre']} userfiles={base['userfiles']}")
        result["artist_before"] = self.read_artist()

        if write_text is None:
            self.out("\nRead-only run. Add --write \"TEXT\" to try a write.")
            return result

        if base["name"] is None or base["genre"] is None or base["userfiles"] is None:
            self.out("\nNot writing: the disc name, genre or userfiles couldn't be read, and the write "
                     "has to carry the genre and userfiles (or they'd be reset) and the name has to "
                     "be known (so it can be put back if the write replaces it).")
            return result

        self.out(f"\n>> WRITE_NAME, info_type 0x02, slot {self.slot}, index {index}: {write_text!r} "
                 f"(genre {base['genre']}, userfiles 0x{base['userfiles']:02X} carried over)")
        request = proto.encode_data_access(proto.Action.WRITE_NAME, proto.DataType.TEXT_DATA,
                                           slot=self.slot, info_type=ARTIST, genre=base["genre"])
        payload = proto.encode_text_data(slot=self.slot, index=index, text=write_text,
                                         userfiles=base["userfiles"], info_type=ARTIST,
                                         genre=base["genre"])
        self._frames.clear()
        try:
            self.link.send_write(proto.CMD_DATA_ACCESS, request, proto.CMD_TEXT_DATA, payload)
            result["wrote"] = True
            self.out("   sent; the changer ACK'd both transactions.")
        except PCLinkError as exc:
            self.out(f"   write failed: {type(exc).__name__}: {exc}")
        for f in self._frames:
            self.out(f"   frame during write: {f!r}")

        time.sleep(self.settle)
        self.out("\n--- Reading back ---")
        result["artist_after"] = self.read_artist()
        after = self.read_state()
        result["after"] = after
        for key in ("name", "genre", "userfiles"):
            if after[key] != base[key]:
                result["changed"].append(key)
        self.out(f"\nAfter: name={after['name']!r} genre={after['genre']} userfiles={after['userfiles']}")
        if result["changed"]:
            self.out("\n!!! CHANGED by the artist write: " + ", ".join(result["changed"]))
            if "name" in result["changed"]:
                self.out(f"!!! The disc name was {base['name']!r}. Put it back from the Disc Data tab.")
        else:
            self.out("\nDisc name, genre and userfiles unchanged.")
        return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Probe the CD-425M for artist-name (info_type 0x02) support.")
    ap.add_argument("port", help="serial port, e.g. COM3")
    ap.add_argument("slot", type=int, help="slot to probe (1-200); pick one holding a disc")
    ap.add_argument("--write", metavar="TEXT", help="also try writing this artist name (ASCII)")
    ap.add_argument("--index", type=int, default=0, help="TextData index for the write (default 0)")
    args = ap.parse_args(argv)

    log_path = f"probe_artist_{args.slot}_{datetime.now():%Y%m%d_%H%M%S}.log"
    log_file = open(log_path, "w", encoding="utf-8")

    def out(line=""):
        print(line)
        log_file.write(line + "\n")
        log_file.flush()

    link = PCLinkConnection(args.port)
    link.on_raw = lambda d, data, note: out(f"   [{d}] {data.hex(' ')}  {note}".rstrip())
    try:
        link.open()
        probe = Probe(link, args.slot, out)
        out(f"Handshake on {args.port}...")
        link.handshake(timeout=5.0)
        time.sleep(0.5)
        probe.run(args.write, args.index)
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
