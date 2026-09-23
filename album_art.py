"""
album_art.py
============
Album cover lookup for the Disc Data tab, run after a gnudb.org read.

1. gnudb first (v1.9.1). A `cddb read` response carries the entry's
   cover art as xmcd comment lines, one pair per MusicBrainz release
   (documented on https://gnudb.org/howtognudb.php):

       # Cover: https://coverartarchive.org/release/<mbid>/<id>-500.jpg
       # Artid: <mbid>

   gnudb_client.read() collects the Cover URLs into GnudbDisc.cover_urls;
   they're tried in order. These belong to the exact entry the user
   picked, so they're preferred over any search.
2. iTunes Search API as the fallback, for entries with no Cover line or
   whose images all fail to download: search by gnudb's artist/album and
   take the artwork of a result that matches (see pick_best_result).

Both serve JPEGs, which Tkinter can't show on its own, so this needs
Pillow (`pip install pillow`, a requirement as of v1.9.1). Images are
decoded and shrunk here, on the worker thread; the app only turns the
result into an ImageTk.PhotoImage.

No personal data is sent: only the artist/album text and a generic
User-Agent, not the gnudb contact email.
"""

from __future__ import annotations

import io
import json
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

try:
    from PIL import Image
except ImportError:  # the app still runs; the lookup just says what's missing
    Image = None

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
DEFAULT_TIMEOUT = 8.0
DEFAULT_SIZE = 200  # pixels; the shown image fits in a square this size
ITUNES_FETCH_SIZE = 600  # ask iTunes for this, then shrink to DEFAULT_SIZE
SEARCH_LIMIT = 10
HTTP_USER_AGENT = "Mozilla/5.0 (compatible; KenwoodPCLinkController)"


class AlbumArtError(Exception):
    """Network failure, an image that won't decode, or Pillow missing (as
    opposed to "no art found", which is a normal result:
    fetch_album_art() returns None)."""


@dataclass
class AlbumArt:
    image: object  # PIL.Image.Image, already shrunk to fit DEFAULT_SIZE
    source: str  # "gnudb" or "iTunes"
    url: str
    exact: bool = True  # False for an iTunes starts-with title match
    artist: str = ""  # as the iTunes result spells it ("" for gnudb)
    album: str = ""


# Edition/remaster suffixes that often differ between gnudb and iTunes:
# "Album (Remastered)", "Album [Deluxe Edition]".
_BRACKETED = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")


def normalize(text: str) -> str:
    """Comparison key: lowercase, accents and punctuation dropped,
    bracketed suffixes removed, '&' -> 'and', a leading 'the' dropped.
    "The Tragically Hip" and "Tragically Hip" compare equal, and so do
    "Trouble at the Henhouse" and "Trouble At the Henhouse (Remastered)"."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = _BRACKETED.sub("", text).replace("&", " and ")
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def pick_best_result(results: list, artist: str, album: str):
    """The iTunes result whose artwork to use, and whether the album
    matched as well as the artist: (result, exact) or (None, False).

    The artist must match (after normalize()) -- a search on a common
    album title otherwise happily returns someone else's cover. Among
    those, an album-title match wins; if none matches exactly, one whose
    title starts with ours (or ours with its) is next ("Album" vs "Album
    - Single"). Failing that, nothing: a different album by the same
    artist would be the wrong cover, so it's better to show none."""
    want_artist = normalize(artist)
    want_album = normalize(album)
    if not want_album:
        return None, False
    candidates = [
        r for r in results
        if r.get("artworkUrl100")
        and (not want_artist or normalize(r.get("artistName", "")) == want_artist)
    ]
    for r in candidates:
        if normalize(r.get("collectionName", "")) == want_album:
            return r, True
    for r in candidates:
        got = normalize(r.get("collectionName", ""))
        if got and (got.startswith(want_album) or want_album.startswith(got)):
            return r, False
    return None, False


def itunes_artwork_url(artwork_url: str, size: int = ITUNES_FETCH_SIZE) -> str:
    """Rewrites iTunes' ".../100x100bb.jpg" artwork URL to ask for a
    bigger `size`x`size` JPEG (the size part of the URL is how Apple's
    image server picks the size)."""
    return re.sub(r"/\d+x\d+([a-z]*)\.(jpg|jpeg|png|webp)$", rf"/{size}x{size}\1.\2", artwork_url)


def build_search_url(artist: str, album: str) -> str:
    term = f"{artist} {album}".strip()
    params = {"term": term, "entity": "album", "media": "music", "limit": str(SEARCH_LIMIT)}
    return f"{ITUNES_SEARCH_URL}?{urllib.parse.urlencode(params)}"


def _get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": HTTP_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise AlbumArtError(f"HTTP {exc.code} {exc.reason} from {url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AlbumArtError(f"couldn't reach {url}: {type(exc).__name__}: {exc}") from exc


def decode_image(data: bytes, size: int = DEFAULT_SIZE):
    """Decodes JPEG/PNG bytes with Pillow and shrinks them to fit a
    `size`x`size` square (aspect ratio kept). Raises AlbumArtError if
    Pillow is missing or the bytes aren't an image (e.g. an HTML error
    page served with HTTP 200)."""
    if Image is None:
        raise AlbumArtError("Pillow isn't installed -- run: pip install pillow")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # Pillow raises several types for bad data
        raise AlbumArtError(f"not a readable image ({data[:8]!r}): {exc}") from exc
    img = img.convert("RGB")
    img.thumbnail((size, size))
    return img


def fetch_gnudb_cover(cover_urls: list, timeout: float = DEFAULT_TIMEOUT, errors=None):
    """The first of gnudb's `# Cover:` URLs that downloads and decodes, as
    AlbumArt, or None. Each failure is appended to `errors` (if given) so
    the caller can log why it fell back to iTunes."""
    for url in cover_urls:
        try:
            return AlbumArt(image=decode_image(_get(url, timeout)), source="gnudb", url=url)
        except AlbumArtError as exc:
            if errors is not None:
                errors.append(str(exc))
    return None


def fetch_itunes_cover(artist: str, album: str, timeout: float = DEFAULT_TIMEOUT):
    """Searches iTunes and downloads the matching cover, or returns None
    if no result matches (see pick_best_result). Raises AlbumArtError for
    network problems or an image that won't decode."""
    raw = _get(build_search_url(artist, album), timeout)
    try:
        results = json.loads(raw.decode("utf-8", errors="replace")).get("results", [])
    except (ValueError, AttributeError) as exc:
        raise AlbumArtError(f"iTunes search didn't return JSON: {raw[:120]!r}") from exc

    best, exact = pick_best_result(results, artist, album)
    if best is None:
        return None
    url = itunes_artwork_url(best["artworkUrl100"])
    return AlbumArt(
        image=decode_image(_get(url, timeout)), source="iTunes", url=url, exact=exact,
        artist=best.get("artistName", ""), album=best.get("collectionName", ""),
    )


def fetch_album_art(disc, timeout: float = DEFAULT_TIMEOUT, errors=None):
    """Cover art for a gnudb_client.GnudbDisc: gnudb's own Cover URLs
    first, then iTunes. Returns None if neither has one. gnudb failures
    go into `errors` (if given); an iTunes failure raises AlbumArtError."""
    if Image is None:
        raise AlbumArtError("Pillow isn't installed -- run: pip install pillow")
    art = fetch_gnudb_cover(getattr(disc, "cover_urls", []), timeout, errors)
    if art is not None or not disc.album:
        return art
    return fetch_itunes_cover(disc.artist, disc.album, timeout)
