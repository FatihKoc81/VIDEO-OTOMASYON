"""Dublaj hattı testleri. Ağ, Whisper veya gerçek TTS kullanmaz; FFmpeg gerekir."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dub_pipeline as dp  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


class PureTests(unittest.TestCase):
    def test_speed_for(self):
        self.assertEqual(dp.speed_for(2.0, 3.0), 1.0)
        self.assertAlmostEqual(dp.speed_for(3.0, 2.5), 1.2)
        self.assertEqual(dp.speed_for(10.0, 2.0, max_speed=1.45), 1.45)
        self.assertEqual(dp.speed_for(1.0, 0.0), 1.0)

    def test_build_timeline_pushes_overflow(self):
        self.assertEqual(dp.build_timeline([0.0, 2.0, 5.0], [1.0, 4.0, 1.0]), [0.0, 2.0, 6.0])

    def test_merge_short(self):
        segs = [dp.Segment(0.0, 0.5, "Hi"), dp.Segment(0.6, 3.0, "there friend"),
                dp.Segment(5.0, 8.0, "Next")]
        out = dp.merge_short(segs)
        self.assertEqual(len(out), 2)
        self.assertEqual((out[0].start, out[0].end, out[0].text), (0.0, 3.0, "Hi there friend"))

    def test_parse_translation(self):
        raw = '```json\n{"items":[{"i":3,"tr":"Merhaba"},{"i":4,"tr":"Dünya"}]}\n```'
        self.assertEqual(dp.parse_translation(raw, [3, 4]), {3: "Merhaba", 4: "Dünya"})
        with self.assertRaises(ValueError):
            dp.parse_translation('{"items":[{"i":3,"tr":"x"}]}', [3, 4])

    def test_translate_falls_back_to_single_lines(self):
        segs = [dp.Segment(0, 1, "a"), dp.Segment(1, 2, "b")]
        calls = []

        def fake_chat(messages):
            payload = json.loads(messages[1]["content"].split("\n", 1)[1])
            calls.append(len(payload))
            if len(payload) > 1:
                return "bozuk yanıt"
            return json.dumps({"items": [{"i": payload[0]["i"], "tr": "TR-" + payload[0]["t"]}]})

        dp.translate(segs, "en", chat_fn=fake_chat)
        self.assertEqual([s.tr for s in segs], ["TR-a", "TR-b"])
        self.assertEqual(calls, [2, 1, 1])

    def test_redact(self):
        self.assertNotIn("sk-abcdef123456", dp.redact("hata sk-abcdef123456 Bearer xyz"))
        self.assertNotIn("xyz", dp.redact("Bearer xyz"))

    def test_choose_translator_is_free_by_default(self):
        self.assertEqual(dp.choose_translator({}), "local")
        # Depodaki ücretli OpenAI anahtarı çeviriyi API'ye yöneltmemeli.
        self.assertEqual(dp.choose_translator({"OPENAI_API_KEY": "sk-x"}), "local")
        self.assertEqual(dp.choose_translator({"DUB_LLM_API_KEY": "gsk-x"}), "llm")
        self.assertEqual(dp.choose_translator({"DUB_LLM_API_KEY": "x", "DUB_TRANSLATOR": "local"}),
                         "local")
        with self.assertRaises(ValueError):
            dp.choose_translator({"DUB_TRANSLATOR": "paid"})

    def test_llm_config_never_uses_openai_key(self):
        old = {k: os.environ.pop(k, None) for k in ("DUB_LLM_API_KEY", "OPENAI_API_KEY")}
        try:
            os.environ["OPENAI_API_KEY"] = "sk-test"
            with self.assertRaises(RuntimeError):
                dp.llm_config()
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v

    def test_local_translation_rejects_unknown_language(self):
        with self.assertRaises(RuntimeError):
            dp.translate_local([dp.Segment(0, 1, "x")], "xx")
        self.assertEqual(dp.NLLB_CODES["en"], "eng_Latn")

    def test_stamp(self):
        self.assertEqual(dp.stamp(3725.5), "01:02:05,500")


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg yok")
class FfmpegSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.old_out = dp.OUT
        dp.OUT = self.tmp

    def tearDown(self):
        dp.OUT = self.old_out
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_tts(self, text, dst, voice):
        # Metin uzunluğuna göre sinüs: uzun satır yuvayı aşar ve hızlandırmayı tetikler.
        secs = 0.08 * len(text)
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=440:d=%.2f" % secs,
                        "-c:a", "libmp3lame", str(dst)], check=True, capture_output=True)

    def test_voice_track_and_mix(self):
        video = self.tmp / "in.mp4"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=25:d=6",
                        "-f", "lavfi", "-i", "sine=f=200:d=6", "-shortest", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)],
                       check=True, capture_output=True)
        segs = [dp.Segment(0.5, 2.0, "x", "Kısa satır."),
                dp.Segment(2.0, 3.0, "y", "Bu satır yuvasına sığmayacak kadar uzun."),
                dp.Segment(4.5, 5.5, "z", "Son.")]
        track, places, durs = dp.voice_track(segs, 6.0, self.fake_tts, "v", self.tmp / "parts")
        self.assertAlmostEqual(dp.media_duration(track), max(6.0, places[-1] + durs[-1]),
                               delta=0.15)
        self.assertEqual(places[0], 0.5)
        self.assertGreaterEqual(places[2], places[1] + durs[1] - 0.01)
        # Uzun satır hızlandırıldı: ham 3.2 sn, kap 1.45 → ~2.2 sn.
        self.assertLess(durs[1], 0.08 * len(segs[1].tr) - 0.5)
        srt = dp.write_srt(segs, places, durs, self.tmp / "tr.srt")
        self.assertIn("Kısa satır.", srt.read_text(encoding="utf-8"))
        for burn in (False, True):
            out = dp.mix(video, track, srt, self.tmp / ("out_%s.mp4" % burn), 0.12, burn)
            self.assertTrue(dp.has_audio(out))
            self.assertAlmostEqual(dp.media_duration(out), 6.0, delta=0.3)
        streams = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(self.tmp / "out_False.mp4")], text=True).split()
        self.assertIn("subtitle", streams)


if __name__ == "__main__":
    unittest.main()
