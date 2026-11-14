.pragma library
// Pure decisions the pages make, kept apart from Silica so tests/qml_logic_test.js can run them
// under node. Rules: ES5 only (Qt 5.6's engine), no QML types, no qsTr — return codes and let the
// page map them to text. A bug here shows up as a wrong subtitle, a wrong skip or a broken seek on
// the device, so every function has a test.

// Base language code — "pt" from "pt-BR", "es" from "es-419", "en" from "en-orig". Used so a
// remembered language still matches the same language labelled at a different granularity.
function baseLang(code) {
    return (code || "").split("-")[0].toLowerCase()
}

// The caption track to auto-enable for a remembered language `want`, or null. Exact code first
// (real tracks, then auto-translations), then the same base language at a different granularity
// (a manual "pt-BR" preference vs an ASR "pt" track, "en" vs "en-orig", "es-419" vs "es").
function pickCaption(want, tracks, translations) {
    if (!want)
        return null
    tracks = tracks || []
    translations = translations || []
    var i
    for (i = 0; i < tracks.length; i++)
        if (tracks[i].lang === want) return tracks[i]
    for (i = 0; i < translations.length; i++)
        if (translations[i].lang === want) return translations[i]
    var base = baseLang(want)
    for (i = 0; i < tracks.length; i++)
        if (baseLang(tracks[i].lang) === base) return tracks[i]
    for (i = 0; i < translations.length; i++)
        if (baseLang(translations[i].lang) === base) return translations[i]
    return null
}

// The caption line at time t (seconds). Cues are time-sorted, so the caller keeps a cursor
// (`index`) and the last sampled time; the scan only moves forward unless time went backwards
// (a seek), which rescans from the start. Returns {index, text}.
function captionAt(cues, t, index, lastT) {
    if (!cues || cues.length === 0)
        return { index: 0, text: "" }
    var i = index
    if (i < 0 || t < lastT) i = 0
    while (i < cues.length && t >= cues[i].start + cues[i].dur) i++
    return { index: i, text: (i < cues.length && t >= cues[i].start) ? cues[i].text : "" }
}

// Per-category SponsorBlock action from the saved map: "skip" | "manual" | "off". A category with
// no saved action is off.
function sbAction(actions, cat) {
    var v = actions ? actions[cat] : undefined
    return (v === "skip" || v === "manual") ? v : "off"
}

// What SponsorBlock does at position `pos` (seconds): {skip: segment to jump past now, or null;
// manual: segment to offer a skip button for, or null}. The last 0.5 s of a segment doesn't count,
// so a skip that lands on the segment's end can't loop. Auto-skip only while playing: a paused
// user parked in a segment isn't dragged forward. A "skip" segment wins over a "manual" overlap.
function sponsorDecision(segments, pos, actions, playing) {
    var manual = null
    for (var i = 0; i < (segments || []).length; i++) {
        var s = segments[i]
        if (pos < s.start || pos >= s.end - 0.5)
            continue
        var act = sbAction(actions, s.category)
        if (act === "skip") {
            if (playing)
                return { skip: s, manual: null }
        } else if (act === "manual" && manual === null) {
            manual = s
        }
    }
    return { skip: null, manual: manual }
}

// One 10 s step of a double-tap seek. `state` = {active, zone, amount, targetMs} (amount in signed
// seconds). The first tap starts from the current position; more taps on the same side stack;
// switching side restarts the count from the target reached so far. Clamped to [0, duration]
// (duration 0 = unknown, so no upper clamp). Returns the new state.
function seekStep(state, zone, positionMs, durationMs) {
    var step = (zone === "right") ? 10000 : -10000
    var active = state.active, amount = state.amount, target = state.targetMs
    if (!active) {
        active = true
        amount = 0
        target = positionMs
    } else if (state.zone !== zone) {
        amount = 0
    }
    target = Math.max(0, target + step)
    if (durationMs > 0)
        target = Math.min(durationMs, target)
    return { active: active, zone: zone, amount: amount + step / 1000, targetMs: target }
}

// How to play a resolve answer. mode:
//   "dual"  — separate video + audio URLs on the C++ player; the container exts choose each
//             branch's seek plan, so they MUST be set before the URLs (CLAUDE.md invariant 9).
//   "muxed" — one progressive URL (e.g. itag 18) on the C++ player in single-source mode; the exts
//             are cleared so a previous dual attempt's "mp4" can't give it a downloadbuffer.
//   "hls"   — an m3u8 manifest, played by QtMultimedia, unproxied.
//   "none"  — nothing playable.
function playPlan(info) {
    info = info || {}
    if (info.video_url && info.audio_url)
        return { mode: "dual", videoUrl: info.video_url, audioUrl: info.audio_url,
                 videoExt: info.video_ext || "", audioExt: info.audio_ext || "" }
    if (info.muxed_url) {
        if (info.muxed_proto && info.muxed_proto.indexOf("m3u8") >= 0)
            return { mode: "hls", url: info.muxed_url }
        return { mode: "muxed", videoUrl: info.muxed_url, audioUrl: "", videoExt: "", audioExt: "" }
    }
    return { mode: "none" }
}

// Leaving the watch page: keep the pipeline (background audio, or a paused video that reattaches
// where it was on reopen) or stop it. A never-started video (not playing, position 0) isn't kept.
function keepPlayerOnLeave(backgroundAudio, useGst, playing, positionMs) {
    return !!(backgroundAudio && useGst && (playing || positionMs > 0))
}

// Holding the display awake while a video plays (not a setting: every video player does it). Only
// while the video is visibly playing: on screen, app in front, position still moving. Anything else
// (paused, ended, failed, stalled, backgrounded) hands the screen back to the system's own timeout.
// The stall cap is the safety net: a player can keep reporting "playing" after an error or a stream
// that died without an end, and that must never hold the screen on all night.
var AWAKE_STALL_MS = 30000
function awakeStalled(sinceMoveMs) {
    return sinceMoveMs >= AWAKE_STALL_MS
}
function keepAwake(playing, onScreen, appActive, stalled) {
    return !!(playing && onScreen && appActive && !stalled)
}

// The queue entry after `index`, or -1 at the end. Autoplay and the next-video prefetch both use
// it, so the video that gets warmed is the one that plays.
function nextQueueIndex(queue, index) {
    var ni = index + 1
    return (queue && ni < queue.length && queue[ni] && queue[ni].id) ? ni : -1
}

// Title of the chapter playing at `pos` (seconds): the last one that has started. "" before the
// first chapter or when there are none. Chapters are sorted by start.
function chapterAt(chapters, pos) {
    var t = ""
    for (var i = 0; i < (chapters || []).length; i++) {
        if (pos >= chapters[i].start) t = chapters[i].title
        else break
    }
    return t
}

// --- Watch page: captions, recovery, reattach, history ------------------------------------------

// The caption language to remember when the user picks `track` (null / no URL = Off → "").
function captionPref(track) {
    return (track && track.url) ? (track.lang || "") : ""
}

// A playback error on the watch page. Returns {action, resumeMs, recoverAtMs}:
//   "defer" — the app is in the background: the stall was almost certainly caused by the blank
//             itself, so reload on resume and don't spend the retry budget;
//   "fail"  — no video, a resolve already running, or the retry budget is spent: show the error;
//   "retry" — re-resolve and reload, resuming at resumeMs (the live position, else the pending
//             resume point). 5 s of clean playback past it (recoverAtMs) restores the budget.
function recoveryPlan(appActive, hasVideo, resolving, retries, maxRetries, positionMs, resumeMs) {
    if (!appActive)
        return { action: "defer" }
    if (!hasVideo || resolving || retries >= maxRetries)
        return { action: "fail" }
    var atMs = positionMs > 0 ? positionMs : resumeMs
    return { action: "retry", resumeMs: atMs > 0 ? atMs : 0, recoverAtMs: atMs + 5000 }
}

// The retry budget resets once playback has run clean past the recovery mark.
function recovered(retries, positionMs, recoverAtMs) {
    return retries > 0 && positionMs > recoverAtMs
}

// Opening a watch page for the video that is still playing in the background: adopt it as it is
// (no re-resolve, no restart). Needs the background player on this very video, its stashed resolve
// answer, and a pipeline that has actually started.
function canReattach(bgAudioActive, nowPlayingId, videoId, lastResolvedId, hasInfo, playing, positionMs) {
    return !!(bgAudioActive && nowPlayingId === videoId && lastResolvedId === videoId && hasInfo
              && (playing || positionMs > 0))
}

// A stacked watch page came back on top: take the shared player back when it has moved on to
// another video. Not on first show (still resolving / never played) and not for an info-only page.
function needsReclaim(pageActive, infoOnly, resolving, played, nowPlayingId, videoId) {
    return !!(pageActive && !infoOnly && !resolving && played && nowPlayingId !== videoId)
}

// Whole seconds for the watch history. A finished video is saved with its full length; the
// player's duration can be missing at the end, so the metadata length stands in for it.
function wholeSeconds(ms) {
    return Math.floor((ms || 0) / 1000)
}
function finishedSeconds(durationMs, metaSeconds) {
    var dur = wholeSeconds(durationMs)
    return dur > 0 ? dur : Math.floor(metaSeconds || 0)
}

// --- Links ------------------------------------------------------------------------------------

// The D-Bus openUrl argument: the URL dispatcher sometimes sends a one-element array of strings.
function dbusUrlArg(url) {
    if (typeof url === "string")
        return url
    return (url && url.length > 0) ? String(url[0]) : ""
}

// Where a classified link (engine parse_youtube_url) goes: {page: "video", id} | {page: "channel",
// ref, id} | {page: "playlist", ref} (saved to the library, then the library opens), or null.
function routeFor(res) {
    if (!res || !res.kind)
        return null
    if (res.kind === "video")
        return { page: "video", id: res.id || res.url }
    if (res.kind === "channel")
        return { page: "channel", ref: res.url, id: res.id || "" }
    if (res.kind === "playlist")
        return { page: "playlist", ref: res.id || res.url }
    return null
}

// An engine result as a toast: {ok, text}. text is "" when the engine gave none, and the caller
// shows its own translated fallback.
function resultMessage(res) {
    if (!res || !res.ok)
        return { ok: false, text: (res && res.error) ? res.error : "" }
    return { ok: true, text: res.summary || "" }
}

// --- Lists: feed, search, history, channel ---------------------------------------------------

// Whether a refreshed feed differs from what's shown (ids, in order). An unchanged feed is left
// alone so the user's scroll position isn't reset.
function feedChanged(shownIds, items) {
    if (shownIds.length !== items.length)
        return true
    for (var i = 0; i < items.length; i++)
        if (shownIds[i] !== items[i].id)
            return true
    return false
}

// Backfill for feed rows ([{id, duration}]): remove Shorts (when hidden) and fill in lengths.
// Returns [{index, remove: true} | {index, duration}] in DESCENDING index order, so applying them
// one by one never shifts a later index.
function feedBackfill(rows, durations, shorts, hideShorts) {
    var shortSet = {}
    var i
    if (hideShorts && shorts)
        for (i = 0; i < shorts.length; i++)
            shortSet["#" + shorts[i]] = true      // prefixed: an id like "constructor" can't false-hit
    var out = []
    for (i = rows.length - 1; i >= 0; i--) {
        var r = rows[i]
        if (shortSet["#" + r.id]) {
            out.push({ index: i, remove: true })
            continue
        }
        var d = durations ? durations[r.id] : undefined
        if (d !== undefined && d > 0 && r.duration !== d)
            out.push({ index: i, duration: d })
    }
    return out
}

// One search results page: drop items already shown (by id, else url). `seen` is the set of keys
// shown so far; returns {items, seen} with a new set. Items with no key are always kept.
function dedupPage(items, seen) {
    var next = {}, k
    for (k in seen) next[k] = seen[k]
    var out = []
    for (var i = 0; i < (items || []).length; i++) {
        var key = items[i].id || items[i].url || ""
        if (key.length > 0 && next["#" + key])
            continue
        if (key.length > 0)
            next["#" + key] = true
        out.push(items[i])
    }
    return { items: out, seen: next }
}

// Search paging: more pages exist when the engine says so, except that a later page adding
// nothing new means the results have run out (overlapping windows would page forever).
function searchHasMore(engineHasMore, start, added) {
    return !!engineHasMore && (start <= 1 || added > 0)
}

// History paging: a full page back means there may be another.
function fullPage(count, pageSize) {
    return count >= pageSize
}

// A playlist page opened on a saved YouTube playlist with no videos stored yet (just imported)
// fetches them once, so the user doesn't have to tap Refresh.
function shouldAutoRefreshPlaylist(kind, itemCount, alreadyRefreshed) {
    return kind === "youtube" && itemCount === 0 && !alreadyRefreshed
}

// A channel's first /videos page, when the RSS preview couldn't paint anything: open the channel's
// playlists instead when it has no Videos tab or no uploads. Returns "playlists", "error" or "".
function channelFirstPage(res, hadRss) {
    if (!res || !res.ok) {
        if (hadRss)
            return ""                          // keep the RSS list; scrolling can retry
        return (res && res.reason === "no_videos_tab") ? "playlists" : "error"
    }
    if (!hadRss && (res.items || []).length === 0)
        return "playlists"
    return ""
}

// --- Settings pages ---------------------------------------------------------------------------

// Default quality combo: index ↔ setting value ("0" = best available).
var QUALITY_VALUES = ["1080", "720", "480", "360", "0"]
function qualityIndex(value) {
    return Math.max(0, QUALITY_VALUES.indexOf(value))
}
function qualityValue(index) {
    return QUALITY_VALUES[index]
}

// SponsorBlock category combo: Off / Auto-skip / Show skip button.
function sbActionIndex(action) {
    return action === "skip" ? 1 : (action === "manual" ? 2 : 0)
}
function sbIndexAction(index) {
    return index === 1 ? "skip" : (index === 2 ? "manual" : "off")
}

// Equalizer presets (dB per band, 10 bands), in the order the page names them; Flat is 0.
var EQ_PRESETS = [
    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    [6, 5, 4, 2, 0, 0, 0, 0, 0, 0],
    [0, 0, 0, 0, 0, 0, 2, 4, 5, 6],
    [5, 3, -1, -2, -1, 1, 3, 4, 4, 4],
    [-1, 2, 4, 4, 2, 0, -1, -1, 0, 1],
    [-2, -1, 0, 2, 4, 4, 3, 2, 0, -1],
    [4, 3, 0, -2, -3, 0, 2, 3, 4, 5]
]
// Picking preset `index`: {bands, enable} — a curve other than Flat turns the equalizer on.
// null for an index out of range.
function eqPreset(index, eqEnabled) {
    if (index < 0 || index >= EQ_PRESETS.length)
        return null
    return { bands: EQ_PRESETS[index].slice(), enable: index !== 0 && !eqEnabled }
}

// The PO-token provider's status line: {state, tone}. state picks the text, tone the colour
// ("neutral" | "error" | "good"). `p` = {installing, deno, installed, enabled, responding, lastError}.
function potStatus(p) {
    var state
    if (p.installing) state = "installing"
    else if (!p.deno) state = "no_deno"
    else if (!p.installed) state = "not_installed"
    else if (!p.enabled) state = "off"
    else if (p.responding) state = "working"
    else if (p.lastError) state = "failing"
    else state = "starting"
    var tone
    if (p.installing) tone = "neutral"
    else if (!p.deno) tone = "error"
    else if (p.enabled && p.responding) tone = "good"
    else if (p.enabled && p.lastError) tone = "error"     // red only on a genuine error
    else tone = "neutral"
    return { state: state, tone: tone }
}

// --- Player controls --------------------------------------------------------------------------

// Translations matching the caption search filter (display name or language code).
function filterTranslations(translations, filter) {
    var f = (filter || "").toLowerCase()
    if (f === "")
        return translations
    var out = []
    for (var i = 0; i < (translations || []).length; i++) {
        var t = translations[i]
        if ((t.name || "").toLowerCase().indexOf(f) >= 0 || (t.lang || "").toLowerCase().indexOf(f) >= 0)
            out.push(t)
    }
    return out
}

// Name of the audio track playing now (matched by itag), "" if none.
function audioTrackName(tracks, itag) {
    for (var i = 0; i < (tracks || []).length; i++)
        if (tracks[i].itag === itag)
            return tracks[i].name
    return ""
}

// --- Formatting -------------------------------------------------------------------------------

function _pad2(n) {
    return (n < 10 ? "0" : "") + n
}
function _hms(s) {
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60)
    return (h > 0 ? h + ":" + _pad2(m) : "" + m) + ":" + _pad2(s % 60)
}

// A video length badge: "4:05", "1:02:03"; "" when unknown.
function fmtDur(sec) {
    if (!sec || sec <= 0)
        return ""
    return _hms(Math.floor(sec))
}

// The player clock from milliseconds: like fmtDur, but 0 / unknown reads "0:00".
function fmtClock(ms) {
    if (!ms || ms < 0)
        ms = 0
    return _hms(Math.floor(ms / 1000))
}

// 1.2K / 3.4M abbreviation for views and likes.
function fmtCount(n) {
    if (!n || n < 0)
        return "0"
    if (n >= 999950)                          // 999,950+ would round to "1000.0K"
        return (n / 1000000).toFixed(1).replace(".0", "") + "M"
    if (n >= 1000)
        return (n / 1000).toFixed(1).replace(".0", "") + "K"
    return "" + n
}

// Playback speed: "1×", "1.25×", "0.5×" — whole rates drop the decimal.
function fmtRate(r) {
    return (r === Math.floor(r) ? r.toFixed(0) : ("" + r)) + "×"
}
