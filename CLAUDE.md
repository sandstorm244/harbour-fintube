# CLAUDE.md

Guidance for AI agents and new contributors working in this repo. The README is the
user-facing intro; this file is the working map — where things live, the invariants that
shape the code, and the traps that bite you if you edit blind. Almost every non-obvious
line already has a comment explaining *why*; when in doubt, read the comment at the cited
location before changing it.

## What this is

FinTube is a native YouTube client for **Sailfish OS** (version 1.4.1, GPLv3). Three layers
that talk in a strict line:

| Layer  | Where                                        | Role |
|--------|----------------------------------------------|------|
| UI     | `qml/pages/*.qml`, `qml/*.qml`               | Silica views. Deliberately dumb — no real logic. |
| Bridge | `qml/Backend.qml`                            | Async PyOtherSide facade. Nothing returns a value. |
| Engine | `python/youfish.py` (6.3k lines)             | yt-dlp, format selection, media proxy, PO-token sidecar, all data stores. |
| Player | `src/videoplayer.cpp`, `src/hwvideosink.cpp` | C++ GStreamer `VideoPlayer` (a QML type) + hardware EGLImage sink. |

Cookie login lives in `python/ytm.py` (a slim sibling of FinTune's music `ytm.py`; here its
whole public API is `netscape_cookies()`). `youfish.py` is **shared** with the FinTune music
app — many test paths are guard-aware so the same engine works in both.

**Naming quirk:** the product/QML type is `FinTube`, but the engine is branded **"youfish"**
internally — env vars `YOUFISH_DEBUG` / `YOUFISH_HWDEC`, the `[youfish]` log prefix, the
`youfish-player` pipeline name. Same thing.

## Build, run, test

```sh
sh build.sh          # shadow build via sfdk → ../harbour-fintube.build/RPMS/
                     # override target: TARGET=SailfishOS-5.1.0.11-aarch64 sh build.sh
sh clean.sh          # remove build scatter from the source tree
python3 python/test_youfish.py   # offline engine tests — pure stdlib, externals mocked
```

- The **engine test suite** (`python/test_youfish.py`, ~2.6k lines) is the main safety net.
  It is fully offline (no device, network, or yt-dlp) and runs in CI on every push/PR
  (`.github/workflows/tests.yml`). It had silently rotted to 8 failures before CI was added —
  keep it green. **Run it after any change to `youfish.py`.**
- The **release build** (`.github/workflows/build.yml`) only fires on tags matching `1.*` and
  cross-builds aarch64, publishing the RPM as a GitHub release.
- The C++ player and the real yt-dlp/ffmpeg/Deno/PO-token processes are **not** covered by any
  automated test — verify those on-device.

## The invariants (do not break these)

1. **The localhost media proxy is not optional.** GStreamer's libsoup stack gets an unfixable
   403 from googlevideo; urllib with identical headers gets 206. So every DASH + progressive
   stream round-trips through an in-process `127.0.0.1` HTTP server (`_MediaProxyHandler`,
   `youfish.py:1012`) that re-fetches with the format's own User-Agent and serves backpressured
   byte ranges. HLS m3u8 is passed through **unproxied** (proxying a manifest breaks segment
   URLs). This proxy is also why the app must run **unsandboxed** (`[X-Sailjail]
   Sandboxing=Disabled` in `harbour-fintube.desktop`) and therefore **cannot ship on Jolla
   Harbour** — distribute via Chum/OpenRepos.

2. **Format selection is property-based, never itags.** `_video_candidates` /
   `_audio_candidates` (`youfish.py:154` / `:237`) filter and rank by codec family, height, fps,
   and language. The default pick *and* the quality/dub menus all read from these two functions —
   that is where playback policy lives. Itags rotate; a hardcoded list silently loses variants.
   AV1 is dropped (`_codec_family` returns `''`) — no on-device decoder.

3. **yt-dlp has two forms that must stay in channel lockstep.** The frozen binary is the
   universal default and the fallback for *every* failure; the importable zipapp is an automatic
   in-process fast path, gated on device Python ≥ 3.10 (`_FAST_RESOLVE_PY_OK`). "Fast resolve" is
   **plumbing, not a setting** — no on/off key. A nightly binary next to a stable zipapp silently
   misses the fix the user switched channels for; the ProvidersPage version-skew alarm exists for
   the cases lockstep can't cover.

4. **`PR_SET_PDEATHSIG` is thread-scoped, not process-scoped.** The Deno PO-token sidecar MUST be
   spawned from a dedicated long-lived owner thread that parks on `proc.wait()`
   (`_ensure_pot_server`, `youfish.py:2770`). Fork it from any short-lived caller (an install
   thread, a reader-thread re-resolve) and the kernel SIGKILLs it the instant that caller returns.

5. **The stream registry has a strict lock order:** `_streams_lock` → `s.cond`. `do_GET` takes
   only `s.cond`, never `_streams_lock`; no `proc.wait()` ever runs under a lock (killed children
   are reaped off-lock). Preserve this in any proxy/stream change.

6. **All resolve inputs that affect output are in the cache key.** `_resolve_key`
   (`youfish.py:1364`) = video id + effective client + pot_active + default_quality + audio_lang +
   hw_decode + signed_in. Changing any output-affecting setting (or login) must call
   `invalidate_resolve_cache()`.

7. **One shared C++ player at app scope.** `gplayer` lives in `harbour-fintube.qml`, not in
   VideoPage, so audio survives navigation. A VideoPage only *borrows* it by reparenting it into
   its `videoSurface`. The readonly `holdsPlayer` (`gplayer.parent === videoSurface`) is the master
   ownership guard — SponsorBlock skips, error recovery, autoplay, position saves, and teardown are
   all gated on it so a back-stack page can never touch the current owner's playback.

8. **Two PyOtherSide workers, one interpreter.** `py` handles everything; `pyFast`
   (`Backend.qml:816`) is a second worker thread for latency-critical playback calls (resolve,
   parseUrl, get_position) so they never queue behind a minutes-long feed backfill. `pyFast` is
   declared **after** `py` on purpose so `pyotherside.send` events still land on the one
   `onReceived`.

9. **qtdemux cannot push-seek.** mp4/m4a branches get an on-disk `downloadbuffer` + `KEY_UNIT`
   seeks; webm/matroska seek exactly with `FLUSH|ACCURATE`. This is why **`videoExt`/`audioExt`
   must be set before the URL properties** — they choose the per-branch seek plan in
   `buildPipeline`. Muxed mode must *clear* the exts. The seek machinery in `videoplayer.cpp`
   (`extIsMp4:19`, seek plan `:534`, `sendSeek:288`, `ASYNC_DONE` handler `:941`,
   `retrySplitSeek:358`) is the hardest part of the C++ layer; its comments are on-device
   measurements — treat them as regression docs.

10. **User data = small JSON stores, atomic writes, stale-while-revalidate.** Everything lives
    under `~/.local/share/harbour-fintube`, written through `_atomic_write_json`
    (`youfish.py:4040`: 0600 temp file → `os.replace`, so a crash can't truncate the live store).
    The feed is the archetype — RSS paints instantly, yt-dlp backfills only *due* + *unclassified*
    entries, durations/avatars cache with immutability or TTL rules. New feature here = a
    `*_path()` helper, a tolerant `_load_*` with a safe default, and `_atomic_write_json` on write.

## Where things live

**Engine (`python/youfish.py`)** — the map, by line:
- Resolve core: `resolve:3462`, `_resolve_uncached:3469`, `_resolve_and_cache:1406` (single-flight
  leader/joiner), `prefetch_resolve:1471` (speculative warm).
- Format selection: `_video_candidates:154`, `_audio_candidates:237`, `_pick_video:3815`,
  `_pick_audio:3834`.
- Mid-stream 403 self-heal: `_reresolve:1253`, `_reader:696` (resumes at the same byte offset,
  same itag ⇒ byte-identical).
- Media proxy: `_MediaProxyHandler:1012`, `_DirectFetch:540` (in-process urllib streamer that
  replaces the per-playback yt-dlp spawn), `_ensure_proxy:1155`, `_proxied:1183`,
  `_proxy_url_ok:452` (SSRF allow-list).
- yt-dlp management: `_ytdlp_path:1554`, `install_ytdlp:1658`, `ytdlp_update:1579`, zipapp at
  `install_ytdlp_zipapp:1769` / `_import_yt_dlp:1899`, in-process dump `_inproc_dump:2073`.
- PO-token sidecar: `_ensure_pot_server:2770`, `_pot_server_flags:2556` (default-deny Deno
  sandbox), `install_pot_provider:3086`, `prewarm:2881`.
- Helpers: `install_deno:2426`, `install_ffmpeg:2202` (SHA-256-pinned to a frozen JVS build).
- Search/discovery: `search:3282`, `_search_filter_sp:3230` (hand-rolled protobuf `sp=`),
  `related:3354`, `search_suggestions:3395`, `parse_youtube_url:3248`.
- Subs + feed: `subscription_feed:5520`, `feed_durations:5633`, feed cache `5437–5516`,
  `channel_videos:5237`, `channel_avatar:5192`.
- Watch state (**two separate stores**): resume points `positions.json`
  (`get_position:4345`/`set_position:4354`), watch history `watch_history.json`
  (`record_watch:4460`, `set_watched:4505`, `watch_history:4416`).
- Playlists: `_load_playlists:6083`…`refresh_playlist:6221`. Downloads: `download:5959`.
  SponsorBlock: `sponsor_segments:4551`. Captions: `caption_tracks:3423`, `caption_cues:4618`.
  Comments: `comments:5761`. Imports: `import_youtube_account:4805`, `import_newpipe:4878`.

**Player (`src/`)** — `main()` in `harbour-fintube.cpp:53` (manual QQuickView so it can install a
64 MB `QNetworkDiskCache` thumbnail cache; demotes `droidvdec` to `GST_RANK_NONE`; disables
persistent GL/scenegraph to survive display-off/on). `VideoPlayer::buildPipeline:428` and
`teardown:703` are the core; `HwVideoSink` (`hwvideosink.cpp`) is the zero-copy EGLImage renderer,
a two-thread object glued by `m_mutex` — GL/EGL calls only in `paint()`/`paintFrame()` on the
render thread, everything else under the lock.

**Bridge/app** — `Backend.qml` (async facade, ~70 mirrored properties, 5 signals), app entry
`harbour-fintube.qml` (owns `gplayer` + `nowPlaying`, cover, MPRIS via Loader, D-Bus `openUrl`).
**VideoPage.qml** (1.7k lines) is the watch screen and by far the most intricate QML file — read
`holdsPlayer` + the attach/park reparenting dance first.

## Gotchas that will bite you

- **Set `videoExt`/`audioExt` before the URLs** (see invariant 9). Ordering-sensitive.
- **Qt 5.6 platform floor:** no `Qt.callLater` (use a zero-interval Timer restart); `Connections.enabled`
  needs 5.7 (disable by setting `target: null`); `Screen.hasCutouts`/cutout APIs are typeof-guarded.
- **`droidvdec` is demoted to rank NONE in `main()`** yet HW mode still uses it — `buildPipeline`
  instantiates it *explicitly* to sidestep autoplug, which won't reliably pick it.
- **Comments must use yt-dlp's default web client** — `player_client=android` returns zero comments
  (`youfish.py:5798`). Don't reintroduce it.
- **The token-free primary dump is anonymous** (no cookies); YouTube gates authenticated token-free
  requests but not anonymous ones. Only the fallback re-runs with cookies.
- **Download video ids are sanitised** to `[\w-]{≤64}` before hitting the `-o` template to block
  path traversal / output-template injection (`youfish.py:5993`).
- **Video downloads require ffmpeg** — YouTube retired the muxed 22/18 fallback; audio (`m4a`,
  format 140) needs nothing.
- **`rpm/harbour-fintube.spec` requires `git`** (runtime clone of the bgutil PO-token provider),
  and **deliberately does NOT require yt-dlp** — the app fetches/updates it itself. Never add it.
- **Local vs CI SDK mismatch:** `build.sh` defaults to `SailfishOS-5.0.0.62-aarch64` while
  `.github/workflows/build.yml` pins release `5.0.0.43`. Keep in mind when a build reproduces
  differently.
- **`harbour-fintube.pro` link order:** adding `PKGCONFIG` drops the sailfishapp libs, so the
  explicit `LIBS += -lsailfishapp -lmdeclarativecache5` + `-pie -rdynamic` are load-bearing; the
  final binary + Makefile must stay at the source root for `%qmake5_install`.
