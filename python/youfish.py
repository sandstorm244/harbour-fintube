"""Youfish backend: thin wrapper over an external yt-dlp binary + a local media proxy.

The app never pins a yt-dlp version — it shells out to whatever yt-dlp is on the
device, and the user updates that binary themselves. Every call is made from
PyOtherSide's worker thread, so blocking subprocess calls are fine here.

Playback note: googlevideo rejects GStreamer's libsoup HTTP stack with 403 (not a
fixable header — curl/urllib with identical headers get 206), so BOTH tracks of the
dual-source pipeline stream through a tiny localhost proxy (below) that refetches
the real URL with the format's own User-Agent and serves byte ranges from a
bounded, backpressured on-disk download job.

PO tokens (2026 reality): YouTube increasingly binds a Proof-of-Origin token to the
stream URLs — without one many clients return nothing fetchable. The bgutil
provider (OPT-IN, user-installed; see install_pot_provider) mints them on a
sandboxed Deno sidecar. The common path avoids the mint entirely: resolve() asks
the InnerTube fast path first and its yt-dlp fallback's primary dump is the TOKEN-FREE
JS-less client (_token_free_client), run anonymously; the token machinery is only
the safety net for gated/restricted videos (see _resolve_uncached / _default_client).
"""

import atexit
import calendar
import contextlib
import ctypes
import base64
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
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

# Whether the DEVICE python can run the in-process fast-resolve zipapp at all. yt-dlp
# requires Python >= 3.10 (verified 2026-09: SFOS 5.1 ships 3.11 — fine; SFOS <= 5.0 ships
# 3.8 — never). The frozen binary bundles its OWN modern Python, which is exactly why it
# stays the universal default and fallback. Gates the in-process path and the launch-time
# zipapp autofetch; bump when upstream moves again — going stale is safe (the import just
# fails once and every resolve takes the binary).
_FAST_RESOLVE_PY_OK = sys.version_info >= (3, 10)

# Flags applied to every network-facing yt-dlp call. -4 forces IPv4: dual-stack connects
# can hang when a network advertises IPv6 routes it can't actually carry.
# (This is where PO-token / player-client args will accrue in M2.)
_COMMON_ARGS = ("-4",)


# --------------------------------------------------------------------------- #
# Authenticated extraction: hand yt-dlp the imported YouTube login as cookies.
# The session comes from the optional `ytm` module (import_browser_login reads the Sailfish
# Browser's cookie jar). It is materialised to an EPHEMERAL, owner-only temp file per yt-dlp call
# and removed straight after — there is never a persistent plaintext cookies file on disk, and a
# per-call file means parallel calls (the subscription feed) never share/clobber one cookie jar.
# --------------------------------------------------------------------------- #

def _write_cookies_temp():
    """Write the imported YouTube login (if any) to a fresh 0600 cookies.txt and return its path,
    or "" when signed out / the ytm module isn't present (FinTube without a login). The CALLER
    must remove the file when the yt-dlp call finishes."""
    text = ""
    try:
        import ytm
        text = ytm.netscape_cookies()
    except Exception:
        text = ""
    if not text:
        return ""
    fd, path = tempfile.mkstemp(prefix="ytdlp-ck-", suffix=".txt")   # mkstemp creates it 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass
        return ""
    return path


@contextlib.contextmanager
def _cookies_args():
    """Yield ["--cookies", <ephemeral file>] for authenticated extraction (age-gated / members /
    premium content, fewer bot-wall 403s) when a YouTube login is imported, else []. The temp file
    lives only for the `with` block. Used to splice *cargs into a yt-dlp argv right after
    *_COMMON_ARGS. For a long-lived Popen (download) call _write_cookies_temp() directly instead."""
    path = _write_cookies_temp()
    try:
        yield (["--cookies", path] if path else [])
    finally:
        if path:
            try:
                os.remove(path)
            except Exception:
                pass

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


def _is_manifest(f):
    """1 if this is an HLS/manifest variant (m3u8) rather than a direct progressive/DASH URL, else 0.
    Some clients expose BOTH at every resolution; the direct URL is preferred as a pure tiebreaker —
    it needs no manifest round-trip before media (faster preroll) and rides the app's proven
    range-seekable proxy path (proxying a manifest is the fragile case the proxy code warns about).
    Without this the manifest variant can win the pick purely by list order (measured 2026-09-06:
    the anonymous token-free primary picked manifest v=617 over the direct equivalent)."""
    return 1 if ("m3u8" in (f.get("protocol") or "").lower()
                 or "manifest.googlevideo" in (f.get("url") or "")) else 0


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
        return (-(f.get("height") or 0), codec_rank, f.get("fps") or 0, _is_manifest(f))
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


def _is_drc(f):
    """1 for a DRC ('stable volume' / dynamic-range-compressed) audio track, else 0. YouTube marks
    these with a `-drc` format_id suffix (some clients — e.g. web_embedded — expose them alongside the
    normal tracks). Used only as a tie-break so the ORIGINAL dynamics win over the normalized variant
    when both are offered at the same language+codec; DRC is still picked if it's all that's on offer."""
    return 1 if ("drc" in (f.get("format_id") or "").lower()
                 or "drc" in (f.get("format_note") or "").lower()) else 0


# Bitrate-tier words yt-dlp appends to an audio format_note ("German, low"). Stripped to leave
# the bare language name for the audio picker.
_AUDIO_TIER_RE = re.compile(r",\s*(?:ultralow|low|medium|high)\s*$", re.I)


def _audio_lang_name(f):
    """Human language label for an audio track. yt-dlp's format_note is already a localized
    language name followed by a bitrate tier — 'German, low', 'Chinese (Simplified), medium',
    'English original (default), low'. Strip the trailing tier and the 'original (default)'
    role marker (surfaced separately via is_original); fall back to the language code."""
    note = _AUDIO_TIER_RE.sub("", (f.get("format_note") or "").strip())
    note = re.sub(r"\s*\boriginal\b", "", note, flags=re.I)
    note = re.sub(r"\s*\(default\)", "", note, flags=re.I)
    note = note.strip().strip(",").strip()
    # A note that was ONLY a tier word (no comma — e.g. "medium") slips past the tier regex above;
    # reject a bare tier so it never masquerades as a language name.
    if re.fullmatch(r"(?:ultralow|low|medium|high)", note, flags=re.I):
        note = ""
    return note or (f.get("language") or "").strip() or "Audio"


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
        # bitrate; then CODEC (opus first), then bitrate. Opus is preferred OVER bitrate because
        # Opus/WebM audio flows through matroskademux, which PUSH-seeks over the range-seekable proxy
        # exactly like the WebM/VP9 video — whereas AAC/M4A goes through qtdemux, whose push-mode seek
        # returns FALSE on this SFOS/libhybris GStreamer (the "audio= 0" desync), forcing a whole-file
        # audio downloadbuffer that grinds before every preroll. Opus keeps BOTH branches push-mode:
        # fast preroll + A/V-synced seeks, no downloadbuffer. (Opus 251 ~160k >= AAC 140 ~128k, so
        # this rarely costs quality; falls back to AAC when no opus track exists.)
        # Then non-DRC before DRC: within the same language+codec, the ORIGINAL dynamics beat the
        # loudness-normalized ("stable volume") variant that some clients (web_embedded) also expose;
        # placed AFTER codec so we never trade the opus push-seek win for a non-DRC AAC track, and
        # a DRC track is still chosen when it's the only one offered.
        return (-_audio_orig_pref(f), codec_rank, _is_drc(f), -abr, _is_manifest(f))
    cands.sort(key=key)
    return cands


# --------------------------------------------------------------------------- #
# Local media proxy: injects a browser User-Agent and forwards byte ranges.
# --------------------------------------------------------------------------- #

_proxy_port = None
_proxy_lock = threading.Lock()
_ipv4_forced = False

# --- Download-backed streaming substrate ------------------------------------- #
# A per-(video,itag) job streams googlevideo bytes (in-process _DirectFetch by default; the
# yt-dlp child as fallback — see _spawn); the reader thread pwrites them into a temp file and
# advances an in-process `edge` counter; do_GET serves preads gated by `edge`. Backpressure is
# end-to-end either way: the reader only pulls when the read-ahead gate is open (GStreamer
# buffer full -> wfile.write blocks -> cursor stops advancing -> reader stops pulling -> the
# fetch pauses / the child blocks on its pipe), so disk stays bounded with no SIGSTOP /
# --limit-rate machinery. `edge` is OUR counter (bytes we actually pwrote), never getsize(),
# so a read can never see a byte we didn't place.
_SESS_CHUNK   = 256 << 10    # pipe read / pwrite unit
_READAHEAD    = 32 << 20     # download at most this far past the play cursor (the read-ahead cap)
_SEEK_SOON    = 4  << 20     # forward seek within this of edge -> block; beyond -> restart
_KEEPBACK     = 8  << 20     # bytes kept behind the cursor for cheap short backward seeks (D3)
_IDLE         = 25.0         # reap a stream idle (refs==0) this long
_REAP_EVERY   = 5.0
_STALL        = 120.0        # _wait gives up if edge hasn't advanced this long (D8 backup watchdog);
                             # must exceed the ~90s _ytdlp_formats re-resolve timeout — real in-download
                             # stalls are caught by --socket-timeout 30, not by this backstop.
_MAX_STREAMS  = 8
_MIN_FREE     = 300 << 20
_RESUME_TRIES = 3            # cap on CONSECUTIVE no-progress pipe deaths (reset on progress, D5/R9)
# FALLOC_FL_* literals (Linux; not exposed as os.* names) — reclaim the consumed prefix in place (D3)
_FALLOC_KEEP  = 0x01         # FALLOC_FL_KEEP_SIZE
_FALLOC_PUNCH = 0x02         # FALLOC_FL_PUNCH_HOLE
_PUNCH_OK     = True         # cleared on the first fallocate failure -> degrade to full-file
# Range-restart resume: on-device Range test PASSED 2026-09-03 — the frozen yt-dlp FORWARDS
# --add-header "Range: bytes=N-" on a direct-URL `-o -` download (reported total = clen - N, no 403),
# so resuming AT s.edge is safe and gives snappy seeks/resume. This is the shipped mode: the resume
# path spawns at s.edge and never resets edge/origin to 0, which by construction keeps disk bounded
# by the do_GET hole-punch during resume too (eliminates R8's balloon and R9's edge-reset problem).
# Keep the False branch as a DOCUMENTED FALLBACK ONLY: it re-downloads from 0 (offsets stay
# corruption-proof), can grow the temp file during a deep resume, and relies on the reader's
# free-space fail-safe to turn a would-be device-fill into a clean FAIL — never the shipped mode.
_RANGE_RESTART = True
# In-process direct fetch (default): the download job streams googlevideo via urllib INSIDE
# this process instead of spawning the frozen yt-dlp binary per job — the same ~1.3s spawn tax
# fast-resolve removed from resolve() was still paid on every playback start (twice: video +
# audio jobs) and on every mid-stream resume. _DirectFetch mirrors the child's proven behaviour
# (headers, IPv4, 30s socket timeout, bounded 10M Range chunks for the burst-window speedup)
# behind the exact proc surface _reader/_reap expect. Fallback doctrine (see _spawn/_reader): a
# stream whose direct fetch dies at byte 0 flips to the binary child for its remaining life —
# worst case is the status quo plus one failed HTTPS round-trip. Set False to force the child.
_DIRECT_STREAM = True
_DIRECT_CHUNK  = 10 << 20    # bounded Range chunk (== the child's --http-chunk-size 10M)
# A freshly minted PO-token URL (pot=) is refused with 403 for its first ~3 s, then served.
# Measured 2026-10-22 on a PC (mweb + bgutil 1.3.2 and 2.0.2, every run): 403 until 2.6-3.1 s
# after extraction, then 206. yt-dlp's own downloads never see it; playback asks at once. The
# direct fetch keeps retrying a YOUNG token URL's first request through this window instead of
# dying into a re-resolve, whose fresh URL would be just as young and refused the same way.
_POT_WARMUP = 6.0            # seconds a young token URL's first request keeps being retried
_POT_WARMUP_STEP = 0.5       # pause between those retries
_GV_URL_LIFETIME = 6 * 3600  # googlevideo URLs expire 6 h after issue, so expire= dates them
_streams = {}                # (video_id, itag) -> _Stream
_streams_lock = threading.Lock()
_reap_pending = []           # R3/R5: Popen zombies to wait() OFF-lock, drained by _reaper + atexit
_reap_lock = threading.Lock()
_STREAM_DIR = None           # <data_dir>/streamcache, set in _ensure_proxy


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


_plog_t0 = None
def _plog(msg):
    # DIAG: prefix every line with seconds since the first log line, so REQ->DONE gaps expose
    # yt-dlp cold-start latency vs slow throughput (wrote / elapsed) directly.
    global _plog_t0
    if not _DEBUG:
        return
    try:
        if _plog_t0 is None:
            _plog_t0 = time.monotonic()
        with open("/tmp/youfish-proxy.log", "a") as fh:
            fh.write("[%7.2f] %s\n" % (time.monotonic() - _plog_t0, msg))
    except Exception:
        pass


def _tlog(msg):
    """Timing trace to stdout (visible under YOUFISH_DEBUG, like ytm's [ytm] lines) — for
    profiling start latency. Cheap; compiled out in normal use by the _DEBUG gate.
    Wall-clock stamped so field logs expose the *gaps between* lines (a stall in untimed
    code is invisible to per-step durations alone)."""
    if _DEBUG:
        try:
            now = time.time()
            print("[youfish/t %s.%03d] %s"
                  % (time.strftime("%H:%M:%S", time.localtime(now)), int(now % 1 * 1000), msg))
        except Exception:
            pass


def _timed_fn(label):
    """Decorator that logs a query function's total wall time (label + seconds) under YOUFISH_DEBUG.
    When debug is OFF it returns the function UNWRAPPED — literally zero overhead in normal use. Used
    to profile every user-facing yt-dlp/network query on-device (grep the log for `[youfish/t] q.`).
    Internal calls resolve to the wrapped module global too, so nested paths (feed workers) are timed.
    """
    def deco(fn):
        if not _DEBUG:
            return fn

        def wrapper(*a, **kw):
            _t0 = time.time()
            try:
                return fn(*a, **kw)
            finally:
                _tlog("%s %.2fs" % (label, time.time() - _t0))
        wrapper.__name__ = getattr(fn, "__name__", "fn")
        return wrapper
    return deco


def _spawn_tax_probe():
    """Measure the pure yt-dlp cold-start spawn tax: `yt-dlp --version` does ~no real work, so its
    wall time is almost entirely process launch (unpack the frozen binary + boot CPython + import the
    yt_dlp tree). Logged once per launch under YOUFISH_DEBUG so the log shows how much of EVERY query
    is just the spawn — the #1 number for deciding whether in-process / a daemon is worth it."""
    if not _DEBUG:
        return
    path = _ytdlp_path()
    if not path:
        _tlog("spawn_tax: yt-dlp not found")
        return
    best, ver = None, "?"
    for _ in range(3):                       # min of a few runs → the warm-FS best case, the fair floor
        _t0 = time.time()
        try:
            ver = (subprocess.run([path, "--version"], capture_output=True, text=True,
                                  timeout=30).stdout or "?").strip()
        except Exception as ex:
            _tlog("spawn_tax: probe failed (%s)" % ex)
            return
        dt = time.time() - _t0
        best = dt if best is None else min(best, dt)
    _tlog("spawn_tax %.2fs  (min of 3x `yt-dlp --version`; ~pure process launch) — yt-dlp %s"
          % (best, ver))


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


def _probe_url_ok(url, ua, timeout=3):
    """Fast pre-flight for the token-free→token fallback (token-free primary → mweb): does this
    googlevideo URL actually SERVE bytes, or 403 at byte 0? A gated token-free stream looks fine
    at resolve but 403s the instant the player fetches it, so resolve probes one chosen URL and,
    on a real 403, re-extracts with the token client BEFORE playback. Returns False ONLY on a definite HTTP 403 (the escalation trigger);
    True on 2xx AND on any ambiguous failure (timeout / DNS / other HTTP code) — we never escalate to
    the slower client on a maybe, so a flaky network can't make resolve pay for BOTH clients. One tiny
    Range: bytes=0-1 GET, forced IPv4, with the SAME UA the player will use (a mismatched UA 403s on
    its own and would be a false trigger)."""
    if not url:
        return True
    try:
        _force_ipv4()
        req = urllib.request.Request(url, headers={"User-Agent": ua or _BROWSER_UA,
                                                   "Range": "bytes=0-1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(2)
            return True
    except urllib.error.HTTPError as ex:
        return ex.code != 403
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# Per-(video_id, itag) download job. The HTTP connection is ephemeral (every seek is a fresh
# do_GET, since we answer Connection: close); the download JOB persists here across connections,
# so a backward / nearby-forward seek is served from disk instead of re-fetching from zero.
# Invariant: [origin, edge) is always contiguous and fully valid; the reader only advances edge.
# Lock order is ALWAYS _streams_lock -> s.cond. do_GET takes s.cond ALONE (never nests
# _streams_lock under it). refs lives under s.cond. No proc.wait() ever runs under _streams_lock.
# --------------------------------------------------------------------------- #
class _Stream:
    def __init__(s, vid, itag, url, ua, total):
        s.vid, s.itag, s.url, s.ua = vid, itag, url, ua
        s.total = total                    # _clen(url): authoritative total, known up front
        s.path = os.path.join(_STREAM_DIR, "s-%s-%s-%d.dat"
                              % (vid, itag, int(time.time() * 1000)))
        s.fd = os.open(s.path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        s.origin = 0                       # first valid byte of the live segment (advances on reclaim)
        s.edge = 0                         # one past the last byte we've pwritten
        s.cursor = 0                       # furthest byte any connection has served (read-ahead gate)
        s.dl_start = 0                     # content offset the current yt-dlp proc streams FROM (D10)
        s.state = "RUN"                    # RUN | DONE | FAIL | DEAD
        s.refs = 0                         # R7/R10: mutated ONLY under s.cond
        s.last_active = time.time()
        s.edge_ts = time.time()            # last time edge advanced -> stall watchdog (D8)
        s.cursor_at_last_death = 0         # R9: cursor at the previous pipe death -> resets tries
        s.cond = threading.Condition()
        s.proc = None
        s.use_binary = False               # flipped when a direct fetch dies at byte 0 -> child
        s.gen = 0                          # fences a stale reader across a restart


def _free_bytes(path):
    """Free bytes on the filesystem holding `path`. On error return a huge number so a statvfs
    hiccup never wedges playback on a free-space guess (the reader's periodic re-check, D3, is the
    real device-full guard)."""
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except Exception:
        return 1 << 62


def _reap_proc(proc):
    """Best-effort wait() on an exited / killed child so it can't linger as a zombie. Called ONLY
    off any lock: _reader's inline resume reap (holds no lock), _reader's R4 self-kill, and the
    atexit sweep. _reap_locked NEVER calls this (R3/R5) — it queues to _reap_pending instead."""
    if not proc:
        return
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _gv_request(url, ua, start, end):
    """A googlevideo Range GET with the proven 6-header set (see _spawn) and the format's own UA."""
    return urllib.request.Request(url, headers={
        "User-Agent": ua or _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-us,en;q=0.5",
        "Sec-Fetch-Mode": "navigate",
        "Accept-Encoding": "identity",
        "Range": "bytes=%d-%d" % (start, end),
    })


class _DirectFetch:
    """In-process googlevideo streamer — a duck-typed stand-in for the yt-dlp child (_spawn):
    same `.stdout.read(n)` / `.kill()` / `.poll()` / `.wait()` surface, so _reader and the reap
    machinery run unchanged. Exists because every playback start (and every mid-stream resume)
    paid the same ~1.3s frozen-binary spawn tax that fast-resolve removed from resolve() — twice
    per video (video + audio jobs). Mirrors what the child did for an already-resolved DIRECT
    URL: the proven 6-header set with the format's own UA, forced IPv4, a 30s socket timeout,
    and — the load-bearing part — BOUNDED ~10M Range GETs per chunk (the --http-chunk-size
    trick: each bounded request re-enters googlevideo's full-speed burst window, where one
    open-ended GET gets paced down to ~playback bitrate; on-device 2026-09-03: 0.58->10 MB/s).

    Error surface: read() returns b"" at clean EOF *and* on any failure — exactly a child pipe
    closing — so _reader's existing resume machinery (re-resolve, R9 tries cap, D5) handles
    both; this object never retries what it can't fix (it has no way to re-resolve a URL).
    A mid-chunk truncation IS self-healed by reopening from the current offset (cheap — no
    process to relaunch), capped so a no-progress loop still dies into the resume path, and so is
    a young PO-token URL's warm-up 403 (_warming_up), which a re-resolve can't fix. kill()
    from the reap paths closes the live response, which unblocks a concurrent read(); a read
    blocked in connect() rides out its own <=30s timeout (the reader re-checks DEAD/gen right
    after, same as a slow child kill today)."""

    def __init__(self, url, ua, at, total):
        _force_ipv4()                # the in-process equivalent of the child's -4
        self.url, self.ua = url, ua
        self.pos = at                # next content offset to fetch (bytes are handed out in order)
        self.total = total           # from clen=; None -> learned from the first 206 Content-Range
        self.resp = None
        self.chunk_end = -1          # last offset of the open bounded chunk; None = open-ended 200
        self.reopens = 0             # consecutive ZERO-PROGRESS reopens (truncation guard)
        self.got = 0                 # bytes handed out so far
        self.warm_until = None       # end of a young token URL's warm-up (see _POT_WARMUP)
        self.returncode = None       # duck: None while live, 0 clean EOF / killed, 1 error death
        self.err = ""                # why it died ("HTTP 403", "timeout", ...) — for the debug log
        self.stdout = self           # _reader drains proc.stdout.read(n)

    def _open_next(self):
        end = self.pos + _DIRECT_CHUNK - 1
        if self.total is not None:
            end = min(end, self.total - 1)
        resp = urllib.request.urlopen(_gv_request(self.url, self.ua, self.pos, end), timeout=30)
        code = getattr(resp, "status", None) or resp.getcode()
        if code == 200:              # server ignored the Range: stream this one response to EOF
            self.chunk_end = None
        else:                        # 206: note the chunk bound; learn the total ("bytes a-b/N")
            self.chunk_end = end
            if self.total is None:
                m = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range", "") or "")
                if m:
                    self.total = int(m.group(1))
        self.resp = resp

    def read(self, n):
        """Next <=n bytes at self.pos; b"" at clean EOF or on any failure (= child pipe close)."""
        while self.returncode is None:
            if self.resp is None:
                if self.total is not None and self.pos >= self.total:
                    self.returncode = 0                   # everything delivered — clean EOF
                    return b""
                try:
                    self._open_next()
                except urllib.error.HTTPError as ex:
                    if ex.code == 403 and self._warming_up():
                        time.sleep(_POT_WARMUP_STEP)              # kill() during it ends the loop
                        continue
                    self.returncode = 0 if ex.code == 416 else 1   # 416: past EOF (no-clen case)
                    self.err = "HTTP %s" % ex.code
                    return b""
                except Exception as ex:
                    self.returncode = 1                   # DNS / TLS / timeout / reset / ...
                    self.err = repr(ex)[:120]
                    return b""
            try:
                buf = self.resp.read(n)
            except Exception:
                buf = b""
            if buf:
                self.pos += len(buf)
                self.got += len(buf)
                self.reopens = 0
                return buf
            try:                                          # response exhausted (or died) — retire it
                self.resp.close()
            except Exception:
                pass
            self.resp = None
            if self.chunk_end is None:                    # open-ended 200 finished -> stream done
                self.returncode = 0
                return b""
            if self.pos > self.chunk_end:                 # bounded chunk fully consumed — normal;
                continue                                  # loop opens the next burst window
            self.reopens += 1                             # truncated mid-chunk: reopen from pos,
            if self.reopens > 3:                          # but never loop on zero progress
                self.returncode = 1
                return b""
        return b""

    def _warming_up(self):
        """A 403 before the first byte on a PO-token URL issued within the last minute: is it
        still inside the warm-up window (_POT_WARMUP)? A token-free, older or already-streaming
        URL never waits: its 403 is a real refusal and goes straight to the resume path."""
        if self.got or "pot=" not in self.url:
            return False
        now = time.time()
        if self.warm_until is None:
            if _expire_ts(self.url) - now < _GV_URL_LIFETIME - 60:
                return False
            self.warm_until = now + _POT_WARMUP
        return now < self.warm_until

    # --- duck-typed child-process surface (for _reap_proc / _reap_locked / _reaper) --- #
    def kill(self):
        self.returncode = 0
        resp, self.resp = self.resp, None
        try:
            if resp is not None:
                resp.close()                              # unblocks a concurrent read()
        except Exception:
            pass

    terminate = kill

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def _spawn(s, at):
    """The download job for bytes [at, ...) of s.url — an in-process _DirectFetch by default,
    the yt-dlp CHILD when this stream flipped to the fallback (or _DIRECT_STREAM is off). Both
    expose the same duck surface (.stdout.read/.kill/.poll/.wait), so _reader and the reap
    machinery are agnostic. `s.url` is an already-resolved DIRECT googlevideo URL (from
    _proxied / _reresolve), so no cookies / PO-token / extractor args belong on either path.

    Child path: mirrors the EXACT 6-header set the retired _fetch proved on-device 2026-09-03
    (a bare request 403s at byte 0, empty body = bot-check) plus --socket-timeout so a stalled
    fetch dies into the resume path. preexec_fn makes the kernel SIGKILL the child if the
    worker dies. With _RANGE_RESTART=True `at` is s.edge on a resume; the frozen yt-dlp
    forwards the Range header (verified), so streamed content offset == at (pwrite offset
    stays correct). (D8, D10)"""
    if _DIRECT_STREAM and not s.use_binary:
        try:
            _plog("spawn direct itag=%s at=%d" % (s.itag, at))
            return _DirectFetch(s.url, s.ua, at, s.total)
        except Exception as ex:                # constructor is offline/lazy; belt-and-braces
            s.use_binary = True
            _plog("direct-fetch init failed (%r) -> binary child" % ex)
    _plog("spawn child itag=%s at=%d" % (s.itag, at))
    argv = [_ytdlp_path(), *_COMMON_ARGS, "--no-playlist",
            "--socket-timeout", "30",
            # googlevideo paces a single open-ended GET down to ~playback bitrate; --http-chunk-size
            # makes yt-dlp issue BOUNDED Range GETs per chunk, each re-entering its full-speed burst
            # window. On-device 2026-09-03: 0.58->10.09 MB/s WiFi, 0.55->1.30 MB/s 4G. Offset-safe:
            # the injected "Range: bytes=<at>-" (below) becomes HttpFD req_start and chunking continues
            # FROM there, so the reader's pwrite offset == content offset (D10) still holds (verified:
            # first 64KB byte-identical to the non-chunked Range fetch, no double-offset on this build).
            "--http-chunk-size", "10M",
            "--user-agent", s.ua,
            "--add-header", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "--add-header", "Accept-Language: en-us,en;q=0.5",
            "--add-header", "Sec-Fetch-Mode: navigate",
            "--add-header", "Accept-Encoding: identity"]
    if at > 0:
        argv += ["--add-header", "Range: bytes=%d-" % at]
    argv += ["-o", "-", "--", s.url]
    return subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            preexec_fn=_set_pdeathsig)


def _reader(s, gen):
    """The SOLE writer of s.fd. Drains yt-dlp's pipe into the temp file; pwrites at the offset the
    proc actually streamed from (dl_start + bytes-read this proc), so the offset ALWAYS equals the
    content offset in BOTH range modes (D10). The read-ahead cap doubles as the backpressure gate.
    On a mid-stream pipe death it re-resolves a fresh URL and resumes. In the SHIPPED mode
    (_RANGE_RESTART=True) resume spawns at s.edge and preserves origin/edge, so the do_GET hole-punch
    keeps disk bounded during resume just like steady playback. The False FALLBACK re-downloads from
    0 (bytes re-pwritten idempotently at the same offsets); it can grow the temp file during a deep
    resume, and only the per-8MiB free-space fail-safe below bounds it — a documented fallback limit."""
    tries = 0
    last_free_edge = 0                                       # edge at the last free-space check (D3)
    wfd = -1
    try:
        with s.cond:
            if s.state == "DEAD" or gen != s.gen:
                return
            try:
                wfd = os.dup(s.fd)
            except OSError:
                wfd = -1
        if wfd < 0:
            with s.cond:
                if s.state not in ("DONE", "DEAD"):
                    s.state = "FAIL"; s.cond.notify_all()
            return
        while True:
            proc = s.proc
            nproc = 0                                        # bytes THIS proc has produced (D10)
            while True:
                with s.cond:                                # read-ahead cap == backpressure
                    got = s.cond.wait_for(lambda: s.state == "DEAD"
                                          or gen != s.gen
                                          or s.edge - s.cursor < _READAHEAD,
                                          timeout=2.0)       # wake on DEAD/gen change or an open gate
                    if s.state == "DEAD" or gen != s.gen:
                        return
                    if not got:              # 2s timeout with the gate STILL closed: we're already
                        continue             # >= _READAHEAD past the play cursor (a paused / slow
                                             # reader). Loop and keep waiting — do NOT read another
                                             # chunk past the cap. Without this the cap was advisory:
                                             # a paused video kept pulling ~1 chunk/2s until the whole
                                             # file was on disk. The timeout now only re-checks DEAD/gen. (M2)
                buf = proc.stdout.read(_SESS_CHUNK)          # blocks on the network; never spins
                if not buf:
                    break
                try:
                    os.pwrite(wfd, buf, s.dl_start + nproc) # D10: offset == content offset streamed
                except OSError:                              # ENOSPC / bad fd -> clean FAIL
                    with s.cond:
                        if s.state not in ("DONE", "DEAD"):
                            s.state = "FAIL"; s.cond.notify_all()
                    return
                nproc += len(buf)
                with s.cond:
                    if s.state == "DEAD" or gen != s.gen:
                        return
                    new_edge = s.dl_start + nproc
                    if new_edge > s.edge:
                        s.edge = new_edge
                        s.edge_ts = time.time()              # D8: mark forward progress
                        s.cond.notify_all()                  # wake do_GETs blocked at the edge
                if s.edge - last_free_edge >= (8 << 20):     # D3 fail-safe: a growing stream can't
                    last_free_edge = s.edge                  #     fill the device (fallback-mode guard)
                    if _free_bytes(_STREAM_DIR) < _MIN_FREE:
                        with s.cond:
                            if s.state not in ("DONE", "DEAD"):
                                s.state = "FAIL"; s.cond.notify_all()
                        return
            # pipe closed: clean finish, our own reap, or mid-stream death (expired URL / 403)
            with s.cond:
                if s.state == "DEAD" or gen != s.gen:
                    return
                if s.total is not None and s.edge >= s.total:
                    s.state = "DONE"; s.cond.notify_all(); return
            if s.total is None:                              # R2: length-unknown (rare no-clen/bare-200)
                with s.cond:
                    if nproc > 0:                            # produced bytes then clean EOF == the end
                        s.state = "DONE"; s.cond.notify_all(); return
                    # nproc == 0 -> a real byte-0 death; fall through to the capped resume path
            with s.cond:
                s.edge_ts = time.time()   # B4: recovery in progress — don't let the stall watchdog abort re-resolve
            _reap_proc(proc)                                 # off-lock reap of the exited child
            if isinstance(proc, _DirectFetch) and _DEBUG:
                _tlog("direct-fetch itag=%s died after %d bytes at=%d: %s [%s]"
                      % (s.itag, nproc, s.dl_start, proc.err or "eof", _url_shape(s.url)))
            refused = False
            if isinstance(proc, _DirectFetch) and nproc == 0:
                # The direct fetch produced NOTHING (403/expired/blocked at byte 0). Flip this
                # stream to the binary child before the resume respawn — the fallback doctrine:
                # worst case becomes exactly the pre-direct behaviour. (The re-resolve below may
                # also hand the next spawn a fresh URL; the child gets first go at it.)
                if _ytdlp_path():                            # no yt-dlp (it's optional since the
                    s.use_binary = True                      # fast path): keep the direct fetch
                    _plog("direct-fetch dead at byte 0 (itag=%s) -> binary child" % s.itag)
                # An unexpired InnerTube-minted URL refused at byte 0 is evidence against the fast
                # path itself on this network — enough of them and resolve goes back to yt-dlp.
                # Only an HTTP answer counts: a network failure is no verdict on the URL. The
                # recovery then skips the fast client, and the cached resolve that handed out
                # this URL is dropped so a replay doesn't start from it again.
                if proc.returncode == 1 and proc.err.startswith("HTTP ") \
                        and _fast_client_url(s.url) and _expire_ts(s.url) > time.time() + 60:
                    refused = True
                    _fast_strike("itag %s refused at byte 0 (%s)" % (s.itag, proc.err))
                    _drop_resolved(s.vid)
            if s.cursor > s.cursor_at_last_death:            # R9: credit REAL playback advance (cursor
                tries = 0                                    #     survives a False resume's edge=0),
            s.cursor_at_last_death = s.cursor                #     cap only genuinely stuck streams
            tries += 1
            if tries > _RESUME_TRIES:
                with s.cond:
                    s.state = "FAIL"; s.cond.notify_all()
                return
            fresh = _reresolve(s.vid, s.itag, s.url, refused)  # reuse the rate-limited 403 refresh
            if fresh and _proxy_url_ok(fresh):
                s.url = fresh
            with s.cond:
                if s.state == "DEAD" or gen != s.gen:
                    return
                if _RANGE_RESTART:                           # shipped: trust the Range, resume at edge
                    s.dl_start = s.edge
                else:                                        # fallback: re-download from 0; bytes
                    s.origin = 0; s.edge = 0; s.dl_start = 0 #   re-pwritten at same offsets. cursor is
                    s.edge_ts = time.time()                  #   preserved (serve position).
            newproc = _spawn(s, s.dl_start)                  # R4: spawn into a local...
            with s.cond:                                     # ...then commit under the DEAD/gen re-check
                if s.state == "DEAD" or gen != s.gen:
                    try: newproc.kill()
                    except Exception: pass
                    _reap_proc(newproc)                      # reader holds no lock -> inline wait ok
                    return
                s.proc = newproc                             # now a reap either kills this or we did
    finally:
        # ANY unhandled path terminates the stream cleanly, so blocked do_GETs wake, refs drain, and
        # the reaper collects it — never leave state RUN behind a dead reader thread.
        if wfd >= 0:
            try: os.close(wfd)
            except OSError: pass
        with s.cond:
            if s.state not in ("DONE", "DEAD"):
                s.state = "FAIL"
                s.cond.notify_all()


def _acquire(vid, itag, url, ua, total, start, why=None):
    """Return the _Stream that will serve bytes from `start`, creating / restarting as needed.
    The ONLY place a seek (re)starts a download. Bumps refs (caller MUST drop it in a
    finally). Returns None at capacity / low disk / nothing to feed it, so do_GET can answer 503;
    the reason is appended to `why` (it becomes the 503's text, which GStreamer logs).
    refs is mutated under s.cond; acquire already holds _streams_lock and takes s.cond AFTER it,
    preserving the _streams_lock -> s.cond order (no deadlock). (D1, D7, R1, R6, R7)"""
    key = (vid, itag)
    with _streams_lock:
        s = _streams.get(key)
        if s and s.state in ("RUN", "DONE") and s.origin <= start <= s.edge + _SEEK_SOON:
            with s.cond:                                   # R1: reuse ONLY live/complete streams
                s.refs += 1                                # R7: refs under s.cond (a FAIL stream falls
                s.last_active = time.time()                #     through below and is rebuilt fresh)
            return s
        if s:                                              # DEAD/FAIL, far-forward, or below-origin
            _reap_locked(s)
            del _streams[key]
        if len(_streams) >= _MAX_STREAMS:
            _reap_one_idle_locked()
        free = _free_bytes(_STREAM_DIR)
        refusal = None
        if len(_streams) >= _MAX_STREAMS:
            refusal = "too many open streams (%d)" % len(_streams)
        elif free < _MIN_FREE:
            refusal = "not enough free storage for the stream cache (%d MB free, needs %d MB)" % (
                free >> 20, _MIN_FREE >> 20)
        elif not _DIRECT_STREAM and not _ytdlp_path():     # D7: never build a _Stream we can't
            refusal = "yt-dlp not installed"               #     feed. The in-process direct fetch
        if refusal:                                        #     needs no yt-dlp: a fast-path resolve
            _plog("acquire refused itag=%s: %s" % (itag, refusal))   # plays without it installed
            if why is not None:
                why.append(refusal)
            return None
        s = None
        try:                                               # D7: guarded construction
            s = _Stream(vid, itag, url, ua, total)         #     no orphaned fd / tempfile / proc
            if _RANGE_RESTART:                             # responsive deep seek: download FROM start
                s.origin = s.edge = s.cursor = start
                s.dl_start = start
                if start:
                    os.ftruncate(s.fd, 0)                  # reclaim; [0,start) stays a free hole
            else:                                          # fallback: download from 0, gate waits for
                s.origin = s.edge = 0                      #   edge to reach the target. cursor=start
                s.dl_start = 0                             #   anchors the read-ahead gate at the play
                s.cursor = start                           #   position (D1) so the reader fills toward it
            s.refs = 1                                     # R7: fresh object, uncontended
            s.gen += 1
            s.edge_ts = time.time()
            s.proc = _spawn(s, s.dl_start)
            threading.Thread(target=_reader, args=(s, s.gen), daemon=True).start()
        except Exception as ex:
            _plog("acquire failed: %r" % ex)
            if why is not None:
                why.append("stream setup failed: %s" % str(ex)[:120])
            if s is not None:
                if s.proc is not None:                     # R6: kill+queue a child spawned before the
                    try: s.proc.kill()                     #     Thread.start() that raised (else it
                    except Exception: pass                 #     blocks on its pipe until pdeathsig)
                    with _reap_lock:
                        _reap_pending.append(s.proc)       # R3/R5: wait() off-lock in the reaper
                try: os.close(s.fd)
                except Exception: pass
                try: os.remove(s.path)
                except OSError: pass
            return None
        _streams[key] = s
        return s


def _wait(s, pos):
    """Bytes readable at `pos` right now, or None at clean EOF / failure / reap / stall. Blocks at
    the live edge until the reader advances past `pos` (woken by its notify; 1 s liveness fallback).
    The wait is ALWAYS timed and every terminal state returns None, so no path blocks forever (D8)."""
    with s.cond:
        while True:
            if s.state == "DEAD":
                return None
            if s.origin <= pos < s.edge:
                return s.edge - pos                        # on disk -> serve now
            if s.state in ("DONE", "FAIL"):
                return None                                # DONE: clean EOF; FAIL: short close (D11)
            if pos < s.origin:
                return None                                # below reclaimed origin (acquire restarts)
            if s.state == "RUN" and pos >= s.edge and time.time() - s.edge_ts > _STALL:
                return None                                # D8: edge stuck at the live edge -> give up
            s.cond.wait(timeout=1.0)


def _reap_locked(s):
    """Tear a stream down. Caller holds _streams_lock and removes the key afterwards. state=DEAD,
    the kill, AND the fd close all happen under s.cond, so a do_GET taking its per-connection os.dup
    under the same cond either dups a still-valid fd or sees DEAD and bails — never dups a closed /
    recycled fd. os.remove is immediate; POSIX keeps the inode alive for every outstanding dup.
    NO proc.wait() here (R3/R5): the killed child is QUEUED to _reap_pending and reaped off-lock by
    the reaper, so a wedged child never stalls the registry while _streams_lock is held. (D2, R3, R5)"""
    with s.cond:
        s.state = "DEAD"
        s.cond.notify_all()                                # wake the reader + every do_GET
        try:
            if s.proc:
                s.proc.kill()
        except Exception:
            pass
        try:
            os.close(s.fd)
        except Exception:
            pass
        try:
            os.remove(s.path)
        except OSError:
            pass
    if s.proc:                                             # R3/R5: append is atomic; no wait() on-lock
        with _reap_lock:
            _reap_pending.append(s.proc)


def _reap_one_idle_locked():
    """Reap the least-recently-active idle (refs==0) stream to free a slot. Caller holds the lock.
    refs / last_active are read under s.cond (R7); a stale read only delays a reap by one cycle."""
    victim = None
    for k, s in _streams.items():
        with s.cond:
            idle = s.refs <= 0
            la = s.last_active
        if idle and (victim is None or la < victim[2]):
            victim = (k, s, la)
    if victim:
        _reap_locked(victim[1])
        del _streams[victim[0]]


def _reaper():
    """Background: drain zombie children off-lock (R3/R5), then reap streams idle (no connection) past
    _IDLE. Steady playback (foreground or background audio) always holds >=1 connection, so it is never
    reaped; a seek drops refs to 0 for milliseconds << _IDLE. refs/last_active read under s.cond (R7)."""
    global _reap_pending
    while True:
        time.sleep(_REAP_EVERY)
        # R3/R5: reap SIGKILLed children here, holding NO lock, so a wedged child never stalls do_GET.
        with _reap_lock:
            pend = _reap_pending; _reap_pending = []
        keep = []
        for p in pend:
            try:
                if p.poll() is None: p.wait(timeout=1)     # brief; SIGKILL usually reaps in ms
            except Exception: pass
            try:
                if p.poll() is None: keep.append(p)        # still not dead -> retry next cycle
            except Exception: pass
        if keep:
            with _reap_lock:
                _reap_pending.extend(keep)
        now = time.time()
        with _streams_lock:
            for k, s in list(_streams.items()):
                with s.cond:                               # R7: read refs/last_active under s.cond
                    reap = s.refs <= 0 and now - s.last_active > _IDLE
                if reap:
                    _reap_locked(s)
                    del _streams[k]


def _sweep_all_streams():
    """Kill every live stream and delete all stream-cache temp files. Run at proxy startup (mop up a
    previous hard crash's leftovers) and via atexit (leave no orphaned yt-dlp child / temp file).
    Drains _reap_pending with a blocking wait too — the process is exiting, so a short wait is fine
    (R3/R5)."""
    with _streams_lock:
        for k, s in list(_streams.items()):
            _reap_locked(s)
            del _streams[k]
    with _reap_lock:
        pend = list(_reap_pending); _reap_pending[:] = []
    for p in pend:
        _reap_proc(p)
    if not _STREAM_DIR:
        return
    import glob
    for p in glob.glob(os.path.join(_STREAM_DIR, "s-*.dat")):
        try:
            os.remove(p)
        except OSError:
            pass


def release_playback(video_id, keep_itags=None):
    """PyOtherSide entry, called from QML teardown (Component.onDestruction, nowPlaying
    stopRequested, switchQuality / switchAudio). Reap every stream for this video whose itag is NOT
    in keep_itags. Reaps regardless of refs — safe because each live do_GET serves from its OWN dup
    (D2). Keyed off video_id, so it is correct even when the departing page tears down after the next
    page's resolve()."""
    keep = {str(i) for i in (keep_itags or [])}
    with _streams_lock:
        for k, s in list(_streams.items()):
            if k[0] == video_id and k[1] not in keep:
                _reap_locked(s)
                del _streams[k]
    return {"ok": True}


class _MediaProxyHandler(http.server.BaseHTTPRequestHandler):
    # libsoup (souphttpsrc) sends HTTP/1.1 requests; answer in kind. Bytes come from a per-itag
    # download job (_Stream): yt-dlp streams into a temp file, we serve preads gated by that file's
    # live edge, so every seek reuses one bounded, backpressured download instead of re-fetching
    # from zero. Body is close-delimited (Connection: close), the framing souphttpsrc accepts.
    # do_GET NEVER takes _streams_lock: it only ever takes s.cond (alone), so the sole global lock
    # ordering in the module stays _streams_lock -> s.cond with no hazard here (R7/R10).
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # keep the app log quiet

    def do_GET(self):
        global _PUNCH_OK
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        target = q.get("u", [None])[0]
        video_id = q.get("v", [None])[0]
        itag = q.get("itag", [None])[0]
        ua = q.get("ua", [None])[0] or _BROWSER_UA  # the format's client UA (googlevideo is UA-bound)
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
        total = _clen(target)  # googlevideo's full length, straight from the URL (authoritative)
        _plog("REQ itag=%s range=%s total=%s" % (itag, raw_range or "none", total))
        if total is not None and start >= total:
            # A seek to (or past) the end: nothing to serve. 416 + "*/total" is what souphttpsrc
            # turns into a clean EOS; a 206 here would carry a negative Content-Length.
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % total)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return

        why = []
        s = _acquire(video_id, itag, target, ua, total, start, why)
        if s is None:   # the reason rides in the status text: it's what GStreamer's error shows
            msg = "no stream capacity: " + (why[0] if why else "unknown")
            self.send_error(503, msg.encode("ascii", "replace").decode())   # status line is latin-1
            return

        # D1 (WARM reuse): anchor the read-ahead gate at (or past) our start. A forward seek into
        # (edge, edge+_SEEK_SOON] reuses a stream whose cursor still sits below edge; without this
        # the reader stays pinned at its cap and we'd block forever.
        with s.cond:
            if start > s.cursor:
                s.cursor = start
                s.cond.notify_all()

        # D2: take our OWN dup of the fd, under s.cond, re-checking the stream wasn't just reaped.
        # Every os.pread / os.fallocate below uses cfd; POSIX keeps the inode alive until we close it
        # in finally, so a concurrent _reap_locked (release_playback / seek restart) can never make
        # us touch a recycled fd.
        cfd = -1
        with s.cond:
            if s.state != "DEAD":
                try:
                    cfd = os.dup(s.fd)
                except OSError:
                    cfd = -1
        if cfd < 0:
            with s.cond:                     # R7/R10: refs under s.cond (do_GET never takes _streams_lock)
                s.refs -= 1
                s.last_active = time.time()
            self.send_error(503, "stream gone")
            return

        try:
            # Framing: 206 + Content-Range/Content-Length when the client sent a Range and total is
            # known; 200 + Content-Length when total known and no Range; bare close-delimited 200
            # when total is unknown (no clen=, non-seekable). Content-Type is generic — decodebin
            # typefinds the container. NOTE (D11): if a stream goes FAIL after these headers are sent,
            # the body closes short (truncated 206); inherent, minimised by the D5/R9 resume logic.
            ctype = "application/octet-stream"
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
            written = 0
            while total is None or pos < total:
                n = _wait(s, pos)                # readable bytes at pos, or None at EOF/fail/reap/stall
                if n is None:
                    break
                data = os.pread(cfd, min(_SESS_CHUNK, n), pos)   # D2: from our private dup
                if not data:
                    break
                self.wfile.write(data)
                pos += len(data)
                written += len(data)
                with s.cond:
                    if pos > s.cursor:           # advance the read-ahead gate -> reader may fetch on
                        s.cursor = pos
                        s.cond.notify_all()
                    # R7/R10: reclaim the consumed prefix in place, but ONLY when THIS is the sole
                    # connection (s.refs == 1), checked atomically with the punch under s.cond. refs
                    # can only rise to 2 by another thread taking s.cond, so no second reader can
                    # appear mid-punch; with refs==1 there is exactly one reader (this do_GET) at
                    # pos==cursor, so punching [origin, cursor-_KEEPBACK) never touches a live byte.
                    # refs>1 (transient seek overlap) simply skips the punch until it drops back to 1:
                    # bounded extra disk, never zeros. Best-effort on cfd (a valid dup even if s.fd
                    # was just reaped); disabled permanently on first failure. (D3)
                    if _PUNCH_OK and s.refs == 1 and pos - s.origin > _KEEPBACK:
                        new_origin = pos - _KEEPBACK
                        try:
                            os.fallocate(cfd, _FALLOC_PUNCH | _FALLOC_KEEP,
                                         s.origin, new_origin - s.origin)
                            s.origin = new_origin
                        except Exception:
                            _PUNCH_OK = False   # degrade to full-file; never crash
                    s.last_active = time.time()
            self.wfile.flush()
            _plog("DONE start=%d wrote=%d" % (start, written))
        except (BrokenPipeError, ConnectionResetError):
            _plog("CLIENT-CLOSED start=%d" % start)   # player seeked/stopped — normal; stream kept
        except Exception as ex:
            _plog("proxy error start=%d: %r" % (start, ex))
            self.close_connection = True
        finally:
            try:
                os.close(cfd)                   # D2: release our dup; inode freed at the last close
            except OSError:
                pass
            with s.cond:                        # R7/R10: refs under s.cond, single-lock, no _streams_lock
                s.refs -= 1
                s.last_active = time.time()


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def _ensure_proxy():
    """Start the localhost proxy once; return its port. Also prepares the stream cache
    (<data_dir>/streamcache), sweeps any temp files a prior hard crash left, starts the idle reaper,
    and registers an atexit sweep so no yt-dlp child or temp file is ever orphaned. IPv4 pinning:
    _DirectFetch calls _force_ipv4() itself (the in-process equivalent of the child's -4)."""
    global _proxy_port, _STREAM_DIR
    with _proxy_lock:
        if _proxy_port:
            return _proxy_port
        if _DEBUG:
            try:
                open("/tmp/youfish-proxy.log", "w").close()  # fresh log each app run
            except Exception:
                pass
        _STREAM_DIR = os.path.join(_data_dir(), "streamcache")
        try:
            os.makedirs(_STREAM_DIR, exist_ok=True)
        except Exception:
            pass
        _sweep_all_streams()  # remove s-*.dat left behind by a previous hard crash
        server = _ThreadingHTTPServer(("127.0.0.1", 0), _MediaProxyHandler)
        _proxy_port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        threading.Thread(target=_reaper, daemon=True).start()
        atexit.register(_sweep_all_streams)
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


@_timed_fn("q.formats")
def _ytdlp_formats(video_id, anon=False):
    """Run yt-dlp and return {itag: direct_url} for every format that has a URL.

    anon=True mirrors resolve()'s PRIMARY dump — token-free AND cookie-free — so the refreshed
    map comes from the same client + auth posture that produced the playing URLs (same itag
    shapes) and dodges the authenticated-token-free gating the anonymous-primary fix proved
    (see _dump). anon=False is the reliable safety net: cookies (restricted content) + a
    minted PO token, exactly like resolve()'s fallback dumps."""
    path = _ytdlp_path()
    if not path or not video_id:
        return {}
    url = video_id if "://" in video_id else "https://www.youtube.com/watch?v=" + video_id
    _ensure_pot_server()  # a fresh URL is just as PO-gated; keep the token sidecar warm
    if _DEBUG and _pot_active():   # gate state on the WARM (self-heal) side, to compare with resolve's
        _tlog("reresolve gate: port=%s http=%r" % (_pot_ready_on_port(), _pot_http_ping(0.5)["ok"]))
    try:
        with (contextlib.nullcontext([]) if anon else _cookies_args()) as cargs:
            proc = subprocess.run([path, *_COMMON_ARGS, *cargs, *_pot_ytdlp_args(),
                                   *_yt_extractor_args(want_pot=not anon, cookies=bool(cargs)),
                                   "--dump-single-json", "--", url],
                                  capture_output=True, text=True, timeout=90,
                                  preexec_fn=_set_pdeathsig)   # D6: die with the app if abandoned
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout)
        out = {}
        for f in data.get("formats", []):
            fid, u = f.get("format_id"), f.get("url")
            if not fid or not u:
                continue
            out[fid] = u
            # yt-dlp numbers same-itag dub tracks by position (251-8); the fast path names them
            # by track id (251-es.3). Also key each by language so a recovery that crosses from
            # one path to the other still finds its track (see _track_alias).
            if f.get("language") and "-" in fid:
                base = fid.split("-")[0] + ("-drc" if "-drc" in fid else "")
                out.setdefault("%s@%s" % (base, f["language"]), u)
        return out
    except Exception:
        return {}


def _track_alias(itag):
    """The language key _ytdlp_formats adds for a dub track: "251-es.3" → "251@es"."""
    m = re.fullmatch(r"(\d+(?:-drc)?)-([A-Za-z]+(?:-[A-Za-z]+)*)(?:\.\d+)?", itag or "")
    return "%s@%s" % (m.group(1), m.group(2)) if m else None


def _reresolve(video_id, itag, failed_url, refused=False):
    """Fresh direct URL for (video_id, itag), re-running yt-dlp at most once per stale
    generation. Concurrent video+audio 403s share one refresh: whoever takes the lock
    first re-extracts; the other sees a cached URL that differs from its failed one and
    reuses it without a second yt-dlp run.

    Mirrors resolve()'s two-step strategy: an ANONYMOUS token-free dump first (the posture
    that produced the playing URLs on the common path — same itag shapes, and immune to the
    authenticated-token-free gating), then the cookie'd + PO-token dump only when the
    anonymous pass didn't yield THIS itag (restricted content, or URLs born from the mweb
    gated-fallback whose shapes a token-free dump may not reproduce). Both dumps count
    against the spawn rate-limit; worst case this holds _url_cache_lock across two 90s
    dumps — acceptable for a rare recovery path where reliability beats latency.

    `refused`: googlevideo refused the failed URL, an unexpired fast-path one, at byte 0. Asking
    the same client again only hands back another URL it refuses (on device: three rounds, ~6s,
    each probing fine), so this skips the fast client and any cached fast URL and goes to yt-dlp.
    """
    alias = _track_alias(itag)

    def pick(fmts):                     # this itag, or the same dub track under yt-dlp's naming
        return fmts.get(itag) or (fmts.get(alias) if alias else None)

    with _url_cache_lock:
        ent = _url_cache.get(video_id)
        if ent and time.time() - ent["ts"] < _URL_CACHE_TTL:
            cached = pick(ent["fmts"])
            if cached and cached != failed_url and not (refused and _fast_client_url(cached)):
                return cached  # another track already refreshed this generation
        now = time.time()
        _reresolve_spawns[:] = [t for t in _reresolve_spawns if now - t < _RERESOLVE_WINDOW]
        if len(_reresolve_spawns) >= _RERESOLVE_BURST:
            _plog("reresolve rate-limited (%d in %.0fs)" % (len(_reresolve_spawns), _RERESOLVE_WINDOW))
            return None
        _reresolve_spawns.append(now)
        # A stream the InnerTube fast path started: re-ask the same client first (one cheap POST,
        # same itag naming + UA) before paying for a yt-dlp run.
        fresh = _fast_formats_map(video_id) if _fast_client_url(failed_url) and not refused else {}
        if fresh.get(itag) and not _fast_urls_ok([fresh[itag]], _it().player_client()[2]):
            fresh = {}
        src = "innertube" if fresh.get(itag) else (_default_client() or "auto")
        if not fresh.get(itag):
            fresh = _ytdlp_formats(video_id, anon=True)
        if not pick(fresh):
            if len(_reresolve_spawns) < _RERESOLVE_BURST:      # the fallback spawn pays the limit too
                _reresolve_spawns.append(time.time())
                fresh2 = _ytdlp_formats(video_id)              # cookie'd + minted-token safety net
                if fresh2:
                    fresh = {**fresh, **fresh2}                # merged map still serves the other track
            else:
                _plog("reresolve token fallback rate-limited")
        if not fresh:
            return None
        if _DEBUG:   # the WARM re-resolve's token + client for the exact itag that 403'd
            _tlog("reresolve itag=%s %s client=%s"
                  % (itag, _pot_of(pick(fresh) or ""), src))
        _url_cache[video_id] = {"ts": time.time(), "fmts": fresh}
        if len(_url_cache) > _URL_CACHE_MAX:  # evict oldest beyond the cap
            for k, _ in sorted(_url_cache.items(),
                               key=lambda kv: kv[1]["ts"])[:len(_url_cache) - _URL_CACHE_MAX]:
                _url_cache.pop(k, None)
        return pick(fresh)


# --------------------------------------------------------------------------- #
# InnerTube fast path (innertube.py): the NewPipe approach — ask YouTube's own JSON API one small
# question instead of running yt-dlp. Used IN FRONT of yt-dlp for resolve, the 403 self-heal,
# channel listings, avatars and the feed's duration/Shorts classification; every caller falls back
# to the yt-dlp path that existed before on ANY failure, so the worst case is the old behaviour
# plus one cheap failed request. On-device yt-dlp costs a 1.3s spawn or a multi-second in-process
# HTML parse per question; the InnerTube answer is one ~100-300 ms POST.
# --------------------------------------------------------------------------- #
_INNERTUBE = {"mod": None, "tried": False}
# Circuit breaker for the fast PLAYER: a bot-check answer means the client/visitor is burned (not
# that this one video is restricted), so after a couple in a row stop trying for a while — every
# resolve would otherwise pay a doomed request before its yt-dlp fallback.
_FAST_PLAYER_BOT_LIMIT = 2
_FAST_PLAYER_COOLDOWN = 30 * 60
_fast_player_state = {"bot_fails": 0, "off_until": 0.0, "strikes": []}
# The same breaker also trips on googlevideo REFUSING fast-path URLs (probe or byte-0 403): the
# request shape is fine on most networks, but if this one rejects it, yt-dlp's URLs are the cure.
_FAST_URL_STRIKES = 2
_FAST_URL_WINDOW = 10 * 60


def _it():
    """The innertube module, or None when it isn't shipped (the shared engine also runs in FinTune)
    or failed to import — callers then go straight to yt-dlp."""
    if not _INNERTUBE["tried"]:
        # Before its pool can open a single connection: a pooled IPv6 socket would mint player
        # URLs bound to an address the (IPv4-pinned) proxy never fetches from.
        _force_ipv4()
        try:
            import innertube as _m
            _INNERTUBE["mod"] = _m
        except Exception as ex:
            _plog("innertube unavailable (%s) — yt-dlp only" % ex)
        _INNERTUBE["tried"] = True
    return _INNERTUBE["mod"]


_it_fallbacks = {}             # area -> [count, last reason]: InnerTube answers yt-dlp had to cover


def _it_note(area, ex):
    """Count an InnerTube fallback that points at OUR side (a layout or API change the parsers
    don't know yet), for the diagnostics report. Unplayable videos and network errors aren't
    counted: those fall back for reasons no parser update would fix."""
    it = _INNERTUBE.get("mod")
    if it is None or not isinstance(ex, it.InnertubeError) or isinstance(ex, it.Unplayable):
        return
    ent = _it_fallbacks.setdefault(area, [0, ""])
    ent[0] += 1
    ent[1] = str(ex)[:120]


def _it_fallback_report():
    """Diagnostics lines for the InnerTube fast path."""
    it = _it()      # not _INNERTUBE["mod"]: that is None until the first caller imported it
    if it is None:
        return ["InnerTube fast path: unavailable (yt-dlp only)"]
    try:
        client = it.player_client()[0]
    except Exception:
        client = "?"
    L = ["InnerTube fast path: player client %s" % client]
    if not _it_fallbacks:
        L.append("  no layout fallbacks this session")
    for area in sorted(_it_fallbacks):
        n, why = _it_fallbacks[area]
        L.append("  %-12s fell back to yt-dlp %dx (last: %s)" % (area, n, why))
    if _it_fallbacks:
        L.append("  (repeated fallbacks = YouTube changed a page layout: the app keeps working via "
                 "yt-dlp, a newer innertube.py makes it fast again)")
    return L


def _fast_player():
    """The innertube module if the fast PLAYER path may run now, else None. A user-chosen
    player_client is an explicit troubleshooting choice that only yt-dlp can honour, so it
    disables the fast path; so does the bot-check circuit breaker."""
    it = _it()
    if it is None:
        return None
    if (get_settings().get("player_client") or "").strip().lower() not in ("", "auto"):
        return None
    if time.time() < _fast_player_state["off_until"]:
        return None
    return it


def _fast_player_raw(video_id):
    """innertube.player_info() with the circuit-breaker bookkeeping, or None on any failure."""
    it = _fast_player()
    if it is None or not re.fullmatch(r"[\w-]{11}", video_id or ""):
        return None
    _force_ipv4()          # googlevideo URLs are IP-bound: extract on the family the proxy streams on
    _t = time.time()
    try:
        data = it.player_info(video_id)
    except Exception as ex:
        if getattr(ex, "bot", False):
            _fast_player_state["bot_fails"] += 1
            if _fast_player_state["bot_fails"] >= _FAST_PLAYER_BOT_LIMIT:
                _fast_player_state["bot_fails"] = 0
                _fast_player_state["off_until"] = time.time() + _FAST_PLAYER_COOLDOWN
                _plog("fast player bot-walled twice — yt-dlp only for %ds" % _FAST_PLAYER_COOLDOWN)
        _it_note("player", ex)
        _tlog("fast player failed %.2fs → yt-dlp: %s" % (time.time() - _t, str(ex)[:120]))
        return None
    _fast_player_state["bot_fails"] = 0
    _tlog("fast player %.2fs (%d formats)" % (time.time() - _t, len(data.get("formats") or [])))
    return data


def _fast_strike(why):
    """One piece of evidence that googlevideo won't serve fast-path URLs on this network. Two
    within _FAST_URL_WINDOW open the same breaker the bot-check uses: resolve goes back to
    yt-dlp (whose URLs this network demonstrably accepted) for _FAST_PLAYER_COOLDOWN."""
    now = time.time()
    st = _fast_player_state
    st["strikes"] = [t for t in st.get("strikes") or [] if now - t < _FAST_URL_WINDOW] + [now]
    _tlog("fast path strike %d/%d: %s" % (len(st["strikes"]), _FAST_URL_STRIKES, why))
    if len(st["strikes"]) >= _FAST_URL_STRIKES:
        st["strikes"] = []
        st["off_until"] = now + _FAST_PLAYER_COOLDOWN
        _tlog("fast player off for %ds — yt-dlp resolves meanwhile" % _FAST_PLAYER_COOLDOWN)


def _probe_status(url, ua, timeout=3):
    """HTTP status of the first request playback will make (same header set, same Range: byte 0
    to the end of _DirectFetch's first bounded chunk), reading only 2 bytes of it, or None when
    the network itself failed (no verdict on the URL). A 2-byte Range was too lenient: on device
    (2026-10-15) four fast-path URLs in a row answered it 206 while playback's own 0-clen request
    got 403 at byte 0 the same second, costing a 12s start."""
    end, total = _DIRECT_CHUNK - 1, _clen(url)
    if total:
        end = min(end, total - 1)
    try:
        _force_ipv4()
        with urllib.request.urlopen(_gv_request(url, ua, 0, end), timeout=timeout) as r:
            r.read(2)
            return getattr(r, "status", None) or r.getcode()
    except urllib.error.HTTPError as ex:
        return ex.code
    except Exception:
        return None


def _fast_urls_ok(urls, ua):
    """Probe fast-path URLs in parallel (~one RTT) before anything plays them. Any HTTP refusal
    is a strike and a False — the caller then uses yt-dlp instead."""
    urls = [u for u in urls if u]
    codes = [None] * len(urls)

    def run(i, u):
        codes[i] = _probe_status(u, ua)
    _t = time.time()
    ts = [threading.Thread(target=run, args=(i, u), daemon=True) for i, u in enumerate(urls)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    ok = all(c is None or 200 <= c < 300 for c in codes)
    _tlog("fast probe %.2fs %s%s" % (time.time() - _t, codes, "" if ok else " → yt-dlp"))
    if not ok:
        _fast_strike("probe answered %s" % codes)
    return ok


def _url_shape(u):
    """DEBUG: the identity-free shape of a googlevideo URL — minting client, IP family, whether it
    carries an n-challenge / PO token, time to expiry, and which params are signed."""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        ip, exp = q.get("ip", [""])[0], _expire_ts(u)
        return "c=%s ip=%s n=%s pot=%s exp=%+ds sparams=%s" % (
            q.get("c", ["?"])[0], "v6" if ":" in ip else ("v4" if ip else "-"),
            "y" if q.get("n") else "-", "y" if q.get("pot") else "-",
            (exp - int(time.time())) if exp else 0, q.get("sparams", [""])[0])
    except Exception:
        return "?"


def _fast_player_data(video_id):
    """yt-dlp-shaped resolve data from ONE InnerTube player request, or None to use yt-dlp. A
    plain on-demand video qualifies with a playable HD pair; a running livestream goes to the live
    client's muxed HLS (_fast_live_data). Upcoming streams and anything without a pair stay on
    yt-dlp. The pair resolve() will actually pick is probed first, so a URL googlevideo refuses
    never reaches the player."""
    data = _fast_player_raw(video_id)
    if data is not None and data.get("live_status") == "is_live":
        return _fast_live_data(video_id)
    if data is None or data.get("live_status") != "not_live":
        return None
    fs = data.get("formats") or []
    _s = get_settings()
    try:
        cap = int(_s.get("default_quality") or 0)
    except (TypeError, ValueError):
        cap = 0
    video, audio = _pick_video(fs, cap), _pick_audio(fs, _s.get("audio_lang") or "")
    if not (video and audio):
        return None
    ua = (video.get("http_headers") or {}).get("User-Agent", "")
    if not _fast_urls_ok([video.get("url"), audio.get("url")], ua):
        return None
    return data


def _fast_live_data(video_id):
    """A running livestream from the live client's muxed HLS master (innertube.live_info), or None
    to use yt-dlp. GStreamer fetches HLS itself (unproxied, invariant 1), so there's no probe: the
    manifest fetch inside live_info already proved googlevideo answers this client here."""
    it = _fast_player()
    if it is None:
        return None
    _t = time.time()
    try:
        data = it.live_info(video_id)
    except Exception as ex:
        _it_note("live", ex)
        _tlog("fast live failed %.2fs → yt-dlp: %s" % (time.time() - _t, str(ex)[:120]))
        return None
    if not _pick(data.get("formats") or [], _MUXED_ITAGS):
        _tlog("fast live: no playable muxed rung → yt-dlp")
        return None
    _tlog("fast live %.2fs (%d variants)" % (time.time() - _t, len(data.get("formats") or [])))
    return data


def _fast_client_url(url):
    """True if a googlevideo URL was minted for the fast path's player client (its `c=` param), so
    a 403 refresh can re-ask the same client and get the same itag shapes + UA back."""
    it = _it()
    if it is None or not url:
        return False
    try:
        cname = it.player_client()[1]
    except Exception:
        return False
    return bool(cname) and re.search(r"[?&]c=%s(?:&|$)" % re.escape(cname), url) is not None


def _fast_formats_map(video_id):
    """{format_id: url} from a fresh fast-path player request ({} on failure) — _reresolve's
    cheap first try for a stream the fast path started."""
    data = _fast_player_raw(video_id)
    if not data:
        return {}
    return {f["format_id"]: f["url"] for f in (data.get("formats") or [])
            if f.get("format_id") and f.get("url")}


def _innertube_prewarm():
    """Mint the player visitor id (and open a pooled keep-alive connection) off the UI path at
    launch, so the first tap-to-play is a single request."""
    it = _it()
    if it is None:
        return
    _tlog("innertube prewarm: python %s, %s" % (sys.version.split()[0], ssl.OPENSSL_VERSION))
    try:
        _force_ipv4()
        it.visitor_data()
    except Exception as ex:
        _plog("innertube prewarm failed: %s" % ex)


# --------------------------------------------------------------------------- #
# Resolve-RESULT cache + single-flight + speculative prefetch.  (REBUILD #1)
#
# Distinct from _url_cache (mid-stream 403 refresh). Stores the FULL {ok, info}
# resolve() payload keyed by (video_id + every setting that changes the output),
# stamped with a freshness deadline from the googlevideo `expire=` in the URLs.
# prefetch_resolve() fills it on a background thread; the cache-first resolve()
# front-door reads it (instant hit), JOINS an in-flight resolve rather than
# double-spawning, else resolves for real and caches a usefully-fresh success.
# --------------------------------------------------------------------------- #
_resolve_cache = {}                        # key -> {"payload": {ok,info}, "good_until": epoch, "ts": epoch}
_resolve_cache_lock = threading.Lock()
_resolve_inflight = {}                     # key -> threading.Event (leader signals joiners)
_RESOLVE_CACHE_MAX = 24
_RESOLVE_CACHE_MAX_TTL = 20 * 60           # never trust an entry longer than this, even if expire is hours out
_RESOLVE_SAFETY = 120                      # drop an entry this many secs BEFORE its URLs actually expire
# D1: join ceiling. _resolve_uncached runs up to THREE subprocess.run(timeout=90) dumps
# (primary + probe→mweb gated fallback + its SABR widen; the hard-fail widen is mutually
# exclusive with the probe path), so ~270s worst case. The joiner must wait PAST that,
# never time out early and launch a second resolve. 300s covers 3x90s + margin.
_RESOLVE_JOIN_TIMEOUT = 300

_prefetch_sema = threading.BoundedSemaphore(2)   # <=2 speculative yt-dlp jobs at once (no swarm)
_prefetch_pending = set()                  # keys queued/running as prefetch (debounce)
_prefetch_lock = threading.Lock()

_EXPIRE_RE = re.compile(r"(?:[?&]|%26|%3F|/)expire(?:=|/|%3D)(\d{9,11})", re.IGNORECASE)

# D10: settings keys that change resolve()'s OUTPUT — a change to any of these drops the cache.
_RESOLVE_OUTPUT_KEYS = ("default_quality", "audio_lang", "hw_decode",
                        "player_client", "pot_provider")


def _expire_ts(u):
    """googlevideo `expire` unix-ts out of a URL — raw OR embedded/quoted in a proxied `u=`
    param (where the real validity clock lives). 0 if none (HLS / odd shape)."""
    if not u:
        return 0
    m = _EXPIRE_RE.search(u) or _EXPIRE_RE.search(urllib.parse.unquote(u))
    return int(m.group(1)) if m else 0


def _good_until(info):
    """Earliest picked-URL expiry minus a safety margin, capped at a sane max. resolve() never
    parses expire, so we do it here over muxed/video/audio URLs."""
    now = time.time()
    exps = [e for e in (_expire_ts(info.get("muxed_url")),
                        _expire_ts(info.get("video_url")),
                        _expire_ts(info.get("audio_url"))) if e]
    if not exps:                           # HLS-only / no parseable expire -> short conservative TTL
        return now + 5 * 60
    return min(min(exps) - _RESOLVE_SAFETY, now + _RESOLVE_CACHE_MAX_TTL)


def _signed_in():
    """Coarse login state for the cache key (a login change alters extraction -> invalidates)."""
    try:
        import ytm
        return bool(ytm.netscape_cookies())
    except Exception:
        return False


# yt-dlp errors that are YouTube's final word on a video. No other client, cookie set or the
# binary changes them, so a resolve that hears one stops there instead of paying 1-5 s more of
# extractions to hear it again (a removed track in a queue used to cost ~5 s before the skip).
# Private videos are final only signed out: signed in, the cookie'd retry may be granted access.
_FINAL_ERR = re.compile(r"video has been removed|has been terminated|live event will begin|"
                        r"Premieres in|not made this video available in your country|"
                        r"Incomplete YouTube ID", re.I)


def _final_error(msg):
    msg = msg or ""
    if "try again later" in msg.lower():
        return False                                   # rate limiting, worded as "unavailable"
    return bool(_FINAL_ERR.search(msg)) or ("Private video" in msg and not _signed_in())


def _ytdlp_err(stderr, default):
    """The user-facing part of a yt-dlp failure: its last ERROR line, without the
    "ERROR: [extractor] id:" prefix or the desktop advice (--cookies-from-browser, FAQ links) that
    means nothing on a phone. stderr can open with WARNING lines; those aren't the reason."""
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    errs = [l for l in lines if l.startswith("ERROR:")]
    msg = errs[-1] if errs else (lines[-1] if lines else "")
    msg = re.sub(r"^ERROR:\s*(?:\[[^\]]+\]\s*(?:[\w-]+:\s+)?)?", "", msg)
    msg = re.split(r"\s+(?:Use --|See\s+https?://|Also see\s)", msg)[0]
    return msg.strip()[:300] or default


def _resolve_key(video_id):
    """video_id PLUS every hidden input that changes resolve()'s output. caption_lang /
    sponsorblock / hide_* are excluded — they don't affect the returned URLs."""
    s = get_settings()
    return "\x1f".join((
        str(video_id),
        _default_client() or "auto",               # player_client (effective) — client + UA + ladder
        "1" if _pot_active() else "0",              # PO provider active -> flips client/token path
        str(s.get("default_quality") or 0),         # video-rung cap
        (s.get("audio_lang") or "").lower(),        # dub language
        "1" if s.get("hw_decode") else "0",         # VP9<->H.264 codec preference
        "1" if _signed_in() else "0",               # login -> age/members/premium extraction
    ))


def _evict_resolve_cache_locked():
    if len(_resolve_cache) <= _RESOLVE_CACHE_MAX:
        return
    victims = sorted(_resolve_cache.items(), key=lambda kv: kv[1]["ts"])[
        :len(_resolve_cache) - _RESOLVE_CACHE_MAX]
    for k, _ in victims:
        _resolve_cache.pop(k, None)


def _resolve_cache_get(key):
    now = time.time()
    with _resolve_cache_lock:
        ent = _resolve_cache.get(key)
        if ent and ent["good_until"] > now:
            return ent["payload"]
        if ent:
            _resolve_cache.pop(key, None)          # expired -> drop
    return None


def invalidate_resolve_cache():
    """Clear the whole resolve cache. Called on any output-affecting settings change and on
    login/logout (cheap — small dict, refills on demand)."""
    with _resolve_cache_lock:
        _resolve_cache.clear()


def _drop_resolved(video_id):
    """Forget every cached resolve of one video (any settings key): googlevideo refused its URLs."""
    pre = str(video_id) + "\x1f"
    with _resolve_cache_lock:
        for k in [k for k in _resolve_cache if k.startswith(pre)]:
            _resolve_cache.pop(k, None)


def _resolve_and_cache(video_id, key=None, speculative=False):
    """The one place a resolve actually happens. Cache hit -> instant. An in-flight resolve for
    the SAME key -> JOINED (waited on), never double-spawned. Else run the real _resolve_uncached
    and cache a fresh, non-live, full-ladder success. Runs the subprocess on WHATEVER thread calls
    it, so the prefetch path MUST call it from a background thread (never the worker).

    `speculative` is threaded for triggers (b)/(c) (D9): unused in #1, behaviour identical. Later
    it will skip the widen retry (I9) and cap good_until (I12); do NOT branch on it yet."""
    if key is None:
        key = _resolve_key(video_id)

    hit = _resolve_cache_get(key)
    if hit is not None:
        return hit

    with _resolve_cache_lock:
        ev = _resolve_inflight.get(key)
        if ev is None:
            ev = threading.Event()
            _resolve_inflight[key] = ev
            leader = True
        else:
            leader = False

    if not leader:                                 # ---- JOIN the in-flight resolve (D1) ----
        if not ev.wait(_RESOLVE_JOIN_TIMEOUT):     # wait PAST the leader's 2x90s ceiling
            hit = _resolve_cache_get(key)          # timed out (near-impossible): re-check cache
            if hit is not None:
                return hit
            # NEVER launch a second subprocess. The leader is about to populate; a soft error
            # lets QML retry — cheaper than a 2x resolve. (D1)
            return {"ok": False, "error": "still resolving"}
        hit = _resolve_cache_get(key)
        if hit is not None:
            return hit
        # Leader finished but cached nothing (failure / live / degraded / stale key). Rare
        # single double-spawn on the non-cacheable path only — acknowledged, not a swarm leak.
        return _resolve_uncached(video_id)

    try:                                            # ---- LEADER ----
        payload = _resolve_uncached(video_id)
        if payload.get("ok"):
            info = payload.get("info") or {}
            key2 = _resolve_key(video_id)                       # D2: recompute AFTER the resolve
            full_ladder = bool((info.get("video_url") and info.get("audio_url"))
                               or info.get("qualities"))         # D4: HD pair or a real ladder
            cacheable = (key2 == key                             # D2: world didn't move under us
                         and not info.get("is_live")             # D3: never cache live
                         and full_ladder)                        # D4: never cache muxed-only
            if cacheable:
                gu = _good_until(info)
                if gu > time.time() + 5:                         # only store something worth serving
                    with _resolve_cache_lock:
                        _resolve_cache[key] = {"payload": payload,
                                               "good_until": gu, "ts": time.time()}
                        _evict_resolve_cache_locked()
        # Failure / live / degraded / stale-key: returned to the immediate caller, NOT cached
        # (a transient bot-wall or SABR-thin window must re-resolve fresh on the next tap).
        return payload
    finally:
        with _resolve_cache_lock:
            _resolve_inflight.pop(key, None)
        ev.set()


def prefetch_resolve(video_id, speculative=False):
    """PyOtherSide entry: kick a speculative resolve on a BACKGROUND thread, return instantly.
    Deduped (one per key), capped at 2 concurrent spawns. A key already fresh in cache, already
    in flight, or over the cap is a fast no-op. `speculative` is the (b)/(c) seam (D9)."""
    if not video_id:
        return {"ok": True, "queued": False}
    key = _resolve_key(video_id)

    if _resolve_cache_get(key) is not None:
        return {"ok": True, "queued": False, "cached": True}

    with _prefetch_lock:
        if key in _prefetch_pending:
            return {"ok": True, "queued": False, "inflight": True}
        _prefetch_pending.add(key)

    def _bg():
        # D7: a throwaway prefetch thread must NEVER be the one to START/restart the POT sidecar
        # — PR_SET_PDEATHSIG arms against THIS short-lived thread, so the kernel would SIGKILL the
        # sidecar the instant _bg returns, sabotaging the worker's token source. Defer to prewarm's
        # parked, correctly-armed thread and skip this speculative attempt (it warms on the next
        # prefetch or the real foreground tap).
        if _pot_active() and not _pot_ready_on_port():
            try:
                prewarm()
            except Exception:
                pass
            with _prefetch_lock:
                _prefetch_pending.discard(key)
            return
        # Non-blocking acquire = DROP at the 2-spawn ceiling (don't queue a swarm).
        if not _prefetch_sema.acquire(blocking=False):
            with _prefetch_lock:
                _prefetch_pending.discard(key)
            return
        try:
            _resolve_and_cache(video_id, key, speculative=speculative)
        except Exception:
            pass
        finally:
            _prefetch_sema.release()
            with _prefetch_lock:
                _prefetch_pending.discard(key)

    try:
        threading.Thread(target=_bg, daemon=True).start()
    except Exception:
        # D5: thread/FD exhaustion under a scroll burst — discard the key so a failed start can't
        # wedge this video as permanently "pending" (mirrors _bg's finally).
        with _prefetch_lock:
            _prefetch_pending.discard(key)
        return {"ok": False, "queued": False, "error": "spawn failed"}
    return {"ok": True, "queued": True}


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


def _ytdlp_version_path():
    return os.path.join(_data_dir(), "ytdlp_version.json")


def ytdlp_version(fresh=False):
    """Installed yt-dlp version string, or '' if missing/broken. Remembered per binary (path +
    size + mtime): `--version` is a full frozen-binary cold start (~1.1s+ on device) that used to
    hold the main lane at every launch. An update or reinstall changes the stat, so a stale answer
    can't outlive the binary; `fresh` forces the run (install uses it to prove the binary works)."""
    path = _ytdlp_path()
    if not path:
        return ""
    try:
        st = os.stat(path)
        sig = [path, st.st_size, st.st_mtime_ns]
    except OSError:
        sig = None
    if sig and not fresh:
        try:
            with open(_ytdlp_version_path()) as f:
                c = json.load(f)
            if c.get("sig") == sig and c.get("version"):
                return c["version"]
        except Exception:
            pass
    try:
        out = subprocess.run([path, "--version"], capture_output=True,
                             text=True, timeout=15)
        v = out.stdout.strip()
    except Exception:
        return ""
    if v and sig:
        try:
            _atomic_write_json(_ytdlp_version_path(), {"sig": sig, "version": v})
        except Exception:
            pass
    return v


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
    if _ARM32 and path == _managed_ytdlp():
        return _ytdlp_update_by_download()
    channel = _ytdlp_channel()
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


# A 32-bit ARM userland: the armv7hl images, which some phones run on a 64-bit kernel. Asked of
# the interpreter, not uname, which reports the kernel ("aarch64") there; every binary we fetch
# has to match the userland. What changes on it: yt-dlp comes as the stable one-directory armv7l
# zip (no nightly build, no self-update), Deno has no 32-bit build at all (so no PO-token
# provider; playback relies on the token-free clients), and ffmpeg is the armhf static build.
_ARM32 = struct.calcsize("P") == 4

# Standalone build (self-contained — bundles its own Python, so it doesn't depend on the device's
# Python version). "latest" redirects to the current release asset; each release also publishes
# SHA2-256SUMS, which we verify the download against. On 32-bit ARM it's a zip of a directory
# (executable + _internal/), unpacked by _unpack_ytdlp_dir.
_YTDLP_ASSET = "yt-dlp_linux_armv7l.zip" if _ARM32 else "yt-dlp_linux_aarch64"
_YTDLP_DIR_NAME = "yt-dlp-armv7l"   # bin/ subdir holding the unpacked 32-bit build
# Release bases PER UPDATE CHANNEL. The binary hops channels via its own --update-to, but every
# direct download here (first binary install and — crucially — the importable ZIPAPP, which has
# no self-updater) must come from the repo matching the user's channel: a nightly binary next to
# a stable zipapp means the in-process fast path is missing the very breakage fix the user
# switched to nightly FOR — it fails (or goes SABR-thin) and every resolve silently pays a dead
# in-process attempt before the binary rescues it. Both repos publish the identical asset set
# (yt-dlp_linux_aarch64, the yt-dlp zipapp, SHA2-256SUMS); verified 2026-09-06.
_YTDLP_RELEASE_BASES = {
    "stable": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/",
    "nightly": "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/",
}


def _ytdlp_channel():
    """The user's yt-dlp update channel: "stable" unless explicitly "nightly". Always stable on
    32-bit ARM: the nightly repo publishes no armv7l build, and the zipapp must match the binary."""
    if _ARM32:
        return "stable"
    return "nightly" if (get_settings().get("ytdlp_channel") == "nightly") else "stable"


def _ytdlp_release_base():
    """GitHub release-asset base URL for the user's channel (binary, zipapp and sums alike)."""
    return _YTDLP_RELEASE_BASES[_ytdlp_channel()]


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


def _expected_sha256(ctx, asset=None):
    """The published SHA-256 for `asset` (this device's yt-dlp build by default; the
    arch-independent zipapp for fast resolve), from the CHANNEL repo's SHA2-256SUMS file (or None)."""
    asset = asset or _YTDLP_ASSET
    with _https_open(_ytdlp_release_base() + "SHA2-256SUMS", ctx, timeout=30) as resp:
        text = resp.read().decode("utf-8", "replace")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset:
            return parts[0].strip().lower()
    return None


def _unpack_ytdlp_dir(zpath):
    """Install the 32-bit one-directory build from its zip: unpack it into bin/yt-dlp-armv7l and
    point bin/yt-dlp at its executable with a relative symlink, so every caller keeps running
    bin/yt-dlp (the bootloader finds _internal/ through the link's real path). The tree is unpacked
    beside the live one and swapped in by rename, so a failed unpack leaves a working install
    alone. The replaced tree is parked as .old until the next install rather than deleted, in case
    a yt-dlp process is still running from it."""
    import zipfile
    bindir = os.path.dirname(_managed_ytdlp())
    final = os.path.join(bindir, _YTDLP_DIR_NAME)
    new, old = final + ".new", final + ".old"
    for d in (new, old):
        shutil.rmtree(d, ignore_errors=True)
    exe = _YTDLP_ASSET[:-len(".zip")]
    root = os.path.realpath(new) + os.sep
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():                 # no member may land outside the new tree
            if not os.path.realpath(os.path.join(new, name)).startswith(root):
                raise ValueError("unsafe path in the yt-dlp archive: " + name)
        z.extractall(new)
    exe_path = os.path.join(new, exe)
    if not os.path.isfile(exe_path):
        raise ValueError("the yt-dlp archive has no " + exe)
    os.chmod(exe_path, 0o755)                     # zipfile doesn't restore permissions
    if os.path.isdir(final):
        os.rename(final, old)
    os.rename(new, final)
    link = _managed_ytdlp() + ".link"
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(os.path.join(_YTDLP_DIR_NAME, exe), link)
    os.replace(link, _managed_ytdlp())


def _install_ytdlp_sync(progress=None):
    """Download, verify and install this device's yt-dlp build into our bin/. Returns
    (ok, message, version); progress(pct) is called as the download advances."""
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
        with _https_open(_ytdlp_release_base() + _YTDLP_ASSET, ctx) as resp:
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
                    if total > 0 and progress:
                        pct = done * 100.0 / total
                        if int(pct) != last:
                            last = int(pct)
                            progress(pct)
        if expected and h.hexdigest().lower() != expected:
            os.remove(tmp)
            tmp = None
            return False, "Checksum mismatch — download discarded, nothing installed", ""
        if _YTDLP_ASSET.endswith(".zip"):
            _unpack_ytdlp_dir(tmp)
            os.remove(tmp)
        else:
            os.chmod(tmp, 0o755)
            os.replace(tmp, dest)
        tmp = None
        ver = ytdlp_version(fresh=True)  # exercises the binary — confirms it actually runs
        if ver:
            note = "Installed yt-dlp " + ver
            if not expected:
                note += " (checksum unavailable, not verified)"
            return True, note, ver
        return False, ("Downloaded + checksum OK, but the binary won't run here — "
                       "the sandbox is likely blocking exec from the data dir"), ""
    except Exception as ex:
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False, str(ex), ""


def install_ytdlp():
    """Download yt-dlp into our data dir (the one place that's writable AND visible inside the
    Sailjail sandbox). HTTPS-only, checksum-verified against the release's SHA2-256SUMS. Runs
    in the background; progress + result go to QML via pyotherside."""
    import pyotherside

    def run():
        ok, msg, ver = _install_ytdlp_sync(
            lambda pct: pyotherside.send("ytdlp_install_progress", pct))
        pyotherside.send("ytdlp_install_done", ok, msg, ver)

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def _ytdlp_latest_tag():
    """The latest stable yt-dlp release tag ('' on any failure). yt-dlp's version string is the
    tag, so the two compare directly."""
    try:
        _force_ipv4()
        ctx = ssl.create_default_context()
        req = urllib.request.Request(
            "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
            headers={"User-Agent": _BROWSER_UA, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            return (json.loads(resp.read().decode()).get("tag_name") or "").strip()
    except Exception:
        return ""


def _ytdlp_update_by_download():
    """ytdlp_update for the 32-bit build, which can't update itself (yt-dlp: "Auto-update is not
    supported for unpackaged Linux executable"): download the latest stable release again,
    unless that is already the installed version."""
    cur = ytdlp_version()
    latest = _ytdlp_latest_tag()
    if not latest:     # don't pull ~40 MB blind when we can't tell whether it's needed
        return {"ok": False, "error": "Couldn't reach GitHub to check for a newer yt-dlp",
                "version": cur, "channel": "stable"}
    if cur and latest == cur:
        return {"ok": True, "output": "yt-dlp is up to date (stable@%s)" % cur,
                "version": cur, "channel": "stable"}
    ok, msg, ver = _install_ytdlp_sync()
    res = {"ok": ok, "output": msg, "version": ver or ytdlp_version(), "channel": "stable"}
    if not ok:
        res["error"] = msg
    return res


# --------------------------------------------------------------------------- #
# Fast resolve: run yt-dlp IN-PROCESS instead of spawning the frozen
# binary. That binary is a PyInstaller onefile — it re-unpacks to TMPDIR and re-imports
# yt_dlp on EVERY call (~1.3s spawn tax on this CPU). Importing yt-dlp ONCE and keeping
# a warm YoutubeDL in the worker removes that tax and keeps the player-JS / n-sig caches
# warm across resolves — the in-process advantage that keeps NewPipe fast. The binary
# stays the resilient DEFAULT (self-updating, needs no device Python/deps) AND the
# fallback: ANY failure here (missing/broken/incompatible zip, import or extraction
# error) falls through to the binary, so opting in can never make a resolve fail that the
# binary would have served. Scope: only the TOKEN-FREE hot dump (_token_free_client, no PO
# token) runs in-process; every token / widen / gated dump keeps using the binary, so the
# bgutil PO-token PLUGIN machinery is untouched and never needed in-process.
# --------------------------------------------------------------------------- #

_YTDLP_ZIPAPP_ASSET = "yt-dlp"   # the arch-independent zipapp in the same (channel) release


def _ytdlp_zipapp_path():
    """The importable yt-dlp zipapp under our data dir (fast-resolve only, never exec'd)."""
    return os.path.join(_data_dir(), "bin", "yt-dlp.zip")


def _zipapp_version(path):
    """Read yt_dlp/version.py's __version__ out of the zipapp WITHOUT importing it (importing
    would pin this whole process to one yt_dlp for its lifetime). "" if it can't be read."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            src = z.read("yt_dlp/version.py").decode("utf-8", "replace")
        m = re.search(r"""__version__\s*=\s*['"]([^'"]+)['"]""", src)
        return m.group(1) if m else ""
    except Exception:
        return ""


def fast_resolve_status():
    """For the Providers UI: is the in-process fast-resolve copy set up — and can this
    device's python run it at all (python_ok drives the honest why-not text). NOT a
    setting: fast resolve engages automatically wherever python_ok holds and the copy
    exists; the binary is always the fallback."""
    path = _ytdlp_zipapp_path()
    present = os.path.isfile(path)
    return {"installed": present,
            "version": _zipapp_version(path) if present else "",
            "python_ok": _FAST_RESOLVE_PY_OK,
            "python_version": "%d.%d" % sys.version_info[:2]}


def install_ytdlp_zipapp():
    """Download the arch-independent yt-dlp ZIPAPP into our data dir for fast resolve. HTTPS-only,
    checksum-verified against the release's SHA2-256SUMS, then structurally validated (must be a
    zip exposing the yt_dlp package). Background; progress + result go to QML via pyotherside."""
    import pyotherside
    import zipfile

    # Engine-side gate, mirroring the QML pythonOk checks: on a too-old OS python the zipapp
    # can never run, so refuse the download outright. Covers the race where an install rides
    # along before fast_resolve_status() has told the UI that python_ok is false.
    if not _FAST_RESOLVE_PY_OK:
        pyotherside.send("ytdlp_zipapp_done", False,
                         "In-process yt-dlp needs OS Python 3.10+ — the binary is used instead.", "")
        return {"ok": False}

    def run():
        global _YT_DLP_MOD
        tmp = None
        try:
            _force_ipv4()
            ctx = ssl.create_default_context()
            expected = _expected_sha256(ctx, _YTDLP_ZIPAPP_ASSET)   # None if the sums can't be parsed
            dest_dir = os.path.join(_data_dir(), "bin")
            os.makedirs(dest_dir, exist_ok=True)
            dest = _ytdlp_zipapp_path()
            # Per-thread: the launch autofetch and an Update tap can overlap.
            tmp = "%s.part-%d" % (dest, threading.get_ident())
            h = hashlib.sha256()
            with _https_open(_ytdlp_release_base() + _YTDLP_ZIPAPP_ASSET, ctx) as resp:
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
                                pyotherside.send("ytdlp_zipapp_progress", pct)
            if expected and h.hexdigest().lower() != expected:
                os.remove(tmp)
                pyotherside.send("ytdlp_zipapp_done", False,
                                 "Checksum mismatch — download discarded", "")
                return
            # zipimport reads the central directory past the shebang prefix, so a plain
            # ZipFile check is enough to confirm the yt_dlp package is importable from it.
            try:
                with zipfile.ZipFile(tmp) as z:
                    ok_shape = "yt_dlp/__init__.py" in z.namelist()
            except Exception:
                ok_shape = False
            if not ok_shape:
                os.remove(tmp)
                pyotherside.send("ytdlp_zipapp_done", False,
                                 "Downloaded file is not a yt-dlp zipapp — discarded", "")
                return
            os.chmod(tmp, 0o644)
            os.replace(tmp, dest)
            if _YT_DLP_IMPORT_DONE:
                # The imported copy loads its modules lazily from this path, and zipimport keeps
                # the OLD file's offsets, so each later import from the new file fails. Retire the
                # in-process tier for this session: the (updated) binary answers until relaunch.
                _YT_DLP_MOD = None
            ver = _zipapp_version(dest)
            note = "Installed yt-dlp zipapp " + (ver or "(unknown version)")
            if not expected:
                note += " (checksum unavailable, not verified)"
            if _YT_DLP_IMPORT_DONE:   # a copy is already imported (one-shot per process) — tell
                note += " — takes effect next app launch"    # the user why nothing changes yet
            pyotherside.send("ytdlp_zipapp_done", True, note, ver)
        except Exception as ex:
            try:
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            pyotherside.send("ytdlp_zipapp_done", False, str(ex), "")

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


_ZIPAPP_AUTOFETCH_DONE = False


def _autofetch_zipapp():
    """Self-heal the fast-resolve copy at launch, once per process: a capable device that
    already has the yt-dlp binary (so the user consented to yt-dlp — the zipapp is the same
    software in importable form, from the same release) but no zipapp fetches one in the
    background. A failure stays silent and leaves the binary in charge; the next launch or
    an Update tap retries. This is what keeps "the copy always exists" true for installs
    that predate it — there is no settings row to re-enable."""
    global _ZIPAPP_AUTOFETCH_DONE
    if _ZIPAPP_AUTOFETCH_DONE or not _FAST_RESOLVE_PY_OK:
        return
    _ZIPAPP_AUTOFETCH_DONE = True
    try:
        if os.path.isfile(_ytdlp_zipapp_path()) or not _ytdlp_path():
            return
    except Exception:
        return
    install_ytdlp_zipapp()


# ---- in-process extraction (the warm path) --------------------------------- #

_YT_DLP_MOD = None
_YT_DLP_IMPORT_DONE = False
_yt_dlp_import_lock = threading.Lock()
# One warm YoutubeDL per THREAD: the long-lived worker thread keeps its player-JS / n-sig
# caches warm across resolves; throwaway prefetch threads each get their own — so no two
# threads ever share a YoutubeDL, which is what makes this lock-free and race-free.
_inproc_tls = threading.local()


class _InprocLogger:
    """Swallow yt-dlp's chatter; surface errors only, and only when debugging."""
    def debug(self, m):
        pass

    def info(self, m):
        pass

    def warning(self, m):
        pass

    def error(self, m):
        if _DEBUG:
            _tlog("inproc yt-dlp error: " + str(m)[:200])


def _import_yt_dlp():
    """Import the yt-dlp ZIPAPP once, module-wide, and cache it (None if unavailable). Inserting
    the zipapp at the FRONT of sys.path makes zipimport load OUR yt_dlp regardless of any system
    copy. One-time and irreversible for the process — an updated zip takes effect next launch."""
    global _YT_DLP_MOD, _YT_DLP_IMPORT_DONE
    if not _FAST_RESOLVE_PY_OK:
        return None      # the device python can't run yt-dlp at all — the single gate point
    if _YT_DLP_IMPORT_DONE:
        return _YT_DLP_MOD
    with _yt_dlp_import_lock:
        if _YT_DLP_IMPORT_DONE:
            return _YT_DLP_MOD
        import sys
        zp = _ytdlp_zipapp_path()
        if not os.path.isfile(zp):
            return None          # not installed yet — stay RETRYABLE (don't cache), so a resolve
                                 # during the enable-and-download flow picks it up once it lands
        mod = None
        try:
            if zp not in sys.path:
                sys.path.insert(0, zp)
            _ensure_deno_on_path()   # in-process n-sig also uses Deno when it's present
            import yt_dlp as _m
            mod = _m
            # The InnerTube fast path follows this yt-dlp's client definitions (web version + its
            # JS-less default player client), so a YouTube-forced client change arrives with a
            # yt-dlp update instead of needing an app release.
            it = _it()
            if it is not None:
                try:
                    _tlog("yt-dlp %s in-process; innertube player client: %s"
                          % (getattr(getattr(mod, "version", None), "__version__", "?"),
                             it.configure_from_ytdlp(mod)))
                except Exception as ex:
                    _plog("innertube client sync failed: %s" % ex)
        except Exception as ex:
            _plog("fast-resolve: yt-dlp import failed (%s) — using binary" % ex)
            mod = None
        _YT_DLP_MOD = mod
        _YT_DLP_IMPORT_DONE = True   # the zip existed: cache the outcome (success, or a hard import
                                     # failure we shouldn't retry every resolve)
        return _YT_DLP_MOD


def _fast_resolve_ready():
    """True when the importable yt-dlp actually loaded (the device-python gate lives at the
    top of _import_yt_dlp). No user setting — in-process is automatic, the binary is the
    fallback, not an option."""
    return _import_yt_dlp() is not None


def _parse_extractor_args(extra):
    """Turn a ["--extractor-args", "youtube:k=v;k2=v2,v3"] argv fragment into YoutubeDL's
    extractor_args dict {"youtube": {"k": ["v"], "k2": ["v2", "v3"]}}. [] -> {}."""
    out = {}
    i = 0
    while i < len(extra):
        if extra[i] == "--extractor-args" and i + 1 < len(extra):
            ie, _, kvs = extra[i + 1].partition(":")
            d = {}
            for kv in kvs.split(";"):
                if not kv:
                    continue
                k, _, v = kv.partition("=")
                d[k.strip()] = [x for x in v.split(",") if x != ""] if v else []
            if ie.strip():
                out[ie.strip().lower()] = d
            i += 2
            continue
        i += 1
    return out


def _inproc_apply_cookies(ydl, anon=False):
    """Sync the warm YoutubeDL's cookie jar with the imported YouTube login (from ytm), reloading
    only when the cookie text actually changed (sign in/out) — a no-op on the common path. anon=True
    forces an EMPTY jar (the token-free primary resolves cookie-free to dodge auth gating)."""
    text = ""
    if not anon:
        try:
            import ytm
            text = ytm.netscape_cookies() or ""
        except Exception:
            text = ""
    if getattr(_inproc_tls, "cookie_hash", None) == hash(text):
        return
    _inproc_tls.cookie_hash = None   # stamped only once the jar matches: a failed load retries
    try:
        jar = ydl.cookiejar
        jar.clear()
        if text:
            fd, p = tempfile.mkstemp(prefix="ytdlp-ck-", suffix=".txt")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(text)
                jar.load(p)
            finally:
                try:
                    os.remove(p)
                except Exception:
                    pass
        _inproc_tls.cookie_hash = hash(text)
    except Exception:
        pass


def _inproc_ydl(mod):
    """The warm, per-thread YoutubeDL (built once per thread; see _inproc_tls)."""
    ydl = getattr(_inproc_tls, "ydl", None)
    if ydl is None:
        ydl = mod.YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "no_color": True,
            "source_address": "0.0.0.0",   # == yt-dlp -4 (force IPv4), matching _COMMON_ARGS
            # Bound each in-process request. The BINARY path has a hard 90s aggregate cap
            # (subprocess timeout=90) that extract_info lacks; per-request bounding is the
            # in-process stand-in, so one wedged socket can't pin the worker indefinitely.
            "socket_timeout": 20,
            # No explicit cachedir: use yt-dlp's default (~/.cache/yt-dlp), the SAME the frozen
            # binary uses (app is unsandboxed → writable). So the in-process path shares the
            # binary's warm n-sig / player-JS disk cache — a first-of-session resolve is warmer.
            "logger": _InprocLogger(),
        })
        _inproc_tls.ydl = ydl
        _inproc_tls.cookie_hash = None
    return ydl


_orig_popen = None   # set once when the DEBUG Deno probe is installed


def _inproc_install_deno_probe():
    """DEBUG-only, idempotent: wrap subprocess.Popen so we can time how long each resolve spends in
    **Deno** (the n-signature solver) vs everything else (network + CPython extraction) — the one
    number that decides whether the JS runtime is worth touching. All subprocess spawns funnel
    through Popen (run/check_output call it), so this catches every Deno invocation. Transparent:
    it only instruments deno-named spawns from a thread that armed an accumulator (`_inproc_tls.deno`),
    counts each process once, and passes everything else straight through unchanged."""
    global _orig_popen
    if _orig_popen is not None:
        return
    _orig_popen = subprocess.Popen

    def _popen(cmd, *a, **k):
        p = _orig_popen(cmd, *a, **k)
        acc = getattr(_inproc_tls, "deno", None)
        if acc is None:
            return p
        try:
            a0 = cmd[0] if isinstance(cmd, (list, tuple)) else cmd
            is_deno = "deno" in os.path.basename(str(a0)).lower()
        except Exception:
            is_deno = False
        if not is_deno:
            return p
        t = time.time()
        counted = [False]
        _oc, _ow = p.communicate, p.wait

        def _mark():
            if not counted[0]:
                counted[0] = True
                acc[0] += 1
                acc[1] += time.time() - t

        def communicate(*aa, **kk):
            try:
                return _oc(*aa, **kk)
            finally:
                _mark()

        def wait(*aa, **kk):
            try:
                return _ow(*aa, **kk)
            finally:
                _mark()

        p.communicate = communicate
        p.wait = wait
        return p

    subprocess.Popen = _popen


def _inproc_dump(url, extra, anon=False):
    """Extract `url` in-process with the warm YoutubeDL and return a dict shaped exactly like the
    binary's --dump-single-json (via sanitize_info). Raises on failure — the caller falls back.
    anon=True resolves cookie-free (the token-free primary; see _dump)."""
    mod = _import_yt_dlp()
    if mod is None:
        raise RuntimeError("yt-dlp zipapp not importable")
    ydl = _inproc_ydl(mod)
    ydl.params["extractor_args"] = _parse_extractor_args(extra)
    _inproc_apply_cookies(ydl, anon)
    if _DEBUG:                       # arm the Deno-share probe for THIS resolve (this thread)
        _inproc_install_deno_probe()
        _inproc_tls.deno = [0, 0.0]  # [n_spawns, total_seconds]
    info = ydl.extract_info(url, download=False)
    return ydl.sanitize_info(info)


def _ytdlp_run(path, argv, timeout):
    """`yt-dlp <argv>` for a call that ends in --dump-single-json, as a CompletedProcess. Runs
    in-process on the imported zipapp when it loaded: the SAME argv goes through
    yt_dlp.parse_options, so the two forms can't drift apart, and it skips the frozen binary's
    cold start (~1.1s on device). Any in-process failure re-runs the binary, whose answer and
    error text the callers already handle; so does a device where the zipapp can't load. This is
    the middle tier behind the InnerTube fast paths: yt-dlp's parsers, maintained upstream."""
    mod = _import_yt_dlp()
    if mod is not None:
        _t = time.time()
        try:
            data = _inproc_json(mod, argv)
            _tlog("yt-dlp in-process %.2fs: %s" % (time.time() - _t, str(argv[-1])[:80]))
            return subprocess.CompletedProcess([path, *argv], 0, json.dumps(data), "")
        except Exception as ex:
            _tlog("yt-dlp in-process failed %.2fs → binary: %s" % (time.time() - _t, str(ex)[:120]))
    return subprocess.run([path, *argv], capture_output=True, text=True, timeout=timeout)


def _inproc_json(mod, argv):
    """What `yt-dlp <argv>` would print for --dump-single-json, computed in-process on a fresh
    YoutubeDL (a fresh one per call, so per-call options like --playlist-items can't leak into
    the warm resolve instance). Raises on any failure."""
    po = mod.parse_options(list(argv))
    if len(po.urls) != 1:
        raise ValueError("expected one URL, got %d" % len(po.urls))
    opts = dict(po.ydl_opts)
    for k in ("dump_single_json", "forcejson", "forceprint", "print_to_file"):
        opts.pop(k, None)                   # the answer comes back as a value, not on stdout
    opts.update(quiet=True, no_warnings=True, noprogress=True, logger=_InprocLogger())
    if not opts.get("socket_timeout"):
        opts["socket_timeout"] = 20         # the binary's aggregate timeout has no in-process twin
    with mod.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(po.urls[0], download=False)
        if info is None:
            # The CLI default ignoreerrors="only_download" makes a failed extraction return
            # None instead of raising; the binary exits 1 with the real message, so it answers.
            raise RuntimeError("yt-dlp extraction failed")
        # Exactly what the binary prints for -J (post_extract + sanitize_info, no key stripping:
        # clean_infojson's stripping is for .info.json files and would drop "entries").
        ydl.post_extract(info)
        return ydl.sanitize_info(info)


def _inproc_comments(url, extra):
    """Comment walk on the warm per-thread YoutubeDL (fast-resolve opt-in): no binary spawn
    tax, and the continuation walk reuses the warm TLS session. Follows the SIGNED-IN view
    like the binary path (cookies applied, not anon). The two per-call params are set and
    RESTORED in a finally — a leaked getcomments=True would make every later resolve on this
    thread silently pay a full comment walk, the one leak this warm-instance design must never
    allow. Raises on any failure — the caller falls back to the binary."""
    mod = _import_yt_dlp()
    if mod is None:
        raise RuntimeError("yt-dlp zipapp not importable")
    ydl = _inproc_ydl(mod)
    ydl.params["extractor_args"] = _parse_extractor_args(extra)
    _inproc_apply_cookies(ydl)
    had = {k: ydl.params.get(k) for k in ("getcomments", "ignore_no_formats_error")}
    ydl.params["getcomments"] = True
    ydl.params["ignore_no_formats_error"] = True
    try:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)
    finally:
        for k, v in had.items():
            if v is None:
                ydl.params.pop(k, None)
            else:
                ydl.params[k] = v


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


# Static aarch64 build, armhf on 32-bit ARM (self-contained; John Van Sickle's release is the de-facto
# arm64 source). The armhf binary is pinned the same way (verified to run on the armv7hl SDK target).
# It's a .tar.xz carrying ffmpeg + ffprobe under a versioned dir; a companion .md5 lets us verify
# the archive before unpacking. (MD5 is weak, but the transfer is HTTPS + cert-verified.)
# NOTE (2026-09): the JVS arm64 build has been FROZEN since Aug 2024 (Last-Modified checked
# live) — fine for us (ffmpeg only muxes here, and the pinned SHA below stays valid), but it is
# a single point of failure. If the site ever dies, BtbN/FFmpeg-Builds on GitHub publishes
# linuxarm64 static builds with per-asset sums (the same trust model as the yt-dlp and Deno
# installers): switching means re-pointing these URLs and re-pinning _FFMPEG_SHA256.
_FFMPEG_URL = ("https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-%s-static.tar.xz"
               % ("armhf" if _ARM32 else "arm64"))
_FFMPEG_MD5_URL = _FFMPEG_URL + ".md5"

# A trusted SHA-256 of the extracted ffmpeg BINARY, pinned out-of-band. The .md5 companion above is
# served by the same host, so it only guards against transfer corruption — an attacker who serves a
# tampered archive serves a matching .md5. When THIS pin is set it's the AUTHORITATIVE integrity
# check on the actual executable we run (a host/supply-chain compromise can't forge it). Empty =
# fall back to the corruption-only MD5. NOTE: this is the hash of the `ffmpeg` binary itself
# (sha256sum ~/.local/share/<app>/bin/ffmpeg), so upgrading ffmpeg means re-pinning. Set to a
# known-good build; a mismatching download is REFUSED while staged (never promoted over a
# working install) unless the user explicitly accepts it via allow_unpinned — see the M12
# integrity gate in install_ffmpeg.
_FFMPEG_SHA256 = ("0afba4a11110e6e402053e0fc14c33a7eb207d7a588688ae87dba471a0f06c71" if _ARM32 else
                  "6bb182d0d75d23028db82e9e4f723ca69b853d055698486e6984ddb2c06fb8ce")


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


def install_ffmpeg(allow_unpinned=False):
    """Download the static ffmpeg archive and unpack ffmpeg+ffprobe into our bin/ (beside yt-dlp).
    HTTPS-only. When a known-good SHA-256 is pinned, the extracted binary MUST match it or the
    install is REFUSED (staged, never promoted over a working install) — `allow_unpinned=True`
    accepts an unverified newer build after the user confirms. Background thread; events to QML."""
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
            # Unpack the two binaries to STAGING names first (basename only, so a malicious archive
            # path can't escape our dir), so the pinned SHA-256 is verified BEFORE anything is promoted
            # over an existing working install. (M12)
            got = {}
            with tarfile.open(tmp, "r:xz") as tf:
                for m in tf.getmembers():
                    base = os.path.basename(m.name)
                    if m.isfile() and base in ("ffmpeg", "ffprobe"):
                        src = tf.extractfile(m)
                        if src is None:
                            continue
                        stage = os.path.join(dest_dir, base + ".new")
                        with open(stage, "wb") as out:
                            shutil.copyfileobj(src, out)
                        os.chmod(stage, 0o755)
                        got[base] = stage
            os.remove(tmp)
            tmp = None

            def _discard_staged():
                for p in got.values():
                    try:
                        os.remove(p)
                    except Exception:
                        pass

            if "ffmpeg" not in got:
                _discard_staged()
                pyotherside.send("ffmpeg_install_done", False,
                                 "Archive didn't contain an ffmpeg binary", "")
                return
            # Integrity GATE (not just a marker): when a known-good SHA-256 is pinned, the extracted
            # ffmpeg MUST match it. A mismatch is either a newer upstream build (the release URL always
            # points at the latest, which we can't have pinned) OR a tampered binary from the single
            # host we trust on TLS alone — we can't tell which, so we do NOT install it silently. The
            # user can retry with allow_unpinned to accept an unverified newer build. (M12)
            pinned_ok = False
            if _FFMPEG_SHA256:
                got_sha = _sha256_file(got["ffmpeg"])
                pinned_ok = (got_sha == _FFMPEG_SHA256.strip().lower())
                if not pinned_ok and not allow_unpinned:
                    _discard_staged()
                    pyotherside.send("ffmpeg_install_done", False,
                                     "This ffmpeg build doesn't match the known-good pinned build — it "
                                     "may be a newer release or tampered, so it was NOT installed. Use "
                                     "“Install unverified build” to accept it anyway.", "", True)
                    return
            # Accepted (pin matched, no pin set, or the user overrode) — promote the staged binaries.
            for base, stage in got.items():
                os.replace(stage, os.path.join(dest_dir, base))
            ver = ffmpeg_version()  # exercises the binary — confirms it actually runs
            if ver:
                note = "Installed ffmpeg " + ver
                if pinned_ok:
                    note += " (SHA-256 verified — pinned build)"
                elif _FFMPEG_SHA256:
                    note += " (unverified build — accepted by you)"
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


def update_ffmpeg(allow_unpinned=False):
    """Fetch + install the latest static ffmpeg (the release URL always points at the current
    build). Same flow + events as install_ffmpeg(); `allow_unpinned` accepts an unverified newer
    build the pinned SHA-256 can't vouch for (the user confirms via the Providers UI)."""
    return install_ffmpeg(allow_unpinned)


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
_ARM32_NO_DENO = ("Deno has no 32-bit ARM build, so the PO-token provider can't run on this phone. "
                  "Playback uses the clients that need no token.")
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
    if _ARM32:
        pyotherside.send("deno_install_done", False, _ARM32_NO_DENO, "")
        return {"ok": False}

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
    # The directory handed to yt-dlp's --plugin-dirs. yt-dlp DISCOVERS plugins by globbing one subdir
    # level down (<dir>/*/yt_dlp_plugins — the same shape as its auto-scan of
    # ~/.config/yt-dlp/plugins/<name>/yt_dlp_plugins), NOT <dir>/yt_dlp_plugins directly. The bgutil
    # repo keeps the plugin at <repo>/plugin/yt_dlp_plugins, so we hand yt-dlp the REPO ROOT (it then
    # finds <repo>/plugin/yt_dlp_plugins). Pointing straight at plugin/ (whose yt_dlp_plugins is a
    # DIRECT child) matched the glob nothing → "Plugin directories: none", ZERO providers loaded, and
    # the app silently ran only on a user's stray ~/.config install if any (measured on-device
    # 2026-09-02 — our managed plugin had never loaded via --plugin-dirs).
    return _pot_repo_dir()


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
    """yt-dlp args to load ONLY the app's own bundled bgutil plugin when the provider is active; else
    []. `--no-plugin-dirs` FIRST empties yt-dlp's plugin search list — otherwise yt-dlp ALSO scans the
    default ~/.config/yt-dlp/plugins and ~/.local/share dirs, and a user's stray manual bgutil install
    there SHADOWS our managed copy (namespace import is first-match-wins, no warning — measured: a
    stray 1.3.1 silently beat our 1.3.2, and 1.3.1 wouldn't mint the web_embedded token). Then
    `--plugin-dirs` adds only our repo. Order matters: --no-plugin-dirs MUST come first, or it also
    wipes our dir. Keeps yt-dlp untouched whenever the provider isn't set up/enabled."""
    return ["--no-plugin-dirs", "--plugin-dirs", _pot_plugin_dir()] if _pot_active() else []


def _pot_bind_localhost():
    """Patch the cloned server to bind 127.0.0.1 instead of all interfaces.

    Upstream main.ts hardcodes host "::" (fallback "0.0.0.0") with no env/flag — its own
    comment says a localhost default is planned 'in the next major version', so we make that
    change early. The SUCCESS LOG is patched too: upstream prints a HARDCODED "[::]:<port>"
    address string regardless of the actual bind, so an unpatched log reads as an
    all-interfaces bind even when the rebind worked — a recurring false alarm when reading
    device logs (verify for real with `ss -tlnp | grep 4416`). The "address " prefix keeps
    the error lines ("Could not listen on [::]…") untouched. Best-effort + idempotent: if
    the source shape ever changes, the replaces are no-ops and the server just keeps
    upstream behaviour (the low-severity status quo). Deno runs the .ts directly, so the
    rewrite takes effect on the next server start."""
    main_ts = os.path.join(_pot_server_dir(), "src", "main.ts")
    try:
        with open(main_ts) as f:
            src = f.read()
        patched = (src.replace('host: "::"', 'host: "127.0.0.1"')
                      .replace('host: "0.0.0.0"', 'host: "127.0.0.1"')
                      .replace('address [::]:', 'address 127.0.0.1:')
                      .replace('address 0.0.0.0:', 'address 127.0.0.1:'))
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


def _pot_of(u):
    """DEBUG: the streaming PO-token (`pot=`) state of a googlevideo URL, WITHOUT leaking the
    token — 'no-url' when the pick has no URL at all (a genuinely dead pick), 'no-pot' when the
    URL just carries no pot= param (NORMAL for token-free clients like the anonymous primary
    — only suspect when that same stream 403s at byte 0), else the token's length +
    8-char prefix. The old single 'MISSING' label conflated those two very different states."""
    if not u:
        return "no-url"
    try:
        p = urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get("pot", [""])[0]
        return ("len=%d pfx=%s" % (len(p), p[:8])) if p else "no-pot"
    except Exception:
        return "?"


def _pot_server_log_tail(n=30):
    """Last n non-empty lines of the provider server's log (potprovider/server.log), or '' if
    there's none. This is where a Deno crash / NotCapable / OOM prints its reason."""
    try:
        with open(os.path.join(_pot_dir(), "server.log"), "r", errors="replace") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        return "\n".join(lines[-max(1, int(n)):])
    except Exception:
        return ""


def _pot_plugin_probe(timeout=30):
    """Does the installed yt-dlp actually RESOLVE our bgutil plugin directory? The clone keeps
    server+plugin in lockstep, but the plugin must ALSO be loadable by whatever yt-dlp binary is
    installed — and when it isn't, everything else looks healthy (server up, /ping answering)
    while every gated video quietly runs token-less. One OFFLINE binary run answers it:
    `--simulate` on a dummy scheme fails before any network, and the verbose debug header is
    where yt-dlp reports plugin-dir resolution — the exact line that caught the 2026-09-02
    'Plugin directories: none' incident. (NB `--version` short-circuits BEFORE plugin loading
    and prints none of this.) Costs one spawn (~1.3s on-device); diagnostics-only.

    Returns {checked, loaded, detail, js_runtimes, bgutil_lines}:
      loaded  True  → a 'Plugin directories' line names our repo (the historical failure mode
                      is ruled out);
              False → the line exists WITHOUT our repo (e.g. 'none') — yt-dlp runs unplugged;
              None  → no such line (very old yt-dlp / probe inconclusive) — no false alarms.
      js_runtimes   yt-dlp's own '[debug] JS runtimes' view — names Deno when reachable for the
                    n-sig solver, 'none' when EJS would fall back to the slow built-in.
      bgutil_lines  any output mentioning bgutil — a plugin that RESOLVES but fails to import
                    surfaces its warning/traceback here, which dir resolution alone can't see."""
    path = _ytdlp_path()
    if not path or not _pot_installed():
        return {"checked": False, "loaded": None, "detail": "",
                "js_runtimes": "", "bgutil_lines": ""}
    try:
        proc = subprocess.run(
            [path, "--no-plugin-dirs", "--plugin-dirs", _pot_plugin_dir(),
             "-v", "--simulate", "--", "youfish-probe:"],
            capture_output=True, text=True, timeout=timeout)
        out = (proc.stderr or "") + "\n" + (proc.stdout or "")
    except Exception as ex:
        return {"checked": False, "loaded": None, "detail": "probe failed: %s" % ex,
                "js_runtimes": "", "bgutil_lines": ""}
    lines = [ln.strip() for ln in out.splitlines()]
    dir_line = next((ln for ln in lines if "Plugin directories" in ln), "")
    js_line = next((ln for ln in lines if "JS runtimes" in ln), "")
    # The repo PATH itself contains "bgutil", so exclude the lines that merely echo it (the
    # argv dump and the dir line) — what's left is genuine plugin chatter (warnings/tracebacks).
    bgutil = "\n".join(ln for ln in lines
                       if "bgutil" in ln.lower()
                       and ln != dir_line and "Command-line config" not in ln)
    return {"checked": True,
            "loaded": (_pot_repo_dir() in dir_line) if dir_line else None,
            "detail": dir_line,
            "js_runtimes": js_line.split(":", 1)[-1].strip() if js_line else "",
            "bgutil_lines": bgutil}


def _set_pdeathsig():
    """Ask the kernel to SIGKILL the Deno child if FinTube dies, so the sidecar can never be
    left orphaned (Linux PR_SET_PDEATHSIG = 1). Best-effort; runs in the forked child."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGKILL)
    except Exception:
        pass


def _ensure_pot_server(wait=25.0):
    """Start the token server if the provider is active and it isn't already listening.
    Returns True once something is listening on the port. No-op (returns False) when the
    provider isn't installed/enabled, so normal calls are entirely unaffected. `wait`
    bounds the TOTAL time this caller may spend here (lock join + port wait): the resolve
    hot path passes a short grace so it never owns a slow boot — False there just means
    the dump runs token-free (its gate re-checks the port), while the in-flight boot keeps
    going on the owner thread and serves the next call."""
    if not _pot_active():
        return False
    if _pot_ready_on_port():
        return True
    global _pot_proc, _pot_last_error
    deadline = time.time() + wait
    # A boot may already be in flight under the lock (prewarm's call, usually). Join it only
    # within this caller's budget: the 2026-09-08 field log showed a resolve on a CPU-starved
    # device blocking ~11s on the lock, then burning its OWN full window on a server that never
    # came up — 40s spent to conclude "proceed token-free". Timing out here abandons nothing:
    # the holder's boot continues regardless.
    if not _pot_lock.acquire(timeout=max(0.1, deadline - time.time())):
        return _pot_ready_on_port()
    try:
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
            # Spawn on a DEDICATED long-lived daemon thread that then parks on the child for its
            # whole life. PR_SET_PDEATHSIG is armed against the THREAD that forks the child, not the
            # process — so if the sidecar were Popen'd on a short-lived caller (the install thread, a
            # download thread, or a reader-thread re-resolve) the kernel would SIGKILL it the instant
            # that caller returned: the "server dies just after Provider ready" bug. Parking here
            # keeps pdeathsig armed to fire only when the app itself exits, whoever asked to start it.
            spawned = threading.Event()
            def _own_pot_server():
                global _pot_proc, _pot_last_error
                try:
                    proc = subprocess.Popen(
                        _pot_server_flags(), cwd=_pot_server_dir(), env=env,
                        stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                        preexec_fn=_set_pdeathsig)
                except Exception as ex:
                    _pot_last_error = "Couldn't launch the Deno server: " + str(ex)
                    _pot_proc = None
                    spawned.set()
                    return
                _pot_proc = proc
                atexit.register(stop_pot_server)
                spawned.set()
                try:
                    proc.wait()          # park for the child's whole life (pdeathsig stays armed here)
                except Exception:
                    pass
            threading.Thread(target=_own_pot_server, daemon=True,
                             name="pot-server-owner").start()
            spawned.wait(5)              # the Popen is near-instant; let it happen before we poll
            if _pot_proc is None:
                return False             # Popen failed — _pot_last_error already set by the owner
        # The server LISTENS quickly (on an unloaded device); the BotGuard VM warms on the first
        # token request, which the yt-dlp plugin waits out itself — so we only wait for the port
        # to open, and only within this caller's remaining budget.
        while time.time() < deadline:
            if _pot_ready_on_port():
                _pot_last_error = ""
                return True
            if _pot_proc is None or _pot_proc.poll() is not None:
                _pot_last_error = ("Provider server exited (code %s) just after starting — see the "
                                   "server log in the diagnostics below."
                                   % (_pot_proc.poll() if _pot_proc is not None else "?"))
                return False   # died during startup — see potprovider/server.log
            time.sleep(0.3)
        _pot_last_error = "Provider server didn't open port %d within %.0fs." % (_POT_PORT, wait)
        return _pot_ready_on_port()
    finally:
        _pot_lock.release()


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


_YTDLP_WARM_DELAY = 5.0       # seconds after launch: past the launch feed refresh + classification
_ytdlp_warm_armed = False


def _delayed_ytdlp_warm():
    _import_yt_dlp()
    if _DEBUG:          # profiling: log the isolated yt-dlp spawn tax once per launch
        _spawn_tax_probe()


def prewarm():
    """Start the PO-token server in the background at app launch, so the first resolve doesn't pay
    the ~2s Deno startup on its critical path. No-op unless the provider is installed + enabled.
    Runs on its OWN daemon thread so the PyOtherSide worker (and the UI behind it) never blocks on
    the port wait — fire-and-forget from QML at startup."""
    global _ytdlp_warm_armed
    # Warm the yt-dlp zipapp import (and, under debug, the spawn-tax probe) a few seconds AFTER
    # launch, once per process (prewarm is also the provider "nudge"): the import is ~1.4s of CPU-bound Python on device, and run at once it starved the
    # launch feed refresh (9 parallel RSS fetches took 1.1–1.4s each, finishing as it finished).
    # Nothing at launch needs it: resolve, lists and the feed go through InnerTube first, and any
    # fallback that does need yt-dlp imports it on demand (the import is module-global and locked).
    # _autofetch_zipapp then backfills a missing copy in the background (once per launch).
    if not _ytdlp_warm_armed:
        _ytdlp_warm_armed = True
        _t = threading.Timer(_YTDLP_WARM_DELAY, _delayed_ytdlp_warm)
        _t.daemon = True
        _t.name = "ytdlp-import-prewarm"
        _t.start()
    # InnerTube fast path: mint the player visitor id now so the first tap-to-play is ONE request.
    threading.Thread(target=_innertube_prewarm, daemon=True, name="innertube-prewarm").start()
    _autofetch_zipapp()
    _pot_rotate_log()   # fresh server.log per launch (keeps the previous one as server.log.prev)
    if not _pot_active():
        return
    # _ensure_pot_server() now brings the sidecar up on its OWN dedicated owner thread that parks on
    # the child (PR_SET_PDEATHSIG is armed against the forking thread, so it must be a long-lived
    # one) — so prewarm just has to TRIGGER it off the UI path. A throwaway daemon thread is fine: it
    # returns as soon as the port is up (or the 25s start times out), and the owner thread it spun up
    # keeps the sidecar alive until app exit.
    threading.Thread(target=_ensure_pot_server, daemon=True, name="pot-prewarm").start()


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
        # True when the Deno in use is the APP-MANAGED copy — the one that has no other
        # updater, so the UI offers "Update Deno" for it (a system/user Deno is theirs).
        "deno_managed": bool(deno) and deno == _managed_deno(),
        # 32-bit ARM: no Deno build exists, so the UI explains instead of offering the setup.
        "arm32": _ARM32,
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
    # The axis nothing else checks: does THIS yt-dlp binary actually load our plugin? A healthy
    # server + an unloaded plugin still means token-less gated videos. One offline spawn.
    probe = (_pot_plugin_probe() if (installed and ytdlp)
             else {"checked": False, "loaded": None, "detail": "",
                   "js_runtimes": "", "bgutil_lines": ""})

    L = []
    L.append("FinTube / FinTune — PO-token provider diagnostics")
    L.append("app data dir: " + _data_dir())
    L.append("userland    : " + ("32-bit ARM (no Deno build: provider unavailable, yt-dlp stable only)"
                                 if _ARM32 else "64-bit"))
    L.append("")
    L.append("Deno   : " + ((deno + "  (v" + (deno_version() or "?") + ")") if deno else "NOT FOUND"))
    L.append("git    : " + (git or "NOT FOUND"))
    L.append("yt-dlp : " + ((ytdlp + "  (" + (ytdlp_version() or "?") + ")") if ytdlp else "NOT FOUND"))
    L.append("ffmpeg : " + ((ffmpeg + "  (" + (ffmpeg_version() or "?") + ")") if ffmpeg
                            else "not installed (HD merge unavailable)"))
    L.append("")
    L.extend(_it_fallback_report())
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
    if probe["checked"]:
        if probe["loaded"] is True:
            L.append("plugin in yt-dlp   : loads (" + probe["detail"] + ")")
        elif probe["loaded"] is False:
            L.append("plugin in yt-dlp   : NOT LOADED — " + (probe["detail"] or "not resolved")
                     + " — yt-dlp runs WITHOUT the token plugin; reinstall the provider or "
                       "update yt-dlp")
        else:
            L.append("plugin in yt-dlp   : undetermined ("
                     + (probe["detail"] or "no plugin report from this yt-dlp") + ")")
        L.append("yt-dlp JS runtime  : " + (probe["js_runtimes"] or "(not reported)"))
        if probe["bgutil_lines"]:
            L.append("bgutil mentions    : " + probe["bgutil_lines"][:300])
    # A confirmed-unloaded plugin overrides "working": the server answering is irrelevant if
    # yt-dlp never calls it.
    verdict = ("working" if (enabled and ping["ok"] and probe["loaded"] is not False)
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
        "plugin_loaded": probe["loaded"], "js_runtimes": probe["js_runtimes"],
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


def install_pot_provider(tag=None, persist_tag=False):
    """Clone + set up the bgutil PO-token provider (opt-in). Background thread; progress and
    the final result go to QML via pyotherside, mirroring install_ytdlp(). `persist_tag` records
    the tag as the chosen `pot_tag` ONLY once the install succeeds (set by update_pot_provider).

    Deps are installed WITHOUT --allow-scripts, so npm lifecycle scripts never run during
    setup (node-canvas's native binary is skipped — jsdom degrades gracefully without it,
    which is what lets the server run with ffi fully denied)."""
    import pyotherside

    if _ARM32:
        pyotherside.send("pot_install_done", False, _ARM32_NO_DENO)
        return {"ok": False}

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
            # Build into a STAGING dir and swap it in only once everything succeeds, so a failure
            # partway through (network drop, bad tag, dep-install error) leaves any EXISTING working
            # install untouched instead of destroying it up-front. (M3)
            staging = repo + ".new"
            shutil.rmtree(staging, ignore_errors=True)    # clear a stale temp from a prior failed run
            pyotherside.send("pot_install_progress", "Cloning provider (" + the_tag + ")…")
            cp = subprocess.run(
                [git, "clone", "--depth", "1", "--branch", the_tag, "--single-branch",
                 _POT_REPO, staging],
                capture_output=True, text=True, timeout=240)
            if cp.returncode != 0:
                shutil.rmtree(staging, ignore_errors=True)
                _pot_last_error = "Clone failed: " + (cp.stderr.strip()[-200:] or "git error")
                pyotherside.send("pot_install_done", False,
                                 "Clone failed: " + (cp.stderr.strip()[-200:] or "git error"))
                return
            pyotherside.send("pot_install_progress", "Installing dependencies (Deno)…")
            server = os.path.join(staging, "server")
            lock = os.path.join(server, "deno.lock")
            base = [deno, "install"]
            if get_settings().get("pot_needs_ffi"):
                base.append("--allow-scripts")   # only if node-canvas's native build is needed
            cmd = base + (["--frozen"] if os.path.isfile(lock) else [])
            dp = subprocess.run(cmd, cwd=server, capture_output=True, text=True, timeout=900)
            if dp.returncode != 0 and "--frozen" in cmd:   # lock mismatch? retry unlocked
                dp = subprocess.run(base, cwd=server, capture_output=True, text=True, timeout=900)
            if dp.returncode != 0:
                shutil.rmtree(staging, ignore_errors=True)
                _pot_last_error = "Dependency install failed: " + (dp.stderr.strip()[-200:] or "deno error")
                pyotherside.send("pot_install_done", False,
                                 "Dependency install failed: " + (dp.stderr.strip()[-200:] or "deno error"))
                return
            if not os.path.isfile(os.path.join(server, "src", "main.ts")):
                shutil.rmtree(staging, ignore_errors=True)
                pyotherside.send("pot_install_done", False,
                                 "Setup finished but the server entry is missing.")
                return
            # Staging built cleanly. Stop the OLD sidecar FIRST — otherwise it keeps serving on the
            # port and _ensure_pot_server() below would see the port open and never restart, so an
            # UPDATE would silently keep running the old server/plugin version. (M3)
            stop_pot_server()
            # Swap the new tree in with two same-filesystem renames (sub-millisecond window; the
            # previous install stays recoverable under .old until the new one is promoted).
            old = repo + ".old"
            shutil.rmtree(old, ignore_errors=True)
            if os.path.isdir(repo):
                os.rename(repo, old)
            os.rename(staging, repo)
            shutil.rmtree(old, ignore_errors=True)
            with open(_pot_marker(), "w") as f:
                f.write(the_tag)
            if persist_tag:
                set_setting("pot_tag", the_tag)  # remember the updated tag ONLY after a clean install
            set_setting("pot_provider", True)    # installed → enabled
            _ensure_pot_server()                 # fresh start (old one stopped above) so the new
                                                 # server + plugin version actually takes effect
            pyotherside.send("pot_install_done", True,
                             "Provider ready (" + the_tag + "). Videos now fetch a per-video token.")
        except subprocess.TimeoutExpired:
            shutil.rmtree(_pot_repo_dir() + ".new", ignore_errors=True)
            pyotherside.send("pot_install_done", False, "Setup timed out.")
        except Exception as ex:
            shutil.rmtree(_pot_repo_dir() + ".new", ignore_errors=True)
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
    # Persist the new tag only AFTER a successful install (inside install_pot_provider) — otherwise a
    # failed update would leave the setting claiming a version that was never actually installed. (M3)
    return install_pot_provider(latest, persist_tag=True)


# YouTube search filter tokens (the results-page `sp` query param — base64 of the filter
# protobuf, percent-encoded). ytsearch: is video-only, so channel search instead hits the
# results URL with the channel filter applied.
_SEARCH_SP = {
    "channel": "EgIQAg%3D%3D",
    "playlist": "EgIQAw%3D%3D",
}


def _pb_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_field(num, val):
    """One protobuf varint field (wire type 0): tag byte then the varint value."""
    return bytes([num << 3]) + _pb_varint(val)


# YouTube's search `sp` param is a tiny protobuf, base64- then percent-encoded. Field layout is
# CONFIRMED by decoding YouTube's own codes (channel = EgIQAg== = 12 02 10 02; dur<4min = EgIYAQ==
# = 12 02 18 01; sort-by-views = CAM= = 08 03):
#   top field 1  = sort_by   (1 rating, 2 upload-date, 3 view-count; 0/relevance omitted)
#   sub  field 1 = uploaded  (1 hour, 2 today, 3 week, 4 month, 5 year)
#   sub  field 2 = type      (1 video, 2 channel, 3 playlist)
#   sub  field 3 = duration  (1 <4min, 2 >20min, 3 4-20min)
def _search_filter_sp(sort=0, date=0, dur=0, video_only=False):
    """Build the `sp=` value (percent-encoded) for a filtered search, or "" when nothing is set.
    video_only pins type=video so the results page returns clean video rows, not mixed shelves."""
    top = _pb_field(1, sort) if sort else b""
    sub = b""
    if date:
        sub += _pb_field(1, date)         # fields kept in ascending order, as YouTube emits them
    if video_only:
        sub += _pb_field(2, 1)
    if dur:
        sub += _pb_field(3, dur)
    if sub:
        top += bytes([(2 << 3) | 2]) + _pb_varint(len(sub)) + sub   # field 2, length-delimited
    if not top:
        return ""
    return urllib.parse.quote(base64.b64encode(top).decode("ascii"), safe="")


def parse_youtube_url(url):
    """Classify an incoming YouTube link → {kind, id, url}. kind ∈ video|channel|playlist|"".

    Order matters: a watch URL can carry both v= and list= (a video inside a playlist); we
    open the video, so v=/youtu.be/shorts are matched before a bare list=.
    """
    if isinstance(url, (list, tuple)):   # D-Bus activation may hand the URL as a 1-element array
        url = url[0] if url else ""
    if not url:
        return {"kind": "", "id": "", "url": ""}
    u = str(url).strip()
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


# InnerTube search pages: the web app's own /search, paged by continuation like the channel tabs
# (_it_channel). Page 1 is fetched once per query; scrolling deeper only fetches what's new —
# the yt-dlp path re-downloaded the whole window (ytsearch<end>) for every page.
_search_pages = {}             # (query, params, kind) -> {"ts", "items": [innertube items], "cont"}
_search_pages_lock = threading.Lock()
_SEARCH_PAGES_TTL = 600
_SEARCH_FIRST_FRESH = 120      # a new search (start=1) reuses a page 1 younger than this
_SEARCH_PAGES_MAX = 8


def _it_search_window(query, params, want, end, fresh_first):
    """innertube.search results of kind `want` for (query, params), at least `end` of them when
    the results run that deep. Raises on failure; search() then falls back to yt-dlp."""
    it = _it()
    key = (query, params, want)
    now = time.time()
    with _search_pages_lock:
        ent = _search_pages.get(key)
        if ent and (now - ent["ts"] > _SEARCH_PAGES_TTL
                    or (fresh_first and now - ent["ts"] > _SEARCH_FIRST_FRESH)):
            ent = None
        ent = dict(ent, items=list(ent["items"])) if ent else None
    if ent is None:
        r = it.search(query, params=params)
        ent = {"ts": now, "items": [e for e in r["items"] if e.get("kind") == want],
               "cont": r["continuation"]}
        if not ent["items"]:
            # Almost always a YouTube layout change our parser doesn't know yet; yt-dlp's (kept
            # current upstream) gets the query. A genuinely empty search just pays one yt-dlp run.
            raise it.InnertubeError("search page parsed empty")
    pages = 0
    while len(ent["items"]) < end and ent["cont"] and pages < 8:
        r = it.search(continuation=ent["cont"])
        pages += 1
        ent["items"] += [e for e in r["items"] if e.get("kind") == want]
        ent["cont"] = r["continuation"]
        if not r["items"]:
            break
    with _search_pages_lock:
        _search_pages[key] = ent
        for k in sorted(_search_pages, key=lambda k: _search_pages[k]["ts"])[
                :max(0, len(_search_pages) - _SEARCH_PAGES_MAX)]:
            _search_pages.pop(k, None)
    return ent


def _it_list_video(e, now):
    """An innertube search/related item → the list-row shape _video_entry produces."""
    ago = e.get("ago")
    return {
        "type": "video",
        "id": e["id"],
        "title": e.get("title") or "(untitled)",
        "uploader": e.get("channel") or "",
        "duration": e.get("duration") or 0,
        "thumbnail": _video_thumb(e["id"]),
        "url": "",
        "subscribers": 0,
        "views": e.get("views") or 0,
        "posted": _rel_from_ts(now - ago) if ago is not None else "",
        "live": 1 if e.get("live") else 0,
    }


def _it_is_short(e):
    d = e.get("duration") or 0
    return bool(e.get("short")) or 0 < d <= _SHORT_MAX_SECS


def _search_innertube(query, n, kind, start, sp):
    """search() through innertube, or None to fall back to yt-dlp."""
    if _it() is None:
        return None
    want = "channel" if kind == "channel" else "video"
    end = start + n - 1
    _t = time.time()
    try:
        ent = _it_search_window(query, sp, want, end, fresh_first=(start <= 1))
    except Exception as ex:
        _it_note("search", ex)
        _tlog("search innertube failed %.2fs → yt-dlp: %s" % (time.time() - _t, str(ex)[:120]))
        return None
    _tlog("search innertube %.2fs [%d:%d] of %d" % (time.time() - _t, start, end, len(ent["items"])))
    raw = ent["items"]
    page = raw[start - 1:end]
    has_more = len(page) >= n and (bool(ent["cont"]) or len(raw) > end)
    if want == "channel":
        items = [{"type": "channel", "id": e["id"], "title": e.get("title") or "(channel)",
                  "uploader": "", "duration": 0,
                  "thumbnail": _pick_avatar({"thumbnails": e.get("avatars") or []}, 176),
                  "url": e.get("url") or "", "subscribers": e.get("subscribers") or 0,
                  "views": 0, "posted": ""} for e in page]
        return {"ok": True, "items": items, "kind": kind, "has_more": has_more,
                "filtered_members": 0}
    kept = [e for e in page if not e.get("members")]
    filtered_members = len(page) - len(kept)
    if _hide_shorts():
        kept = [e for e in kept if not _it_is_short(e)]
    now = time.time()
    return {"ok": True, "items": [_it_list_video(e, now) for e in kept], "kind": kind,
            "has_more": has_more, "filtered_members": filtered_members}


@_timed_fn("q.search")
def search(query, n=15, kind="video", start=1, filters=None):
    """One page of search results. `start` is the 1-based index of the first result wanted, so
    the UI can page in more as it scrolls (mirrors channel_videos). Members-only videos are
    dropped (they can't be played without the membership); Shorts too when hide_shorts is on.

    `filters` (video kind only) = {"sort","date","dur"} in yt `sp` values; when any is set the
    query runs through the results page with an `sp=` filter instead of plain ytsearch.
    InnerTube /search answers first (_search_innertube); yt-dlp is the fallback."""
    path = _ytdlp_path()
    if kind not in ("video", "channel"):
        kind = "video"
    f = filters or {}
    try:
        fsort = int(f.get("sort") or 0)
        fdate = int(f.get("date") or 0)
        fdur = int(f.get("dur") or 0)
    except (TypeError, ValueError):
        fsort = fdate = fdur = 0
    try:
        start = max(1, int(start))
        n = max(1, int(n))
    except (TypeError, ValueError):
        start, n = 1, 15
    # Fast path: InnerTube /search with the same sp= filter. Video searches always pin
    # type=video, matching ytsearch's video-only results. Any failure → yt-dlp below.
    fast = _search_innertube(query, n, kind, start,
                             _SEARCH_SP["channel"] if kind == "channel" else
                             _search_filter_sp(fsort, fdate, fdur, video_only=True))
    if fast is not None:
        return fast
    if not path:
        return {"ok": False, "error": "yt-dlp not found"}
    try:
        end = start + n - 1
        if kind == "channel":
            target = ("https://www.youtube.com/results?search_query=%s&sp=%s"
                      % (urllib.parse.quote(query), _SEARCH_SP["channel"]))
        elif fsort or fdate or fdur:
            # A filter is set → run the results page with an sp= filter (video-only). Paging still
            # works: --playlist-items below slices [start:end] out of the paginated results.
            sp = _search_filter_sp(fsort, fdate, fdur, video_only=True)
            target = ("https://www.youtube.com/results?search_query=%s&sp=%s"
                      % (urllib.parse.quote(query), sp))
        else:
            # ytsearch<end> fetches enough hits to cover the wanted window; --playlist-items
            # then returns just [start:end], so paging deeper is a bigger fetch sliced tighter.
            target = "ytsearch%d:%s" % (end, query)
        # `approximate_date` makes the Youtube(Tab) extractor emit an approximate `timestamp`
        # parsed from each result's "N years ago" text (publishedTimeText). Without it that field
        # is dropped and the row shows views but no post date. It only gates PARSING of data
        # already in the flat response — no extra request, so it's free. (Namespaced to youtubetab;
        # a harmless no-op on the channel-results path, which carries no per-item date.)
        xargs = ["--extractor-args", "youtubetab:approximate_date"]
        with _cookies_args() as cargs:
            proc = _ytdlp_run(
                path, [*_COMMON_ARGS, *cargs, *xargs, "--flat-playlist",
                       "--playlist-items", "%d:%d" % (start, end),
                       "--dump-single-json", "--", target], 90)
        if proc.returncode != 0:
            return {"ok": False, "error": _ytdlp_err(proc.stderr, "search failed")}
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


# watch-next answers (related videos, likes, upload date), shared by related() and video_info().
_next_cache = {}               # video id -> (ts, innertube.watch_next result)
_next_cache_lock = threading.Lock()
_next_inflight = {}            # video id -> Event: the info view asks for details and comments at once
_NEXT_TTL = 600
_NEXT_MAX = 32


def _watch_next(vid):
    """innertube.watch_next(vid), cached briefly and single-flight; None when unavailable or it
    failed."""
    it = _it()
    if it is None or not re.fullmatch(r"[\w-]{11}", vid or ""):
        return None
    now = time.time()
    with _next_cache_lock:
        hit = _next_cache.get(vid)
        if hit and now - hit[0] < _NEXT_TTL:
            return hit[1]
        ev = _next_inflight.get(vid)
        leader = ev is None
        if leader:
            ev = _next_inflight[vid] = threading.Event()
    if not leader:                      # join the request already out instead of sending a twin
        ev.wait(15)
        with _next_cache_lock:
            hit = _next_cache.get(vid)
        return hit[1] if hit else None
    _t = time.time()
    try:
        r = it.watch_next(vid)
        _tlog("watch_next %.2fs (%d related)" % (time.time() - _t, len(r.get("related") or [])))
        with _next_cache_lock:
            _next_cache[vid] = (now, r)
            for k in sorted(_next_cache, key=lambda k: _next_cache[k][0])[
                    :max(0, len(_next_cache) - _NEXT_MAX)]:
                _next_cache.pop(k, None)
        return r
    except Exception as ex:
        _it_note("watch-next", ex)
        _tlog("watch_next failed %.2fs: %s" % (time.time() - _t, str(ex)[:120]))
        return None
    finally:
        with _next_cache_lock:
            _next_inflight.pop(vid, None)
        ev.set()


@_timed_fn("q.related")
def related(video_id, n=20):
    """Recommendations for a video: the InnerTube watch-next sidebar first (one POST, shared with
    video_info), else its YouTube autoplay Mix (watch?v=ID&list=RD<ID>) pulled flat through
    yt-dlp. The seed video is dropped; Shorts are filtered when hide_shorts is on."""
    vid = video_id or ""
    if "://" in vid:                     # accept a full watch URL too → pull its v= id for the RD list
        try:
            vid = urllib.parse.parse_qs(urllib.parse.urlparse(vid).query).get("v", [vid])[0]
        except Exception:
            pass
    if not vid:
        return {"ok": False, "error": "no video id"}
    nx = _watch_next(vid)
    if nx and nx.get("related"):
        try:
            n = max(1, int(n))
        except (TypeError, ValueError):
            n = 20
        ents = [e for e in nx["related"] if e.get("id") != vid and not e.get("members")]
        if _hide_shorts():
            ents = [e for e in ents if not _it_is_short(e)]
        now = time.time()
        return {"ok": True, "items": [_it_list_video(e, now) for e in ents[:n]]}
    path = _ytdlp_path()
    if not path:
        return {"ok": False, "error": "yt-dlp not found"}
    try:
        n = max(1, int(n))
        url = "https://www.youtube.com/watch?v=%s&list=RD%s" % (
            urllib.parse.quote(vid), urllib.parse.quote(vid))
        with _cookies_args() as cargs:
            proc = _ytdlp_run(
                path, [*_COMMON_ARGS, *cargs,
                       "--extractor-args", "youtubetab:approximate_date",
                       "--flat-playlist", "--playlist-items", "1:%d" % (n + 1),   # +1: seed is item 1
                       "--dump-single-json", "--", url], 90)
        if proc.returncode != 0:
            return {"ok": False, "error": _ytdlp_err(proc.stderr, "no recommendations")}
        data = json.loads(proc.stdout)
        entries = [e for e in (data.get("entries") or [])
                   if e and e.get("id") and e.get("id") != vid]
        if _hide_shorts():
            entries = [e for e in entries if not _is_short(e)]
        items = [it for it in (_video_entry(e) for e in entries) if it.get("id")][:n]
        return {"ok": True, "items": items}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "recommendations timed out"}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


_sugg_cache = {}               # query -> (ts, [suggestions]); backspacing over a prefix is free
_SUGG_TTL = 600
_SUGG_MAX = 128


def search_suggestions(query):
    """YouTube search autocomplete via Google's public suggest endpoint — a cheap HTTP
    call, no yt-dlp. Returns ["term", ...]. Rides innertube's keep-alive pool when present, so a
    keystroke costs one request on a warm connection instead of a fresh TLS handshake."""
    q = (query or "").strip()
    if not q:
        return {"ok": True, "suggestions": []}
    hit = _sugg_cache.get(q)
    if hit and time.time() - hit[0] < _SUGG_TTL:
        return {"ok": True, "suggestions": list(hit[1])}
    _force_ipv4()
    url = ("https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&q=%s"
           % urllib.parse.quote(q))
    try:
        it = _it()
        if it is not None:
            status, body = it._transport("GET", url, None, {"User-Agent": _BROWSER_UA}, 5)
            if status != 200:
                raise IOError("HTTP %s" % status)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
            with urllib.request.urlopen(req, timeout=5) as r:
                body = r.read()
        data = json.loads(body.decode("utf-8", "replace"))
        sugg = data[1] if isinstance(data, list) and len(data) > 1 else []
        out = [s for s in sugg if isinstance(s, str)][:10]
        _sugg_cache[q] = (time.time(), out)
        if len(_sugg_cache) > _SUGG_MAX:
            for k in sorted(_sugg_cache, key=lambda k: _sugg_cache[k][0])[
                    :len(_sugg_cache) - _SUGG_MAX]:
                _sugg_cache.pop(k, None)
        return {"ok": True, "suggestions": out}
    except Exception:
        return {"ok": False, "suggestions": []}


def _caption_pick_json3(fmts):
    """From a yt-dlp caption format list, the json3 entry (has per-cue timing we can
    parse), or None. json3 is preferred over srv1/2/3/vtt/ttml for its clean segments."""
    for f in fmts or []:
        if (f.get("ext") or "").lower() == "json3" and f.get("url"):
            return f
    return None


def _caption_tracks(data):
    """Split yt-dlp caption data into the short real-track list and the big auto-translate
    list — returns (tracks, translations).

    tracks: creator-uploaded subtitles (kind="manual") + the ONE genuine auto-generated
            (ASR) track per language (kind="asr"). Almost always <=6 rows.
    translations: every machine-translated auto-caption — auto_caption entries whose URL
            carries &tlang=. This is the ~100-language set, kept separate so the UI can
            demote it behind an opt-in, filterable list instead of dumping it inline."""
    tracks, translations = [], []
    seen = set()
    for code, fmts in (data.get("subtitles") or {}).items():
        if code.startswith("live_chat"):
            continue
        fmt = _caption_pick_json3(fmts)
        if not fmt:
            continue
        tracks.append({"lang": code, "name": fmt.get("name") or code,
                       "kind": "manual", "url": fmt["url"]})
        seen.add(code)
    for code, fmts in (data.get("automatic_captions") or {}).items():
        fmt = _caption_pick_json3(fmts)
        if not fmt:
            continue
        name = fmt.get("name") or code
        if "tlang=" in fmt["url"]:
            translations.append({"lang": code, "name": name, "url": fmt["url"]})
        elif code not in seen:
            # base ASR for this language (no tlang) — a genuine "auto-generated" track
            tracks.append({"lang": code, "name": name, "kind": "asr", "url": fmt["url"]})
            seen.add(code)
    # Drop a machine-translation for any language we already have a real track for (YouTube offers
    # auto-translate to EVERY language, so e.g. a video with manual Japanese still lists a
    # translated Japanese — the real track wins, so it doesn't belong in the translate list).
    translations = [t for t in translations if t["lang"] not in seen]
    translations.sort(key=lambda t: (t["name"] or t["lang"]).lower())
    return tracks, translations


def resolve(video_id):
    """Cache-first resolve. Returns a fresh cached result instantly; joins an in-flight prefetch
    for the SAME key instead of spawning a second yt-dlp; else resolves and caches. Same
    {ok, info|error} shape — every caller (Backend.resolve) is unchanged."""
    return _resolve_and_cache(video_id)


def _resolve_uncached(video_id):
    """Resolve a video to playable stream URLs.

    `muxed_url` (single stream, routed through the local proxy) feeds the prototype
    player; the raw `video_url` + `audio_url` pair is for the dual-source pipeline.
    """
    path = _ytdlp_path()
    url = video_id
    if "://" not in url:
        url = "https://www.youtube.com/watch?v=" + video_id
    _t0 = time.time()
    # FAST PATH: one InnerTube player request (see _fast_player_data). When it yields a playable HD
    # pair, yt-dlp, the PO-token sidecar and the probe below are skipped entirely; anything it
    # can't serve (live, restricted, bot-walled, ciphered) drops to the unchanged yt-dlp ladder.
    fast = _fast_player_data(video_id) if "://" not in video_id else None
    if fast is None:
        if not path:
            return {"ok": False, "error": "yt-dlp not found"}
        # Bring the PO-token sidecar up (no-op unless installed+enabled) — with a short grace, never
        # owning the boot: the primary ladder is token-free, so when the server isn't up
        # in time the dump proceeds without it and the still-booting server serves the next resolve.
        _ensure_pot_server(wait=4)
        _tlog("pot_ensure %.2fs" % (time.time() - _t0))
    def _dump(extra, anon=False):
        """Run yt-dlp --dump-single-json with extra args; return (data, error).

        Fast resolve (opt-in): the TOKEN-FREE hot dump runs IN-PROCESS via the warm YoutubeDL,
        skipping the ~1.3s frozen-binary spawn tax; ANY failure falls through to the binary below.
        The token path (fetch_pot=always baked into `extra`) always takes the binary — that's
        where the bgutil PO-token plugin lives — so the in-process path never needs it.
        (Provider INACTIVE → a widen retry carries no fetch_pot marker and may run in-process
        too; equivalent by construction, since without the provider the binary loads no plugin.)

        anon=True resolves WITHOUT the login cookies. YouTube gates token-free clients
        HARD for AUTHENTICATED requests but not anonymous ones — confirmed on-device 2026-09-05: the
        same videos that 403'd (→ ~10s mweb+token fallback) signed-in resolved token-free in ~1.3s
        signed OUT. So the primary dump goes anonymous (public videos skip the gate entirely) and only
        the fallback re-runs WITH cookies, for genuinely restricted content (age-gated/members/private)."""
        _td = time.time()
        if _DEBUG and _pot_active():   # was the token server actually ANSWERING when we extracted?
            _tlog("dump gate: port=%s http=%r" % (_pot_ready_on_port(), _pot_http_ping(0.5)["ok"]))
        if "fetch_pot=always" not in " ".join(extra) and _fast_resolve_ready():
            try:
                data = _inproc_dump(url, extra, anon)
                if _DEBUG:   # break the wall time into Deno (n-sig) vs the rest (network + CPU)
                    _dn = getattr(_inproc_tls, "deno", None) or [0, 0.0]
                    _tot = time.time() - _td
                    _tlog("dump(inproc) %.2fs [deno %dx %.2fs | rest %.2fs]"
                          % (_tot, _dn[0], _dn[1], max(0.0, _tot - _dn[1])))
                else:
                    _tlog("dump(inproc) %.2fs" % (time.time() - _td))
                return data, ""
            except Exception as ex:
                if _final_error(str(ex)):     # the binary would only say the same, ~1 s later
                    _tlog("dump(inproc) final %.2fs: %s" % (time.time() - _td, str(ex)[:120]))
                    return None, _ytdlp_err(str(ex), "resolve failed")
                _tlog("dump(inproc) failed %.2fs → binary: %s"
                      % (time.time() - _td, str(ex)[:120]))
        with (contextlib.nullcontext([]) if anon else _cookies_args()) as cargs:
            proc = subprocess.run(
                [path, *_COMMON_ARGS, *cargs, *_pot_ytdlp_args(), *extra,
                 "--dump-single-json", "--", url],
                capture_output=True, text=True, timeout=90,
                preexec_fn=_set_pdeathsig)   # D6: SIGKILL an orphaned prefetch child with the app
        _tlog("dump %.2fs rc=%d%s" % (time.time() - _td, proc.returncode, " anon" if anon else ""))
        if proc.returncode != 0:
            return None, _ytdlp_err(proc.stderr, "resolve failed")
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
        _client_used = _default_client() or "auto"   # which client actually produced the URLs (debug)
        if fast is not None:
            # Fast-path data carries a probed HD pair (or, live, a muxed HLS rung): none of the
            # widen/probe branches below may fire for it.
            data, err = fast, ""
            _client_used = "innertube:" + ("live" if fast.get("is_live")
                                           else _it().player_client()[0])
        else:
            # Primary = token-free AND cookie-free. Authenticated token-free requests get gated by
            # YouTube; anonymous ones don't. Public videos resolve here fast + un-gated; a restricted
            # video fails this and drops to the cookie'd (+token) fallback below. (on-device 2026-09-05)
            data, err = _dump(_yt_extractor_args(), anon=True)
        # A hard failure (data is None) is usually YouTube's "confirm you're not a bot" check
        # tripping this client — retry once with the wider set. tv/android_vr use different
        # attestation and often pass where web/web_embedded get bot-checked.
        if data is None:
            if not _final_error(err):       # removed / not yet live / geo-blocked: no client helps
                data2, err2 = _dump(_yt_extractor_args(client_override=_RETRY_CLIENTS,
                                                       want_pot=True))
                if data2 is not None:
                    data = data2; _client_used = _RETRY_CLIENTS + "(widen)"
                else:
                    err = err or err2
        # The token-free primary usually returns the full fetchable ladder. If SABR
        # degraded it to muxed-only (no HD dual-source pair), widen the client net once to
        # hunt for a fetchable HD pair elsewhere — only switch if the result is actually
        # better (HD found, or the primary had nothing playable at all).
        elif fast is None and not _hd_pair(data):
            data2, _ = _dump(_yt_extractor_args(client_override=_RETRY_CLIENTS, want_pot=True))
            if data2 is not None and _hd_pair(data2):
                data = data2; _client_used = _RETRY_CLIENTS + "(widen)"
            elif data2 is not None and not _playable(data) and _playable(data2):
                data = data2; _client_used = _RETRY_CLIENTS + "(widen)"
        if data is None:
            return {"ok": False, "error": err}
        formats = data.get("formats", [])
        if data.get("live_status") == "post_live":
            # A stream that just ended: until YouTube has processed the recording, its formats are
            # DASH segment lists (sq=0..N) and the proxy serves one URL as one file, so the first
            # ~1 MB segment would play and then stop. Refuse with the reason instead.
            formats = [f for f in formats if f.get("protocol") != "http_dash_segments"]
            data["formats"] = formats
            if not _playable(data):
                return {"ok": False, "error": "This livestream just ended and YouTube is still "
                                              "processing the recording. Try again later."}
        # Token-free probe: the primary client is the fast default and needs NO token, but token-free
        # clients are the ones YouTube gates unpredictably — when gated the stream 403s the instant
        # playback fetches it. Probe one chosen URL; on a real 403 re-extract with the reliable TOKEN
        # path (mweb + a minted PO token) — done HERE, before playback, so the itags stay consistent
        # (a mid-stream client switch can't: clients emit different itag shapes). Only when the
        # token-free client is OUR default choice (provider set up, no user-set player_client, no widen fired) — a user who
        # explicitly picks a client keeps it, and a widen result already left _client_used != it.
        _manual_client = (get_settings().get("player_client") or "").strip().lower() not in ("", "auto")
        if _pot_active() and not _manual_client and _client_used == _token_free_client():
            _pt = (_pick_audio(formats, "") or _pick_video(formats, 0)
                   or _pick(formats, _MUXED_ITAGS) or {})
            if _pt.get("url") and "m3u8" not in (_pt.get("protocol") or ""):
                _tp = time.time()
                _ok = _probe_url_ok(_pt["url"], (_pt.get("http_headers") or {}).get("User-Agent", ""))
                if _DEBUG:
                    _tlog("probe %.2fs ok=%s%s" % (time.time() - _tp, _ok,
                                                   "" if _ok else " → mweb fallback"))
                if not _ok:
                    data2, _ = _dump(_yt_extractor_args(client_override="mweb", want_pot=True))
                    if data2 is not None and _playable(data2):
                        data = data2
                        formats = data.get("formats", [])
                        _client_used = "mweb(gated-fallback)"
                        # mweb can (rarely) also come back SABR-thin; give the fallback the SAME
                        # HD-pair widen the normal path gets, so a gated video doesn't silently
                        # drop to 360p muxed when an HD pair was reachable via the wider client net.
                        if not _hd_pair(data):
                            data3, _ = _dump(_yt_extractor_args(client_override=_RETRY_CLIENTS,
                                                                want_pot=True))
                            if data3 is not None and ((_hd_pair(data3))
                                                      or (not _playable(data) and _playable(data3))):
                                data = data3
                                formats = data.get("formats", [])
                                _client_used = "mweb(gated)+widen"
        _s = get_settings()
        try:
            cap = int(_s.get("default_quality") or 0)
        except (TypeError, ValueError):
            cap = 0
        video = _pick_video(formats, cap)   # capped by the user's Default-quality setting
        audio = _pick_audio(formats, _s.get("audio_lang") or "")   # honour remembered dub language
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
        if _DEBUG:   # instant-403 probe: which client, and does the COLD dump carry a valid pot= token?
            _tlog("resolve picks %s [client=%s]: v=%s %s | a=%s %s | m=%s %s"
                  % (video_id, _client_used,
                     (video or {}).get("format_id"), _pot_of((video or {}).get("url", "")),
                     (audio or {}).get("format_id"), _pot_of((audio or {}).get("url", "")),
                     (muxed or {}).get("format_id"), _pot_of((muxed or {}).get("url", ""))))
            _tlog("resolve urls: v[%s] a[%s]" % (_url_shape((video or {}).get("url", "")),
                                                  _url_shape((audio or {}).get("url", ""))))
            if _pot_active():   # a first-mint crash/OOM shows here as an exit-code line between dumps
                _tlog("pot log: " + ((_pot_server_log_tail(6) or "(none)").replace("\n", " | ")))
        # HLS (m3u8) plays fine directly, and proxying the manifest breaks segment
        # resolution; only progressive URLs (itag 18) need the UA-injecting proxy.
        muxed_url = ""
        if muxed:
            if "m3u8" in muxed.get("protocol", ""):
                muxed_url = muxed["url"]
            else:
                muxed_url = _proxied(muxed["url"], video_id, muxed.get("format_id"), http_ua)
        # Quality menu. Distinct rows per (resolution, high-fps, premium): a 60fps upload gets its
        # own "1080p60" entry, and a premium enhanced-bitrate track its own "… Premium" entry,
        # instead of all collapsing into one "1080p". _video_candidates still preferred-codec-sorts
        # and caps at 1080p; deduping on the composite key keeps the FIRST (preferred codec) of each
        # variant. NOTE: this only shapes the MENU — the DEFAULT pick (_pick_video) is untouched, so
        # playback still starts on the decode-light 30fps H.264 rung; 60fps/Premium are opt-in taps.
        qualities = []
        seen_q = set()
        for qf in _video_candidates(formats):
            qh = qf.get("height") or 0
            qfps = qf.get("fps") or 0
            q_premium = "premium" in (qf.get("format_note") or "").lower()
            key = (qh, qfps > 30, q_premium)
            if key in seen_q:
                continue
            seen_q.add(key)
            label = "%dp" % qh
            if qfps > 30:
                label += "%d" % round(qfps)      # 1080p -> 1080p60 / 1080p50
            if q_premium:
                label += " Premium"
            qualities.append({
                "itag": str(qf.get("format_id") or ""),
                "label": label,
                "video_url": _proxied(qf["url"], video_id, qf.get("format_id"), http_ua),
                "height": qh, "fps": qfps, "premium": q_premium,
                "ext": qf.get("ext") or "",   # container rides along for the seek strategy
            })
        # Menu order: resolution high→low, then Premium first, then higher fps first.
        qualities.sort(key=lambda q: (-q["height"], 0 if q["premium"] else 1, -q["fps"]))
        # Audio-track picker: one entry per available LANGUAGE (best rung of each), original/default
        # first — mirrors _pick_audio's ordering. Dubbed videos only; single-language videos yield
        # <=1 entry and the UI hides the row. Each URL is proxied like the main audio track.
        audio_url = _proxied(audio["url"], video_id, audio.get("format_id"), http_ua) if audio else ""
        audio_tracks = []
        seen_alang = set()
        for af in _audio_candidates(formats):
            alang = (af.get("language") or "").strip()
            # Only LANGUAGE-TAGGED tracks are a dub choice. A normal single-audio video tags none of
            # its (many) rungs — skipping them keeps audio_tracks empty so the UI hides the row;
            # a dubbed video tags every track (original included), one entry per language.
            if not alang:
                continue
            akey = alang.lower()
            if akey in seen_alang:
                continue
            seen_alang.add(akey)
            audio_tracks.append({
                "lang": alang,
                "name": _audio_lang_name(af),
                "is_original": _audio_orig_pref(af) > 0,
                "itag": str(af.get("format_id") or ""),
                "audio_url": _proxied(af["url"], video_id, af.get("format_id"), http_ua),
                "ext": af.get("ext") or "",   # container rides along for the seek strategy
            })
        # A dubbed video whose ORIGINAL source audio yt-dlp left untagged is skipped above — yet it
        # is what's actually playing. When the picked track isn't already listed, prepend it as the
        # "Original" so it's shown, highlighted, and reselectable (its lang is usually "" → the next
        # video's _pick_audio treats that as "use the original"). Keeps the invariant that whatever
        # is playing always has a row. Only fires once a genuine dub list already exists.
        if audio_tracks and audio and \
                str(audio.get("format_id") or "") not in {t["itag"] for t in audio_tracks}:
            a_lang = (audio.get("language") or "").strip()
            audio_tracks.insert(0, {
                "lang": a_lang,
                "name": _audio_lang_name(audio) if a_lang else "Original",
                # name already reads "Original" here, so don't also append the "(original)" marker.
                "is_original": bool(a_lang) and _audio_orig_pref(audio) > 0,
                "itag": str(audio.get("format_id") or ""),
                "audio_url": audio_url,
                "ext": audio.get("ext") or "",
            })
        chapters = [{"start": c.get("start_time") or 0, "title": c.get("title") or ""}
                    for c in (data.get("chapters") or []) if c.get("start_time") is not None]
        # Captions ride along in the same dump — no extra yt-dlp spawn. `tracks` is the short
        # real list (Off + manual + one ASR/lang); `translations` is the big auto-translate set.
        tracks, translations = _caption_tracks(data)
        _tlog("resolve TOTAL %.2fs" % (time.time() - _t0))
        return {"ok": True, "info": {
            "title": data.get("title") or "",
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
            "audio_url": audio_url,
            "qualities": qualities,
            "audio_tracks": audio_tracks,
            "tracks": tracks,
            "translations": translations,
            "http_ua": http_ua,
            "muxed_itag": muxed.get("format_id", "") if muxed else "",
            "muxed_proto": muxed.get("protocol", "") if muxed else "",
            "video_itag": video.get("format_id", "") if video else "",
            "audio_itag": audio.get("format_id", "") if audio else "",
            # Container per branch, for the player's seek strategy: matroska (webm) branches
            # push-seek over the range proxy as-is, but qtdemux (mp4/m4a) refuses push-mode
            # ACCURATE seeks — the C++ player keys a per-branch downloadbuffer off these.
            "video_ext": (video.get("ext") or "") if video else "",
            "audio_ext": (audio.get("ext") or "") if audio else "",
        }}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


def _video_info_innertube(video_id):
    """video_info() from innertube (player_info + watch_next concurrently), or None to use yt-dlp —
    which still returns metadata for videos the player won't serve here (age/geo walls)."""
    it = _it()
    vid = video_id or ""
    if "://" in vid:
        pu = parse_youtube_url(vid) or {}
        vid = pu.get("id") or "" if pu.get("kind") == "video" else ""
    if it is None or not re.fullmatch(r"[\w-]{11}", vid):
        return None
    box = {}
    th = threading.Thread(target=lambda: box.update(nx=_watch_next(vid)), daemon=True)
    th.start()
    _t = time.time()
    try:
        d = it.player_info(vid)
    except Exception as ex:
        _tlog("video_info innertube failed %.2fs → yt-dlp: %s" % (time.time() - _t, str(ex)[:120]))
        return None
    th.join(8)
    nx = box.get("nx") or {}
    _tlog("video_info innertube %.2fs" % (time.time() - _t))
    return {"ok": True, "info": {
        "title": d.get("title") or "",
        "is_live": bool(d.get("is_live")),
        "uploader": d.get("uploader") or "",
        "channel_id": d.get("channel_id") or "",
        "channel_url": d.get("channel_url") or "",
        "description": d.get("description") or "",
        "duration": d.get("duration") or 0,
        "chapters": [{"start": c.get("start_time") or 0, "title": c.get("title") or ""}
                     for c in (d.get("chapters") or []) if c.get("start_time") is not None],
        "thumbnail": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid,
        "view_count": d.get("view_count") or 0,
        "like_count": nx.get("likes") or 0,
        "upload_date": nx.get("upload_date") or "",
    }}


@_timed_fn("q.video_info")
def video_info(video_id):
    """Lightweight metadata for the info-only view (title, channel, description, chapters, stats)
    — no playback. Deliberately skips the PO-token sidecar and resolve()'s HD-pair client retries,
    and still returns metadata when the video isn't playable here (geo-blocked / bot-walled), which
    is exactly when you might still want to read its description and comments.

    PRIMARY client is `android`: it returns full metadata through YouTube's mobile API WITHOUT the
    web player's JS (the signature / n-param challenge) — that JS download + interpretation is the
    bulk of the extraction cost, and it's disproportionately slow on-device, so android is markedly
    faster (measured ~1.5s vs ~1.6–2.9s on the dev host; a bigger gap on ARM). Falls back to the
    default client set if android ever fails (age/region edge cases), so this is never SLOWER to
    succeed than the plain extraction, only faster in the common case. (player_skip=js was tried and
    REJECTED — it makes the web response invalid → "Video unavailable"; the JS is load-bearing.)

    InnerTube answers first (_video_info_innertube: one player POST + watch-next in parallel,
    ~0.5s); everything above is the fallback for what it can't answer."""
    fast = _video_info_innertube(video_id)
    if fast is not None:
        return fast
    path = _ytdlp_path()
    if not path:
        return {"ok": False, "error": "yt-dlp not found"}
    url = video_id
    if "://" not in url:
        url = "https://www.youtube.com/watch?v=" + video_id

    def _dump(extra):
        # --ignore-no-formats-error: this is metadata only, and without it an age-gated video or
        # an upcoming premiere (exactly what the info-only view is for) fails on its formats.
        with _cookies_args() as cargs:
            return _ytdlp_run(
                path, [*_COMMON_ARGS, *cargs, *extra, "--skip-download",
                       "--ignore-no-formats-error", "--dump-single-json", "--", url], 90)

    try:
        proc = _dump(["--extractor-args", "youtube:player_client=android"])
        if proc.returncode != 0:              # android failed → retry with the default client set
            proc = _dump([])
        if proc.returncode != 0:
            return {"ok": False, "error": _ytdlp_err(proc.stderr, "info failed")}
        data = json.loads(proc.stdout)
        if not data.get("channel_id") and data.get("title") == "youtube video #" + video_id:
            # A nonexistent id comes back as yt-dlp's placeholder, not as an error.
            return {"ok": False, "error": "Video unavailable"}
        chapters = [{"start": c.get("start_time") or 0, "title": c.get("title") or ""}
                    for c in (data.get("chapters") or []) if c.get("start_time") is not None]
        return {"ok": True, "info": {
            "title": data.get("title") or "",
            "is_live": bool(data.get("is_live")) or (data.get("live_status") == "is_live"),
            "uploader": data.get("uploader") or data.get("channel") or "",
            "channel_id": data.get("channel_id") or data.get("uploader_id") or "",
            "channel_url": data.get("channel_url") or data.get("uploader_url") or "",
            "description": data.get("description") or "",
            "duration": data.get("duration") or 0,
            "chapters": chapters,
            "thumbnail": data.get("thumbnail") or "",
            "view_count": data.get("view_count") or 0,
            "like_count": data.get("like_count") or 0,
            "upload_date": data.get("upload_date") or "",   # YYYYMMDD
        }}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "info timed out"}
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
    falls back to the LOWEST rung offered — the nearest to the requested ceiling — so playback
    still happens without overshooting the user's quality/decode budget more than it must."""
    cands = _video_candidates(formats)
    if not cands:
        return None
    for f in cands:
        if not cap or (f.get("height") or 0) <= cap:
            return f
    # Cap below everything offered → nearest rung above it. min() is stable (first minimal
    # element wins), and within the lowest height group the sort already put the preferred
    # codec / lower fps / direct variant first — so that's the one min() lands on.
    return min(cands, key=lambda f: f.get("height") or 0)


def _pick_audio(formats, prefer_lang=""):
    """Best audio track with a URL — the top of the property-based audio ladder (see
    _audio_candidates: highest bitrate, opus preferred at a tie, original/default language).

    With a preferred language (the persisted audio_lang) picks the best rung of THAT language when
    the video dubs it — exact BCP-47 match first, then base code (a remembered 'en' matches
    'en-US') — else falls back to the top of the ladder (original/default source audio)."""
    cands = _audio_candidates(formats)
    if not cands:
        return None
    want = (prefer_lang or "").strip().lower()
    if want:
        for f in cands:
            if (f.get("language") or "").strip().lower() == want:
                return f
        base = want.split("-")[0]
        for f in cands:
            if (f.get("language") or "").strip().lower().split("-")[0] == base:
                return f
    return cands[0]


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
            # The entry's alternate link is /shorts/<id> for a Short, /watch?v=<id> for a
            # normal video -- a free, exact Shorts classifier straight from the feed.
            "is_short": 1 if "/shorts/" in grab(r'<link rel="alternate" href="([^"]+)"') else 0,
        })
    return out


# Search rows are stored in one QML ListModel, so both kinds return the SAME keys — a
# ListModel fixes its roles from the first row, and a missing key would blank that role.
def _video_entry(e):
    vid = e.get("id", "")
    return {
        "type": "video",
        "id": vid,
        "title": e.get("title") or "(untitled)",
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


def _atomic_write_json(path, obj):
    """Write obj as JSON to `path` atomically: serialise to a private (0600) temp file in the same
    directory, then os.replace() it over the target — atomic on POSIX, so a crash / battery-pull /
    ENOSPC mid-write can never truncate the live store (a truncated store loads as {} and the next
    save would then persist the wipe). Mirrors ytm.py's _save_cookies. Raises on failure, leaving
    the existing file untouched, so callers' current try/except still reports it."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


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
                      # Per-category SponsorBlock action: "skip" (auto-skip), "manual" (show a
                      # tap-to-skip button), or absent/"off" (ignore). Historically only these three
                      # were auto-skipped; every other fetched category stays off until the user
                      # opts in on the SponsorBlock page. Applied client-side (see VideoPage), so it
                      # is NOT a resolve-output key.
                      "sponsorblock_actions": {"sponsor": "skip", "selfpromo": "skip",
                                               "interaction": "skip"},
                      # Which signed-in Google LOGIN yt-dlp acts as for account-scoped imports
                      # (subscriptions / playlists). Applied as ?authuser=N on those URLs; 0 = the
                      # first/default account. yt-dlp derives session index + channel from the page
                      # it gets back, so this only switches between separate Google LOGINS — not
                      # brand-account channels within one login (yt-dlp exposes no hook for those).
                      # Hide already-watched videos from the subscription feed + channel lists.
                      "hide_watched": False,
                      # Max watch-history entries kept; the oldest are dropped past this. User-set.
                      "history_limit": 500,
                      # Allow fullscreen while staying in portrait (the video fills the screen
                      # without rotating to landscape).
                      "portrait_fullscreen": False,
                      # Playback speed remembered across videos (applied to each new one).
                      "playback_rate": 1.0,
                      # Keep audio playing (video decoder frozen) when you leave the video page to
                      # browse elsewhere in the app — the cover + lockscreen controls drive it.
                      "background_audio": True,
                      # Preferred caption language code, remembered across videos ("" = off).
                      # On load the player auto-selects a matching track/translation if one exists.
                      "caption_lang": "",
                      # Preferred audio (dub) language code, remembered across videos ("" = the
                      # video's original/default source audio). resolve() starts a matching dub when
                      # the video offers one; the player's Audio picker also switches live.
                      "audio_lang": "",
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
                      # download_dir: where completed downloads are written. "" = the app's own
                      # downloads folder (default); a picked folder (e.g. ~/Videos, an SD card)
                      # overrides it, validated writable before use (see _downloads_dir).
                      "download_dir": "",
                      # PO-token provider (bgutil): opt-in, user-installed. pot_needs_ffi
                      # stays False unless a build genuinely needs node-canvas's native addon
                      # (jsdom degrades gracefully without it).
                      "pot_provider": False, "pot_needs_ffi": False}
# Fast resolve (in-process yt-dlp) deliberately has NO settings key: it is not a preference
# but plumbing. It engages automatically wherever the device python can run it and the
# importable copy exists (installed with the binary, refreshed by Update, backfilled at
# launch by _autofetch_zipapp); the binary stays the fallback for every failure. A stale
# "fast_resolve" key from older builds may linger in settings.json — it is simply ignored.
# Same for "keep_display_on": holding the display awake while a video plays is not a setting
# either (qml/pages/KeepAwake.qml always does it, and lets go when playback stops or stalls).

# Widened client net, tried in ONE extra yt-dlp pass when the primary (token-free client, or
# yt-dlp's auto pick when no provider is set up) hard-fails on a bot-wall or comes back
# SABR-thin (no fetchable HD pair). yt-dlp queries them all and merges formats; the
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
        _atomic_write_json(path, s)
        os.chmod(path, 0o600)     # owner-only (privacy)
    except Exception:
        pass
    if key in _RESOLVE_OUTPUT_KEYS:      # D10: an output-affecting change drops cached resolves
        invalidate_resolve_cache()
    return get_settings()


def set_audio_track(lang, is_original):
    """The player's Audio picker chose a track: remember it for the next video. A dub is stored by
    its language. The video's ORIGINAL track stores "" (original audio), never its language:
    remembering "pl" after "Polish (original)" made the next English video play its Polish dub."""
    return set_setting("audio_lang", "" if is_original else (lang or "").strip())


def _token_free_client():
    """The TOKEN-FREE, JS-less client the yt-dlp primary dump asks for: the installed yt-dlp's own
    JS-less default (visionos as of 2026.08), as adopted by innertube.configure_from_ytdlp — the
    same client the InnerTube fast path uses, so both paths mint the same itag shapes.
    (This was a hardcoded `tv_embedded` until 2026-10: yt-dlp removed that client in 2026.01.31 and
    silently skips unknown names, so the dump quietly ran yt-dlp's default set while the probe
    gating and every log line still said tv_embedded.)"""
    it = _it()
    try:
        return (it.player_client()[0] if it is not None else "") or "visionos"
    except Exception:
        return "visionos"


def _default_client():
    """Which YouTube client resolve()'s yt-dlp path uses by default.

    A user-set player_client always wins. Otherwise, when the PO-token provider is set up we default
    to the TOKEN-FREE JS-less client (_token_free_client): the full range-fetchable HD ladder with
    NO Proof-of-Origin token, so resolve skips the ~4-5s on-device BotGuard mint entirely. Token-free
    clients are the ones YouTube gates unpredictably, so resolve PROBES the picked URL once and, only
    on a real 403 (gated), falls back to the reliable token path (`mweb` + a minted token). The
    provider is thus a SAFETY NET for the rare gated video, not a per-resolve tax. (History: default was
    web_embedded, then mweb-as-blanket-default on 2026-09-03 which put the mint on EVERY resolve — the
    "slow as of late" reports; token-free-first removes it from the common path.) With no provider set
    up we leave yt-dlp on its own auto pick. resolve() also widens to _RETRY_CLIENTS if SABR-thin."""
    return _user_client() or (_token_free_client() if _pot_active() else "")


def _user_client():
    """The player_client chosen in Settings, or "" when it's left on automatic."""
    c = (get_settings().get("player_client") or "").strip()
    return c if c.lower() != "auto" else ""


def _yt_extractor_args(client_override=None, want_pot=False, cookies=False):
    """`--extractor-args` for yt-dlp built from settings (or []).

    player_client picks a YouTube client. client_override lets resolve() widen the client set
    on a retry without touching the saved preference. want_pot forces a PO-token mint (see below).

    `cookies`: the run carries login cookies. yt-dlp then drops every client that doesn't support
    cookies, the JS-less token-free default (visionos) among them, and with none left it fails
    ("Skipping client ... since it does not support cookies"). So a signed-in run gets no automatic
    client and yt-dlp uses its own signed-in default set; a user-chosen client or an override
    still goes through. (Unnoticed while the default was tv_embedded: yt-dlp skipped that unknown
    name before its signed-in fallback, so signed-in downloads worked by accident.)
    """
    parts = []
    if client_override is not None:
        client = client_override
    elif cookies and not _user_client():
        client = ""
    else:
        client = _default_client()
    if client and client.lower() != "auto":
        parts.append("player_client=" + client)
    # fetch_pot=always forces yt-dlp to actually mint a PO token even for clients it marks GVS-token
    # OPTIONAL. Our default (mweb) marks it required=True and would fetch anyway, but this is kept as a
    # belt-and-braces safety net: a client with NO GVS-token policy (e.g. web_embedded — the former
    # default) defaults to required=False, and under fetch_pot=auto yt-dlp EARLY-RETURNS without ever
    # contacting the provider → no token → the URLs 403 under YouTube's "bind GVS PO Token to video id"
    # experiment (yt-dlp PR #14471). fetch_pot=always defeats that gate for any such client (incl. the
    # _RETRY_CLIENTS widen). Only on the paths that build/stream formats (resolve, re-resolve, download)
    # and only when the provider is active — NOT the --flat-playlist metadata passes, where a mint is
    # pure wasted BotGuard latency.
    if want_pot and _pot_active():
        parts.append("fetch_pot=always")
    return ["--extractor-args", "youtube:" + ";".join(parts)] if parts else []


def _hide_shorts():
    return bool(get_settings().get("hide_shorts", True))


_SHORT_MAX_SECS = 60   # a known duration in (0, this] reads as a Short for the feed/list filters


def _is_short(e):
    """A YouTube Short: its watch URL says so, or (fallback) it's <=60s. The duration
    heuristic can catch a genuinely short normal video, which is why it's user-toggleable."""
    if "/shorts/" in (e.get("url") or ""):
        return True
    dur = e.get("duration")
    return dur is not None and 0 < dur <= _SHORT_MAX_SECS


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


def _feed_shorts_filter(items):
    """Drop Shorts from a feed item list when hide_shorts is on. Shorts are identified two ways,
    both definitive: the RSS entry's /shorts/ link (set inline by _feed_from_rss, so they drop
    with no flash) and channel /shorts-tab membership (discovered + cached by feed_durations, the
    fallback for rows that never came through RSS). A duration heuristic can't tell a 3-minute
    Short from a 3-minute normal video, so it is never used."""
    if not _hide_shorts():
        return items
    shorts = _feed_durations_cache.get("shorts") or _load_saved_shorts()
    return [it for it in items if not it.get("is_short") and it.get("id") not in shorts]


def _feed_with_durations(items):
    """Fill each item's duration from the (warm) persistent duration cache, so a length we've ALREADY
    measured shows WITH the feed instead of popping in a beat later on the separate feed_durations
    round-trip. Only genuinely-new (uncached) videos still fill in afterwards."""
    dmap = _feed_durations_cache.get("map") or _load_saved_durations()
    _feed_durations_cache["map"] = dmap          # warm the in-memory map for feed_durations to reuse
    if not dmap:
        return items
    for it in items:
        d = dmap.get(it.get("id"))
        if d and it.get("duration") != d:
            it["duration"] = d
    return items


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


_RESUME_TTL = 30 * 86400   # resume points idle (unplayed) longer than this are dropped — you won't
                           # resume a video you half-watched a month ago


def _pos_secs(entry):
    """Seconds from a positions entry, tolerating the old plain-int on-disk format."""
    v = entry.get("s") if isinstance(entry, dict) else entry
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _pos_fresh(entry, now):
    """Keep a positions entry? Old int entries (no timestamp) are grandfathered as touched-now;
    dict entries survive while touched within _RESUME_TTL. Returns the (migrated) entry, or None
    to drop it."""
    if not isinstance(entry, dict):
        return {"s": _pos_secs(entry), "t": now}          # old int format → stamp it now
    if now - float(entry.get("t") or 0) > _RESUME_TTL:
        return None
    return entry


def get_position(video_id):
    """Saved watch position (seconds) for a video; 0 if none, or if the resume point has gone
    stale (idle past _RESUME_TTL)."""
    ent = _load_positions().get(video_id)
    if isinstance(ent, dict) and time.time() - float(ent.get("t") or 0) > _RESUME_TTL:
        return 0
    return _pos_secs(ent)


def set_position(video_id, seconds):
    """Remember (or, with seconds<=0, forget) where a video was left off, with a last-touched
    stamp. Each write also migrates old int entries and sweeps any idle past _RESUME_TTL, so the
    file self-prunes. Insertion-ordered, count-capped so it can't grow without bound."""
    if not video_id:
        return
    now = time.time()
    d = _load_positions()
    d.pop(video_id, None)                 # reinsert at the end = most-recent
    d = {k: f for k, f in ((k, _pos_fresh(v, now)) for k, v in d.items()) if f is not None}
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = 0
    if seconds > 0:
        d[video_id] = {"s": seconds, "t": now}
    if len(d) > 300:
        d = dict(list(d.items())[-300:])
    try:
        _atomic_write_json(_positions_path(), d)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Watch history — richer per-video progress (a play fraction + a sticky 'watched' flag at
# >=80%), used to draw the thumbnail progress bar + the WATCHED tag. Kept SEPARATE from the
# resume store (positions.json), which is cleared once a video finishes; this remembers that
# the video WAS watched even after its resume point is gone.
# --------------------------------------------------------------------------- #
_WATCHED_FRACTION = 0.8


def _history_limit():
    """Max watch-history entries to keep (user setting; the store drops the oldest past this)."""
    try:
        n = int(get_settings().get("history_limit", 500))
    except (TypeError, ValueError):
        n = 500
    return max(50, min(5000, n))          # clamp to a sane range


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


def watch_history(start=1, n=40):
    """One page of recently-watched videos, newest first, for the History page: [{id,title,channel,
    thumbnail,pos,dur,frac,watched,ts}]. `start` is the 1-based index of the first row wanted, so the
    UI pages the rest in as it scrolls (a large history is slow to append to a ListModel all at once).
    Thumbnail is derived from the id. Ordered by the store's insertion order (record_watch reinserts
    each play at the end) rather than the 1s-granular ts, so replays within the same second still
    sort most-recent-first."""
    d = _load_watch_history()
    rows = list(d.items())
    rows.reverse()                 # insertion order is oldest→newest, so reverse = newest first
    try:
        start = max(1, int(start))
        n = max(1, int(n))
    except (TypeError, ValueError):
        start, n = 1, 40
    out = []
    for vid, e in rows[start - 1:start - 1 + n]:
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
    lim = _history_limit()
    if len(d) > lim:
        d = dict(list(d.items())[-lim:])
    try:
        _atomic_write_json(_watch_history_path(), d)
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
    lim = _history_limit()
    if len(d) > lim:
        d = dict(list(d.items())[-lim:])
    try:
        _atomic_write_json(_watch_history_path(), d)
    except Exception:
        return {"ok": False}
    return {"ok": True, "watched": 1 if watched else 0}


# SponsorBlock categories we ASK the API for. What actually happens to each segment (auto-skip,
# show a manual skip button, or ignore) is a per-category CLIENT setting (`sponsorblock_actions`,
# applied by the player) — so we fetch the whole skippable set here and let the UI decide. Widening
# this list only changes what's *available* to configure; unconfigured categories default to off.
_SB_CATEGORIES = ('["sponsor","selfpromo","interaction","intro","outro","preview",'
                  '"filler","music_offtopic"]')


_sb_cache = {}                 # video id -> (ts, segments); VideoPage asks on every (re)open
_SB_TTL = 600
_SB_MAX = 64


def sponsor_segments(video_id):
    """SponsorBlock skip segments for a video: [{start, end, category}] in seconds. Uses the
    public sponsor.ajay.app API; 404 just means nobody's submitted any. Answers are cached
    briefly (failures aren't), so re-opening a video doesn't re-ask."""
    if not video_id:
        return {"ok": True, "segments": []}
    hit = _sb_cache.get(video_id)
    if hit and time.time() - hit[0] < _SB_TTL:
        return {"ok": True, "segments": list(hit[1])}
    res = _sponsor_segments_fetch(video_id)
    if res.get("ok"):
        _sb_cache[video_id] = (time.time(), list(res["segments"]))
        if len(_sb_cache) > _SB_MAX:
            for k in sorted(_sb_cache, key=lambda k: _sb_cache[k][0])[:len(_sb_cache) - _SB_MAX]:
                _sb_cache.pop(k, None)
    return res


def _sponsor_segments_fetch(video_id):
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


def _parse_json3(data):
    """json3 caption events -> display cues [{start, dur, text}] (seconds).

    Tames the auto-caption 'rolling' footgun: ASR json3 emits overlapping, partially
    repeated events (the word-by-word scroll). We drop empty/whitespace events, merge a
    line that's re-emitted back-to-back (extend it rather than stack a duplicate), then
    clamp each cue's end to the next cue's start so nothing overlaps on screen. Residual:
    a genuine append-style scroll still shows the line growing word-by-word — acceptable."""
    if not isinstance(data, dict):        # a valid-JSON but non-object body (null/[]/42) -> no cues
        return []
    rough = []
    for ev in (data.get("events") or []):
        text = "".join(s.get("utf8", "") for s in (ev.get("segs") or []))
        text = " ".join(text.split())   # collapse newlines / runs of whitespace
        if not text:
            continue
        start = (ev.get("tStartMs") or 0) / 1000.0
        dur = (ev.get("dDurationMs") or 0) / 1000.0
        rough.append([start, start + dur if dur > 0 else start + 4.0, text])
    rough.sort(key=lambda c: c[0])
    merged = []
    for start, end, text in rough:
        if merged and merged[-1][2] == text and start <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], end)   # same line re-emitted -> extend it
        else:
            merged.append([start, end, text])
    cues = []
    for i, (start, end, text) in enumerate(merged):
        if i + 1 < len(merged) and end > merged[i + 1][0]:
            end = merged[i + 1][0]                     # no two cues on screen at once
        if end - start < 0.05:
            continue
        cues.append({"start": round(start, 3), "dur": round(end - start, 3), "text": text})
    return cues


_caption_cache = {}   # url -> parsed cues. Session-lived; caption URLs are stable per resolve, so
                      # re-selecting a track (or toggling off->on) is instant and skips the network
                      # — which also avoids YouTube's timedtext rate limit under quick switching.


def caption_cues(url):
    """Fetch + parse one json3 caption track (URL from resolve()'s tracks/translations)
    into {"ok", "cues":[{start,dur,text}]}. Public timedtext, no cookies/PO-token needed."""
    if not url:
        return {"ok": False, "cues": []}
    if url in _caption_cache:
        return {"ok": True, "cues": _caption_cache[url]}
    _force_ipv4()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return {"ok": False, "cues": []}   # not cached — a transient 429/timeout stays retryable
    cues = _parse_json3(data)
    _caption_cache[url] = cues
    if len(_caption_cache) > 12:           # cap; evict oldest insertions
        for k in list(_caption_cache)[:len(_caption_cache) - 12]:
            del _caption_cache[k]
    return {"ok": True, "cues": cues}


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
        _atomic_write_json(_subs_path(), subs)
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
    # (No feed-cache poke needed: a NEW channel is uncached → due → pulled in on the next refresh;
    # an UNSUBSCRIBED one is excluded by _merged_feed_items(ids) and pruned on the next fetch.)
    _feed_durations_cache["ts"] = 0.0   # invalidate the (separate) duration map
    return {"ok": True, "subscribed": subscribed, "subscriptions": subs}


def _account_slot():
    """The chosen Google-login index for the yt-dlp FALLBACK path, taken from the account selector
    (ytm.selected_account). 0 = default account. The primary InnerTube path uses the full identity
    (login + channel) directly; this only covers the case where InnerTube is unavailable."""
    try:
        import ytm
        return max(0, int(ytm.selected_account().get("authuser", 0) or 0))
    except Exception:
        return 0


def _with_authuser(url):
    """Append ?authuser=N (or &authuser=N) for the selected login on the yt-dlp fallback path. No-op
    for slot 0. Switches between separate Google LOGINS only — brand-account channels can't be
    selected this way (yt-dlp has no override); the InnerTube path handles those."""
    n = _account_slot()
    if n <= 0:
        return url
    return url + ("&" if "?" in url else "?") + "authuser=" + str(n)


def _subscription_channels():
    """This account's subscribed channels as [{id,name,url,thumbnail}]. Prefers the InnerTube path
    (respects the selected login/channel, incl. brand accounts); falls back to yt-dlp's feed/channels
    (default account only) when InnerTube is unavailable or empty. Raises on a hard fetch failure."""
    import ytm
    # Preferred: InnerTube, per the selected identity.
    if hasattr(ytm, "subscriptions_innertube"):
        try:
            r = ytm.subscriptions_innertube()
            if r.get("ok") and r.get("channels"):
                _tlog("subscriptions via InnerTube: %d" % len(r["channels"]))
                return r["channels"]
            _tlog("subscriptions InnerTube empty/nok (%s) → yt-dlp" % (r.get("error") or "empty"))
        except Exception as ex:
            _tlog("subscriptions InnerTube error → yt-dlp: %s" % str(ex)[:120])
    # Fallback: yt-dlp feed/channels.
    path = _ytdlp_path()
    if not path:
        raise RuntimeError("yt-dlp not found")
    url = _with_authuser("https://www.youtube.com/feed/channels")
    with _cookies_args() as cargs:
        proc = _ytdlp_run(
            path, [*_COMMON_ARGS, *cargs, "--flat-playlist", "--dump-single-json", "--", url], 120)
    if proc.returncode != 0:
        raise RuntimeError(_ytdlp_err(proc.stderr, "subscription fetch failed"))
    data = json.loads(proc.stdout)
    out = []
    for e in (data.get("entries") or []):
        if not e:
            continue
        cid = e.get("id") or e.get("channel_id") or ""
        curl = e.get("url") or e.get("channel_url") or ""
        if not (cid or "").startswith("UC"):
            m = re.search(r"/channel/(UC[\w-]+)", curl or "")   # some entries carry only a URL
            cid = m.group(1) if m else ""
        if not cid:
            continue
        thumb = ""
        thumbs = e.get("thumbnails")
        if isinstance(thumbs, list) and thumbs and isinstance(thumbs[-1], dict):
            thumb = thumbs[-1].get("url", "") or ""
        out.append({"id": cid, "name": e.get("title") or e.get("channel") or e.get("uploader") or cid,
                    "url": curl or ("https://www.youtube.com/channel/" + cid), "thumbnail": thumb})
    return out


def import_youtube_subscriptions():
    """Import the signed-in account's YouTube subscriptions (InnerTube per selected identity, yt-dlp
    fallback). New channels are added to the local store, deduped by id; import-summary shaped."""
    try:
        import ytm
        signed_in = bool(ytm.netscape_cookies())
    except Exception:
        signed_in = False
    if not signed_in:
        return {"ok": False, "error": "Not signed in — import your YouTube login from the browser "
                                      "first (More → Providers → YouTube account)."}
    try:
        channels = _subscription_channels()
    except Exception as ex:
        return {"ok": False, "error": str(ex)}
    subs = list_subscriptions()
    have = set(s.get("id") for s in subs if s.get("id"))
    added = 0
    for ch in channels:
        cid = ch.get("id") or ""
        if not cid.startswith("UC") or cid in have:
            continue
        subs.append({"id": cid, "name": ch.get("name") or cid,
                     "url": ch.get("url") or ("https://www.youtube.com/channel/" + cid),
                     "thumbnail": ch.get("thumbnail") or ""})
        have.add(cid)
        added += 1
    if added:
        _save_subscriptions(subs)
        _feed_durations_cache["ts"] = 0.0   # a fresh channel is due on the next feed refresh
    total = len(channels)
    if total:
        summary = ("Imported %d new channel%s (%d already subscribed)."
                   % (added, "" if added == 1 else "s", total - added))
    else:
        summary = "No subscriptions found for this account."
    return {"ok": True, "added": added, "skipped": total - added, "total": total,
            "count": len(subs), "summary": summary}


def _library_playlists():
    """This account's playlists as [{yt_id,title}]. Prefers InnerTube (per selected identity), falls
    back to yt-dlp feed/playlists. Raises on a hard fetch failure."""
    import ytm
    if hasattr(ytm, "playlists_innertube"):
        try:
            r = ytm.playlists_innertube()
            if r.get("ok") and r.get("playlists"):
                _tlog("playlists via InnerTube: %d" % len(r["playlists"]))
                return r["playlists"]
            _tlog("playlists InnerTube empty/nok (%s) → yt-dlp" % (r.get("error") or "empty"))
        except Exception as ex:
            _tlog("playlists InnerTube error → yt-dlp: %s" % str(ex)[:120])
    path = _ytdlp_path()
    if not path:
        raise RuntimeError("yt-dlp not found")
    url = _with_authuser("https://www.youtube.com/feed/playlists")
    with _cookies_args() as cargs:
        proc = _ytdlp_run(
            path, [*_COMMON_ARGS, *cargs, "--flat-playlist", "--dump-single-json", "--", url], 120)
    if proc.returncode != 0:
        raise RuntimeError(_ytdlp_err(proc.stderr, "playlist fetch failed"))
    data = json.loads(proc.stdout)
    list_re = re.compile(r"[?&]list=([0-9A-Za-z_-]+)")
    out = []
    for e in (data.get("entries") or []):
        if not e:
            continue
        yt_id = e.get("id") or ""
        if not yt_id:
            m = list_re.search(e.get("url") or "")
            yt_id = m.group(1) if m else ""
        if yt_id:
            out.append({"yt_id": yt_id, "title": e.get("title") or e.get("channel") or "Playlist"})
    return out


def import_youtube_playlists():
    """Import the signed-in account's YouTube playlists (InnerTube per selected identity, yt-dlp
    fallback). Each becomes a kind="youtube" library entry with EMPTY items — opening or refreshing
    it fetches the videos, like a NewPipe-imported saved playlist. Deduped by list id."""
    try:
        import ytm
        signed_in = bool(ytm.netscape_cookies())
    except Exception:
        signed_in = False
    if not signed_in:
        return {"ok": False, "error": "Not signed in — import your YouTube login from the browser "
                                      "first (More → Providers → YouTube account)."}
    try:
        playlists = _library_playlists()
    except Exception as ex:
        return {"ok": False, "error": str(ex)}
    lst = _load_playlists()
    have_yt = set(p.get("yt_id") for p in lst if p.get("yt_id"))
    added = 0
    for pl in playlists:
        yt_id = (pl.get("yt_id") or "").strip()
        # Skip empties, dupes, and any channel-id row (UC…) that isn't a real playlist — uploads is
        # UU…, liked LL, watch-later WL, user playlists PL…, so only UC is excluded.
        if not yt_id or yt_id.startswith("UC") or yt_id in have_yt:
            continue
        title = (pl.get("title") or "Playlist").strip()[:100] or "Playlist"
        lst.insert(0, {"id": uuid.uuid4().hex[:12], "title": title,
                       "kind": "youtube", "yt_id": yt_id, "items": []})
        have_yt.add(yt_id)
        added += 1
    if added:
        _save_playlists(lst)
    total = len(playlists)
    if total:
        summary = ("Imported %d new playlist%s (%d already saved)."
                   % (added, "" if added == 1 else "s", total - added))
    else:
        summary = "No playlists found for this account."
    return {"ok": True, "added": added, "skipped": total - added, "total": total,
            "count": len(lst), "summary": summary}


def import_youtube_account():
    """One-shot import of BOTH subscriptions and playlists from the signed-in account — the
    'Import from YouTube' action. Combined summary; ok when either half succeeds."""
    subs = import_youtube_subscriptions()
    pls = import_youtube_playlists()
    sa = subs.get("added", 0) if subs.get("ok") else 0
    pa = pls.get("added", 0) if pls.get("ok") else 0
    if not subs.get("ok") and not pls.get("ok"):
        return {"ok": False, "subs": subs, "playlists": pls, "subs_added": 0, "playlists_added": 0,
                "error": subs.get("error") or pls.get("error") or "import failed"}
    # Only name a half that actually added something, so a re-run (both already imported) reads
    # "Nothing new to import." instead of "Imported 0 subscriptions and 0 playlists."
    parts = []
    if sa:
        parts.append("%d subscription%s" % (sa, "" if sa == 1 else "s"))
    if pa:
        parts.append("%d playlist%s" % (pa, "" if pa == 1 else "s"))
    summary = ("Imported " + " and ".join(parts) + ".") if parts else "Nothing new to import."
    return {"ok": True, "subs": subs, "playlists": pls,
            "subs_added": sa, "playlists_added": pa, "summary": summary}


# QML-facing thin wrappers over the optional `ytm` cookie module, so the QML side only ever calls
# one module (youfish) and degrades gracefully if ytm isn't present.
def youtube_login_status():
    try:
        import ytm
        return ytm.login_status()
    except Exception:
        return {"logged_in": False, "count": 0, "imported_at": 0, "source": ""}


def youtube_import_login():
    """Import the signed-in session from the Sailfish Browser cookie jar (see ytm)."""
    try:
        import ytm
        res = ytm.import_browser_login()
    except Exception as ex:
        return {"ok": False, "count": 0, "error": str(ex)}
    invalidate_resolve_cache()    # D10: login changes extraction (age/members/premium)
    return res


def youtube_logout():
    try:
        import ytm
        res = ytm.logout()
    except Exception:
        res = {"logged_in": False}
    invalidate_resolve_cache()    # D10: logout changes extraction
    return res


def youtube_list_accounts():
    """Enumerate the signed-in Google logins + channels for the account picker (see ytm)."""
    try:
        import ytm
        return ytm.list_accounts()
    except Exception as ex:
        return {"ok": False, "accounts": [], "error": str(ex)}


def youtube_select_account(authuser="0", page_id="", datasync_id="", name=""):
    """Switch the active account/channel for account-scoped fetches (see ytm)."""
    try:
        import ytm
        res = ytm.select_account(authuser, page_id, datasync_id, name)
    except Exception as ex:
        return {"ok": False, "error": str(ex)}
    invalidate_resolve_cache()    # a different account can change extraction (members/premium)
    return res


def youtube_selected_account():
    try:
        import ytm
        return ytm.selected_account()
    except Exception:
        return {"authuser": "0", "page_id": "", "name": ""}


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
        _feed_durations_cache["ts"] = 0.0   # new subs are auto-due in the per-channel feed cache

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
        lim = _history_limit()
        if len(merged) > lim:
            merged = dict(list(merged.items())[-lim:])
        if hist_added:
            try:
                _atomic_write_json(_watch_history_path(), merged)
            except Exception:
                pass

    pos_added = 0
    if imported:
        now = time.time()
        positions = _load_positions()
        for ts, vid, entry in imported:
            if vid in positions:
                continue                      # don't overwrite a local resume point
            pp, dd = entry["p"], entry["d"]
            if pp > 10 and not (dd > 0 and pp > dd - 15):
                positions[vid] = {"s": pp, "t": now}   # stamp imported points touched-now (fresh 30d)
                pos_added += 1
        if pos_added:
            if len(positions) > 300:
                positions = dict(list(positions.items())[-300:])
            try:
                _atomic_write_json(_positions_path(), positions)
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
_AVATAR_CACHE_TTL = 7 * 86400   # avatars rarely change; a week (persisted to disk below) avoids
                                 # re-running yt-dlp per channel-view, across restarts
_AVATAR_CACHE_MAX = 128

_avatar_state = {"loaded": False}


def _avatar_cache_path():
    return os.path.join(_data_dir(), "avatar_cache.json")


def _load_avatar_cache():
    """The persisted avatar cache, dropping entries already past the TTL — so avatars survive an
    app restart instead of costing a fresh yt-dlp spawn per channel each session."""
    try:
        with open(_avatar_cache_path()) as f:
            d = json.load(f)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    now = time.time()
    out = {}
    for k, ent in d.items():
        if (isinstance(ent, dict) and isinstance(ent.get("res"), dict)
                and now - float(ent.get("ts") or 0) < _AVATAR_CACHE_TTL):
            out[k] = {"ts": float(ent["ts"]), "res": ent["res"]}
    return out


def _save_avatar_cache():
    try:
        _atomic_write_json(_avatar_cache_path(), _avatar_cache)
    except Exception:
        pass


def _avatar_hydrate():
    """Load the on-disk avatar cache once. Call from inside _avatar_cache_lock."""
    if not _avatar_state["loaded"]:
        _avatar_cache.update(_load_avatar_cache())
        _avatar_state["loaded"] = True


def _avatar_store(keys, res):
    """Cache an avatar result under every given key (the channel ref as asked + its UC id), and
    persist only when something actually changed — channel_videos calls this on every page 1."""
    now = time.time()
    with _avatar_cache_lock:
        _avatar_hydrate()
        changed = False
        for k in keys:
            if not k:
                continue
            ent = _avatar_cache.get(k)
            if not ent or ent["res"] != res or now - ent["ts"] > _AVATAR_CACHE_TTL / 2:
                _avatar_cache[k] = {"ts": now, "res": res}
                changed = True
        if not changed:
            return
        if len(_avatar_cache) > _AVATAR_CACHE_MAX:  # evict oldest beyond the cap
            for k, _ in sorted(_avatar_cache.items(),
                               key=lambda kv: kv[1]["ts"])[:len(_avatar_cache) - _AVATAR_CACHE_MAX]:
                _avatar_cache.pop(k, None)
        _save_avatar_cache()            # persist so avatars survive an app restart


# --- InnerTube channel pages (fast path for channel_videos / channel_avatar) --------------- #
# One `browse` POST returns a channel's newest ~30 uploads WITH lengths, views and ages plus the
# header (avatar, subscribers, video count); continuation tokens page further. Pages are cached per
# channel so scroll-to-load-more continues from the stored token instead of re-listing from the top
# (each yt-dlp page used to re-walk every earlier page), and so the avatar fetch on video open
# leaves page 1 warm for a tap on the channel name.
_chan_pages = {}               # UC id -> {"ts", "items": [innertube items], "cont": token|None, "channel"}
_chan_pages_lock = threading.Lock()
_CHAN_PAGES_TTL = 600          # later pages ride the cached continuation this long
_CHAN_FIRST_FRESH = 120        # a page 1 younger than this is reused as-is
_CHAN_PAGES_MAX = 16


def _it_channel(channel, need=0, fresh_first=False):
    """(cid, entry) for a channel ref from the InnerTube page cache — fetching page 1 when missing
    or stale, then continuation pages until it holds `need` items (or the channel runs out).
    Raises on any failure; callers fall back to yt-dlp."""
    it = _it()
    if it is None:
        raise RuntimeError("innertube unavailable")
    _force_ipv4()
    cid = it.resolve_channel(channel)
    now = time.time()
    with _chan_pages_lock:
        ent = _chan_pages.get(cid)
        ent = dict(ent) if ent else None
    if ent is None or now - ent["ts"] > (_CHAN_FIRST_FRESH if fresh_first else _CHAN_PAGES_TTL):
        tab = it.channel_tab(cid, "videos")
        if not tab["items"]:
            # A channel without uploads has no Videos tab (channel_tab raises for that), so an
            # empty one is a layout change: let yt-dlp read it rather than show nothing.
            raise it.InnertubeError("videos tab parsed empty")
        ent = {"ts": now, "items": list(tab["items"]), "cont": tab["continuation"],
               "channel": tab["channel"]}
    while len(ent["items"]) < need and ent["cont"]:
        nxt = it.continuation(ent["cont"])
        ent["items"] = ent["items"] + nxt["items"]
        ent["cont"] = nxt["continuation"]
        if not nxt["items"]:
            break
    with _chan_pages_lock:
        _chan_pages[cid] = ent
        if len(_chan_pages) > _CHAN_PAGES_MAX:
            for k, _ in sorted(_chan_pages.items(),
                               key=lambda kv: kv[1]["ts"])[:len(_chan_pages) - _CHAN_PAGES_MAX]:
                _chan_pages.pop(k, None)
    # Every listing teaches lengths for free — the feed then shows them with no classification pass.
    _learn_durations({e["id"]: e["duration"] for e in ent["items"] if e.get("duration")})
    return cid, ent


def _it_avatar(ch, size=176):
    return _pick_avatar({"thumbnails": ch.get("avatars") or []}, size)


def _it_video_item(e, uploader, now):
    """An innertube listing item → the channel/feed row shape the QML lists use."""
    ago = e.get("ago")
    return {
        "id": e["id"],
        "title": e.get("title") or "(untitled)",
        "uploader": uploader,
        "duration": e.get("duration") or 0,
        "thumbnail": _video_thumb(e["id"]),
        "views": e.get("views") or 0,
        "posted": _rel_from_ts(now - ago) if ago is not None else "",
        "live": 1 if e.get("live") else 0,
    }


def channel_avatar(channel):
    """Just the channel's avatar URL + id — cheap enough to fetch on video open.

    InnerTube first (one browse POST, which also warms the channel's page 1); the yt-dlp fallback
    fetches one flat entry so yt-dlp still hands back the channel metadata (avatar) without listing
    the whole uploads tab. Cached for a week (_AVATAR_CACHE_TTL, persisted to disk) so opening
    several of a channel's videos doesn't re-fetch each time.
    """
    if not channel:
        return {"ok": False}
    with _avatar_cache_lock:
        _avatar_hydrate()               # load the on-disk cache once (survives restarts)
        ent = _avatar_cache.get(channel)
        if ent and time.time() - ent["ts"] < _AVATAR_CACHE_TTL:
            return ent["res"]
    try:
        cid, ent = _it_channel(channel, fresh_first=True)
        thumb = _it_avatar(ent["channel"])
        if thumb:
            res = {"ok": True, "id": cid, "thumbnail": thumb}
            _avatar_store((channel, cid), res)
            return res
    except Exception as ex:
        _tlog("channel_avatar innertube failed → yt-dlp: %s" % str(ex)[:120])
    path = _ytdlp_path()
    if not path:
        return {"ok": False}
    url = channel
    if "://" not in url:
        url = "https://www.youtube.com/channel/%s" % channel
    if not url.rstrip("/").endswith("/videos"):
        url = url.rstrip("/") + "/videos"
    try:
        with _cookies_args() as cargs:
            proc = _ytdlp_run(
                path, [*_COMMON_ARGS, *cargs, "--flat-playlist", "--playlist-items", "1",
                       "--dump-single-json", "--", url], 60)
        if proc.returncode != 0:
            return {"ok": False}
        data = json.loads(proc.stdout)
        res = {"ok": True,
               "id": data.get("channel_id") or data.get("id") or "",
               "thumbnail": _pick_avatar(data, 176)}
        _avatar_store((channel,), res)
        return res
    except Exception:
        return {"ok": False}


def _channel_videos_innertube(channel, start, n):
    """channel_videos() via the InnerTube page cache, or None to fall back to yt-dlp."""
    try:
        start = max(1, int(start))
        n = max(1, int(n))
        cid, ent = _it_channel(channel, need=start - 1 + n, fresh_first=(start <= 1))
    except Exception as ex:
        _it_note("channel", ex)
        _tlog("channel_videos innertube failed → yt-dlp: %s" % str(ex)[:120])
        return None
    ch = ent["channel"]
    raw = ent["items"][start - 1:start - 1 + n]
    has_more = len(ent["items"]) > start - 1 + n or bool(ent["cont"])
    entries = [e for e in raw if not e.get("members")]
    if _hide_shorts():                       # the Videos tab has no Shorts; mirror the old ≤60s rule
        entries = [e for e in entries if not (0 < (e.get("duration") or 0) <= _SHORT_MAX_SECS)]
    if _hide_watched():
        watched = _watched_ids()
        entries = [e for e in entries if e["id"] not in watched]
    now = time.time()
    name = ch.get("name") or ""
    thumb = _it_avatar(ch)
    if thumb:
        _avatar_store((channel, cid), {"ok": True, "id": cid, "thumbnail": thumb})
    return {"ok": True, "items": [_it_video_item(e, name, now) for e in entries],
            "has_more": has_more, "channel": {
                "id": cid,
                "name": name,
                "url": ch.get("url") or "https://www.youtube.com/channel/%s" % cid,
                "thumbnail": thumb,
                "subscribers": ch.get("subscribers") or 0,
                "video_count": ch.get("video_count") or 0,
            }}


def _channel_error(err):
    """A failed channel listing. yt-dlp refuses a channel with no uploads ("…does not have a videos
    tab"); reason "no_videos_tab" tells the page to open the channel's playlists instead, so the UI
    doesn't depend on yt-dlp's wording."""
    out = {"ok": False, "error": err}
    if re.search(r"videos tab", err or "", re.I):
        out["reason"] = "no_videos_tab"
    return out


@_timed_fn("q.channel_videos")
def channel_videos(channel, start=1, n=30):
    """A page of a channel's uploads (a channel_id or any channel URL). `start` is the
    1-based index of the first video wanted, so the UI can page in more as it scrolls.
    InnerTube first (see _it_channel); yt-dlp's flat listing is the fallback."""
    res = _channel_videos_innertube(channel, start, n)
    if res is not None:
        return res
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
        with _cookies_args() as cargs:
            proc = _ytdlp_run(
                path, [*_COMMON_ARGS, *cargs, "--flat-playlist",
                       "--extractor-args", "youtubetab:approximate_date",   # upload dates inline (RSS is gone)
                       "--playlist-items", "%d:%d" % (start, start + n - 1),
                       "--dump-single-json", "--", url], 90)
        if proc.returncode != 0:
            return _channel_error(_ytdlp_err(proc.stderr, "channel fetch failed"))
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
            "title": e.get("title") or "(untitled)",
            "uploader": e.get("uploader") or e.get("channel") or data.get("channel") or "",
            "duration": e.get("duration") or 0,
            # Flat entries often omit thumbnails; derive the reliable one from the id.
            "thumbnail": _video_thumb(e.get("id", "")) or _pick_thumb(e),
            "views": e.get("view_count") or 0,
            "posted": _rel_date(e),
            "live": 1 if (e.get("live_status") == "is_live" or e.get("is_live")) else 0,
        } for e in entries]
        # Dates come inline now (approximate_date extractor arg) — no RSS date-fill needed.
        return {"ok": True, "items": items, "has_more": has_more, "channel": {
            "id": data.get("channel_id") or data.get("id") or "",
            "name": data.get("channel") or data.get("uploader") or data.get("title") or "",
            "url": data.get("channel_url") or data.get("webpage_url") or url,
            "thumbnail": _pick_avatar(data, 176),
            "subscribers": data.get("channel_follower_count") or 0,
            "video_count": data.get("playlist_count") or 0,
        }}
    except Exception as ex:
        return _channel_error(str(ex))


def _rss_get(cid):
    """A channel's RSS feed XML, or raise. A feed refresh fetches dozens of channels from the same
    host, so this goes through innertube's keep-alive pool (one TLS handshake per pooled socket
    instead of one per channel), and through urllib without the module or on a redirect."""
    _force_ipv4()
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=%s" % cid
    it = _it()
    if it is not None and hasattr(it, "request"):
        status, data, _ = it.request("GET", url, None, {"User-Agent": _BROWSER_UA}, 10)
        if status == 200:
            return data.decode("utf-8", "replace")
        if not 300 <= status < 400:
            raise IOError("feed HTTP %d" % status)
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode("utf-8", "replace")


@_timed_fn("q.channel_rss")
def channel_feed_rss(channel):
    """Fast first paint for the channel view: the latest ~15 uploads from the channel's RSS feed
    (spawn-free), in the same item shape channel_videos returns. Works only when `channel` resolves
    to a UC id (RSS keys on channel_id) -- a handle/user URL returns ok:False so ChannelPage falls
    back to channel_videos. Carries no live status and only a minimal header (name/id/url);
    durations are filled from the persistent cache where already known, and channel_videos backfills
    the rest (durations + avatar/subscribers/count + the uploads past ~15) in the background."""
    ref = str(channel or "")
    cid = ref if (ref.startswith("UC") and "/" not in ref) else ""
    if not cid:
        m = re.search(r"/channel/(UC[\w-]+)", ref)
        cid = m.group(1) if m else ""
    if not cid:
        return {"ok": False, "items": []}
    try:
        xml = _rss_get(cid)
    except Exception:
        return {"ok": False, "items": []}
    ents = _parse_feed_entries(xml)
    m = re.search(r"<title>(.*?)</title>", xml, re.S)      # feed-level <title> = the channel name
    chan_name = html.unescape(m.group(1)) if m else ""
    hide_shorts = _hide_shorts()
    items = []
    for e in ents:
        if hide_shorts and e.get("is_short"):
            continue
        vid = e["id"]
        items.append({
            "id": vid,
            "title": e["title"],
            "uploader": e["uploader"] or chan_name,
            "duration": 0,
            "thumbnail": _video_thumb(vid),
            "views": e.get("views") or 0,
            "posted": _rel_from_iso(e.get("published")),
            "live": 0,
        })
    items = _feed_with_durations(items)          # show any length we already know, instantly
    return {"ok": True, "items": items, "has_more": True, "channel": {
        "id": cid,
        "name": chan_name,
        "url": "https://www.youtube.com/channel/%s" % cid,
        "thumbnail": "",
        "subscribers": 0,
        "video_count": 0,
    }}


def _feed_fetch_channel(path, cid, per_channel):
    """One subscribed channel's recent uploads → [(sort_ts, item), ...]. The channel's RSS
    feed (feeds/videos.xml, spawn-free urllib) is the PRIMARY source: fast + exact dates, but no
    duration/live/Shorts flag (backfilled later by feed_durations — the NewPipe/FreeTube
    fast-mode tradeoff). When RSS returns nothing (YouTube breaks it intermittently — mirrors how
    NewPipe/FreeTube cross-fall-back) the InnerTube Videos tab stands in (one POST, lengths inline,
    approximate dates), and yt-dlp's /videos tab is the last resort. Any total failure of all
    three contributes nothing."""
    rows = _feed_from_rss(cid, per_channel)
    if not rows:
        rows = _feed_from_innertube(cid, per_channel)
    return rows if rows else _feed_from_ytdlp(path, cid, per_channel)


@_timed_fn("feed.innertube")
def _feed_from_innertube(cid, per_channel):
    """FALLBACK #1: a channel's Videos tab via one InnerTube browse POST. Lengths + live flag inline;
    dates are YouTube's relative ones ("3 days ago"), so cross-channel ordering is approximate —
    still far better than an empty channel. Same row shape as the RSS / yt-dlp paths."""
    it = _it()
    if it is None or not str(cid).startswith("UC"):
        return []
    _force_ipv4()
    try:
        tab = it.channel_tab(cid, "videos")
    except Exception:
        return []
    now = time.time()
    if tab["items"] and all(e.get("ago") is None and not e.get("live") for e in tab["items"]):
        _it_note("feed", it.InnertubeError("videos tab parsed without dates"))
        return []                              # undatable rows would sink to the bottom: yt-dlp
    chan = tab["channel"].get("name") or ""
    rows = []
    for e in tab["items"][:per_channel]:
        if e.get("members"):
            continue
        ago = e.get("ago")
        if e.get("live") or e.get("upcoming"):
            sort_ts = now                          # ride the top, like the yt-dlp path does
        else:
            sort_ts = (now - ago) if ago is not None else 0
        row = _it_video_item(e, chan, now)
        row.update({"channel_id": cid, "is_short": 0})
        rows.append((sort_ts, row))
    _learn_durations({e["id"]: e["duration"] for e in tab["items"] if e.get("duration")})
    return rows


@_timed_fn("feed.ytdlp")
def _feed_from_ytdlp(path, cid, per_channel):
    """FALLBACK: a channel's /videos tab via yt-dlp (flat). Duration/live/approximate-date inline;
    the tab excludes Shorts itself, so no separate durations or Shorts pass is needed."""
    url = "https://www.youtube.com/channel/%s/videos" % cid
    try:
        with _cookies_args() as cargs:
            proc = _ytdlp_run(
                path, [*_COMMON_ARGS, *cargs, "--flat-playlist",
                       "--extractor-args", "youtubetab:approximate_date",
                       "--playlist-items", "1:%d" % per_channel,
                       "--dump-single-json", "--", url], 60)
        if proc.returncode != 0:
            return []
        data = json.loads(proc.stdout)
    except Exception:
        return []
    if not isinstance(data, dict):   # a dead channel must not take the whole feed down
        return []
    chan = data.get("channel") or data.get("uploader") or ""
    rows = []
    for e in (data.get("entries") or []):
        vid = e.get("id")
        if not vid or _is_members_only(e):
            continue
        ls = e.get("live_status")
        live = 1 if (ls == "is_live" or e.get("is_live")) else 0
        # Live/upcoming items carry no upload timestamp — key them by NOW so they ride the top (newer
        # than any real upload), NOT the bottom where a 0 key would sink them. Using now (not a huge
        # now+1e9 sentinel) means an ended stream that then fails to refetch AGES and sinks on its
        # own, instead of staying pinned above everyone else's fresh uploads forever.
        ts = e.get("timestamp")
        sort_ts = ts if ts else (time.time() if ls in ("is_live", "is_upcoming") else 0)
        rows.append((sort_ts, {
            "id": vid,
            "title": e.get("title") or "(untitled)",
            "uploader": e.get("uploader") or e.get("channel") or chan,
            "duration": e.get("duration") or 0,
            "thumbnail": _video_thumb(vid) or _pick_thumb(e),
            "views": e.get("view_count") or 0,
            "posted": _rel_date(e),
            "live": live,
            "channel_id": cid,
            "is_short": 0,
        }))
    return rows


@_timed_fn("feed.rss")
def _feed_from_rss(cid, per_channel):
    """PRIMARY: a channel's RSS feed (feeds/videos.xml). Fast + exact dates but — the fast-mode
    tradeoff NewPipe/FreeTube also live with — NO duration or live status (both come back 0). Same
    item shape as the yt-dlp path so the merged feed stays uniform."""
    if not str(cid).startswith("UC"):
        return []
    try:
        ents = _parse_feed_entries(_rss_get(cid))
    except Exception:
        return []
    rows = []
    for e in ents[:per_channel]:
        vid = e["id"]
        rows.append((_iso_ts(e.get("published")), {
            "id": vid,
            "title": e["title"],
            "uploader": e["uploader"],
            "duration": 0,                         # RSS carries no length (fast-mode tradeoff)
            "thumbnail": _video_thumb(vid),
            "views": e.get("views") or 0,
            "posted": _rel_from_iso(e.get("published")),
            "live": 0,                             # …nor live status
            "channel_id": cid,
            "is_short": e.get("is_short") or 0,
        }))
    return rows


# Feed cache — PER CHANNEL, in ONE file: {cid: {"ts": fetched_at, "rows": [[sort_ts, item], ...]}}.
# Per-channel entries let a refresh re-fetch only channels likely to have something new (adaptive
# TTL) and keep a channel's last-good videos when its own fetch fails (resilience).
_feed_cache = {}
_feed_state = {"loaded": False}


def _feed_cache_path():
    return os.path.join(_data_dir(), "feed_cache.json")


def _channel_ttl(newest_ts):
    """Adaptive refresh interval for one channel, from the age of its newest upload — hot channels
    refresh often, dormant ones coast on a long TTL, so a refresh only spawns yt-dlp for channels
    that plausibly posted since last time."""
    age = time.time() - (newest_ts or 0)
    if age < 2 * 86400:
        return 10 * 60        # posted in the last 2 days → check every 10 min
    if age < 14 * 86400:
        return 60 * 60        # last 2 weeks → hourly
    if age < 60 * 86400:
        return 6 * 3600       # last 2 months → every 6 h
    return 24 * 3600          # dormant → daily


def _channel_due(cid, now):
    """A channel needs re-fetching: never cached, or past its (adaptive) TTL. An entry with NO rows
    (a new sub whose fetch failed, or a channel with no uploads) retries hourly — often enough to
    recover, but not on every refresh (which would thrash it and pin the feed 'stale' forever)."""
    ent = _feed_cache.get(cid)
    if not ent:
        return True
    rows = ent.get("rows") or []
    if not rows:
        return (now - ent.get("ts", 0)) > 3600
    newest = max(r[0] for r in rows)
    return (now - ent.get("ts", 0)) > _channel_ttl(newest)


def _merged_feed_items(ids):
    """Every subscribed channel's cached rows, merged newest-first → [item, ...]. Skips any
    malformed row so a hand-edited / corrupted cache file can't crash the sort."""
    rows = []
    for cid in ids:
        ent = _feed_cache.get(cid)
        if not ent:
            continue
        for r in (ent.get("rows") or []):
            if isinstance(r, (list, tuple)) and len(r) == 2 and isinstance(r[0], (int, float)):
                rows.append(r)
    rows.sort(key=lambda r: r[0], reverse=True)
    return [r[1] for r in rows]


def _load_feed_cache():
    """Per-channel feed cache from disk (so a cold launch shows the previous feed instantly)."""
    out = {}
    try:
        with open(_feed_cache_path()) as f:
            d = json.load(f)
        if isinstance(d, dict):
            for cid, ent in d.items():
                if isinstance(ent, dict) and isinstance(ent.get("rows"), list):
                    out[cid] = {"ts": float(ent.get("ts") or 0.0), "rows": ent["rows"]}
    except Exception:
        pass
    return out


def _save_feed_cache():
    try:
        # A copy: json.dump iterating the live dict while another lane adds a channel raises
        # "dictionary changed size during iteration" (and the save was silently lost).
        _atomic_write_json(_feed_cache_path(), dict(_feed_cache))
    except Exception:
        pass


def _feed_hydrate():
    if not _feed_state["loaded"]:
        _feed_cache.update(_load_feed_cache())
        _feed_state["loaded"] = True


@_timed_fn("feed.TOTAL")
def subscription_feed(limit=100, force=False, refresh_all=False):
    """Subscribed channels' recent uploads, newest first — built per channel from yt-dlp /videos
    (+ RSS fallback, see _feed_fetch_channel), cached PER CHANNEL to disk and served
    stale-while-revalidate.

    - normal load (force=False): return the merged cache IMMEDIATELY with a `stale` flag (true when
      any channel is past its adaptive TTL); the caller refreshes in the background.
    - background refresh (force=True): re-fetch only channels that are DUE (adaptive per-channel TTL)
      — hot channels often, dormant ones rarely.
    - pull-to-refresh (refresh_all=True): re-fetch every channel.
    A channel whose fetch fails keeps its last-good videos instead of dropping out of the feed."""
    subs = list_subscriptions()
    ids = [s.get("id") for s in subs if str(s.get("id") or "").startswith("UC")]
    if not ids:
        return {"ok": True, "items": []}
    _feed_hydrate()
    now = time.time()
    have_cache = any((_feed_cache.get(c) or {}).get("rows") for c in ids)
    if not force and have_cache:
        return {"ok": True,
                "items": _feed_hide_watched(_feed_shorts_filter(_feed_with_durations(_merged_feed_items(ids))))[:int(limit)],
                "cached": True,
                "stale": any(_channel_due(c, now) for c in ids)}
    path = _ytdlp_path()
    if not path:
        return {"ok": False, "error": "yt-dlp not found", "items": []}
    due = ids if refresh_all else [c for c in ids if _channel_due(c, now)]
    if due:
        per_channel = 15
        results = {}

        def fetch(c):
            # One broken channel (terminated, a parser surprise) is an empty answer for that
            # channel only; raising here would lose the whole refresh.
            try:
                return _feed_fetch_channel(path, c, per_channel)
            except Exception as ex:
                _tlog("feed fetch %s failed: %s" % (c, ex))
                return []
        try:
            import concurrent.futures
            # RSS / InnerTube are plain HTTP (I/O-bound) — wider than the old spawn-sized pool.
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(due))) as ex:
                for cid, rows in zip(due, ex.map(fetch, due)):
                    results[cid] = rows
        except Exception:
            for cid in due:
                results[cid] = fetch(cid)
        for cid, rows in results.items():
            if rows:                          # success → replace + stamp
                _feed_cache[cid] = {"ts": now, "rows": rows}
            else:
                # Empty (a transient failure, or a channel with no uploads): KEEP any last-good
                # videos (resilience — the channel doesn't vanish) but stamp the attempt so it
                # retries on its TTL instead of re-fetching every refresh + pinning 'stale' forever.
                ent = _feed_cache.get(cid) or {}
                _feed_cache[cid] = {"ts": now, "rows": ent.get("rows") or []}
    for cid in list(_feed_cache):             # drop unsubscribed channels
        if cid not in ids:
            del _feed_cache[cid]
    _save_feed_cache()
    return {"ok": True,
            "items": _feed_hide_watched(_feed_shorts_filter(_feed_with_durations(_merged_feed_items(ids))))[:int(limit)],
            "cached": False,
            "stale": any(_channel_due(c, now) for c in ids)}


_feed_durations_cache = {"ts": 0.0, "map": {}, "shorts": set()}
_durations_lock = threading.Lock()        # _learn_durations runs from several workers
# Feed ids we already tried (and failed) to classify → when. A live/upcoming item is in no tab
# with a length, so without this every feed_durations call would re-ask for it.
_classify_tried = {}
_CLASSIFY_RETRY = 30 * 60


def _learn_durations(durs, shorts=()):
    """Fold lengths / Shorts membership learned from ANY listing (channel pages, the feed fallback,
    classification) into the persistent caches. Both are immutable facts, so a hit is good forever.
    Saves a snapshot only when something new arrived."""
    if not durs and not shorts:
        return
    with _durations_lock:
        dmap = _feed_durations_cache.get("map") or _load_saved_durations()
        sh = _feed_durations_cache.get("shorts") or _load_saved_shorts()
        new_d = {k: int(v) for k, v in (durs or {}).items() if v and dmap.get(k) != int(v)}
        new_s = set(shorts or ()) - sh
        dmap.update(new_d)
        sh |= new_s
        _feed_durations_cache["map"], _feed_durations_cache["shorts"] = dmap, sh
        # Saved under the lock: two workers saving their snapshots outside it could land the
        # older one last and drop what the newer one learned.
        if new_d:
            _save_durations(dict(dmap))
        if new_s:
            _save_shorts(set(sh))


def _durations_path():
    return os.path.join(_data_dir(), "durations.json")


def _shorts_path():
    return os.path.join(_data_dir(), "feed_shorts.json")


def _load_saved_durations():
    """The persistent {video_id: seconds} duration cache. Durations are immutable, so a hit is
    valid forever — this is what lets a repeat launch skip yt-dlp entirely."""
    try:
        with open(_durations_path()) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {}
        return {str(k): int(v) for k, v in d.items() if int(v or 0) > 0}
    except Exception:
        return {}


def _load_saved_shorts():
    """The persistent set of video ids known to be Shorts (from channel /shorts tabs). Once a
    Short, always a Short → cache forever."""
    try:
        with open(_shorts_path()) as f:
            d = json.load(f)
        return set(str(v) for v in d) if isinstance(d, list) else set()
    except Exception:
        return set()


def _save_durations(d):
    try:
        if len(d) > 3000:                       # tiny + immutable; keep the file bounded (LRU tail)
            d = dict(list(d.items())[-3000:])
        _atomic_write_json(_durations_path(), d)
    except Exception:
        pass


def _save_shorts(s):
    try:
        lst = list(s)
        if len(lst) > 4000:
            lst = lst[-4000:]
        _atomic_write_json(_shorts_path(), lst)
    except Exception:
        pass


@_timed_fn("feed.durations")
def feed_durations(limit_per_channel=30, force=False):
    """For the current subscription feed: {"durations": {video_id: seconds}, "shorts": [video_id]}.
    RSS carries neither (except a /shorts/ link), so this pulls them from each channel's listing —
    the Videos tab for durations, then only if something is still unclassified the Shorts tab (a
    DEFINITIVE Shorts set: length can't tell a 3-min Short from a 3-min video) and the Live tab.
    InnerTube answers each with one POST; yt-dlp's flat listing (2 spawns per channel — the old
    multi-minute launch stall) is only the per-channel fallback. Both facts are immutable, so
    they're cached to disk and only channels with a never-classified video are asked at all. A
    fully-cached feed returns instantly with no network. Called AFTER the RSS feed shows."""
    subs = list_subscriptions()
    chan_base = {}
    for s in subs:
        cid = str(s.get("id") or "")
        url = str(s.get("url") or "")
        if cid.startswith("UC"):
            chan_base[cid] = "https://www.youtube.com/channel/%s" % cid
        elif url:
            chan_base[url] = url.rstrip("/")
    if not chan_base:
        return {"durations": {}, "shorts": []}

    # Warm both caches from disk once (durations + Shorts membership are immutable → never expire).
    dmap = _feed_durations_cache["map"] or _load_saved_durations()
    shorts = _feed_durations_cache["shorts"] or _load_saved_shorts()
    _feed_durations_cache["map"], _feed_durations_cache["shorts"] = dmap, shorts

    _feed_hydrate()
    uc_ids = [s.get("id") for s in subs if str(s.get("id") or "").startswith("UC")]
    feed_items = _merged_feed_items(uc_ids)      # the ids actually in the feed (per-channel cache)
    feed_ids = [it.get("id") for it in feed_items if it.get("id")]

    # Shorts detected straight from the RSS <link> href are classified for free: merge them
    # into the persistent Shorts set so _feed_shorts_filter drops them with no flash AND no
    # Shorts-tab listing below is ever spent classifying them.
    _learn_durations({}, {it.get("id") for it in feed_items if it.get("is_short")})

    def result():
        dm = _feed_durations_cache.get("map") or {}
        sh = _feed_durations_cache.get("shorts") or set()
        return {"durations": {k: dm[k] for k in feed_ids if k in dm},
                "shorts": [v for v in feed_ids if v in sh]}

    # A feed video is "classified" once we have its duration OR know it's a Short.
    def classified(vid):
        return vid in (_feed_durations_cache.get("map") or {}) or \
            vid in (_feed_durations_cache.get("shorts") or set())

    now = time.time()
    # Ids that couldn't be classified recently (live/upcoming: in no tab with a length) aren't
    # re-asked on every call — only on an explicit refresh or after _CLASSIFY_RETRY.
    def retry_ok(vid):
        return force or now - _classify_tried.get(vid, 0) > _CLASSIFY_RETRY

    need = {}                                   # channel key -> base URL
    need_ids = {}                               # channel key -> unclassified ids (None = unknown)
    for it in feed_items:
        vid, cid = it.get("id"), it.get("channel_id")
        if vid and not classified(vid) and cid in chan_base and retry_ok(vid):
            need[cid] = chan_base[cid]
            need_ids.setdefault(cid, set()).add(vid)
    # Feed items without a matching channel_id tag (an older in-memory cache) but with unclassified
    # ids → fall back to scanning all channels. Runs BEFORE the instant-return below.
    if not need and feed_ids and any(not classified(v) and retry_ok(v) for v in feed_ids):
        need = dict(chan_base)
    if not need:
        return result()                         # nothing to fetch → instant, no network
    it_mod = _it()
    # Throttle the yt-dlp fallback: don't re-spawn within 5 min unless the user explicitly
    # refreshed. (The InnerTube path is a POST per channel, so the per-id retry gate above is
    # all it needs.)
    if it_mod is None and not force and _feed_durations_cache["ts"] \
            and now - _feed_durations_cache["ts"] < 300:
        return result()
    path = _ytdlp_path()

    def flat(url):
        if not path:
            return []
        try:
            with _cookies_args() as cargs:
                proc = _ytdlp_run(
                    path, [*_COMMON_ARGS, *cargs, "--flat-playlist",
                           "--playlist-end", str(int(limit_per_channel)), "--dump-single-json", "--", url], 60)
            if proc.returncode != 0:
                return []
            return json.loads(proc.stdout).get("entries", []) or []
        except Exception:
            return []

    def fetch_ytdlp(base):
        durs, sh = {}, set()
        for e in flat(base + "/videos"):
            vid, dur = e.get("id"), e.get("duration")
            if vid and dur:
                durs[vid] = int(dur)
        # Always scan /shorts (even with hide_shorts off): it's what CLASSIFIES a Short, so it drops
        # out of `need` (keeping the no-subprocess fast path working) and the filter stays correct if
        # the user later turns hide_shorts on.
        for e in flat(base + "/shorts"):
            vid, dur = e.get("id"), e.get("duration")
            if vid:
                sh.add(vid)
                if dur:
                    durs[vid] = int(dur)
        return durs, sh

    def fetch_innertube(key, base):
        """Videos tab (lengths) → only if ids are still unclassified, the Shorts tab, then the Live
        tab (past streams carry a length). One POST each; None = fall back to yt-dlp."""
        if it_mod is None:
            return None
        try:
            _force_ipv4()
            cid = key if key.startswith("UC") else it_mod.resolve_channel(base)
            durs, sh = {}, set()
            items = it_mod.channel_tab(cid, "videos")["items"]
            for e in items:
                if e.get("duration"):
                    durs[e["id"]] = e["duration"]
            if not durs and not any(e.get("live") or e.get("upcoming") for e in items):
                raise it_mod.InnertubeError("videos tab parsed without lengths")
        except Exception as ex:
            _it_note("feed-lengths", ex)
            _tlog("feed.durations innertube %s failed → yt-dlp: %s" % (key, str(ex)[:100]))
            return None
        want = need_ids.get(key)
        left = (set(want) if want is not None else {"?"}) - set(durs)
        for tab in ("shorts", "streams"):
            if not left:
                break
            try:
                items = it_mod.channel_tab(cid, tab)["items"]
            except Exception:
                continue                        # no such tab on this channel, or a blip
            for e in items:
                if e.get("short"):
                    sh.add(e["id"])
                elif e.get("duration"):
                    durs[e["id"]] = e["duration"]
            left -= set(durs) | sh
        return durs, sh

    def fetch(kb):
        res = fetch_innertube(*kb)
        return res if res is not None else fetch_ytdlp(kb[1])

    jobs = list(need.items())
    _tlog("feed.durations: classifying %d channel(s) (%s)"
          % (len(jobs), "innertube" if it_mod is not None else "2 yt-dlp listings each"))
    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
            for durs, sh in ex.map(fetch, jobs):
                _learn_durations(durs, sh)
    except Exception:
        for kb in jobs:
            durs, sh = fetch(kb)
            _learn_durations(durs, sh)
    _feed_durations_cache["ts"] = now
    for ids in need_ids.values():               # whatever is STILL unclassified waits for a retry
        for vid in ids:
            if not classified(vid):
                _classify_tried[vid] = now
    if len(_classify_tried) > 2000:
        for vid in [v for v, t in _classify_tried.items() if now - t > _CLASSIFY_RETRY]:
            _classify_tried.pop(vid, None)
    return result()


# Reply fetching multiplies the continuation walk, so it's globally budgeted: yt-dlp walks
# top threads first, so the most-visible threads get their replies and deeper ones don't —
# keeping an on-demand load bounded rather than paging every thread's replies.
_REPLIES_PER_THREAD = 12   # cap replies fetched from any one comment thread
_REPLY_BUDGET = 150        # global reply cap across all threads for one comments() call


def _comments_innertube(video_id, n, with_replies):
    """comments() from InnerTube /next, or None to let yt-dlp walk them. Top-level pages are
    sequential (each carries the next token); the first reply page of each thread is fetched in
    parallel within the same budgets as the yt-dlp walk, which paged them one thread at a time.
    Anonymous, like the rest of the InnerTube paths (yt-dlp's walk carried the login)."""
    it = _it()
    vid = video_id or ""
    if "://" in vid:
        pu = parse_youtube_url(vid) or {}
        vid = pu.get("id") or "" if pu.get("kind") == "video" else ""
    if it is None or not re.fullmatch(r"[\w-]{11}", vid):
        return None
    _t = time.time()
    tok = (_watch_next(vid) or {}).get("comments_token")
    if not tok:
        return None                     # comments off, or no watch-next: yt-dlp has the last word
    threads, total, pages = [], None, 0
    try:
        while tok and len(threads) < n and pages < 10:
            page = it.comments(tok)
            pages += 1
            if total is None:
                total = page["total"]
            if pages == 1 and not page["comments"] and (page["total"] or page["continuation"]):
                raise it.InnertubeError("comments page parsed empty")
            threads += page["comments"]
            tok = page["continuation"]
            if not page["comments"]:
                break
    except Exception as ex:
        _it_note("comments", ex)
        _tlog("comments innertube failed %.2fs → yt-dlp: %s" % (time.time() - _t, str(ex)[:120]))
        return None
    threads = threads[:n]
    plan, budget = [], _REPLY_BUDGET
    for c in threads if with_replies else []:
        if budget <= 0:
            break
        if c.get("replies_token") and c.get("reply_count"):
            plan.append(c)
            budget -= min(c["reply_count"], _REPLIES_PER_THREAD)

    def fetch(c):
        try:
            return c["id"], it.comments(c["replies_token"])["comments"][:_REPLIES_PER_THREAD]
        except Exception:
            return c["id"], []          # a thread's replies are optional; the comment still shows
    replies = {}
    if plan:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(plan))) as ex:
            for cid, rs in ex.map(fetch, plan):
                replies[cid] = rs
    keys = ("id", "author", "text", "likes", "time", "thumbnail", "is_uploader")
    out = []
    for c in threads:
        kids = replies.get(c["id"]) or []
        item = {k: c[k] for k in keys}
        item["replies"] = [{k: r[k] for k in keys} for r in kids]
        rc = c.get("reply_count") or 0
        item["reply_count"] = rc if rc >= len(kids) else len(kids)
        out.append(item)
    _tlog("comments innertube %.2fs (%d threads, %d pages, %d reply fetches)"
          % (time.time() - _t, len(out), pages, len(plan)))
    return {"ok": True, "comments": out, "count": len(out), "total": total or 0}


@_timed_fn("q.comments")
def comments(video_id, limit=20, with_replies=True):
    """Fetch up to `limit` top-level comments (top-sorted) for a video, each carrying a bounded
    set of its replies under `replies` (+ `reply_count`).

    Comment extraction walks YouTube's continuation tokens, so it's slow — this is called
    on demand (tap to load), never as part of resolve(). Replies add more walking, capped by
    _REPLY_BUDGET / _REPLIES_PER_THREAD (or off entirely via with_replies=False); NB the reply
    walk costs ~one round-trip PER THREAD regardless of the per-thread cap (YouTube pages
    replies per continuation), so the budgets bound the payload, not the latency. The UI
    reveals the batch a few at a time as the user scrolls, and reveals each thread's replies
    on tap.

    Speed (measured 2026-09-06): the dump is run LEAN — player_skip=js,configs and
    skip=hls,dash spare the player JS + n-sig solve (a Deno spawn!) and the manifest fetches,
    none of which comments need; --ignore-no-formats-error keeps a formats-less extraction
    from erroring. Comments come from the watch page's initial data + /next continuations,
    verified unaffected. With fast-resolve on, the walk also runs IN-PROCESS on the warm
    YoutubeDL (no spawn tax, warm TLS); any failure falls back to the binary as usual.

    InnerTube answers first (_comments_innertube: a few /next pages plus parallel reply pages,
    ~1s instead of the 3–8s walk); all of the above is its fallback.
    """
    try:
        n = max(1, int(limit))
    except (TypeError, ValueError):
        n = 50
    fast = _comments_innertube(video_id, n, with_replies)
    if fast is not None:
        return fast
    path = _ytdlp_path()
    if not path:
        return {"ok": False, "error": "yt-dlp not found"}
    url = video_id
    if "://" not in url:
        url = "https://www.youtube.com/watch?v=" + video_id
    # max_comments = total, max-parents, max-replies (global), max-replies-per-thread.
    # The trailing LEAN profile skips everything a comment walk doesn't need (see docstring).
    _lean = ";player_skip=js,configs;skip=hls,dash,translated_subs"
    if with_replies:
        xargs = ("youtube:max_comments=%d,%d,%d,%d;comment_sort=top"
                 % (n + _REPLY_BUDGET, n, _REPLY_BUDGET, _REPLIES_PER_THREAD)) + _lean
    else:
        xargs = "youtube:max_comments=%d,%d,0,0;comment_sort=top" % (n, n) + _lean
    # NB: comments MUST use yt-dlp's default (web) client — unlike video_info. player_client=android
    # was MEASURED on-device to return NO comments (the mobile player response carries no comment
    # continuation), so forcing it just burns a whole extraction before falling back to web. Don't
    # reintroduce it; the web client's continuation walk is the only path that yields comments.
    try:
        data = None
        if _fast_resolve_ready():          # warm in-process walk first; binary on ANY failure
            try:
                _tc = time.time()
                data = _inproc_comments(url, ["--extractor-args", xargs])
                _tlog("comments(inproc) %.2fs" % (time.time() - _tc))
            except Exception as ex:
                _tlog("comments(inproc) failed → binary: %s" % str(ex)[:120])
                data = None
        if data is None:
            with _cookies_args() as cargs:
                proc = subprocess.run(
                    [path, *_COMMON_ARGS, *cargs, "--skip-download", "--write-comments",
                     "--ignore-no-formats-error",
                     "--extractor-args", xargs, "--dump-single-json", "--", url],
                    capture_output=True, text=True, timeout=180)
            if proc.returncode != 0:
                return {"ok": False, "error": _ytdlp_err(proc.stderr, "comments failed")}
            data = json.loads(proc.stdout)
        raw = data.get("comments") or []

        def _fmt(c):
            return {
                "id": c.get("id") or "",
                "author": c.get("author") or "",
                "text": c.get("text") or "",
                "likes": c.get("like_count") or 0,
                "time": c.get("_time_text") or "",
                "thumbnail": c.get("author_thumbnail") or "",
                "is_uploader": bool(c.get("author_is_uploader")),
            }

        # yt-dlp returns a FLAT list; a reply carries parent == its top-level comment's id
        # ("root" marks a top-level comment). Group replies under their parent, order preserved.
        replies_by_parent = {}
        for c in raw:
            parent = c.get("parent")
            if parent and parent != "root":
                replies_by_parent.setdefault(parent, []).append(c)

        out = []
        for c in raw:
            parent = c.get("parent")
            if parent and parent != "root":
                continue                      # a reply — attached to its parent below
            item = _fmt(c)
            kids = replies_by_parent.get(c.get("id")) or []
            item["replies"] = [_fmt(k) for k in kids]
            # `reply_count` is YouTube's real thread total when yt-dlp reports it (may exceed the
            # budgeted count we fetched); the UI shows however many `replies` it actually got.
            rc = c.get("reply_count")
            item["reply_count"] = rc if isinstance(rc, int) and rc >= len(kids) else len(kids)
            out.append(item)
            if len(out) >= n:
                break
        # comment_count is YouTube's real total; `count` is how many top-level we actually fetched.
        return {"ok": True, "comments": out, "count": len(out),
                "total": data.get("comment_count") or 0}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "comments timed out"}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


# --------------------------------------------------------------------------- #
# Downloads: audio (140 → .m4a; needs no ffmpeg), or video — best HD video+audio merged via
# ffmpeg (→ .mkv). Video REQUIRES ffmpeg since 2026: YouTube removed the muxed progressive
# formats (22/18 — verified live 2026-09-06, `-f 22/18` errors), so there is no single-file
# fallback left; without ffmpeg a video download is refused with a clear message instead of
# failing on yt-dlp's cryptic "Requested format is not available".
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
    s = re.sub(r"[^\w\-. ]+", "_", s or "")[:80]
    # \w keeps non-ASCII letters, and 80 CJK characters are 240 bytes: with the " [id] kind.ext"
    # suffix that passes the 255-BYTE filename limit (ENAMETOOLONG). Cap the bytes too.
    s = s.encode("utf-8")[:150].decode("utf-8", "ignore").strip()
    return s or "video"


# downloads.json is read-modify-written by the download thread, delete_download and
# list_downloads' self-heal; reentrant because the writers read through list_downloads.
_downloads_lock = threading.RLock()


def list_downloads():
    """Completed downloads: [{id, title, kind, path}, ...]. Drops entries whose file is gone."""
    with _downloads_lock:
        try:
            with open(_downloads_path()) as f:
                lst = json.load(f)
            if not isinstance(lst, list):
                return []
        except Exception:
            return []
        live = [d for d in lst
                if isinstance(d, dict) and d.get("path") and os.path.exists(d["path"])]
        if len(live) != len(lst):
            _save_downloads(live)
        return live


def _save_downloads(lst):
    try:
        _atomic_write_json(_downloads_path(), lst)
    except Exception:
        pass


def download(video_id, title, kind):
    """Kick off a background download. kind = "audio" (m4a, no ffmpeg needed) | "video" (best
    HD video+audio merged via ffmpeg → .mkv). Video downloads REFUSE with a clear message when
    ffmpeg is missing — the old <=360p muxed fallback died with YouTube's progressive formats."""
    import pyotherside
    kind = "audio" if kind == "audio" else "video"
    merge = []
    if kind == "audio":
        fmt, ext = "140", "m4a"
    elif not _ffmpeg_dir():
        # No single-file fallback exists any more (see the section comment above), so running
        # yt-dlp anyway just burns a spawn to produce a cryptic error. Refuse up front with a
        # message that says WHAT to do; the UI surfaces it through the normal done event.
        pyotherside.send("download_done", video_id, kind, False,
                         "Video downloads need ffmpeg (YouTube removed the combined formats). "
                         "Install it from Providers — audio downloads work without it.")
        return {"ok": False, "error": "ffmpeg required for video downloads"}
    else:
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
        ck = _write_cookies_temp()   # authenticated download (age-gated / members); rm in finally
        try:
            _ensure_pot_server()  # a download is just as PO-gated as playback
            proc = subprocess.Popen(
                [binp, *_COMMON_ARGS, *(["--cookies", ck] if ck else []),
                 *_yt_extractor_args(want_pot=True, cookies=bool(ck)), *_pot_ytdlp_args(),
                 *_ffmpeg_args(),
                 # --no-continue: with --no-part a file already at the final name looks like
                 # a partial download, and "resuming" it 403s or appends to a fixed-up file.
                 "--no-playlist", "-f", fmt, *merge, "--no-part", "--no-continue", "--newline",
                 "-o", base + ".%(ext)s", "--", url],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                # Die with the app: the result can only be REGISTERED (downloads.json + the
                # download_done event) while the app lives, so an app-exit orphan would keep
                # burning network/CPU writing a file the app can never list. Same doctrine
                # as every other child in this module (D6).
                preexec_fn=_set_pdeathsig)
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
                cand = glob.glob(glob.escape(base) + ".*")   # base has "[id]": a glob class
                fpath = cand[0] if cand else fpath
            if proc.returncode == 0 and os.path.exists(fpath):
                with _downloads_lock:
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
        finally:
            if ck:
                try:
                    os.remove(ck)
                except Exception:
                    pass

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def delete_download(video_id, kind):
    keep = []
    with _downloads_lock:
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
        _atomic_write_json(_playlists_path(), lst)
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


@_timed_fn("q.playlist")
def youtube_playlist(ref, limit=200):
    """Fetch a YouTube playlist's videos (flat). ref = a list id or any playlist URL."""
    path = _ytdlp_path()
    if not path:
        return {"ok": False, "error": "yt-dlp not found"}
    url = ref if "://" in (ref or "") else ("https://www.youtube.com/playlist?list=" + (ref or ""))
    try:
        with _cookies_args() as cargs:
            proc = _ytdlp_run(
                path, [*_COMMON_ARGS, *cargs, *_yt_extractor_args(), "--flat-playlist",
                       "--playlist-end", str(int(limit)), "--dump-single-json", "--", url], 120)
        if proc.returncode != 0:
            return {"ok": False, "error": _ytdlp_err(proc.stderr, "playlist fetch failed")}
        data = json.loads(proc.stdout)
        items = [{
            "id": e.get("id", ""),
            "title": e.get("title") or "(untitled)",
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
            with _cookies_args() as cargs:
                proc = _ytdlp_run(
                    path, [*_COMMON_ARGS, *cargs, *_yt_extractor_args(), "--flat-playlist",
                           "--dump-single-json", "--", base + tab], 90)
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
