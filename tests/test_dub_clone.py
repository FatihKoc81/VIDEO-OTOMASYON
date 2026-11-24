"""Ses klonu / dudak senkronu hattının GPU gerektirmeyen kısımlarının testleri."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dub_clone as dc  # noqa: E402
import dub_pipeline as dp  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


class PureTests(unittest.TestCase):
    def test_split_text_respects_limit(self):
        text = "Bu birinci cümle. " * 30
        parts = dc.split_text(text, limit=60)
        self.assertTrue(all(0 < len(p) <= 60 for p in parts))
        self.assertEqual(" ".join(parts).split(), text.split())

    def test_split_text_long_sentence_without_punctuation(self):
        text = "kelime " * 80
        parts = dc.split_text(text, limit=50)
        self.assertTrue(all(len(p) <= 50 for p in parts))
        self.assertEqual(" ".join(parts).split(), text.split())
        self.assertEqual(dc.split_text("Kısa."), ["Kısa."])
        self.assertEqual(dc.split_text("   "), [])

    def test_cluster_speakers(self):
        a, b = [1.0, 0.0, 0.1], [0.0, 1.0, 0.1]
        labels = dc.cluster_speakers([None, a, [0.95, 0.05, 0.1], b, None, a], threshold=0.8)
        self.assertEqual(labels, [0, 0, 0, 1, 1, 0])

    def test_pick_references(self):
        segs = [dp.Segment(0, 1, "", speaker=0), dp.Segment(1, 9, "", speaker=0),
                dp.Segment(9, 12, "", speaker=1), dp.Segment(12, 30, "", speaker=0),
                dp.Segment(30, 31, "", speaker=1)]
        self.assertEqual(dc.pick_references(segs, 0, max_total=20), [1, 3])
        self.assertEqual(dc.pick_references(segs, 1), [2])
        only_short = [dp.Segment(0, 0.5, "", speaker=2)]
        self.assertEqual(dc.pick_references(only_short, 2), [0])

    def test_consent_required(self):
        with self.assertRaises(SystemExit):
            dc.require_consent(False)
        dc.require_consent(True)

    def test_main_refuses_without_consent(self):
        with self.assertRaises(SystemExit):
            dc.main(["video.mp4"])


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg yok")
class MixWithBackgroundTest(unittest.TestCase):
    def test_background_and_label(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            def gen(args, name):
                subprocess.run(["ffmpeg", "-y", *args, str(tmp / name)], check=True,
                               capture_output=True)
                return tmp / name
            video = gen(["-f", "lavfi", "-i", "color=c=gray:s=320x240:r=25:d=4", "-f", "lavfi",
                         "-i", "sine=f=300:d=4", "-shortest", "-c:v", "libx264",
                         "-pix_fmt", "yuv420p", "-c:a", "aac"], "v.mp4")
            track = gen(["-f", "lavfi", "-i", "sine=f=500:d=4", "-ar", "24000", "-ac", "1"], "t.wav")
            bg = gen(["-f", "lavfi", "-i", "sine=f=100:d=4", "-ac", "2"], "bg.wav")
            srt = tmp / "s.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nMerhaba\n", encoding="utf-8")
            for i, (burn, label) in enumerate([(False, None), (False, dp.AI_LABEL),
                                               (True, dp.AI_LABEL)]):
                out = dp.mix(video, track, srt, tmp / ("o%d.mp4" % i), background=bg,
                             burn=burn, label=label)
                self.assertTrue(dp.has_audio(out))
                self.assertAlmostEqual(dp.media_duration(out), 4.0, delta=0.3)
            tags = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                            "format_tags=comment", "-of", "csv=p=0",
                                            str(tmp / "o1.mp4")], text=True)
            self.assertIn("Yapay zek", tags)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
