"""
batch_tagging.py
================
Batch gnudb tagging (v1.13.0): the pure half, with no Tkinter or serial
I/O, so it's directly testable (test_batch_tagging.py). The app
(pclink_app.py) does the loading, the lookups and the writes.

Why it has to load every disc: the changer only gives a TOC (and so a
DiscID) for the disc in the drive (CONFIRMED, README "Honest gaps" #4).
So the batch goes through every unnamed disc from the Library's scan,
loads it with ChangeDisc (which starts it playing, as the Library's Play
does), waits for its TOC, and asks gnudb.org for candidates. That's the
same disc-by-disc walk the deferred "ALL DATA READ" fallback would need.

Then the user reviews each disc: picks a candidate, sees exactly what
would be written, and approves it (or skips). Nothing is written without
that. The write is the Backup restore's (library_backup.plan_disc_restore):
only what differs, every write carrying the disc's genre and userfiles,
and nothing at all if the slot's track count no longer matches the TOC or
the disc has been named since the scan.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import pclink_protocol as proto
import library_backup

DISC_NAME_MAX = library_backup.DISC_NAME_MAX  # 25, CONFIRMED (v1.8.5)
# Stored names read back as TextData, 25 characters at most (v1.12.9/
# v1.12.11 logs). Track titles are written whole, as the Disc Data tab
# does, and checked on the first 25 after the write.
STORED_TEXT_MAX = 25
# A track-names read returns the disc name and tracks 1-20, nothing more,
# even for a 26-track disc whose titles 21-26 had just been written and
# ACK'd (v1.13.0 run, slot 32; the manual's limit is 20 titles per disc).
# Titles past 20 are still written, but the check can't see them.
READABLE_TRACK_TITLES = 20
# Seconds between gnudb.org requests in a batch, so a long run doesn't get
# the IP throttled (gnudb's policy asks clients not to hammer it; the
# limit itself isn't published).
GNUDB_MIN_INTERVAL = 2.0


# -- Which discs ----------------------------------------------------------

def unnamed_slots(raw_library: dict | None) -> tuple[list[int], list[int]]:
    """Slots the batch should look up, from the Library's scan (backup
    form): discs whose name is "" (the changer has none stored). Also
    returns the slots whose name couldn't be read (null), which are left
    out, since they may well have a name."""
    if not raw_library:
        return [], []
    unnamed, unread = [], []
    for disc in raw_library.get("discs") or []:
        if disc.get("name") == "":
            unnamed.append(disc["slot"])
        elif disc.get("name") is None:
            unread.append(disc["slot"])
    return sorted(unnamed), sorted(unread)


# -- TOC -> DiscID --------------------------------------------------------

def toc_discid(entry: dict | None) -> dict | None:
    """proto.calculate_cddb_discid() for a complete TOC from the app's
    _toc_cache (tracks contiguous from the first, plus the lead-out), or
    None while it isn't complete -- the same rule the TOC panel and
    "Query gnudb.org" use."""
    if not entry or not entry.get("tracks") or entry.get("leadout") is None:
        return None
    numbers = sorted(entry["tracks"])
    if numbers != list(range(numbers[0], numbers[-1] + 1)):
        return None
    return proto.calculate_cddb_discid([entry["tracks"][n] for n in numbers] + [entry["leadout"]])


# -- What a gnudb entry becomes on the changer ----------------------------

def disc_title(gnudb_disc, fold=lambda s: s) -> str:
    """"Artist / Album" (as the Disc Data tab's gnudb column shows it),
    folded to ASCII and cut to the 25 characters the changer keeps."""
    title = f"{gnudb_disc.artist} / {gnudb_disc.album}" if gnudb_disc.artist else gnudb_disc.album
    return fold((title or "").strip())[:DISC_NAME_MAX]


def plan_target(slot: int, gnudb_disc, track_count: int, fold=lambda s: s,
                match_genre=lambda s: "") -> tuple[dict, list[str]]:
    """The disc as it should end up, in library_backup.parse_library's
    disc form (so plan_disc_restore can plan the write), plus notes for
    the review: anything cut, dropped or left alone.

    `track_count` is the TOC's (the disc in the slot when it was loaded).
    `match_genre` maps gnudb's free-text genre to a changer genre name, or
    "" (the app passes match_changer_genre). With no match the disc's
    current genre is kept. Userfiles are always kept."""
    notes = []
    full = f"{gnudb_disc.artist} / {gnudb_disc.album}" if gnudb_disc.artist else gnudb_disc.album
    name = disc_title(gnudb_disc, fold)
    if len(fold((full or "").strip())) > DISC_NAME_MAX:
        notes.append(f"The disc name is cut to the {DISC_NAME_MAX} characters the changer keeps.")

    tracks = {}
    for n, title in sorted(gnudb_disc.track_titles.items()):
        title = fold((title or "").strip())
        if title and 1 <= n <= track_count:
            tracks[n] = title
    extra = sorted(n for n in gnudb_disc.track_titles if n > track_count)
    if extra:
        notes.append(f"gnudb has {max(gnudb_disc.track_titles)} titles but the disc has "
                     f"{track_count} tracks: titles {extra[0]}-{extra[-1]} aren't written. "
                     "Check it's the right album.")
    missing = [n for n in range(1, track_count + 1) if n not in tracks]
    if missing:
        notes.append(f"No gnudb title for {len(missing)} track(s): "
                     f"{', '.join(map(str, missing))}.")
    unchecked = sorted(n for n in tracks if n > READABLE_TRACK_TITLES)
    if unchecked:
        notes.append(f"Titles {unchecked[0]}-{unchecked[-1]} are written but can't be checked: "
                     f"the changer reads back {READABLE_TRACK_TITLES} track titles at most "
                     f"(its manual's limit), so they may not be kept.")
    if any(len(t) > STORED_TEXT_MAX for t in tracks.values()):
        notes.append(f"Some track titles are over {STORED_TEXT_MAX} characters; the changer "
                     f"is expected to keep the first {STORED_TEXT_MAX}.")

    genre_name = match_genre(gnudb_disc.genre or "")
    genre = proto.GENRE_NAME_TO_CODE.get(genre_name) if genre_name else None
    if genre is None:
        notes.append(f"gnudb's genre {gnudb_disc.genre!r} isn't one of the changer's, so the "
                     "disc's current genre is kept." if gnudb_disc.genre else
                     "gnudb has no genre, so the disc's current genre is kept.")

    target = {"slot": slot, "track_count": track_count, "name": name, "tracks": tracks,
              "genre": genre, "userfiles": None}
    return target, notes


def plan_write(target: dict, current: dict,
               ours: str | None = None) -> tuple[list, int | None, str | None]:
    """The write for an approved disc, given a fresh read of the slot
    (the app's _read_slot_state_sync). library_backup.plan_disc_restore
    does the work (and its checks: track count, genre/userfiles known,
    CD-Text), after one more check of its own: the disc still has no
    name -- or only `ours`, the name this batch itself wrote to it (so a
    disc whose check failed can be written again).
    Returns (items, userfiles_mask, skip_reason)."""
    if current.get("track_count") and current.get("name") is None:
        return [], None, "its disc name couldn't be read"
    if current.get("name") and current["name"] != ours:
        return [], None, f"it has been named since the scan ({current['name']!r})"
    if current.get("cdtext"):
        return [], None, "it's a CD-Text disc, which takes its titles from the disc"
    return library_backup.plan_disc_restore(target, current)


def verify(target: dict, after: dict) -> list[str]:
    """What didn't read back as written: [] when the slot matches. Names
    are compared on the first 25 characters (STORED_TEXT_MAX), and only
    tracks 1-20 (READABLE_TRACK_TITLES), the ones a read returns."""
    if after.get("name") is None or after.get("tracks") is None:
        return ["the slot couldn't be read back"]
    problems = []
    if after["name"] != target["name"][:STORED_TEXT_MAX]:
        problems.append(f"disc name reads {after['name']!r}")
    for n, title in sorted(target["tracks"].items()):
        if n > READABLE_TRACK_TITLES:
            continue
        got = after["tracks"].get(n)
        if got != title[:STORED_TEXT_MAX]:
            problems.append(f"track {n} reads {got!r}")
    if target["genre"] is not None and after.get("genre") != target["genre"]:
        problems.append(f"genre reads {proto.GENRES.get(after.get('genre'), after.get('genre'))!r}")
    return problems


def preview_rows(target: dict, current_genre: int | None) -> list[tuple[str, str]]:
    """(field, value) rows for the review window."""
    if target["genre"] is not None:
        genre = proto.GENRES.get(target["genre"], "?")
    elif current_genre is not None:
        genre = f"{proto.GENRES.get(current_genre, '?')} (kept)"
    else:
        genre = "(kept)"
    rows = [("Disc Name", target["name"]), ("Genre", genre)]
    for n in range(1, target["track_count"] + 1):
        rows.append((f"Track {n}", target["tracks"].get(n, "-")))
    return rows


# -- Per-disc progress ----------------------------------------------------

WAITING = "Waiting"
LOADING = "Loading..."
NO_TOC = "No TOC"
CDTEXT = "CD-Text, skipped"
NO_MATCH = "No match"
LOOKUP_FAILED = "Lookup failed"
RATE_LIMITED = "Not looked up (rate-limited)"
FOUND = "To review"
WRITING = "Writing..."
WRITTEN = "Written"
CHECK_FAILED = "Written, check failed"
NOT_WRITTEN = "Not written"
SKIPPED = "Skipped"
STOPPED = "Not loaded (stopped)"

# Statuses that can still be looked up again (the disc's DiscID is known).
RETRY_LOOKUP = frozenset({NO_MATCH, LOOKUP_FAILED, RATE_LIMITED})
# Statuses where the user can still pick and write a match.
REVIEWABLE = frozenset({FOUND, NOT_WRITTEN, SKIPPED, CHECK_FAILED})


@dataclass
class BatchDisc:
    slot: int
    status: str = WAITING
    note: str = ""
    discid: dict | None = None      # proto.calculate_cddb_discid() result
    track_count: int | None = None  # from the TOC
    matches: list = field(default_factory=list)  # gnudb_client.GnudbMatch
    written_name: str | None = None  # disc name this batch wrote (see plan_write)

    def row(self) -> tuple[str, str, str, str]:
        """(slot, DiscID, status, best candidate) for the review table."""
        best = self.matches[0].title if self.matches else ""
        status = f"{self.status}: {self.note}" if self.note else self.status
        return (str(self.slot), self.discid["discid"] if self.discid else "", status, best)


def lower_first(status: str) -> str:
    """'To review' -> 'to review', but 'CD-Text, skipped' stays as it is."""
    return status if status[1:2].isupper() else status[:1].lower() + status[1:]


def summary(discs: list[BatchDisc]) -> str:
    counts: dict[str, int] = {}
    for d in discs:
        counts[d.status] = counts.get(d.status, 0) + 1
    return ", ".join(f"{n} {lower_first(status)}" for status, n in counts.items())


class Pacer:
    """Keeps gnudb.org requests at least `interval` seconds apart, across
    threads (the batch's lookups and the review window's reads)."""

    def __init__(self, interval: float = GNUDB_MIN_INTERVAL, clock=time.monotonic, sleep=time.sleep):
        self.interval, self.clock, self.sleep = interval, clock, sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self.clock()
            if self._last is not None and now - self._last < self.interval:
                self.sleep(self.interval - (now - self._last))
            self._last = self.clock()
