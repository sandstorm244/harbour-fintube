"""FinTube YouTube session layer.

Two jobs:
  1. Import a signed-in YouTube session from the Sailfish Browser's cookie jar and hand it to
     yt-dlp as a Netscape cookies file (netscape_cookies()) — the original, unchanged path.
  2. A small InnerTube (youtubei) client used ONLY for account-scoped things yt-dlp can't do per
     signed-in identity: enumerate the signed-in Google accounts/channels (the account switcher)
     and fetch THIS account's subscriptions + playlists. yt-dlp derives the active account/channel
     from the page it's served and offers no override, so importing a NON-default account (a second
     Google login, or a brand-account channel) has to go through InnerTube with the identity headers
     X-Goog-AuthUser (login) + X-Goog-PageId (channel).

The InnerTube identity (API key + client version) is SELF-HEALING: scraped from youtube.com's ytcfg
and cached, so a client-version rotation doesn't silently start 400ing. Run with YOUFISH_DEBUG=1 to
see what each step sees ([ytm] log lines).

Login flow: the user signs into youtube.com in the real Sailfish Browser, then FinTube reads the
browser's cookie jar once (import_browser_login). The session is kept in a private 0600 store.
"""

import gzip
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_APP = "harbour-fintube"
_DEBUG = bool(os.environ.get("YOUFISH_DEBUG"))


def _log(msg):
    if _DEBUG:
        try:
            print("[ytm] " + msg)
        except Exception:
            pass


def _data_dir():
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

# A signed-in Google session must carry one of these (used to REJECT a jar that has youtube cookies
# but no login, so Import gives a clear message instead of a dead session).
_SESSION_MARKERS = ("__Secure-3PAPISID", "SAPISID", "__Secure-3PSID", "__Secure-1PSID", "SID")

# The auth cookies InnerTube needs in the Cookie header.
_AUTH_COOKIE_NAMES = ("__Secure-3PAPISID", "SAPISID", "__Secure-1PAPISID",
                      "__Secure-3PSID", "__Secure-1PSID", "SID", "HSID", "SSID",
                      "APISID", "SIDCC", "__Secure-3PSIDCC", "__Secure-1PSIDCC",
                      "LOGIN_INFO", "PREF", "VISITOR_INFO1_LIVE",
                      "__Secure-3PSIDTS", "__Secure-1PSIDTS")

# InnerTube (regular YouTube WEB client).
_YT_ORIGIN = "https://www.youtube.com"
_INNERTUBE = _YT_ORIGIN + "/youtubei/v1/"
_DEFAULT_INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"   # public web site key, not secret
_DEFAULT_CLIENT_VERSION = "2.20241201.00.00"
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------- #
# IPv4 pin (avoid stalled connects on unroutable-IPv6 networks — see youfish._force_ipv4) + gzip.
# --------------------------------------------------------------------------- #
_ipv4_forced = False


def _force_ipv4():
    global _ipv4_forced
    if _ipv4_forced:
        return
    _orig = socket.getaddrinfo

    def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        return _orig(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_only
    _ipv4_forced = True


def _read_body(resp):
    """Read a response body, transparently gunzipping it (InnerTube browse responses are large)."""
    raw = resp.read()
    try:
        enc = (resp.headers.get("Content-Encoding") or "").lower()
    except Exception:
        enc = ""
    if "gzip" in enc:
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    return raw


# --------------------------------------------------------------------------- #
# Cookie store (0600 JSON). Holds the yt-dlp Netscape text, the InnerTube Cookie header + SAPISID,
# and the chosen account identity (authuser / page_id / datasync_id / name).
# --------------------------------------------------------------------------- #
_cookies_lock = threading.Lock()


def _load_cookies():
    try:
        with open(_cookies_path()) as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def _save_cookies(c):
    with _cookies_lock:
        try:
            d = _data_dir()
            fd, tmp = tempfile.mkstemp(prefix=".youtube_login.", suffix=".tmp", dir=d)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(c, f)
                os.chmod(tmp, 0o600)
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
    """Read google/youtube cookie rows from a Firefox/Gecko cookies.sqlite. Returns rows of
    (name, value, host, path, expiry, secure). Copies the DB (and -wal/-shm) first so a running
    browser's lock/WAL can't block us."""
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
    """Build Netscape/Mozilla cookies.txt TEXT from cookie rows (for yt-dlp --cookies)."""
    lines = ["# Netscape HTTP Cookie File", ""]
    for name, value, host, cpath, expiry, secure in rows:
        if not host:
            continue
        lines.append("\t".join([host, "TRUE" if host.startswith(".") else "FALSE",
                                 cpath or "/", "TRUE" if secure else "FALSE",
                                 str(int(expiry) if expiry else 0), name, value]))
    return "\n".join(lines) + "\n"


def netscape_cookies():
    """The imported session as Netscape cookies.txt text, or '' when not signed in (for yt-dlp)."""
    return _load_cookies().get("ytdlp", "")


# --------------------------------------------------------------------------- #
# Self-healing InnerTube identity (key + client version), scraped from youtube.com's ytcfg and
# cached. Never blocks the request path: returns cached-or-default at once, refreshes in the bg.
# --------------------------------------------------------------------------- #
_YT_CFG_TTL = 12 * 3600
_yt_cfg_cache = None
_yt_cfg_lock = threading.Lock()
_yt_cfg_warming = False


def _yt_config_path():
    return os.path.join(_data_dir(), "yt_innertube_config.json")


def _yt_cfg_load():
    try:
        with open(_yt_config_path()) as f:
            d = json.load(f)
        return {"key": d.get("key") or _DEFAULT_INNERTUBE_KEY,
                "version": d.get("version") or _DEFAULT_CLIENT_VERSION,
                "ts": float(d.get("ts", 0))}
    except Exception:
        return {"key": _DEFAULT_INNERTUBE_KEY, "version": _DEFAULT_CLIENT_VERSION, "ts": 0}


def _yt_config():
    global _yt_cfg_cache
    cfg = _yt_cfg_cache
    if cfg is None:
        cfg = _yt_cfg_load()
        _yt_cfg_cache = cfg
    if time.time() - cfg.get("ts", 0) > _YT_CFG_TTL:
        _warm_yt_config()
    return cfg


def _fetch_yt_identity(timeout=15):
    """Scrape the current InnerTube key + WEB client version from youtube.com's ytcfg. (key, ver)."""
    _force_ipv4()
    req = urllib.request.Request(_YT_ORIGIN + "/", headers={
        "User-Agent": _BROWSER_UA,
        "Accept-Encoding": "gzip",
        "Cookie": "SOCS=CAI;",          # skip the EU consent interstitial
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = _read_body(resp).decode("utf-8", "replace")
    except Exception as ex:
        _log("identity scrape failed: %s" % ex)
        return (None, None)
    m_key = re.search(r'"INNERTUBE_API_KEY":\s*"([^"]+)"', html)
    m_ver = re.search(r'"INNERTUBE_CLIENT_VERSION":\s*"([^"]+)"', html)
    key = m_key.group(1) if m_key else None
    ver = m_ver.group(1) if m_ver else None
    if ver and not re.match(r"^\d+\.\d{6}", ver):    # sanity: looks like 2.YYYYMMDD.xx.xx
        ver = None
    return (key, ver)


def _warm_yt_config():
    global _yt_cfg_warming
    with _yt_cfg_lock:
        if _yt_cfg_warming:
            return
        _yt_cfg_warming = True

    def _bg():
        global _yt_cfg_cache, _yt_cfg_warming
        try:
            key, ver = _fetch_yt_identity()
            if ver:                     # the version is what drifts; only commit when we got one
                cfg = {"key": key or _DEFAULT_INNERTUBE_KEY, "version": ver, "ts": time.time()}
                try:
                    with open(_yt_config_path(), "w") as f:
                        json.dump(cfg, f)
                except Exception:
                    pass
                _yt_cfg_cache = cfg
                _log("innertube identity refreshed: version=%s" % ver)
        except Exception:
            pass
        finally:
            _yt_cfg_warming = False

    threading.Thread(target=_bg, daemon=True).start()


def innertube_identity():
    """Current InnerTube identity, for diagnostics: {version, source}."""
    cfg = _yt_config()
    return {"version": cfg.get("version", ""),
            "source": "live" if cfg.get("ts", 0) else "default"}


# --------------------------------------------------------------------------- #
# Auth + the chosen account identity.
# --------------------------------------------------------------------------- #

def _sapisidhash(sapisid, origin):
    ts = str(int(time.time()))
    digest = hashlib.sha1(("%s %s %s" % (ts, sapisid, origin)).encode()).hexdigest()
    return "SAPISIDHASH %s_%s" % (ts, digest)


def _cookie_and_sapisid():
    """(Cookie header, SAPISID) for InnerTube. Prefers the stored header; falls back to deriving
    both from the yt-dlp Netscape text (older imports that didn't store them separately)."""
    c = _load_cookies()
    header, sapisid = c.get("cookie", ""), c.get("sapisid", "")
    if header and sapisid:
        return header, sapisid
    vals = {}
    for line in (c.get("ytdlp") or "").split("\n"):
        if line and not line.startswith("#") and "\t" in line:
            cols = line.split("\t")
            if len(cols) == 7:
                vals[cols[5]] = cols[6]
    sapisid = sapisid or vals.get("__Secure-3PAPISID") or vals.get("SAPISID") or ""
    header = header or "; ".join("%s=%s" % (n, vals[n]) for n in _AUTH_COOKIE_NAMES if n in vals)
    return header, sapisid


def is_logged_in():
    return bool(netscape_cookies())


def _selected_identity():
    """The chosen account/channel (or default) as {authuser, page_id, datasync_id, name}."""
    c = _load_cookies()
    return {"authuser": str(c.get("authuser") or "0"),
            "page_id": c.get("page_id") or "",
            "datasync_id": c.get("datasync_id") or "",
            "name": c.get("selected_name") or ""}


def selected_account():
    """The current identity, for QML: {authuser, page_id, name}."""
    i = _selected_identity()
    return {"authuser": i["authuser"], "page_id": i["page_id"], "name": i["name"]}


def select_account(authuser="0", page_id="", datasync_id="", name=""):
    """Persist the identity to act as; applied to every InnerTube call (and to the yt-dlp fallback's
    ?authuser). Returns {ok, name, error?}."""
    with _cookies_lock:
        c = _load_cookies()
        if not c.get("sapisid") and not c.get("ytdlp"):
            return {"ok": False, "error": "Sign in first (Import from browser)."}
        c["authuser"] = str(authuser or "0")
        c["page_id"] = page_id or ""
        c["datasync_id"] = datasync_id or ""
        c["selected_name"] = name or ""
        _save_cookies(c)
    _log("selected account authuser=%s page_id=%s name=%r" % (authuser, page_id, name))
    return {"ok": True, "name": name or ""}


# --------------------------------------------------------------------------- #
# InnerTube call.
# --------------------------------------------------------------------------- #

def _context():
    cfg = _yt_config()
    return {"client": {"clientName": "WEB", "clientVersion": cfg["version"],
                       "hl": "en", "gl": "US"}, "user": {"lockedSafetyMode": False}}


def _innertube(endpoint, body, timeout=30, authuser=None, page_id=None):
    """POST an InnerTube endpoint authed with the imported cookies (SAPISIDHASH) + identity headers.
    authuser/page_id override the stored selection (used to PROBE each login). Raises on network/HTTP
    error; a 400 also kicks an identity re-scrape."""
    cookie, sapisid = _cookie_and_sapisid()
    if not sapisid:
        raise RuntimeError("not signed in")
    _force_ipv4()
    cfg = _yt_config()
    ident = _selected_identity()
    au = ident["authuser"] if authuser is None else str(authuser)
    pid = ident["page_id"] if page_id is None else (page_id or "")
    payload = {"context": _context()}
    payload.update(body or {})
    headers = {
        "User-Agent": _BROWSER_UA,
        "Content-Type": "application/json",
        "Origin": _YT_ORIGIN,
        "Referer": _YT_ORIGIN + "/",
        "Accept-Encoding": "gzip",
        "Cookie": cookie,
        "Authorization": _sapisidhash(sapisid, _YT_ORIGIN),
        "X-Goog-AuthUser": au,
    }
    if pid:
        headers["X-Goog-PageId"] = pid
    url = _INNERTUBE + endpoint + "?key=" + cfg["key"] + "&prettyPrint=false"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(_read_body(resp).decode())
    except urllib.error.HTTPError as he:
        detail = ""
        try:
            detail = _read_body(he).decode()[:200]
        except Exception:
            pass
        _log("innertube %s HTTP %s authuser=%s page_id=%s: %s"
             % (endpoint, he.code, ident["authuser"], ident["page_id"], detail))
        if he.code == 400:
            _warm_yt_config()          # often a stale client version — re-scrape for next time
        raise


# --------------------------------------------------------------------------- #
# Defensive renderer navigation.
# --------------------------------------------------------------------------- #

def _nav(obj, path, default=None):
    cur = obj
    for k in path:
        try:
            cur = cur[k]
        except (KeyError, IndexError, TypeError):
            return default
    return cur


def _iter_find(node, key):
    """Yield every dict stored under `key` anywhere in the tree (YouTube reshuffles containers)."""
    if isinstance(node, dict):
        v = node.get(key)
        if isinstance(v, dict):
            yield v
        for vv in node.values():
            yield from _iter_find(vv, key)
    elif isinstance(node, list):
        for vv in node:
            yield from _iter_find(vv, key)


def _first_continuation(data):
    for c in _iter_find(data, "continuationItemRenderer"):
        tok = _nav(c, ["continuationEndpoint", "continuationCommand", "token"], "")
        if tok:
            return tok
    return ""


def _skeleton(node, depth=0, maxdepth=8):
    """A values-free view of a JSON node — nested keys with leaf TYPES, no actual values. Safe to
    paste from a debug log without redacting names/ids, while still revealing the structure."""
    if depth > maxdepth:
        return "…"
    if isinstance(node, dict):
        return {k: _skeleton(v, depth + 1, maxdepth) for k, v in node.items()}
    if isinstance(node, list):
        return [_skeleton(node[0], depth + 1, maxdepth)] if node else []
    return type(node).__name__


def _dump_shape(label, data):
    """Log an InnerTube response's shape so an unexpected tree can be diagnosed from device logs:
    top-level keys, every distinct *Renderer / account-ish key anywhere, and a raw head. DEBUG only."""
    if not _DEBUG:
        return
    try:
        top = list(data.keys()) if isinstance(data, dict) else type(data).__name__
        keys = set()

        def _walk(n):
            if isinstance(n, dict):
                for k in n:
                    if k.endswith("Renderer") or "ccount" in k or "Item" in k or "ontinuation" in k:
                        keys.add(k)
                for v in n.values():
                    _walk(v)
            elif isinstance(n, list):
                for v in n:
                    _walk(v)

        _walk(data)
        _log("%s top-keys: %s" % (label, top))
        _log("%s renderer-keys: %s" % (label, sorted(keys)))
        _log("%s raw-head: %s" % (label, json.dumps(data)[:2000]))
    except Exception as ex:
        _log("%s dump failed: %s" % (label, ex))


def _runs_text(node):
    return "".join(r.get("text", "") for r in (node.get("runs") or [])) if isinstance(node, dict) else ""


# --------------------------------------------------------------------------- #
# Account switcher — enumerate logins + channels (incl. brand accounts).
# --------------------------------------------------------------------------- #

def _account_sections(node, out):
    """accountItemSectionRenderer nodes, in order — one per Google login (section index=authuser)."""
    if isinstance(node, dict):
        if isinstance(node.get("accountItemSectionRenderer"), dict):
            out.append(node["accountItemSectionRenderer"])
        for v in node.values():
            _account_sections(v, out)
    elif isinstance(node, list):
        for v in node:
            _account_sections(v, out)


def _parse_account_item(item, authuser, sel):
    name = _runs_text(item.get("accountName")) or _nav(item, ["accountName", "simpleText"], "") or ""
    handle = (_nav(item, ["channelHandle", "simpleText"], "")
              or _runs_text(item.get("channelHandle"))
              or _nav(item, ["accountByline", "simpleText"], "")
              or _runs_text(item.get("accountByline")))
    thumbs = _nav(item, ["accountPhoto", "thumbnails"]) or []
    thumb = thumbs[-1].get("url", "") if thumbs and isinstance(thumbs[-1], dict) else ""
    # Identity tokens live under serviceEndpoint.selectActiveIdentityEndpoint.supportedTokens; scan
    # defensively for pageId (brand channel) + datasyncId anywhere under the item, either way.
    page_id, datasync, gaia = "", "", ""
    for tok in _iter_find(item, "pageIdToken"):
        page_id = page_id or tok.get("pageId", "")
    for tok in _iter_find(item, "datasyncIdToken"):
        datasync = datasync or tok.get("datasyncId", "")
    for tok in _iter_find(item, "accountStateToken"):
        gaia = gaia or tok.get("obfuscatedGaiaId", "")
    if not (name or page_id):
        return None
    # Selection is driven solely by OUR stored identity (select_account), NOT the browser's active
    # channel (item.isSelected) — otherwise the browser-active channel and our chosen one both light
    # up. With nothing stored, sel defaults to authuser 0 / no page id, i.e. the default channel.
    selected = str(authuser) == sel["authuser"] and (page_id or "") == sel["page_id"]
    return {"name": name or "Channel", "handle": handle or "", "thumb": thumb or "",
            "authuser": str(authuser), "page_id": page_id or "", "datasync_id": datasync or "",
            "gaia": gaia or "", "selected": selected}


def _account_item_dicts(node):
    """The account entries in a switcher response — keyed "accountItem" (older: "accountItemRenderer")."""
    out = []
    for k in ("accountItem", "accountItemRenderer"):
        out.extend(_iter_find(node, k))
    return out


def list_accounts():
    """Signed-in Google logins + channels (incl. brand accounts) for the account picker:
    {ok, accounts:[{name, handle, thumb, authuser, page_id, datasync_id, selected}], error?}.

    account/accounts_list returns only the ACTIVE account for the authuser it's called with — so we
    PROBE X-Goog-AuthUser=0,1,2,… (each yields that login's account(s), incl. brand channels), stop
    at the first logged-out/empty index, and dedupe. That probe IS the switch mechanism, so anything
    we can list here we can select. Heavily logged (YOUFISH_DEBUG=1)."""
    if not is_logged_in():
        return {"ok": False, "accounts": [], "error": "Not signed in — import your login first."}
    sel = _selected_identity()
    uniq, seen = [], set()
    first_data = None
    for n in range(0, 8):
        try:
            data = _innertube("account/accounts_list", {}, authuser=n, page_id="")
        except Exception as ex:
            _log("accounts_list authuser=%d failed: %s" % (n, ex))
            if n == 0 and not uniq:      # first-probe failure is transient/network, not signed-out
                return {"ok": False, "accounts": [],
                        "error": "Couldn't reach YouTube to list accounts. Check your connection "
                                 "and try again."}
            break
        if first_data is None:
            first_data = data
        logged_out = bool(_nav(data, ["responseContext", "mainAppWebResponseContext", "loggedOut"], False))
        items = _account_item_dicts(data)
        _log("accounts_list authuser=%d: loggedOut=%s items=%d" % (n, logged_out, len(items)))
        if logged_out or not items:
            break                                  # contiguous indices — nothing past here
        added_here = 0
        for it in items:
            a = _parse_account_item(it, n, sel)
            if not a:
                continue
            key = (a["gaia"] or a["datasync_id"] or a["name"], a["page_id"])
            if key in seen:
                continue                           # authuser didn't switch (same account) → dedupe
            seen.add(key)
            uniq.append(a)
            added_here += 1
        if added_here == 0:
            break                                  # only repeats of accounts we already have
    _log("list_accounts: %d identities" % len(uniq))
    if not uniq:
        if _DEBUG and first_data is not None:
            _dump_shape("accounts_list", first_data)
        return {"ok": False, "accounts": [],
                "error": "No accounts found — the imported session looks signed out. Open "
                         "youtube.com in the Sailfish Browser, make sure you're signed in, then "
                         "Re-import."}
    if _DEBUG:
        items0 = _account_item_dicts(first_data)
        if items0:
            _log("list_accounts: accountItem skeleton: %s" % json.dumps(_skeleton(items0[0])))
    return {"ok": True, "accounts": uniq}


# --------------------------------------------------------------------------- #
# Account-scoped data via InnerTube (respects the selected identity).
# --------------------------------------------------------------------------- #

def _channel_thumb(renderer):
    thumbs = _nav(renderer, ["thumbnail", "thumbnails"]) or []
    url = thumbs[-1].get("url", "") if thumbs and isinstance(thumbs[-1], dict) else ""
    return ("https:" + url) if url.startswith("//") else url


def subscriptions_innertube():
    """This account's subscriptions via the FEchannels browse. {ok, channels:[{id,name,url,
    thumbnail}], error?}. Walks continuations (capped). Defensive parse + debug logging."""
    if not is_logged_in():
        return {"ok": False, "channels": [], "error": "not signed in"}
    try:
        data = _innertube("browse", {"browseId": "FEchannels"})
    except Exception as ex:
        return {"ok": False, "channels": [], "error": str(ex)}
    chans, seen = [], set()

    def collect(d):
        for key in ("channelRenderer", "gridChannelRenderer"):
            for r in _iter_find(d, key):
                cid = (r.get("channelId")
                       or _nav(r, ["navigationEndpoint", "browseEndpoint", "browseId"], ""))
                if not cid or not cid.startswith("UC") or cid in seen:
                    continue
                seen.add(cid)
                name = (_nav(r, ["title", "simpleText"], "") or _runs_text(r.get("title"))
                        or _nav(r, ["displayName", "simpleText"], "") or cid)
                chans.append({"id": cid, "name": name,
                              "url": "https://www.youtube.com/channel/" + cid,
                              "thumbnail": _channel_thumb(r)})

    first = data
    collect(data)
    token, pages = _first_continuation(data), 0
    while token and pages < 20:
        pages += 1
        try:
            data = _innertube("browse", {"continuation": token})
        except Exception as ex:
            _log("subscriptions continuation failed: %s" % ex)
            break
        before = len(chans)
        collect(data)
        token = _first_continuation(data)
        if len(chans) == before and not token:
            break
    _log("subscriptions_innertube: %d channels over %d continuation(s)" % (len(chans), pages))
    if not chans:
        _dump_shape("FEchannels", first)
    return {"ok": True, "channels": chans}


def playlists_innertube():
    """This account's saved/created playlists via the FEplaylist_aggregation browse.
    {ok, playlists:[{yt_id, title}], error?}. Defensive parse across old + lockup layouts."""
    if not is_logged_in():
        return {"ok": False, "playlists": [], "error": "not signed in"}
    try:
        data = _innertube("browse", {"browseId": "FEplaylist_aggregation"})
    except Exception as ex:
        return {"ok": False, "playlists": [], "error": str(ex)}
    out, seen = [], set()

    def add(pid, title):
        pid = (pid or "").strip()
        if not pid or pid.startswith("UC") or pid in seen:   # UC = a channel row, not a playlist
            return
        seen.add(pid)
        out.append({"yt_id": pid, "title": (title or "Playlist").strip()[:100] or "Playlist"})

    def collect(d):
        for key in ("gridPlaylistRenderer", "playlistRenderer"):
            for r in _iter_find(d, key):
                add(r.get("playlistId"),
                    _nav(r, ["title", "simpleText"], "") or _runs_text(r.get("title")))
        for r in _iter_find(d, "lockupViewModel"):           # newer layout
            title = _nav(r, ["metadata", "lockupMetadataViewModel", "title", "content"], "")
            add(r.get("contentId"), title)

    first = data
    collect(data)
    token, pages = _first_continuation(data), 0
    while token and pages < 20:
        pages += 1
        try:
            data = _innertube("browse", {"continuation": token})
        except Exception as ex:
            _log("playlists continuation failed: %s" % ex)
            break
        before = len(out)
        collect(data)
        token = _first_continuation(data)
        if len(out) == before and not token:
            break
    _log("playlists_innertube: %d playlists over %d continuation(s)" % (len(out), pages))
    if not out:
        _dump_shape("FEplaylist_aggregation", first)
    return {"ok": True, "playlists": out}


# --------------------------------------------------------------------------- #
# Import / status / logout.
# --------------------------------------------------------------------------- #

def import_browser_login():
    """Import the signed-in YouTube session from the Sailfish Browser's cookie jar into the 0600
    store: the Netscape text (yt-dlp), the InnerTube Cookie header + SAPISID, and a reset identity
    (back to the default account). Returns {ok, count, source?, error?}."""
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
    jar = {name: value for (name, value, host, cpath, expiry, secure) in rows}
    names = set(jar)
    if not (names & set(_SESSION_MARKERS)):
        return {"ok": False, "count": 0,
                "error": "Found browser cookies, but not a signed-in Google session. Sign in at "
                         "youtube.com in the Sailfish Browser first."}
    sapisid = jar.get("__Secure-3PAPISID") or jar.get("SAPISID") or ""
    cookie_header = "; ".join("%s=%s" % (n, jar[n]) for n in _AUTH_COOKIE_NAMES if n in jar)
    _save_cookies({"ytdlp": _netscape_from_rows(rows), "cookie": cookie_header, "sapisid": sapisid,
                   "source": used, "count": len(rows), "imported_at": int(time.time()),
                   "authuser": "0", "page_id": "", "datasync_id": "", "selected_name": ""})
    _log("imported %d cookie rows from %s (sapisid=%s)" % (len(rows), used, "yes" if sapisid else "no"))
    return {"ok": True, "count": len(rows), "source": used}


def login_status():
    """For the QML account UI: {logged_in, count, imported_at, source}."""
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
