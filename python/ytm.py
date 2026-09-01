"""FinTube login layer: import a signed-in YouTube session from the Sailfish Browser's
cookie jar and hand it to yt-dlp as a cookies file.

This is a deliberately SLIM sibling of FinTune's ytm.py — only the cookie path. There is no
InnerTube / OAuth / SAPISIDHASH machinery here: FinTube does all its YouTube access through
yt-dlp, so the only thing we need is a Netscape cookies.txt that yt-dlp can consume with
`--cookies`. The engine (youfish) pulls that text via `netscape_cookies()` and materialises it
to an ephemeral, owner-only file for the lifetime of a single yt-dlp call — see
youfish._write_cookies_temp / _cookies_args.

Login flow: the user signs into youtube.com in the real Sailfish Browser (a Gecko browser, so
Google's embedded-webview login block never applies), then FinTube reads the browser's own
cookie jar once (import_browser_login). No copy-paste, no Google Cloud client. The imported
session is kept in a private 0600 store; the browser plaintext is never persisted as a
cookies file on disk.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time

_APP = "harbour-fintube"
_DEBUG = bool(os.environ.get("YOUFISH_DEBUG"))


def _log(msg):
    if _DEBUG:
        try:
            print("[ytm] " + msg)
        except Exception:
            pass


def _data_dir():
    # FinTube's own data dir (matches youfish._data_dir's target), so the login is independent of
    # FinTune's. Not the migration path — youfish owns that; here we only ever read/write our store.
    d = os.path.expanduser("~/.local/share/" + _APP)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _cookies_path():
    return os.path.join(_data_dir(), "youtube_login.json")


# Candidate Sailfish Browser (Gecko) cookie jars, newest layout first.
_BROWSER_COOKIE_PATHS = [
    "~/.local/share/org.sailfishos/browser/.mozilla/cookies.sqlite",
    "~/.mozilla/mozembed/cookies.sqlite",
    "~/.local/share/org.sailfishos/sailfish-browser/.mozilla/cookies.sqlite",
]

# A signed-in Google session must carry one of these; used only to REJECT a jar that has
# youtube cookies but no login (so "Import" gives a clear message instead of a dead session).
_SESSION_MARKERS = ("__Secure-3PAPISID", "SAPISID", "__Secure-3PSID", "__Secure-1PSID", "SID")


def _load_cookies():
    try:
        with open(_cookies_path()) as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


_cookies_lock = threading.Lock()


def _save_cookies(c):
    # Atomic + private: mkstemp creates the temp 0600 (no world-readable window), then os.replace()
    # over the target (atomic on POSIX, so a crash mid-write can't truncate the live store).
    with _cookies_lock:
        try:
            d = _data_dir()
            fd, tmp = tempfile.mkstemp(prefix=".youtube_login.", suffix=".tmp", dir=d)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(c, f)
                os.chmod(tmp, 0o600)          # session cookies — owner-only
                os.replace(tmp, _cookies_path())
            except Exception:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                raise
        except Exception:
            pass


def _read_cookie_jar(path):
    """Read google/youtube cookie rows from a Firefox/Gecko cookies.sqlite. Returns a list of
    (name, value, host, cpath, expiry, secure). Copies the DB (and its -wal/-shm sidecars, so a
    just-completed login not yet checkpointed is picked up) first, so a running browser's lock/WAL
    can't block us."""
    tmp = None
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
        shutil.copyfile(path, tmp)
        for ext in ("-wal", "-shm"):
            if os.path.exists(path + ext):
                shutil.copyfile(path + ext, tmp + ext)
        con = sqlite3.connect(tmp)
        try:
            rows = con.execute(
                "SELECT name, value, host, path, expiry, isSecure FROM moz_cookies "
                "WHERE host LIKE '%youtube.com' OR host LIKE '%google.com'").fetchall()
        finally:
            con.close()
        return rows
    except Exception as ex:
        _log("cookie read failed (%s): %s" % (path, ex))
        return []
    finally:
        for p in ((tmp, tmp + "-wal", tmp + "-shm") if tmp else ()):
            try:
                os.remove(p)
            except Exception:
                pass


def _netscape_from_rows(rows):
    """Build Netscape/Mozilla cookies.txt TEXT from cookie rows (for yt-dlp --cookies). Stored as
    text in the 0600 store; the engine materialises it to an ephemeral file per yt-dlp call."""
    lines = ["# Netscape HTTP Cookie File", ""]
    for name, value, host, cpath, expiry, secure in rows:
        if not host:
            continue
        lines.append("\t".join([host, "TRUE" if host.startswith(".") else "FALSE",
                                 cpath or "/", "TRUE" if secure else "FALSE",
                                 str(int(expiry) if expiry else 0), name, value]))
    return "\n".join(lines) + "\n"


def netscape_cookies():
    """The imported session as Netscape cookies.txt text, or '' when not signed in. Consumed by
    the engine (youfish) to write an ephemeral, private cookies file per yt-dlp call."""
    return _load_cookies().get("ytdlp", "")


def import_browser_login():
    """Import the signed-in YouTube session from the Sailfish Browser's cookie jar into our 0600
    store. Returns {ok, count, source?, error?}. Called from QML (Providers → YouTube account →
    Import from browser)."""
    rows, used = [], ""
    for cand in _BROWSER_COOKIE_PATHS:
        p = os.path.expanduser(cand)
        if os.path.isfile(p):
            rows = _read_cookie_jar(p)
            used = p
            if rows:
                break
    if not rows:
        return {"ok": False, "count": 0,
                "error": "No browser cookies found. Open the Sailfish Browser, sign in at "
                         "youtube.com, then try Import again."}
    names = set(name for (name, value, host, cpath, expiry, secure) in rows)
    if not (names & set(_SESSION_MARKERS)):
        return {"ok": False, "count": 0,
                "error": "Found browser cookies, but not a signed-in Google session. Sign in at "
                         "youtube.com in the Sailfish Browser first."}
    _save_cookies({"ytdlp": _netscape_from_rows(rows), "source": used,
                   "count": len(rows), "imported_at": int(time.time())})
    _log("imported %d youtube/google cookie rows from %s" % (len(rows), used))
    return {"ok": True, "count": len(rows), "source": used}


def login_status():
    """Whether a login is imported, for the QML account UI: {logged_in, count, imported_at, source}."""
    c = _load_cookies()
    return {"logged_in": bool(c.get("ytdlp")),
            "count": int(c.get("count") or 0),
            "imported_at": int(c.get("imported_at") or 0),
            "source": c.get("source", "")}


def logout():
    """Forget the imported session (removes the 0600 store)."""
    try:
        os.remove(_cookies_path())
    except Exception:
        pass
    return {"logged_in": False}
