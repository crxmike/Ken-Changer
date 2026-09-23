"""
gnudb_client.py
================
Minimal HTTP client for gnudb.org's CDDB-compatible query protocol.
Protocol reference: https://gnudb.org/howtognudb.php (fetched directly
while building this -- see the docstrings below for what's actually
specified there vs. inferred).

Only implements what this app needs: `cddb query` (find candidate entries
for a disc, given the DiscID this app already computes from a real TOC)
and `cddb read` (fetch the full track listing for one of them). Uses only
the Python standard library (urllib) -- no extra dependency beyond
pyserial.

gnudb.org's usage policy (from the page above) requires:
  - A real, contactable email address in every request's "hello" string --
    not a generic/default placeholder.
  - A descriptive application name + version, not a generic name like
    "CDClient" or "MyApp".
  - A "cddb query" before every "cddb read" (this module's read() doesn't
    enforce that itself -- the caller is expected to have a match from
    query() first, which is how pclink_app.py uses it).
Non-compliance risks the server blocking access. See build_hello()'s
docstring for how this is enforced on the caller.
"""

from __future__ import annotations

import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass, field

GNUDB_HOST = "gnudb.gnudb.org"
GNUDB_PATH = "/~cddb/cddb.cgi"
# Both documented (gnudb.org's own "sites" command lists this host on both
# port 80 and port 443). Tried in this order; a plain-HTTP timeout is a
# common symptom of a firewall/ISP that's deprioritized or blocked port
# 80 specifically while leaving 443 open, so falling back automatically
# is worth it rather than just failing.
GNUDB_URLS = [
    f"http://{GNUDB_HOST}{GNUDB_PATH}",
    f"https://{GNUDB_HOST}{GNUDB_PATH}",
]
PROTOCOL_LEVEL = 6
DEFAULT_TIMEOUT = 8.0  # per attempt -- kept fairly short since there are 2 URLs to try

# HTTP-level User-Agent header (distinct from the CDDB-protocol-level
# `hello=` string built by build_hello(), which is what actually satisfies
# gnudb.org's documented policy of identifying the app/version). This one
# used to be "python-urllib (gnudb.org CDDB client)" -- self-identifying as
# a script is a common signature some sites' WAF/bot-protection layers
# flag. Changed after a real session where one query() call was followed
# by ALL HTTP(S) access to gnudb.org getting blocked for that IP (site
# loaded fine in a browser beforehand, ping/DNS stayed fine throughout --
# consistent with an application-layer block reacting to something in the
# request itself, not a network problem). Not confirmed as the actual
# cause -- this is an untested hypothesis being tried out, not a fix
# verified against a real block-and-recovery cycle yet.
HTTP_USER_AGENT = "Mozilla/5.0 (compatible; KenwoodPCLinkController)"


class GnudbError(Exception):
    """Raised for network failures and for CDDB-level error responses
    (as opposed to "no match found", which is a normal, non-error result
    -- query() returns an empty list for that instead of raising)."""


class GnudbRateLimited(GnudbError):
    """The server answered with an HTTP status that usually means this IP
    is being throttled or blocked (403/429/503). Retrying right away only
    makes it worse, so _get() stops at the first one instead of falling
    back to the other URL."""


# HTTP statuses treated as "rate-limited/blocked" rather than a generic
# failure. 403 is what the user's earlier block looked like from a
# browser's point of view; 429/503 are the standard throttling codes. Not
# confirmed against a real gnudb.org block response yet.
RATE_LIMIT_STATUSES = (403, 429, 503)


@dataclass
class GnudbMatch:
    """One candidate entry from a `cddb query` response."""
    category: str
    discid: str
    title: str  # "Artist / Album", as the server formats it
    # False for code 211 ("inexact matches"): the server's best guesses for
    # a DiscID it doesn't know exactly, which can be a different album.
    exact: bool = True


@dataclass
class GnudbDisc:
    """A full entry fetched via `cddb read`."""
    category: str
    discid: str
    artist: str
    album: str
    year: str = ""
    genre: str = ""
    track_titles: dict = field(default_factory=dict)  # 1-based track number -> title


def build_hello(contact_email: str, app_name: str, app_version: str) -> str:
    """Builds the `hello=name host appname version` string gnudb.org
    requires on every request. Per gnudb.org's stated policy, `name`+host
    together must be a real, contactable email address (not a generic
    default), and app_name/app_version must actually identify this
    application rather than a generic placeholder -- the caller is
    responsible for prompting the user for their own email rather than
    hardcoding one here."""
    if "@" in contact_email:
        name, _, host = contact_email.partition("@")
    else:
        name, host = contact_email, "unknown.invalid"
    # None of the four hello fields may themselves contain spaces (they're
    # space-separated on the wire, '+'-encoded by urlencode() below).
    name = (name.replace(" ", "_") or "user")
    host = (host.replace(" ", "_") or "unknown.invalid")
    app_name = app_name.replace(" ", "")
    app_version = app_version.replace(" ", "")
    return f"{name} {host} {app_name} {app_version}"


def _get(cmd: str, hello: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Issues one HTTP GET per gnudb.org's CGI protocol:
    http://gnudb.gnudb.org/~cddb/cddb.cgi?cmd=...&hello=...&proto=6
    `cmd` and `hello` are plain, space-separated command strings; urlencode
    turns their spaces into the '+' the docs show, which is exactly the
    format specified.

    Tries each URL in GNUDB_URLS in order (plain HTTP first, matching the
    docs' primary example, then HTTPS as a fallback), since a timeout on
    port 80 specifically is a common firewall/ISP symptom rather than a
    genuine server problem. Raises GnudbError with every attempted URL and
    its specific failure if all of them fail, so the URL can be tested
    directly (e.g. pasted into a browser) to tell a network/firewall issue
    apart from a code issue.

    Catches both `urllib.error.URLError` and bare `OSError` (which
    includes `TimeoutError`/`socket.timeout`). These aren't the same
    thing: urllib only wraps a failure in `URLError` when it happens
    while establishing/sending the request. A timeout that happens while
    *waiting for the response* (connection succeeded, request was sent,
    server just never answered -- confirmed via a real traceback from the
    user, raised from `http.client`'s `getresponse()`) surfaces as a raw
    `TimeoutError` instead, which is not a `URLError` subclass. Without
    catching it too, that case skipped the HTTPS fallback entirely and
    escaped uncaught into the caller's worker thread instead of becoming
    a normal, logged `GnudbError`.

    An HTTP error status (urllib's HTTPError) is different: the server was
    reached and answered, so trying the other URL won't help and just
    sends another request to a server that may be throttling us. It
    raises straight away -- GnudbRateLimited for RATE_LIMIT_STATUSES,
    plain GnudbError otherwise."""
    params = {"cmd": cmd, "hello": hello, "proto": str(PROTOCOL_LEVEL)}
    query_string = urllib.parse.urlencode(params, quote_via=urllib.parse.quote_plus)

    attempts = []
    for base_url in GNUDB_URLS:
        url = f"{base_url}?{query_string}"
        req = urllib.request.Request(url, headers={"User-Agent": HTTP_USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            # Protocol level 6 entries are UTF-8 per the docs' recommendation.
            return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # HTTPError is a URLError subclass, so it has to come first.
            if exc.code in RATE_LIMIT_STATUSES:
                raise GnudbRateLimited(
                    f"gnudb.org refused the request (HTTP {exc.code}) -- this IP is "
                    f"probably rate-limited or blocked. Wait a while (it has taken "
                    f"hours before) and try again. URL: {url}"
                ) from exc
            raise GnudbError(f"gnudb.org returned HTTP {exc.code} {exc.reason}. URL: {url}") from exc
        except (urllib.error.URLError, OSError) as exc:
            attempts.append(f"{url} -> {type(exc).__name__}: {exc}")
            continue

    detail = "; ".join(attempts)
    raise GnudbError(f"Couldn't reach gnudb.org (tried {len(attempts)} URL(s)): {detail}")


def _status_code(lines: list) -> str:
    """The 3-digit CDDB status code from a response's first line. Raises
    GnudbError if the body isn't a CDDB response at all (e.g. an HTML block
    or error page served with HTTP 200), with the start of it for the log."""
    if not lines:
        raise GnudbError("Empty response from gnudb.org")
    code = lines[0].split(" ", 1)[0]
    if len(code) != 3 or not code.isdigit():
        raise GnudbError(
            f"gnudb.org sent something that isn't a CDDB response (possibly a "
            f"block or error page): {lines[0][:120]!r}"
        )
    return code


def query(
    discid: str,
    track_offsets: list,
    total_seconds: int,
    hello: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> list:
    """`cddb query discid ntrks off1 off2 ... nsecs`.

    Returns a list of GnudbMatch candidates -- empty if there's genuinely
    no match (server code 202, not an error). Raises GnudbError for a
    network problem or any other non-success response.

    The docs' own worked example uses status code 210 for "found exact
    matches, list follows" in the example text, while the code table
    elsewhere on the same page lists 200 for a single exact match and 211
    for a list of inexact matches. Handled tolerantly here: 200 is treated
    as one exact match on the status line itself; both 210 and 211 are
    treated as a multi-line list terminated by a lone ".". Matches from a
    211 list are marked exact=False.
    """
    n = len(track_offsets)
    cmd = " ".join(
        ["cddb", "query", discid, str(n)]
        + [str(o) for o in track_offsets]
        + [str(total_seconds)]
    )
    text = _get(cmd, hello, timeout=timeout)
    lines = text.splitlines()
    code = _status_code(lines)
    status_line = lines[0]
    parts = status_line.split(" ", 1)

    if code == "200":
        rest = parts[1] if len(parts) > 1 else ""
        toks = rest.split(" ", 2)
        if len(toks) < 3:
            raise GnudbError(f"Malformed exact-match response: {status_line!r}")
        return [GnudbMatch(category=toks[0], discid=toks[1], title=toks[2])]

    if code in ("210", "211"):
        matches = []
        for line in lines[1:]:
            if line.strip() == ".":
                break
            toks = line.split(" ", 2)
            if len(toks) < 3:
                continue
            matches.append(GnudbMatch(
                category=toks[0], discid=toks[1], title=toks[2], exact=(code == "210"),
            ))
        return matches

    if code == "202":
        return []

    raise GnudbError(f"gnudb.org query failed: {status_line}")


def read(category: str, discid: str, hello: str, timeout: float = DEFAULT_TIMEOUT) -> GnudbDisc:
    """`cddb read category discid` -- fetches the full xmcd entry for one
    match from query(). Per gnudb.org's policy, this should always be
    preceded by a query() call (enforced by how pclink_app.py drives this
    module, not by this function itself)."""
    cmd = f"cddb read {category} {discid}"
    text = _get(cmd, hello, timeout=timeout)
    lines = text.splitlines()
    code = _status_code(lines)
    status_line = lines[0]
    if code != "210":
        raise GnudbError(f"gnudb.org read failed: {status_line}")

    fields: dict = {}
    for line in lines[1:]:
        if line.strip() == ".":
            break
        if line.startswith("#") or "=" not in line:
            continue  # comment lines (track offsets, disc length, etc.)
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.rstrip("\r")
        # xmcd continuation lines: a repeated key means "append to the
        # existing value" (used for titles too long for one line).
        fields[key] = fields.get(key, "") + value if key in fields else value

    dtitle = fields.get("DTITLE", "")
    if " / " in dtitle:
        artist, _, album = dtitle.partition(" / ")
    else:
        artist, album = "", dtitle

    track_titles = {}
    i = 0
    while True:
        key = f"TTITLE{i}"
        if key not in fields:
            break
        # xmcd's TTITLE0 is track 1 -- a 0-based *index* per the CDDB/xmcd
        # standard itself (documented, not inferred). This is unrelated to
        # the Kenwood changer's own "index" field in its TextData replies,
        # which real hardware testing showed already equals the 1-based
        # track number directly with no offset -- two different protocols,
        # two different (coincidentally opposite) conventions.
        track_titles[i + 1] = fields[key]
        i += 1

    return GnudbDisc(
        category=category,
        discid=discid,
        artist=artist,
        album=album,
        year=fields.get("DYEAR", ""),
        genre=fields.get("DGENRE", ""),
        track_titles=track_titles,
    )
