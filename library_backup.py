"""
library_backup.py
=================
Library backup and restore (v1.10.0): the pure half, with no Tkinter or
serial I/O, so it's directly testable (test_library_backup.py). The app
(pclink_app.py) walks the changer's slots and feeds what it reads in here.

Why: every disc name, track name, genre and userfile the user has set
lives only in the changer's memory, and that memory can be lost or go
stale after a power outage (confirmed by the user, see README's Disc Map
caveat). A backup file is the way back.

A backup is a JSON file:

    {
      "format": "ken-changer-library", "format_version": 1,
      "app_version": "1.10.0", "exported_at": "2026-09-24T12:00:00",
      "userfile_names": {"1": "Road Trip", "3": "Jazz"},
      "program": [{"slot": 1, "track": 3}, {"slot": 2, "track": "all"}],
      "discs": [
        {"slot": 1, "track_count": 12, "name": "Artist / Album",
         "genre": "Rock", "userfiles": [1, 3],
         "tracks": {"1": "First Song", "2": "Second Song"}}
      ]
    }

A disc's `name`, `genre`, `userfiles` or `tracks` is null when the export
couldn't read it; "" / {} / [] mean the changer has none stored. Restore
leaves null fields alone. `program` is null when it wasn't read.

Restore only adds or overwrites, never erases: a name missing from the
backup leaves whatever the changer has. It also refuses to write a slot
whose track count no longer matches the backup's, since that's most
likely a different disc in that slot now. That check only works when
both counts are known: `track_count` is null for a disc the changer
hadn't loaded since power-on (it reports 99 then, see UNKNOWN_TRACK_COUNT).
"""

from __future__ import annotations

import csv
import io
import json

import pclink_protocol as proto

FORMAT = "ken-changer-library"
FORMAT_VERSION = 1
DISC_NAME_MAX = 25  # owner's manual p. 28; CONFIRMED the changer keeps the first 25 (v1.8.5)
USERFILE_COUNT = 8
# CONFIRMED (v1.10.1): after power-on, DiscInfo reports 99 tracks for every
# disc the changer hasn't loaded yet; the real count only appears once
# the disc has been played. So 99 means "not known yet", stored as null.
UNKNOWN_TRACK_COUNT = 99


class LibraryError(ValueError):
    """A backup file that can't be read or restored."""


# -- Export ---------------------------------------------------------------

def userfile_numbers(mask: int) -> list[int]:
    """Bitmask -> userfile numbers (bit n-1 = #n, CONFIRMED v1.6.2)."""
    return [n for n in range(1, USERFILE_COUNT + 1) if mask & (1 << (n - 1))]


def userfile_mask(numbers: list[int]) -> int:
    return sum(1 << (n - 1) for n in numbers)


def known_track_count(track_count: int | None) -> int | None:
    """DiscInfo's track count, or None when the changer doesn't know it."""
    return None if track_count == UNKNOWN_TRACK_COUNT else track_count


def disc_record(slot: int, track_count: int | None, name: str | None,
                tracks: dict[int, str] | None, genre: int | None,
                userfiles: int | None, cdtext: bool = False) -> dict:
    """One disc's backup entry from what the app read (None = not read).
    A track-names read starts with an index-0 frame that repeats the disc
    name (CONFIRMED, v1.10.0 export log); it's not a track, so it's
    dropped. `cdtext` (v1.12.11, part of a slot read) isn't saved: the
    changer reports it again on every read."""
    return {
        "slot": slot,
        "track_count": known_track_count(track_count),
        "name": name,
        "genre": None if genre is None else proto.GENRES.get(genre, f"0x{genre:02X}"),
        "userfiles": None if userfiles is None else userfile_numbers(userfiles),
        "tracks": None if tracks is None else {str(n): t for n, t in sorted(tracks.items())
                                               if t and n >= 1},
    }


def build_library(discs: list[dict], userfile_names_by_bit: dict[int, str] | None,
                  program_items: list | None, app_version: str, exported_at: str) -> dict:
    """The whole backup. `userfile_names_by_bit` is keyed by userfile BIT,
    as the changer (and the app's cache) keys them (CONFIRMED v1.7.1);
    the file keys them by userfile number, which is what people read.
    `program_items` is decoded DiscListing items, or None if not read."""
    names = None
    if userfile_names_by_bit is not None:
        names = {str(n): userfile_names_by_bit[1 << (n - 1)]
                 for n in range(1, USERFILE_COUNT + 1)
                 if userfile_names_by_bit.get(1 << (n - 1))}
    program = None
    if program_items is not None:
        program = [{"slot": i["slot"],
                    "track": "all" if i["track"] == proto.LISTING_ALL_TRACKS else i["track"]}
                   for i in program_items]
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "app_version": app_version,
        "exported_at": exported_at,
        "userfile_names": names,
        "program": program,
        "discs": sorted(discs, key=lambda d: d["slot"]),
    }


def library_to_json(library: dict) -> str:
    return json.dumps(library, indent=2, ensure_ascii=False) + "\n"


def library_to_csv(library: dict) -> str:
    """A flat catalog, one row per track (a disc with no track names gets
    one row). For reading and printing, not for restoring."""
    names = library.get("userfile_names") or {}
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["Slot", "Disc Name", "Genre", "Userfiles", "Tracks", "Track", "Track Name"])
    for disc in library["discs"]:
        userfiles = "; ".join(
            f"#{n} {names[str(n)]}" if names.get(str(n)) else f"#{n}"
            for n in disc.get("userfiles") or []
        )
        head = [disc["slot"], disc.get("name") or "", disc.get("genre") or "", userfiles,
                disc.get("track_count") if disc.get("track_count") is not None else ""]
        tracks = sorted((int(n), t) for n, t in (disc.get("tracks") or {}).items())
        if not tracks:
            writer.writerow(head + ["", ""])
        for n, title in tracks:
            writer.writerow(head + [n, title])
    return out.getvalue()


# -- Reading a backup back in ---------------------------------------------

def parse_library(text: str, fold=lambda s: s) -> dict:
    """Parse and check a backup file, normalizing it for restore:
    genres become codes, userfiles a mask, track keys ints, program steps
    (slot, track) tuples. Every text goes through `fold` (the app passes
    ascii_fold, for a hand-edited file) and disc names are cut to the 25
    characters the changer keeps. Raises LibraryError."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LibraryError(f"Not a valid JSON file: {exc}") from None
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        raise LibraryError("This isn't a Ken Changer library backup.")
    if raw.get("format_version") != FORMAT_VERSION:
        raise LibraryError(
            f"Backup format version {raw.get('format_version')!r} isn't supported "
            f"(this app reads version {FORMAT_VERSION})."
        )

    discs = []
    seen = set()
    for n, d in enumerate(raw.get("discs") or [], start=1):
        where = f"Disc entry {n}"
        if not isinstance(d, dict):
            raise LibraryError(f"{where} isn't an object.")
        slot = d.get("slot")
        if not isinstance(slot, int) or not 1 <= slot <= 200:
            raise LibraryError(f"{where}: slot {slot!r} isn't 1-200.")
        if slot in seen:
            raise LibraryError(f"Slot {slot} appears twice.")
        seen.add(slot)
        where = f"Slot {slot}"

        track_count = d.get("track_count")
        if track_count is not None and (not isinstance(track_count, int) or track_count < 0):
            raise LibraryError(f"{where}: track_count {track_count!r} isn't a number.")
        track_count = known_track_count(track_count)  # v1.10.0 files can hold 99

        name = d.get("name")
        if name is not None:
            if not isinstance(name, str):
                raise LibraryError(f"{where}: name isn't text.")
            name = fold(name.strip())[:DISC_NAME_MAX]

        genre = d.get("genre")
        if genre is not None:
            if genre not in proto.GENRE_NAME_TO_CODE:
                raise LibraryError(f"{where}: unknown genre {genre!r}.")
            genre = proto.GENRE_NAME_TO_CODE[genre]

        userfiles = d.get("userfiles")
        if userfiles is not None:
            if (not isinstance(userfiles, list)
                    or any(not isinstance(u, int) or not 1 <= u <= USERFILE_COUNT for u in userfiles)):
                raise LibraryError(f"{where}: userfiles must be a list of numbers 1-8.")
            userfiles = userfile_mask(userfiles)

        tracks = d.get("tracks")
        if tracks is not None:
            if not isinstance(tracks, dict):
                raise LibraryError(f"{where}: tracks isn't an object.")
            parsed = {}
            for key, title in tracks.items():
                try:
                    number = int(key)
                except ValueError:
                    raise LibraryError(f"{where}: track {key!r} isn't a number.") from None
                if number == 0:
                    continue  # v1.10.0 files hold the disc name here too
                if not 1 <= number <= 99 or not isinstance(title, str):
                    raise LibraryError(f"{where}: bad track entry {key!r}.")
                if title.strip():
                    parsed[number] = fold(title.strip())
            tracks = parsed

        discs.append({"slot": slot, "track_count": track_count, "name": name,
                      "genre": genre, "userfiles": userfiles, "tracks": tracks})

    names = {}
    for key, value in (raw.get("userfile_names") or {}).items():
        if key not in {str(n) for n in range(1, USERFILE_COUNT + 1)} or not isinstance(value, str):
            raise LibraryError(f"Bad userfile name entry {key!r}.")
        if value.strip():
            names[int(key)] = fold(value.strip())[:DISC_NAME_MAX]

    program = None
    if raw.get("program") is not None:
        program = []
        for n, step in enumerate(raw["program"], start=1):
            try:
                slot, track = step["slot"], step["track"]
            except (TypeError, KeyError):
                raise LibraryError(f"Program step {n} needs a slot and a track.") from None
            track = proto.LISTING_ALL_TRACKS if track == "all" else track
            if not isinstance(slot, int) or not isinstance(track, int):
                raise LibraryError(f"Program step {n} isn't valid.")
            program.append((slot, track))

    return {
        "exported_at": raw.get("exported_at"),
        "discs": sorted(discs, key=lambda d: d["slot"]),
        "userfile_names": names,
        "program": program,
    }


# -- Restore planning -----------------------------------------------------

def plan_disc_restore(saved: dict, current: dict) -> tuple[list, int | None, str | None]:
    """What to write to put one parsed backup disc back, given what the
    changer holds now. `current` has the same keys as a parsed disc
    (track_count, name, tracks, genre as a code, userfiles as a mask),
    None for anything that couldn't be read.

    Returns (items, userfiles_mask, skip_reason). `items` is in
    _write_to_changer_worker's (index, text, info_type, label, genre)
    form. Nothing to write and no skip reason means the slot already
    matches, which is also how the app checks a slot after writing it.

    Every TextData write sets the disc's genre (CONFIRMED v1.5.1) and its
    userfiles (CONFIRMED v1.8.1/v1.8.2), so every item carries the target
    genre and the mask; with neither saved nor currently known the slot
    is skipped rather than risk resetting them. A genre/userfiles change
    with no name to write rides on the disc name, as on the Userfiles
    tab (v1.8.1)."""
    slot = saved["slot"]
    now_count = current.get("track_count")
    if now_count is None:
        return [], None, "couldn't read the slot's disc info"
    if now_count == 0:
        return [], None, "the slot is empty now"
    # Only compare when both counts are real: 99 means the changer hasn't
    # loaded the disc since power-on (v1.10.1), so it proves nothing.
    now_count = known_track_count(now_count)
    saved_count = known_track_count(saved.get("track_count"))
    if now_count is not None and saved_count is not None and saved_count != now_count:
        return [], None, (
            f"it has {now_count} tracks now but {saved_count} in the backup "
            "-- probably a different disc"
        )

    genre = saved["genre"] if saved.get("genre") is not None else current.get("genre")
    mask = saved["userfiles"] if saved.get("userfiles") is not None else current.get("userfiles")
    if genre is None or mask is None:
        return [], None, "its current genre/userfiles couldn't be read, and a write would reset them"

    cdtext = bool(current.get("cdtext"))
    items = []
    now_tracks = current.get("tracks") or {}
    if not cdtext:
        # A CD-Text disc's titles come from the disc itself (v1.12.11), so
        # its names are left alone.
        if saved.get("name") and saved["name"] != current.get("name"):
            items.append((0, saved["name"], proto.InfoType.DISC_NAMES, "Disc Name", genre))
        for track, title in sorted((saved.get("tracks") or {}).items()):
            if (now_count is None or track <= now_count) and title != now_tracks.get(track):
                items.append((track, title, proto.InfoType.TRACK_NAMES, f"Track {track}", genre))

    if not items and (genre != current.get("genre") or mask != current.get("userfiles")):
        item, error = state_carrier(cdtext, saved.get("name") or current.get("name"),
                                    now_tracks.get(1), genre)
        if error:
            return [], None, "its genre/userfiles differ, but " + error
        items.append(item)

    return items, mask, None


# The stored copy of a CD-Text title is cut to 25 characters (v1.12.11
# log, "We Wish You A Merry Chris"), like a disc title (manual p. 28).
CARRIER_TEXT_MAX = 25


def state_carrier(cdtext: bool, disc_name: str | None, track1: str | None,
                  genre: int) -> tuple[tuple | None, str | None]:
    """The write that carries a disc's genre and userfiles when no name is
    being changed: every TextData write sets both for the whole disc
    (genre CONFIRMED v1.5.1 on a track write, userfiles CONFIRMED v1.8.1 on
    a disc-name write), so one name is re-sent unchanged with them.

    Normally that's the disc name. A CD-Text disc (v1.12.11) re-sends
    track 1's name instead: its disc name can't be read while it's in the
    drive (the endless stream, v1.12.4) and its front panel ignores a
    written one, but track 1 reads fine either way, and the changer
    stores it as the CD-Text title cut to 25, which is what's re-sent.
    CONFIRMED on real hardware (v1.12.11, slot 4 in the drive): a track 1
    write set userfiles 0x00 -> 0x07, and another set the genre.

    Returns ((index, text, info_type, label, genre), None), or
    (None, reason) when there's no name to re-send."""
    if cdtext:
        if not track1:
            return None, ("on a CD-Text disc they're written by re-sending track 1's "
                          "name, and it couldn't be read")
        return (1, track1[:CARRIER_TEXT_MAX], proto.InfoType.TRACK_NAMES,
                "Track 1 (genre/userfiles)", genre), None
    if not disc_name:
        return None, "they're written by re-sending the disc name and the disc has no name"
    return (0, disc_name, proto.InfoType.DISC_NAMES, "Disc Name (genre/userfiles)", genre), None


def plan_userfile_names_restore(saved: dict[int, str], current_by_bit: dict[int, str]) -> list[tuple[int, str]]:
    """(number, name) for each saved userfile name the changer doesn't
    already have. `current_by_bit` is keyed by userfile BIT (v1.7.1)."""
    return [(n, name) for n, name in sorted(saved.items())
            if current_by_bit.get(1 << (n - 1)) != name]
