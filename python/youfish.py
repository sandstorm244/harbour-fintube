"""Youfish backend: thin wrapper over an external yt-dlp binary + a local media proxy.

The app never pins a yt-dlp version — it shells out to whatever yt-dlp is on the
device, and the user updates that binary themselves. Every call is made from
PyOtherSide's worker thread, so blocking subprocess calls are fine here.

Playback note: googlevideo rejects GStreamer's default `souphttpsrc` User-Agent
with HTTP 403, and QtMultimedia's MediaPlayer can't set request headers. So the
prototype player streams through a tiny localhost proxy (below) that refetches the
real URL with a browser User-Agent and forwards byte ranges. This reuses
QtMultimedia's rendering; the raw dual-track GStreamer player (M1, for 720p) will
set headers itself and won't need the proxy.

IMPORTANT (2026 reality): yt-dlp increasingly needs a Proof-of-Origin (PO) token
to return real formats — without one you get "no video format available". The PO
token is minted by a bgutil provider on a bundled Deno/Node runtime; wiring that
sidecar is milestone M2. For now yt-dlp's android_vr client resolves without one.
"""

import atexit
import calendar
import ctypes
import hashlib
import html
import http.server
import json
import os
import re
import shutil
import signal
import socket
import socketserver
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

# Flags applied to every network-facing yt-dlp call. -4 forces IPv4: dual-stack connects
# can hang when a network advertises IPv6 routes it can't actually carry.
# (This is where PO-token / player-client args will accrue in M2.)
_COMMON_ARGS = ("-4",)

# Playback uses SEPARATE video-only + audio-only tracks fed to a raw dual-source GStreamer
# pipeline (YouTube killed the old muxed itag 22 in mid-2024). We select video by its PROPERTIES
# (the codec / height / fps yt-dlp reports on every format), NOT a hardcoded itag list — itags are
# undocumented and YouTube keeps rotating/adding them, so any fixed list silently misses variants
# (e.g. it's how the 1080p30 pair 137/248 got dropped while only the 1080p60 pair was listed).
# Selecting by property covers every fps/resolution automatically.
#
# Codec rules (target hardware):
#  - AV1 (av01): excluded everywhere — no AV1 decoder at all.
#  - H.264 (avc1): software-decodes smoothly → preferred when hw decode is OFF.
#  - VP9 (vp9/vp09): ~25-30% leaner and hardware-decoded when droidvdec works → preferred when
#    hw decode is ON (software fallback otherwise).
#  - Nothing above 1080p: 1440p/2160p are VP9/AV1-only and won't decode smoothly on this hardware.
_MAX_VIDEO_HEIGHT = 1080
# Single muxed URL — the only thing QtMultimedia can play directly. Prefer HLS (95/94/93,
# served by web/ios clients) then progressive itag 18 (360p H.264+AAC, universally present).
_MUXED_ITAGS = ("95", "94", "93", "18")


def _codec_family(vcodec):
    """'h264' | 'vp9' | '' for a yt-dlp vcodec string. '' = a codec we don't play (av01 / none)."""
    vc = (vcodec or "").lower()
    if vc.startswith(("avc", "h264")):
        return "h264"
    if vc.startswith(("vp9", "vp09")):
        return "vp9"
    return ""


def _video_candidates(formats):
    """Playable video-only tracks (H.264/VP9, ≤1080p, with a direct URL), best-first. Ordered by
    the active codec preference (VP9-first when hw decode is on, else H.264-first), then resolution
    high→low, then framerate low→high (lighter to decode). Drives both the default pick and the
    quality menu — property-based, so every itag variant is covered with no hardcoded list."""
    prefer_vp9 = bool(get_settings().get("hw_decode"))
    cands = []
    for f in formats:
        if not f.get("url"):
            continue
        if (f.get("acodec") or "none").lower() != "none":
            continue                                   # video-only tracks only
        if not _codec_family(f.get("vcodec")):
            continue                                   # av01 / unknown → skip
        h = f.get("height") or 0
        if h <= 0 or h > _MAX_VIDEO_HEIGHT:
            continue
        cands.append(f)

    def key(f):
        fam = _codec_family(f.get("vcodec"))
        codec_rank = 0 if fam == ("vp9" if prefer_vp9 else "h264") else 1
        return (-(f.get("height") or 0), codec_rank, f.get("fps") or 0)
    cands.sort(key=key)
    return cands


def _audio_family(acodec):
    """'opus' | 'aac' | '' for a yt-dlp acodec string. '' = a codec we don't use (none / exotic)."""
    ac = (acodec or "").lower()
    if ac.startswith("opus"):
        return "opus"
    if ac.startswith(("mp4a", "aac")):
        return "aac"
    return ""


def _audio_orig_pref(f):
    """How much YouTube/yt-dlp prefers this track's LANGUAGE: >0 = original/default source audio,
    <0 = dubbed / descriptive. Uses yt-dlp's own `language_preference` (≈10 original / -1 dub /
    -10 descriptive) when present, else the format_note wording. Without this a same-bitrate DUB
    can outrank the source track (English video → Portuguese dub)."""
    lp = f.get("language_preference")
    if isinstance(lp, (int, float)):
        return lp
    note = (f.get("format_note") or "").lower()
    if "descriptive" in note or "description" in note:
        return -10
    if "original" in note or "default" in note:
        return 10
    return 0


def _audio_candidates(formats):
    """Playable audio-only tracks (opus/AAC, with a direct URL), best-first. Ordered by bitrate
    high→low (opus preferred at a tie — better quality per bit; original/default language over
    dubs). Bitrate order naturally interleaves the codecs, so the music player's SABR-fallback
    ladder tries the best of each codec early. Property-based — mirrors _video_candidates, so no
    hardcoded itag list to go stale."""
    cands = []
    for f in formats:
        if not f.get("url"):
            continue
        if (f.get("vcodec") or "none").lower() != "none":
            continue                                   # audio-only tracks only
        if not _audio_family(f.get("acodec")):
            continue                                   # exotic / none → skip
        cands.append(f)

    def key(f):
        codec_rank = 0 if _audio_family(f.get("acodec")) == "opus" else 1
        abr = f.get("abr") or f.get("tbr") or 0
        # LANGUAGE is the primary key so the SOURCE track always beats a dub regardless of its
        # bitrate; then highest bitrate, then codec preference.
        return (-_audio_orig_pref(f), -abr, codec_rank)
    cands.sort(key=key)
    return cands


# --------------------------------------------------------------------------- #
# Local media proxy: injects a browser User-Agent and forwards byte ranges.
# --------------------------------------------------------------------------- #

_proxy_port = None
_proxy_lock = threading.Lock()
_CHUNK = 1 << 20  # fetch googlevideo in 1 MiB bounded ranges; open-ended requests are flaky
_ipv4_forced = False


def _force_ipv4():
    """Make this process's socket lookups return IPv4 addresses only.

    googlevideo publishes AAAA records, but when a network advertises IPv6 it can't route,
    each connect stalls before falling back to IPv4 — longer than souphttpsrc's read timeout,
    so the pipeline errors out ("Socket I/O timed out") before the proxy, stuck in the same
    stall, can answer. This is the in-process equivalent of the `-4` flag passed to yt-dlp.
    yt-dlp runs in a separate
    process, and QtMultimedia's networking lives in the C++/Qt side, so patching
    getaddrinfo here only affects the proxy's own urllib fetches.
    """
    global _ipv4_forced
    if _ipv4_forced:
        return
    _orig = socket.getaddrinfo

    def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        return _orig(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_only
    _ipv4_forced = True


# Proxy tracing is off unless YOUFISH_DEBUG is set in the environment, so no /tmp log file is
# written in normal use. Trace playback with `YOUFISH_DEBUG=1 harbour-youfish`.
_DEBUG = bool(os.environ.get("YOUFISH_DEBUG"))


def _plog(msg):
    if not _DEBUG:
        return
    try:
        with open("/tmp/youfish-proxy.log", "a") as fh:
            fh.write(msg + "\n")
    except Exception:
        pass


def _tlog(msg):
    """Timing trace to stdout (visible under YOUFISH_DEBUG, like ytm's [ytm] lines) — for
    profiling start latency. Cheap; compiled out in normal use by the _DEBUG gate."""
    if _DEBUG:
        try:
            print("[youfish/t] " + msg)
        except Exception:
            pass


def _clen(url):
    """Total content length of a googlevideo stream, read straight from its URL.

    With query-param range requests the response is a 200 whose Content-Length is only
    the chunk size, so the URL's own clen= is how we learn the real total.
    """
    m = re.search(r"[?&]clen=(\d+)", url)
    return int(m.group(1)) if m else None


# The proxy exists only to refetch YouTube DASH/progressive media (googlevideo) with the right UA.
# Constrain it to https + Google hosts so it can't be turned into an open forward-proxy: reaching
# localhost services, file:// reads, or arbitrary hosts (SSRF) via a crafted u= parameter.
_PROXY_ALLOW_SUFFIXES = (".googlevideo.com", ".youtube.com", ".ytimg.com",
                         ".googleusercontent.com", ".google.com")


def _proxy_url_ok(url):
    try:
        p = urllib.parse.urlsplit(url)
    except Exception:
        return False
    if p.scheme != "https":
        return False
    host = (p.hostname or "").lower()
    return any(host == s.lstrip(".") or host.endswith(s) for s in _PROXY_ALLOW_SUFFIXES)


class _MediaProxyHandler(http.server.BaseHTTPRequestHandler):
    # libsoup (souphttpsrc) sends HTTP/1.1 requests; answer in kind. The body is
    # close-delimited (Connection: close, no Content-Length), which is valid 1.1 and
    # the framing souphttpsrc consumes most reliably.
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # keep the app log quiet

    def _fetch(self, url, start, end, ua):
        # Fetch [start,end] via googlevideo's DASH range QUERY parameter, NOT a Range
        # header. A Range header gets an initial grace then 403s sustained pulling
        # (~a minute in); the range= query param — what the web player and yt-dlp use —
        # streams the whole file. `range` isn't signature-covered, so appending it to a
        # signed URL is fine. Total length comes from clen= in the URL (see _clen).
        # `ua` is the format's OWN User-Agent (android client URLs are bound to it — the
        # proxy's old hardcoded Chrome UA got 403s once we moved off the SABR web client).
        sep = "&" if "?" in url else "?"
        ranged = "%s%srange=%d-%d" % (url, sep, start, end)
        req = urllib.request.Request(ranged, headers={"User-Agent": ua})
        return urllib.request.urlopen(req, timeout=30)

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        target = q.get("u", [None])[0]
        video_id = q.get("v", [None])[0]
        itag = q.get("itag", [None])[0]
        ua = q.get("ua", [None])[0] or _BROWSER_UA  # the format's client UA (see _fetch)
        if not target:
            self.send_error(400, "missing target")
            return
        if not _proxy_url_ok(target):
            self.send_error(403, "blocked target")   # not an https Google/googlevideo host
            return
        raw_range = self.headers.get("Range", "")
        start = 0
        m = re.match(r"bytes=(\d+)-", raw_range)
        if m:
            start = int(m.group(1))
        _plog("REQ itag=%s range=%s" % (itag, raw_range or "none"))
        written = 0
        url = [target]  # mutable holder: swapped for a fresh URL on a mid-stream 403

        def fetch(s, e):
            """Fetch bytes s..e, transparently refreshing the URL once on a 403."""
            try:
                return self._fetch(url[0], s, e, ua)
            except urllib.error.HTTPError as ex:
                if ex.code == 403 and video_id and itag:
                    fresh = _reresolve(video_id, itag, url[0])
                    if fresh and _proxy_url_ok(fresh):   # yt-dlp should return googlevideo; verify
                        _plog("refresh itag=%s at byte %d (403)" % (itag, s))
                        url[0] = fresh
                        return self._fetch(fresh, s, e, ua)
                raise

        try:
            total = _clen(target)  # googlevideo's full length, straight from the URL
            first = fetch(start, start + _CHUNK - 1)
            if total is None:  # non-googlevideo fallback: derive it from the response
                cr = re.search(r"/(\d+)\s*$", first.headers.get("Content-Range", ""))
                if cr:
                    total = int(cr.group(1))
            ctype = first.headers.get("Content-Type", "application/octet-stream")
            _plog("GET start=%d first_status=%s total=%s ctype=%s"
                  % (start, first.status, total, ctype))

            # Close-delimited framing: no Content-Length, Connection: close, stream the
            # whole thing then drop the socket so libsoup reads to EOF. This is the one
            # transfer mode souphttpsrc accepts unconditionally; Content-Length responses
            # (1.0 and 1.1, 200 and 206) all got rejected with wrote=0.
            # Send a real Content-Length (and 206/Content-Range when the client asked for
            # a range) so souphttpsrc knows the stream size and reports it seekable — that
            # is what lets the demuxer honour scrubbing. Body is still terminated by
            # Connection: close, the framing we know souphttpsrc accepts. Content-Length is
            # exact: we stream precisely total-start bytes below.
            if total is not None and raw_range:
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, total - 1, total))
                self.send_header("Content-Length", str(total - start))
            elif total is not None:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(total - start))
            else:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            pos = start
            data = first.read()
            first.close()
            self.wfile.write(data)
            written += len(data)
            pos += len(data)
            while total is not None and pos < total:
                end = min(pos + _CHUNK, total) - 1
                nxt = fetch(pos, end)
                data = nxt.read()
                nxt.close()
                if not data:
                    break
                self.wfile.write(data)
                written += len(data)
                pos += len(data)
            self.wfile.flush()
            _plog("DONE start=%d wrote=%d" % (start, written))
        except (BrokenPipeError, ConnectionResetError):
            # player closed the connection (seek/stop) — normal. wrote=0 here means
            # souphttpsrc rejected our response outright, which is the bug to watch for.
            _plog("CLIENT-CLOSED start=%d wrote=%d" % (start, written))
        except Exception as ex:
            _plog("proxy error start=%d wrote=%d: %r" % (start, written, ex))
            self.close_connection = True


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def _ensure_proxy():
    """Start the localhost proxy once; return its port."""
    global _proxy_port
    with _proxy_lock:
        if _proxy_port:
            return _proxy_port
        _force_ipv4()  # else every upstream fetch stalls ~30s on dead IPv6
        if _DEBUG:
            try:
                open("/tmp/youfish-proxy.log", "w").close()  # fresh log each app run
            except Exception:
                pass
        server = _ThreadingHTTPServer(("127.0.0.1", 0), _MediaProxyHandler)
        _proxy_port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return _proxy_port


def _proxied(url, video_id="", itag="", ua=""):
    """Wrap a stream URL so playback goes through the header-injecting proxy.

    video_id + itag ride along so the proxy can re-resolve a fresh URL if this one
    starts 403ing mid-stream (googlevideo throttles sustained streaming access). `ua`
    is the format's own User-Agent, forwarded so the proxy fetches googlevideo with the
    exact UA yt-dlp used (android-client URLs 403 under a mismatched UA).
    """
    if not url:
        return ""
    port = _ensure_proxy()
    q = "http://127.0.0.1:%d/play?u=%s" % (port, urllib.parse.quote(url, safe=""))
    if video_id and itag:
        q += "&v=%s&itag=%s" % (urllib.parse.quote(str(video_id), safe=""),
                                urllib.parse.quote(str(itag), safe=""))
    if ua:
        q += "&ua=" + urllib.parse.quote(ua, safe="")
    return q


# --- Fresh-URL refresh on mid-stream 403 -------------------------------------- #
# A googlevideo stream URL stops honouring sustained access after ~a minute and
# starts returning 403 partway through (throttle/session limit, not expiry). yt-dlp
# copes by re-extracting; the proxy does the same — on a 403 it re-resolves the
# video, swaps in the fresh URL for the same itag, and resumes at the identical byte
# offset (same itag => same encoding => byte-identical stream).
_url_cache = {}          # video_id -> {"ts": epoch, "fmts": {itag: fresh_url}}
_url_cache_lock = threading.Lock()
_URL_CACHE_TTL = 3600    # an entry only coordinates one playback; googlevideo URLs outlive it
_URL_CACHE_MAX = 64      # bound it so a long session can't stack refreshes without limit
# Rate-limit yt-dlp spawns triggered by the proxy's 403-refresh path, so a local caller can't
# hammer it with novel video_ids to force an unbounded stream of forks. Legit playback re-resolves
# rarely (only on a mid-stream 403), so a small burst is plenty. Guarded by _url_cache_lock.
_reresolve_spawns = []   # recent spawn timestamps
_RERESOLVE_WINDOW = 60.0
_RERESOLVE_BURST = 8


def _ytdlp_formats(video_id):
    """Run yt-dlp and return {itag: direct_url} for every format that has a URL."""
    path = _ytdlp_path()
    if not path or not video_id:
        return {}
    url = video_id if "://" in video_id else "https://www.youtube.com/watch?v=" + video_id
    _ensure_pot_server()  # a fresh URL is just as PO-gated; keep the token sidecar warm
    try:
        proc = subprocess.run([path, *_COMMON_ARGS, *_pot_ytdlp_args(), *_yt_extractor_args(),
                               "--dump-single-json", "--", url],
                              capture_output=True, text=True, timeout=90)
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout)
        return {f.get("format_id"): f.get("url")
                for f in data.get("formats", []) if f.get("format_id") and f.get("url")}
    except Exception:
        return {}


def _reresolve(video_id, itag, failed_url):
    """Fresh direct URL for (video_id, itag), re-running yt-dlp at most once per stale
    generation. Concurrent video+audio 403s share one refresh: whoever takes the lock
    first re-extracts; the other sees a cached URL that differs from its failed one and
    reuses it without a second yt-dlp run.
    """
    with _url_cache_lock:
        ent = _url_cache.get(video_id)
        if ent and time.time() - ent["ts"] < _URL_CACHE_TTL:
            cached = ent["fmts"].get(itag)
            if cached and cached != failed_url:
                return cached  # another track already refreshed this generation
        now = time.time()
        _reresolve_spawns[:] = [t for t in _reresolve_spawns if now - t < _RERESOLVE_WINDOW]
        if len(_reresolve_spawns) >= _RERESOLVE_BURST:
            _plog("reresolve rate-limited (%d in %.0fs)" % (len(_reresolve_spawns), _RERESOLVE_WINDOW))
            return None
        _reresolve_spawns.append(now)
        fresh = _ytdlp_formats(video_id)
        if not fresh:
            return None
        _url_cache[video_id] = {"ts": time.time(), "fmts": fresh}
        if len(_url_cache) > _URL_CACHE_MAX:  # evict oldest beyond the cap
            for k, _ in sorted(_url_cache.items(),
                               key=lambda kv: kv[1]["ts"])[:len(_url_cache) - _URL_CACHE_MAX]:
                _url_cache.pop(k, None)
        return fresh.get(itag)


# --------------------------------------------------------------------------- #
# yt-dlp wrappers
# --------------------------------------------------------------------------- #

def _managed_ytdlp():
    """The app-managed yt-dlp, living under our own data dir. This is the only spot that is
    both writable and reachable from inside the Sailjail sandbox — the user's ~/.local/bin
    and a trimmed PATH are masked from the jail — so it's checked first."""
    return os.path.join(_data_dir(), "bin", "yt-dlp")


def _system_binary(name):
    """A user/system copy of `name` to fall back on when the app has no managed copy of its own. A
    GUI-launched SFOS app runs with a TRIMMED PATH (no ~/.local/bin), so we consult PATH via `which`
    AND the standard user-local / system spots explicitly — the same approach as _DENO_CANDIDATES.
    Lets a user who keeps their own yt-dlp/ffmpeg (e.g. in ~/.local/bin, shared with other apps) skip
    a second app-managed copy. A managed copy still WINS when present, so Install/Update always put the
    app back in control of exactly what it runs. Returns an executable path, or None."""
    found = shutil.which(name)
    if found and os.access(found, os.X_OK):
        return found
    for p in (os.path.expanduser("~/.local/bin/" + name),
              "/usr/local/bin/" + name, "/usr/bin/" + name):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _ytdlp_path():
    """yt-dlp for the app to run. Prefers the app-managed copy in our own bin/ (so Install/Update stay
    in control of what runs); otherwise falls back to a user/system yt-dlp (PATH, ~/.local/bin,
    /usr/local/bin, /usr/bin — see _system_binary) so a user who keeps their own copy needn't have the
    app fetch a second one. Missing entirely → the UI prompts a download."""
    _ensure_deno_on_path()  # yt-dlp's bundled EJS challenge-solver needs Deno reachable on PATH
    managed = _managed_ytdlp()
    if os.path.isfile(managed) and os.access(managed, os.X_OK):
        return managed
    return _system_binary("yt-dlp")


def ytdlp_version():
    """Installed yt-dlp version string, or '' if missing/broken."""
    path = _ytdlp_path()
    if not path:
        return ""
    try:
        out = subprocess.run([path, "--version"], capture_output=True,
                             text=True, timeout=15)
        return out.stdout.strip()
    except Exception:
        return ""


def ytdlp_update():
    """Run yt-dlp's own self-updater and report the result, on the settings-chosen channel.

    This works for the standalone binary the user installed (it downloads the latest
    release from GitHub and replaces itself in place); a pip/package install refuses
    and says so, which we surface verbatim. Extraction is the part YouTube keeps
    breaking, so this is the app's main maintenance lever — no youfish rebuild needed.
    """
    path = _ytdlp_path()
    if not path:
        return {"ok": False, "error": "yt-dlp not found", "version": ""}
    channel = "nightly" if (get_settings().get("ytdlp_channel") == "nightly") else "stable"
    try:
        # --update-to <channel>@latest is unambiguous whichever channel the binary is on now;
        # it can pull ~30 MB over a phone link, so allow generous time.
        proc = subprocess.run([path, *_COMMON_ARGS, "--update-to", channel + "@latest"],
                              capture_output=True, text=True, timeout=300)
        out = (proc.stdout + proc.stderr).strip()
        return {"ok": proc.returncode == 0,
                "output": (out[-400:] if out else "yt-dlp reported nothing"),
                "version": ytdlp_version(), "channel": channel}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "update timed out", "version": ytdlp_version()}
    except Exception as ex:
        return {"ok": False, "error": str(ex), "version": ytdlp_version()}


# Standalone aarch64 build (self-contained — bundles its own Python, so it doesn't depend on
# the device's Python version). "latest" redirects to the current release asset; each release
# also publishes SHA2-256SUMS, which we verify the download against.
_YTDLP_ASSET = "yt-dlp_linux_aarch64"
_YTDLP_RELEASE_BASE = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
_YTDLP_DOWNLOAD_URL = _YTDLP_RELEASE_BASE + _YTDLP_ASSET
_YTDLP_SUMS_URL = _YTDLP_RELEASE_BASE + "SHA2-256SUMS"


def _https_open(url, ctx, timeout=60):
    """Open a URL, refusing anything that isn't HTTPS end-to-end (initial URL and, after
    GitHub's redirect to its asset host, the final URL too). Cert verification is on via ctx."""
    if not url.lower().startswith("https://"):
        raise ValueError("refusing non-HTTPS URL: " + url)
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
    resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    if not resp.geturl().lower().startswith("https://"):
        resp.close()
        raise ValueError("download redirected to a non-HTTPS URL")
    return resp


def _expected_sha256(ctx):
    """The published SHA-256 for our asset, from the release's SHA2-256SUMS file (or None)."""
    with _https_open(_YTDLP_SUMS_URL, ctx, timeout=30) as resp:
        text = resp.read().decode("utf-8", "replace")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == _YTDLP_ASSET:
            return parts[0].strip().lower()
    return None


def install_ytdlp():
    """Download yt-dlp into our data dir (the one place that's writable AND visible inside the
    Sailjail sandbox). HTTPS-only, checksum-verified against the release's SHA2-256SUMS. Runs
    in the background; progress + result go to QML via pyotherside."""
    import pyotherside

    def run():
        tmp = None
        try:
            _force_ipv4()  # pin IPv4 — avoid a stalled connect on unroutable-IPv6 networks
            ctx = ssl.create_default_context()  # verifies the server certificate
            expected = _expected_sha256(ctx)    # None if the sums file can't be parsed
            dest_dir = os.path.join(_data_dir(), "bin")
            os.makedirs(dest_dir, exist_ok=True)
            dest = _managed_ytdlp()
            tmp = dest + ".part"
            h = hashlib.sha256()
            with _https_open(_YTDLP_DOWNLOAD_URL, ctx) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                last = -1
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if total > 0:
                            pct = done * 100.0 / total
                            if int(pct) != last:
                                last = int(pct)
                                pyotherside.send("ytdlp_install_progress", pct)
            if expected and h.hexdigest().lower() != expected:
                os.remove(tmp)
                pyotherside.send("ytdlp_install_done", False,
                                 "Checksum mismatch — download discarded, nothing installed", "")
                return
            os.chmod(tmp, 0o755)
            os.replace(tmp, dest)
            ver = ytdlp_version()  # exercises the binary — confirms it actually runs
            if ver:
                note = "Installed yt-dlp " + ver
                if not expected:
                    note += " (checksum unavailable, not verified)"
                pyotherside.send("ytdlp_install_done", True, note, ver)
            else:
                pyotherside.send("ytdlp_install_done", False,
                                 "Downloaded + checksum OK, but the binary won't run here — "
                                 "the sandbox is likely blocking exec from the data dir", "")
        except Exception as ex:
            try:
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            pyotherside.send("ytdlp_install_done", False, str(ex), "")

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# ffmpeg: optional, app-managed. yt-dlp needs it to MERGE separate HD video+audio
# tracks into one file; without it, video downloads fall back to muxed 360p (itag
# 22/18). Bundled the same way as yt-dlp — a static aarch64 build unpacked into our
# own bin/ — and handed to yt-dlp via --ffmpeg-location.
# --------------------------------------------------------------------------- #

def _managed_ffmpeg():
    return os.path.join(_data_dir(), "bin", "ffmpeg")


def _ffmpeg_path():
    """ffmpeg for the app (HD download merging). Prefers the app-managed copy in our bin/; otherwise
    falls back to a user/system ffmpeg (PATH, ~/.local/bin, /usr/local/bin, /usr/bin — see
    _system_binary) so a user's own copy is reused instead of fetching a second. None → the UI offers
    a Download/Update button."""
    managed = _managed_ffmpeg()
    if os.path.isfile(managed) and os.access(managed, os.X_OK):
        return managed
    return _system_binary("ffmpeg")


def _ffmpeg_dir():
    """Directory holding a usable ffmpeg, for yt-dlp's --ffmpeg-location (or None)."""
    p = _ffmpeg_path()
    return os.path.dirname(p) if p else None


def _ffmpeg_args():
    d = _ffmpeg_dir()
    return ["--ffmpeg-location", d] if d else []


def ffmpeg_version():
    """Installed ffmpeg version string, or '' if missing/broken."""
    path = _ffmpeg_path()
    if not path:
        return ""
    try:
        out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=15)
        first = (out.stdout or "").splitlines()[0] if out.stdout else ""
        m = re.search(r"ffmpeg version (\S+)", first)
        return m.group(1) if m else (first[:40] if first else "")
    except Exception:
        return ""


# Static aarch64 build (self-contained; John Van Sickle's release is the de-facto arm64 source).
# It's a .tar.xz carrying ffmpeg + ffprobe under a versioned dir; a companion .md5 lets us verify
# the archive before unpacking. (MD5 is weak, but the transfer is HTTPS + cert-verified.)
_FFMPEG_URL = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz"
_FFMPEG_MD5_URL = _FFMPEG_URL + ".md5"

# A trusted SHA-256 of the extracted ffmpeg BINARY, pinned out-of-band. The .md5 companion above is
# served by the same host, so it only guards against transfer corruption — an attacker who serves a
# tampered archive serves a matching .md5. When THIS pin is set it's the AUTHORITATIVE integrity
# check on the actual executable we run (a host/supply-chain compromise can't forge it). Empty =
# fall back to the corruption-only MD5. NOTE: this is the hash of the `ffmpeg` binary itself
# (sha256sum ~/.local/share/<app>/bin/ffmpeg), so upgrading ffmpeg means re-pinning. Set to a
# known-good build; a download that doesn't match is treated as a newer build, not rejected.
_FFMPEG_SHA256 = "6bb182d0d75d23028db82e9e4f723ca69b853d055698486e6984ddb2c06fb8ce"


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _expected_ffmpeg_md5(ctx):
    with _https_open(_FFMPEG_MD5_URL, ctx, timeout=30) as resp:
        text = resp.read().decode("utf-8", "replace")
    parts = text.split()
    return parts[0].strip().lower() if parts else None


def install_ffmpeg():
    """Download the static ffmpeg archive, verify its MD5, and unpack ffmpeg+ffprobe into our
    bin/ (beside yt-dlp). HTTPS-only. Background thread; progress/result to QML via events."""
    import pyotherside
    import tarfile

    def run():
        tmp = None
        try:
            _force_ipv4()  # pin IPv4 — avoid a stalled connect on unroutable-IPv6 networks
            ctx = ssl.create_default_context()
            expected = _expected_ffmpeg_md5(ctx)
            dest_dir = os.path.join(_data_dir(), "bin")
            os.makedirs(dest_dir, exist_ok=True)
            tmp = os.path.join(dest_dir, "ffmpeg-dl.tar.xz.part")
            h = hashlib.md5()
            with _https_open(_FFMPEG_URL, ctx) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                last = -1
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if total > 0:
                            pct = done * 100.0 / total
                            if int(pct) != last:
                                last = int(pct)
                                pyotherside.send("ffmpeg_install_progress", pct)
            # The .md5 (same host) catches transfer corruption only; the authoritative check is the
            # pinned SHA-256 of the extracted binary, done after unpacking below.
            if expected and h.hexdigest().lower() != expected:
                os.remove(tmp)
                pyotherside.send("ffmpeg_install_done", False,
                                 "Checksum mismatch — download discarded, nothing installed", "")
                return
            if not _FFMPEG_SHA256 and expected and h.hexdigest().lower() != expected:
                os.remove(tmp)
                pyotherside.send("ffmpeg_install_done", False,
                                 "Checksum mismatch — download discarded, nothing installed", "")
                return
            # Unpack just the two binaries, flattened into bin/. Write only the basename to our
            # own directory, so a malicious path in the archive can't escape it.
            got = []
            with tarfile.open(tmp, "r:xz") as tf:
                for m in tf.getmembers():
                    base = os.path.basename(m.name)
                    if m.isfile() and base in ("ffmpeg", "ffprobe"):
                        src = tf.extractfile(m)
                        if src is None:
                            continue
                        outp = os.path.join(dest_dir, base)
                        with open(outp, "wb") as out:
                            shutil.copyfileobj(src, out)
                        os.chmod(outp, 0o755)
                        got.append(base)
            os.remove(tmp)
            tmp = None
            if "ffmpeg" not in got:
                pyotherside.send("ffmpeg_install_done", False,
                                 "Archive didn't contain an ffmpeg binary", "")
                return
            # Integrity: the archive was HTTPS + companion-MD5 verified above (transfer integrity).
            # The pinned SHA-256 of the extracted binary is a KNOWN-GOOD marker, not a gate: when it
            # matches we say so; when it doesn't, this is simply a newer JVS build (the release URL
            # always points at the latest, and we can't pin a hash we've never seen), so we still
            # install it. That's what keeps "download the latest ffmpeg" working without a rebuild.
            pinned_ok = False
            if _FFMPEG_SHA256:
                ffbin = os.path.join(dest_dir, "ffmpeg")
                got_sha = _sha256_file(ffbin) if os.path.exists(ffbin) else ""
                pinned_ok = (got_sha == _FFMPEG_SHA256.strip().lower())
            ver = ffmpeg_version()  # exercises the binary — confirms it actually runs
            if ver:
                note = "Installed ffmpeg " + ver
                if pinned_ok:
                    note += " (SHA-256 verified — pinned build)"
                elif _FFMPEG_SHA256:
                    note += " (newer build — transfer verified, not pinned)"
                elif not expected:
                    note += " (checksum unavailable, not verified)"
                pyotherside.send("ffmpeg_install_done", True, note, ver)
            else:
                pyotherside.send("ffmpeg_install_done", False,
                                 "Unpacked, but the binary won't run here", "")
        except Exception as ex:
            try:
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            pyotherside.send("ffmpeg_install_done", False, str(ex), "")

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def update_ffmpeg():
    """Fetch + install the latest static ffmpeg (the release URL always points at the current
    build; this overwrites the existing binary). Same flow + events as install_ffmpeg()."""
    return install_ffmpeg()


# --------------------------------------------------------------------------- #
# PO-token provider (bgutil): an OPT-IN, user-installed sidecar.
#
# YouTube now binds a Proof-of-Origin token to each video id, so a token can't be
# pasted once and reused — it must be minted per video. The bgutil provider does this:
# a small Deno HTTP server keeps a BotGuard VM warm and mints a fresh token on demand,
# and a pure-Python yt-dlp plugin auto-calls it. We clone + set it up on request (like
# yt-dlp itself), never bundle it, and run the server under Deno's default-deny sandbox:
# network + env only, reads jailed to its own folder, and NO write / run / blanket-ffi —
# the capabilities an npm supply-chain worm would need. See install_pot_provider().
# --------------------------------------------------------------------------- #
_POT_REPO = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git"
_POT_TAG = "1.3.2"          # pinned KNOWN-GOOD release (matches the bundled yt-dlp bgutil plugin).
                            # The EFFECTIVE tag (see _pot_effective_tag) can be updated to the
                            # latest release from inside the app, with no rebuild.
_POT_PORT = 4416            # bgutil's default HTTP port; the plugin probes 127.0.0.1:4416

_DENO_CANDIDATES = (
    os.path.expanduser("~/.deno/bin/deno"),  # default deno install location
    os.path.expanduser("~/.local/bin/deno"),  # common user-local spot (a launcher's PATH omits it)
    "/usr/local/bin/deno",
    "/usr/bin/deno",
)

# Deno ships as a single self-contained binary (aarch64/glibc) from its GitHub releases, so — like
# yt-dlp — the app can fetch it into its own bin/ instead of needing a manual system install.
_DENO_ASSET = "deno-aarch64-unknown-linux-gnu.zip"
_DENO_DOWNLOAD_URL = "https://github.com/denoland/deno/releases/latest/download/" + _DENO_ASSET
_DENO_SUMS_URL = _DENO_DOWNLOAD_URL + ".sha256sum"


def _managed_deno():
    return os.path.join(_data_dir(), "bin", "deno")


_pot_proc = None
_pot_lock = threading.Lock()
_pot_last_error = ""     # human-readable reason the sidecar last failed to start/answer (diagnostics)
_pot_log_rotated = False  # server.log is rotated once per app launch (see _pot_rotate_log)


def _deno_path():
    """Deno binary, or None. Prefers the app-managed copy (install_deno) in our own bin/; then a
    launcher's trimmed PATH; then ~/.deno/bin + ~/.local/bin."""
    managed = _managed_deno()
    if os.path.isfile(managed) and os.access(managed, os.X_OK):
        return managed
    found = shutil.which("deno")
    if found:
        return found
    for p in _DENO_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


_deno_ejs_logged = False


def _ensure_deno_on_path():
    """Put the JS runtime on PATH for yt-dlp's child processes. yt-dlp solves YouTube's signature /
    `n` challenges by running its bundled yt-dlp-ejs scripts through a JS runtime (Deno); a launcher's
    trimmed PATH hides our managed/user Deno, so prepend its folder. Idempotent; a no-op when there's
    no Deno (yt-dlp still falls back to its built-in Python interpreter while that path survives)."""
    global _deno_ejs_logged
    deno = _deno_path()
    if not deno:
        return
    d = os.path.dirname(deno)
    if d and d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    if _DEBUG and not _deno_ejs_logged:   # confirm the EJS runtime wiring once (YOUFISH_DEBUG)
        _deno_ejs_logged = True
        print("[youfish] EJS: yt-dlp will use Deno at " + deno)


def _git_path():
    found = shutil.which("git")
    if found:
        return found
    for p in ("/usr/bin/git", "/usr/local/bin/git", os.path.expanduser("~/.local/bin/git")):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def deno_version():
    """Installed Deno version string, or '' if missing/broken."""
    path = _deno_path()
    if not path:
        return ""
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15)
        first = (out.stdout or "").splitlines()[0] if out.stdout else ""
        m = re.search(r"deno (\S+)", first)
        return m.group(1) if m else (first[:40] if first else "")
    except Exception:
        return ""


def install_deno():
    """Download Deno (the PO-token provider's runtime) into our bin/ — a single self-contained
    binary, fetched + verified like yt-dlp, so the provider needs no manual runtime install.
    Background thread; progress + result via pyotherside (deno_install_progress / deno_install_done)."""
    import pyotherside
    import zipfile

    def run():
        tmp = None
        try:
            _force_ipv4()
            ctx = ssl.create_default_context()
            expected = None
            try:   # verify against the release's per-asset .sha256sum when present; else HTTPS-only
                with _https_open(_DENO_SUMS_URL, ctx, timeout=30) as resp:
                    parts = resp.read().decode("utf-8", "replace").split()
                    expected = parts[0].strip().lower() if parts else None
            except Exception:
                expected = None
            dest_dir = os.path.join(_data_dir(), "bin")
            os.makedirs(dest_dir, exist_ok=True)
            tmp = os.path.join(dest_dir, "deno-dl.zip.part")
            h = hashlib.sha256()
            with _https_open(_DENO_DOWNLOAD_URL, ctx) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                last = -1
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if total > 0:
                            pct = done * 100.0 / total
                            if int(pct) != last:
                                last = int(pct)
                                pyotherside.send("deno_install_progress", pct)
            if expected and h.hexdigest().lower() != expected:
                os.remove(tmp)
                pyotherside.send("deno_install_done", False,
                                 "Checksum mismatch — download discarded, nothing installed", "")
                return
            # The archive holds a single `deno` binary; extract just that (by basename) into bin/.
            dest = _managed_deno()
            got = False
            with zipfile.ZipFile(tmp) as zf:
                for name in zf.namelist():
                    if os.path.basename(name) == "deno" and not name.endswith("/"):
                        with zf.open(name) as src, open(dest, "wb") as out:
                            shutil.copyfileobj(src, out)
                        os.chmod(dest, 0o755)
                        got = True
                        break
            os.remove(tmp)
            tmp = None
            if not got:
                pyotherside.send("deno_install_done", False,
                                 "Archive didn't contain a deno binary", "")
                return
            ver = deno_version()   # exercises the binary — confirms it actually runs
            if ver:
                note = "Installed Deno " + ver
                if not expected:
                    note += " (checksum unavailable, not verified)"
                pyotherside.send("deno_install_done", True, note, ver)
            else:
                pyotherside.send("deno_install_done", False,
                                 "Downloaded, but the binary won't run here", "")
        except Exception as ex:
            try:
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            pyotherside.send("deno_install_done", False, str(ex), "")

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def _pot_dir():
    return os.path.join(_data_dir(), "potprovider")


def _pot_repo_dir():
    return os.path.join(_pot_dir(), "bgutil-ytdlp-pot-provider")


def _pot_server_dir():
    return os.path.join(_pot_repo_dir(), "server")


def _pot_plugin_dir():
    # Passed to yt-dlp via --plugin-dirs; must be the dir that CONTAINS yt_dlp_plugins/.
    return os.path.join(_pot_repo_dir(), "plugin")


def _pot_marker():
    return os.path.join(_pot_dir(), ".installed")


def _pot_installed():
    return (os.path.isfile(_pot_marker())
            and os.path.isfile(os.path.join(_pot_server_dir(), "src", "main.ts")))


def _pot_active():
    """Installed AND enabled — the gate for both the sidecar and the yt-dlp plugin args."""
    return _pot_installed() and bool(get_settings().get("pot_provider", False))


def _canvas_node_path():
    """Absolute path to node-canvas's native addon, if it was ever built (only when the
    tight, no-native-code setup turned out to need it). Empty otherwise."""
    import glob
    hits = glob.glob(os.path.join(_pot_server_dir(), "node_modules", "**", "canvas.node"),
                     recursive=True)
    return hits[0] if hits else ""


def _pot_server_flags():
    """Deno argv for the token server — least privilege.

    Denied outright: write, run (subprocess), and blanket ffi — the powers a compromised
    npm dependency would need to steal files, plant a backdoor, or run native code. Reads
    are jailed to the server's own tree (jsdom loads a bundled stylesheet + resolves
    node_modules from there); network + env are all it legitimately needs. jsdom degrades
    gracefully without node-canvas, so no native addon is built or loaded. (If some future
    build genuinely needs canvas, ffi is granted to that ONE .node file — never wholesale.)
    """
    flags = [
        _deno_path(), "run",
        # --allow-net is UNRESTRICTED on purpose. We tried scoping it to the loopback listen + a
        # fixed list of Google/BotGuard hosts, but the token generator's network targets shift as
        # YouTube reworks BotGuard (new attestation hosts, redirects to the current challenge page,
        # plus Node-compat sockets that bind 0.0.0.0:0 locally before connecting out). Every miss
        # made Deno kill the server mid-request with NotCapable — the connection just closes with
        # no response — so NO PO token was ever produced and every video hit the "confirm you're
        # not a bot" wall. The list was unmaintainable against YouTube's changes. Exfiltration
        # defence now rests on the powers that actually matter and stay locked below: the server
        # still can't WRITE files, RUN processes, or load native code (FFI), and can only READ its
        # own tree — so a compromised npm dep can't steal files, persist, or execute anything.
        # Broad outbound network is the acceptable price of a token generator that keeps working.
        "--allow-net",
        "--allow-env",                        # server reads PORT / token-TTL (+ open-ended) from env
        "--allow-read=" + _pot_server_dir(),  # jsdom CSS + node_modules, confined to our dir
        "--deny-write",
        "--deny-run",
        "--v8-flags=--max-old-space-size=8192",  # BotGuard VM warmup peaks above the 2 GB default
    ]
    canvas = _canvas_node_path() if get_settings().get("pot_needs_ffi") else ""
    flags.append(("--allow-ffi=" + canvas) if canvas else "--deny-ffi")
    flags.append(os.path.join(_pot_server_dir(), "src", "main.ts"))
    return flags


def _pot_ytdlp_args():
    """--plugin-dirs pointing at the bgutil yt-dlp plugin when the provider is active; else
    []. Keeps yt-dlp behaving exactly as before whenever the provider isn't set up/enabled."""
    return ["--plugin-dirs", _pot_plugin_dir()] if _pot_active() else []


def _pot_bind_localhost():
    """Patch the cloned server to bind 127.0.0.1 instead of all interfaces.

    Upstream main.ts hardcodes host "::" (fallback "0.0.0.0") with no env/flag — its own
    comment says a localhost default is planned 'in the next major version', so we make that
    change early. Best-effort + idempotent: if the source shape ever changes, the replace is a
    no-op and the server just keeps binding all interfaces (the low-severity status quo). Deno
    runs the .ts directly, so the rewrite takes effect on the next server start."""
    main_ts = os.path.join(_pot_server_dir(), "src", "main.ts")
    try:
        with open(main_ts) as f:
            src = f.read()
        patched = (src.replace('host: "::"', 'host: "127.0.0.1"')
                      .replace('host: "0.0.0.0"', 'host: "127.0.0.1"'))
        if patched != src:
            with open(main_ts, "w") as f:
                f.write(patched)
    except Exception:
        pass


def _pot_disable_webgpu():
    """Neutralize Deno's WebGPU in the cloned server before BotGuard can touch it.

    Deno exposes navigator.gpu, but on the libhybris/Mali GL stack the native
    GPU.requestAdapter() SEGFAULTS the whole process (YouTube's newer webpage-challenge flow
    fingerprints the GPU; a headless x86 server just gets a null adapter and moves on). We prepend
    a one-liner to src/main.ts that makes requestAdapter() return null — the normal 'no WebGPU'
    result — so BotGuard falls back to the software fingerprint instead of crashing. Idempotent
    (marker-guarded) + best-effort: if the entry file ever moves, it's a no-op and the server runs
    as it does today. Uses defineProperty so it also wins if the method is non-writable."""
    main_ts = os.path.join(_pot_server_dir(), "src", "main.ts")
    marker = "/* youfish:no-webgpu */"
    shim = (marker + ' try{if(globalThis.GPU&&globalThis.GPU.prototype)'
            'Object.defineProperty(globalThis.GPU.prototype,"requestAdapter",'
            '{value:async()=>null,configurable:true});}catch(_e){}\n')
    try:
        with open(main_ts) as f:
            src = f.read()
        if marker in src:
            return
        with open(main_ts, "w") as f:
            f.write(shim + src)
    except Exception:
        pass


def _pot_ready_on_port(timeout=0.25):
    try:
        with socket.create_connection(("127.0.0.1", _POT_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _pot_http_ping(timeout=1.5):
    """Confirm the token server is actually ANSWERING HTTP, not just holding the port open — a
    wedged Deno process can keep the socket bound while replying to nothing, which a bare TCP
    connect (_pot_ready_on_port) can't tell apart from healthy. Hits the bgutil server's /ping
    route; any HTTP reply (even an error status) means it's alive and processing. Returns
    {ok, version} — version comes from /ping's JSON when present, else ''."""
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/ping" % _POT_PORT,
                                     headers={"User-Agent": "youfish"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(8192)
        try:
            ver = str(json.loads(body.decode("utf-8", "replace")).get("version") or "")
        except Exception:
            ver = ""
        return {"ok": True, "version": ver}
    except urllib.error.HTTPError:
        return {"ok": True, "version": ""}   # server answered with an HTTP error → it IS alive
    except Exception:
        return {"ok": False, "version": ""}


def _pot_server_log_tail(n=30):
    """Last n non-empty lines of the provider server's log (potprovider/server.log), or '' if
    there's none. This is where a Deno crash / NotCapable / OOM prints its reason."""
    try:
        with open(os.path.join(_pot_dir(), "server.log"), "r", errors="replace") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        return "\n".join(lines[-max(1, int(n)):])
    except Exception:
        return ""


def _set_pdeathsig():
    """Ask the kernel to SIGKILL the Deno child if FinTube dies, so the sidecar can never be
    left orphaned (Linux PR_SET_PDEATHSIG = 1). Best-effort; runs in the forked child."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGKILL)
    except Exception:
        pass


def _ensure_pot_server():
    """Start the token server if the provider is active and it isn't already listening.
    Returns True once something is listening on the port. No-op (returns False) when the
    provider isn't installed/enabled, so normal calls are entirely unaffected."""
    if not _pot_active():
        return False
    if _pot_ready_on_port():
        return True
    global _pot_proc, _pot_last_error
    with _pot_lock:
        if _pot_ready_on_port():
            return True
        if not _deno_path():
            _pot_last_error = "Deno runtime not found — install it from Providers → Download Deno."
            return False
        if not (_pot_proc and _pot_proc.poll() is None):
            if _pot_proc is not None:   # a tracked child died — record its exit code so the log tail
                try:                    # (and diagnostics) show WHY, e.g. -11 SIGSEGV / -9 SIGKILL(OOM)
                    with open(os.path.join(_pot_dir(), "server.log"), "a") as _lf:
                        _lf.write("[youfish] previous provider server exited (code %s)\n"
                                  % _pot_proc.poll())
                except Exception:
                    pass
            _pot_bind_localhost()   # ensure a fresh spawn binds 127.0.0.1, not all interfaces
            _pot_disable_webgpu()   # stub WebGPU — its native requestAdapter segfaults on Mali/libhybris
            env = dict(os.environ)
            env["PORT"] = str(_POT_PORT)
            try:
                logf = open(os.path.join(_pot_dir(), "server.log"), "ab", buffering=0)
            except Exception:
                logf = subprocess.DEVNULL
            try:
                _pot_proc = subprocess.Popen(
                    _pot_server_flags(), cwd=_pot_server_dir(), env=env,
                    stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                    preexec_fn=_set_pdeathsig)
            except Exception as ex:
                _pot_last_error = "Couldn't launch the Deno server: " + str(ex)
                return False
            atexit.register(stop_pot_server)
        # The server LISTENS quickly; the BotGuard VM warms on the first token request,
        # which the yt-dlp plugin waits out itself — so we only wait for the port to open.
        deadline = time.time() + 25
        while time.time() < deadline:
            if _pot_ready_on_port():
                _pot_last_error = ""
                return True
            if _pot_proc.poll() is not None:
                _pot_last_error = ("Provider server exited (code %s) just after starting — see the "
                                   "server log in the diagnostics below." % _pot_proc.poll())
                return False   # died during startup — see potprovider/server.log
            time.sleep(0.3)
        _pot_last_error = "Provider server didn't open port %d within 25s." % _POT_PORT
        return _pot_ready_on_port()


def _pot_rotate_log():
    """Start each app launch with a fresh server.log so it doesn't accumulate stale 'Started POT
    server' lines across runs (the log is otherwise append-only and never trimmed). Keeps exactly
    ONE previous log as server.log.prev, so the last session is still inspectable. Idempotent per
    process (guarded) and best-effort. os.replace is atomic; any process still holding the old fd
    keeps writing to the renamed inode, so this is safe even if a server were mid-write."""
    global _pot_log_rotated
    if _pot_log_rotated:
        return
    _pot_log_rotated = True
    try:
        log = os.path.join(_pot_dir(), "server.log")
        if os.path.isfile(log):
            os.replace(log, log + ".prev")   # overwrites an older .prev
    except Exception:
        pass


def prewarm():
    """Start the PO-token server in the background at app launch, so the first resolve doesn't pay
    the ~2s Deno startup on its critical path. No-op unless the provider is installed + enabled.
    Runs on its OWN daemon thread so the PyOtherSide worker (and the UI behind it) never blocks on
    the port wait — fire-and-forget from QML at startup."""
    _pot_rotate_log()   # fresh server.log per launch (keeps the previous one as server.log.prev)
    if not _pot_active():
        return
    def _bg():
        # PR_SET_PDEATHSIG on the Deno child is armed against the THREAD that spawns it — on Linux
        # the parent-death signal is tied to the creating task, not the process. If this short-lived
        # thread returned right after the server came up, the kernel would SIGKILL the child the
        # instant the thread ends. THAT is why the sidecar only stayed up when a played video or the
        # diagnostics started it (both run on the long-lived PyOtherSide worker thread) and silently
        # died when prewarm started it at launch. So: skip if it's already listening, otherwise start
        # it and PARK on the child for its whole life here — keeping pdeathsig correctly armed to fire
        # when this daemon thread is torn down at app exit.
        try:
            if _pot_ready_on_port():
                return
            _ensure_pot_server()
            proc = _pot_proc
            if proc is not None and proc.poll() is None:
                proc.wait()
        except Exception:
            pass
    threading.Thread(target=_bg, daemon=True).start()


def stop_pot_server():
    """Terminate the token sidecar (called on app exit + when the user disables it)."""
    global _pot_proc
    p, _pot_proc = _pot_proc, None
    if not p:
        return
    try:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
    except Exception:
        pass


def pot_status():
    """Provider state for the Providers UI. `running` = the port is open; `responding` = the
    server actually answers HTTP (the real 'it's working' signal — a wedged process can hold the
    port without replying). `last_error` carries the reason it isn't working, when there is one."""
    deno = _deno_path()
    running = _pot_ready_on_port()
    ping = _pot_http_ping() if running else {"ok": False, "version": ""}
    return {
        "installed": _pot_installed(),
        "enabled": bool(get_settings().get("pot_provider", False)),
        "deno": bool(deno),
        "deno_path": deno or "",
        "running": running,
        "responding": bool(ping["ok"]),
        "server_version": ping["version"],
        "tag": _pot_effective_tag(),
        "default_tag": _POT_TAG,
        "updated": bool((get_settings().get("pot_tag") or "").strip()),
        "last_error": _pot_last_error,
    }


def pot_diagnostics():
    """A copy-pasteable health report for the PO-token provider and everything it depends on —
    for the Providers 'Run diagnostics' action, so a stuck user (or someone helping them) can see
    at a glance which piece is missing. Reports the resolved binaries + versions, the provider's
    install/enable state, whether the Deno server is alive and answering, and the tail of its log.
    Returns {report: <multiline text>, ...structured flags}."""
    installed = _pot_installed()
    enabled = bool(get_settings().get("pot_provider", False))
    # Snapshot the tracked child BEFORE any restart below, so a prior crash's exit code isn't masked
    # by a fresh spawn. poll() is None while alive, an int once exited (0 clean; negative = signal).
    prev = _pot_proc
    prev_code = prev.poll() if prev is not None else None
    # Actively TRY to (re)start when enabled + installed but nothing is listening — so "Run diagnostics"
    # reflects a real start ATTEMPT (and populates _pot_last_error when the server can't come up at all),
    # instead of a passive snapshot that can't tell "won't start" from "not tried".
    restart_tried = False
    restart_ok = None
    if enabled and installed and not _pot_ready_on_port():
        restart_tried = True
        restart_ok = _ensure_pot_server()

    deno = _deno_path()
    git = _git_path()
    ytdlp = _ytdlp_path()
    ffmpeg = _ffmpeg_path()
    running = _pot_ready_on_port()
    ping = _pot_http_ping() if running else {"ok": False, "version": ""}

    L = []
    L.append("FinTube / FinTune — PO-token provider diagnostics")
    L.append("app data dir: " + _data_dir())
    L.append("")
    L.append("Deno   : " + ((deno + "  (v" + (deno_version() or "?") + ")") if deno else "NOT FOUND"))
    L.append("git    : " + (git or "NOT FOUND"))
    L.append("yt-dlp : " + ((ytdlp + "  (" + (ytdlp_version() or "?") + ")") if ytdlp else "NOT FOUND"))
    L.append("ffmpeg : " + ((ffmpeg + "  (" + (ffmpeg_version() or "?") + ")") if ffmpeg
                            else "not installed (HD merge unavailable)"))
    L.append("")
    L.append("provider installed : " + (("yes (" + _pot_effective_tag() + ")") if installed else "no"))
    L.append("provider enabled   : " + ("yes" if enabled else "no"))
    # Distinguish a server that STARTED-THEN-DIED (with its exit code) from one never started this
    # session — the key clue, since the sidecar can open its port fine and only crash later on the
    # first token mint (BotGuard warmup), which leaves the status "on" but nothing listening.
    if prev is not None and prev_code is None:
        L.append("server process     : alive (started by this app)")
    elif prev is not None:
        L.append("server process     : STARTED, then EXITED (code %s) — it opened its port, then the "
                 "process ended (negative = fatal signal: -11 SIGSEGV, -9 SIGKILL/OOM); see the log "
                 "below" % prev_code)
    else:
        L.append("server process     : not started in this app session")
    if restart_tried:
        L.append("restart attempt    : " + ("server came up" if restart_ok
                                             else "FAILED — " + (_pot_last_error or "unknown reason")))
    L.append("port %d listening  : %s" % (_POT_PORT, "yes" if running else "no"))
    L.append("answering HTTP     : " + ("yes" + (" (server v" + ping["version"] + ")"
                                                 if ping["version"] else "")
                                        if ping["ok"] else "no"))
    verdict = ("working" if (enabled and ping["ok"])
               else "NOT working" if enabled else "installed but switched off" if installed
               else "not set up")
    L.append("verdict            : " + verdict)
    if _pot_last_error:
        L.append("last error         : " + _pot_last_error)
    L.append("")
    L.append("note: /ping only proves the HTTP server answers; the real proof is a token mint — look "
             "for 'Generating POT' / 'poToken:' in the log below, which means it's genuinely working.")
    tail = _pot_server_log_tail(30)
    if tail:
        L.append("")
        L.append("--- server.log (last lines) ---")
        L.append(tail)

    return {
        "report": "\n".join(L),
        "deno": bool(deno), "git": bool(git), "ytdlp": bool(ytdlp), "ffmpeg": bool(ffmpeg),
        "installed": installed, "enabled": enabled,
        "running": running, "responding": bool(ping["ok"]),
        "prev_exit": prev_code,
        "last_error": _pot_last_error,
    }


def set_pot_enabled(on):
    """Turn the provider on/off (keeps the install either way) and start/stop the sidecar."""
    set_setting("pot_provider", bool(on))
    if on:
        _ensure_pot_server()
    else:
        stop_pot_server()
    return pot_status()


def _pot_effective_tag():
    """The provider release to install: a stored override if the user updated it, else the pinned
    known-good default. This is what makes the version no longer a hardcoded dead-end."""
    t = (get_settings().get("pot_tag") or "").strip()
    return t or _POT_TAG


def _pot_latest_tag():
    """Latest provider release tag from GitHub ('' on any failure). Used only by the explicit
    update action — never auto-applied, so the Deno sidecar can't silently drift out of step with
    the installed yt-dlp bgutil plugin (the two speak a versioned protocol)."""
    try:
        _force_ipv4()
        ctx = ssl.create_default_context()
        url = ("https://api.github.com/repos/Brainicism/"
               "bgutil-ytdlp-pot-provider/releases/latest")
        req = urllib.request.Request(url, headers={
            "User-Agent": _BROWSER_UA, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            return (json.loads(resp.read().decode()).get("tag_name") or "").strip()
    except Exception:
        return ""


def install_pot_provider(tag=None):
    """Clone + set up the bgutil PO-token provider (opt-in). Background thread; progress and
    the final result go to QML via pyotherside, mirroring install_ytdlp().

    Deps are installed WITHOUT --allow-scripts, so npm lifecycle scripts never run during
    setup (node-canvas's native binary is skipped — jsdom degrades gracefully without it,
    which is what lets the server run with ffi fully denied)."""
    import pyotherside

    def run():
        global _pot_last_error
        the_tag = tag or _pot_effective_tag()
        try:
            deno = _deno_path()
            if not deno:
                _pot_last_error = "Deno runtime not found — install it from Providers → Download Deno."
                pyotherside.send("pot_install_done", False,
                                 "Deno runtime not found. Tap Download Deno, then retry.")
                return
            git = _git_path()
            if not git:
                _pot_last_error = "git not found on device (needed to clone the provider)."
                pyotherside.send("pot_install_done", False, "git not found on device.")
                return
            os.makedirs(_pot_dir(), exist_ok=True)
            repo = _pot_repo_dir()
            if os.path.isdir(repo):
                shutil.rmtree(repo, ignore_errors=True)   # clean (re)install
            pyotherside.send("pot_install_progress", "Cloning provider (" + the_tag + ")…")
            cp = subprocess.run(
                [git, "clone", "--depth", "1", "--branch", the_tag, "--single-branch",
                 _POT_REPO, repo],
                capture_output=True, text=True, timeout=240)
            if cp.returncode != 0:
                _pot_last_error = "Clone failed: " + (cp.stderr.strip()[-200:] or "git error")
                pyotherside.send("pot_install_done", False,
                                 "Clone failed: " + (cp.stderr.strip()[-200:] or "git error"))
                return
            pyotherside.send("pot_install_progress", "Installing dependencies (Deno)…")
            server = _pot_server_dir()
            lock = os.path.join(server, "deno.lock")
            base = [deno, "install"]
            if get_settings().get("pot_needs_ffi"):
                base.append("--allow-scripts")   # only if node-canvas's native build is needed
            cmd = base + (["--frozen"] if os.path.isfile(lock) else [])
            dp = subprocess.run(cmd, cwd=server, capture_output=True, text=True, timeout=900)
            if dp.returncode != 0 and "--frozen" in cmd:   # lock mismatch? retry unlocked
                dp = subprocess.run(base, cwd=server, capture_output=True, text=True, timeout=900)
            if dp.returncode != 0:
                _pot_last_error = "Dependency install failed: " + (dp.stderr.strip()[-200:] or "deno error")
                pyotherside.send("pot_install_done", False,
                                 "Dependency install failed: " + (dp.stderr.strip()[-200:] or "deno error"))
                return
            if not os.path.isfile(os.path.join(server, "src", "main.ts")):
                pyotherside.send("pot_install_done", False,
                                 "Setup finished but the server entry is missing.")
                return
            with open(_pot_marker(), "w") as f:
                f.write(the_tag)
            set_setting("pot_provider", True)    # installed → enabled
            _ensure_pot_server()                 # warm it now so the first video is instant
            pyotherside.send("pot_install_done", True,
                             "Provider ready (" + the_tag + "). Videos now fetch a per-video token.")
        except subprocess.TimeoutExpired:
            pyotherside.send("pot_install_done", False, "Setup timed out.")
        except Exception as ex:
            pyotherside.send("pot_install_done", False, str(ex))

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def update_pot_provider():
    """Resolve the latest provider release and (re)install it, remembering it as the chosen tag.
    User-initiated, like ytdlp_update() — the sidecar only moves on an explicit request, so it
    stays in step with the yt-dlp bgutil plugin. Reports via the same pot_install_* events."""
    latest = _pot_latest_tag()
    if not latest:
        import pyotherside
        pyotherside.send("pot_install_done", False,
                         "Couldn't reach GitHub to find the latest provider release.")
        return {"ok": False}
    set_setting("pot_tag", latest)
    return install_pot_provider(latest)


# YouTube search filter tokens (the results-page `sp` query param — base64 of the filter
# protobuf, percent-encoded). ytsearch: is video-only, so channel search instead hits the
# results URL with the channel filter applied.
_SEARCH_SP = {
    "channel": "EgIQAg%3D%3D",
    "playlist": "EgIQAw%3D%3D",
}


def parse_youtube_url(url):
    """Classify an incoming YouTube link → {kind, id, url}. kind ∈ video|channel|playlist|"".

    Order matters: a watch URL can carry both v= and list= (a video inside a playlist); we
    open the video, so v=/youtu.be/shorts are matched before a bare list=.
    """
    if not url:
        return {"kind": "", "id": "", "url": ""}
    u = url.strip()
    m = re.search(r"youtu\.be/([\w-]{11})", u)
    if not m:
        m = re.search(r"[?&]v=([\w-]{11})", u)
    if not m:
        m = re.search(r"/(?:shorts|embed|live|v)/([\w-]{11})", u)
    if m:
        vid = m.group(1)
        return {"kind": "video", "id": vid, "url": "https://www.youtube.com/watch?v=" + vid}
    m = re.search(r"[?&]list=([\w-]+)", u)
    if m:
        return {"kind": "playlist", "id": m.group(1),
                "url": "https://www.youtube.com/playlist?list=" + m.group(1)}
    m = re.search(r"/channel/(UC[\w-]+)", u)
    if m:
        return {"kind": "channel", "id": m.group(1),
                "url": "https://www.youtube.com/channel/" + m.group(1)}
    m = re.search(r"youtube\.com/((?:@|c/|user/)[\w.\-]+)", u)
    if m:
        return {"kind": "channel", "id": "", "url": "https://www.youtube.com/" + m.group(1)}
    return {"kind": "", "id": "", "url": u}


def search(query, n=15, kind="video", start=1):
    """One page of search results. `start` is the 1-based index of the first result wanted, so
    the UI can page in more as it scrolls (mirrors channel_videos). Members-only videos are
    dropped (they can't be played without the membership); Shorts too when hide_shorts is on."""
    path = _ytdlp_path()
    if not path:
        return {"ok": False, "error": "yt-dlp not found"}
    if kind not in ("video", "channel"):
        kind = "video"
    try:
        start = max(1, int(start))
        n = max(1, int(n))
        end = start + n - 1
        if kind == "channel":
            target = ("https://www.youtube.com/results?search_query=%s&sp=%s"
                      % (urllib.parse.quote(query), _SEARCH_SP["channel"]))
        else:
            # ytsearch<end> fetches enough hits to cover the wanted window; --playlist-items
            # then returns just [start:end], so paging deeper is a bigger fetch sliced tighter.
            target = "ytsearch%d:%s" % (end, query)
        proc = subprocess.run(
            [path, *_COMMON_ARGS, "--flat-playlist",
             "--playlist-items", "%d:%d" % (start, end),
             "--dump-single-json", "--", target],
            capture_output=True, text=True, timeout=90)
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr.strip()[:300] or "search failed")}
        data = json.loads(proc.stdout)
        raw = [e for e in data.get("entries", []) if e and (e.get("id") or e.get("url"))]
        has_more = len(raw) >= n          # a full page back → assume another page exists
        filtered_members = 0
        entries = raw
        if kind == "video":
            kept = [e for e in entries if not _is_members_only(e)]
            filtered_members = len(entries) - len(kept)
            entries = kept
            if _hide_shorts():
                entries = [e for e in entries if not _is_short(e)]
        build = _channel_entry if kind == "channel" else _video_entry
        items = [build(e) for e in entries]
        # Videos always have an id; channels can navigate by URL, so keep either.
        items = [it for it in items if it.get("id") or it.get("url")]
        return {"ok": True, "items": items, "kind": kind, "has_more": has_more,
                "filtered_members": filtered_members}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


def search_suggestions(query):
    """YouTube search autocomplete via Google's public suggest endpoint — a cheap HTTP
    call, no yt-dlp. Returns ["term", ...]."""
    q = (query or "").strip()
    if not q:
        return {"ok": True, "suggestions": []}
    _force_ipv4()
    url = ("https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&q=%s"
           % urllib.parse.quote(q))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        sugg = data[1] if isinstance(data, list) and len(data) > 1 else []
        return {"ok": True, "suggestions": [s for s in sugg if isinstance(s, str)][:10]}
    except Exception:
        return {"ok": False, "suggestions": []}


def resolve(video_id):
    """Resolve a video to playable stream URLs.

    `muxed_url` (single stream, routed through the local proxy) feeds the prototype
    player; the raw `video_url` + `audio_url` pair is for the dual-source pipeline.
    """
    path = _ytdlp_path()
    if not path:
        return {"ok": False, "error": "yt-dlp not found"}
    url = video_id
    if "://" not in url:
        url = "https://www.youtube.com/watch?v=" + video_id
    _t0 = time.time()
    _ensure_pot_server()  # bring the PO-token sidecar up (no-op unless installed+enabled)
    _tlog("pot_ensure %.2fs" % (time.time() - _t0))
    def _dump(extra):
        """Run yt-dlp --dump-single-json with extra args; return (data, error)."""
        _td = time.time()
        proc = subprocess.run(
            [path, *_COMMON_ARGS, *_pot_ytdlp_args(), *extra, "--dump-single-json", "--", url],
            capture_output=True, text=True, timeout=90)
        _tlog("dump %.2fs rc=%d" % (time.time() - _td, proc.returncode))
        if proc.returncode != 0:
            return None, (proc.stderr.strip()[:300] or "resolve failed")
        try:
            return json.loads(proc.stdout), ""
        except Exception as ex:
            return None, str(ex)

    def _hd_pair(d):
        fs = d.get("formats", [])
        return bool(_pick_video(fs) and _pick_audio(fs))

    def _playable(d):
        fs = d.get("formats", [])
        return bool(_pick(fs, _MUXED_ITAGS) or _hd_pair(d))

    try:
        data, err = _dump(_yt_extractor_args())
        # A hard failure (data is None) is usually YouTube's "confirm you're not a bot" check
        # tripping this client — retry once with the wider set. tv/android_vr use different
        # attestation and often pass where web/web_embedded get bot-checked.
        if data is None:
            data2, err2 = _dump(_yt_extractor_args(client_override=_RETRY_CLIENTS))
            if data2 is not None:
                data = data2
            else:
                err = err or err2
        # The primary (web_embedded) usually returns the full fetchable ladder. If SABR
        # degraded it to muxed-only (no HD dual-source pair), widen the client net once to
        # hunt for a fetchable HD pair elsewhere — only switch if the result is actually
        # better (HD found, or the primary had nothing playable at all).
        elif not _hd_pair(data):
            data2, _ = _dump(_yt_extractor_args(client_override=_RETRY_CLIENTS))
            if data2 is not None and _hd_pair(data2):
                data = data2
            elif data2 is not None and not _playable(data) and _playable(data2):
                data = data2
        if data is None:
            return {"ok": False, "error": err}
        formats = data.get("formats", [])
        try:
            cap = int(get_settings().get("default_quality") or 0)
        except (TypeError, ValueError):
            cap = 0
        video = _pick_video(formats, cap)   # capped by the user's Default-quality setting
        audio = _pick_audio(formats)
        muxed = _pick(formats, _MUXED_ITAGS)
        if not muxed and not (video and audio):
            return {"ok": False,
                    "error": "No playable format — try a different Player client in "
                             "Settings (some videos need a PO token)."}
        # The proxy must fetch googlevideo with the SAME User-Agent yt-dlp used for these
        # formats — android-client URLs 403 under a mismatched UA. All picked formats come
        # from one client, so a single UA covers them.
        http_ua = ((video or audio or muxed or {}).get("http_headers") or {}).get(
            "User-Agent", "") or _BROWSER_UA
        # HLS (m3u8) plays fine directly, and proxying the manifest breaks segment
        # resolution; only progressive URLs (itag 18) need the UA-injecting proxy.
        muxed_url = ""
        if muxed:
            if "m3u8" in muxed.get("protocol", ""):
                muxed_url = muxed["url"]
            else:
                muxed_url = _proxied(muxed["url"], video_id, muxed.get("format_id"), http_ua)
        # Quality menu: one entry per resolution, highest first. _video_candidates is already
        # sorted (resolution high→low, then preferred codec, then lower fps), so deduping by
        # height keeps the FIRST track at each resolution — the preferred codec (H.264 in software
        # mode, VP9 in hardware mode) at its lower framerate — matching how it actually decodes.
        qualities = []
        seen_heights = set()
        for qf in _video_candidates(formats):
            qh = qf.get("height") or 0
            if qh in seen_heights:
                continue
            seen_heights.add(qh)
            qualities.append({
                "itag": str(qf.get("format_id") or ""),
                "label": "%dp" % qh,
                "video_url": _proxied(qf["url"], video_id, qf.get("format_id"), http_ua),
            })
        chapters = [{"start": c.get("start_time") or 0, "title": c.get("title") or ""}
                    for c in (data.get("chapters") or []) if c.get("start_time") is not None]
        _tlog("resolve TOTAL %.2fs" % (time.time() - _t0))
        return {"ok": True, "info": {
            "title": data.get("title", ""),
            "is_live": bool(data.get("is_live")) or (data.get("live_status") == "is_live"),
            "uploader": data.get("uploader") or data.get("channel") or "",
            "channel_id": data.get("channel_id") or data.get("uploader_id") or "",
            "channel_url": data.get("channel_url") or data.get("uploader_url") or "",
            "description": data.get("description") or "",
            "duration": data.get("duration") or 0,
            "chapters": chapters,
            "muxed_url": muxed_url,
            # Route DASH tracks through the proxy too: googlevideo 403s GStreamer's
            # libsoup HTTP stack (not a fixable header — curl/urllib both get 206), so
            # souphttpsrc fetches localhost and urllib does the real request.
            "video_url": _proxied(video["url"], video_id, video.get("format_id"), http_ua) if video else "",
            "audio_url": _proxied(audio["url"], video_id, audio.get("format_id"), http_ua) if audio else "",
            "qualities": qualities,
            "http_ua": http_ua,
            "muxed_itag": muxed.get("format_id", "") if muxed else "",
            "muxed_proto": muxed.get("protocol", "") if muxed else "",
            "video_itag": video.get("format_id", "") if video else "",
            "audio_itag": audio.get("format_id", "") if audio else "",
        }}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


def _pick(formats, itags):
    """First format (in itag-preference order) that exists and has a direct URL."""
    by_itag = {f.get("format_id"): f for f in formats}
    for itag in itags:
        f = by_itag.get(itag)
        if f and f.get("url"):
            return f
    return None


def _pick_video(formats, cap=0):
    """Best playable video-only track at or below `cap` pixels tall (0 = no cap = best).

    Candidates are property-selected + sorted best-first (highest resolution, preferred codec,
    lower fps); returns the first whose height is within the cap. If nothing fits under the cap,
    falls back to the highest available so playback still happens — this is what makes 'Default
    quality' a ceiling that degrades gracefully when the exact rung isn't offered."""
    cands = _video_candidates(formats)
    if not cands:
        return None
    for f in cands:
        if not cap or (f.get("height") or 0) <= cap:
            return f
    return cands[0]                            # cap below everything offered → highest available


def _pick_audio(formats):
    """Best audio track with a URL — the top of the property-based audio ladder (see
    _audio_candidates: highest bitrate, opus preferred at a tie, original/default language)."""
    cands = _audio_candidates(formats)
    return cands[0] if cands else None


def _norm_url(u):
    """Give a URL a scheme. YouTube hands back avatar URLs protocol-relative
    (`//yt3.ggpht.com/...`) or bare (`yt3.ggpht.com/...`); without a scheme QML resolves
    them against the local file:// base and can't open them."""
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if "://" not in u:
        return "https://" + u
    return u


def _sized_avatar(url, size=176):
    """Shrink a ggpht/googleusercontent avatar to `size` px. These URLs encode the
    dimension as `=sNNN-...`; the largest offered can be ~800px, wasteful for a small icon."""
    url = _norm_url(url)
    if not url:
        return ""
    return re.sub(r"=s\d+", "=s%d" % int(size), url)


def _pick_thumb(entry):
    thumbs = entry.get("thumbnails") or []
    if thumbs:
        return _norm_url(thumbs[-1].get("url", ""))
    return _norm_url(entry.get("thumbnail", "") or "")


def _pick_avatar(entry, size=176):
    """A channel's SQUARE avatar, sized down. Channel metadata carries both the avatar and
    a wide banner; `thumbs[-1]` (largest) is usually the banner, so filter to square ones."""
    thumbs = entry.get("thumbnails") or []
    squares = [t for t in thumbs
               if t.get("url") and t.get("width") and t.get("height")
               and abs(int(t["width"]) - int(t["height"])) <= 2]
    if squares:
        squares.sort(key=lambda t: int(t["width"]))
        chosen = next((t for t in squares if int(t["width"]) >= size), squares[-1])
        return _sized_avatar(chosen["url"], size)
    # No dimensions to tell avatar from banner: the avatar is normally listed first.
    if thumbs:
        return _sized_avatar(thumbs[0].get("url", ""), size)
    return _sized_avatar(entry.get("thumbnail", "") or "", size)


def _video_thumb(vid):
    """Deterministic 320x180 thumbnail for a standard 11-char video id — small and always
    present, unlike the maxres URLs flat search sometimes hands back."""
    if vid and len(vid) == 11:
        return "https://i.ytimg.com/vi/%s/mqdefault.jpg" % vid
    return ""


def _rel_from_ts(ts):
    """A "3 weeks ago"-style string from a unix timestamp."""
    try:
        secs = time.time() - float(ts)
    except Exception:
        return ""
    if secs < 0:
        secs = 0
    day = 86400.0
    if secs < day:
        return "today"
    if secs < 2 * day:
        return "yesterday"

    def _n(unit_secs, word):
        v = int(secs // unit_secs)
        return "%d %s%s ago" % (v, word, "" if v == 1 else "s")

    if secs < 7 * day:
        return _n(day, "day")
    if secs < 30 * day:
        return _n(7 * day, "week")
    if secs < 365 * day:
        return _n(30 * day, "month")
    return _n(365 * day, "year")


def _rel_from_iso(iso):
    """"3 weeks ago" from an ISO-8601 UTC timestamp like 2024-06-15T14:00:00+00:00."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", iso or "")
    if not m:
        return ""
    try:
        ts = calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))
    except Exception:
        return ""
    return _rel_from_ts(ts)


def _rel_date(e):
    """Relative date from a flat entry's timestamp/upload_date, or "" if it lacks one."""
    ts = e.get("timestamp")
    if not ts:
        ud = str(e.get("upload_date") or "")
        if len(ud) == 8:
            try:
                ts = time.mktime(time.strptime(ud, "%Y%m%d"))
            except Exception:
                ts = None
    return _rel_from_ts(ts) if ts else ""


def _channel_dates(channel_id):
    """{video_id: "3 weeks ago"} for a channel's recent uploads, from its RSS feed — exact
    publish dates the flat listing omits. The feed only carries the latest ~15 videos."""
    if not channel_id or not str(channel_id).startswith("UC"):
        return {}
    _force_ipv4()
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=%s" % channel_id
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            xml = r.read().decode("utf-8", "replace")
    except Exception:
        return {}
    out = {}
    for vid, pub in re.findall(
            r"<yt:videoId>([\w-]+)</yt:videoId>.*?<published>([^<]+)</published>",
            xml, re.S):
        rel = _rel_from_iso(pub)
        if rel:
            out[vid] = rel
    return out


def _iso_ts(iso):
    """Unix timestamp from an ISO-8601 UTC string (for sorting), 0 if unparseable."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", iso or "")
    if not m:
        return 0
    try:
        return calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))
    except Exception:
        return 0


def _parse_feed_entries(xml):
    """Parse a channel RSS feed into video dicts (id, title, published, uploader, views)."""
    out = []
    for block in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        vm = re.search(r"<yt:videoId>([\w-]+)</yt:videoId>", block)
        if not vm:
            continue

        def grab(pat):
            g = re.search(pat, block, re.S)
            return g.group(1) if g else ""

        out.append({
            "id": vm.group(1),
            "title": html.unescape(grab(r"<title>(.*?)</title>")) or "(untitled)",
            "published": grab(r"<published>([^<]+)</published>"),
            "uploader": html.unescape(grab(r"<name>(.*?)</name>")),
            "views": int(grab(r'<media:statistics\s+views="(\d+)"') or 0),
        })
    return out


# Search rows are stored in one QML ListModel, so both kinds return the SAME keys — a
# ListModel fixes its roles from the first row, and a missing key would blank that role.
def _video_entry(e):
    vid = e.get("id", "")
    return {
        "type": "video",
        "id": vid,
        "title": e.get("title", "(untitled)"),
        "uploader": e.get("uploader") or e.get("channel") or "",
        "duration": e.get("duration") or 0,
        "thumbnail": _video_thumb(vid) or _pick_thumb(e),
        "url": "",
        "subscribers": 0,
        "views": e.get("view_count") or 0,
        "posted": _rel_date(e),
        # 1 when this is a running livestream (yt-dlp flat `live_status`), for a LIVE badge.
        "live": 1 if (e.get("live_status") == "is_live" or e.get("is_live")) else 0,
    }


def _channel_entry(e):
    return {
        "type": "channel",
        "id": e.get("channel_id") or e.get("id") or "",
        "title": e.get("title") or e.get("channel") or e.get("uploader") or "(channel)",
        "uploader": "",
        "duration": 0,
        "thumbnail": _pick_avatar(e, 176),
        "url": e.get("url") or e.get("channel_url") or e.get("uploader_url") or "",
        "subscribers": e.get("channel_follower_count") or 0,
        "views": 0,
        "posted": "",
    }


# --------------------------------------------------------------------------- #
# Subscriptions (a plain JSON file the app owns) + channel browsing.
# --------------------------------------------------------------------------- #

_dir_ready = False


def _data_dir():
    """Our data dir. On first use, migrate the old harbour-youfish dir over (one-time) so the
    FinTube rename keeps the user's subs, positions, downloads and installed yt-dlp binary."""
    global _dir_ready
    d = os.path.expanduser("~/.local/share/harbour-fintube")
    if _dir_ready:
        return d
    try:
        old = os.path.expanduser("~/.local/share/harbour-youfish")
        if not os.path.isdir(d) and os.path.isdir(old):
            os.rename(old, d)   # carries bin/yt-dlp, subscriptions, positions, downloads
    except Exception:
        pass
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    _dir_ready = True
    return d


def _subs_path():
    return os.path.join(_data_dir(), "subscriptions.json")


# --------------------------------------------------------------------------- #
# Settings (a small JSON file the app owns) + Shorts filtering.
# --------------------------------------------------------------------------- #
_SETTINGS_DEFAULTS = {"hide_shorts": True, "sponsorblock": True,
                      # Hide already-watched videos from the subscription feed + channel lists.
                      "hide_watched": False,
                      # Allow fullscreen while staying in portrait (the video fills the screen
                      # without rotating to landscape).
                      "portrait_fullscreen": False,
                      # Playback speed remembered across videos (applied to each new one).
                      "playback_rate": 1.0,
                      "player_client": "",
                      # yt-dlp update channel: "stable" (default) or "nightly" (YouTube fixes
                      # land days sooner, less tested). Drives ytdlp_update()'s --update-to target.
                      "ytdlp_channel": "stable",
                      # default_quality caps the auto-selected video height (px); "0" = best
                      # available. 720 is a comfortable software-decode HD default.
                      "default_quality": "720",
                      # hw_decode routes video through droidvdec->droideglsink (hardware) and
                      # switches the ladder to VP9-first. Experimental; software is the default.
                      "hw_decode": False,
                      # 10-band equalizer: off by default, flat. eq_bands = per-band gain in dB
                      # (-24..+12), applied by the C++ player's equalizer-10bands.
                      "eq_enabled": False,
                      "eq_bands": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                      # Volume boost (linear gain, 1.0 = none) above system max; a soft limiter in
                      # the player keeps the extra gain from hard-clipping. For quiet BT output.
                      "boost_gain": 1.0,
                      # keep_display_on: hold the display awake while a video is actually playing
                      # (off by default — costs battery). Also sidesteps the display-off/on GL
                      # corruption during playback since the screen never blanks.
                      "keep_display_on": False,
                      # download_dir: where completed downloads are written. "" = the app's own
                      # downloads folder (default); a picked folder (e.g. ~/Videos, an SD card)
                      # overrides it, validated writable before use (see _downloads_dir).
                      "download_dir": "",
                      # PO-token provider (bgutil): opt-in, user-installed. pot_needs_ffi
                      # stays False unless a build genuinely needs node-canvas's native addon
                      # (jsdom degrades gracefully without it).
                      "pot_provider": False, "pot_needs_ffi": False}

# Widened client net, tried in ONE extra yt-dlp pass when the primary (web_embedded) comes
# back SABR-thin (no fetchable HD pair). yt-dlp queries them all and merges formats; the
# url-presence filter in _pick keeps only the ones a SABR client can't serve. Unknown names
# are skipped with a warning, never a hard error, so a broad net here is safe.
_RETRY_CLIENTS = "tv,mweb,android,android_vr"


def _settings_path():
    return os.path.join(_data_dir(), "settings.json")


def _load_settings():
    try:
        with open(_settings_path()) as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except Exception:
        return {}


def get_settings():
    """Current settings merged over defaults — for the QML settings UI."""
    s = dict(_SETTINGS_DEFAULTS)
    s.update(_load_settings())
    return s


def set_setting(key, value):
    s = _load_settings()
    s[key] = value
    try:
        path = _settings_path()
        with open(path, "w") as f:
            json.dump(s, f)
        os.chmod(path, 0o600)     # owner-only (privacy)
    except Exception:
        pass
    return get_settings()


def _default_client():
    """Which YouTube client resolve() uses by default.

    A user-set player_client always wins. Otherwise, when the PO-token provider is active we
    use `web_embedded`: it dodges YouTube's SABR experiment (which strips the adaptive DASH
    URLs from the web/web_safari clients yt-dlp auto-picks) and returns the full, actually
    range-fetchable HD ladder once the token unlocks it. With no provider we leave yt-dlp on
    its own auto pick. resolve() widens to _RETRY_CLIENTS if this comes back SABR-thin."""
    c = (get_settings().get("player_client") or "").strip()
    if c and c.lower() != "auto":
        return c
    return "web_embedded" if _pot_active() else ""


def _yt_extractor_args(client_override=None):
    """`--extractor-args` for yt-dlp built from settings (or []).

    player_client picks a YouTube client. client_override lets resolve() widen the client set
    on a retry without touching the saved preference.
    """
    parts = []
    client = client_override if client_override is not None else _default_client()
    if client and client.lower() != "auto":
        parts.append("player_client=" + client)
    return ["--extractor-args", "youtube:" + ";".join(parts)] if parts else []


def _hide_shorts():
    return bool(get_settings().get("hide_shorts", True))


def _is_short(e):
    """A YouTube Short: its watch URL says so, or (fallback) it's <=60s. The duration
    heuristic can catch a genuinely short normal video, which is why it's user-toggleable."""
    if "/shorts/" in (e.get("url") or ""):
        return True
    dur = e.get("duration")
    return dur is not None and 0 < dur <= 60


def _hide_watched():
    return bool(get_settings().get("hide_watched", False))


def _watched_ids():
    """Set of video ids marked watched (w=1) in the watch history, or empty when this app has no
    watch-history store (FinTune) — so the shared filter is a safe no-op there."""
    loader = globals().get("_load_watch_history")
    if not loader:
        return set()
    try:
        return set(k for k, v in loader().items() if isinstance(v, dict) and v.get("w"))
    except Exception:
        return set()


def _feed_hide_watched(items):
    """Drop watched videos from a feed item list when the setting is on (else unchanged)."""
    if not _hide_watched():
        return items
    watched = _watched_ids()
    return [it for it in items if it.get("id") not in watched]


def _is_members_only(e):
    """Best-effort: a members-only ('subscriber_only') or premium video, which can't be played
    without the membership, so we hide it from results. yt-dlp sets `availability` from the
    'Members only' badge on flat search/playlist entries — not always populated in flat mode,
    hence best-effort (the `filtered_members` count in search() surfaces how many were dropped)."""
    av = (e.get("availability") or "").lower()
    return av in ("subscriber_only", "premium_only", "needs_auth")


# --------------------------------------------------------------------------- #
# Resume points (per-video watch position) + SponsorBlock segments.
# --------------------------------------------------------------------------- #
def _positions_path():
    return os.path.join(_data_dir(), "positions.json")


def _load_positions():
    try:
        with open(_positions_path()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def get_position(video_id):
    """Saved watch position (seconds) for a video, 0 if none."""
    try:
        return int(_load_positions().get(video_id, 0))
    except Exception:
        return 0


def set_position(video_id, seconds):
    """Remember (or, with seconds<=0, forget) where a video was left off. Kept as an
    insertion-ordered LRU so the file can't grow without bound."""
    if not video_id:
        return
    d = _load_positions()
    d.pop(video_id, None)                 # reinsert at the end = most-recent
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = 0
    if seconds > 0:
        d[video_id] = seconds
    if len(d) > 300:
        d = dict(list(d.items())[-300:])
    try:
        with open(_positions_path(), "w") as f:
            json.dump(d, f)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Watch history — richer per-video progress (a play fraction + a sticky 'watched' flag at
# >=80%), used to draw the thumbnail progress bar + the WATCHED tag. Kept SEPARATE from the
# resume store (positions.json), which is cleared once a video finishes; this remembers that
# the video WAS watched even after its resume point is gone.
# --------------------------------------------------------------------------- #
_WATCHED_FRACTION = 0.8


def _watch_history_path():
    return os.path.join(_data_dir(), "watch_history.json")


def _load_watch_history():
    try:
        with open(_watch_history_path()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def watch_state():
    """The whole watch map for the UI: {video_id: {p,d,f,w,t,ti,ch}} — p=position s, d=duration s,
    f=fraction 0..1, w=watched 0/1, t=unix ts, ti=title, ch=channel. Small (LRU-capped at 500),
    read once at startup and refreshed after each save; QML delegates read it per thumbnail."""
    return _load_watch_history()


def watch_history(limit=200):
    """Recently-watched videos, newest first, for the History page: [{id,title,channel,thumbnail,
    pos,dur,frac,watched,ts}]. Thumbnail is derived from the id. Ordered by the store's insertion
    order (record_watch reinserts each play at the end) rather than the 1s-granular ts, so replays
    within the same second still sort most-recent-first."""
    d = _load_watch_history()
    rows = list(d.items())
    rows.reverse()                 # insertion order is oldest→newest, so reverse = newest first
    out = []
    for vid, e in rows[:max(1, int(limit))]:
        if not isinstance(e, dict):
            continue
        out.append({
            "id": vid,
            "title": e.get("ti") or vid,
            "channel": e.get("ch") or "",
            "thumbnail": _video_thumb(vid),
            "pos": int(e.get("p") or 0),
            "dur": int(e.get("d") or 0),
            "frac": float(e.get("f") or 0.0),
            "watched": 1 if e.get("w") else 0,
            "ts": int(e.get("t") or 0),
        })
    return out


def clear_watch_history():
    """Wipe the watch history (History page → clear). Leaves resume points alone."""
    try:
        p = _watch_history_path()
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass
    return {"ok": True}


def record_watch(video_id, position, duration, title="", channel=""):
    """Record playback progress. Updates BOTH the resume point (positions.json, with the
    too-early / basically-finished zeroing) and the watch history the UI reads. A video becomes
    'watched' at >=80% (or within 15s of the end); watched is sticky — a later re-watch that
    only reaches, say, 20% won't clear it. title/channel are stored for the History list."""
    if not video_id:
        return
    try:
        position = max(0, int(position))
    except (TypeError, ValueError):
        position = 0
    try:
        duration = max(0, int(duration))
    except (TypeError, ValueError):
        duration = 0

    # Resume point: forget if barely started or within 15s of the end.
    resume = position
    if position < 10 or (duration > 0 and position > duration - 15):
        resume = 0
    set_position(video_id, resume)

    # Watch history: play fraction + sticky watched flag + title/channel (kept from a prior entry
    # if this save didn't carry them).
    frac = (position / duration) if duration > 0 else 0.0
    frac = max(0.0, min(1.0, frac))
    watched = frac >= _WATCHED_FRACTION or (duration > 0 and position > duration - 15)
    d = _load_watch_history()
    prev = d.pop(video_id, None)                 # reinsert at end = most-recent
    pw = prev if isinstance(prev, dict) else {}
    if pw.get("w"):
        watched = True                           # sticky
    d[video_id] = {"p": position, "d": duration, "f": round(frac, 4),
                   "w": 1 if watched else 0, "t": int(time.time()),
                   "ti": (title or pw.get("ti") or ""),
                   "ch": (channel or pw.get("ch") or "")}
    if len(d) > 500:
        d = dict(list(d.items())[-500:])
    try:
        with open(_watch_history_path(), "w") as f:
            json.dump(d, f)
    except Exception:
        pass


def set_watched(video_id, watched=True, title="", channel=""):
    """Explicitly mark a video watched (or unwatched) from a long-press menu, without playing
    it — writes the same watch-history store the History page reads and hide-watched filters on.
    Marking watched also clears the resume point (positions.json): finished means it shouldn't
    offer to resume from the middle next time. Unlike record_watch's sticky flag, an explicit
    unwatch here DOES clear 'w' (the user asked for it), and leaves the resume point alone.
    Reinserts the entry at the end so it also counts as most-recent."""
    if not video_id:
        return {"ok": False}
    watched = bool(watched)
    d = _load_watch_history()
    prev = d.pop(video_id, None)                  # pop + reinsert = most-recent ordering
    pw = prev if isinstance(prev, dict) else {}
    if not watched and not pw:
        return {"ok": True, "watched": 0}         # nothing to clear — don't create a stub entry
    if watched:
        set_position(video_id, 0)                 # "watched" = finished → forget the resume point
    entry = dict(pw)
    entry["w"] = 1 if watched else 0
    entry["t"] = int(time.time())
    entry["ti"] = title or pw.get("ti") or ""
    entry["ch"] = channel or pw.get("ch") or ""
    # Watched = complete: fill the fraction so the thumbnail's red progress bar reads as done, and
    # move the recorded position to the end to match (the resume point itself was cleared above).
    # Unwatched = fresh: zero the fraction + position so the red bar disappears entirely.
    if watched:
        entry["f"] = 1.0
        entry["p"] = int(entry.get("d") or 0)
    else:
        entry["f"] = 0.0
        entry["p"] = 0
    d[video_id] = entry
    if len(d) > 500:
        d = dict(list(d.items())[-500:])
    try:
        with open(_watch_history_path(), "w") as f:
            json.dump(d, f)
    except Exception:
        return {"ok": False}
    return {"ok": True, "watched": 1 if watched else 0}


# Categories we skip. selfpromo + interaction (subscribe/like reminders) go with sponsors.
_SB_CATEGORIES = '["sponsor","selfpromo","interaction"]'


def sponsor_segments(video_id):
    """SponsorBlock skip segments for a video: [{start, end, category}] in seconds. Uses the
    public sponsor.ajay.app API; 404 just means nobody's submitted any."""
    if not video_id:
        return {"ok": True, "segments": []}
    _force_ipv4()
    url = ("https://sponsor.ajay.app/api/skipSegments?videoID=%s&categories=%s"
           % (urllib.parse.quote(video_id), urllib.parse.quote(_SB_CATEGORIES)))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "harbour-youfish"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as ex:
        return {"ok": True, "segments": []} if ex.code == 404 else {"ok": False, "segments": []}
    except Exception:
        return {"ok": False, "segments": []}
    segs = []
    for s in data if isinstance(data, list) else []:
        seg = s.get("segment") or []
        if len(seg) == 2 and seg[1] > seg[0]:
            segs.append({"start": float(seg[0]), "end": float(seg[1]),
                         "category": s.get("category", "")})
    segs.sort(key=lambda x: x["start"])
    return {"ok": True, "segments": segs}


def list_subscriptions():
    """Saved channels: [{id, name, url, thumbnail}, ...]."""
    try:
        with open(_subs_path()) as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_subscriptions(subs):
    try:
        with open(_subs_path(), "w") as fh:
            json.dump(subs, fh)
    except Exception:
        pass


def is_subscribed(channel_id):
    return bool(channel_id) and any(
        s.get("id") == channel_id for s in list_subscriptions())


def toggle_subscription(channel_id, name="", url="", thumbnail=""):
    """Add or remove a channel; returns the new state + full list."""
    if not channel_id:
        return {"ok": False, "subscribed": False, "subscriptions": list_subscriptions()}
    subs = list_subscriptions()
    if any(s.get("id") == channel_id for s in subs):
        subs = [s for s in subs if s.get("id") != channel_id]
        subscribed = False
    else:
        subs.append({"id": channel_id, "name": name or channel_id,
                     "url": url, "thumbnail": thumbnail})
        subscribed = True
    _save_subscriptions(subs)
    _feed_cache["ts"] = 0.0             # subs changed → rebuild the home feed next time
    _feed_durations_cache["ts"] = 0.0   # …and its duration map
    return {"ok": True, "subscribed": subscribed, "subscriptions": subs}


def _np_query(con, sql):
    """Run a SELECT against a NewPipe/PipePipe backup DB, returning [] when the table or a column
    is absent — the schema varies across NewPipe versions and PipePipe forks, so a missing piece
    should skip its section, not abort the whole import."""
    import sqlite3
    try:
        return con.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []


def _np_video_id(url):
    """Extract an 11-char YouTube video id from a streams.url (watch?v= / youtu.be/ / shorts/ /
    embed/). Returns "" for a non-YouTube or malformed row."""
    if not url:
        return ""
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([0-9A-Za-z_-]{11})", url)
    return m.group(1) if m else ""


def import_newpipe(path):
    """Import a NewPipe / PipePipe backup: subscriptions, watch history + resume points, local
    playlists, and bookmarked YouTube playlists.

    `path` is the exported .zip (which contains newpipe.db) or a raw newpipe.db. Everything is read
    offline (no network) and YouTube-only (service_id 0 — SoundCloud/PeerTube/etc. rows are dropped).
    Imported data is MERGED into the existing stores without clobbering anything already local.
    Returns {ok, added, skipped, total, count, history, resume, playlists, remote, summary, error?};
    added/skipped/total/count are the subscription figures (kept for back-compat) and `summary` is a
    ready-to-show sentence."""
    import zipfile
    import sqlite3
    p = (path or "").strip()
    if p.startswith("file://"):
        p = p[len("file://"):]
    p = os.path.expanduser(p)
    if not os.path.isfile(p):
        return {"ok": False, "error": "File not found."}
    tmp_db = None
    try:
        if zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as zf:
                name = None
                for n in zf.namelist():
                    b = os.path.basename(n).lower()
                    if b == "newpipe.db" or (b.endswith(".db") and not n.endswith("/")):
                        name = n
                        break
                if not name:
                    return {"ok": False, "error": "No newpipe.db inside that backup zip."}
                tmp_db = os.path.join(_data_dir(), "newpipe-import.db")
                with zf.open(name) as src, open(tmp_db, "wb") as out:
                    shutil.copyfileobj(src, out)
            db_path = tmp_db
        else:
            db_path = p                       # a raw .db handed over directly
        con = sqlite3.connect(db_path)
        try:
            return _import_newpipe_db(con)
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return {"ok": False, "error": "That file isn't a NewPipe/PipePipe database."}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}
    finally:
        if tmp_db:
            try:
                os.remove(tmp_db)
            except Exception:
                pass


def _import_newpipe_db(con):
    """Read every supported table from an open NewPipe backup connection and merge it into our
    stores. Split out from import_newpipe so the zip/tempfile handling stays readable. Feature-
    guarded so the identical code also runs in FinTune (no watch-history store): the history
    section self-skips there rather than erroring."""
    # streams is the join hub: history / resume / playlist rows carry only a stream_id pointing
    # here, and this is where the title/uploader/duration/thumbnail actually live.
    streams = {}
    for row in _np_query(
            con, "SELECT uid, service_id, url, title, duration, uploader, thumbnail_url "
                 "FROM streams"):
        uid, service_id, url, title, duration, uploader, thumb = row
        if service_id not in (0, None):
            continue
        vid = _np_video_id(url)
        if not vid:
            continue
        try:
            dur = int(duration or 0)
        except (TypeError, ValueError):
            dur = 0
        streams[uid] = {"id": vid, "title": title or vid, "uploader": uploader or "",
                        "duration": dur, "thumbnail": thumb or _video_thumb(vid)}

    # ---- subscriptions ------------------------------------------------------
    subs = list_subscriptions()
    have_sub = set(s.get("id") for s in subs if s.get("id"))
    ch_re = re.compile(r"/channel/(UC[0-9A-Za-z_-]{20,})")
    sub_rows = _np_query(con, "SELECT service_id, url, name, avatar_url FROM subscriptions")
    if not sub_rows:                          # a very old export without avatar_url
        sub_rows = [(r[0], r[1], r[2], "") for r in
                    _np_query(con, "SELECT service_id, url, name FROM subscriptions")]
    subs_added = subs_skipped = subs_total = 0
    for row in sub_rows:
        service_id, url, name = row[0], row[1], row[2]
        avatar = row[3] if len(row) > 3 else ""
        if service_id not in (0, None):       # 0 = YouTube; skip SoundCloud/PeerTube/Bandcamp/…
            continue
        subs_total += 1
        m = ch_re.search(url or "")
        if not m:
            subs_skipped += 1                 # a handle/@ URL we can't map to a channel id offline
            continue
        cid = m.group(1)
        if cid in have_sub:
            continue
        have_sub.add(cid)
        subs.append({"id": cid, "name": name or cid,
                     "url": "https://www.youtube.com/channel/%s" % cid,
                     "thumbnail": avatar or ""})
        subs_added += 1
    if subs_added:
        _save_subscriptions(subs)
        _feed_cache["ts"] = 0.0
        _feed_durations_cache["ts"] = 0.0

    # ---- watch history + resume points --------------------------------------
    # stream_state.progress_time is the resume position in MILLISECONDS; NewPipe clears it once a
    # video finishes, so a stream that's in history with no state row is treated as fully watched.
    # stream_history has one row per access (composite PK stream_id+access_date): keep the latest
    # access_date (epoch ms) and sum repeat_count.
    state = {}
    for sid, pt in _np_query(con, "SELECT stream_id, progress_time FROM stream_state"):
        state[sid] = pt
    hist = {}
    for sid, access_date, repeat in _np_query(
            con, "SELECT stream_id, access_date, repeat_count FROM stream_history"):
        h = hist.setdefault(sid, {"access": 0, "repeat": 0})
        try:
            ad = int(access_date or 0)
        except (TypeError, ValueError):
            ad = 0
        if ad > h["access"]:
            h["access"] = ad
        try:
            h["repeat"] += int(repeat or 0)
        except (TypeError, ValueError):
            pass

    watched_thresh = globals().get("_WATCHED_FRACTION", 0.8)
    imported = []                             # (ts, video_id, entry), sorted oldest→newest below
    for sid in (set(state) | set(hist)):
        meta = streams.get(sid)
        if not meta:
            continue                          # unresolved / non-YouTube stream
        dur = meta["duration"]
        pos_ms = state.get(sid)
        try:
            pos = int(int(pos_ms) / 1000) if pos_ms else 0
        except (TypeError, ValueError):
            pos = 0
        h = hist.get(sid) or {}
        ts = int((h.get("access") or 0) / 1000)
        frac = (pos / dur) if dur > 0 else 0.0
        frac = max(0.0, min(1.0, frac))
        near_end = dur > 0 and pos > dur - 15
        finished = sid in hist and sid not in state   # state cleared on finish ≈ fully watched
        watched = frac >= watched_thresh or near_end or finished
        imported.append((ts, meta["id"], {
            "p": pos, "d": dur, "f": round(frac, 4), "w": 1 if watched else 0,
            "t": ts, "ti": meta["title"], "ch": meta["uploader"]}))
    imported.sort(key=lambda x: x[0])

    hist_added = 0
    has_history = ("_load_watch_history" in globals() and "_watch_history_path" in globals())
    if has_history and imported:
        existing_hist = _load_watch_history()
        merged = {}
        for ts, vid, entry in imported:
            if vid in existing_hist:
                continue                      # keep the user's own, fresher record
            merged[vid] = entry
            hist_added += 1
        for vid, e in existing_hist.items():  # local history appended last = stays newest
            merged.pop(vid, None)
            merged[vid] = e
        if len(merged) > 500:
            merged = dict(list(merged.items())[-500:])
        if hist_added:
            try:
                with open(_watch_history_path(), "w") as f:
                    json.dump(merged, f)
            except Exception:
                pass

    pos_added = 0
    if imported:
        positions = _load_positions()
        for ts, vid, entry in imported:
            if vid in positions:
                continue                      # don't overwrite a local resume point
            pp, dd = entry["p"], entry["d"]
            if pp > 10 and not (dd > 0 and pp > dd - 15):
                positions[vid] = pp
                pos_added += 1
        if pos_added:
            if len(positions) > 300:
                positions = dict(list(positions.items())[-300:])
            try:
                with open(_positions_path(), "w") as f:
                    json.dump(positions, f)
            except Exception:
                pass

    # ---- local playlists ----------------------------------------------------
    playlists = _load_playlists()
    have_local = set((p.get("title") or "").strip().lower()
                     for p in playlists if p.get("kind", "local") == "local")
    have_yt = set(p.get("yt_id") for p in playlists if p.get("yt_id"))
    members = {}                              # playlist uid -> [stream_id, ...] in join order
    for pl_id, sid, join_index in _np_query(
            con, "SELECT playlist_id, stream_id, join_index FROM playlist_stream_join "
                 "ORDER BY playlist_id, join_index"):
        members.setdefault(pl_id, []).append(sid)
    pl_added = 0
    for uid, name in _np_query(con, "SELECT uid, name FROM playlists ORDER BY display_index"):
        title = (name or "Playlist").strip()[:100] or "Playlist"
        if title.lower() in have_local:
            continue                          # a same-named local list already exists
        items, seen = [], set()
        for sid in members.get(uid, []):
            meta = streams.get(sid)
            if not meta or meta["id"] in seen:
                continue
            seen.add(meta["id"])
            items.append({"id": meta["id"], "title": meta["title"],
                          "uploader": meta["uploader"], "duration": meta["duration"],
                          "thumbnail": meta["thumbnail"]})
        playlists.append({"id": uuid.uuid4().hex[:12], "title": title,
                          "kind": "local", "items": items})
        have_local.add(title.lower())
        pl_added += 1

    # ---- bookmarked YouTube playlists ---------------------------------------
    # Only the metadata is in the backup (not the video list), so store the list id with empty
    # items; opening it in the library fetches the contents through the normal refresh path.
    list_re = re.compile(r"[?&]list=([0-9A-Za-z_-]+)")
    rp_added = 0
    for row in _np_query(con, "SELECT service_id, name, url FROM remote_playlists"):
        service_id, name, url = row[0], row[1], row[2]
        if service_id not in (0, None):
            continue
        m = list_re.search(url or "")
        if not m:
            continue
        yt_id = m.group(1)
        if yt_id in have_yt:
            continue
        have_yt.add(yt_id)
        playlists.append({"id": uuid.uuid4().hex[:12],
                          "title": (name or "Playlist").strip()[:100] or "Playlist",
                          "kind": "youtube", "yt_id": yt_id, "items": []})
        rp_added += 1
    if pl_added or rp_added:
        _save_playlists(playlists)

    # ---- summary ------------------------------------------------------------
    def _n(n, one, many=None):
        return "%d %s" % (n, one if n == 1 else (many or one + "s"))
    parts = []
    if subs_added:
        parts.append(_n(subs_added, "subscription"))
    if hist_added:
        parts.append(_n(hist_added, "watched video"))
    if pos_added:
        parts.append(_n(pos_added, "resume point"))
    if pl_added:
        parts.append(_n(pl_added, "playlist"))
    if rp_added:
        parts.append(_n(rp_added, "saved playlist"))
    summary = ("Imported " + ", ".join(parts) + ".") if parts else "Nothing new to import."
    return {"ok": True, "added": subs_added, "skipped": subs_skipped, "total": subs_total,
            "count": len(subs), "history": hist_added, "resume": pos_added,
            "playlists": pl_added, "remote": rp_added, "summary": summary}


_avatar_cache = {}         # channel -> {"ts": epoch, "res": {...}}
_avatar_cache_lock = threading.Lock()
_AVATAR_CACHE_TTL = 86400  # avatars rarely change; a day avoids re-running yt-dlp per view
_AVATAR_CACHE_MAX = 128


def channel_avatar(channel):
    """Just the channel's avatar URL + id — cheap enough to fetch on video open.

    Fetches one flat entry so yt-dlp still hands back the channel metadata (avatar)
    without listing the whole uploads tab. Cached for a day so opening several of a
    channel's videos doesn't re-run yt-dlp each time.
    """
    path = _ytdlp_path()
    if not path or not channel:
        return {"ok": False}
    with _avatar_cache_lock:
        ent = _avatar_cache.get(channel)
        if ent and time.time() - ent["ts"] < _AVATAR_CACHE_TTL:
            return ent["res"]
    url = channel
    if "://" not in url:
        url = "https://www.youtube.com/channel/%s" % channel
    if not url.rstrip("/").endswith("/videos"):
        url = url.rstrip("/") + "/videos"
    try:
        proc = subprocess.run(
            [path, *_COMMON_ARGS, "--flat-playlist", "--playlist-items", "1",
             "--dump-single-json", "--", url],
            capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return {"ok": False}
        data = json.loads(proc.stdout)
        res = {"ok": True,
               "id": data.get("channel_id") or data.get("id") or "",
               "thumbnail": _pick_avatar(data, 176)}
        with _avatar_cache_lock:
            _avatar_cache[channel] = {"ts": time.time(), "res": res}
            if len(_avatar_cache) > _AVATAR_CACHE_MAX:  # evict oldest beyond the cap
                for k, _ in sorted(_avatar_cache.items(),
                                   key=lambda kv: kv[1]["ts"])[:len(_avatar_cache) - _AVATAR_CACHE_MAX]:
                    _avatar_cache.pop(k, None)
        return res
    except Exception:
        return {"ok": False}


def channel_videos(channel, start=1, n=30):
    """A page of a channel's uploads (a channel_id or any channel URL). `start` is the
    1-based index of the first video wanted, so the UI can page in more as it scrolls."""
    path = _ytdlp_path()
    if not path:
        return {"ok": False, "error": "yt-dlp not found"}
    url = channel
    if "://" not in url:
        url = "https://www.youtube.com/channel/%s" % channel
    if not url.rstrip("/").endswith("/videos"):
        url = url.rstrip("/") + "/videos"
    try:
        start = max(1, int(start))
        n = max(1, int(n))
        proc = subprocess.run(
            [path, *_COMMON_ARGS, "--flat-playlist",
             "--playlist-items", "%d:%d" % (start, start + n - 1),
             "--dump-single-json", "--", url],
            capture_output=True, text=True, timeout=90)
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr.strip()[:300] or "channel fetch failed")}
        data = json.loads(proc.stdout)
        raw = [e for e in data.get("entries", []) if e.get("id")]
        has_more = len(raw) >= n     # a full page back → assume another page exists
        entries = [e for e in raw if not _is_members_only(e)]
        if _hide_shorts():
            entries = [e for e in entries if not _is_short(e)]
        if _hide_watched():
            watched = _watched_ids()
            entries = [e for e in entries if e.get("id") not in watched]
        items = [{
            "id": e.get("id", ""),
            "title": e.get("title", "(untitled)"),
            "uploader": e.get("uploader") or e.get("channel") or data.get("channel") or "",
            "duration": e.get("duration") or 0,
            # Flat entries often omit thumbnails; derive the reliable one from the id.
            "thumbnail": _video_thumb(e.get("id", "")) or _pick_thumb(e),
            "views": e.get("view_count") or 0,
            "posted": _rel_date(e),
            "live": 1 if (e.get("live_status") == "is_live" or e.get("is_live")) else 0,
        } for e in entries]
        # Flat entries lack dates; fill the recent ones from the channel's RSS feed.
        if start <= 1:
            dates = _channel_dates(data.get("channel_id") or "")
            for it in items:
                if not it["posted"] and it["id"] in dates:
                    it["posted"] = dates[it["id"]]
        return {"ok": True, "items": items, "has_more": has_more, "channel": {
            "id": data.get("channel_id") or data.get("id") or "",
            "name": data.get("channel") or data.get("uploader") or data.get("title") or "",
            "url": data.get("channel_url") or data.get("webpage_url") or url,
            "thumbnail": _pick_avatar(data, 176),
            "subscribers": data.get("channel_follower_count") or 0,
            "video_count": data.get("playlist_count") or 0,
        }}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


_feed_cache = {"ts": 0.0, "items": []}


def subscription_feed(limit=100, force=False):
    """Compile subscribed channels' recent uploads into one feed, newest first. Built from
    each channel's RSS feed (fast + carries dates/views), fetched in parallel and cached
    briefly so returning to the home page is instant."""
    subs = list_subscriptions()
    ids = [s.get("id") for s in subs if str(s.get("id") or "").startswith("UC")]
    if not ids:
        return {"ok": True, "items": []}
    if (not force and _feed_cache["items"]
            and time.time() - _feed_cache["ts"] < 300):
        return {"ok": True, "items": _feed_hide_watched(_feed_cache["items"])[:int(limit)],
                "cached": True}
    _force_ipv4()

    def fetch(cid):
        url = "https://www.youtube.com/feeds/videos.xml?channel_id=%s" % cid
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
            with urllib.request.urlopen(req, timeout=10) as r:
                return _parse_feed_entries(r.read().decode("utf-8", "replace"))
        except Exception:
            return []

    entries = []
    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(12, len(ids))) as ex:
            for res in ex.map(fetch, ids):
                entries.extend(res)
    except Exception:
        for cid in ids:
            entries.extend(fetch(cid))

    entries.sort(key=lambda e: _iso_ts(e.get("published")), reverse=True)
    items = [{
        "id": e["id"],
        "title": e["title"],
        "uploader": e["uploader"],
        "duration": 0,
        "thumbnail": _video_thumb(e["id"]),
        "views": e.get("views") or 0,
        "posted": _rel_from_iso(e.get("published")),
    } for e in entries]
    _feed_cache["ts"] = time.time()
    _feed_cache["items"] = items
    return {"ok": True, "items": _feed_hide_watched(items)[:int(limit)]}


_feed_durations_cache = {"ts": 0.0, "map": {}}


def feed_durations(limit_per_channel=30):
    """{video_id: duration_seconds} for subscribed channels' recent uploads. RSS (the feed
    source) has no duration, so this pulls it from yt-dlp's flat listing — fetched in
    parallel and cached, and called AFTER the RSS feed shows so it never blocks it."""
    subs = list_subscriptions()
    urls = []
    for s in subs:
        cid = str(s.get("id") or "")
        url = str(s.get("url") or "")
        if cid.startswith("UC"):
            urls.append("https://www.youtube.com/channel/%s/videos" % cid)
        elif url:
            u = url.rstrip("/")
            urls.append(u if u.endswith("/videos") else u + "/videos")
    if not urls:
        return {}
    if (_feed_durations_cache["map"]
            and time.time() - _feed_durations_cache["ts"] < 300):
        return _feed_durations_cache["map"]
    path = _ytdlp_path()
    if not path:
        return {}

    def fetch(u):
        try:
            proc = subprocess.run(
                [path, *_COMMON_ARGS, "--flat-playlist",
                 "--playlist-end", str(int(limit_per_channel)), "--dump-single-json", "--", u],
                capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                return {}
            data = json.loads(proc.stdout)
            out = {}
            for e in data.get("entries", []):
                vid, dur = e.get("id"), e.get("duration")
                if vid and dur:
                    out[vid] = int(dur)
            return out
        except Exception:
            return {}

    dmap = {}
    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(urls))) as ex:
            for res in ex.map(fetch, urls):
                dmap.update(res)
    except Exception:
        for u in urls:
            dmap.update(fetch(u))
    _feed_durations_cache["ts"] = time.time()
    _feed_durations_cache["map"] = dmap
    return dmap


def comments(video_id, limit=50):
    """Fetch up to `limit` top-level comments (top-sorted, replies skipped) for a video.

    Comment extraction walks YouTube's continuation tokens, so it's slow — this is called
    on demand (tap to load), never as part of resolve(). We fetch one capped batch and the
    UI reveals it a few at a time as the user scrolls.
    """
    path = _ytdlp_path()
    if not path:
        return {"ok": False, "error": "yt-dlp not found"}
    url = video_id
    if "://" not in url:
        url = "https://www.youtube.com/watch?v=" + video_id
    try:
        n = max(1, int(limit))
    except (TypeError, ValueError):
        n = 50
    # max_comments = total,max-parents,max-replies,max-replies-per-thread — parents only.
    xargs = "youtube:max_comments=%d,%d,0,0;comment_sort=top" % (n, n)
    try:
        proc = subprocess.run(
            [path, *_COMMON_ARGS, "--skip-download", "--write-comments",
             "--extractor-args", xargs, "--dump-single-json", "--", url],
            capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr.strip()[:300] or "comments failed")}
        data = json.loads(proc.stdout)
        raw = data.get("comments") or []
        out = []
        for c in raw:
            parent = c.get("parent")
            if parent and parent != "root":
                continue  # defensive: skip replies even though we asked for none
            out.append({
                "author": c.get("author") or "",
                "text": c.get("text") or "",
                "likes": c.get("like_count") or 0,
                "time": c.get("_time_text") or "",
                "thumbnail": c.get("author_thumbnail") or "",
                "is_uploader": bool(c.get("author_is_uploader")),
            })
            if len(out) >= n:
                break
        # comment_count is YouTube's real total; `count` is how many we actually fetched.
        return {"ok": True, "comments": out, "count": len(out),
                "total": data.get("comment_count") or 0}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "comments timed out"}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


# --------------------------------------------------------------------------- #
# Downloads: audio (140 → .m4a), or video — merged best HD video+audio (→ .mkv) when ffmpeg is
# installed, else muxed progressive (22/18 → .mp4).
# yt-dlp runs in a background thread; progress + completion go to QML via
# pyotherside.send events. Metadata is tracked in downloads.json.
# --------------------------------------------------------------------------- #
def _downloads_dir():
    """Where completed downloads are written. Defaults to a 'downloads' folder in the app's data
    dir; a user-set download_dir (Settings) overrides it when that folder exists and is writable —
    so media can land in ~/Videos, ~/Music, an SD card, etc. Falls back to the default if the chosen
    folder can't be created/written (e.g. an unmounted card), so a download never goes nowhere."""
    custom = (get_settings().get("download_dir") or "").strip()
    if custom:
        p = os.path.expanduser(custom)
        try:
            os.makedirs(p, exist_ok=True)
            if os.access(p, os.W_OK):
                return p
        except Exception:
            pass
    d = os.path.join(_data_dir(), "downloads")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def download_location():
    """Where downloads go, for the Settings UI: the configured value ('' = app default), the
    effective absolute dir actually in use, and whether a custom dir is set."""
    configured = (get_settings().get("download_dir") or "").strip()
    return {"configured": configured, "effective": _downloads_dir(),
            "custom": bool(configured)}


def set_download_dir(path):
    """Choose the download folder (Settings → folder picker). '' resets to the app's own folder.
    Validates that the folder can be created + written and REFUSES (keeps the previous value) if
    not, so a bad pick can't silently send downloads nowhere. Accepts a plain path or a file:// URL.
    Returns download_location() plus {ok, error?}."""
    p = (path or "").strip()
    if p.startswith("file://"):
        p = p[len("file://"):]
    p = os.path.expanduser(p)
    if not p:
        set_setting("download_dir", "")
        return dict(download_location(), ok=True)
    try:
        os.makedirs(p, exist_ok=True)
        if not os.access(p, os.W_OK):
            return dict(download_location(), ok=False, error="That folder isn't writable.")
    except Exception as ex:
        return dict(download_location(), ok=False, error=str(ex))
    set_setting("download_dir", p)
    return dict(download_location(), ok=True)


def _downloads_path():
    return os.path.join(_data_dir(), "downloads.json")


def _safe_name(s):
    s = re.sub(r"[^\w\-. ]+", "_", s or "")[:80].strip()
    return s or "video"


def list_downloads():
    """Completed downloads: [{id, title, kind, path}, ...]. Drops entries whose file is gone."""
    try:
        with open(_downloads_path()) as f:
            lst = json.load(f)
        if not isinstance(lst, list):
            return []
    except Exception:
        return []
    live = [d for d in lst if d.get("path") and os.path.exists(d["path"])]
    if len(live) != len(lst):
        _save_downloads(live)
    return live


def _save_downloads(lst):
    try:
        with open(_downloads_path(), "w") as f:
            json.dump(lst, f)
    except Exception:
        pass


def download(video_id, title, kind):
    """Kick off a background download. kind = "audio" (m4a) | "video". Video merges the best HD
    video+audio via ffmpeg when it's installed (→ .mkv); without ffmpeg it falls back to a muxed
    progressive stream (<=360p, → .mp4)."""
    import pyotherside
    kind = "audio" if kind == "audio" else "video"
    merge = []
    if kind == "audio":
        fmt, ext = "140", "m4a"
    elif _ffmpeg_dir():
        # ffmpeg present → merge best separate video+audio. Cap by the Default-quality setting;
        # exclude AV1 (no hardware decoder on the target). mkv holds any codec combo (VP9/opus or
        # H.264/m4a) cleanly, and GStreamer plays it back fine.
        try:
            cap = int(get_settings().get("default_quality") or 0)
        except (TypeError, ValueError):
            cap = 0
        h = ("[height<=%d]" % cap) if cap else ""
        # HD adaptive first; then muxed progressive (22/18) so a SABR-thin result still yields
        # *something* to download rather than erroring out with "no format".
        fmt = "bestvideo%s[vcodec!*=av01]+bestaudio/22/18/best" % h
        ext, merge = "mkv", ["--merge-output-format", "mkv"]
    else:
        fmt, ext = "22/18", "mp4"
    binp = _ytdlp_path()
    if not binp:
        pyotherside.send("download_done", video_id, kind, False, "yt-dlp not found")
        return {"ok": False}
    # Sanitise the id before it reaches the -o output template and the URL: strip anything
    # outside [\w-] so a crafted id can't traverse out of downloads/ (../) or inject a yt-dlp
    # output-template field (%(...)s). Real YouTube ids are 11 chars of [\w-], so this is a
    # no-op for them. The stored entry + progress events still use the original id for UI matching.
    vid = re.sub(r"[^\w-]", "", video_id)[:64]
    url = "https://www.youtube.com/watch?v=" + vid
    base = os.path.join(_downloads_dir(), "%s [%s] %s" % (_safe_name(title), vid, kind))

    def run():
        try:
            _ensure_pot_server()  # a download is just as PO-gated as playback
            proc = subprocess.Popen(
                [binp, *_COMMON_ARGS, *_yt_extractor_args(), *_pot_ytdlp_args(), *_ffmpeg_args(),
                 "--no-playlist", "-f", fmt, *merge, "--no-part", "--newline",
                 "-o", base + ".%(ext)s", "--", url],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            last = -1
            tail = []                              # keep the last lines to explain a failure
            for line in proc.stdout:
                s = line.rstrip()
                if s:
                    tail.append(s)
                    if len(tail) > 15:
                        tail.pop(0)
                m = re.search(r"\[download\]\s+([\d.]+)%", line)
                if m:
                    pct = float(m.group(1))
                    if int(pct) != last:
                        last = int(pct)
                        pyotherside.send("download_progress", video_id, kind, pct)
            proc.wait()
            fpath = base + "." + ext
            if proc.returncode == 0 and not os.path.exists(fpath):
                import glob
                cand = glob.glob(base + ".*")
                fpath = cand[0] if cand else fpath
            if proc.returncode == 0 and os.path.exists(fpath):
                lst = [d for d in list_downloads()
                       if not (d.get("id") == video_id and d.get("kind") == kind)]
                lst.insert(0, {"id": video_id, "title": title or video_id,
                               "kind": kind, "path": fpath})
                _save_downloads(lst)
                pyotherside.send("download_done", video_id, kind, True, "")
            else:
                # Surface yt-dlp's own tail output so a failure is diagnosable, not a shrug.
                msg = ("\n".join(tail))[-400:] or "download failed"
                pyotherside.send("download_done", video_id, kind, False, msg)
        except Exception as ex:
            pyotherside.send("download_done", video_id, kind, False, str(ex))

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def delete_download(video_id, kind):
    keep = []
    for d in list_downloads():
        if d.get("id") == video_id and d.get("kind") == kind:
            try:
                if d.get("path") and os.path.exists(d["path"]):
                    os.remove(d["path"])
            except Exception:
                pass
        else:
            keep.append(d)
    _save_downloads(keep)
    return {"ok": True, "downloads": keep}


# --------------------------------------------------------------------------- #
# Playlists: a local library of user-made lists and saved YouTube playlists.
# Stored in playlists.json as [{id, title, kind: local|youtube, yt_id, items:[...]}].
# --------------------------------------------------------------------------- #
def _playlists_path():
    return os.path.join(_data_dir(), "playlists.json")


def _load_playlists():
    try:
        with open(_playlists_path()) as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save_playlists(lst):
    try:
        with open(_playlists_path(), "w") as f:
            json.dump(lst, f)
    except Exception:
        pass


def _playlist_summary(p):
    items = p.get("items", [])
    return {
        "id": p.get("id", ""),
        "title": p.get("title", "(untitled)"),
        "kind": p.get("kind", "local"),        # local | youtube
        "yt_id": p.get("yt_id", ""),
        "count": len(items),
        "thumbnail": items[0].get("thumbnail", "") if items else "",
    }


def list_playlists():
    """Lightweight list for the library page (no per-item payload)."""
    return [_playlist_summary(p) for p in _load_playlists()]


def get_playlist(pl_id):
    for p in _load_playlists():
        if p.get("id") == pl_id:
            return {"ok": True, "playlist": p}
    return {"ok": False, "error": "not found"}


def create_playlist(title):
    lst = _load_playlists()
    p = {"id": uuid.uuid4().hex[:12], "title": (title or "New playlist").strip()[:100] or "New playlist",
         "kind": "local", "items": []}
    lst.insert(0, p)
    _save_playlists(lst)
    return {"ok": True, "id": p["id"], "playlists": list_playlists()}


def rename_playlist(pl_id, title):
    lst = _load_playlists()
    for p in lst:
        if p.get("id") == pl_id:
            p["title"] = (title or p.get("title", "")).strip()[:100] or p.get("title", "")
    _save_playlists(lst)
    return {"ok": True, "playlists": list_playlists()}


def delete_playlist(pl_id):
    _save_playlists([p for p in _load_playlists() if p.get("id") != pl_id])
    return {"ok": True, "playlists": list_playlists()}


def add_to_playlist(pl_id, video_id, title="", uploader="", duration=0, thumbnail=""):
    """Append a video to a local playlist (no-op if it's already in there)."""
    lst = _load_playlists()
    for p in lst:
        if p.get("id") == pl_id:
            items = p.setdefault("items", [])
            if not any(it.get("id") == video_id for it in items):
                items.append({"id": video_id, "title": title or video_id,
                              "uploader": uploader or "", "duration": duration or 0,
                              "thumbnail": thumbnail or _video_thumb(video_id)})
            break
    _save_playlists(lst)
    return {"ok": True}


def remove_from_playlist(pl_id, video_id):
    lst = _load_playlists()
    for p in lst:
        if p.get("id") == pl_id:
            p["items"] = [it for it in p.get("items", []) if it.get("id") != video_id]
    _save_playlists(lst)
    return get_playlist(pl_id)


def youtube_playlist(ref, limit=200):
    """Fetch a YouTube playlist's videos (flat). ref = a list id or any playlist URL."""
    path = _ytdlp_path()
    if not path:
        return {"ok": False, "error": "yt-dlp not found"}
    url = ref if "://" in (ref or "") else ("https://www.youtube.com/playlist?list=" + (ref or ""))
    try:
        proc = subprocess.run(
            [path, *_COMMON_ARGS, *_yt_extractor_args(), "--flat-playlist",
             "--playlist-end", str(int(limit)), "--dump-single-json", "--", url],
            capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr.strip()[:300] or "playlist fetch failed")}
        data = json.loads(proc.stdout)
        items = [{
            "id": e.get("id", ""),
            "title": e.get("title", "(untitled)"),
            "uploader": e.get("uploader") or e.get("channel") or "",
            "duration": e.get("duration") or 0,
            "thumbnail": _video_thumb(e.get("id", "")) or _pick_thumb(e),
            "live": 1 if (e.get("live_status") == "is_live" or e.get("is_live")) else 0,
        } for e in data.get("entries", []) if e.get("id")]
        return {"ok": True,
                "title": data.get("title") or "Playlist",
                "uploader": data.get("uploader") or data.get("channel") or "",
                "yt_id": data.get("id") or "",
                "items": items}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


def save_youtube_playlist(ref):
    """Fetch a YouTube playlist and store it in the library (kind=youtube), deduped by list id."""
    res = youtube_playlist(ref)
    if not res.get("ok"):
        return res
    lst = _load_playlists()
    yt_id = res.get("yt_id") or ref
    existing = next((p for p in lst if p.get("yt_id") == yt_id), None)
    if existing:
        existing["title"] = res["title"]
        existing["items"] = res["items"]
    else:
        lst.insert(0, {"id": uuid.uuid4().hex[:12], "title": res["title"],
                       "kind": "youtube", "yt_id": yt_id, "items": res["items"]})
    _save_playlists(lst)
    return {"ok": True, "playlists": list_playlists()}


def refresh_playlist(pl_id):
    """Re-fetch a saved YouTube playlist's items from YouTube."""
    lst = _load_playlists()
    for p in lst:
        if p.get("id") == pl_id and p.get("kind") == "youtube":
            res = youtube_playlist(p.get("yt_id") or "")
            if res.get("ok"):
                p["title"] = res["title"]
                p["items"] = res["items"]
                _save_playlists(lst)
                return get_playlist(pl_id)
            return res
    return get_playlist(pl_id)


def channel_playlists(channel):
    """A channel's playlists (its /playlists tab). Falls back to /releases so music/topic
    channels — whose uploads live under Releases as albums — still return something.
    Each item: {yt_id, title, thumbnail, count}."""
    path = _ytdlp_path()
    if not path:
        return {"ok": False, "error": "yt-dlp not found"}
    url = channel if "://" in (channel or "") else ("https://www.youtube.com/channel/%s" % channel)
    base = url.rstrip("/")
    for suffix in ("/videos", "/featured", "/streams", "/shorts", "/playlists", "/releases"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break

    def fetch(tab):
        try:
            proc = subprocess.run(
                [path, *_COMMON_ARGS, *_yt_extractor_args(), "--flat-playlist",
                 "--dump-single-json", "--", base + tab],
                capture_output=True, text=True, timeout=90)
            if proc.returncode != 0:
                return []
            data = json.loads(proc.stdout)
            out = []
            for e in data.get("entries", []):
                plid = e.get("id") or ""
                if not plid:
                    continue
                out.append({
                    "yt_id": plid,
                    "title": e.get("title") or "(playlist)",
                    "thumbnail": _pick_thumb(e),
                    "count": e.get("playlist_count") or 0,
                })
            return out
        except Exception:
            return []

    items = fetch("/playlists") or fetch("/releases")
    return {"ok": True, "items": items}
