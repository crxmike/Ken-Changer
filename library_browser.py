"""
library_browser.py
==================
The Library tab (v1.11.0): browse every disc in the changer, search disc
and track names, filter by genre or userfile. The pure half, with no
Tkinter or serial I/O, so it's directly testable (test_library_browser.py).

The browser's data is a library in library_backup's format: the raw dict
build_library() makes (what gets saved), and parse_library()'s normalized
form (what gets shown: genre codes, userfile masks, int track numbers).
It comes from one of three places:
  - a "Scan Changer" (the same slot walk as the Backup tab's export),
  - the Backup tab's own export, which reads the same data,
  - "Open Backup...", a .json backup file, for browsing offline.

The last changer scan is saved to library_cache.json next to the app, so
the browser isn't empty on the next launch. The changer's data can have
changed since (discs moved, names written from the remote), so the tab
says when that scan was made. The cache is an ordinary backup file, so
it can be restored from like one.
"""

from __future__ import annotations

import os

import library_backup
import pclink_protocol as proto

CACHE_FILENAME = "library_cache.json"

SORT_KEYS = ("slot", "name", "genre", "userfiles", "tracks")


# -- The cache file -------------------------------------------------------

def save_cache(path: str, raw_library: dict) -> None:
    """Write via a temp file, so a crash mid-write can't leave a
    half-written cache behind."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(library_backup.library_to_json(raw_library))
    os.replace(tmp, path)


def load_library_text(text: str) -> dict:
    """A backup file's text -> the browser's (normalized) library. Raises
    library_backup.LibraryError."""
    return library_backup.parse_library(text)


def replace_disc(raw_library: dict, record: dict | None, slot: int) -> dict:
    """A copy of a raw library with one slot's disc replaced by a fresh
    library_backup.disc_record(), or removed (record None: the slot is
    empty now). Used by "Rescan Disc"."""
    discs = [d for d in raw_library["discs"] if d["slot"] != slot]
    if record is not None:
        discs.append(record)
    return dict(raw_library, discs=sorted(discs, key=lambda d: d["slot"]))


# -- Searching and filtering ----------------------------------------------

def _words(query: str) -> list[str]:
    return query.casefold().split()


def matching_tracks(disc: dict, query: str) -> set[int]:
    """Track numbers whose title contains every word of the query."""
    words = _words(query)
    if not words:
        return set()
    return {n for n, title in (disc.get("tracks") or {}).items()
            if all(w in title.casefold() for w in words)}


def disc_matches(disc: dict, query: str) -> bool:
    """True when every word of the query appears somewhere in the disc:
    its name, its genre, or any of its track names. So "hip gift" finds
    The Tragically Hip's disc through its "Gift Shop" track. A bare
    number also matches the slot."""
    words = _words(query)
    if not words:
        return True
    genre = proto.GENRES.get(disc.get("genre"), "") if disc.get("genre") is not None else ""
    haystack = " ".join([disc.get("name") or "", genre,
                         *(disc.get("tracks") or {}).values()]).casefold()
    return all(w in haystack or w == str(disc["slot"]) for w in words)


def filter_discs(discs: list[dict], query: str = "", genre: int | None = None,
                 userfile: int | None = None) -> list[dict]:
    """Discs matching the search box, the genre filter (a code) and the
    userfile filter (a userfile NUMBER, 1-8). None means "any". A disc
    whose genre/userfiles weren't read (None) doesn't match a filter on
    them."""
    out = []
    for disc in discs:
        if genre is not None and disc.get("genre") != genre:
            continue
        if userfile is not None and not (disc.get("userfiles") or 0) & (1 << (userfile - 1)):
            continue
        if disc_matches(disc, query):
            out.append(disc)
    return out


def sort_discs(discs: list[dict], key: str = "slot", reverse: bool = False) -> list[dict]:
    """Sort by one of SORT_KEYS. Discs with no value for the key (no name,
    unknown track count...) always go last, whichever the direction; ties
    fall back to slot order."""
    if key not in SORT_KEYS:
        raise ValueError(f"unknown sort key {key!r}")

    def value(disc):
        if key == "slot":
            return disc["slot"]
        if key == "name":
            return (disc.get("name") or "").casefold() or None
        if key == "genre":
            g = disc.get("genre")
            return None if g is None else proto.GENRES.get(g, f"0x{g:02X}").casefold()
        if key == "userfiles":
            u = disc.get("userfiles")
            # Order by the lowest userfile number a disc is in, then the rest.
            return None if not u else library_backup.userfile_numbers(u)
        return disc.get("track_count")

    have = [d for d in discs if value(d) is not None]
    missing = [d for d in discs if value(d) is None]
    have.sort(key=lambda d: d["slot"])
    have.sort(key=value, reverse=reverse)  # stable: ties stay in slot order
    return have + sorted(missing, key=lambda d: d["slot"])


# -- What the tables show -------------------------------------------------

def genre_label(code: int | None) -> str:
    return "" if code is None else proto.GENRES.get(code, f"0x{code:02X}")


def userfile_label(number: int, userfile_names: dict[int, str]) -> str:
    """"#3" or "#3 Jazz". `userfile_names` is keyed by userfile NUMBER,
    as parse_library returns it."""
    name = userfile_names.get(number)
    return f"#{number} {name}" if name else f"#{number}"


def disc_row(disc: dict) -> tuple[str, str, str, str, str]:
    """(slot, name, genre, userfiles, tracks) for the disc table. "?" is a
    value the scan couldn't read; an unknown track count (a disc not
    played since power-on, CONFIRMED v1.10.1) shows the number of named
    tracks with a "?", since that's all that's known."""
    name = disc.get("name")
    userfiles = disc.get("userfiles")
    count = disc.get("track_count")
    if count is None:
        named = len(disc.get("tracks") or {})
        count_text = f"{named}?" if named else "?"
    else:
        count_text = str(count)
    return (
        str(disc["slot"]),
        "?" if name is None else (name or "(no name)"),
        "?" if disc.get("genre") is None else genre_label(disc["genre"]),
        "?" if userfiles is None else ", ".join(
            f"#{n}" for n in library_backup.userfile_numbers(userfiles)),
        count_text,
    )


def track_rows(disc: dict) -> list[tuple[int, str]]:
    """(track, title) for the track pane. With a known track count, every
    track is listed, "" where it has no name; otherwise only the named
    ones."""
    tracks = disc.get("tracks") or {}
    count = disc.get("track_count")
    numbers = range(1, count + 1) if count else sorted(tracks)
    return [(n, tracks.get(n, "")) for n in numbers]


def genre_choices(discs: list[dict]) -> list[tuple[str, int]]:
    """(label, code) for each genre some disc has, by label."""
    codes = {d["genre"] for d in discs if d.get("genre") is not None}
    return sorted(((genre_label(c), c) for c in codes), key=lambda p: p[0].casefold())


def userfile_choices(userfile_names: dict[int, str]) -> list[tuple[str, int]]:
    """(label, number) for all eight userfiles."""
    return [(userfile_label(n, userfile_names), n)
            for n in range(1, library_backup.USERFILE_COUNT + 1)]


def summary(shown: list[dict], total: list[dict]) -> str:
    named = sum(len(d.get("tracks") or {}) for d in shown)
    head = f"{len(shown)} disc(s)" if len(shown) == len(total) else \
        f"{len(shown)} of {len(total)} disc(s)"
    return f"{head}, {named} named track(s)"


def format_when(exported_at: str | None) -> str:
    """"2026-09-24T12:00:00" -> "2026-09-24 12:00"."""
    if not exported_at:
        return "an unknown date"
    return exported_at.replace("T", " ")[:16]
