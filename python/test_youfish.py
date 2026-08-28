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
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import youfish  # noqa: E402


def vf(fid, height, vcodec, fps=30, url="v"):
    """A video-only format."""
    return {"format_id": fid, "height": height, "vcodec": vcodec, "acodec": "none",
            "fps": fps, "url": url, "http_headers": {"User-Agent": "UA"}}


def af(fid, abr, acodec, url="a", note=None, lang_pref=None):
    """An audio-only format."""
    f = {"format_id": fid, "abr": abr, "acodec": acodec, "vcodec": "none", "url": url,
         "http_headers": {"User-Agent": "UA"}}
    if note is not None:
        f["format_note"] = note
    if lang_pref is not None:
        f["language_preference"] = lang_pref
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
        self.assertEqual(heights, ["1080p", "720p"])               # one rung per resolution

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
