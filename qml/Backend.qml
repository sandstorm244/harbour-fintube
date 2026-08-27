import QtQuick 2.0
import io.thp.pyotherside 1.5

// Non-visual wrapper around the Python module. All calls are async and run off
// the UI thread (PyOtherSide worker), which is what we want for yt-dlp subprocess
// calls that take a few seconds.
Item {
    id: backend

    // True once we've confirmed a working yt-dlp is present.
    property bool ready: false
    property bool pyReady: false         // Python module imported and callable
    property string ytdlpVersion: ""
    property bool updating: false
    property bool installing: false      // downloading yt-dlp into the app data dir
    property real installPct: -1
    property bool ffmpegReady: false     // ffmpeg present (bundled/system) → HD merged downloads
    property string ffmpegVersion: ""
    property bool ffmpegInstalling: false
    property real ffmpegPct: -1
    property string ffmpegStatusMsg: ""
    property bool denoInstalling: false  // downloading Deno (the PO provider's runtime) into bin/
    property real denoPct: -1
    property string denoStatusMsg: ""
    property var subscriptions: []
    property bool hideShorts: true       // filter Shorts out of results (persisted by Python)
    property bool sponsorBlock: true     // auto-skip SponsorBlock segments during playback
    property string playerClient: ""     // yt-dlp youtube player_client ("" = auto)
    property string ytdlpChannel: "stable" // yt-dlp update channel: "stable" | "nightly"
    property string defaultQuality: "720" // baseline video height cap ("1080"/"720"/…/"0"=best)
    property bool hwDecode: false        // hardware video decode (droidvdec); experimental
    property bool keepDisplayOn: false   // hold the display awake while a video plays
    property bool eqEnabled: false       // 10-band equalizer on/off
    property var  eqBands: [0,0,0,0,0,0,0,0,0,0]  // per-band gain (dB), applied by the C++ player
    property real boostGain: 1.0         // volume boost (linear, 1.0 = none) above system max
    property var downloads: []           // completed downloads [{id,title,kind,path}]
    property var playlists: []           // library: [{id,title,kind,yt_id,count,thumbnail}]
    // Watch history: {videoId: {p,d,f,w,t}} — drives the thumbnail progress bar + WATCHED tag.
    // Loaded once at startup, reassigned (whole map) after each save so delegate bindings refresh.
    property var watchMap: ({})

    // PO-token provider (bgutil) — opt-in, user-installed Deno sidecar (see SettingsPage).
    property bool potInstalled: false
    property bool potEnabled: false      // installed AND switched on → used by yt-dlp
    property bool potDeno: false         // a Deno runtime is present (required to install/run)
    property bool potRunning: false      // the token server is currently listening
    property bool potInstalling: false   // clone + deno install in progress
    property string potStatusMsg: ""     // latest setup progress / result line
    property string potTag: ""           // pinned provider version
    property bool potResponding: false   // server actually ANSWERS HTTP → "working" (vs just port-open)
    property string potServerVersion: "" // version the running provider reports (from its /ping)
    property string potLastError: ""     // why the provider last failed to start / respond (diagnostics)
    property string potDenoPath: ""      // where Deno was found ("" = not found)

    // Download folder — where completed downloads are written. downloadDir is the configured value
    // ("" = the app's own folder); downloadDirEffective is the absolute path actually in use.
    property string downloadDir: ""
    property string downloadDirEffective: ""

    signal resolved(var info)
    signal resolveError(string message)
    signal updateFinished(bool ok, string message)
    signal downloadProgress(string videoId, string kind, real percent)
    signal downloadFinished(string videoId, string kind, bool ok, string message)

    // One page of search results → the caller's callback (page-scoped, and drives the
    // scroll-to-load-more paging in SearchPage, mirroring channelVideos). `start` is the
    // 1-based first index. Callback gets the raw result {ok, items, has_more, ...}.
    function searchPage(query, kind, start, callback) {
        if (!query) { callback({}); return }
        py.call("youfish.search", [query, 15, kind || "video", start || 1], function(res) {
            callback(res || {})
        })
    }

    // Search autocomplete → caller's callback (page-scoped; called on every keystroke).
    function suggest(query, callback) {
        py.call("youfish.search_suggestions", [query], function(res) {
            callback(res && res.suggestions ? res.suggestions : [])
        })
    }

    // Classify an incoming youtube.com/youtu.be link → {kind, id, url} for the URL handler.
    function parseUrl(url, callback) {
        py.call("youfish.parse_youtube_url", [url], function(res) { callback(res || {}) })
    }

    function resolve(videoId) {
        py.call("youfish.resolve", [videoId], function(res) {
            if (res && res.ok) backend.resolved(res.info)
            else backend.resolveError(res ? res.error : "resolve failed")
        })
    }

    // Re-run the yt-dlp presence/version check (called on launch and on demand).
    function recheck() {
        py.call("youfish.ytdlp_version", [], function(v) {
            backend.ytdlpVersion = v || ""
            backend.ready = (v && v.length > 0)
        })
    }

    // --- Settings (persisted by Python) ---
    function loadSettings() {
        py.call("youfish.get_settings", [], function(s) {
            if (!s) return
            backend.hideShorts = !!s.hide_shorts
            backend.sponsorBlock = !!s.sponsorblock
            backend.playerClient = s.player_client || ""
            backend.ytdlpChannel = s.ytdlp_channel || "stable"
            backend.defaultQuality = s.default_quality || "720"
            backend.hwDecode = !!s.hw_decode
            backend.keepDisplayOn = !!s.keep_display_on
            backend.eqEnabled = !!s.eq_enabled
            if (s.eq_bands && s.eq_bands.length === 10)
                backend.eqBands = s.eq_bands
            backend.boostGain = s.boost_gain || 1.0
        })
    }

    // --- Audio effects (equalizer + volume boost), applied by the C++ VideoPlayer ---
    function setEqEnabled(on) {
        py.call("youfish.set_setting", ["eq_enabled", !!on], function(s) {
            if (s) backend.eqEnabled = !!s.eq_enabled
        })
    }
    function setEqBands(bands) {
        py.call("youfish.set_setting", ["eq_bands", bands], function(s) {
            if (s && s.eq_bands && s.eq_bands.length === 10)
                backend.eqBands = s.eq_bands
        })
    }
    function setBoostGain(gain) {
        py.call("youfish.set_setting", ["boost_gain", gain], function(s) {
            if (s) backend.boostGain = s.boost_gain || 1.0
        })
    }

    // Baseline video-quality cap (applied by resolve() as a ceiling that degrades down).
    function setDefaultQuality(q) {
        py.call("youfish.set_setting", ["default_quality", q], function(s) {
            if (s) backend.defaultQuality = s.default_quality || "720"
        })
    }

    // Hardware video decode (droidvdec→droideglsink). Experimental; takes effect on the next
    // video, and resolve() switches the format ladder to VP9-first while it's on.
    function setHwDecode(on) {
        py.call("youfish.set_setting", ["hw_decode", !!on], function(s) {
            if (s) backend.hwDecode = !!s.hw_decode
        })
    }

    function setKeepDisplayOn(on) {
        py.call("youfish.set_setting", ["keep_display_on", !!on], function(s) {
            if (s) backend.keepDisplayOn = !!s.keep_display_on
        })
    }

    function setHideShorts(on) {
        py.call("youfish.set_setting", ["hide_shorts", !!on], function(s) {
            if (s) backend.hideShorts = !!s.hide_shorts
        })
    }

    function setSponsorBlock(on) {
        py.call("youfish.set_setting", ["sponsorblock", !!on], function(s) {
            if (s) backend.sponsorBlock = !!s.sponsorblock
        })
    }

    // Generic string setting (player_client). Mirrors the saved value back onto the property.
    function setSetting(key, value) {
        py.call("youfish.set_setting", [key, value], function(s) {
            if (!s) return
            backend.playerClient = s.player_client || ""
            backend.ytdlpChannel = s.ytdlp_channel || "stable"
        })
    }

    // --- Resume points + SponsorBlock (per-video, from Python) ---
    function resumePosition(videoId, callback) {
        py.call("youfish.get_position", [videoId], function(sec) { callback(sec || 0) })
    }
    function savePosition(videoId, seconds) {
        py.call("youfish.set_position", [videoId, seconds], function() {})
    }
    // Record playback progress: updates the resume point AND the watch history (progress bar +
    // WATCHED tag + History list), then refreshes watchMap so visible thumbnails update on return.
    function saveWatch(videoId, position, duration, title, channel) {
        if (!videoId) return
        py.call("youfish.record_watch",
                [videoId, position || 0, duration || 0, title || "", channel || ""],
                function() { backend.loadWatchState() })
    }
    function loadWatchState() {
        py.call("youfish.watch_state", [], function(m) { backend.watchMap = m || ({}) })
    }
    // Recently-watched videos (newest first) for the History page.
    function watchHistory(callback) {
        py.call("youfish.watch_history", [200], function(list) { callback(list || []) })
    }
    function clearWatchHistory(callback) {
        py.call("youfish.clear_watch_history", [], function(res) {
            backend.loadWatchState()
            if (callback) callback(res || {})
        })
    }
    function sponsorSegments(videoId, callback) {
        py.call("youfish.sponsor_segments", [videoId], function(res) {
            callback(res && res.ok ? (res.segments || []) : [])
        })
    }

    // Home feed: subscribed channels' recent uploads merged newest-first (from Python,
    // via each channel's RSS). `force` bypasses the short backend cache.
    function subscriptionFeed(force, callback) {
        py.call("youfish.subscription_feed", [100, !!force], function(res) {
            callback(res || {})
        })
    }

    // {video_id: seconds} durations for feed videos (from yt-dlp; RSS lacks them). Called
    // after the feed shows so the length badges fill in without slowing the initial load.
    function feedDurations(callback) {
        py.call("youfish.feed_durations", [30], function(res) {
            callback(res || {})
        })
    }

    // --- Subscriptions (persisted by Python) + channel browsing ---
    function loadSubscriptions() {
        py.call("youfish.list_subscriptions", [], function(list) {
            backend.subscriptions = list || []
        })
    }

    function isSubscribed(channelId) {
        if (!channelId) return false
        for (var i = 0; i < backend.subscriptions.length; i++)
            if (backend.subscriptions[i].id === channelId) return true
        return false
    }

    function toggleSubscription(channelId, name, url, thumbnail) {
        py.call("youfish.toggle_subscription",
                [channelId, name || "", url || "", thumbnail || ""], function(res) {
            if (res && res.subscriptions)
                backend.subscriptions = res.subscriptions
        })
    }

    // Import channel subscriptions from a NewPipe / PipePipe backup (.zip or raw newpipe.db).
    // Refreshes the list on success; result {ok, added, skipped, total, error?} → caller callback.
    function importNewpipe(path, callback) {
        py.call("youfish.import_newpipe", [path || ""], function(res) {
            if (res && res.added > 0) backend.loadSubscriptions()
            if (callback) callback(res || {})
        })
    }

    // A page of a channel's uploads → the caller's callback (page-scoped, and drives the
    // scroll-to-load-more paging in ChannelPage). `start` is the 1-based first index.
    function channelVideos(channel, start, callback) {
        py.call("youfish.channel_videos", [channel, start || 1, 30], function(res) {
            callback(res || {})
        })
    }

    // Async avatar lookup; result goes straight to the caller's callback so it lands on
    // the page that asked (no cross-page signal confusion).
    function fetchChannelAvatar(channel, callback) {
        py.call("youfish.channel_avatar", [channel], function(res) {
            callback(res && res.ok ? res : {})
        })
    }

    // On-demand comment fetch (slow — walks YouTube continuations). Result to the caller's
    // callback so it lands on the video page that asked. One capped batch; the UI paginates.
    function fetchComments(videoId, limit, callback) {
        py.call("youfish.comments", [videoId, limit || 50], function(res) {
            callback(res || {})
        })
    }

    // --- Downloads (background, progress via pyotherside events) ---
    function download(videoId, title, kind) {
        py.call("youfish.download", [videoId, title || "", kind], function() {})
    }
    function loadDownloads() {
        py.call("youfish.list_downloads", [], function(list) { backend.downloads = list || [] })
    }
    function deleteDownload(videoId, kind) {
        py.call("youfish.delete_download", [videoId, kind], function(res) {
            if (res && res.downloads) backend.downloads = res.downloads
        })
    }
    // Where downloads are written: load the current folder, or set/reset it (folder picker in
    // Settings). setDownloadDir validates writability in Python and reports {ok, error?}.
    function loadDownloadLocation() {
        py.call("youfish.download_location", [], function(r) {
            if (!r) return
            backend.downloadDir = r.configured || ""
            backend.downloadDirEffective = r.effective || ""
        })
    }
    function setDownloadDir(path, callback) {
        py.call("youfish.set_download_dir", [path || ""], function(r) {
            if (r) {
                backend.downloadDir = r.configured || ""
                backend.downloadDirEffective = r.effective || ""
            }
            if (callback) callback(r || {})
        })
    }

    // --- Playlists (local library + saved YouTube playlists) ---
    function loadPlaylists() {
        py.call("youfish.list_playlists", [], function(list) { backend.playlists = list || [] })
    }
    function getPlaylist(plId, callback) {
        py.call("youfish.get_playlist", [plId], function(res) {
            callback(res && res.ok ? res.playlist : null)
        })
    }
    function createPlaylist(title, callback) {
        py.call("youfish.create_playlist", [title || ""], function(res) {
            if (res && res.playlists) backend.playlists = res.playlists
            if (callback) callback(res || {})
        })
    }
    function renamePlaylist(plId, title) {
        py.call("youfish.rename_playlist", [plId, title || ""], function(res) {
            if (res && res.playlists) backend.playlists = res.playlists
        })
    }
    function deletePlaylist(plId) {
        py.call("youfish.delete_playlist", [plId], function(res) {
            if (res && res.playlists) backend.playlists = res.playlists
        })
    }
    // video = {id, title, uploader, duration, thumbnail}
    function addToPlaylist(plId, video) {
        py.call("youfish.add_to_playlist",
                [plId, video.id, video.title || "", video.uploader || "",
                 video.duration || 0, video.thumbnail || ""],
                function() { backend.loadPlaylists() })
    }
    function removeFromPlaylist(plId, videoId, callback) {
        py.call("youfish.remove_from_playlist", [plId, videoId], function(res) {
            backend.loadPlaylists()
            if (callback) callback(res && res.ok ? res.playlist : null)
        })
    }
    // Fetch a YouTube playlist without saving (view on the fly).
    function youtubePlaylist(ref, callback) {
        py.call("youfish.youtube_playlist", [ref, 200], function(res) { callback(res || {}) })
    }
    // Save a YouTube playlist into the library (deduped by list id).
    function saveYoutubePlaylist(ref, callback) {
        py.call("youfish.save_youtube_playlist", [ref], function(res) {
            if (res && res.playlists) backend.playlists = res.playlists
            if (callback) callback(res || {})
        })
    }
    function refreshPlaylist(plId, callback) {
        py.call("youfish.refresh_playlist", [plId], function(res) {
            backend.loadPlaylists()
            if (callback) callback(res && res.ok ? res.playlist : null)
        })
    }
    // A channel's playlists (its Playlists tab; falls back to Releases for music channels).
    function channelPlaylists(channel, callback) {
        py.call("youfish.channel_playlists", [channel], function(res) { callback(res || {}) })
    }

    // Download yt-dlp into the app data dir (the sandbox-reachable location). Progress +
    // completion arrive as pyotherside events (see onReceived), reusing updateFinished.
    function installYtdlp() {
        if (backend.installing) return
        backend.installing = true
        backend.installPct = 0
        py.call("youfish.install_ytdlp", [], function() {})
    }

    // Self-update yt-dlp via its own `-U`. Can take a while (downloads the binary).
    function updateYtdlp() {
        if (backend.updating) return
        backend.updating = true
        py.call("youfish.ytdlp_update", [], function(res) {
            backend.updating = false
            if (res && res.version) {
                backend.ytdlpVersion = res.version
                backend.ready = res.version.length > 0
            }
            backend.updateFinished(!!(res && res.ok),
                res ? (res.output || res.error || "") : "update failed")
        })
    }

    // ffmpeg — optional, enables HD merged downloads. Managed like yt-dlp (bundled binary).
    function recheckFfmpeg() {
        py.call("youfish.ffmpeg_version", [], function(v) {
            backend.ffmpegVersion = v || ""
            backend.ffmpegReady = (v && v.length > 0)
        })
    }
    function installFfmpeg() {
        if (backend.ffmpegInstalling) return
        backend.ffmpegInstalling = true
        backend.ffmpegPct = 0
        backend.ffmpegStatusMsg = ""
        py.call("youfish.install_ffmpeg", [], function() {})
    }
    // Download Deno (the PO provider's runtime) into our own bin/ — so the provider needs no
    // manual runtime install. ~40 MB one-time fetch; progress/result arrive as pyotherside events.
    function installDeno() {
        if (backend.denoInstalling) return
        backend.denoInstalling = true
        backend.denoPct = 0
        backend.denoStatusMsg = ""
        py.call("youfish.install_deno", [], function() {})
    }

    // --- PO-token provider (bgutil): opt-in setup + on/off, all driven from Python ---
    function loadPotStatus() {
        py.call("youfish.pot_status", [], function(s) {
            if (!s) return
            backend.potInstalled = !!s.installed
            backend.potEnabled = !!s.enabled
            backend.potDeno = !!s.deno
            backend.potDenoPath = s.deno_path || ""
            backend.potRunning = !!s.running
            backend.potResponding = !!s.responding
            backend.potServerVersion = s.server_version || ""
            backend.potLastError = s.last_error || ""
            backend.potTag = s.tag || ""
        })
    }
    // Full copy-pasteable health report for the provider + its deps → caller callback. The report
    // action actively (re)starts the server, so refresh the status props afterward — otherwise the
    // top status line stays stale ("server not started") while the report already says "working".
    function potDiagnostics(callback) {
        py.call("youfish.pot_diagnostics", [], function(res) {
            backend.loadPotStatus()
            if (callback) callback(res || {})
        })
    }
    function installPotProvider() {
        if (backend.potInstalling) return
        backend.potInstalling = true
        backend.potStatusMsg = "Starting…"
        py.call("youfish.install_pot_provider", [], function() {})
    }
    // Resolve the latest provider release from GitHub + (re)install it. User-initiated, so the
    // sidecar only moves in step with the yt-dlp plugin on an explicit tap — never silently.
    function updatePotProvider() {
        if (backend.potInstalling) return
        backend.potInstalling = true
        backend.potStatusMsg = "Checking for the latest provider…"
        py.call("youfish.update_pot_provider", [], function() {})
    }
    function setPotEnabled(on) {
        py.call("youfish.set_pot_enabled", [!!on], function(s) {
            if (!s) return
            backend.potInstalled = !!s.installed
            backend.potEnabled = !!s.enabled
            backend.potRunning = !!s.running
            backend.potResponding = !!s.responding
            backend.potLastError = s.last_error || ""
        })
    }
    // Best-effort clean shutdown of the sidecar on app exit (a kernel PDEATHSIG backstops it).
    function stopPotServer() {
        py.call("youfish.stop_pot_server", [], function() {})
    }

    Python {
        id: py
        Component.onCompleted: {
            addImportPath(Qt.resolvedUrl("../python").toString().replace("file://", ""))
            importModule("youfish", function() {
                backend.pyReady = true
                backend.recheck()
                backend.recheckFfmpeg()
                backend.loadSubscriptions()
                backend.loadSettings()
                backend.loadDownloads()
                backend.loadDownloadLocation()
                backend.loadPlaylists()
                backend.loadWatchState()
                backend.loadPotStatus()
                py.call("youfish.prewarm", [], function() {})  // POT server up before first play
            })
        }
        // Background-download progress/completion events from the Python thread.
        onReceived: {
            if (data[0] === "download_progress")
                backend.downloadProgress(data[1], data[2], data[3])
            else if (data[0] === "download_done") {
                backend.downloadFinished(data[1], data[2], data[3], data[4])
                backend.loadDownloads()
            }
            else if (data[0] === "ytdlp_install_progress")
                backend.installPct = data[1]
            else if (data[0] === "ytdlp_install_done") {
                backend.installing = false
                backend.installPct = -1
                if (data[3] && data[3].length > 0) {
                    backend.ytdlpVersion = data[3]
                    backend.ready = true
                }
                backend.updateFinished(!!data[1], data[2])
            }
            else if (data[0] === "ffmpeg_install_progress")
                backend.ffmpegPct = data[1]
            else if (data[0] === "ffmpeg_install_done") {
                backend.ffmpegInstalling = false
                backend.ffmpegPct = -1
                backend.ffmpegStatusMsg = data[2]
                if (data[3] && data[3].length > 0) {
                    backend.ffmpegVersion = data[3]
                    backend.ffmpegReady = true
                }
            }
            else if (data[0] === "deno_install_progress")
                backend.denoPct = data[1]
            else if (data[0] === "deno_install_done") {
                backend.denoInstalling = false
                backend.denoPct = -1
                backend.denoStatusMsg = data[2]
                backend.loadPotStatus()   // refresh potDeno — the provider setup unlocks once found
            }
            else if (data[0] === "pot_install_progress")
                backend.potStatusMsg = data[1]
            else if (data[0] === "pot_install_done") {
                backend.potInstalling = false
                backend.potStatusMsg = data[2]
                backend.loadPotStatus()
            }
        }
        onError: console.log("python error: " + traceback)
    }
}
