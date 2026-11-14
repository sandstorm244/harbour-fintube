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
| Engine | `python/youfish.py` (7.2k lines)             | yt-dlp, format selection, media proxy, PO-token sidecar, all data stores. |
|        | `python/innertube.py` (1.1k lines)           | Direct InnerTube client: the fast path in front of yt-dlp. |
| Player | `src/videoplayer.cpp`, `src/hwvideosink.cpp` | C++ GStreamer `VideoPlayer` (a QML type) + hardware EGLImage sink. |

Cookie login lives in `python/ytm.py` (a slim sibling of FinTune's music `ytm.py`; here its
whole public API is `netscape_cookies()`). `youfish.py` is **shared** with the FinTune music
app — many test paths are guard-aware so the same engine works in both.

`innertube.py` is shared with FinTune too, but **byte-identical**: copy it across, never fork
it. It talks to YouTube's own API directly (pooled keep-alive HTTPS, no subprocess):
WEB for browse/search/next, the JS-less `VISIONOS` client (from yt-dlp's
`_DEFAULT_JSLESS_CLIENTS`) for `/player`, `ANDROID_VR` for live HLS. youfish reaches it only
through `_it()`, which returns `None` when the module is missing, so every caller works without it.

**Naming quirk:** the product/QML type is `FinTube`, but the engine is branded **"youfish"**
internally — env vars `YOUFISH_DEBUG` / `YOUFISH_HWDEC`, the `[youfish]` log prefix, the
`youfish-player` pipeline name. Same thing.

## Build, run, test

```sh
sh build.sh          # shadow build via sfdk → ../harbour-fintube.build/RPMS/
                     # override target: TARGET=SailfishOS-5.1.0.11-aarch64 sh build.sh
sh clean.sh          # remove build scatter from the source tree
sh test.sh           # every offline test: engine, parsers, coverage check, QML logic
python3 python/test_youfish.py   # offline engine tests — pure stdlib, externals mocked
python3 tests/test_parsers.py    # innertube parsers vs recorded YouTube answers (--live/--record)
python3 tests/coverage_report.py # engine functions no test runs (--check in CI)
python3 tests/live_ytdlp.py      # real yt-dlp + live YouTube, yt-dlp paths only (--pot)
node tests/qml_logic_test.js     # qml/logic.js — the pages' pure decisions
```

- **Parser tests run on recorded answers.** `tests/fixtures/*.json.gz` are real YouTube answers
  (public IP scrubbed to 203.0.113.1); the checks assert what the app needs, not exact values.
  When a parser drifts, `--live` shows which; fix it, then `--record`. A parser change gets a
  `--live` run before it ships.
- **Coverage ratchet.** `tests/coverage_report.py --check` fails on any engine function no test
  runs, unless `tests/coverage_baseline.txt` lists it with the reason it can't run offline.
  New code needs a test; the baseline only shrinks (`--update` after adding tests).
- **`TESTING.md`** is the release procedure (beta tag `1.x.y-betaN` → pre-release → device
  checklist → final tag) and the per-feature device checklist. A new or changed feature gets a
  checklist line there and, where it has logic, a test.
- **Page decisions live in `qml/logic.js`** (`.pragma library`, ES5 only, no QML types, no qsTr):
  caption choice and what gets remembered, SponsorBlock skip, seek stepping, the dual/muxed/HLS
  play plan, queue advance, the display hold (`keepAwake`: only while a video visibly plays; not a
  setting), error recovery, reattach/reclaim of the shared player, link routing,
  feed change detection and Shorts removal, search dedup and paging, the channel→playlists
  redirect (engine `reason: "no_videos_tab"`), the quality/SponsorBlock/equalizer settings maps,
  the PO-token status line, and every number/time formatter (`fmtDur`, `fmtClock`, `fmtCount`,
  `fmtRate`; pages keep one-line wrappers). Pages apply the result and keep the qsTr text. Anything
  *stored* is decided in the engine instead (`set_audio_track`).
  The node harness rejects ES6 syntax Qt 5.6 can't parse. Put new page logic there, with a test.

- The **engine test suite** (`python/test_youfish.py`, ~4.5k lines) is the main safety net.
  It is fully offline (no device, network, or yt-dlp) and runs in CI on every push/PR
  (`.github/workflows/tests.yml`, together with the QML logic suite). It had silently rotted to 8
  failures before CI was added —
  keep it green. **Run it after any change to `youfish.py`.**
- The **release build** (`.github/workflows/build.yml`) only fires on tags matching `1.*`, runs
  both test suites first, cross-builds aarch64 + armv7hl and publishes a GitHub release. A tag
  with a dash (`1.6.0-beta1`) is published as a pre-release.
- **`tests/live_ytdlp.py`** is the only test that runs the real yt-dlp (binary and in-process,
  InnerTube off, `--pot` adds Deno + the PO-token provider). Bot walls and rate limits are
  skips, not failures. Weekly + on demand in `.github/workflows/live.yml` (arm64 runner, so
  the phone's builds); not in `test.sh` or the release gate. Run it from home before a release.
- The C++ player and ffmpeg (video downloads) are **not** covered by any automated test, and
  the real yt-dlp/Deno/PO-token processes only by that live check — verify those on-device. The
  InnerTube tests run on fixtures; the module-level
  `youfish._it = lambda: None` keeps every older test on the yt-dlp path, and
  `InnertubeEngineWiring` swaps in `_FakeIT`. Any change to the live request shape needs a
  network check on a PC and then on the device.

## The invariants (do not break these)

1. **The localhost media proxy is not optional.** GStreamer's libsoup stack gets an unfixable
   403 from googlevideo; urllib with identical headers gets 206. So every DASH + progressive
   stream round-trips through an in-process `127.0.0.1` HTTP server (`_MediaProxyHandler`,
   `youfish.py:1029`) that re-fetches with the format's own User-Agent and serves backpressured
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
   (`_ensure_pot_server`, `youfish.py:3129`). Fork it from any short-lived caller (an install
   thread, a reader-thread re-resolve) and the kernel SIGKILLs it the instant that caller returns.

5. **The stream registry has a strict lock order:** `_streams_lock` → `s.cond`. `do_GET` takes
   only `s.cond`, never `_streams_lock`; no `proc.wait()` ever runs under a lock (killed children
   are reaped off-lock). Preserve this in any proxy/stream change.

6. **All resolve inputs that affect output are in the cache key.** `_resolve_key`
   (`youfish.py:1646`) = video id + effective client + pot_active + default_quality + audio_lang +
   hw_decode + signed_in. Changing any output-affecting setting (or login) must call
   `invalidate_resolve_cache()`.

7. **One shared C++ player at app scope.** `gplayer` lives in `harbour-fintube.qml`, not in
   VideoPage, so audio survives navigation. A VideoPage only *borrows* it by reparenting it into
   its `videoSurface`. The readonly `holdsPlayer` (`gplayer.parent === videoSurface`) is the master
   ownership guard — SponsorBlock skips, error recovery, autoplay, position saves, and teardown are
   all gated on it so a back-stack page can never touch the current owner's playback.

8. **Three PyOtherSide workers, one interpreter.** `py` handles the rest. `pyFast`
   (`Backend.qml:887`) carries latency-critical calls: resolve, prefetch, parseUrl, get_position,
   suggestions, SponsorBlock and caption cues. `pyBg` (`Backend.qml:902`) carries the slow chores:
   the feed refresh, `feed_durations` and the comment walk. Nothing that can fall back to a
   multi-second yt-dlp spawn goes on `pyFast` except resolve itself. Both extra lanes are declared
   **after** `py` on purpose so `pyotherside.send` events still land on the one `onReceived`. At
   startup `prewarm` is the first `py` call. It only starts threads, so it must not queue behind
   the version checks. It delays the yt-dlp zipapp import by `_YTDLP_WARM_DELAY` (5s): the import
   is ~1.4s of CPU-bound Python on device and starved the launch feed refresh. Anything that
   needs yt-dlp earlier imports it on demand.

9. **qtdemux cannot push-seek.** mp4/m4a branches get an on-disk `downloadbuffer` + `KEY_UNIT`
   seeks; webm/matroska seek exactly with `FLUSH|ACCURATE`. This is why **`videoExt`/`audioExt`
   must be set before the URL properties** — they choose the per-branch seek plan in
   `buildPipeline`. Muxed mode must *clear* the exts. The seek machinery in `videoplayer.cpp`
   (`extIsMp4:19`, seek plan `:534`, `sendSeek:288`, `ASYNC_DONE` handler `:941`,
   `retrySplitSeek:358`) is the hardest part of the C++ layer; its comments are on-device
   measurements — treat them as regression docs.

10. **User data = small JSON stores, atomic writes, stale-while-revalidate.** Everything lives
    under `~/.local/share/harbour-fintube`, written through `_atomic_write_json`
    (`youfish.py:4665`: 0600 temp file → `os.replace`, so a crash can't truncate the live store).
    The feed is the archetype — RSS paints instantly, yt-dlp backfills only *due* + *unclassified*
    entries, durations/avatars cache with immutability or TTL rules. New feature here = a
    `*_path()` helper, a tolerant `_load_*` with a safe default, and `_atomic_write_json` on write.

11. **Every InnerTube fast path falls back to yt-dlp, and never hands the player a URL it hasn't
    checked.** resolve goes `_fast_player_data` first (`youfish.py:1499`: one `/player` POST,
    ~0.2s), and its request shape mirrors yt-dlp's exactly (`innertube._post(..., player=True)`:
    hl=en, UTC, no gl, visitor id in the header only, `contentCheckOk`/`racyCheckOk`). Before the
    data is accepted, the exact video+audio pair resolve will pick is probed (`_fast_urls_ok`)
    with playback's own first request: the same header set (`_gv_request`) and the same Range as
    `_DirectFetch`'s first chunk, read for 2 bytes. A 2-byte Range once passed URLs that the real
    request got 403 on. A refused probe, or an HTTP refusal at byte 0 mid-stream, is a strike; the
    latter's recovery skips the fast client and drops the video's cached resolve. Two strikes in 10
  minutes, or two bot walls, open a
    breaker (`_fast_player_state`) and yt-dlp resolves for the cooldown. A network failure in the
    probe is no verdict. A running livestream goes to `_fast_live_data` (ANDROID_VR muxed HLS
    itags 91–96, unproxied, not probed). Search, related, video_info, channels and the feed's
    classification have the same shape: InnerTube first, the old yt-dlp code untouched behind it.
    googlevideo URLs are IP-bound, so `_it()` runs `_force_ipv4()` before the module can open a
    pooled socket.

12. **Maintenance floor: a YouTube layout change may make the app slower, never broken.** Our
    InnerTube parsers are the one part nobody upstream maintains, so an answer that parses to
    *nothing* is treated as layout drift, not as an empty result. That covers an empty search page,
    an empty Videos tab, feed lengths with no durations and feed rows with no dates. Each raises
    or returns empty, so yt-dlp answers instead. Behind that, every yt-dlp listing call goes
    through `_ytdlp_run` (`youfish.py:2409`). It runs the binary's exact argv in-process on the
    zipapp via `yt_dlp.parse_options`, skipping the ~1.1s spawn, and falls back to the binary on
    any failure. So the order is InnerTube → yt-dlp in-process → yt-dlp binary. Structural
    fallbacks are counted per area (`_it_note`) and show in the provider diagnostics report as
    "InnerTube fast path … fell back to yt-dlp N×". Repeated ones mean `innertube.py` needs an
    update; until then the app keeps working. Don't add an InnerTube path that can return a
    plausible-looking empty answer without falling back.

## Where things live

**Engine (`python/youfish.py`)** — the map, by line:
- Resolve core: `resolve:4029`, `_resolve_uncached:4036`, `_resolve_and_cache:1688` (single-flight
  leader/joiner), `prefetch_resolve:1753` (speculative warm).
- InnerTube fast path: `_it:1346`, `_fast_player_raw:1412`, `_fast_strike:1436`,
  `_probe_status:1450`, `_fast_urls_ok:1464`, `_fast_player_data:1499`, `_fast_live_data:1525`,
  `_gv_request:542`. Client choice: `_token_free_client:4822` / `_default_client:4836`.
- Format selection: `_video_candidates:154`, `_audio_candidates:237`, `_pick_video:4440`,
  `_pick_audio:4459`.
- Mid-stream 403 self-heal: `_reresolve:1270` (fast client first, then yt-dlp), `_reader:705`
  (resumes at the same byte offset, same itag ⇒ byte-identical).
- Media proxy: `_MediaProxyHandler:1029`, `_DirectFetch:554` (in-process urllib streamer that
  replaces the per-playback yt-dlp spawn), `_ensure_proxy:1172`, `_proxied:1200`,
  `_proxy_url_ok:454` (SSRF allow-list).
- yt-dlp listing calls: `_ytdlp_run:2409` / `_inproc_json:2428` (in-process first, binary
  fallback). InnerTube drift counters: `_it_note:1365`, `_it_fallback_report:1377`.
- yt-dlp management: `_ytdlp_path:1836`, `ytdlp_version:1852` (cached per binary stat in
  `ytdlp_version.json`), `install_ytdlp:1966`, `ytdlp_update:1887`, zipapp at
  `install_ytdlp_zipapp:2077` / `_import_yt_dlp:2207`, in-process dump `_inproc_dump:2392`.
- PO-token sidecar: `_ensure_pot_server:3129`, `_pot_server_flags:2915` (default-deny Deno
  sandbox), `install_pot_provider:3460`, `prewarm:3249`.
- Helpers: `install_deno:2785`, `install_ffmpeg:2561` (SHA-256-pinned to a frozen JVS build).
- Search/discovery: `search:3759` (InnerTube first: `_search_innertube:3724`, continuation pages
  cached in `_search_pages`), `_search_filter_sp:3604` (hand-rolled protobuf `sp=`),
  `related:3889` and `video_info:4369` (both via the cached `_watch_next:3848`;
  `_video_info_innertube:4329`), `search_suggestions:3945`, `parse_youtube_url:3622`.
- Subs + feed: `subscription_feed:6454`, `feed_durations:6595`, feed cache `6374–6453`,
  per-channel fetch `_feed_fetch_channel:6244` (RSS → InnerTube → yt-dlp),
  `channel_videos:6132` (`_it_channel:5990`), `channel_avatar:6047`.
- Watch state (**two separate stores**): resume points `positions.json`
  (`get_position:4994`/`set_position:5003`), watch history `watch_history.json`
  (`record_watch:5109`, `set_watched:5154`, `watch_history:5065`).
- Playlists: `_load_playlists:7167`…`refresh_playlist:7304`. Downloads: `download:7043`.
  SponsorBlock: `sponsor_segments:5209` (10-min cache). Captions: `_caption_tracks:3990`,
  `caption_cues:5290`. Comments: `comments:6839`, InnerTube first (`_comments_innertube:6771`:
  sequential top-level pages, the first reply page of each thread in parallel, same budgets), then
  the yt-dlp walk. Imports: `import_youtube_account:5539`,
  `import_newpipe:5640`.

**InnerTube (`python/innertube.py`)** — transport `request` (the keep-alive pool, public: the feed's
RSS `_rss_get` and FinTune's `ytm._innertube` use it too) / `_transport` / `_post`, client config
`configure_from_ytdlp:219` (adopts yt-dlp's WEB version, JS-less player client and android_vr
when the zipapp is imported), visitor ids `_mint_visitor:306`, `player_info:824`,
`live_info:943`, `channel_tab:581`, `search:1035`, `comments:1105`, `watch_next:1137`. Comment
bodies live in `frameworkUpdates` entity payloads that the thread items only point at. Parsers read
accessibility labels and text by content, not by field name: YouTube swaps fields (a channel
result's `subscriberCountText` holds the @handle).

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
- **Qt 5.6 platform floor:** no `Qt.callLater` (use a zero-interval Timer restart);
  `Connections.enabled`
  needs 5.7 (disable by setting `target: null`); `Screen.hasCutouts`/cutout APIs are typeof-guarded.
- **`droidvdec` is demoted to rank NONE in `main()`** yet HW mode still uses it — `buildPipeline`
  instantiates it *explicitly* to sidestep autoplug, which won't reliably pick it.
- **Comments must use yt-dlp's default web client** — `player_client=android` returns zero comments
  (`youfish.py:6882`). Don't reintroduce it.
- **The token-free primary dump is anonymous** (no cookies); YouTube gates authenticated token-free
  requests but not anonymous ones. Only the fallback re-runs with cookies.
- **`tv_embedded` is gone.** yt-dlp 2026.01.31 removed it, and later versions skip the name
  silently. The token-free client is whatever `innertube.player_client()` reports
  (`_token_free_client`), which follows yt-dlp's JS-less default. Don't hardcode a client name.
- **A signed-in yt-dlp run must not name the token-free client.** yt-dlp drops every client
  without cookie support (visionos, android, ios) and fails with none left, so the download and
  the cookie'd re-resolve pass `_yt_extractor_args(cookies=True)`, which leaves the client to
  yt-dlp's signed-in default unless the user picked one.
- **Resolve errors pass through `_ytdlp_err`**, which keeps the last `ERROR:` line minus the
  extractor prefix and yt-dlp's desktop advice (`--cookies-from-browser`, FAQ links). Errors that
  match `_FINAL_ERR` (removed, terminated, not yet live, geo-blocked, private while signed out) skip
  the binary re-run and the widen retry: no client changes YouTube's answer.
- **A fresh PO-token URL is refused for its first ~3 s.** googlevideo answers 403 to a `pot=`
  URL until ~2.6–3.1 s after extraction, then serves it (measured 2026-10-22, bgutil 1.3.2 and
  2.0.2). `_DirectFetch._warming_up` retries such a URL's first request for `_POT_WARMUP` (6 s)
  instead of dying into a re-resolve, whose new URL would be just as young. Token-free, older
  or already-streaming URLs never wait. `tests/live_ytdlp.py --pot` is the check.
- **A just-ended livestream (`live_status == "post_live"`) is refused** with a "still processing"
  message. Until YouTube finishes the recording, its formats are DASH segment lists that the proxy
  can't serve as one file.
- **32-bit ARM userland (`_ARM32`)** is detected from the interpreter's pointer size, never
  uname: armv7hl images often run on a 64-bit kernel that reports `aarch64`. There yt-dlp is
  the stable-only `yt-dlp_linux_armv7l.zip` one-directory build, unpacked to
  `bin/yt-dlp-armv7l/` behind a `bin/yt-dlp` symlink (`_unpack_ytdlp_dir`); it can't self-update,
  so `ytdlp_update` re-downloads when the latest tag differs (`_ytdlp_update_by_download`).
  `_ytdlp_channel()` pins stable (keeps the zipapp in lockstep). Deno/PO token are refused
  (`_ARM32_NO_DENO`; `pot_status().arm32` hides the setup in the UI); ffmpeg is the pinned armhf
  JVS build. Verified only under the SDK armv7hl target (`sfdk engine exec sb2 -t
  SailfishOS-5.1.0.11-armv7hl.default python3 …`), never on hardware. CI builds aarch64 + armv7hl;
  qreal is float on armv7hl, so write `qreal(1.0)` in qFuzzyCompare.
- **Every user-visible QML string goes through `qsTr()`** (`CONFIG += sailfishapp_i18n`; the build's
  lupdate refreshes `translations/*.ts`). Use `.arg()` for values, never `+` concatenation; `%n` for
  counts (`qsTr("%n video(s)", "", n)`) — `translations/harbour-fintube-en.ts` supplies the English
  plural forms and `main()` installs it as the fallback under the device language. Never wrap
  setting keys/values, `py.call` names, or anything compared with `===`: use a flag instead of
  testing translated text (`ProvidersPage.diagRunning`). Python-sourced messages stay English.
- **Qt sets the process locale from the device**, so `strptime("%b")` fails on non-English phones.
  Month names are parsed by hand (`innertube._upload_date`).
- **Debug lines worth grepping** (`YOUFISH_DEBUG=1`, `[youfish/t …]`): `fast player`, `fast live`,
  `fast path strike`, `fast player off`, `resolve picks <id> [client=innertube:…]`, `resolve urls:`,
  `direct-fetch itag=… died after N bytes`, `search innertube`, `watch_next`, `video_info
  innertube`,
  `comments innertube`, `yt-dlp in-process`. Always printed (QML): `FinTube: display hold on/off`.
- **Download video ids are sanitised** to `[\w-]{≤64}` before hitting the `-o` template to block
  path traversal / output-template injection (`youfish.py:7077`).
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
