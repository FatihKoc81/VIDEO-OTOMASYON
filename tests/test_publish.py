import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import publish as pb
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import video_pipeline as vp


class Resp:
    def __init__(self, data=None, status=200, headers=None):
        self._d, self.status_code, self.headers = data or {}, status, headers or {}
    def json(self):
        return self._d


class FakeHttp:
    """Ağsız sahte istemci: (yöntem, url parçası) → yanıt; çağrılar kaydedilir."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []
    def _go(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, part), resp in self.routes.items():
            if m == method and part in url:
                return resp(kw) if callable(resp) else resp
        raise AssertionError("beklenmeyen istek " + method + " " + url)
    def post(self, url, **kw): return self._go("POST", url, **kw)
    def put(self, url, **kw): return self._go("PUT", url, **kw)
    def get(self, url, **kw): return self._go("GET", url, **kw)


class PublishTest(unittest.TestCase):
    def setUp(self):
        pb.time.sleep = lambda s: None
        self.tmp = tempfile.TemporaryDirectory()
        self.video = Path(self.tmp.name) / "v.mp4"
        self.video.write_bytes(b"x" * 1000)

    def tearDown(self):
        self.tmp.cleanup()

    def test_configured_and_targets(self):
        env = {"TIKTOK_CLIENT_KEY": "k", "TIKTOK_CLIENT_SECRET": "s", "TIKTOK_REFRESH_TOKEN": "r", "IG_ACCESS_TOKEN": " "}
        self.assertEqual(pb.configured(env), ["tiktok"])
        env.update({"YT_CLIENT_ID": "a", "YT_CLIENT_SECRET": "b", "YT_REFRESH_TOKEN": "c"})
        self.assertEqual(pb.wanted(env), ["tiktok", "youtube"])
        self.assertEqual(pb.wanted({**env, "PUBLISH_TARGETS": "youtube, instagram"}), ["youtube"])

    def test_silence_is_refused(self):
        self.assertEqual(pb.parse_mean_volume("mean_volume: -inf dB"), (False, -999.0))
        self.assertFalse(pb.parse_mean_volume("mean_volume: -72.1 dB")[0])
        self.assertTrue(pb.parse_mean_volume("[Parsed_volumedetect_0] mean_volume: -21.4 dB")[0])
        self.assertFalse(pb.parse_mean_volume("")[0])

    def test_texts_and_chunks(self):
        t = pb.texts({"title": "Uzayın <sesi>", "caption": "Merak et #uzay #bilim"})
        self.assertTrue(t["yt_title"].endswith("#Shorts"))
        self.assertNotIn("<", t["yt_title"])
        self.assertEqual(t["yt_tags"], ["uzay", "bilim"])
        self.assertIn("yapay zekâ", t["tiktok"])
        self.assertEqual(pb.tiktok_chunks(1000), (1000, 1))
        size = 100 * 1024 * 1024
        chunk, n = pb.tiktok_chunks(size)
        self.assertEqual(n, 10)
        self.assertLessEqual(size - chunk * (n - 1), 128 * 1024 * 1024)
        self.assertEqual(pb.pick_privacy(["SELF_ONLY"], "PUBLIC_TO_EVERYONE"), "SELF_ONLY")
        self.assertEqual(pb.pick_privacy(["SELF_ONLY", "PUBLIC_TO_EVERYONE"], ""), "PUBLIC_TO_EVERYONE")

    def test_safe_hides_tokens(self):
        self.assertNotIn("abc123secret", pb.safe("bad access_token=abc123secret&x=1"))
        self.assertNotIn("IGAAxyzxyzxyzxyz", pb.safe("token IGAAxyzxyzxyzxyz invalid"))

    def test_tiktok_flow(self):
        env = {"TIKTOK_CLIENT_KEY": "k", "TIKTOK_CLIENT_SECRET": "s", "TIKTOK_REFRESH_TOKEN": "r"}
        http = FakeHttp({
            ("POST", "/oauth/token/"): Resp({"access_token": "act.1"}),
            ("POST", "creator_info"): Resp({"data": {"privacy_level_options": ["SELF_ONLY"]}, "error": {"code": "ok"}}),
            ("POST", "video/init"): Resp({"data": {"publish_id": "p1", "upload_url": "https://up.tiktok/u"}, "error": {"code": "ok"}}),
            ("PUT", "up.tiktok"): Resp(status=201),
            ("POST", "status/fetch"): Resp({"data": {"status": "PUBLISH_COMPLETE"}}),
        })
        out = pb.tiktok_publish(self.video, "metin", env, http)
        self.assertEqual(out, {"ok": True, "id": "p1", "privacy": "SELF_ONLY", "status": "PUBLISH_COMPLETE"})
        init = [c for c in http.calls if "video/init" in c[1]][0][2]["json"]
        self.assertTrue(init["post_info"]["is_aigc"])
        self.assertEqual(init["source_info"]["video_size"], 1000)
        put = [c for c in http.calls if c[0] == "PUT"][0][2]
        self.assertEqual(put["headers"]["Content-Range"], "bytes 0-999/1000")

    def test_tiktok_bad_token(self):
        http = FakeHttp({("POST", "/oauth/token/"): Resp({"error": "invalid_grant", "error_description": "refresh_token=rft.secretsecret expired"})})
        with self.assertRaises(pb.PublishError) as e:
            pb.tiktok_publish(self.video, "m", {"TIKTOK_CLIENT_KEY": "k", "TIKTOK_CLIENT_SECRET": "s", "TIKTOK_REFRESH_TOKEN": "r"}, http)
        self.assertNotIn("secretsecret", str(e.exception))

    def test_instagram_flow(self):
        http = FakeHttp({
            ("GET", "/me"): Resp({"user_id": "42"}),
            ("POST", "/42/media_publish"): Resp({"id": "m9"}),
            ("POST", "/42/media"): Resp({"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v21.0/c1"}),
            ("POST", "rupload"): Resp({"success": True}),
            ("GET", "/c1"): Resp({"status_code": "FINISHED"}),
        })
        self.assertEqual(pb.instagram_publish(self.video, "cap", {"IG_ACCESS_TOKEN": "t"}, http), {"ok": True, "id": "m9"})
        up = [c for c in http.calls if "rupload" in c[1]][0][2]["headers"]
        self.assertEqual((up["offset"], up["file_size"]), ("0", "1000"))

    def test_youtube_flow(self):
        http = FakeHttp({
            ("POST", "oauth2.googleapis.com"): Resp({"access_token": "ya29.x"}),
            ("POST", "upload/youtube"): Resp(headers={"Location": "https://www.googleapis.com/upload/session1"}),
            ("PUT", "session1"): Resp({"id": "yt1"}),
        })
        env = {"YT_CLIENT_ID": "a", "YT_CLIENT_SECRET": "b", "YT_REFRESH_TOKEN": "c"}
        out = pb.youtube_publish(self.video, pb.texts({"title": "T", "caption": "C"}), env, http)
        self.assertEqual(out, {"ok": True, "id": "yt1", "privacy": "public"})

    def test_auto_topic_rotates(self):
        self.assertNotEqual(vp.auto_topic(1), vp.auto_topic(2))
        os.environ["VIDEO_MODE"] = "auto"
        try:
            self.assertIn(vp.auto_topic(3), vp.story_prompt(vp.settings({}), hour=3))
        finally:
            del os.environ["VIDEO_MODE"]


if __name__ == "__main__":
    unittest.main()
