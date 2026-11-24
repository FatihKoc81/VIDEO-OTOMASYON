import base64
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import kaggle_job as kj  # noqa: E402

OK = dict(job_id="0123456789ab", source_url="https://example.com/v.mp4", source_lang="en",
          consent="true")


class KaggleJobTests(unittest.TestCase):
    def test_validate(self):
        kj.validate(**OK)
        for bad in (dict(job_id="../x"), dict(source_url="http://example.com/v.mp4"),
                    dict(source_url="file:///etc/passwd"), dict(source_lang="en;rm"),
                    dict(consent="false")):
            with self.assertRaises(ValueError):
                kj.validate(**{**OK, **bad})

    def test_build_embeds_params_safely(self):
        with tempfile.TemporaryDirectory() as d:
            url = 'https://example.com/v.mp4?a="1"\'\n__import__("os")'
            kid = kj.build(d, "fatih", OK["job_id"], url, "", "true", "true")
            self.assertEqual(kid, "fatih/dublaj-0123456789ab")
            meta = json.loads((Path(d) / "kernel-metadata.json").read_text())
            self.assertTrue(meta["is_private"] and meta["enable_gpu"] and meta["enable_internet"])
            self.assertEqual(meta["title"], "dublaj-0123456789ab")
            src = (Path(d) / "run.py").read_text()
            self.assertNotIn("example.com", src)  # yalnızca base64 içinde
            enc = re.search(r'b64decode\("([A-Za-z0-9+/=]+)"\)', src).group(1)
            params = json.loads(base64.b64decode(enc))
            self.assertEqual(params["source_url"], url)
            self.assertIs(params["lipsync"], True)
            compile(src, "run.py", "exec")
            self.assertNotIn("API_KEY", src.replace('env.pop("DUB_LLM_API_KEY", None)', ""))

    def test_build_rejects_bad_username(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(ValueError):
            kj.build(d, "", OK["job_id"], OK["source_url"], "", "false", "true")

    def test_parse_status(self):
        self.assertEqual(kj.parse_status('x has status "KernelWorkerStatus.COMPLETE"'), "complete")
        self.assertEqual(kj.parse_status('has status "running"'), "running")
        self.assertEqual(kj.parse_status('has status "error"'), "error")
        self.assertEqual(kj.parse_status('cancelAcknowledged'), "cancelled")
        self.assertEqual(kj.parse_status(''), "unknown")

    def test_wait(self):
        seq = iter(["queued", "running", "complete"])
        self.assertEqual(kj.wait("u/k", interval=1, sleep=lambda _: None,
                                 status_fn=lambda: next(seq)), "complete")
        self.assertEqual(kj.wait("u/k", interval=60, limit_minutes=2, sleep=lambda _: None,
                                 status_fn=lambda: "running"), "timeout")


if __name__ == "__main__":
    unittest.main()
