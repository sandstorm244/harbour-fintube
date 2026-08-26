# FinTube

A native **YouTube client for Sailfish OS**. Silica/QML UI, a Python backend over
PyOtherSide, stream resolution through a **user-managed `yt-dlp` binary**, and a
custom **C++ GStreamer player** (software *and* hardware decode) for real DASH
playback — the things QtMultimedia can't do on its own.

FinTune, its sibling YouTube **Music** client, shares this engine
(`python/youfish.py` + the `src/` C++ player).

## Features

- **Search** with autocomplete, infinite scroll, and channel search.
- **Playback** via a raw GStreamer dual-source pipeline (separate video + audio
  tracks, muxed via a local proxy):
  - **Software** decode (default) and **hardware** decode (`droidvdec` →
    `droideglsink`, zero-copy) — a Settings toggle.
  - **Property-based format selection** — picks by resolution / fps / codec, never
    by hardcoded itag numbers, so it never goes stale. H.264 + VP9, ≤1080p; AV1 is
    excluded (no decoder on the target hardware).
  - Quality menu, a default-quality ceiling, and graceful codec fallback.
- **PO-token provider** (opt-in) — a sandboxed Deno `bgutil` sidecar that mints the
  per-video Proof-of-Origin token YouTube now demands, pre-warmed at launch.
- **SponsorBlock** auto-skip, **chapters**, and **comments**.
- **Subscriptions** + a subscription feed; **channel** pages.
- **Watch history**, **resume positions**, and a **watched** indicator (≥80% =
  watched) with a played-progress bar on thumbnails.
- **Downloads** (audio or video) for offline viewing.
- **Audio effects** — 10-band EQ, plus a volume boost + soft limiter.
- **Quality-of-life** — keep-display-on while playing, landscape fullscreen that
  rotates both ways (camera-cutout aware), auto audio-only when backgrounded
  (freezes the video decoder, keeps sound), and automatic recovery from stream
  403s and display-blank GL context loss.

## Architecture

| Layer | Where | What |
|---|---|---|
| UI | `qml/` (Silica) | `SearchPage`, `VideoPage`, `ChannelPage`, `HistoryPage`, `SettingsPage`, … |
| Bridge | `qml/Backend.qml` | PyOtherSide — all Python calls run off the UI thread |
| Resolver | `python/youfish.py` | drives `yt-dlp`, picks formats, runs the localhost media proxy + the PO-token sidecar |
| Player | `src/videoplayer.cpp` · `src/hwvideosink.cpp` | C++ GStreamer `VideoPlayer` (a QML type) + the hardware EGLImage sink |

## Prerequisites (on the device)

**Nothing to install by hand.** The app fetches every helper below itself — it just
asks you to confirm each download, then installs it into its own data dir. There are
no packages to hunt down and no RPM dependencies to satisfy first.

- **`yt-dlp`** — *not* bundled or depended-on; the app checks for it at launch and,
  on your confirmation, installs/updates it into its own data dir. Extraction breaks
  every few weeks, so keeping it current matters — the app exposes `yt-dlp -U` (a
  one-tap update). *(Advanced: it will also reuse the SailfishOS\:Chum package or a
  `yt-dlp_linux_aarch64` already on PATH, but you don't need either.)*
- **Deno 2.x** *(optional)* — only for the PO-token provider (Settings → *Set up
  provider*, one confirm-to-install step). Without a token, some videos return "no
  playable format".
- **ffmpeg** *(optional)* — only for downloads; the app fetches a static build on
  confirmation.

## Staying current (no app rebuilds)

The design goal: when YouTube changes something, you update a *helper*, not the app.
Every moving part lives outside the binary and updates from Settings:

- **yt-dlp** — the extraction engine (the part YouTube breaks most). *Update* runs its
  own `-U`; that alone fixes the vast majority of breakages.
- **PO-token provider** — the version is no longer baked in. *Update to latest* resolves
  the newest bgutil release from GitHub and re-installs it. (It's a deliberate tap, not
  automatic, so the sidecar stays in step with the yt-dlp plugin it talks to.)
- **ffmpeg** — *Update* re-fetches the current static build.

So a YouTube-side change is a tap, not a new release on your side.

## Build

With the Sailfish SDK (`sfdk`) configured. **Shadow build (recommended)** keeps this tree
pristine — every intermediate and the RPM land in a sibling `harbour-fintube.build/`:

```sh
sh build.sh                      # → ../harbour-fintube.build/RPMS/harbour-fintube-<ver>.aarch64.rpm
# override the target:  TARGET=SailfishOS-5.1.0.11-aarch64 sh build.sh
```

Or the classic **in-source build** (scatters qmake output into this dir — `sh clean.sh`
tidies it, and the `.pro` corrals the `.o`/`moc_*` into `.build/`):

```sh
sfdk -c target=SailfishOS-5.1.0.11-aarch64.default build   # → RPMS/harbour-fintube-<ver>.aarch64.rpm
```

Install on the connected device:

```sh
rpm -U --force <path-to>/harbour-fintube-<ver>.aarch64.rpm
```

## Tests

Pure, offline unit tests for the resolve / format-selection layer (no device,
network, or yt-dlp needed — `resolve()`'s externals are mocked):

```sh
python3 python/test_youfish.py
```

## License

GNU Public License Version 3

## Notice

This appplication has been vibecoded, if you dont like that feel free to not install the application.
