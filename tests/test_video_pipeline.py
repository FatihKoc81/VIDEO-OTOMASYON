import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import video_pipeline as vp


class SettingsTest(unittest.TestCase):
    def test_defaults_and_bounds(self):
        c = vp.settings({})
        self.assertEqual((c["aspect"], c["duration"], c["voice"], c["prompt"]), ("9:16", 60, "tr-TR-AhmetNeural", ""))
        c = vp.settings({"VIDEO_ASPECT": "16:9", "VIDEO_DURATION": "999", "VIDEO_VOICE": "Kadın", "VIDEO_PROMPT": " uzay "})
        self.assertEqual((c["aspect"], c["duration"], c["voice"], c["prompt"]), ("16:9", 180, "tr-TR-EmelNeural", "uzay"))
        self.assertEqual(vp.settings({"VIDEO_DURATION": "abc", "VIDEO_ASPECT": "4:3"})["duration"], 60)
        self.assertEqual(vp.settings({"VIDEO_ASPECT": "4:3"})["aspect"], "9:16")
        self.assertEqual(vp.settings({"VIDEO_DURATION": "3"})["duration"], 15)

    def test_frame_and_words(self):
        self.assertEqual(vp.frame_size("9:16"), (1080, 1920))
        self.assertEqual(vp.frame_size("16:9"), (1920, 1080))
        lo, hi = vp.word_range(60)
        self.assertTrue(100 <= lo < hi <= 135)
        self.assertIn("Konu / talimat: uzay", vp.story_prompt(vp.settings({"VIDEO_PROMPT": "uzay"})))

    def test_parse_story(self):
        raw = '```json\n{"title":"T","script":"S","caption":"C","pixabay_query":"Q"}\n```'
        self.assertEqual(vp.parse_story(raw)["script"], "S")
        with self.assertRaises(ValueError):
            vp.parse_story('{"title":"T"}')
        with self.assertRaises(ValueError):
            vp.parse_story("yok")

    def test_llm_lanes_prefer_free_groq(self):
        self.assertEqual(vp.llm_lanes({}), [])
        lanes = vp.llm_lanes({"DUB_LLM_API_KEY": "g", "OPENAI_API_KEY": "o"})
        self.assertEqual([l[0] for l in lanes], ["https://api.groq.com/openai/v1", "https://api.openai.com/v1"])

    def test_captions(self):
        srt = vp.captions_srt("bir iki üç dört beş altı", 6.0)
        self.assertIn("00:00:00,000 --> 00:00:05,000", srt)
        self.assertIn("00:00:05,000 --> 00:00:06,000", srt)


if __name__ == "__main__":
    unittest.main()
