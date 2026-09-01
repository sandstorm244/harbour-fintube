#!/usr/bin/env python3
"""Offline regression tests for FinTube's shared engine (youfish.py).

Runs with NO device, network, yt-dlp, or PO-token server — resolve()'s externals are all mocked,
so this exercises the pure format-selection + resolve wiring in isolation. Run:  python3 test_youfish.py

Property-based format selection (R93 video / R103 audio) kept regressing, so the selection helpers
and a mocked resolve() are pinned here. Notably the dub-instead-of-source audio pick (R105) is
covered so it can't come back. youfish.py imports `pyotherside` only inside functions, so
`import youfish` is safe here.
"""

import json
import os
import shutil
import sqlite3
import time
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import youfish  # noqa: E402


def vf(fid, height, vcodec, fps=30, url="v", note=None):
    """A video-only format."""
    f = {"format_id": fid, "height": height, "vcodec": vcodec, "acodec": "none",
         "fps": fps, "url": url, "http_headers": {"User-Agent": "UA"}}
    if note is not None:
        f["format_note"] = note
    return f


def af(fid, abr, acodec, url="a", note=None, lang_pref=None, lang=None):
    """An audio-only format."""
    f = {"format_id": fid, "abr": abr, "acodec": acodec, "vcodec": "none", "url": url,
         "http_headers": {"User-Agent": "UA"}}
    if note is not None:
        f["format_note"] = note
    if lang_pref is not None:
        f["language_preference"] = lang_pref
    if lang is not None:
        f["language"] = lang
    return f


def muxed(fid="18", url="m"):
    return {"format_id": fid, "height": 360, "vcodec": "avc1", "acodec": "mp4a.40.2",
            "url": url, "protocol": "https", "http_headers": {"User-Agent": "UA"}}


# --- pure helpers ----------------------------------------------------------------------------- #

class CodecFamily(unittest.TestCase):
    def test_video(self):
        self.assertEqual(youfish._codec_family("avc1.4d401f"), "h264")
        self.assertEqual(youfish._codec_family("vp09.00.10"), "vp9")
        self.assertEqual(youfish._codec_family("av01.0.08M"), "")   # AV1 undecodable here
        self.assertEqual(youfish._codec_family(None), "")

    def test_audio(self):
        self.assertEqual(youfish._audio_family("opus"), "opus")
        self.assertEqual(youfish._audio_family("mp4a.40.2"), "aac")
        self.assertEqual(youfish._audio_family("none"), "")
        self.assertEqual(youfish._audio_family(None), "")


class AudioOrigPref(unittest.TestCase):
    def test_language_preference_field_wins(self):
        self.assertEqual(youfish._audio_orig_pref({"language_preference": 10}), 10)
        self.assertEqual(youfish._audio_orig_pref({"language_preference": -1}), -1)

    def test_note_fallback(self):
        self.assertEqual(youfish._audio_orig_pref({"format_note": "English original (default)"}), 10)
        self.assertEqual(youfish._audio_orig_pref({"format_note": "English descriptive"}), -10)
        self.assertEqual(youfish._audio_orig_pref({"format_note": "Portuguese"}), 0)
        self.assertEqual(youfish._audio_orig_pref({}), 0)


class VideoCandidates(unittest.TestCase):
    def setUp(self):
        self._gs = youfish.get_settings
        youfish.get_settings = lambda: {"hw_decode": False}

    def tearDown(self):
        youfish.get_settings = self._gs

    def test_excludes_av1_and_over_max_height(self):
        fmts = [vf("137", 1080, "avc1"), vf("399", 1080, "av01"), vf("271", 1440, "vp09")]
        got = [f["format_id"] for f in youfish._video_candidates(fmts)]
        self.assertEqual(got, ["137"])   # av01 + 1440p dropped

    def test_sw_prefers_h264_hw_prefers_vp9(self):
        fmts = [vf("137", 1080, "avc1"), vf("248", 1080, "vp09")]
        self.assertEqual(youfish._video_candidates(fmts)[0]["format_id"], "137")
        youfish.get_settings = lambda: {"hw_decode": True}
        self.assertEqual(youfish._video_candidates(fmts)[0]["format_id"], "248")

    def test_sorted_by_height_desc(self):
        fmts = [vf("135", 480, "avc1"), vf("137", 1080, "avc1"), vf("136", 720, "avc1")]
        got = [f["height"] for f in youfish._video_candidates(fmts)]
        self.assertEqual(got, [1080, 720, 480])

    def test_pick_video_honours_cap(self):
        fmts = [vf("137", 1080, "avc1"), vf("136", 720, "avc1"), vf("135", 480, "avc1")]
        self.assertEqual(youfish._pick_video(fmts, 720)["height"], 720)      # capped
        self.assertEqual(youfish._pick_video(fmts, 0)["height"], 1080)       # uncapped = best
        self.assertEqual(youfish._pick_video(fmts, 240)["height"], 1080)     # cap below all -> best


class AudioCandidates(unittest.TestCase):
    def test_ladder_order_matches_old_hardcoded(self):
        fmts = [af("250", 70, "opus"), af("140", 128, "mp4a.40.2"), af("251", 160, "opus"),
                af("599", 31, "mp4a"), af("249", 50, "opus"), af("139", 48, "mp4a.40.5"),
                af("600", 35, "opus")]
        got = [f["format_id"] for f in youfish._audio_candidates(fmts)]
        self.assertEqual(got, ["251", "140", "250", "249", "139", "600", "599"])

    def test_source_beats_same_bitrate_dub(self):
        fmts = [af("251-3", 160, "opus", lang_pref=-1),    # dub
                af("251-0", 160, "opus", lang_pref=10)]    # source
        self.assertEqual(youfish._pick_audio(fmts)["format_id"], "251-0")

    def test_source_beats_higher_bitrate_dub(self):
        fmts = [af("251-3", 160, "opus", lang_pref=-1),        # dub, higher bitrate
                af("140-0", 128, "mp4a.40.2", lang_pref=10)]   # source, lower bitrate
        self.assertEqual(youfish._pick_audio(fmts)["format_id"], "140-0")

    def test_excludes_video_only_and_empty(self):
        self.assertEqual(youfish._audio_candidates([vf("137", 1080, "avc1")]), [])
        self.assertIsNone(youfish._pick_audio([vf("137", 1080, "avc1")]))


class AudioLangName(unittest.TestCase):
    def test_strips_bitrate_tier(self):
        self.assertEqual(youfish._audio_lang_name({"format_note": "German, low"}), "German")
        self.assertEqual(youfish._audio_lang_name({"format_note": "Portuguese, high"}), "Portuguese")

    def test_keeps_region_parens_drops_role_marker(self):
        self.assertEqual(youfish._audio_lang_name(
            {"format_note": "Chinese (Simplified), medium"}), "Chinese (Simplified)")
        self.assertEqual(youfish._audio_lang_name(
            {"format_note": "English original (default), low"}), "English")

    def test_falls_back_to_code_then_placeholder(self):
        self.assertEqual(youfish._audio_lang_name({"format_note": "", "language": "ja"}), "ja")
        self.assertEqual(youfish._audio_lang_name({}), "Audio")

    def test_bare_tier_note_never_masquerades_as_name(self):
        # A note that is ONLY a tier word (no comma) must not leak as the language name.
        self.assertEqual(youfish._audio_lang_name({"format_note": "medium", "language": "de"}), "de")
        self.assertEqual(youfish._audio_lang_name({"format_note": "low"}), "Audio")


class PickAudioPreferredLang(unittest.TestCase):
    def _dubs(self):
        return [af("251-en", 160, "opus", lang_pref=10, lang="en"),   # source/original
                af("251-pt", 158, "opus", lang_pref=-1, lang="pt"),
                af("251-es", 158, "opus", lang_pref=-1, lang="es-419")]

    def test_no_preference_picks_source(self):
        self.assertEqual(youfish._pick_audio(self._dubs())["format_id"], "251-en")

    def test_exact_preference_picks_that_dub(self):
        self.assertEqual(youfish._pick_audio(self._dubs(), "pt")["format_id"], "251-pt")

    def test_base_code_fallback(self):
        # remembered "es" matches the offered "es-419"
        self.assertEqual(youfish._pick_audio(self._dubs(), "es")["format_id"], "251-es")

    def test_missing_preference_falls_back_to_source(self):
        self.assertEqual(youfish._pick_audio(self._dubs(), "de")["format_id"], "251-en")


# --- resolve() smoke tests (externals mocked) ------------------------------------------------- #

class ResolveSmoke(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for name in ("_ytdlp_path", "_ensure_pot_server", "_pot_ytdlp_args",
                     "_yt_extractor_args", "_proxied", "get_settings"):
            self._saved[name] = getattr(youfish, name)
        self._saved["run"] = youfish.subprocess.run

        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._ensure_pot_server = lambda: True
        youfish._pot_ytdlp_args = lambda: []
        youfish._yt_extractor_args = lambda client_override=None: []
        youfish._proxied = lambda url, *a, **k: url
        youfish.get_settings = lambda: {"default_quality": 0, "hw_decode": False}

    def tearDown(self):
        for name, fn in self._saved.items():
            if name == "run":
                youfish.subprocess.run = fn
            else:
                setattr(youfish, name, fn)

    def _mock_ytdlp(self, formats, title="T"):
        data = {"title": title, "formats": formats, "duration": 100}
        def fake_run(cmd, **kwargs):
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        youfish.subprocess.run = fake_run

    def test_video_resolve_picks_source_audio_and_hd_video(self):
        self._mock_ytdlp([vf("137", 1080, "avc1"),
                          af("251-3", 160, "opus", lang_pref=-1),   # dub
                          af("251-0", 160, "opus", lang_pref=10),   # source
                          muxed()])
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["audio_itag"], "251-0")       # source, not the dub
        self.assertEqual(res["info"]["video_itag"], "137")
        self.assertTrue(res["info"]["qualities"])                  # quality menu populated

    def test_quality_menu_dedups_by_height(self):
        self._mock_ytdlp([vf("137", 1080, "avc1"), vf("248", 1080, "vp09"),
                          vf("136", 720, "avc1"), af("251", 160, "opus", lang_pref=10)])
        res = youfish.resolve("vid")
        heights = [q["label"] for q in res["info"]["qualities"]]
        self.assertEqual(heights, ["1080p", "720p"])               # 30fps, no-premium → one per res

    def test_quality_menu_surfaces_fps_and_premium(self):
        self._mock_ytdlp([
            vf("137", 1080, "avc1", 30),
            vf("299", 1080, "avc1", 60),                           # 1080p60 (free, 60fps source)
            vf("620", 1080, "avc1", 60, note="Premium"),           # enhanced-bitrate premium
            vf("136", 720, "avc1", 30),
            af("251", 160, "opus", lang_pref=10)])
        labels = [q["label"] for q in youfish.resolve("vid")["info"]["qualities"]]
        self.assertIn("1080p", labels)                             # 30fps rung still there
        self.assertIn("1080p60", labels)                           # 60fps its own row
        self.assertTrue(any("Premium" in l for l in labels))       # premium its own row
        self.assertEqual(labels[0], "1080p60 Premium")             # premium first, then higher fps
        self.assertEqual(labels[-1], "720p")                       # lowest resolution last

    def test_audio_tracks_one_per_language_original_first(self):
        # A dubbed video: two rungs each of English (source) + Portuguese; the picker collapses to
        # one entry per language, best rung, original/default first.
        self._mock_ytdlp([vf("137", 1080, "avc1"),
                          af("140-en", 129, "mp4a.40.2", lang_pref=10, lang="en",
                             note="English original (default), medium"),
                          af("251-en", 124, "opus", lang_pref=10, lang="en",
                             note="English original (default), medium"),
                          af("251-pt", 128, "opus", lang_pref=-1, lang="pt",
                             note="Portuguese, medium"),
                          af("249-pt", 48, "opus", lang_pref=-1, lang="pt",
                             note="Portuguese, low")])
        info = youfish.resolve("vid")["info"]
        tracks = info["audio_tracks"]
        self.assertEqual([t["lang"] for t in tracks], ["en", "pt"])   # original language first
        self.assertEqual(tracks[0]["name"], "English")
        self.assertTrue(tracks[0]["is_original"])
        self.assertEqual(tracks[1]["name"], "Portuguese")
        self.assertFalse(tracks[1]["is_original"])
        self.assertEqual(tracks[0]["itag"], "140-en")                 # best rung (129 aac > 124 opus)
        self.assertTrue(tracks[0]["audio_url"])
        self.assertEqual(info["audio_itag"], "140-en")               # started on the original

    def test_untagged_original_prepended_as_original(self):
        # A dubbed video whose SOURCE audio yt-dlp left untagged: it's what plays, so it must appear
        # (as "Original"), highlighted, even though it carries no language tag.
        self._mock_ytdlp([vf("137", 1080, "avc1"),
                          af("251-src", 160, "opus", lang_pref=10),   # original, NO language tag
                          af("251-pt", 158, "opus", lang_pref=-1, lang="pt",
                             note="Portuguese, medium")])
        info = youfish.resolve("vid")["info"]
        tracks = info["audio_tracks"]
        self.assertEqual([t["lang"] for t in tracks], ["", "pt"])     # original prepended, blank lang
        self.assertEqual(tracks[0]["itag"], "251-src")
        self.assertEqual(tracks[0]["name"], "Original")
        self.assertFalse(tracks[0]["is_original"])                    # no double "(original)" marker
        self.assertTrue(tracks[0]["audio_url"])
        self.assertEqual(info["audio_itag"], "251-src")              # playing track is highlightable

    def test_audio_tracks_empty_when_untagged(self):
        # A normal single-audio video has many audio rungs, all with NO language tag. None are a dub
        # choice, so audio_tracks stays empty (the UI hides the row) — never one bogus entry / rung.
        self._mock_ytdlp([vf("137", 1080, "avc1"),
                          af("139", 48, "mp4a.40.5"), af("249", 50, "opus"),
                          af("250", 70, "opus"), af("140", 128, "mp4a.40.2"),
                          af("251", 160, "opus")])
        info = youfish.resolve("vid")["info"]
        self.assertEqual(info["audio_tracks"], [])

    def test_resolve_honours_remembered_audio_lang(self):
        youfish.get_settings = lambda: {"default_quality": 0, "hw_decode": False,
                                        "audio_lang": "pt"}
        self._mock_ytdlp([vf("137", 1080, "avc1"),
                          af("251-en", 160, "opus", lang_pref=10, lang="en"),
                          af("251-pt", 158, "opus", lang_pref=-1, lang="pt")])
        info = youfish.resolve("vid")["info"]
        self.assertEqual(info["audio_itag"], "251-pt")               # started on the remembered dub

    def test_resolve_error_surfaces(self):
        def fail_run(cmd, **kwargs):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="Sign in to confirm")
        youfish.subprocess.run = fail_run
        res = youfish.resolve("vid")
        self.assertFalse(res.get("ok"))
        self.assertIn("Sign in", res.get("error", ""))


class BinaryResolution(unittest.TestCase):
    """Binary resolution after the 2026-08-28 reversal: a managed copy WINS, but with none present
    the app DOES fall back to a user/system yt-dlp/ffmpeg (PATH / ~/.local/bin / … via
    _system_binary) — it is no longer managed-only. Both halves are pinned so neither regresses.
    The extra FinTune-only candidate layers (_CANDIDATE_PATHS / _FINTUBE_DATA_DIR) are neutralised
    so the same test is deterministic in both apps."""
    def setUp(self):
        self._mo, self._mf, self._which = (youfish._managed_ytdlp, youfish._managed_ffmpeg,
                                           youfish.shutil.which)
        self._tmp = tempfile.mkdtemp(prefix="binres-")
        self._sys = {}
        for name in ("yt-dlp", "ffmpeg"):               # a discoverable user/system copy
            p = os.path.join(self._tmp, name)
            with open(p, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(p, 0o755)
            self._sys[name] = p
        self.which_calls = []

        def _spy(name):
            self.which_calls.append(name)
            return self._sys.get(name)                  # only yt-dlp/ffmpeg resolve; deno → None

        youfish.shutil.which = _spy
        youfish._managed_ytdlp = lambda: os.path.join(self._tmp, "absent", "yt-dlp")
        youfish._managed_ffmpeg = lambda: os.path.join(self._tmp, "absent", "ffmpeg")
        # Neutralise FinTune's extra fallback layers so both apps go straight managed → system.
        self._cand = getattr(youfish, "_CANDIDATE_PATHS", None)
        if self._cand is not None:
            youfish._CANDIDATE_PATHS = ()
        self._fdd = getattr(youfish, "_FINTUBE_DATA_DIR", None)
        if self._fdd is not None:
            youfish._FINTUBE_DATA_DIR = os.path.join(self._tmp, "no-fintube")

    def tearDown(self):
        youfish._managed_ytdlp, youfish._managed_ffmpeg, youfish.shutil.which = (
            self._mo, self._mf, self._which)
        if self._cand is not None:
            youfish._CANDIDATE_PATHS = self._cand
        if self._fdd is not None:
            youfish._FINTUBE_DATA_DIR = self._fdd
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_ytdlp_falls_back_to_system(self):
        # No managed copy, but a user/system copy is discoverable → resolved (NOT None), and the
        # system-fallback path was actually taken (which consulted for yt-dlp).
        self.assertEqual(youfish._ytdlp_path(), self._sys["yt-dlp"])
        self.assertIn("yt-dlp", self.which_calls)

    def test_ffmpeg_falls_back_to_system(self):
        self.assertEqual(youfish._ffmpeg_path(), self._sys["ffmpeg"])

    def test_managed_wins_over_system(self):
        managed = os.path.join(self._tmp, "managed-yt-dlp")
        with open(managed, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(managed, 0o755)
        youfish._managed_ytdlp = lambda: managed
        self.assertEqual(youfish._ytdlp_path(), managed)
        self.assertNotIn("yt-dlp", self.which_calls)    # managed short-circuits the fallback


class NewPipeImport(unittest.TestCase):
    """import_newpipe against a synthetic NewPipe backup: subscriptions, watch history + resume,
    local playlists, and bookmarked YouTube playlists — YouTube-only, merged, idempotent. The
    watch-history section is store-guarded, so this also passes in FinTune (no history store)."""
    def setUp(self):
        self._dd = youfish._data_dir
        self._tmp = tempfile.mkdtemp(prefix="npimp-")
        youfish._data_dir = lambda: self._tmp
        self.db = os.path.join(self._tmp, "newpipe.db")
        self._build(self.db)

    def tearDown(self):
        youfish._data_dir = self._dd
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _build(self, path):
        con = sqlite3.connect(path)
        c = con.cursor()
        c.execute("CREATE TABLE streams(uid INTEGER PRIMARY KEY, service_id INTEGER, url TEXT, "
                  "title TEXT, duration INTEGER, uploader TEXT, thumbnail_url TEXT)")
        c.execute("CREATE TABLE subscriptions(uid INTEGER PRIMARY KEY, service_id INTEGER, "
                  "url TEXT, name TEXT, avatar_url TEXT)")
        c.execute("CREATE TABLE stream_history(stream_id INTEGER, access_date INTEGER, "
                  "repeat_count INTEGER)")
        c.execute("CREATE TABLE stream_state(stream_id INTEGER PRIMARY KEY, progress_time INTEGER)")
        c.execute("CREATE TABLE playlists(uid INTEGER PRIMARY KEY, name TEXT, display_index INTEGER)")
        c.execute("CREATE TABLE playlist_stream_join(playlist_id INTEGER, stream_id INTEGER, "
                  "join_index INTEGER)")
        c.execute("CREATE TABLE remote_playlists(uid INTEGER PRIMARY KEY, service_id INTEGER, "
                  "name TEXT, url TEXT)")
        c.executemany("INSERT INTO streams(uid,service_id,url,title,duration,uploader,thumbnail_url)"
                      " VALUES(?,?,?,?,?,?,?)", [
                          (1, 0, "https://www.youtube.com/watch?v=AAAAAAAAAAA", "One", 600, "A", ""),
                          (2, 0, "https://youtu.be/BBBBBBBBBBB", "Two", 300, "B", ""),
                          (3, 1, "https://soundcloud.com/x", "SC", 100, "SC", ""),   # non-YT → dropped
                      ])
        c.executemany("INSERT INTO subscriptions(uid,service_id,url,name,avatar_url) "
                      "VALUES(?,?,?,?,?)", [
                          (1, 0, "https://www.youtube.com/channel/UC11111111111111111111", "A", ""),
                          (2, 0, "https://www.youtube.com/@handle", "H", ""),  # no id → skipped
                          (3, 1, "https://soundcloud.com/c", "SC", ""),        # non-YT → dropped
                      ])
        c.execute("INSERT INTO stream_state VALUES(1, 120000)")               # 20% → resume point
        c.execute("INSERT INTO stream_history VALUES(1, 1700000000000, 0)")
        c.execute("INSERT INTO playlists VALUES(1, 'Mix', 0)")
        c.executemany("INSERT INTO playlist_stream_join VALUES(?,?,?)", [(1, 2, 0), (1, 1, 1)])
        c.execute("INSERT INTO remote_playlists VALUES"
                  "(1, 0, 'Cool', 'https://www.youtube.com/playlist?list=PLxyz')")
        con.commit()
        con.close()

    def test_full_import_and_idempotent(self):
        res = youfish.import_newpipe(self.db)
        self.assertTrue(res["ok"])
        self.assertEqual(res["added"], 1)         # UC1 only (@handle skipped, SoundCloud dropped)
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(res["resume"], 1)        # stream 1 at 20%
        self.assertEqual(res["playlists"], 1)     # local "Mix"
        self.assertEqual(res["remote"], 1)        # bookmarked "Cool"
        has_history = hasattr(youfish, "_load_watch_history")
        self.assertEqual(res["history"], 1 if has_history else 0)
        pls = json.load(open(os.path.join(self._tmp, "playlists.json")))
        mix = next(p for p in pls if p["title"] == "Mix")
        self.assertEqual([it["id"] for it in mix["items"]], ["BBBBBBBBBBB", "AAAAAAAAAAA"])  # join order
        cool = next(p for p in pls if p["title"] == "Cool")
        self.assertEqual((cool["kind"], cool["yt_id"], len(cool["items"])), ("youtube", "PLxyz", 0))
        res2 = youfish.import_newpipe(self.db)    # nothing new on a second run
        self.assertEqual((res2["added"], res2["resume"], res2["playlists"], res2["remote"]),
                         (0, 0, 0, 0))

    def test_rejects_non_database(self):
        junk = os.path.join(self._tmp, "junk.db")
        with open(junk, "wb") as f:
            f.write(b"not a database")
        self.assertFalse(youfish.import_newpipe(junk)["ok"])


class PotTag(unittest.TestCase):
    """The PO-token provider version is no longer a hardcoded dead-end: a stored override wins,
    else the pinned known-good default. (The GitHub 'latest' lookup needs the network, so it
    isn't exercised offline here.)"""
    def setUp(self):
        self._gs = youfish.get_settings

    def tearDown(self):
        youfish.get_settings = self._gs

    def test_defaults_to_pinned_when_no_override(self):
        youfish.get_settings = lambda: {}
        self.assertEqual(youfish._pot_effective_tag(), youfish._POT_TAG)

    def test_override_wins(self):
        youfish.get_settings = lambda: {"pot_tag": "1.4.0"}
        self.assertEqual(youfish._pot_effective_tag(), "1.4.0")

    def test_blank_override_ignored(self):
        youfish.get_settings = lambda: {"pot_tag": "   "}
        self.assertEqual(youfish._pot_effective_tag(), youfish._POT_TAG)


class HideWatched(unittest.TestCase):
    """The hide-watched feed filter drops w=1 videos when the setting is on. Guard-aware: FinTune
    has no watch-history store, so the filter is a safe no-op there (still respects the setting)."""
    def setUp(self):
        self._gs = youfish.get_settings
        self._wh = getattr(youfish, "_load_watch_history", None)

    def tearDown(self):
        youfish.get_settings = self._gs
        if self._wh is not None:
            youfish._load_watch_history = self._wh

    def test_filters_when_on(self):
        youfish.get_settings = lambda: {"hide_watched": True}
        items = [{"id": "AAA"}, {"id": "BBB"}, {"id": "CCC"}]
        if hasattr(youfish, "_load_watch_history"):
            youfish._load_watch_history = lambda: {"AAA": {"w": 1}, "BBB": {"w": 0}}
            self.assertEqual(youfish._feed_hide_watched(items), [{"id": "BBB"}, {"id": "CCC"}])
        else:
            self.assertEqual(youfish._feed_hide_watched(items), items)   # no store → no-op

    def test_noop_when_off(self):
        youfish.get_settings = lambda: {"hide_watched": False}
        items = [{"id": "AAA"}, {"id": "BBB"}]
        self.assertEqual(youfish._feed_hide_watched(items), items)


class SetWatched(unittest.TestCase):
    """set_watched marks a video watched/unwatched from the long-press menu, writing the same
    store the History page + hide-watched read. FinTube-only (FinTune has no watch store)."""
    def setUp(self):
        if not hasattr(youfish, "set_watched"):
            self.skipTest("no watch-history store (FinTune)")
        self._dd = youfish._data_dir
        self._tmp = tempfile.mkdtemp(prefix="setw-")
        youfish._data_dir = lambda: self._tmp

    def tearDown(self):
        if hasattr(self, "_dd"):
            youfish._data_dir = self._dd
            shutil.rmtree(self._tmp, ignore_errors=True)

    def test_mark_watched_creates_entry(self):
        res = youfish.set_watched("AAAAAAAAAAA", True, "Title", "Chan")
        self.assertEqual(res, {"ok": True, "watched": 1})
        e = youfish._load_watch_history()["AAAAAAAAAAA"]
        self.assertEqual(e["w"], 1)
        self.assertEqual(e["f"], 1.0)            # no prior progress → reads as fully played
        self.assertEqual(e["ti"], "Title")
        self.assertEqual(e["ch"], "Chan")

    def test_explicit_unwatch_clears_sticky_flag(self):
        youfish.set_watched("BBBBBBBBBBB", True)
        res = youfish.set_watched("BBBBBBBBBBB", False)
        self.assertEqual(res["watched"], 0)
        self.assertEqual(youfish._load_watch_history()["BBBBBBBBBBB"]["w"], 0)

    def test_unwatch_clears_the_progress_bar(self):
        youfish.record_watch("HHHHHHHHHHH", 300, 600)   # 50% → red bar at 0.5
        youfish.set_watched("HHHHHHHHHHH", True)         # → f=1.0 (full bar)
        youfish.set_watched("HHHHHHHHHHH", False)        # → bar cleared
        e = youfish._load_watch_history()["HHHHHHHHHHH"]
        self.assertEqual(e["w"], 0)
        self.assertEqual(e["f"], 0.0)                    # red bar gone
        self.assertEqual(e["p"], 0)

    def test_unwatch_unknown_is_noop(self):
        res = youfish.set_watched("CCCCCCCCCCC", False)
        self.assertEqual(res, {"ok": True, "watched": 0})
        self.assertNotIn("CCCCCCCCCCC", youfish._load_watch_history())   # no stub entry created

    def test_promote_marks_complete_and_keeps_meta(self):
        youfish.record_watch("DDDDDDDDDDD", 60, 600, "Vid", "By")        # 10% → not watched
        self.assertEqual(youfish._load_watch_history()["DDDDDDDDDDD"]["w"], 0)
        youfish.set_watched("DDDDDDDDDDD", True)                          # promote to watched
        e = youfish._load_watch_history()["DDDDDDDDDDD"]
        self.assertEqual(e["w"], 1)
        self.assertEqual(e["f"], 1.0)           # red bar reads complete
        self.assertEqual(e["p"], 600)           # position moved to the end
        self.assertEqual(e["d"], 600)           # duration preserved
        self.assertEqual(e["ti"], "Vid")        # meta preserved
        self.assertEqual(e["ch"], "By")

    def test_watched_id_is_hidden_by_hide_watched(self):
        youfish.set_watched("EEEEEEEEEEE", True)
        self.assertIn("EEEEEEEEEEE", youfish._watched_ids())

    def test_marking_watched_clears_resume_point(self):
        youfish.set_position("FFFFFFFFFFF", 120)
        self.assertEqual(youfish.get_position("FFFFFFFFFFF"), 120)
        youfish.set_watched("FFFFFFFFFFF", True)
        self.assertEqual(youfish.get_position("FFFFFFFFFFF"), 0)      # seek memory forgotten

    def test_unwatch_leaves_resume_point(self):
        youfish.record_watch("GGGGGGGGGGG", 90, 600)                  # entry + resume=90
        youfish.set_watched("GGGGGGGGGGG", False)                     # unwatch keeps the resume point
        self.assertEqual(youfish.get_position("GGGGGGGGGGG"), 90)


class FeedShortsAndDurations(unittest.TestCase):
    """Persistent duration + Shorts-membership caches and the feed Shorts filter. Shared engine, so
    this passes in both apps (FinTune has the same functions, just no QML consumer)."""
    def setUp(self):
        self._dd, self._gs, self._ls = youfish._data_dir, youfish.get_settings, youfish.list_subscriptions
        self._fd, self._fc = youfish._feed_durations_cache, youfish._feed_cache
        self._tmp = tempfile.mkdtemp(prefix="feeddur-")
        youfish._data_dir = lambda: self._tmp
        youfish._feed_durations_cache = {"ts": 0.0, "map": {}, "shorts": set()}
        youfish._feed_cache = {"ts": 0.0, "items": []}

    def tearDown(self):
        youfish._data_dir, youfish.get_settings, youfish.list_subscriptions = self._dd, self._gs, self._ls
        youfish._feed_durations_cache, youfish._feed_cache = self._fd, self._fc
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_durations_cache_roundtrip(self):
        youfish._save_durations({"a": 10, "b": 200})
        self.assertEqual(youfish._load_saved_durations(), {"a": 10, "b": 200})

    def test_shorts_cache_roundtrip(self):
        youfish._save_shorts({"x", "y"})
        self.assertEqual(youfish._load_saved_shorts(), {"x", "y"})

    def test_shorts_filter_drops_known_shorts(self):
        youfish.get_settings = lambda: {"hide_shorts": True}
        youfish._feed_durations_cache["shorts"] = {"S1"}
        items = [{"id": "V1"}, {"id": "S1"}, {"id": "V2"}]
        self.assertEqual(youfish._feed_shorts_filter(items), [{"id": "V1"}, {"id": "V2"}])

    def test_shorts_filter_noop_when_off(self):
        youfish.get_settings = lambda: {"hide_shorts": False}
        youfish._feed_durations_cache["shorts"] = {"S1"}
        items = [{"id": "V1"}, {"id": "S1"}]
        self.assertEqual(youfish._feed_shorts_filter(items), items)

    def test_feed_durations_all_classified_is_instant(self):
        cid = "UC00000000000000000000AA"                      # fully-classified feed → no yt-dlp needed
        youfish.list_subscriptions = lambda: [{"id": cid, "url": "", "name": "A"}]
        youfish._feed_cache = {"ts": 0.0, "items": [
            {"id": "LONG", "channel_id": cid},
            {"id": "OTHER", "channel_id": cid},
            {"id": "SHORT", "channel_id": cid},
        ]}
        youfish._feed_durations_cache = {"ts": 0.0, "map": {"LONG": 300, "OTHER": 42}, "shorts": {"SHORT"}}
        res = youfish.feed_durations()
        self.assertEqual(res["durations"], {"LONG": 300, "OTHER": 42})   # SHORT has no /videos duration
        self.assertEqual(res["shorts"], ["SHORT"])

    def test_feed_with_durations_fills_cached_lengths(self):
        youfish._feed_durations_cache = {"ts": 0.0, "map": {"A": 120}, "shorts": set()}
        items = [{"id": "A", "duration": 0}, {"id": "C", "duration": 0}]
        out = youfish._feed_with_durations(items)
        self.assertEqual(out[0]["duration"], 120)   # cached → shows immediately with the feed
        self.assertEqual(out[1]["duration"], 0)      # uncached → filled later by feed_durations


class ResumeIdleTTL(unittest.TestCase):
    """Resume points carry a last-touched stamp, expire after _RESUME_TTL idle, and old int-format
    entries are grandfathered. Shared engine → passes in both apps."""
    def setUp(self):
        self._dd = youfish._data_dir
        self._tmp = tempfile.mkdtemp(prefix="pos-")
        youfish._data_dir = lambda: self._tmp

    def tearDown(self):
        youfish._data_dir = self._dd
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_set_get_roundtrip_and_forget(self):
        youfish.set_position("AAAAAAAAAAA", 120)
        self.assertEqual(youfish.get_position("AAAAAAAAAAA"), 120)
        youfish.set_position("AAAAAAAAAAA", 0)
        self.assertEqual(youfish.get_position("AAAAAAAAAAA"), 0)

    def test_stale_entry_expires_on_read(self):
        old = time.time() - (youfish._RESUME_TTL + 3600)
        with open(youfish._positions_path(), "w") as f:
            json.dump({"OLD": {"s": 90, "t": old}}, f)
        self.assertEqual(youfish.get_position("OLD"), 0)          # idle past TTL → gone

    def test_stale_swept_on_write(self):
        old = time.time() - (youfish._RESUME_TTL + 3600)
        with open(youfish._positions_path(), "w") as f:
            json.dump({"OLD": {"s": 90, "t": old}, "KEEP": {"s": 30, "t": time.time()}}, f)
        youfish.set_position("NEW", 50)
        d = youfish._load_positions()
        self.assertNotIn("OLD", d)                                # swept
        self.assertIn("KEEP", d)
        self.assertEqual(youfish.get_position("NEW"), 50)

    def test_old_int_format_grandfathered(self):
        with open(youfish._positions_path(), "w") as f:
            json.dump({"LEGACY": 77}, f)                          # pre-change plain-int format
        self.assertEqual(youfish.get_position("LEGACY"), 77)      # still readable
        youfish.set_position("OTHER", 10)                         # a write migrates LEGACY
        self.assertEqual(youfish._load_positions()["LEGACY"]["s"], 77)   # migrated + kept


class HistoryLimitSetting(unittest.TestCase):
    """Watch history is capped at the history_limit setting (FinTube-only store)."""
    def setUp(self):
        if not hasattr(youfish, "record_watch"):
            self.skipTest("no watch-history store (FinTune)")
        self._dd, self._gs = youfish._data_dir, youfish.get_settings
        self._tmp = tempfile.mkdtemp(prefix="histlim-")
        youfish._data_dir = lambda: self._tmp

    def tearDown(self):
        if hasattr(self, "_dd"):
            youfish._data_dir, youfish.get_settings = self._dd, self._gs
            shutil.rmtree(self._tmp, ignore_errors=True)

    def test_history_capped_at_setting(self):
        youfish.get_settings = lambda: {"history_limit": 50}     # 50 = the clamp floor
        for i in range(53):
            youfish.record_watch("VID%08d" % i, 30, 600)         # 5% → not watched, just recorded
        d = youfish._load_watch_history()
        self.assertEqual(len(d), 50)                             # capped to the setting
        self.assertIn("VID00000052", d)                         # newest kept
        self.assertNotIn("VID00000000", d)                      # oldest dropped

    def test_history_limit_clamped(self):
        youfish.get_settings = lambda: {"history_limit": 999999}
        self.assertEqual(youfish._history_limit(), 5000)         # clamp upper
        youfish.get_settings = lambda: {"history_limit": 1}
        self.assertEqual(youfish._history_limit(), 50)           # clamp lower


class CaptionTracks(unittest.TestCase):
    """_caption_tracks splits yt-dlp caption data into the short real list + the big
    auto-translate set; the ASR base (no tlang) is a real track, tlang entries are not."""
    def _fmts(self, url, name=None, ext="json3"):
        f = {"ext": ext, "url": url}
        if name is not None:
            f["name"] = name
        return [f]

    def test_manual_and_asr_are_tracks_translations_are_split(self):
        data = {
            "subtitles": {"en": self._fmts("http://x/en.json3?fmt=json3", "English")},
            "automatic_captions": {
                "en": self._fmts("http://x/asr.json3?fmt=json3&kind=asr", "English (auto)"),
                "de": self._fmts("http://x/t.json3?fmt=json3&tlang=de", "German"),
                "fr": self._fmts("http://x/t.json3?fmt=json3&tlang=fr", "French"),
            },
        }
        tracks, translations = youfish._caption_tracks(data)
        kinds = {t["lang"]: t["kind"] for t in tracks}
        self.assertEqual(kinds, {"en": "manual"})                    # manual wins the 'en' slot
        self.assertEqual({t["lang"] for t in translations}, {"de", "fr"})
        self.assertTrue(all("tlang=" in t["url"] for t in translations))

    def test_asr_kept_when_no_manual_for_that_lang(self):
        data = {"automatic_captions": {
            "es": self._fmts("http://x/asr.json3?fmt=json3", "Spanish"),        # ASR, no tlang
            "en": self._fmts("http://x/t.json3?fmt=json3&tlang=en", "English"), # translation
        }}
        tracks, translations = youfish._caption_tracks(data)
        self.assertEqual([(t["lang"], t["kind"]) for t in tracks], [("es", "asr")])
        self.assertEqual([t["lang"] for t in translations], ["en"])

    def test_non_json3_and_live_chat_ignored(self):
        data = {
            "subtitles": {
                "en": self._fmts("http://x/en.vtt", "English", ext="vtt"),   # no json3 -> skip
                "live_chat": self._fmts("http://x/lc.json3?fmt=json3"),      # not a subtitle
            },
            "automatic_captions": {},
        }
        tracks, translations = youfish._caption_tracks(data)
        self.assertEqual(tracks, [])
        self.assertEqual(translations, [])

    def test_translation_dropped_when_a_real_track_covers_that_lang(self):
        # YouTube offers auto-translate to EVERY language, so a manual 'ja' still gets a
        # translated 'ja' — the real track wins and the translation is dropped (real-data case).
        data = {
            "subtitles": {"ja": self._fmts("http://x/ja.json3?fmt=json3", "Japanese")},
            "automatic_captions": {
                "ja": self._fmts("http://x/t.json3?fmt=json3&tlang=ja", "Japanese"),
                "de": self._fmts("http://x/t.json3?fmt=json3&tlang=de", "German"),
            },
        }
        tracks, translations = youfish._caption_tracks(data)
        self.assertEqual([t["lang"] for t in tracks], ["ja"])
        self.assertEqual([t["lang"] for t in translations], ["de"])   # 'ja' translation dropped

    def test_translations_sorted_by_name(self):
        data = {"automatic_captions": {
            "z": self._fmts("http://x?tlang=z", "Zulu"),
            "a": self._fmts("http://x?tlang=a", "Afrikaans"),
        }}
        _, translations = youfish._caption_tracks(data)
        self.assertEqual([t["name"] for t in translations], ["Afrikaans", "Zulu"])


class ParseJson3(unittest.TestCase):
    """_parse_json3 turns json3 events into non-overlapping cues and tames rolling ASR."""
    def test_empty_and_whitespace_events_dropped(self):
        data = {"events": [
            {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 1000, "dDurationMs": 1000, "segs": [{"utf8": "hi"}]},
        ]}
        cues = youfish._parse_json3(data)
        self.assertEqual([c["text"] for c in cues], ["hi"])

    def test_multiple_segs_joined_and_newlines_collapsed(self):
        data = {"events": [{"tStartMs": 0, "dDurationMs": 2000,
                            "segs": [{"utf8": "hello"}, {"utf8": "\nworld"}]}]}
        cues = youfish._parse_json3(data)
        self.assertEqual(cues[0]["text"], "hello world")

    def test_overlapping_cues_clamped_to_next_start(self):
        data = {"events": [
            {"tStartMs": 0, "dDurationMs": 5000, "segs": [{"utf8": "a"}]},   # would run to 5s
            {"tStartMs": 2000, "dDurationMs": 2000, "segs": [{"utf8": "b"}]},
        ]}
        cues = youfish._parse_json3(data)
        self.assertAlmostEqual(cues[0]["dur"], 2.0, places=2)               # clamped 5->2s
        self.assertAlmostEqual(cues[1]["start"], 2.0, places=2)

    def test_non_dict_body_returns_empty_not_crash(self):
        # A valid-JSON but non-object timedtext body (null / [] / a number) must not raise.
        for body in (None, [], 42, "text"):
            self.assertEqual(youfish._parse_json3(body), [])

    def test_repeated_line_merged_not_stacked(self):
        data = {"events": [
            {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "same"}]},
            {"tStartMs": 1000, "dDurationMs": 1000, "segs": [{"utf8": "same"}]},
            {"tStartMs": 2000, "dDurationMs": 1000, "segs": [{"utf8": "next"}]},
        ]}
        cues = youfish._parse_json3(data)
        self.assertEqual([c["text"] for c in cues], ["same", "next"])       # one 'same', extended
        self.assertAlmostEqual(cues[0]["start"], 0.0, places=2)
        self.assertAlmostEqual(cues[0]["dur"], 2.0, places=2)               # 0->2s merged


class SubscriptionFeed(unittest.TestCase):
    """subscription_feed is built from each channel's yt-dlp /videos tab (YouTube retired RSS):
    merged newest-first, durations inline, members-only dropped, cached briefly."""
    def setUp(self):
        self._saved = {n: getattr(youfish, n)
                       for n in ("_ytdlp_path", "list_subscriptions", "get_settings", "_data_dir")}
        self._run = youfish.subprocess.run
        self._cache, self._state = youfish._feed_cache, youfish._feed_state
        self._open = youfish.urllib.request.urlopen
        self._tmp = tempfile.mkdtemp(prefix="feed-")
        youfish._feed_cache = {}                        # per-channel: {cid: {"ts", "rows"}}
        youfish._feed_state = {"loaded": False}
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish.get_settings = lambda: {"hide_watched": False}
        youfish._data_dir = lambda: self._tmp          # disk feed-cache writes into a tempdir
        # RSS fallback network is inert by default (raises) so tests stay offline; a test that
        # exercises the fallback overrides urlopen itself.
        def _no_rss(req, timeout=0):
            raise youfish.urllib.error.URLError("no rss in test")
        youfish.urllib.request.urlopen = _no_rss

    def tearDown(self):
        for n, f in self._saved.items():
            setattr(youfish, n, f)
        youfish.subprocess.run = self._run
        youfish._feed_cache, youfish._feed_state = self._cache, self._state
        youfish.urllib.request.urlopen = self._open
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _mock(self, channels):
        """channels: {channel_id: [flat-entry, ...]}; fake_run dispatches on the URL's channel id."""
        def fake_run(cmd, **kw):
            url = cmd[-1]
            cid = next((c for c in channels if c in url), None)
            data = {"channel": cid, "entries": channels.get(cid, [])}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        youfish.subprocess.run = fake_run

    def test_merged_newest_first_durations_inline(self):
        now = int(time.time())
        self._mock({
            "UC_AAA": [{"id": "aaaaaaaaaa1", "title": "A new", "timestamp": now - 3600, "duration": 120},
                       {"id": "aaaaaaaaaa2", "title": "A old", "timestamp": now - 9 * 86400, "duration": 200}],
            "UC_BBB": [{"id": "bbbbbbbbbb1", "title": "B mid", "timestamp": now - 2 * 86400, "duration": 300}],
        })
        youfish.list_subscriptions = lambda: [{"id": "UC_AAA"}, {"id": "UC_BBB"}]
        res = youfish.subscription_feed(force=True)
        self.assertTrue(res["ok"])
        self.assertEqual([i["title"] for i in res["items"]], ["A new", "B mid", "A old"])  # cross-channel sort
        self.assertEqual(res["items"][0]["duration"], 120)          # duration inline, no 2nd pass
        self.assertEqual(res["items"][0]["channel_id"], "UC_AAA")

    def test_members_only_dropped(self):
        now = int(time.time())
        self._mock({"UC_AAA": [
            {"id": "okokokokok1", "title": "ok", "timestamp": now, "duration": 10},
            {"id": "memememem01", "title": "members", "timestamp": now, "duration": 10,
             "availability": "subscriber_only"}]})
        youfish.list_subscriptions = lambda: [{"id": "UC_AAA"}]
        res = youfish.subscription_feed(force=True)
        self.assertEqual([i["title"] for i in res["items"]], ["ok"])

    def test_cache_hit_skips_ytdlp(self):
        now = int(time.time())
        self._mock({"UC_AAA": [{"id": "cccccccccc1", "title": "c", "timestamp": now, "duration": 10}]})
        youfish.list_subscriptions = lambda: [{"id": "UC_AAA"}]
        youfish.subscription_feed(force=True)                       # populate cache
        calls = [0]
        prev = youfish.subprocess.run
        def counting(cmd, **kw):
            calls[0] += 1
            return prev(cmd, **kw)
        youfish.subprocess.run = counting
        res = youfish.subscription_feed(force=False)
        self.assertTrue(res.get("cached"))
        self.assertEqual(calls[0], 0)                               # served from cache, no spawn

    def test_no_subs_is_empty(self):
        youfish.list_subscriptions = lambda: []
        self.assertEqual(youfish.subscription_feed(force=True), {"ok": True, "items": []})

    def test_channel_failure_contributes_nothing(self):
        # yt-dlp fails AND the RSS fallback fails (urlopen raises, per setUp) → nothing from UC_BAD.
        now = int(time.time())
        def fake_run(cmd, **kw):
            if "UC_BAD" in cmd[-1]:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="gone")
            data = {"channel": "UC_OK", "entries":
                    [{"id": "okokokokok1", "title": "ok", "timestamp": now, "duration": 10}]}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        youfish.subprocess.run = fake_run
        youfish.list_subscriptions = lambda: [{"id": "UC_BAD"}, {"id": "UC_OK"}]
        res = youfish.subscription_feed(force=True)
        self.assertEqual([i["title"] for i in res["items"]], ["ok"])  # bad channel dropped, feed still built

    def test_rss_fallback_when_ytdlp_returns_nothing(self):
        # yt-dlp yields nothing for the channel → fall back to its RSS feed (dur/live absent).
        self._mock({"UC_R": []})              # yt-dlp returns an empty tab
        rss = ('<feed><entry><yt:videoId>rssvid00001</yt:videoId><title>RSS vid</title>'
               '<published>2026-08-30T12:00:00+00:00</published>'
               '<author><name>RSS Chan</name></author>'
               '<media:statistics views="42"/></entry></feed>')
        def fake_open(req, timeout=0):
            import io
            class R(io.BytesIO):
                def __enter__(s): return s
                def __exit__(s, *a): return False
            return R(rss.encode())
        youfish.urllib.request.urlopen = fake_open
        youfish.list_subscriptions = lambda: [{"id": "UC_R"}]
        res = youfish.subscription_feed(force=True)
        self.assertEqual([i["title"] for i in res["items"]], ["RSS vid"])
        it = res["items"][0]
        self.assertEqual(it["duration"], 0)          # RSS has no duration
        self.assertEqual(it["live"], 0)              # …nor live status
        self.assertEqual(it["views"], 42)

    def test_disk_cache_survives_worker_restart(self):
        # A cold launch (fresh in-memory cache) hydrates the per-channel cache from disk and shows
        # it WITHOUT spawning yt-dlp — the stale-while-revalidate win.
        now = int(time.time())
        self._mock({"UC_D": [{"id": "diskvid0001", "title": "disk", "timestamp": now, "duration": 9}]})
        youfish.list_subscriptions = lambda: [{"id": "UC_D"}]
        youfish.subscription_feed(force=True)                 # build + persist to disk
        self.assertTrue(os.path.exists(youfish._feed_cache_path()))
        youfish._feed_cache = {}                              # simulate an app relaunch...
        youfish._feed_state = {"loaded": False}               # ...forcing a fresh hydrate from disk
        calls = [0]
        prev = youfish.subprocess.run
        def counting(cmd, **kw):
            calls[0] += 1
            return prev(cmd, **kw)
        youfish.subprocess.run = counting
        res = youfish.subscription_feed(force=False)
        self.assertTrue(res.get("cached"))
        self.assertEqual([i["title"] for i in res["items"]], ["disk"])   # served from disk
        self.assertEqual(calls[0], 0)                         # no yt-dlp spawn on cold launch

    def test_stale_flag_set_when_a_channel_is_due(self):
        now = int(time.time())
        self._mock({"UC_S": [{"id": "stalevid001", "title": "s", "timestamp": now, "duration": 9}]})
        youfish.list_subscriptions = lambda: [{"id": "UC_S"}]
        youfish.subscription_feed(force=True)                 # fresh, hot channel → not due
        self.assertFalse(youfish.subscription_feed(force=False)["stale"])
        youfish._feed_cache["UC_S"]["ts"] = time.time() - 3600   # age past the 10-min hot TTL
        res = youfish.subscription_feed(force=False)
        self.assertTrue(res["cached"] and res["stale"])       # served but flagged for bg refresh

    def test_background_refresh_skips_dormant_fetches_hot(self):
        now = int(time.time())
        self._mock({
            "UC_HOT": [{"id": "hotvideo001", "title": "hot", "timestamp": now, "duration": 9}],
            "UC_DORM": [{"id": "dormvideo01", "title": "dorm", "timestamp": now - 90 * 86400, "duration": 9}],
        })
        youfish.list_subscriptions = lambda: [{"id": "UC_HOT"}, {"id": "UC_DORM"}]
        youfish.subscription_feed(force=True)                 # build both
        # 40 min later: HOT (10-min TTL) is due, DORMANT (24-h TTL) is not.
        for cid in ("UC_HOT", "UC_DORM"):
            youfish._feed_cache[cid]["ts"] = time.time() - 40 * 60
        fetched = []
        prev = youfish.subprocess.run
        def spy(cmd, **kw):
            fetched.append(next((c for c in ("UC_HOT", "UC_DORM") if c in cmd[-1]), "?"))
            return prev(cmd, **kw)
        youfish.subprocess.run = spy
        youfish.subscription_feed(force=True)                 # background refresh (refresh_all=False)
        self.assertEqual(fetched, ["UC_HOT"])                 # only the hot channel re-fetched

    def test_refresh_all_refetches_even_dormant(self):
        now = int(time.time())
        self._mock({"UC_DORM": [{"id": "dormvideo01", "title": "d", "timestamp": now - 90 * 86400, "duration": 9}]})
        youfish.list_subscriptions = lambda: [{"id": "UC_DORM"}]
        youfish.subscription_feed(force=True)                 # build (dormant, not due)
        calls = [0]
        prev = youfish.subprocess.run
        def counting(cmd, **kw):
            calls[0] += 1
            return prev(cmd, **kw)
        youfish.subprocess.run = counting
        youfish.subscription_feed(force=True, refresh_all=True)   # pull-to-refresh
        self.assertEqual(calls[0], 1)                         # dormant re-fetched anyway

    def test_failed_refresh_keeps_last_good(self):
        now = int(time.time())
        self._mock({"UC_K": [{"id": "keepvideo01", "title": "keep", "timestamp": now, "duration": 9}]})
        youfish.list_subscriptions = lambda: [{"id": "UC_K"}]
        youfish.subscription_feed(force=True)                 # cache a good entry
        youfish._feed_cache["UC_K"]["ts"] = time.time() - 3600   # make it due
        # Now both sources fail for this channel (yt-dlp rc!=0, RSS raises per setUp).
        youfish.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="x")
        res = youfish.subscription_feed(force=True)
        self.assertEqual([i["title"] for i in res["items"]], ["keep"])   # last-good retained, not blanked

    def test_empty_channel_not_refetched_every_refresh(self):
        # A channel that returns nothing is stamped so it retries hourly, not on every refresh
        # (else it'd thrash yt-dlp and pin the feed 'stale' forever).
        self._mock({"UC_E": []})                              # yt-dlp empty; RSS also fails (setUp)
        youfish.list_subscriptions = lambda: [{"id": "UC_E"}]
        youfish.subscription_feed(force=True)                 # first fetch → empty, but stamped
        self.assertIn("UC_E", youfish._feed_cache)
        self.assertFalse(youfish.subscription_feed(force=False)["stale"])   # not perpetually stale
        calls = [0]
        prev = youfish.subprocess.run
        def counting(cmd, **kw):
            calls[0] += 1
            return prev(cmd, **kw)
        youfish.subprocess.run = counting
        youfish.subscription_feed(force=True)                 # a refresh moments later
        self.assertEqual(calls[0], 0)                         # within the 1-h retry window → skipped

    def test_live_stream_pinned_to_top(self):
        now = int(time.time())
        self._mock({"UC_L": [
            {"id": "olduploadd01", "title": "old upload", "timestamp": now - 30 * 86400, "duration": 100},
            {"id": "livestream01", "title": "LIVE now", "live_status": "is_live"},   # no timestamp
        ]})
        youfish.list_subscriptions = lambda: [{"id": "UC_L"}]
        res = youfish.subscription_feed(force=True)
        self.assertEqual([i["title"] for i in res["items"]], ["LIVE now", "old upload"])  # live pinned top
        self.assertEqual(res["items"][0]["live"], 1)


class CaptionCuesCache(unittest.TestCase):
    """caption_cues fetches once, caches on success, and leaves failures retryable."""
    def setUp(self):
        youfish._caption_cache.clear()
        self._open = youfish.urllib.request.urlopen
        self.calls = 0

    def tearDown(self):
        youfish.urllib.request.urlopen = self._open
        youfish._caption_cache.clear()

    def _mock(self, payload, fail=False):
        import io
        def fake(req, timeout=0):
            self.calls += 1
            if fail:
                raise youfish.urllib.error.URLError("boom")
            class R(io.BytesIO):
                def __enter__(s): return s
                def __exit__(s, *a): return False
            return R(json.dumps(payload).encode())
        youfish.urllib.request.urlopen = fake

    def test_success_is_cached(self):
        self._mock({"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "hi"}]}]})
        r1 = youfish.caption_cues("http://x/a")
        r2 = youfish.caption_cues("http://x/a")
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertEqual(r1["cues"], r2["cues"])
        self.assertEqual(self.calls, 1)                 # second call served from cache

    def test_failure_not_cached_and_retryable(self):
        self._mock(None, fail=True)
        r = youfish.caption_cues("http://x/b")
        self.assertFalse(r["ok"])
        self.assertNotIn("http://x/b", youfish._caption_cache)
        self.assertEqual(self.calls, 1)                 # a later call would try again

    def test_empty_url(self):
        self.assertEqual(youfish.caption_cues(""), {"ok": False, "cues": []})


# --- YouTube login (cookies) + subscription import ------------------------------------------- #

def _fake_ytm(text):
    """Install a fake `ytm` module exposing netscape_cookies() → text, for the engine's lazy
    `import ytm`. Returns the previous sys.modules entry (or None) so the caller can restore it."""
    prev = sys.modules.get("ytm")
    m = types.ModuleType("ytm")
    m.netscape_cookies = lambda: text
    sys.modules["ytm"] = m
    return prev


class CookiesArgs(unittest.TestCase):
    def setUp(self):
        self._prev = sys.modules.get("ytm")

    def tearDown(self):
        if self._prev is not None:
            sys.modules["ytm"] = self._prev
        else:
            sys.modules.pop("ytm", None)

    def test_signed_out_yields_empty(self):
        _fake_ytm("")                               # netscape_cookies() == "" → no --cookies
        with youfish._cookies_args() as cargs:
            self.assertEqual(cargs, [])

    def test_signed_in_yields_ephemeral_file_removed_after(self):
        _fake_ytm("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tv\n")
        with youfish._cookies_args() as cargs:
            self.assertEqual(cargs[0], "--cookies")
            path = cargs[1]
            self.assertTrue(os.path.exists(path))   # exists during the with
            with open(path) as f:
                self.assertIn("SAPISID", f.read())
        self.assertFalse(os.path.exists(path))      # and is removed after


class ImportYoutubeSubs(unittest.TestCase):
    def setUp(self):
        self._ytdlp = youfish._ytdlp_path
        self._run = youfish.subprocess.run
        self._ls = youfish.list_subscriptions
        self._save = youfish._save_subscriptions
        self._prev_ytm = sys.modules.get("ytm")
        self._store = []
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish.list_subscriptions = lambda: list(self._store)
        youfish._save_subscriptions = lambda subs: self._store.__setitem__(slice(None), subs)
        _fake_ytm("# ck\n")                          # signed in

    def tearDown(self):
        youfish._ytdlp_path = self._ytdlp
        youfish.subprocess.run = self._run
        youfish.list_subscriptions = self._ls
        youfish._save_subscriptions = self._save
        if self._prev_ytm is not None:
            sys.modules["ytm"] = self._prev_ytm
        else:
            sys.modules.pop("ytm", None)

    def _mock_feed(self, entries):
        data = {"entries": entries}
        youfish.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
            returncode=0, stdout=json.dumps(data), stderr="")

    def test_maps_dedupes_and_saves(self):
        self._store[:] = [{"id": "UChave", "name": "Have", "url": "", "thumbnail": ""}]
        self._mock_feed([
            {"id": "UCnew1", "url": "https://www.youtube.com/channel/UCnew1",
             "title": "New One", "thumbnails": [{"url": "t1"}]},
            {"id": "UChave", "url": "x", "title": "Have"},                       # already subscribed
            {"url": "https://www.youtube.com/channel/UCfromurl", "title": "URL Only"},  # id from url
            None,                                                                # malformed
        ])
        res = youfish.import_youtube_subscriptions()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["added"], 2)                # UCnew1 + UCfromurl (UChave deduped)
        ids = [s["id"] for s in self._store]
        self.assertEqual(ids, ["UChave", "UCnew1", "UCfromurl"])
        new1 = [s for s in self._store if s["id"] == "UCnew1"][0]
        self.assertEqual(new1["name"], "New One")
        self.assertEqual(new1["thumbnail"], "t1")
        self.assertEqual(new1["url"], "https://www.youtube.com/channel/UCnew1")

    def test_signed_out_is_a_clear_error(self):
        _fake_ytm("")                                    # not signed in
        self._mock_feed([{"id": "UCx", "title": "X"}])
        res = youfish.import_youtube_subscriptions()
        self.assertFalse(res["ok"])
        self.assertIn("signed in", res["error"].lower())

    def test_ytdlp_failure_surfaces(self):
        youfish.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
            returncode=1, stdout="", stderr="feed error")
        res = youfish.import_youtube_subscriptions()
        self.assertFalse(res["ok"])


class ImportYoutubePlaylists(unittest.TestCase):
    def setUp(self):
        self._ytdlp = youfish._ytdlp_path
        self._run = youfish.subprocess.run
        self._lp = youfish._load_playlists
        self._sp = youfish._save_playlists
        self._prev_ytm = sys.modules.get("ytm")
        self._store = []
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._load_playlists = lambda: list(self._store)
        youfish._save_playlists = lambda lst: self._store.__setitem__(slice(None), lst)
        _fake_ytm("# ck\n")

    def tearDown(self):
        youfish._ytdlp_path = self._ytdlp
        youfish.subprocess.run = self._run
        youfish._load_playlists = self._lp
        youfish._save_playlists = self._sp
        if self._prev_ytm is not None:
            sys.modules["ytm"] = self._prev_ytm
        else:
            sys.modules.pop("ytm", None)

    def _mock_feed(self, entries):
        youfish.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
            returncode=0, stdout=json.dumps({"entries": entries}), stderr="")

    def test_maps_dedupes_and_stores_empty_youtube_entries(self):
        self._store[:] = [{"id": "aaa", "title": "Old", "kind": "youtube",
                           "yt_id": "PLhave", "items": []}]
        self._mock_feed([
            {"id": "PLnew", "url": "https://www.youtube.com/playlist?list=PLnew", "title": "New PL"},
            {"id": "PLhave", "title": "Old"},                                  # dupe by yt_id
            {"url": "https://www.youtube.com/playlist?list=LL", "title": "Liked"},  # id from url
            None,
        ])
        res = youfish.import_youtube_playlists()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["added"], 2)                       # PLnew + LL (PLhave deduped)
        yt_ids = [p["yt_id"] for p in self._store]
        self.assertIn("PLnew", yt_ids)
        self.assertIn("LL", yt_ids)
        newpl = [p for p in self._store if p["yt_id"] == "PLnew"][0]
        self.assertEqual(newpl["kind"], "youtube")
        self.assertEqual(newpl["items"], [])                    # lazy — fetched on open
        self.assertEqual(newpl["title"], "New PL")

    def test_skips_channel_id_rows(self):
        # feed/playlists should only list playlists; if a channel-id (UC…) row slips in, it must
        # NOT be stored as a bogus playlist (opening it would build playlist?list=UC… and fail).
        self._mock_feed([
            {"id": "UCnotaplaylist", "title": "Not a playlist"},
            {"id": "PLreal", "title": "Real"},
        ])
        res = youfish.import_youtube_playlists()
        self.assertEqual(res["added"], 1)
        self.assertEqual([p["yt_id"] for p in self._store], ["PLreal"])

    def test_signed_out_is_a_clear_error(self):
        _fake_ytm("")
        self._mock_feed([{"id": "PLx", "title": "X"}])
        res = youfish.import_youtube_playlists()
        self.assertFalse(res["ok"])
        self.assertIn("signed in", res["error"].lower())


class ImportYoutubeAccount(unittest.TestCase):
    def setUp(self):
        self._ytdlp = youfish._ytdlp_path
        self._run = youfish.subprocess.run
        self._ls = youfish.list_subscriptions
        self._save_s = youfish._save_subscriptions
        self._lp = youfish._load_playlists
        self._sp = youfish._save_playlists
        self._prev_ytm = sys.modules.get("ytm")
        self._subs, self._pls = [], []
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish.list_subscriptions = lambda: list(self._subs)
        youfish._save_subscriptions = lambda s: self._subs.__setitem__(slice(None), s)
        youfish._load_playlists = lambda: list(self._pls)
        youfish._save_playlists = lambda p: self._pls.__setitem__(slice(None), p)
        _fake_ytm("# ck\n")

        def fake(cmd, **kw):
            u = cmd[-1]                                          # the url is the last argv element
            if "feed/channels" in u:
                entries = [{"id": "UCa", "title": "Chan A",
                            "url": "https://www.youtube.com/channel/UCa"}]
            elif "feed/playlists" in u:
                entries = [{"id": "PLa", "title": "PL A",
                            "url": "https://www.youtube.com/playlist?list=PLa"}]
            else:
                entries = []
            return types.SimpleNamespace(returncode=0,
                                         stdout=json.dumps({"entries": entries}), stderr="")
        youfish.subprocess.run = fake

    def tearDown(self):
        youfish._ytdlp_path = self._ytdlp
        youfish.subprocess.run = self._run
        youfish.list_subscriptions = self._ls
        youfish._save_subscriptions = self._save_s
        youfish._load_playlists = self._lp
        youfish._save_playlists = self._sp
        if self._prev_ytm is not None:
            sys.modules["ytm"] = self._prev_ytm
        else:
            sys.modules.pop("ytm", None)

    def test_imports_both_and_combines_summary(self):
        res = youfish.import_youtube_account()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["subs_added"], 1)
        self.assertEqual(res["playlists_added"], 1)
        self.assertIn("subscription", res["summary"])
        self.assertIn("playlist", res["summary"])
        self.assertEqual([s["id"] for s in self._subs], ["UCa"])
        self.assertEqual([p["yt_id"] for p in self._pls], ["PLa"])

    def test_reimport_says_nothing_new(self):
        # Both halves succeed but add nothing (already imported) → clean "Nothing new" summary,
        # not "Imported 0 subscriptions and 0 playlists."
        self._subs[:] = [{"id": "UCa", "name": "Chan A", "url": "", "thumbnail": ""}]
        self._pls[:] = [{"id": "x", "title": "PL A", "kind": "youtube", "yt_id": "PLa", "items": []}]
        res = youfish.import_youtube_account()
        self.assertTrue(res["ok"])
        self.assertEqual(res["subs_added"], 0)
        self.assertEqual(res["playlists_added"], 0)
        self.assertEqual(res["summary"], "Nothing new to import.")

    def test_signed_out_fails_cleanly(self):
        _fake_ytm("")
        res = youfish.import_youtube_account()
        self.assertFalse(res["ok"])
        self.assertIn("signed in", res["error"].lower())
        self.assertEqual(res["subs_added"], 0)          # always present, even on the fail path
        self.assertEqual(res["playlists_added"], 0)


class YtmCookieModule(unittest.TestCase):
    def setUp(self):
        import ytm
        self.ytm = ytm
        self._dd = ytm._data_dir
        self._rj = ytm._read_cookie_jar
        self._paths = ytm._BROWSER_COOKIE_PATHS
        self._tmp = tempfile.mkdtemp()
        # Patch _data_dir (which _cookies_path derives from AND _save_cookies uses for its temp), so
        # the atomic os.replace stays within one filesystem instead of crossing /tmp <-> the real dir.
        ytm._data_dir = lambda: self._tmp

    def tearDown(self):
        self.ytm._data_dir = self._dd
        self.ytm._read_cookie_jar = self._rj
        self.ytm._BROWSER_COOKIE_PATHS = self._paths
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_netscape_from_rows_format(self):
        rows = [("SAPISID", "v", ".youtube.com", "/", 123, 1),
                ("X", "y", "youtube.com", "", 0, 0)]
        txt = self.ytm._netscape_from_rows(rows)
        self.assertTrue(txt.startswith("# Netscape HTTP Cookie File"))
        self.assertIn("\t".join([".youtube.com", "TRUE", "/", "TRUE", "123", "SAPISID", "v"]), txt)
        self.assertIn("\t".join(["youtube.com", "FALSE", "/", "FALSE", "0", "X", "y"]), txt)  # ""→"/"

    def test_status_roundtrip_and_logout(self):
        self.assertFalse(self.ytm.login_status()["logged_in"])
        self.assertEqual(self.ytm.netscape_cookies(), "")
        self.ytm._save_cookies({"ytdlp": "# ck\n", "count": 5,
                                "imported_at": 111, "source": "/jar"})
        st = self.ytm.login_status()
        self.assertTrue(st["logged_in"])
        self.assertEqual(st["count"], 5)
        self.assertEqual(self.ytm.netscape_cookies(), "# ck\n")
        self.ytm.logout()
        self.assertFalse(self.ytm.login_status()["logged_in"])
        self.assertEqual(self.ytm.netscape_cookies(), "")

    def test_import_rejects_jar_without_session(self):
        # cookies present but no login marker (no SAPISID/*PSID) → clear "not signed in" error.
        self.ytm._read_cookie_jar = lambda p: [("PREF", "x", ".youtube.com", "/", 0, 0)]
        self.ytm._BROWSER_COOKIE_PATHS = [self._tmp + "/cookies.sqlite"]
        open(self._tmp + "/cookies.sqlite", "w").close()   # make the candidate path exist
        res = self.ytm.import_browser_login()
        self.assertFalse(res["ok"])
        self.assertIn("signed-in", res["error"].lower())


class CommentsThreads(unittest.TestCase):
    """comments() nests replies under their parent thread and bounds the reply walk."""
    def setUp(self):
        self._path = youfish._ytdlp_path
        self._run = youfish.subprocess.run
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"

    def tearDown(self):
        youfish._ytdlp_path = self._path
        youfish.subprocess.run = self._run

    def _mock(self, payload):
        self.captured = {}
        def fake_run(cmd, **kw):
            self.captured["cmd"] = cmd
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        youfish.subprocess.run = fake_run

    def _xargs(self):
        cmd = self.captured["cmd"]
        return cmd[cmd.index("--extractor-args") + 1]

    def test_replies_nest_under_parent_in_order(self):
        self._mock({"comment_count": 99, "comments": [
            {"id": "a", "parent": "root", "text": "top A", "author": "Ann"},
            {"id": "a.1", "parent": "a", "text": "reply A1", "author": "Bo"},
            {"id": "a.2", "parent": "a", "text": "reply A2", "author": "Cy"},
            {"id": "b", "parent": "root", "text": "top B", "author": "Di"},
        ]})
        res = youfish.comments("vid")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["total"], 99)
        self.assertEqual([c["text"] for c in res["comments"]], ["top A", "top B"])
        a = res["comments"][0]
        self.assertEqual([r["text"] for r in a["replies"]], ["reply A1", "reply A2"])
        self.assertEqual(a["reply_count"], 2)
        self.assertEqual(res["comments"][1]["replies"], [])   # B has no replies
        self.assertEqual(res["comments"][1]["reply_count"], 0)

    def test_reply_count_prefers_youtube_total_when_larger(self):
        # yt-dlp reports 40 replies exist but we only fetched 1 (budget) → show the real 40.
        self._mock({"comments": [
            {"id": "a", "parent": "root", "text": "t", "reply_count": 40},
            {"id": "a.1", "parent": "a", "text": "r"},
        ]})
        c = youfish.comments("vid")["comments"][0]
        self.assertEqual(len(c["replies"]), 1)
        self.assertEqual(c["reply_count"], 40)

    def test_with_replies_flag_shapes_extractor_args(self):
        self._mock({"comments": []})
        youfish.comments("vid", limit=50, with_replies=True)
        on = self._xargs()
        self.assertIn("max_comments=%d,50,%d,%d"
                      % (50 + youfish._REPLY_BUDGET, youfish._REPLY_BUDGET,
                         youfish._REPLIES_PER_THREAD), on)
        youfish.comments("vid", limit=50, with_replies=False)
        self.assertIn("max_comments=50,50,0,0", self._xargs())

    def test_error_return(self):
        def fail(cmd, **kw):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="nope")
        youfish.subprocess.run = fail
        res = youfish.comments("vid")
        self.assertFalse(res["ok"])
        self.assertIn("nope", res["error"])


class VideoInfo(unittest.TestCase):
    """video_info() returns metadata even for an unplayable (formats-less) video."""
    def setUp(self):
        self._path = youfish._ytdlp_path
        self._run = youfish.subprocess.run
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"

    def tearDown(self):
        youfish._ytdlp_path = self._path
        youfish.subprocess.run = self._run

    def _mock(self, payload):
        youfish.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
            returncode=0, stdout=json.dumps(payload), stderr="")

    def test_metadata_and_chapters_no_formats(self):
        self._mock({
            "title": "Hello", "uploader": "Chan", "channel_id": "UC1",
            "channel_url": "https://youtube.com/@chan", "description": "desc here",
            "duration": 212, "view_count": 12345, "like_count": 678,
            "upload_date": "20260101", "thumbnail": "http://t/x.jpg",
            "chapters": [{"start_time": 0, "title": "Intro"},
                         {"start_time": 60, "title": "Part 2"},
                         {"title": "no-start drop"}],
            # deliberately NO "formats" → resolve() would fail, video_info() must not.
        })
        res = youfish.video_info("vid")
        self.assertTrue(res["ok"], res)
        info = res["info"]
        self.assertEqual(info["title"], "Hello")
        self.assertEqual(info["uploader"], "Chan")
        self.assertEqual(info["view_count"], 12345)
        self.assertEqual(info["like_count"], 678)
        self.assertEqual(info["upload_date"], "20260101")
        self.assertEqual(len(info["chapters"]), 2)          # the no-start chapter is dropped
        self.assertEqual(info["chapters"][1]["title"], "Part 2")

    def test_error_return(self):
        youfish.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
            returncode=1, stdout="", stderr="blocked")
        res = youfish.video_info("vid")
        self.assertFalse(res["ok"])
        self.assertIn("blocked", res["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
