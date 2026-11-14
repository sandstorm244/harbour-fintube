# Testing FinTube

Two layers catch different bugs:

- **Automated tests** run in seconds on a PC and in CI. They cover the engine (`python/`), its
  parsers against real recorded YouTube answers, and the decisions the QML pages make
  (`qml/logic.js`).
- **The device checklist** below covers what only the phone can show: the C++ player, real
  YouTube, the Silica UI, and the yt-dlp/Deno/ffmpeg helpers.

A feature belongs in both. When you add or change one, add a test for its logic and a line to
the checklist.

## Automated tests

```sh
sh test.sh                          # everything; prints ALL TESTS PASSED or what failed
python3 python/test_youfish.py      # engine only, verbose
python3 tests/test_parsers.py       # InnerTube parsers vs recorded YouTube answers
python3 tests/coverage_report.py    # which engine code no test runs
python3 tests/live_ytdlp.py         # real yt-dlp against live YouTube (--pot: with PO token)
node tests/qml_logic_test.js        # QML decisions only
```

- **Engine suite** (`python/test_youfish.py`): pure standard library and fully offline. yt-dlp,
  the network and YouTube are mocked, and the localhost media proxy runs for real against a fake
  googlevideo.
- **QML logic suite** (`tests/qml_logic_test.js`): every decision the pages make: captions,
  SponsorBlock, seeking, the play plan (dual / muxed / HLS), queue advance, error recovery,
  reattaching the player, link routing, feed and search paging, the settings maps, the PO-token
  status line and number formatting. It needs Node.js (Fedora:
  `sudo dnf install nodejs`). It also rejects syntax the phone's Qt 5.6 JavaScript can't run
  (`let`, `=>`, template strings and so on), which Node would accept.
- **Parser suites** (`tests/test_parsers.py`): the parsers for YouTube's answers, run against real
  answers recorded from YouTube (`tests/fixtures/`, your IP scrubbed out). They check what the app
  needs (ids, titles, durations, a next page), not exact values.
- **Coverage check** (`tests/coverage_report.py --check`): fails when an engine function has no
  test. `tests/coverage_baseline.txt` lists the few that can't run offline, each with the reason;
  that list may only shrink. Run the script without `--check` to see how much of each module the
  tests run.
- **Live yt-dlp check** (`tests/live_ytdlp.py`): the one test that uses the real yt-dlp and live
  YouTube. It downloads yt-dlp (binary and in-process copy, checks they match) and runs the
  engine's yt-dlp paths with InnerTube switched off: resolve and the first bytes of both streams
  through the proxy, search, related, channels, playlists, comments and an audio download, on both
  forms of yt-dlp. `--pot` also installs Deno and the PO-token provider and checks the token path.
  YouTube's "confirm you're not a bot" and rate limits count as **skipped (inconclusive)**, never
  as failures. Not part of `test.sh`: it needs the network and takes about a minute. GitHub runs
  it every Monday and on demand (Actions → live yt-dlp → Run workflow); its IPs often get the bot
  wall, so a run with skips proves less than one from home.
- **CI**: every offline suite runs on every push and pull request. A release build runs them
  first and doesn't publish if one fails.

**When YouTube changes something.** If diagnostics show the "InnerTube fast path … fell back" count
growing, or users report empty pages, run `python3 tests/test_parsers.py --live`. The failing check
names the parser that no longer understands YouTube's answer. Fix it, then re-record with `--record`
so the offline tests follow the new layout.

**Keep decisions testable.** If a page decides something (which track, which segment, what to
remember), put the decision in `qml/logic.js` as a plain function, or in the engine if it's
stored. The page only applies the result. The "Original audio" bug in 1.5 was a one-line
decision inside `VideoPage.qml` that no test could reach.

## Release procedure

1. `sh test.sh` passes, and so does `python3 tests/live_ytdlp.py --pot` from your own
   connection (it checks the newest yt-dlp still works with the app).
2. Set `Version:` in `rpm/harbour-fintube.spec` (the RPM takes its version from there, not
   from the tag), then tag a **beta**: `git tag 1.6.0-beta1 && git push origin 1.6.0-beta1`.
   A tag with a dash is published as a pre-release, so only people who pick it get it.
3. Install the beta RPM on the phone. Run **Smoke** plus every section the release touched (see
   the table below). If in doubt, run them all; the full list takes about 30 minutes.
4. Fix, then tag `-beta2` and repeat as needed. When clean, tag the release (`1.6.0`).

| Files changed | Run these sections |
|---|---|
| anything | Smoke |
| `python/youfish.py` resolve, format, proxy, `innertube.py` | Playback, Audio and captions, Providers |
| `qml/pages/VideoPage.qml`, `TransportControls.qml`, `qml/logic.js` | Playback, Audio and captions, SponsorBlock, Background and system |
| `src/*.cpp` | Playback, Background and system, Audio effects |
| feed, channel, search code | Home and feed, Search, Channels |
| `ytm.py`, login, account code | Account |
| downloads code | Downloads |
| `rpm/`, `.pro`, `.desktop`, translations | Install and launch, Smoke |

## Device checklist

"Auto:" names the automated tests behind a line. There, the device step only checks that the
result reaches the screen. Lines without it are only checked here.

### Smoke (every release, about 5 minutes)

- [ ] Fresh install over the previous version; the app opens to the feed with no error.
- [ ] Play a normal video: picture and sound within a few seconds, seek forward and back works.
- [ ] Search finds something; a result opens and plays.
- [ ] Leave a playing video: audio continues and the resume bar on Home controls it.
- [ ] Providers page: yt-dlp shows a version, "Run diagnostics" finishes.

### Install and launch

- [ ] Upgrade keeps subscriptions, history, playlists, settings and the yt-dlp install.
- [ ] Phone in another language: the UI is translated and untranslated strings show in English.
- [ ] Opening a YouTube link from the browser or another app opens the video, channel or playlist.
      Try this with the app closed and with it running. (Auto: `ParseYoutubeUrl`, `routeFor`,
      `dbusUrlArg`.)
- [ ] First run with no yt-dlp: Home offers the install; after it, videos play.

### Playback

- [ ] 720p default plays; switching quality keeps the position. The Default quality setting
      shows the value in use. (Auto: `VideoCandidates`, `ResolveSmoke`, `qualityIndex`.)
- [ ] 1080p and 60 fps variants play when offered.
- [ ] Hardware decode on: plays smoothly, no green or black frames; off again works.
- [ ] Seek by slider and by double-tap ∓10 s; taps stack; switching sides counts again.
      (Auto: `seekStep`.)
- [ ] A video with only a muxed stream plays and seeks. (Auto: `playPlan`.)
- [ ] A running livestream plays; a just-ended one shows the "still processing" message.
- [ ] Resume: reopen a half-watched video and it continues where it stopped. (Auto: `ResumeIdleTTL`,
      `wholeSeconds`, `finishedSeconds`.)
- [ ] Watch for 2+ minutes with no stall. If a stall happens, it recovers by itself and resumes
      at the same spot. (Auto: `DirectFetchStreamer`, `ReresolveAnonFirst`, `MediaProxyEndToEnd`,
      `recoveryPlan`, `recovered`.)
- [ ] A playlist's "Play all" advances to the next video at the end; the next one starts fast.
      (Auto: `nextQueueIndex`.)
- [ ] Speed menu: change speed, open another video, the speed is kept. (Auto: `fmtRate`.)
- [ ] Fullscreen in landscape; with "portrait fullscreen" on, also in portrait.
- [ ] Signed in: an age-restricted or members video you have access to plays.

### Audio and captions

- [ ] Dubbed video: the Audio menu lists the languages with the original marked; switching keeps
      the position.
- [ ] Pick a dub, open another dubbed video: it starts on that dub. Pick the **original** of a
      non-English video, then open an English video: it plays in English.
      (Auto: `AudioTrackMemory`, `PickAudioPreferredLang`.)
- [ ] Captions: choose a language, and it shows in sync after a seek. The next video turns on the
      same language by itself, using an auto-translation if needed. (Auto: `pickCaption`,
      `captionAt`, `CaptionTracks`, `ParseJson3`.)
- [ ] Captions Off is remembered. The translation search filters by name or code.
      (Auto: `captionPref`, `filterTranslations`.)

### SponsorBlock

- [ ] A video with a sponsor segment skips it while playing and shows "Skipped sponsor".
- [ ] Paused inside a segment: it doesn't jump.
- [ ] A category set to "Show skip button" shows the button; tapping skips.
- [ ] A category set to Off is ignored; each category's setting sticks.
      (Auto: `sponsorDecision`, `sbActionIndex`.)

### Background and system

- [ ] Background audio on: leave the video page and audio continues. Cover and lockscreen
      play/pause work, and so do headset buttons (MPRIS). (Auto: `keepPlayerOnLeave`.)
- [ ] Background audio off: leaving stops playback.
- [ ] Paused video, go back, reopen it: it reattaches at the same spot without reloading.
      (Auto: `canReattach`.)
- [ ] Open video A, then from it video B, then go back to A: A takes the player back and B stops.
      (Auto: `needsReclaim`.)
- [ ] Screen off during playback and back on: no frozen or corrupted picture.
- [ ] A playing video keeps the screen on. Paused, ended, or the app in the background: the
      screen dims and blanks on the system's own timeout. Same for a downloaded video; downloaded
      audio never holds it. (Auto: `keepAwake`, `awakeStalled`.)
- [ ] Closing the app stops all audio and the PO-token helper.

### Audio effects

- [ ] Equalizer on with a preset changes the sound; picking a preset turns it on; off restores it.
      (Auto: `eqPreset`.)
- [ ] Volume boost is louder without harsh clipping.

### Home and feed

- [ ] The feed paints instantly from cache, then refreshes without jumping the scroll.
      (Auto: `SubscriptionFeed`, `feedChanged`.)
- [ ] Durations appear; with "Hide Shorts" on no Shorts show. (Auto: `FeedShortsAndDurations`,
      `feedBackfill`, `fmtDur`.)
- [ ] "Hide watched" hides watched videos. (Auto: `HideWatched`.)
- [ ] Row menu: mark watched/unwatched updates the progress bar. (Auto: `SetWatched`.)
- [ ] Live videos are pinned on top.

### Search

- [ ] Suggestions appear while typing.
- [ ] Results scroll endlessly with no duplicates. (Auto: `dedupPage`, `searchHasMore`.)
- [ ] Filters (sort, date, length) change the results. (Auto: `SearchFilterSp`, `SearchFiltersRouting`.)
- [ ] A channel result opens the channel.

### Channels and subscriptions

- [ ] Channel page shows its videos and loads more on scroll; Playlists opens from the pull-down.
- [ ] Subscribe and unsubscribe; the feed follows on the next refresh. (Auto: `SubscriptionsStore`.)
- [ ] A channel with no Videos tab opens its playlists instead. (Auto: `ChannelNoVideosTab`,
      `channelFirstPage`.)

### Video page extras

- [ ] Comments load by themselves a few seconds in; "show replies" and "show more" work.
      (Auto: `CommentsThreads`, `InnertubeEngineWiring`.)
- [ ] Chapters show the current chapter name. (Auto: `chapterAt`.)
- [ ] Related videos page opens and plays. (Auto: `Related`.)
- [ ] Share, copy link, open in browser.
- [ ] "Video details" from a row menu shows info without playing.

### History and playlists

- [ ] History lists newest first, pages in on scroll, and Clear empties it. (Auto: `WatchHistoryPages`,
      `fullPage`.)
- [ ] Local playlist: create, rename, add from a row menu, remove, delete. (Auto: `LocalPlaylists`.)
- [ ] Save a YouTube playlist by link; it fills in on first open; refresh it.
      (Auto: `shouldAutoRefreshPlaylist`.)

### Downloads

- [ ] Download audio: progress shows, the file plays offline from Downloads.
- [ ] Download video with ffmpeg installed: merged file plays. Without ffmpeg, a clear message.
      (Auto: `DownloadNeedsFfmpeg`.)
- [ ] Delete a download removes the file. (Auto: `DownloadFolder`.)
- [ ] Pick a custom download folder; reset it. An unwritable folder is refused. (Auto: `DownloadFolder`.)

### Account

- [ ] Import login from the browser; the account and channel picker shows your channels.
      (Auto: `YtmCookieModule`, `YtmInnerTube`, `AccountSelection`.)
- [ ] Import subscriptions and playlists from YouTube. (Auto: `ImportYoutubeAccount`, `resultMessage`.)
- [ ] Import a NewPipe backup. (Auto: `NewPipeImport`.)
- [ ] Sign out; videos still play.

### Providers

- [ ] yt-dlp Update on the stable channel, then switch to nightly and update. The version-skew
      warning stays hidden. (Auto: `ChannelAwareDownloads`, `YtdlpVersionCache`.)
- [ ] Fast resolve shows active on SFOS 5.1+ (Python 3.10+).
- [ ] Install Deno and set up the PO-token provider; the status turns ready and videos still play.
      (Auto: `potStatus`, for the status line.)
- [ ] Install ffmpeg (pinned build passes its checksum).
- [ ] Player client choice: pick one, a video plays; back to Auto.
- [ ] Diagnostics report copies to the clipboard. An "InnerTube fast path … fell back" count
      that keeps growing means `innertube.py` needs an update.

### 32-bit phones (armv7hl)

There is no test phone. Check in the SDK's armv7hl target instead:
`sfdk engine exec sb2 -t SailfishOS-5.1.0.11-armv7hl.default python3 …`

- The release has an armv7hl RPM.
- yt-dlp installs and updates from the 32-bit build. (Auto: `Arm32Userland`.)
- Deno and the PO-token setup are hidden. (Auto: `Arm32Userland`.)
- Playback works before yt-dlp is installed, or when it can't run: delete `bin/yt-dlp` under
  the app's data folder and play something. A refused stream names its reason in the log
  ("no stream capacity: …"). (Auto: `MediaProxyEndToEnd`.)
