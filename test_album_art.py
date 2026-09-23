#!/usr/bin/env python3
"""
test_album_art.py
=================
Tests for the cover art lookup (album_art.py, v1.9.0/v1.9.1) and the
bits of pclink_app.py / gnudb_client.py that drive it. No network:
urllib.request.urlopen is mocked throughout.

  - gnudb's own "# Cover:" lines are parsed out of a `cddb read` (the
    response is gnudb.org's documented example) and tried first
    (TestGnudbCoverLines, TestFetchAlbumArt).
  - iTunes is the fallback. Its canned result is trimmed from a real
    search response for Trouble at the Henhouse (the v1.8.3 disc),
    fetched by hand while building this.
  - Images are decoded with Pillow (real JPEG bytes here) and shown via
    ImageTk (TestPhotoImageShows).

NOT yet tried in the app against the live services.

unittest only, like the rest of this project; needs Pillow, which the
app itself now requires (v1.9.1).
"""

from __future__ import annotations

import io
import json
import sys
import types
import unittest
import urllib.error
import urllib.parse
from unittest import mock

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

from PIL import Image

import album_art
import gnudb_client
import pclink_app as app_mod
from album_art import AlbumArt, AlbumArtError
from gnudb_client import GnudbDisc


def _jpeg(width=500, height=500) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


JPEG = _jpeg()

HENHOUSE_ART = (
    "https://is1-ssl.mzstatic.com/image/thumb/Music124/v4/65/1d/fd/"
    "651dfd6c-4e4e-905e-09ac-77c25406f764/15UMGIM12259.rgb.jpg/100x100bb.jpg"
)
HENHOUSE = {
    "artistName": "The Tragically Hip",
    "collectionName": "Trouble At the Henhouse",
    "artworkUrl100": HENHOUSE_ART,
}

COVER_1 = "https://coverartarchive.org/release/8763f781-57b1-4c84-90da-5cb7180873da/38592060423-500.jpg"
COVER_2 = "https://coverartarchive.org/release/95ade33a-58ff-4466-9d12-239dcdba0ccc/34700528534-500.jpg"

# gnudb.org's documented `cddb read` example (howtognudb.php), trimmed,
# with a couple of metadata fields filled in.
READ_WITH_COVERS = (
    "210 data 860a8c86 CD database entry follows (until terminating `.')\n"
    "# xmcd CD database file\n"
    "#\n"
    "# Track frame offsets:\n"
    "# 150\n"
    "#\n"
    "# Disc length: 2702 seconds\n"
    "#\n"
    "# Revision: 0\n"
    "# Processed by: gnucddb v1.0.1 Copyright (c) Gnudb.\n"
    "# Submitted via: CDex 1.51\n"
    f"# Cover: {COVER_1}\n"
    "# Artid: 8763f781-57b1-4c84-90da-5cb7180873da\n"
    f"# Cover: {COVER_2}\n"
    "# Artid: 95ade33a-58ff-4466-9d12-239dcdba0ccc\n"
    "DISCID=860a8c86\n"
    "DTITLE=Katie Melua / Piece by Piece\n"
    "TTITLE0=Shy Boy\n"
    ".\n"
)


def _result(artist, album, art="https://example.invalid/x.jpg/100x100bb.jpg"):
    return {"artistName": artist, "collectionName": album, "artworkUrl100": art}


def _disc(covers=(), artist="The Tragically Hip", album="Trouble at the Henhouse"):
    return GnudbDisc("rock", "930c540c", artist, album, cover_urls=list(covers))


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_for(search_results, image=JPEG, cover_errors=()):
    """urlopen stand-in: the iTunes search returns `search_results`, a URL
    in `cover_errors` raises URLError, any other URL returns `image`.
    Records every requested URL."""
    seen = []

    def fake(req, timeout=None):
        seen.append(req.full_url)
        if req.full_url in cover_errors:
            raise urllib.error.URLError("down")
        if req.full_url.startswith(album_art.ITUNES_SEARCH_URL):
            return _FakeResponse(json.dumps({"results": search_results}).encode())
        return _FakeResponse(image)

    return fake, seen


class TestGnudbCoverLines(unittest.TestCase):
    def _read(self, text):
        body = text.replace("\n", "\r\n").encode("utf-8")
        with mock.patch("urllib.request.urlopen",
                        side_effect=lambda req, timeout=None: _FakeResponse(body)):
            return gnudb_client.read("data", "860a8c86", "a b c 1")

    def test_cover_urls_parsed_in_order(self):
        disc = self._read(READ_WITH_COVERS)
        self.assertEqual(disc.cover_urls, [COVER_1, COVER_2])
        # The rest of the entry is unaffected.
        self.assertEqual((disc.artist, disc.album), ("Katie Melua", "Piece by Piece"))
        self.assertEqual(disc.track_titles, {1: "Shy Boy"})

    def test_entry_without_cover_lines(self):
        text = "".join(
            line + "\n" for line in READ_WITH_COVERS.splitlines() if "Cover:" not in line
        )
        self.assertEqual(self._read(text).cover_urls, [])


class TestNormalize(unittest.TestCase):
    def test_case_leading_the_and_punctuation(self):
        self.assertEqual(album_art.normalize("The Tragically Hip"), "tragically hip")
        self.assertEqual(album_art.normalize("Tragically Hip"), "tragically hip")
        self.assertEqual(
            album_art.normalize("Trouble At the Henhouse"),
            album_art.normalize("Trouble at the Henhouse"),
        )

    def test_brackets_accents_and_ampersand(self):
        self.assertEqual(album_art.normalize("Album (Remastered)"), "album")
        self.assertEqual(album_art.normalize("Album [Deluxe Edition]"), "album")
        self.assertEqual(album_art.normalize("Beyoncé"), "beyonce")
        self.assertEqual(album_art.normalize("Simon & Garfunkel"), "simon and garfunkel")


class TestPickBestResult(unittest.TestCase):
    def test_real_henhouse_result_matches_gnudb_spelling(self):
        # gnudb's DTITLE from the v1.8.3 session vs iTunes' capitalization.
        best, exact = album_art.pick_best_result(
            [HENHOUSE], "The Tragically Hip", "Trouble at the Henhouse")
        self.assertIs(best, HENHOUSE)
        self.assertTrue(exact)

    def test_exact_album_preferred_over_earlier_prefix_match(self):
        prefix = _result("Artist", "Album - Single")
        exact = _result("Artist", "Album (Remastered)")
        best, is_exact = album_art.pick_best_result([prefix, exact], "Artist", "Album")
        self.assertIs(best, exact)
        self.assertTrue(is_exact)

    def test_prefix_match_is_used_but_not_exact(self):
        prefix = _result("Artist", "Album - Single")
        best, exact = album_art.pick_best_result([prefix], "Artist", "Album")
        self.assertIs(best, prefix)
        self.assertFalse(exact)

    def test_other_artist_with_same_album_title_is_rejected(self):
        best, _ = album_art.pick_best_result(
            [_result("Someone Else", "Greatest Hits")], "Artist", "Greatest Hits")
        self.assertIsNone(best)

    def test_other_album_by_same_artist_is_rejected(self):
        best, _ = album_art.pick_best_result(
            [_result("The Tragically Hip", "Fully Completely")],
            "The Tragically Hip", "Trouble at the Henhouse")
        self.assertIsNone(best)

    def test_result_without_artwork_is_skipped(self):
        no_art = {"artistName": "Artist", "collectionName": "Album"}
        self.assertEqual(album_art.pick_best_result([no_art], "Artist", "Album"), (None, False))

    def test_no_artist_from_gnudb_matches_on_album_alone(self):
        # gnudb DTITLE without " / " gives artist "" (gnudb_client.read).
        r = _result("Anyone", "Album")
        self.assertEqual(album_art.pick_best_result([r], "", "Album"), (r, True))


class TestArtworkUrl(unittest.TestCase):
    def test_real_url_asks_for_bigger_jpeg(self):
        self.assertEqual(
            album_art.itunes_artwork_url(HENHOUSE_ART, 600),
            HENHOUSE_ART[: -len("100x100bb.jpg")] + "600x600bb.jpg",
        )

    def test_search_url_params(self):
        url = album_art.build_search_url("The Tragically Hip", "Trouble at the Henhouse")
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(params["term"], ["The Tragically Hip Trouble at the Henhouse"])
        self.assertEqual(params["entity"], ["album"])
        self.assertEqual(params["media"], ["music"])


class TestDecodeImage(unittest.TestCase):
    def test_jpeg_shrunk_to_fit(self):
        img = album_art.decode_image(_jpeg(500, 400), 200)
        self.assertEqual(img.size, (200, 160))

    def test_html_page_raises(self):
        with self.assertRaises(AlbumArtError):
            album_art.decode_image(b"<html>blocked</html>")

    def test_missing_pillow_raises_with_install_hint(self):
        with mock.patch.object(album_art, "Image", None):
            with self.assertRaisesRegex(AlbumArtError, "pip install pillow"):
                album_art.decode_image(JPEG)


class TestFetchAlbumArt(unittest.TestCase):
    def test_gnudb_cover_used_without_searching_itunes(self):
        fake, seen = _urlopen_for([HENHOUSE])
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            art = album_art.fetch_album_art(_disc([COVER_1, COVER_2]))
        self.assertEqual((art.source, art.url), ("gnudb", COVER_1))
        self.assertEqual(art.image.size, (200, 200))
        self.assertEqual(seen, [COVER_1])

    def test_next_gnudb_cover_tried_when_first_fails(self):
        fake, _ = _urlopen_for([HENHOUSE], cover_errors=(COVER_1,))
        errors = []
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            art = album_art.fetch_album_art(_disc([COVER_1, COVER_2]), errors=errors)
        self.assertEqual((art.source, art.url), ("gnudb", COVER_2))
        self.assertEqual(len(errors), 1)

    def test_itunes_fallback_when_gnudb_has_no_cover(self):
        fake, seen = _urlopen_for([HENHOUSE])
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            art = album_art.fetch_album_art(_disc())
        self.assertEqual(art.source, "iTunes")
        self.assertTrue(art.exact)
        self.assertTrue(seen[0].startswith(album_art.ITUNES_SEARCH_URL))
        self.assertTrue(seen[1].endswith("/600x600bb.jpg"))

    def test_itunes_fallback_when_every_gnudb_cover_fails(self):
        fake, _ = _urlopen_for([HENHOUSE], cover_errors=(COVER_1,))
        errors = []
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            art = album_art.fetch_album_art(_disc([COVER_1]), errors=errors)
        self.assertEqual(art.source, "iTunes")
        self.assertEqual(len(errors), 1)

    def test_no_match_anywhere_returns_none_without_downloading(self):
        fake, seen = _urlopen_for([_result("Other", "Other")])
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            self.assertIsNone(album_art.fetch_album_art(_disc(artist="Artist", album="Album")))
        self.assertEqual(len(seen), 1)

    def test_no_album_and_no_cover_skips_itunes(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertIsNone(album_art.fetch_album_art(_disc(artist="", album="")))
        urlopen.assert_not_called()

    def test_itunes_network_errors_raise_album_art_error(self):
        for exc in (
            urllib.error.URLError("down"),
            TimeoutError("timed out"),
            urllib.error.HTTPError(album_art.ITUNES_SEARCH_URL, 403, "Forbidden", None, None),
        ):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch("urllib.request.urlopen", side_effect=exc):
                    with self.assertRaises(AlbumArtError):
                        album_art.fetch_album_art(_disc())

    def test_non_json_search_raises(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=lambda req, timeout=None: _FakeResponse(b"<html>")):
            with self.assertRaises(AlbumArtError):
                album_art.fetch_album_art(_disc())


class TestAlbumArtWorker(unittest.TestCase):
    """App._album_art_worker driven with a stand-in `self`."""

    def _run(self, result, gnudb_errors=()):
        fake = types.SimpleNamespace(
            logs=[], ui_queue=mock.Mock(), _album_art_cache={}, _disc_data_slot=1,
        )
        fake._log = fake.logs.append

        def fetch(disc, errors=None, **kwargs):
            errors.extend(gnudb_errors)
            if isinstance(result, Exception):
                raise result
            return result

        with mock.patch.object(album_art, "fetch_album_art", side_effect=fetch):
            app_mod.App._album_art_worker(fake, 1, _disc([COVER_1]))
        return fake

    def test_gnudb_cover_is_cached_shown_and_logged(self):
        art = AlbumArt(object(), "gnudb", COVER_1)
        fake = self._run(art)
        self.assertIs(fake._album_art_cache[1], art)
        fake.ui_queue.put.assert_called_once()
        self.assertTrue(any("from the gnudb entry" in line for line in fake.logs))

    def test_itunes_fallback_logged_with_gnudb_failures(self):
        art = AlbumArt(object(), "iTunes", "u", True, "A", "B")
        fake = self._run(art, gnudb_errors=["HTTP 404 Not Found from x"])
        self.assertTrue(any("gnudb's cover link failed" in line for line in fake.logs))
        self.assertTrue(any("using iTunes'" in line for line in fake.logs))

    def test_inexact_itunes_title_is_flagged(self):
        fake = self._run(AlbumArt(object(), "iTunes", "u", False, "A", "B - Single"))
        self.assertTrue(any("closest title" in line for line in fake.logs))

    def test_nothing_found_is_cached_as_none(self):
        fake = self._run(None)
        self.assertIsNone(fake._album_art_cache[1])
        self.assertTrue(any("none found" in line for line in fake.logs))

    def test_failure_is_only_logged(self):
        fake = self._run(AlbumArtError("couldn't reach"))
        self.assertEqual(fake._album_art_cache, {})
        fake.ui_queue.put.assert_not_called()
        self.assertTrue(any("Cover art lookup failed" in line for line in fake.logs))


class TestPhotoImageShows(unittest.TestCase):
    """The same ImageTk path _show_album_art uses, against real Tk."""

    def test_decoded_jpeg_shows(self):
        import tkinter as tk
        from PIL import ImageTk
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display: {exc}")
        try:
            root.withdraw()
            photo = ImageTk.PhotoImage(album_art.decode_image(JPEG), master=root)
            self.assertEqual((photo.width(), photo.height()), (200, 200))
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
