import QtQuick 2.0
import Sailfish.Silica 1.0
import QtMultimedia 5.6
import Sailfish.Share 1.0
import FinTube 1.0

Page {
    id: page
    allowedOrientations: Orientation.All

    property string videoId: ""
    readonly property string shareUrl: "https://www.youtube.com/watch?v=" + page.videoId
    property string title: ""
    property bool fromQueue: false    // launched from a playlist → auto-advance to the next on end
    property string channel: ""
    property string channelId: ""
    property string channelUrl: ""
    property string channelAvatar: ""
    property string description: ""
    property bool descExpanded: false
    property real videoDuration: 0    // seconds, from yt-dlp — scrubber fallback
    property var qualities: []        // [{itag,label,video_url}], highest first
    property string currentQuality: ""
    property int pendingSeekMs: -1    // position to restore after a quality switch
    property int resumeMs: 0          // saved watch position to resume from (0 = none)
    property var sponsorSegments: []  // SponsorBlock [{start,end,category}] in seconds
    property string skipHint: ""      // transient "Skipped …" overlay text

    // Captions. captionCues = [{start,dur,text}] for the active track; currentCaption = the line
    // showing right now, recomputed from positionMs on each tick (same loop SponsorBlock rides).
    property var captionCues: []
    property string captionLang: ""   // active track lang for THIS video ("" = off)
    property string currentCaption: ""
    property int captionIndex: 0      // cached scan cursor into captionCues
    property real captionLastT: -1    // last sampled time (s); a drop signals a backward seek

    // Double-tap-to-seek: tap a side twice to jump ∓10s, keep tapping to stack.
    property bool seekActive: false
    property string seekZone: ""      // "left" | "right"
    property int seekAmount: 0        // signed seconds accumulated this gesture
    property int seekTargetMs: 0
    property string tapZone: ""       // side of a pending (undecided single/double) tap

    // Download status (this video)
    property bool downloading: false
    property real downloadPct: 0
    property string downloadKind: ""
    property string downloadHint: ""  // transient "Downloaded"/"Failed" text

    property real playbackRate: app.backend.playbackRate || 1.0   // remembered across videos
    property var speedSteps: [1.0, 1.25, 1.5, 2.0, 0.75]
    property var chapters: []         // [{start,title}] in seconds, from yt-dlp
    property string currentChapter: {
        if (page.chapters.length === 0) return ""
        var pos = page.positionMs / 1000
        var t = ""
        for (var i = 0; i < page.chapters.length; i++) {
            if (pos >= page.chapters[i].start) t = page.chapters[i].title
            else break
        }
        return t
    }

    // Comments: fetched once on demand (tap to load), then revealed a few at a time as
    // the info area scrolls.
    property var comments: []
    property bool commentsLoading: false
    property bool commentsLoaded: false
    property string commentsError: ""
    property int commentsShown: 5     // client-side page size; grows on scroll
    property int commentsTotal: 0     // YouTube's real total (for the header)

    // Re-evaluates whenever the saved-channel list changes, so the button stays in sync.
    property bool subscribed: {
        var subs = app.backend.subscriptions
        for (var i = 0; i < subs.length; i++)
            if (subs[i].id === page.channelId)
                return true
        return false
    }

    property bool resolving: true
    property string errorText: ""
    property bool useGst: false       // true = our GStreamer player, false = MediaPlayer (HLS)
    property bool isLive: false        // a running livestream (drives the LIVE badge)

    // Auto-recovery from a mid-playback stream failure (usually a googlevideo 403 the proxy
    // couldn't refresh in place). Bounded so a genuinely dead video surfaces the error.
    property int playRetries: 0
    readonly property int maxPlayRetries: 3
    property int recoverAtMs: 0       // once we've played past this, the stream is healthy again
    property bool pendingRecovery: false  // a stall happened while backgrounded → reload on resume

    // Landscape is immersive fullscreen: the video fills the screen and the controls
    // become a tap-to-show overlay.
    property bool landscape: page.orientation === Orientation.Landscape
                             || page.orientation === Orientation.LandscapeInverted
    property bool portraitFull: false   // fill-screen video in portrait (no rotation), if enabled
    property bool controlsShown: true

    // The page draws edge-to-edge under the camera cutout (FullScreen) so landscape is immersive and
    // no app background peeks around the notch. In portrait that would let the notch clip the top of
    // the video strip, so we simply push the video box DOWN by notchOffset (see videoBox) and fill
    // the freed strip with black. Guarded — cutoutMode/CutoutMode only exist on newer Silica.
    function updateCutout() {
        if (typeof page.cutoutMode !== "undefined")
            page.cutoutMode = CutoutMode.FullScreen
    }
    // Portrait-only drop below the physical camera cutout, taken from the OS cutout geometry
    // (Screen.topCutout) so it fits ANY device's notch — 0 where there is none. Guarded:
    // Screen.hasCutouts / Screen.topCutout only exist on newer Silica; typeof avoids a ReferenceError.
    property real notchOffset: (typeof Screen !== "undefined" && Screen.hasCutouts && Screen.topCutout)
                               ? (Screen.topCutout.y + Screen.topCutout.height)
                               : 0

    // One view over whichever backend is actually playing.
    property int positionMs: page.useGst ? gplayer.position : mediaPlayer.position
    property int durationMs: page.useGst
        ? (gplayer.duration > 0 ? gplayer.duration : Math.round(page.videoDuration * 1000))
        : (mediaPlayer.duration > 0 ? mediaPlayer.duration : Math.round(page.videoDuration * 1000))
    property bool isPlaying: page.useGst
                             ? gplayer.playing
                             : (mediaPlayer.playbackState === MediaPlayer.PlayingState)

    function togglePlay() {
        if (page.useGst)
            gplayer.playing ? gplayer.pause() : gplayer.play()
        else
            mediaPlayer.playbackState === MediaPlayer.PlayingState
                ? mediaPlayer.pause() : mediaPlayer.play()
    }
    function seekTo(ms) {
        if (page.useGst)
            gplayer.seek(ms)
        else if (mediaPlayer.seekable)
            mediaPlayer.seek(ms)
    }
    // One 10s step of a double-tap seek in the given zone; stacks while the gesture is live.
    function applySeekStep(zone) {
        var step = (zone === "right") ? 10000 : -10000
        if (!page.seekActive) {
            page.seekActive = true
            page.seekZone = zone
            page.seekAmount = 0
            page.seekTargetMs = page.positionMs
        } else if (page.seekZone !== zone) {
            page.seekZone = zone
            page.seekAmount = 0        // switched direction → fresh count
        }
        var target = Math.max(0, page.seekTargetMs + step)
        if (page.durationMs > 0)
            target = Math.min(page.durationMs, target)
        page.seekTargetMs = target
        page.seekAmount += step / 1000
        seekApplyTimer.restart()      // accumulate; the actual seek fires once tapping settles
        seekHideTimer.restart()
    }
    // Switch video quality: swap the video track (audio unchanged), rebuild, and restore
    // the current position once playback resumes. GStreamer/DASH path only.
    function switchQuality(q) {
        if (!page.useGst || !q || !q.video_url || gplayer.videoUrl === q.video_url)
            return
        page.currentQuality = q.label
        page.pendingSeekMs = page.positionMs
        gplayer.stop()
        gplayer.videoUrl = q.video_url
        gplayer.play()
    }

    // Pick a caption track (or null = Off). `persist` records it as the cross-video preference;
    // the auto-select on load passes false so it doesn't re-write the same value every video.
    function selectCaption(track, persist) {
        page.captionCues = []
        page.currentCaption = ""
        page.captionIndex = 0
        page.captionLastT = -1
        if (!track || !track.url) {
            page.captionLang = ""
            transport.currentCaptionLang = ""
            if (persist) app.backend.setCaptionLang("")
            return
        }
        page.captionLang = track.lang
        transport.currentCaptionLang = track.lang
        if (persist) app.backend.setCaptionLang(track.lang)
        var want = track.lang
        app.backend.captionCues(track.url, function(cues) {
            if (page.captionLang !== want)      // user switched again before this returned
                return
            page.captionCues = cues || []
            page.captionIndex = 0
            page.captionLastT = -1
            page.updateCaption()
        })
    }

    // Base language code — "pt" from "pt-BR", "es" from "es-419", "en" from "en-orig". Used so a
    // remembered language still matches the same language labelled at a different granularity.
    function baseLang(code) {
        return (code || "").split("-")[0].toLowerCase()
    }

    // If a caption language is remembered, auto-enable a matching track for the new video —
    // real tracks first, then the auto-translations. Most people pick one language and keep it.
    function autoSelectCaption(tracks, translations) {
        var want = app.backend.captionLang
        if (!want || want.length === 0)
            return
        var i
        // Exact code match first (real tracks, then translations).
        for (i = 0; i < tracks.length; i++)
            if (tracks[i].lang === want) { page.selectCaption(tracks[i], false); return }
        for (i = 0; i < translations.length; i++)
            if (translations[i].lang === want) { page.selectCaption(translations[i], false); return }
        // Fallback: same base language at a different granularity (manual "pt-BR" preference vs an
        // ASR "pt" track, "en" vs "en-orig", "es-419" vs "es"). The preference is language-level,
        // so a base match still counts — and persist stays false, so the exact pref is untouched.
        var base = page.baseLang(want)
        for (i = 0; i < tracks.length; i++)
            if (page.baseLang(tracks[i].lang) === base) { page.selectCaption(tracks[i], false); return }
        for (i = 0; i < translations.length; i++)
            if (page.baseLang(translations[i].lang) === base) { page.selectCaption(translations[i], false); return }
    }

    // Recompute the on-screen caption for the current position. Cues are time-sorted, so we keep
    // a cursor and only advance it; a backward jump in time resets the scan.
    function updateCaption() {
        var cues = page.captionCues
        if (cues.length === 0) {
            if (page.currentCaption !== "") page.currentCaption = ""
            return
        }
        var t = page.positionMs / 1000
        var i = page.captionIndex
        if (i < 0 || t < page.captionLastT) i = 0        // first run or seeked backward → rescan
        page.captionLastT = t
        while (i < cues.length && t >= cues[i].start + cues[i].dur) i++
        page.captionIndex = i
        var txt = (i < cues.length && t >= cues[i].start) ? cues[i].text : ""
        if (page.currentCaption !== txt) page.currentCaption = txt
    }

    // Start a background download of this video's audio or muxed video.
    function startDownload(kind) {
        page.downloadKind = kind
        page.downloadPct = 0
        page.downloading = true
        page.downloadHint = ""
        app.backend.download(page.videoId, page.title, kind)
    }

    Connections {
        target: app.backend
        onDownloadProgress: {
            if (videoId === page.videoId) {
                page.downloading = true
                page.downloadPct = percent
            }
        }
        onDownloadFinished: {
            if (videoId === page.videoId) {
                page.downloading = false
                page.downloadHint = ok ? "Downloaded"
                                       : ("Download failed" + (message ? ": " + message : ""))
                downloadHintTimer.restart()
            }
        }
    }
    Timer {
        id: downloadHintTimer
        interval: 2500
        onTriggered: page.downloadHint = ""
    }

    // Autoplay queue: when a playlist-launched video ends naturally, roll on to the next one
    // (replace, so Back returns to the playlist, not a stack of finished videos). Only while this
    // page is on top — if the user has navigated elsewhere, let it stop.
    function playNextInQueue() {
        if (page.status !== PageStatus.Active)
            return
        if (page.fromQueue && app.playQueue && app.playQueueIndex + 1 < app.playQueue.length) {
            app.playQueueIndex += 1
            var next = app.playQueue[app.playQueueIndex]
            pageStack.replace(Qt.resolvedUrl("VideoPage.qml"),
                { videoId: next.id, title: next.title || "", fromQueue: true })
        }
    }
    Connections {
        target: gplayer
        onEnded: { page.markFinished(); page.playNextInQueue() }
    }

    // A googlevideo stream can 403 mid-playback (session throttle) faster than the proxy's
    // in-place URL refresh can recover — the failure that makes a manual swipe-out-and-reload
    // fix it. Do that automatically: re-resolve for fresh URLs and reload, resuming where we
    // stalled. Bounded, and the budget resets once playback is healthy again (see below).
    function recoverPlayback(message) {
        transport.qualityMenuOpen = false   // don't let an open menu outlive the reload/re-resolve
        transport.captionMenuOpen = false
        // Backgrounded/locked: the network (and any reload) will just fail again — and the
        // stall was almost certainly caused by the blank itself (WiFi power-save / a frozen
        // fetch). Defer the reload to resume, and don't spend the retry budget on it.
        if (!Qt.application.active) {
            page.pendingRecovery = true
            page.errorText = ""
            return
        }
        if (page.videoId.length === 0 || page.resolving
                || page.playRetries >= page.maxPlayRetries) {
            page.errorText = message
            return
        }
        page.playRetries++
        var atMs = page.positionMs > 0 ? page.positionMs : page.resumeMs
        page.resumeMs = atMs > 0 ? atMs : 0
        page.recoverAtMs = atMs + 5000    // 5s of clean playback past here = recovered
        page.errorText = ""
        page.resolving = true             // re-arm the onResolved handler + show the spinner
        retryTimer.restart()
    }
    Timer {
        id: retryTimer
        interval: 1500                    // brief backoff; a throttle 403 needs a beat to clear
        onTriggered: app.backend.resolve(page.videoId)
    }
    Connections {
        target: mediaPlayer
        onStatusChanged: if (mediaPlayer.status === MediaPlayer.EndOfMedia) {
            page.markFinished(); page.playNextInQueue()
        }
    }

    // Tap-to-load comments. One backend fetch of a capped batch; the section then reveals
    // page.commentsShown of them and grows as the user scrolls (see infoFlick).
    function loadComments() {
        if (page.commentsLoading || page.commentsLoaded)
            return
        page.commentsLoading = true
        page.commentsError = ""
        app.backend.fetchComments(page.videoId, 50, function(res) {
            page.commentsLoading = false
            if (res && res.ok) {
                page.comments = res.comments || []
                page.commentsTotal = res.total || (res.comments ? res.comments.length : 0)
                page.commentsLoaded = true
            } else {
                page.commentsError = (res && res.error) ? res.error : "couldn't load comments"
            }
        })
    }

    // Cycle playback speed through a sensible set (GStreamer path only).
    function cycleSpeed() {
        if (!page.useGst)
            return
        var idx = page.speedSteps.indexOf(page.playbackRate)
        page.playbackRate = page.speedSteps[(idx + 1) % page.speedSteps.length]
        gplayer.rate = page.playbackRate
        app.backend.setPlaybackRate(page.playbackRate)   // remember it for the next video
    }

    // Video-level quick actions (the action bar under the channel line). "share" fires the
    // native SFOS share sheet via ShareAction (see shareLink below).
    function videoAction(act) {
        if (act === "open")
            Qt.openUrlExternally(page.shareUrl)
        else if (act === "copy") {
            Clipboard.text = page.shareUrl
            if (typeof app !== "undefined" && app.showToast) app.showToast("Link copied")
        } else if (act === "share") {
            shareLink.resources = [{ "type": "text/x-url",
                                     "linkTitle": page.title, "status": page.shareUrl }]
            shareLink.trigger()
        }
    }

    // YouTube-style fullscreen: force landscape (and force back to portrait when landscape),
    // so it works without unlocking the device's rotation. LandscapeMask (= Landscape |
    // LandscapeInverted) keeps it landscape-locked but lets the phone flip 180° between the two
    // landscape directions, so "down" can be on either side rather than pinned to one.
    function toggleFullscreen() {
        if (page.portraitFull)
            page.portraitFull = false                              // exit portrait fullscreen
        else if (page.landscape)
            page.allowedOrientations = Orientation.Portrait        // exit landscape immersive
        else if (app.backend.portraitFullscreen)
            page.portraitFull = true                               // fill the screen, stay portrait
        else
            page.allowedOrientations = Orientation.LandscapeMask   // classic: rotate to landscape
    }

    // SponsorBlock: if the play position lands inside a skip segment, jump past it.
    function checkSponsorSkip() {
        if (!app.backend.sponsorBlock || page.sponsorSegments.length === 0 || !page.isPlaying)
            return
        var pos = page.positionMs / 1000
        for (var i = 0; i < page.sponsorSegments.length; i++) {
            var s = page.sponsorSegments[i]
            if (pos >= s.start && pos < s.end - 0.5) {
                page.seekTo(Math.round(s.end * 1000))
                page.showSkipHint(s.category === "selfpromo" ? "Skipped self-promo"
                                  : s.category === "interaction" ? "Skipped reminder"
                                  : "Skipped sponsor")
                return
            }
        }
    }
    function showSkipHint(t) {
        page.skipHint = t
        skipHintTimer.restart()
    }
    // Become the app's now-playing source (drives the cover). Stopping any previous holder
    // first keeps a single pipeline playing at a time.
    function claimNowPlaying() {
        app.nowPlaying.stopRequested()
        app.nowPlaying.title = page.title
        app.nowPlaying.channel = page.channel
        app.nowPlaying.playing = page.isPlaying
        app.nowPlaying.active = true
        nowPlayingConn.target = app.nowPlaying   // listen only after we've claimed
        // Route the equalizer / volume-boost settings to this page's GStreamer player.
        app.activePlayer = gplayer
        app.applyAudioFx()
        // NB: the remembered speed is primed on gplayer before play() (in onResolved) and engaged
        // by the C++ player during preroll — nothing rate-related happens here.
    }

    Connections {
        id: nowPlayingConn
        target: null
        onToggleRequested: page.togglePlay()
        onStopRequested: {
            // A newer video is taking over — stop this one and release the claim.
            page.persistPosition()
            gplayer.stop()
            mediaPlayer.stop()
            nowPlayingConn.target = null
            if (app.activePlayer === gplayer)
                app.activePlayer = null
        }
    }

    // Record playback progress: resume point + watch history (thumbnail bar + WATCHED tag).
    // The too-early / basically-finished zeroing of the resume point now lives in Python
    // (record_watch), which also computes the >=80% watched flag from the raw position.
    function persistPosition() {
        if (!page.videoId)
            return
        var pos = Math.floor(page.positionMs / 1000)
        var dur = Math.floor(page.durationMs / 1000)
        app.backend.saveWatch(page.videoId, pos, dur, page.title, page.channel)
    }

    // Reached the end — mark watched with a full position (position reporting can be flaky at
    // EOS, so pass duration explicitly rather than trusting positionMs).
    function markFinished() {
        if (!page.videoId)
            return
        var dur = Math.floor(page.durationMs / 1000)
        if (dur <= 0)
            dur = Math.floor(page.videoDuration || 0)
        app.backend.saveWatch(page.videoId, dur, dur, page.title, page.channel)
    }

    // 1.2K / 3.4M style abbreviation for like counts.
    function fmtCount(n) {
        if (!n || n < 0) return "0"
        if (n >= 1000000) return (n / 1000000).toFixed(1).replace(".0", "") + "M"
        if (n >= 1000) return (n / 1000).toFixed(1).replace(".0", "") + "K"
        return "" + n
    }

    // After a quality switch the new pipeline needs a beat before it will accept a seek.
    Timer {
        id: resumeSeekTimer
        interval: 500
        onTriggered: {
            if (page.pendingSeekMs >= 0) {
                gplayer.seek(page.pendingSeekMs)
                page.pendingSeekMs = -1
            }
        }
    }

    Connections {
        // A null target = disconnected (Connections.enabled needs Qt 5.7; SFOS is 5.6).
        // Once resolved we stop listening, so a still-open page underneath doesn't grab
        // a resolve meant for a newer video page.
        target: page.resolving ? app.backend : null
        onResolved: {
            page.resolving = false
            page.isLive = !!info.is_live
            // Prime the remembered speed while the pipeline doesn't exist yet (setRate is a no-op
            // without a pipeline — no seek). The C++ player then engages it during preroll, before
            // the first frame, so both the audio and video branches take it together — no flush,
            // no dark flash, no audio/video rate split.
            gplayer.rate = page.playbackRate
            page.channel = info.uploader || ""
            page.channelId = info.channel_id || ""
            page.channelUrl = info.channel_url || ""
            page.description = info.description || ""
            page.videoDuration = info.duration || 0
            page.chapters = info.chapters || []
            page.qualities = info.qualities || []
            // Reflect the track we actually started on — the default-quality cap may not be
            // the top of the ladder — falling back to the highest if we can't match it.
            page.currentQuality = ""
            for (var qi = 0; qi < page.qualities.length; qi++) {
                if (page.qualities[qi].itag === info.video_itag) {
                    page.currentQuality = page.qualities[qi].label
                    break
                }
            }
            if (page.currentQuality.length === 0 && page.qualities.length > 0)
                page.currentQuality = page.qualities[0].label
            // Captions: hand the two lists to the overlay, clear any prior video's track, then
            // auto-enable the remembered language if this video offers it.
            transport.captionTracks = info.tracks || []
            transport.captionTranslations = info.translations || []
            page.selectCaption(null, false)
            page.autoSelectCaption(transport.captionTracks, transport.captionTranslations)
            if (page.channelUrl.length > 0 || page.channelId.length > 0)
                app.backend.fetchChannelAvatar(page.channelUrl || page.channelId,
                    function(res) { if (res && res.thumbnail) page.channelAvatar = res.thumbnail })
            if (info.video_url && info.video_url.length > 0
                    && info.audio_url && info.audio_url.length > 0) {
                page.useGst = true
                gplayer.userAgent = info.http_ua
                gplayer.audioUrl = info.audio_url
                gplayer.videoUrl = info.video_url
                gplayer.play()
                // Resume: seek to the saved spot once the pipeline reports a duration
                // (reusing the quality-switch restore path).
                if (page.resumeMs > 0)
                    page.pendingSeekMs = page.resumeMs
            } else if (info.muxed_url && info.muxed_url.length > 0) {
                // A muxed stream (both tracks in one URL). HLS (m3u8) is adaptive and plays
                // natively through QtMultimedia; a progressive muxed URL (itag 18 — often the
                // ONLY format left once YouTube forces SABR on the adaptive ones) goes through
                // our reliable C++ player in single-source mode (empty audioUrl → one source
                // feeds both tracks). QtMultimedia mishandled the proxied progressive stream.
                if (info.muxed_proto && info.muxed_proto.indexOf("m3u8") >= 0) {
                    page.useGst = false
                    mediaPlayer.source = info.muxed_url
                    mediaPlayer.play()
                } else {
                    page.useGst = true
                    gplayer.userAgent = info.http_ua
                    gplayer.audioUrl = ""
                    gplayer.videoUrl = info.muxed_url
                    gplayer.play()
                    if (page.resumeMs > 0)
                        page.pendingSeekMs = page.resumeMs
                }
            } else {
                page.errorText = "no playable stream"
            }
            if (page.errorText.length === 0)
                page.claimNowPlaying()
        }
        onResolveError: {
            page.resolving = false
            page.errorText = message
            transport.qualityMenuOpen = false
            transport.captionMenuOpen = false
        }
    }

    Connections {
        target: gplayer
        onErrorOccurred: page.recoverPlayback(message)
        // Duration becoming known means the pipeline has prerolled and can seek — the moment to
        // restore position after a quality switch. (Speed is handled in the C++ player at preroll.)
        onDurationChanged: if (page.pendingSeekMs >= 0 && gplayer.duration > 0)
                               resumeSeekTimer.restart()
    }

    // Whenever playback stops — paused or ended — surface the controls (the auto-hide
    // timer only runs while playing, so they stay up until playback resumes).
    onIsPlayingChanged: {
        if (!page.isPlaying)
            page.controlsShown = true
        if (nowPlayingConn.target)          // keep the cover's play/pause icon in sync
            app.nowPlaying.playing = page.isPlaying
    }

    // The quality menu lives inside the controls overlay, so if the controls hide for any
    // reason, close the menu with them — otherwise the open flag leaves an invisible, dead menu.
    onControlsShownChanged: if (!page.controlsShown) {
        transport.qualityMenuOpen = false
        transport.captionMenuOpen = false
    }

    // Position ticks (500ms) drive the SponsorBlock auto-skip check. They also clear the
    // auto-recovery budget: once we've played a few seconds past the last stall, the stream
    // is healthy, so a later, unrelated 403 gets its own fresh set of retries.
    onPositionMsChanged: {
        if (page.playRetries > 0 && page.positionMs > page.recoverAtMs)
            page.playRetries = 0
        page.checkSponsorSkip()
        page.updateCaption()
    }

    Timer {
        id: skipHintTimer
        interval: 1600
        onTriggered: page.skipHint = ""
    }

    // Auto audio-only: freeze the video decoder while the app is hidden (backgrounded or
    // screen locked), resume it when we're back in the foreground. Audio is untouched, so
    // background playback stays gapless while the CPU stops decoding unseen frames.
    Connections {
        target: Qt.application
        onActiveChanged: {
            if (Qt.application.active) {
                videoFreezeTimer.stop()
                // A stall that happened while we were backgrounded (e.g. locking before the
                // stream prerolled) was deferred — now that we're foreground with the network
                // back, reload from scratch. A fresh budget, since we couldn't really try before.
                if (page.pendingRecovery) {
                    page.pendingRecovery = false
                    page.playRetries = 0
                    page.recoverPlayback("")
                    return
                }
                if (page.useGst) {
                    gplayer.setVideoActive(true)
                    // Re-upload the frame: a display-off/on cycle can drop its GL texture,
                    // leaving a black frame until the next decode. A beat later, once the scene
                    // graph has rebuilt, so the repaint lands on a fresh context.
                    resumeRepaintTimer.restart()
                }
            } else if (page.useGst) {
                videoFreezeTimer.restart()   // short delay so quick tab-outs don't freeze
            }
        }
    }
    Timer {
        id: videoFreezeTimer
        interval: 800
        onTriggered: if (page.useGst && !Qt.application.active) gplayer.setVideoActive(false)
    }
    Timer {
        id: resumeRepaintTimer
        interval: 250
        repeat: false
        onTriggered: if (page.useGst) gplayer.requestRepaint()
    }

    // Keep the display awake while a video is actually playing, if the user enabled it. Isolated
    // in a Loader so a device without the Nemo.KeepAlive plugin degrades quietly (toggle inert).
    Loader {
        active: app.backend.keepDisplayOn && page.isPlaying
        source: Qt.resolvedUrl("KeepDisplayOn.qml")
        onStatusChanged: if (status === Loader.Error)
            console.log("FinTube: Nemo.KeepAlive unavailable — keep-display-on is inert")
    }

    // Native SFOS share sheet — the "unified menu" (Bluetooth, email, accounts, …). API matches
    // Opal.LinkHandler's proven usage: set `resources` then call trigger() (see videoAction()).
    ShareAction {
        id: shareLink
        mimeType: "text/x-url"
        title: "Share link"
    }

    // A lone tap toggles the controls, but only after we're sure it wasn't the first half of
    // a double-tap (which seeks instead).
    Timer {
        id: tapTimer
        interval: 250
        // Don't let a single-tap toggle (from a fall-through tap on empty video) hide the
        // controls while the quality menu is open — that would strand the menu invisible.
        onTriggered: if (!transport.qualityMenuOpen && !transport.captionMenuOpen)
                         page.controlsShown = !page.controlsShown
    }
    // Fire the accumulated seek once the taps settle (avoids a burst of flush-seeks).
    Timer {
        id: seekApplyTimer
        interval: 250
        onTriggered: page.seekTo(page.seekTargetMs)
    }
    // The double-tap seek gesture ends once tapping stops for a moment.
    Timer {
        id: seekHideTimer
        interval: 700
        onTriggered: page.seekActive = false
    }

    Component.onCompleted: {
        page.updateCutout()
        app.lastVideo = null    // we're watching this now → hide the resume bar
        // Fetch the saved position FIRST, then resolve — both share the Python worker and
        // resolve() is slow, so kicking it off inside the position callback guarantees
        // resumeMs is set before onResolved fires (otherwise the seek target is still 0).
        app.backend.resumePosition(videoId, function(sec) {
            page.resumeMs = (sec || 0) * 1000
            app.backend.resolve(videoId)
            // Queue sponsor segments AFTER resolve so they don't delay the (slow) resolve.
            if (app.backend.sponsorBlock)
                app.backend.sponsorSegments(videoId, function(segs) { page.sponsorSegments = segs || [] })
        })
    }

    // ---- Notch spacer (portrait only): a black strip so the physical camera cutout sits over black,
    // with the video dropped below it rather than clipped. ----
    Rectangle {
        visible: !page.landscape && !page.portraitFull && page.notchOffset > 0
        color: "black"
        anchors { top: parent.top; left: parent.left; right: parent.right }
        height: page.notchOffset
    }

    // ---- Video surface (top strip in portrait, whole screen in landscape) ----
    Rectangle {
        id: videoBox
        color: "black"
        anchors { top: parent.top; left: parent.left; right: parent.right
                  topMargin: (page.landscape || page.portraitFull) ? 0 : page.notchOffset }
        height: (page.landscape || page.portraitFull) ? page.height : Math.round(width * 9 / 16)

        VideoPlayer {
            id: gplayer
            anchors.fill: parent
            visible: page.useGst
            // Set before playback so the player picks its FBO render target + pipeline branch.
            hwDecode: app.backend.hwDecode
        }
        VideoOutput {
            anchors.fill: parent
            source: mediaPlayer
            fillMode: VideoOutput.PreserveAspectFit
            visible: !page.useGst
        }

        BusyIndicator {
            anchors.centerIn: parent
            size: BusyIndicatorSize.Large
            running: page.resolving
        }
        Label {
            anchors.centerIn: parent
            visible: page.errorText.length > 0
            width: parent.width - 2 * Theme.paddingLarge
            wrapMode: Text.Wrap
            horizontalAlignment: Text.AlignHCenter
            color: Theme.errorColor
            text: page.errorText
        }

        MouseArea {
            anchors.fill: parent
            enabled: !page.resolving && page.errorText.length === 0
            onClicked: {
                var zone = mouse.x < width / 2 ? "left" : "right"
                if (page.seekActive) {
                    page.applySeekStep(zone)                    // already seeking → stack
                } else if (tapTimer.running && page.tapZone === zone) {
                    tapTimer.stop()
                    page.applySeekStep(zone)                    // second tap same side → seek
                } else {
                    page.tapZone = zone
                    tapTimer.restart()                          // wait for a possible 2nd tap
                }
            }
        }

        // Transient "Skipped sponsor" pill (SponsorBlock).
        Rectangle {
            visible: page.skipHint.length > 0
            anchors {
                horizontalCenter: parent.horizontalCenter
                top: parent.top; topMargin: Theme.paddingLarge
            }
            radius: Theme.paddingSmall
            color: "#C8000000"
            width: skipHintLabel.width + 2 * Theme.paddingMedium
            height: skipHintLabel.height + Theme.paddingSmall
            Label {
                id: skipHintLabel
                anchors.centerIn: parent
                text: page.skipHint
                color: "white"
                font.pixelSize: Theme.fontSizeExtraSmall
            }
        }

        // Download status pill (top-left).
        Rectangle {
            visible: page.downloading || page.downloadHint.length > 0
            anchors { left: parent.left; top: parent.top; margins: Theme.paddingMedium }
            radius: Theme.paddingSmall
            color: "#C8000000"
            width: dlLabel.width + 2 * Theme.paddingMedium
            height: dlLabel.height + Theme.paddingSmall
            Label {
                id: dlLabel
                anchors.centerIn: parent
                text: page.downloading
                      ? ("Downloading " + page.downloadKind + " " + Math.round(page.downloadPct) + "%")
                      : page.downloadHint
                color: "white"
                font.pixelSize: Theme.fontSizeExtraSmall
            }
        }

        // Caption band — the active subtitle line, centred over the lower video. A sibling of
        // the controls (not a child) so it stays up while the controls auto-hide. Rides above the
        // scrubber when the controls are showing, drops lower when they're not. Declared BEFORE
        // the controls so an open pill menu / scrim draws over it.
        Item {
            anchors.fill: parent
            visible: page.currentCaption.length > 0 && page.errorText.length === 0
            Rectangle {
                anchors {
                    horizontalCenter: parent.horizontalCenter
                    bottom: parent.bottom
                    bottomMargin: page.controlsShown ? Theme.itemSizeMedium : Theme.paddingLarge
                }
                width: Math.min(captionText.implicitWidth + 2 * Theme.paddingMedium,
                                parent.width - 2 * Theme.paddingLarge)
                height: captionText.height + Theme.paddingSmall
                radius: Theme.paddingSmall
                color: "#B0000000"
                Label {
                    id: captionText
                    anchors.centerIn: parent
                    width: parent.width - 2 * Theme.paddingMedium
                    text: page.currentCaption
                    wrapMode: Text.Wrap
                    horizontalAlignment: Text.AlignHCenter
                    color: "white"
                    font.pixelSize: (page.landscape || page.portraitFull)
                                    ? Theme.fontSizeMedium : Theme.fontSizeSmall
                }
            }
        }

        // Controls overlay the whole video (centred play/pause + bottom scrubber); tap
        // the video to toggle, and they auto-hide while playing (YouTube-style).
        TransportControls {
            id: transport
            anchors.fill: parent
            visible: !page.resolving && page.errorText.length === 0
            enabled: page.controlsShown
            opacity: page.controlsShown ? 1 : 0
            Behavior on opacity { FadeAnimation {} }
            positionMs: page.positionMs
            durationMs: page.durationMs
            isPlaying: page.isPlaying
            playbackRate: page.playbackRate
            speedEnabled: page.useGst
            qualities: page.qualities
            currentQuality: page.currentQuality
            qualityEnabled: page.useGst
            chapters: page.chapters
            currentChapter: page.currentChapter
            fullscreen: page.landscape || page.portraitFull
            onSeekRequested: page.seekTo(ms)
            onTogglePlay: page.togglePlay()
            onCycleSpeed: page.cycleSpeed()
            onQualitySelected: page.switchQuality(q)
            onCaptionChosen: page.selectCaption(track, true)
            onToggleFullscreen: page.toggleFullscreen()
            onInteracted: page.controlsShown = true
        }

        // Double-tap seek indicator — tints the tapped half and shows the running total.
        Rectangle {
            anchors {
                top: parent.top; bottom: parent.bottom
                left: page.seekZone === "left" ? parent.left : parent.horizontalCenter
                right: page.seekZone === "left" ? parent.horizontalCenter : parent.right
            }
            color: "#22FFFFFF"
            opacity: page.seekActive ? 1 : 0
            visible: opacity > 0
            Behavior on opacity { FadeAnimation { duration: 150 } }

            Column {
                anchors.centerIn: parent
                spacing: Theme.paddingSmall
                Label {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: page.seekZone === "left" ? "« «" : "» »"
                    color: "white"
                    font.pixelSize: Theme.fontSizeExtraLarge
                }
                Label {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: (page.seekAmount > 0 ? "+" : "") + page.seekAmount + " s"
                    color: "white"
                    font.pixelSize: Theme.fontSizeMedium
                }
            }
        }
    }

    // Auto-hide the overlay a few seconds after it's shown (only while playing, so a
    // paused video keeps its controls up; and never while the quality menu is open).
    Timer {
        interval: 3500
        running: page.controlsShown && page.isPlaying
                 && !transport.qualityMenuOpen && !transport.captionMenuOpen
        onTriggered: page.controlsShown = false
    }

    // ---- Portrait: title + channel + description below the video (scrolls) ----
    SilicaFlickable {
        id: infoFlick
        visible: !page.landscape && !page.portraitFull
        anchors {
            top: videoBox.bottom; bottom: parent.bottom
            left: parent.left; right: parent.right
        }
        contentHeight: infoColumn.height + 2 * Theme.paddingMedium
        clip: true

        // A moving window over the fetched batch (no network): reveal more as we near the
        // bottom, and unload the tail again once a lot of it sits below the fold — i.e. the
        // user scrolled back up. The wide dead-zone between the two thresholds stops it from
        // thrashing, and trimming only content well below the viewport avoids a visible jump.
        onContentYChanged: {
            if (!page.commentsLoaded)
                return
            var belowFold = contentHeight - (contentY + height)
            if (page.commentsShown < page.comments.length && belowFold < height * 0.5)
                page.commentsShown = Math.min(page.commentsShown + 5, page.comments.length)
            else if (page.commentsShown > 5 && belowFold > height * 2.0)
                page.commentsShown = Math.max(5, page.commentsShown - 5)
        }

        PullDownMenu {
            MenuItem {
                text: "Download audio"
                enabled: !page.downloading
                onClicked: page.startDownload("audio")
            }
            MenuItem {
                text: "Download video"
                enabled: !page.downloading
                onClicked: page.startDownload("video")
            }
        }

        Column {
            id: infoColumn
            y: Theme.paddingMedium
            width: parent.width
            spacing: Theme.paddingMedium

            // LIVE badge — makes it obvious this is a livestream, not a normal video.
            Rectangle {
                x: Theme.horizontalPageMargin
                visible: page.isLive
                width: liveLabel.width + 2 * Theme.paddingMedium
                height: liveLabel.height + Theme.paddingSmall
                radius: Theme.paddingSmall
                color: "#cc0000"
                Label {
                    id: liveLabel
                    anchors.centerIn: parent
                    text: "● LIVE"
                    color: "white"
                    font.bold: true
                    font.pixelSize: Theme.fontSizeExtraSmall
                }
            }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                visible: page.title.length > 0
                text: page.title
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSizeMedium
                color: Theme.highlightColor
            }

            Item {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                visible: page.channel.length > 0
                height: Math.max(channelLabel.height, subscribeButton.height, chAvatar.height)

                Image {
                    id: chAvatar
                    anchors { left: parent.left; verticalCenter: parent.verticalCenter }
                    width: page.channelAvatar.length > 0 ? Theme.iconSizeMedium : 0
                    height: width
                    fillMode: Image.PreserveAspectCrop
                    asynchronous: true
                    smooth: true
                    source: page.channelAvatar
                }

                Button {
                    id: subscribeButton
                    anchors { right: parent.right; verticalCenter: parent.verticalCenter }
                    visible: page.channelId.length > 0
                    text: page.subscribed ? "Subscribed" : "Subscribe"
                    onClicked: app.backend.toggleSubscription(
                        page.channelId, page.channel, page.channelUrl, page.channelAvatar)
                }

                Label {
                    id: channelLabel
                    anchors {
                        left: chAvatar.right
                        leftMargin: page.channelAvatar.length > 0 ? Theme.paddingMedium : 0
                        right: subscribeButton.visible ? subscribeButton.left : parent.right
                        rightMargin: Theme.paddingMedium
                        verticalCenter: parent.verticalCenter
                    }
                    text: page.channel
                    truncationMode: TruncationMode.Fade
                    font.pixelSize: Theme.fontSizeSmall
                    color: channelArea.pressed ? Theme.highlightColor : Theme.secondaryColor

                    // Tap the channel name to open its page.
                    MouseArea {
                        id: channelArea
                        anchors.fill: parent
                        enabled: page.channelUrl.length > 0 || page.channelId.length > 0
                        onClicked: pageStack.push(Qt.resolvedUrl("ChannelPage.qml"),
                            { channelRef: page.channelUrl || page.channelId,
                              channelName: page.channel,
                              channelId: page.channelId,
                              channelThumb: page.channelAvatar })
                    }
                }
            }

            // Action bar — YouTube-style quick actions under the channel line, kept OUT of the
            // pulley so they're one tap (not a pull-down). Share opens the native SFOS share sheet.
            Row {
                x: Theme.horizontalPageMargin
                spacing: Theme.paddingLarge
                visible: page.videoId.length > 0

                Column {
                    width: Theme.iconSizeMedium + Theme.paddingLarge
                    IconButton {
                        anchors.horizontalCenter: parent.horizontalCenter
                        icon.source: "image://theme/icon-m-link"
                        onClicked: page.videoAction("open")
                    }
                    Label {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: "Open"
                        font.pixelSize: Theme.fontSizeTiny
                        color: Theme.secondaryColor
                    }
                }
                Column {
                    width: Theme.iconSizeMedium + Theme.paddingLarge
                    IconButton {
                        anchors.horizontalCenter: parent.horizontalCenter
                        icon.source: "image://theme/icon-m-clipboard"
                        onClicked: page.videoAction("copy")
                    }
                    Label {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: "Copy"
                        font.pixelSize: Theme.fontSizeTiny
                        color: Theme.secondaryColor
                    }
                }
                Column {
                    width: Theme.iconSizeMedium + Theme.paddingLarge
                    IconButton {
                        anchors.horizontalCenter: parent.horizontalCenter
                        icon.source: "image://theme/icon-m-share"
                        onClicked: page.videoAction("share")
                    }
                    Label {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: "Share"
                        font.pixelSize: Theme.fontSizeTiny
                        color: Theme.secondaryColor
                    }
                }
            }

            // (Resolution picker moved into the video overlay — the quality pill, left of
            //  the speed pill; tapping it opens a menu of resolutions. See TransportControls.)

            Label {
                id: descLabel
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                visible: page.description.length > 0
                text: page.description
                wrapMode: Text.Wrap
                maximumLineCount: page.descExpanded ? 100000 : 5
                elide: page.descExpanded ? Text.ElideNone : Text.ElideRight
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.secondaryHighlightColor
            }
            BackgroundItem {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                height: Theme.itemSizeExtraSmall
                // Only when there's actually more to show (or we're already expanded).
                visible: page.description.length > 0 && (descLabel.truncated || page.descExpanded)
                Label {
                    anchors.verticalCenter: parent.verticalCenter
                    text: page.descExpanded ? "Show less" : "Show more"
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.highlightColor
                }
                onClicked: page.descExpanded = !page.descExpanded
            }

            // ---- Comments: tap the header to load, then they reveal on scroll ----
            Column {
                id: commentsCol
                width: parent.width
                spacing: Theme.paddingMedium

                BackgroundItem {
                    x: Theme.horizontalPageMargin
                    width: parent.width - 2 * Theme.horizontalPageMargin
                    height: Theme.itemSizeSmall
                    // After load this is just a header; loadComments() no-ops on re-tap.
                    Label {
                        anchors { left: parent.left; verticalCenter: parent.verticalCenter }
                        text: page.commentsLoaded
                                ? (page.commentsTotal > 0
                                    ? "Comments  ·  " + page.fmtCount(page.commentsTotal)
                                    : "Comments")
                                : (page.commentsLoading ? "Loading comments…" : "Load comments")
                        font.pixelSize: Theme.fontSizeMedium
                        color: (page.commentsLoaded || page.commentsLoading)
                                 ? Theme.highlightColor : Theme.primaryColor
                    }
                    BusyIndicator {
                        anchors { right: parent.right; verticalCenter: parent.verticalCenter }
                        size: BusyIndicatorSize.Small
                        running: page.commentsLoading
                    }
                    onClicked: page.loadComments()
                }

                Label {
                    x: Theme.horizontalPageMargin
                    width: parent.width - 2 * Theme.horizontalPageMargin
                    visible: page.commentsError.length > 0
                    text: page.commentsError
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.errorColor
                }
                Label {
                    x: Theme.horizontalPageMargin
                    width: parent.width - 2 * Theme.horizontalPageMargin
                    visible: page.commentsLoaded && page.comments.length === 0
                    text: "No comments"
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.secondaryColor
                }

                Repeater {
                    model: page.commentsLoaded ? Math.min(page.commentsShown, page.comments.length) : 0
                    delegate: Item {
                        width: commentsCol.width
                        property var c: page.comments[index]
                        height: Math.max(cAvatar.height, cCol.height) + Theme.paddingSmall

                        Image {
                            id: cAvatar
                            x: Theme.horizontalPageMargin
                            y: 0
                            width: (c && c.thumbnail) ? Theme.iconSizeSmall : 0
                            height: width
                            fillMode: Image.PreserveAspectCrop
                            asynchronous: true
                            smooth: true
                            source: (c && c.thumbnail) ? c.thumbnail : ""
                        }
                        Column {
                            id: cCol
                            anchors {
                                left: cAvatar.right
                                leftMargin: (c && c.thumbnail) ? Theme.paddingMedium : 0
                                right: parent.right; rightMargin: Theme.horizontalPageMargin
                                top: parent.top
                            }
                            spacing: Theme.paddingSmall / 2
                            Label {
                                width: parent.width
                                text: (c ? c.author : "")
                                      + (c && c.time ? "  ·  " + c.time : "")
                                font.pixelSize: Theme.fontSizeExtraSmall
                                color: (c && c.is_uploader)
                                         ? Theme.highlightColor : Theme.secondaryHighlightColor
                                truncationMode: TruncationMode.Fade
                            }
                            Label {
                                width: parent.width
                                text: c ? c.text : ""
                                wrapMode: Text.Wrap
                                font.pixelSize: Theme.fontSizeSmall
                                color: Theme.primaryColor
                            }
                            Label {
                                visible: !!(c && c.likes > 0)
                                text: "▲ " + page.fmtCount(c ? c.likes : 0)
                                font.pixelSize: Theme.fontSizeExtraSmall
                                color: Theme.secondaryColor
                            }
                        }
                    }
                }

                BackgroundItem {
                    x: Theme.horizontalPageMargin
                    width: parent.width - 2 * Theme.horizontalPageMargin
                    height: Theme.itemSizeSmall
                    visible: page.commentsLoaded && page.commentsShown < page.comments.length
                    Label {
                        anchors.verticalCenter: parent.verticalCenter
                        text: "Show more comments"
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.highlightColor
                    }
                    onClicked: page.commentsShown =
                        Math.min(page.commentsShown + 5, page.comments.length)
                }
            }
        }

        VerticalScrollDecorator { }
    }

    MediaPlayer {
        id: mediaPlayer
        autoPlay: false
        onError: page.recoverPlayback(errorString)
    }

    // The overlay's own scrim only covers the video surface, so in portrait a tap in the info
    // area below the video wouldn't dismiss an open pill menu. This catches those taps (and
    // swallows them, so they don't also trigger the control beneath). Declared last so it sits
    // above the info flickable; disabled when no menu is open, so taps pass through normally.
    // Zero-height in landscape/portraitFull (videoBox fills the page), so it's inert there.
    MouseArea {
        anchors { left: parent.left; right: parent.right
                  top: videoBox.bottom; bottom: parent.bottom }
        enabled: transport.qualityMenuOpen || transport.captionMenuOpen
        onClicked: {
            transport.qualityMenuOpen = false
            transport.captionMenuOpen = false
        }
    }

    Component.onDestruction: {
        page.persistPosition()
        if (page.videoId.length > 0)        // remember it for quick-resume from home
            app.lastVideo = { id: page.videoId, title: page.title, channel: page.channel }
        if (nowPlayingConn.target)          // we were the active player → clear the cover
            app.nowPlaying.active = false
        if (app.activePlayer === gplayer)
            app.activePlayer = null
        mediaPlayer.stop()
        gplayer.stop()
    }
}
