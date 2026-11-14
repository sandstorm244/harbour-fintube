// Tests for qml/logic.js, the pure decisions the QML pages make. Run: node tests/qml_logic_test.js
// The lib is loaded the way QML sees it (".pragma library" stripped, no Node globals), and is first
// checked for syntax that Qt 5.6's ES5 engine on the phone would reject but Node accepts.
"use strict";
var fs = require("fs"), path = require("path"), vm = require("vm"), assert = require("assert");

var LIB = path.join(__dirname, "..", "qml", "logic.js");

function es5Problems(src) {
    var code = src.replace(/\/\*[\s\S]*?\*\//g, "")           // block comments
                  .replace(/"(?:[^"\\\n]|\\.)*"/g, '""')        // string contents
                  .replace(/'(?:[^'\\\n]|\\.)*'/g, "''")
                  .replace(/\/\/.*$/gm, "");                    // line comments
    var rules = [[/=>/, "arrow function"], [/\blet\s/, "let"], [/\bconst\s/, "const"],
                 [/`/, "template string"], [/\bclass\s/, "class"], [/\.\.\./, "spread"],
                 [/\bof\s/, "for...of"]];
    return rules.filter(function (r) { return r[0].test(code); })
                .map(function (r) { return r[1]; });
}

function load(file) {
    var src = fs.readFileSync(file, "utf8");
    var bad = es5Problems(src);
    if (bad.length)
        throw new Error(path.basename(file) + " uses syntax Qt 5.6 can't run: " + bad.join(", "));
    var ctx = {};
    vm.createContext(ctx);
    vm.runInContext(src.replace(/^\.pragma library.*$/m, ""), ctx, { filename: file });
    return ctx;
}

var L = load(LIB);
var tests = [], failed = 0;
function test(name, fn) { tests.push([name, fn]); }
var eq = assert.deepStrictEqual;
// Objects built inside the vm context have a different Object prototype; compare plain copies.
function plain(v) { return v === undefined ? v : JSON.parse(JSON.stringify(v)); }

// --- captions ---------------------------------------------------------------------------------
var tracks = [{ lang: "en", url: "e" }, { lang: "pt", url: "p" }];
var trans = [{ lang: "de", url: "tde" }, { lang: "es-419", url: "tes" }];

test("baseLang strips the region", function () {
    eq(L.baseLang("pt-BR"), "pt"); eq(L.baseLang("en-orig"), "en"); eq(L.baseLang(""), "");
    eq(L.baseLang(undefined), "");
});
test("pickCaption: nothing remembered picks nothing", function () {
    eq(L.pickCaption("", tracks, trans), null);
});
test("pickCaption: exact real track first", function () {
    eq(L.pickCaption("pt", tracks, trans).url, "p");
});
test("pickCaption: exact translation when no real track", function () {
    eq(L.pickCaption("de", tracks, trans).url, "tde");
});
test("pickCaption: real track beats translation on base match", function () {
    eq(L.pickCaption("pt-BR", tracks, [{ lang: "pt", url: "tpt" }]).url, "p");
});
test("pickCaption: base match on a translation", function () {
    eq(L.pickCaption("es", tracks, trans).url, "tes");
});
test("pickCaption: no match", function () {
    eq(L.pickCaption("ja", tracks, trans), null);
});

var cues = [{ start: 1, dur: 2, text: "a" }, { start: 4, dur: 1, text: "b" }];
test("captionAt: before, during, between, after", function () {
    eq(plain(L.captionAt(cues, 0.5, 0, -1)), { index: 0, text: "" });
    eq(plain(L.captionAt(cues, 1.5, 0, 0.5)), { index: 0, text: "a" });
    eq(plain(L.captionAt(cues, 3.5, 0, 1.5)), { index: 1, text: "" });
    eq(plain(L.captionAt(cues, 4.2, 1, 3.5)), { index: 1, text: "b" });
    eq(plain(L.captionAt(cues, 9, 1, 4.2)), { index: 2, text: "" });
});
test("captionAt: a backward seek rescans from the start", function () {
    eq(plain(L.captionAt(cues, 1.5, 2, 9)), { index: 0, text: "a" });
});
test("captionAt: no cues", function () {
    eq(plain(L.captionAt([], 3, 5, 1)), { index: 0, text: "" });
});

// --- SponsorBlock -----------------------------------------------------------------------------
var segs = [{ start: 10, end: 20, category: "sponsor" }, { start: 30, end: 40, category: "intro" }];
test("sbAction: unknown or missing is off", function () {
    eq(L.sbAction({ sponsor: "skip" }, "sponsor"), "skip");
    eq(L.sbAction({ sponsor: "bogus" }, "sponsor"), "off");
    eq(L.sbAction(undefined, "sponsor"), "off");
});
test("sponsorDecision: skip while playing", function () {
    var d = L.sponsorDecision(segs, 12, { sponsor: "skip" }, true);
    eq(d.skip.end, 20); eq(d.manual, null);
});
test("sponsorDecision: no auto-skip while paused", function () {
    eq(plain(L.sponsorDecision(segs, 12, { sponsor: "skip" }, false)), { skip: null, manual: null });
});
test("sponsorDecision: manual offers a button", function () {
    var d = L.sponsorDecision(segs, 31, { intro: "manual" }, true);
    eq(d.skip, null); eq(d.manual.category, "intro");
});
test("sponsorDecision: the last half second doesn't count (no skip loop)", function () {
    eq(L.sponsorDecision(segs, 19.6, { sponsor: "skip" }, true).skip, null);
    eq(L.sponsorDecision(segs, 20, { sponsor: "skip" }, true).skip, null);
});
test("sponsorDecision: an overlapping skip wins over manual", function () {
    var both = [{ start: 0, end: 50, category: "intro" }, { start: 5, end: 15, category: "sponsor" }];
    var d = L.sponsorDecision(both, 8, { intro: "manual", sponsor: "skip" }, true);
    eq(d.skip.category, "sponsor"); eq(d.manual, null);
});
test("sponsorDecision: off categories are ignored", function () {
    eq(plain(L.sponsorDecision(segs, 12, {}, true)), { skip: null, manual: null });
});

// --- seeking ----------------------------------------------------------------------------------
var idle = { active: false, zone: "", amount: 0, targetMs: 0 };
test("seekStep: first tap starts from the position", function () {
    eq(plain(L.seekStep(idle, "right", 50000, 600000)),
       { active: true, zone: "right", amount: 10, targetMs: 60000 });
});
test("seekStep: taps stack", function () {
    var s = L.seekStep(L.seekStep(idle, "left", 50000, 600000), "left", 0, 600000);
    eq(s.amount, -20); eq(s.targetMs, 30000);
});
test("seekStep: switching side restarts the count from the target", function () {
    var s = L.seekStep(L.seekStep(idle, "right", 50000, 600000), "left", 0, 600000);
    eq(s.zone, "left"); eq(s.amount, -10); eq(s.targetMs, 50000);
});
test("seekStep: clamped to 0 and to the duration", function () {
    eq(L.seekStep(idle, "left", 3000, 600000).targetMs, 0);
    eq(L.seekStep(idle, "right", 595000, 600000).targetMs, 600000);
    eq(L.seekStep(idle, "right", 595000, 0).targetMs, 605000);    // unknown duration: no cap
});

// --- playback plan ----------------------------------------------------------------------------
test("playPlan: dual carries both exts", function () {
    eq(plain(L.playPlan({ video_url: "v", audio_url: "a", video_ext: "mp4", audio_ext: "webm" })),
       { mode: "dual", videoUrl: "v", audioUrl: "a", videoExt: "mp4", audioExt: "webm" });
});
test("playPlan: progressive muxed clears the exts and the audio URL", function () {
    eq(plain(L.playPlan({ muxed_url: "m", muxed_proto: "https", video_ext: "mp4" })),
       { mode: "muxed", videoUrl: "m", audioUrl: "", videoExt: "", audioExt: "" });
});
test("playPlan: HLS goes to QtMultimedia", function () {
    eq(plain(L.playPlan({ muxed_url: "m3", muxed_proto: "m3u8_native" })), { mode: "hls", url: "m3" });
});
test("playPlan: video without audio falls to muxed, or none", function () {
    eq(L.playPlan({ video_url: "v", audio_url: "", muxed_url: "m" }).mode, "muxed");
    eq(L.playPlan({ video_url: "v" }).mode, "none");
    eq(L.playPlan(null).mode, "none");
});

// --- leaving the page, queue ------------------------------------------------------------------
test("keepPlayerOnLeave", function () {
    eq(L.keepPlayerOnLeave(true, true, true, 0), true);       // playing → background audio
    eq(L.keepPlayerOnLeave(true, true, false, 5000), true);   // paused mid-video → reattach later
    eq(L.keepPlayerOnLeave(true, true, false, 0), false);     // never started
    eq(L.keepPlayerOnLeave(false, true, true, 5000), false);  // background audio off
    eq(L.keepPlayerOnLeave(true, false, true, 5000), false);  // HLS page player
});
test("keepAwake: only a visibly playing video holds the display", function () {
    eq(L.keepAwake(true, true, true, false), true);
    eq(L.keepAwake(false, true, true, false), false);   // paused or ended
    eq(L.keepAwake(true, false, true, false), false);   // another page on top
    eq(L.keepAwake(true, true, false, false), false);   // app in the background
    eq(L.keepAwake(true, true, true, true), false);     // "playing" but the position stopped
});
test("awakeStalled: a frozen position lets go after the cap, never before", function () {
    eq(L.awakeStalled(0), false);
    eq(L.awakeStalled(L.AWAKE_STALL_MS - 1), false);
    eq(L.awakeStalled(L.AWAKE_STALL_MS), true);
    eq(L.awakeStalled(8 * 3600 * 1000), true);           // a whole night
});
test("nextQueueIndex", function () {
    var q = [{ id: "a" }, { id: "b" }, {}];
    eq(L.nextQueueIndex(q, 0), 1);
    eq(L.nextQueueIndex(q, 1), -1);      // next entry has no id
    eq(L.nextQueueIndex(q, 2), -1);
    eq(L.nextQueueIndex(null, 0), -1);
});

test("chapterAt", function () {
    var ch = [{ start: 0, title: "Intro" }, { start: 60, title: "Main" }];
    eq(L.chapterAt(ch, 30), "Intro"); eq(L.chapterAt(ch, 60), "Main");
    eq(L.chapterAt([{ start: 10, title: "X" }], 5), ""); eq(L.chapterAt([], 5), "");
});

// --- watch page: captions, recovery, reattach, history ----------------------------------------
test("captionPref: a track's language, Off is empty", function () {
    eq(L.captionPref({ lang: "de", url: "u" }), "de");
    eq(L.captionPref({ lang: "de" }), "");              // no URL = nothing to show
    eq(L.captionPref(null), "");
});
test("recoveryPlan: backgrounded defers without spending a retry", function () {
    eq(plain(L.recoveryPlan(false, true, false, 0, 3, 5000, 0)), { action: "defer" });
});
test("recoveryPlan: no video, resolving or budget spent fails", function () {
    eq(L.recoveryPlan(true, false, false, 0, 3, 5000, 0).action, "fail");
    eq(L.recoveryPlan(true, true, true, 0, 3, 5000, 0).action, "fail");
    eq(L.recoveryPlan(true, true, false, 3, 3, 5000, 0).action, "fail");
});
test("recoveryPlan: retry resumes at the live position, else the pending resume point", function () {
    eq(plain(L.recoveryPlan(true, true, false, 1, 3, 42000, 9000)),
       { action: "retry", resumeMs: 42000, recoverAtMs: 47000 });
    eq(plain(L.recoveryPlan(true, true, false, 0, 3, 0, 9000)),
       { action: "retry", resumeMs: 9000, recoverAtMs: 14000 });
    eq(plain(L.recoveryPlan(true, true, false, 0, 3, 0, 0)),
       { action: "retry", resumeMs: 0, recoverAtMs: 5000 });
});
test("recovered: budget resets only past the mark", function () {
    eq(L.recovered(1, 47001, 47000), true);
    eq(L.recovered(1, 46000, 47000), false);
    eq(L.recovered(0, 99000, 47000), false);
});
test("canReattach", function () {
    eq(L.canReattach(true, "v", "v", "v", true, true, 0), true);
    eq(L.canReattach(true, "v", "v", "v", true, false, 3000), true);    // paused mid-video
    eq(L.canReattach(true, "v", "v", "v", true, false, 0), false);      // never started
    eq(L.canReattach(false, "v", "v", "v", true, true, 0), false);      // no background audio
    eq(L.canReattach(true, "w", "v", "v", true, true, 0), false);       // another video playing
    eq(L.canReattach(true, "v", "v", "w", true, true, 0), false);       // stash is another video's
    eq(L.canReattach(true, "v", "v", "v", false, true, 0), false);      // nothing stashed
});
test("needsReclaim", function () {
    eq(L.needsReclaim(true, false, false, true, "other", "v"), true);
    eq(L.needsReclaim(true, false, false, true, "v", "v"), false);      // still ours
    eq(L.needsReclaim(false, false, false, true, "other", "v"), false); // not on top
    eq(L.needsReclaim(true, true, false, true, "other", "v"), false);   // info-only
    eq(L.needsReclaim(true, false, true, true, "other", "v"), false);   // still resolving
    eq(L.needsReclaim(true, false, false, false, "other", "v"), false); // first show
});
test("history seconds: whole seconds, finished falls back to the metadata length", function () {
    eq(L.wholeSeconds(61999), 61); eq(L.wholeSeconds(undefined), 0);
    eq(L.finishedSeconds(300500, 999), 300);
    eq(L.finishedSeconds(0, 245.7), 245);
    eq(L.finishedSeconds(0, undefined), 0);
});

// --- links ------------------------------------------------------------------------------------
test("dbusUrlArg: string or one-element array", function () {
    eq(L.dbusUrlArg("https://youtu.be/x"), "https://youtu.be/x");
    eq(L.dbusUrlArg(["https://youtu.be/x"]), "https://youtu.be/x");
    eq(L.dbusUrlArg([]), ""); eq(L.dbusUrlArg(null), "");
});
test("routeFor", function () {
    eq(plain(L.routeFor({ kind: "video", id: "abc", url: "u" })), { page: "video", id: "abc" });
    eq(plain(L.routeFor({ kind: "video", url: "u" })), { page: "video", id: "u" });
    eq(plain(L.routeFor({ kind: "channel", url: "cu", id: "UC1" })), { page: "channel", ref: "cu", id: "UC1" });
    eq(plain(L.routeFor({ kind: "channel", url: "cu" })), { page: "channel", ref: "cu", id: "" });
    eq(plain(L.routeFor({ kind: "playlist", id: "PL1", url: "pu" })), { page: "playlist", ref: "PL1" });
    eq(L.routeFor({ kind: "unknown" }), null); eq(L.routeFor({}), null); eq(L.routeFor(null), null);
});
test("resultMessage", function () {
    eq(plain(L.resultMessage({ ok: true, summary: "3 added" })), { ok: true, text: "3 added" });
    eq(plain(L.resultMessage({ ok: true })), { ok: true, text: "" });
    eq(plain(L.resultMessage({ ok: false, error: "boom" })), { ok: false, text: "boom" });
    eq(plain(L.resultMessage(null)), { ok: false, text: "" });
});

// --- lists ------------------------------------------------------------------------------------
test("feedChanged: length, order or ids", function () {
    var items = [{ id: "a" }, { id: "b" }];
    eq(L.feedChanged(["a", "b"], items), false);
    eq(L.feedChanged(["b", "a"], items), true);
    eq(L.feedChanged(["a"], items), true);
    eq(L.feedChanged(["a", "c"], items), true);
});
test("feedBackfill: removes Shorts, fills lengths, descending order", function () {
    var rows = [{ id: "a", duration: 0 }, { id: "s", duration: 0 }, { id: "b", duration: 30 },
                { id: "constructor", duration: 0 }];
    eq(plain(L.feedBackfill(rows, { a: 100, b: 30, s: 20 }, ["s"], true)),
       [{ index: 1, remove: true }, { index: 0, duration: 100 }]);
});
test("feedBackfill: Shorts stay when not hidden; zero or missing lengths ignored", function () {
    var rows = [{ id: "a", duration: 0 }, { id: "s", duration: 0 }];
    eq(plain(L.feedBackfill(rows, { a: 0, s: 20 }, ["s"], false)), [{ index: 1, duration: 20 }]);
    eq(plain(L.feedBackfill(rows, null, null, true)), []);
});
test("dedupPage: drops repeats across pages, keeps keyless items, doesn't touch the input", function () {
    var first = L.dedupPage([{ id: "a" }, { url: "u" }, { id: "a" }, { title: "keyless" }], {});
    eq(plain(first.items), [{ id: "a" }, { url: "u" }, { title: "keyless" }]);
    var before = JSON.stringify(first.seen);
    var second = L.dedupPage([{ id: "a" }, { id: "b" }, { id: "constructor" }], first.seen);
    eq(plain(second.items), [{ id: "b" }, { id: "constructor" }]);
    eq(JSON.stringify(first.seen), before);
    eq(plain(L.dedupPage(undefined, {}).items), []);
});
test("searchHasMore: a later page with nothing new ends paging", function () {
    eq(L.searchHasMore(true, 1, 0), true);
    eq(L.searchHasMore(true, 21, 0), false);
    eq(L.searchHasMore(true, 21, 3), true);
    eq(L.searchHasMore(false, 1, 20), false);
});
test("fullPage", function () { eq(L.fullPage(40, 40), true); eq(L.fullPage(39, 40), false); });
test("shouldAutoRefreshPlaylist: once, only for an empty YouTube playlist", function () {
    eq(L.shouldAutoRefreshPlaylist("youtube", 0, false), true);
    eq(L.shouldAutoRefreshPlaylist("youtube", 0, true), false);
    eq(L.shouldAutoRefreshPlaylist("youtube", 3, false), false);
    eq(L.shouldAutoRefreshPlaylist("local", 0, false), false);
});
test("channelFirstPage", function () {
    eq(L.channelFirstPage({ ok: false, reason: "no_videos_tab" }, false), "playlists");
    eq(L.channelFirstPage({ ok: false, error: "x" }, false), "error");
    eq(L.channelFirstPage(null, false), "error");
    eq(L.channelFirstPage({ ok: false, reason: "no_videos_tab" }, true), "");   // RSS already shown
    eq(L.channelFirstPage({ ok: true, items: [] }, false), "playlists");
    eq(L.channelFirstPage({ ok: true, items: [] }, true), "");
    eq(L.channelFirstPage({ ok: true, items: [{ id: "a" }] }, false), "");
});

// --- settings pages ---------------------------------------------------------------------------
test("quality combo maps both ways; unknown shows the first entry", function () {
    for (var i = 0; i < L.QUALITY_VALUES.length; i++)
        eq(L.qualityIndex(L.qualityValue(i)), i);
    eq(L.qualityValue(L.qualityIndex("720")), "720");
    eq(L.qualityIndex("4320"), 0);
    eq(L.qualityValue(4), "0");
});
test("SponsorBlock combo maps both ways", function () {
    ["off", "skip", "manual"].forEach(function (a) { eq(L.sbIndexAction(L.sbActionIndex(a)), a); });
    eq(L.sbActionIndex(undefined), 0);
});
test("eqPreset: copies the curve, a non-flat curve turns the equalizer on", function () {
    var p = L.eqPreset(1, false);
    eq(plain(p), { bands: [6, 5, 4, 2, 0, 0, 0, 0, 0, 0], enable: true });
    p.bands[0] = 99;
    eq(L.EQ_PRESETS[1][0], 6);
    eq(L.eqPreset(0, false).enable, false);              // Flat
    eq(L.eqPreset(2, true).enable, false);               // already on
    eq(L.eqPreset(7, false), null); eq(L.eqPreset(-1, false), null);
    L.EQ_PRESETS.forEach(function (c) { eq(c.length, 10); });
});
test("potStatus: every state and its tone", function () {
    function st(o) { return plain(L.potStatus(o)); }
    var ok = { deno: true, installed: true, enabled: true };
    eq(st({ installing: true }), { state: "installing", tone: "neutral" });
    eq(st({ deno: false }), { state: "no_deno", tone: "error" });
    eq(st({ deno: true }), { state: "not_installed", tone: "neutral" });
    eq(st({ deno: true, installed: true }), { state: "off", tone: "neutral" });
    eq(st({ deno: true, installed: true, enabled: true, responding: true }), { state: "working", tone: "good" });
    eq(st({ deno: true, installed: true, enabled: true, lastError: "x" }), { state: "failing", tone: "error" });
    eq(st(ok), { state: "starting", tone: "neutral" });
});

// --- player controls --------------------------------------------------------------------------
test("filterTranslations by name or code", function () {
    var t = [{ name: "German", lang: "de" }, { name: "Spanish (Latin America)", lang: "es-419" }];
    eq(plain(L.filterTranslations(t, "")), t);
    eq(plain(L.filterTranslations(t, "GER")), [t[0]]);
    eq(plain(L.filterTranslations(t, "419")), [t[1]]);
    eq(plain(L.filterTranslations(t, "zz")), []);
});
test("audioTrackName", function () {
    var a = [{ itag: "251", name: "English (original)" }, { itag: "250", name: "Polish" }];
    eq(L.audioTrackName(a, "250"), "Polish"); eq(L.audioTrackName(a, "1"), ""); eq(L.audioTrackName(null, "1"), "");
});

// --- formatting -------------------------------------------------------------------------------
test("fmtDur", function () {
    eq(L.fmtDur(0), ""); eq(L.fmtDur(-3), ""); eq(L.fmtDur(undefined), "");
    eq(L.fmtDur(5), "0:05"); eq(L.fmtDur(245.9), "4:05"); eq(L.fmtDur(3723), "1:02:03");
});
test("fmtClock", function () {
    eq(L.fmtClock(0), "0:00"); eq(L.fmtClock(-1), "0:00"); eq(L.fmtClock(65999), "1:05");
    eq(L.fmtClock(3600000), "1:00:00");
});
test("fmtCount", function () {
    eq(L.fmtCount(0), "0"); eq(L.fmtCount(-5), "0"); eq(L.fmtCount(999), "999");
    eq(L.fmtCount(1000), "1K"); eq(L.fmtCount(1250), "1.3K"); eq(L.fmtCount(15000), "15K");
    eq(L.fmtCount(999949), "999.9K"); eq(L.fmtCount(999950), "1M");
    eq(L.fmtCount(1000000), "1M"); eq(L.fmtCount(3400000), "3.4M");
});
test("fmtRate", function () {
    eq(L.fmtRate(1), "1×"); eq(L.fmtRate(1.25), "1.25×"); eq(L.fmtRate(0.5), "0.5×"); eq(L.fmtRate(2), "2×");
});

// --- the ES5 guard itself ---------------------------------------------------------------------
test("es5 guard catches ES6 and ignores comments and strings", function () {
    eq(es5Problems("var f = (a) => a"), ["arrow function"]);
    eq(es5Problems("// let x = 1\nvar s = 'const x'"), []);
});

tests.forEach(function (t) {
    try { t[1](); }
    catch (e) { failed++; console.log("FAIL: " + t[0] + "\n  " + String(e.message).split("\n").join("\n  ")); }
});
console.log((tests.length - failed) + "/" + tests.length + " QML logic tests passed");
process.exit(failed ? 1 : 0);
