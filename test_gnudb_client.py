#!/usr/bin/env python3
"""
test_gnudb_client.py
====================
Tests for the gnudb.org lookup (gnudb_client.py and the bits of
pclink_app.py that drive it). No network: urllib.request.urlopen is
mocked throughout. The live round-trip (query -> inexact-match picker ->
read -> write to the changer) is CONFIRMED against the real server and a
real CD-425M (v1.8.3 session, see TestRealLookupSession). The canned
server responses below follow the CDDB formats gnudb.org documents
(https://gnudb.org/howtognudb.php) but weren't captured from the real
server -- the app doesn't log raw HTTP bodies.

Stdlib-only (unittest), matching the rest of this project.

What's being tested:
  - The request itself: cmd/hello/proto=6 in the query string, the
    User-Agent, HTTP tried before HTTPS (TestRequest).
  - Falling back to HTTPS when HTTP can't be reached, and NOT falling
    back when the server answered with an HTTP error -- 403/429/503
    raise GnudbRateLimited (v1.8.3) (TestTransport).
  - query(): codes 200 / 210 / 211 / 202, error codes, and non-CDDB
    bodies like an HTML block page (TestQuery).
  - read(): DTITLE split, TTITLE0 = track 1, continuation lines,
    comments, DGENRE/DYEAR (TestRead).
  - The app only auto-loads a single EXACT match; a lone inexact (211)
    match goes to the picker (v1.8.3) (TestAutoRead, TestQueryWorker).
  - The Write to Changer warning for a disc name over 25 characters
    (v1.8.3) (TestDiscNameLengthWarning).
  - The real v1.8.3 session (2026-09-23, slot 1, Trouble at the
    Henhouse), built from its logged frames: our DiscID from the real TOC,
    the 44-character disc name the changer cut to 25, and the curly
    apostrophes that went out as '?' (TestRealLookupSession). v1.8.4:
    gnudb text is folded to ASCII on "Copy gnudb -> Custom" (TestAsciiFold).
"""

from __future__ import annotations

import io
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

import gnudb_client
import pclink_app as app_mod
import pclink_protocol as proto
from gnudb_client import GnudbDisc, GnudbError, GnudbMatch, GnudbRateLimited

HELLO = "someone example.com KenwoodPCLinkController 1.8.3"
HTTP_URL, HTTPS_URL = gnudb_client.GNUDB_URLS


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _respond(text: str):
    """A urlopen side effect returning `text` (CRLF line endings, like the server)."""
    body = text.replace("\n", "\r\n").encode("utf-8")
    return lambda req, timeout=None: _FakeResponse(body)


def _http_error(url, code, reason="Forbidden"):
    return urllib.error.HTTPError(url, code, reason, hdrs=None, fp=None)


def _params(req):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)


QUERY_200 = "200 rock 940aac0d Pink Floyd / Dark Side of the Moon\n"
QUERY_210 = (
    "210 Found exact matches, list follows (until terminating `.')\n"
    "rock 940aac0d Pink Floyd / Dark Side of the Moon\n"
    "misc 940aac0d Pink Floyd / The Dark Side Of The Moon (Remaster)\n"
    ".\n"
)
QUERY_211 = (
    "211 Found inexact matches, list follows (until terminating `.')\n"
    "folk 8a09b20b Bob Dylan / Highway 61 Revisited\n"
    ".\n"
)
QUERY_202 = "202 No match found\n"

READ_210 = (
    "210 rock 940aac0d CD database entry follows (until terminating `.')\n"
    "# xmcd\n"
    "#\n"
    "# Track frame offsets:\n"
    "#        150\n"
    "#        28690\n"
    "#\n"
    "# Disc length: 2604 seconds\n"
    "DISCID=940aac0d\n"
    "DTITLE=Katie Melua / Call Off the Search\n"
    "DYEAR=2003\n"
    "DGENRE=Jazz\n"
    "TTITLE0=Call Off the Search\n"
    "TTITLE1=Crawling Up a Hill\n"
    "TTITLE2=The Closest Thing to Crazy (a title long enough that \n"
    "TTITLE2=it was split over two lines)\n"
    "EXTD=\n"
    "EXTT0=\n"
    "PLAYORDER=\n"
    ".\n"
)


class TestBuildHello(unittest.TestCase):
    def test_splits_email_into_user_and_host(self):
        self.assertEqual(
            gnudb_client.build_hello("someone@example.com", "KenwoodPCLinkController", "1.8.3"),
            HELLO,
        )

    def test_no_field_contains_a_space(self):
        hello = gnudb_client.build_hello("a b@c d.com", "Ken App", "1 2")
        self.assertEqual(hello.split(" "), ["a_b", "c_d.com", "KenApp", "12"])


class TestRequest(unittest.TestCase):
    def test_query_sends_cmd_hello_and_proto_6_over_http_first(self):
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(req)
            return _FakeResponse(QUERY_202.encode())

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            gnudb_client.query("940aac0d", [150, 28690], 2604, HELLO)

        self.assertEqual(len(seen), 1)
        req = seen[0]
        self.assertTrue(req.full_url.startswith(HTTP_URL + "?"))
        params = _params(req)
        self.assertEqual(params["cmd"], ["cddb query 940aac0d 2 150 28690 2604"])
        self.assertEqual(params["hello"], [HELLO])
        self.assertEqual(params["proto"], ["6"])
        self.assertIn("KenwoodPCLinkController", req.get_header("User-agent"))

    def test_read_sends_category_and_discid(self):
        with mock.patch("urllib.request.urlopen", side_effect=_respond(READ_210)) as m:
            gnudb_client.read("rock", "940aac0d", HELLO)
        self.assertEqual(_params(m.call_args[0][0])["cmd"], ["cddb read rock 940aac0d"])


class TestTransport(unittest.TestCase):
    def test_unreachable_http_falls_back_to_https(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            if req.full_url.startswith(HTTP_URL + "?"):
                raise urllib.error.URLError("timed out")
            return _FakeResponse(QUERY_202.encode())

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(gnudb_client.query("940aac0d", [150], 60, HELLO), [])
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].startswith(HTTPS_URL + "?"))

    def test_bare_timeout_while_waiting_also_falls_back(self):
        # The real traceback the user hit: TimeoutError from getresponse(),
        # not wrapped in URLError.
        responses = [TimeoutError("timed out"), _FakeResponse(QUERY_202.encode())]

        def fake_urlopen(req, timeout=None):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(gnudb_client.query("940aac0d", [150], 60, HELLO), [])

    def test_both_unreachable_lists_every_url_tried(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(GnudbError) as ctx:
                gnudb_client.query("940aac0d", [150], 60, HELLO)
        self.assertNotIsInstance(ctx.exception, GnudbRateLimited)
        self.assertIn(HTTP_URL, str(ctx.exception))
        self.assertIn(HTTPS_URL, str(ctx.exception))

    def test_rate_limit_statuses_raise_rate_limited_without_retrying(self):
        for code in (403, 429, 503):
            with self.subTest(code=code):
                with mock.patch(
                    "urllib.request.urlopen", side_effect=_http_error(HTTP_URL, code),
                ) as m:
                    with self.assertRaises(GnudbRateLimited) as ctx:
                        gnudb_client.query("940aac0d", [150], 60, HELLO)
                self.assertEqual(m.call_count, 1, "must not send a second request")
                self.assertIn(f"HTTP {code}", str(ctx.exception))
                self.assertIn("rate-limited", str(ctx.exception))

    def test_other_http_error_is_plain_error_without_retrying(self):
        with mock.patch(
            "urllib.request.urlopen", side_effect=_http_error(HTTP_URL, 500, "Server Error"),
        ) as m:
            with self.assertRaises(GnudbError) as ctx:
                gnudb_client.query("940aac0d", [150], 60, HELLO)
        self.assertNotIsInstance(ctx.exception, GnudbRateLimited)
        self.assertEqual(m.call_count, 1)
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_rate_limited_is_still_a_gnudb_error(self):
        # So any caller that only catches GnudbError still handles it.
        self.assertTrue(issubclass(GnudbRateLimited, GnudbError))


class TestQuery(unittest.TestCase):
    def _query(self, text):
        with mock.patch("urllib.request.urlopen", side_effect=_respond(text)):
            return gnudb_client.query("940aac0d", [150, 28690], 2604, HELLO)

    def test_200_single_exact_match(self):
        self.assertEqual(self._query(QUERY_200), [
            GnudbMatch("rock", "940aac0d", "Pink Floyd / Dark Side of the Moon", exact=True),
        ])

    def test_210_exact_match_list(self):
        matches = self._query(QUERY_210)
        self.assertEqual([m.category for m in matches], ["rock", "misc"])
        self.assertEqual(matches[1].title, "Pink Floyd / The Dark Side Of The Moon (Remaster)")
        self.assertTrue(all(m.exact for m in matches))

    def test_211_inexact_match_list_is_marked_inexact(self):
        matches = self._query(QUERY_211)
        self.assertEqual(matches, [
            GnudbMatch("folk", "8a09b20b", "Bob Dylan / Highway 61 Revisited", exact=False),
        ])

    def test_202_no_match_is_empty_not_an_error(self):
        self.assertEqual(self._query(QUERY_202), [])

    def test_cddb_error_code_raises(self):
        with self.assertRaises(GnudbError) as ctx:
            self._query("500 Command syntax error\n")
        self.assertIn("500 Command syntax error", str(ctx.exception))

    def test_html_page_raises_a_clear_error(self):
        with self.assertRaises(GnudbError) as ctx:
            self._query("<!DOCTYPE html>\n<html><body>Access denied</body></html>\n")
        self.assertIn("isn't a CDDB response", str(ctx.exception))

    def test_empty_body_raises(self):
        with self.assertRaises(GnudbError):
            self._query("")

    def test_malformed_200_raises(self):
        with self.assertRaises(GnudbError):
            self._query("200 rock\n")


class TestRead(unittest.TestCase):
    def _read(self, text):
        with mock.patch("urllib.request.urlopen", side_effect=_respond(text)):
            return gnudb_client.read("rock", "940aac0d", HELLO)

    def test_full_entry(self):
        disc = self._read(READ_210)
        self.assertEqual(disc, GnudbDisc(
            category="rock", discid="940aac0d",
            artist="Katie Melua", album="Call Off the Search",
            year="2003", genre="Jazz",
            track_titles={
                1: "Call Off the Search",
                2: "Crawling Up a Hill",
                3: "The Closest Thing to Crazy (a title long enough that "
                   "it was split over two lines)",
            },
        ))

    def test_dtitle_without_separator_is_album_only(self):
        disc = self._read("210 misc 1 entry\nDTITLE=Various Hits\nTTITLE0=One\n.\n")
        self.assertEqual((disc.artist, disc.album), ("", "Various Hits"))
        self.assertEqual(disc.track_titles, {1: "One"})

    def test_album_containing_slash_splits_on_first_separator(self):
        disc = self._read("210 misc 1 entry\nDTITLE=AC/DC / Back in Black\n.\n")
        self.assertEqual((disc.artist, disc.album), ("AC/DC", "Back in Black"))

    def test_non_210_raises(self):
        with self.assertRaises(GnudbError) as ctx:
            self._read("401 rock 940aac0d No such CD entry in database\n")
        self.assertIn("401", str(ctx.exception))

    def test_html_page_raises_a_clear_error(self):
        with self.assertRaises(GnudbError) as ctx:
            self._read("<html>blocked</html>\n")
        self.assertIn("isn't a CDDB response", str(ctx.exception))


class TestAutoRead(unittest.TestCase):
    def test_single_exact_match_loads_straight_away(self):
        m = GnudbMatch("rock", "940aac0d", "A / B", exact=True)
        self.assertIs(app_mod.gnudb_auto_read_match([m]), m)

    def test_single_inexact_match_goes_to_the_picker(self):
        m = GnudbMatch("rock", "940aac0d", "A / B", exact=False)
        self.assertIsNone(app_mod.gnudb_auto_read_match([m]))

    def test_several_matches_go_to_the_picker(self):
        ms = [GnudbMatch("rock", "1", "A / B"), GnudbMatch("misc", "1", "A / B")]
        self.assertIsNone(app_mod.gnudb_auto_read_match(ms))


class TestQueryWorker(unittest.TestCase):
    """App._gnudb_query_worker driven with a stand-in `self`."""

    DISCID = {"discid": "940aac0d", "track_offsets": [150], "total_seconds": 60}

    def _run(self, query_result):
        fake = types.SimpleNamespace(
            logs=[], ui_queue=mock.Mock(), _gnudb_read_worker=mock.Mock(),
            _show_gnudb_match_picker=mock.Mock(),
        )
        fake._log = fake.logs.append
        patch_kwargs = (
            {"side_effect": query_result} if isinstance(query_result, Exception)
            else {"return_value": query_result}
        )
        with mock.patch.object(gnudb_client, "query", **patch_kwargs):
            app_mod.App._gnudb_query_worker(fake, 3, self.DISCID, HELLO)
        return fake

    def test_exact_match_is_read_without_the_picker(self):
        m = GnudbMatch("rock", "940aac0d", "A / B")
        fake = self._run([m])
        fake._gnudb_read_worker.assert_called_once_with(3, m, HELLO)
        fake.ui_queue.put.assert_not_called()

    def test_lone_inexact_match_opens_the_picker(self):
        fake = self._run([GnudbMatch("rock", "940aac0d", "A / B", exact=False)])
        fake._gnudb_read_worker.assert_not_called()
        fake.ui_queue.put.assert_called_once()
        self.assertTrue(any("no exact match (normal for this changer" in line for line in fake.logs))

    def test_rate_limit_is_logged_as_such(self):
        fake = self._run(GnudbRateLimited("gnudb.org refused the request (HTTP 403)"))
        self.assertTrue(any("rate-limited" in line for line in fake.logs))
        fake._gnudb_read_worker.assert_not_called()


class TestDiscNameLengthWarning(unittest.TestCase):
    def test_25_characters_is_fine(self):
        # Real read-back from slot 4 (README "Owner's manual notes").
        name = "The Hip / Trouble at the "
        self.assertEqual(len(name), 25)
        items = [(0, name, proto.InfoType.DISC_NAMES, "Disc Name")]
        self.assertEqual(app_mod.disc_name_length_warning(items), "")

    def test_long_gnudb_style_disc_name_warns_with_the_kept_text(self):
        name = "Pink Floyd / Dark Side of the Moon"
        items = [(0, name, proto.InfoType.DISC_NAMES, "Disc Name", 0)]
        warning = app_mod.disc_name_length_warning(items)
        self.assertIn(f"{len(name)} characters", warning)
        self.assertIn(repr(name[:25]), warning)

    def test_long_track_names_are_not_checked(self):
        # The manual's 25-character limit is for disc titles; track-name
        # limits aren't documented, so no warning for them.
        items = [(1, "x" * 40, proto.InfoType.TRACK_NAMES, "Track 1")]
        self.assertEqual(app_mod.disc_name_length_warning(items), "")


def _data(frame_hex):
    """Payload bytes of a logged frame (strip STX, cmd, 2 length bytes, checksum)."""
    return bytes.fromhex(frame_hex)[4:-1]


class TestRealLookupSession(unittest.TestCase):
    """Frames from the user's v1.8.3 log (real CD-425M + live gnudb.org)."""

    TOC_FRAME = (
        "02 06 2d 00 01 00 01 00 01 0c 00 02 00 05 00 00 09 37 00 13 21 00 18 29 "
        "00 22 35 00 26 14 00 30 01 00 33 58 00 37 19 00 42 12 00 47 27 00 52 38 00 19"
    )
    # What went out for the disc name (44 chars) and what read back (25).
    DISC_NAME_WRITE = (
        "02 fe 33 00 01 00 00 07 00 03 00 54 68 65 20 54 72 61 67 69 63 61 6c 6c 79 20 "
        "48 69 70 20 2f 20 54 72 6f 75 62 6c 65 20 61 74 20 74 68 65 20 48 65 6e 68 6f "
        "75 73 65 35"
    )
    DISC_NAME_READBACK = (
        "02 fe 20 00 01 00 00 07 00 03 00 54 68 65 20 54 72 61 67 69 63 61 6c 6c 79 20 "
        "48 69 70 20 2f 20 54 72 6f 75 30"
    )
    TRACK4_WRITE = (
        "02 fe 17 00 01 00 04 07 01 03 00 44 6f 6e 3f 74 20 57 61 6b 65 20 44 61 64 64 79 59"
    )

    def test_discid_from_the_real_toc(self):
        toc = proto.decode_disc_toc(_data(self.TOC_FRAME))
        result = proto.calculate_cddb_discid(toc["track_times"])
        self.assertEqual(result["discid"], "930c540c")  # what the app queried
        self.assertEqual(result["total_seconds"], 3156)

    def test_changer_toc_has_whole_seconds_only(self):
        # Every frames field on this unit's TOC was 0, so the offsets we
        # send gnudb.org are only accurate to the second.
        toc = proto.decode_disc_toc(_data(self.TOC_FRAME))
        self.assertTrue(all(t["frames"] == 0 for t in toc["track_times"]))

    def test_long_disc_name_went_out_whole_and_came_back_cut_at_25(self):
        name = "The Tragically Hip / Trouble at the Henhouse"
        self.assertEqual(
            proto.encode_frame(proto.CMD_TEXT_DATA, proto.encode_text_data(
                1, 0, name, userfiles=0x07, genre=3,
            )),
            bytes.fromhex(self.DISC_NAME_WRITE),
        )
        back = proto.decode_text_data(_data(self.DISC_NAME_READBACK))["text"]
        self.assertEqual(back, name[:app_mod.DISC_TITLE_MAX])
        self.assertIn(repr(back), app_mod.disc_name_length_warning(
            [(0, name, proto.InfoType.DISC_NAMES, "Disc Name")]
        ))

    def test_curly_apostrophe_went_out_as_question_mark(self):
        sent = proto.encode_text_data(
            1, 4, "Don’t Wake Daddy", userfiles=0x07,
            info_type=proto.InfoType.TRACK_NAMES, genre=3,
        )
        self.assertEqual(sent, _data(self.TRACK4_WRITE))

    def test_folded_text_sends_a_real_apostrophe(self):
        sent = proto.encode_text_data(
            1, 4, app_mod.ascii_fold("Don’t Wake Daddy"), userfiles=0x07,
            info_type=proto.InfoType.TRACK_NAMES, genre=3,
        )
        self.assertEqual(sent[7:], b"Don't Wake Daddy")


class TestRealFoldedWriteSession(unittest.TestCase):
    """The user's v1.8.4 log (2026-09-23): after "Copy gnudb -> Custom" with
    ascii_fold, tracks 4 and 10 went out with a plain apostrophe (0x27) and
    read back exactly -- CONFIRMED on real hardware."""

    TRACK4 = "02 fe 17 00 01 00 04 07 01 03 00 44 6f 6e 27 74 20 57 61 6b 65 20 44 61 64 64 79 71"
    TRACK10 = (
        "02 fe 19 00 01 00 0a 07 01 03 00 4c 65 74 27 73 20 53 74 61 79 20 45 6e 67 61 67 65 64 88"
    )

    def test_folded_writes_match_the_logged_frames(self):
        for index, gnudb_title, frame in (
            (4, "Don’t Wake Daddy", self.TRACK4),
            (10, "Let’s Stay Engaged", self.TRACK10),
        ):
            with self.subTest(index=index):
                sent = proto.encode_frame(proto.CMD_TEXT_DATA, proto.encode_text_data(
                    1, index, app_mod.ascii_fold(gnudb_title), userfiles=0x07,
                    info_type=proto.InfoType.TRACK_NAMES, genre=3,
                ))
                self.assertEqual(sent, bytes.fromhex(frame))

    def test_read_back_is_identical_to_what_was_sent(self):
        # The changer's read reply for these tracks was byte-identical to
        # the write frame, so decoding it gives the plain-apostrophe title.
        self.assertEqual(proto.decode_text_data(_data(self.TRACK4))["text"], "Don't Wake Daddy")
        self.assertEqual(proto.decode_text_data(_data(self.TRACK10))["text"], "Let's Stay Engaged")


class TestAsciiFold(unittest.TestCase):
    def test_typographic_punctuation(self):
        self.assertEqual(app_mod.ascii_fold("Let’s Stay Engaged"), "Let's Stay Engaged")
        self.assertEqual(app_mod.ascii_fold("“Hi” – …"), '"Hi" - ...')

    def test_accents_are_dropped(self):
        self.assertEqual(app_mod.ascii_fold("Beyoncé / Motörhead"), "Beyonce / Motorhead")

    def test_plain_ascii_is_unchanged(self):
        self.assertEqual(app_mod.ascii_fold("700 ft. Ceiling"), "700 ft. Ceiling")

    def test_unmappable_becomes_question_mark(self):
        self.assertEqual(app_mod.ascii_fold("東京"), "??")


class TestGenreMatch(unittest.TestCase):
    """v1.8.7: gnudb's free-text genre -> the changer's genre dropdown."""

    def test_case_is_ignored(self):
        self.assertEqual(app_mod.match_changer_genre("rock"), "Rock")
        self.assertEqual(app_mod.match_changer_genre("JAZZ"), "Jazz")
        self.assertEqual(app_mod.match_changer_genre("alternative rock"), "Alternative Rock")

    def test_hyphens_spaces_and_ampersands(self):
        self.assertEqual(app_mod.match_changer_genre("hip-hop"), "Hip Hop")
        self.assertEqual(app_mod.match_changer_genre("new-age"), "New Age")
        self.assertEqual(app_mod.match_changer_genre("  World   Music "), "World Music")
        self.assertEqual(app_mod.match_changer_genre("rhythm and blues"), "Rhythm & Blues")

    def test_different_words_stay_blank(self):
        # From the user's screenshot: gnudb's "folk rock" isn't a changer
        # genre, and isn't forced into Folk or Rock.
        for text in ("folk rock", "Alternative", "r&b", "-", ""):
            with self.subTest(text=text):
                self.assertEqual(app_mod.match_changer_genre(text), "")

    def test_every_changer_genre_matches_itself(self):
        # Also proves no two changer genres collapse to the same key.
        for name in proto.GENRE_NAME_TO_CODE:
            with self.subTest(name=name):
                self.assertEqual(app_mod.match_changer_genre(name), name)
                self.assertEqual(app_mod.match_changer_genre(name.lower()), name)


class TestMatchSummary(unittest.TestCase):
    def test_shows_category_discid_and_inexact(self):
        line = app_mod.gnudb_match_summary([
            GnudbMatch("rock", "930c540c", "A / B"),
            GnudbMatch("misc", "920c540c", "A / C", exact=False),
        ])
        self.assertEqual(line, "[rock 930c540c] A / B; [misc 920c540c, inexact] A / C")


if __name__ == "__main__":
    unittest.main()
