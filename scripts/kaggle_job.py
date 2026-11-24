"""Dublaj işini Kaggle GPU'sunda çalıştırır (GitHub Actions'tan çağrılır).

  python scripts/kaggle_job.py build --out kernel   # girdiler ortam değişkenlerinden
  python scripts/kaggle_job.py wait <kullanıcı/çekirdek>

Her iş kendi özel (private) Kaggle çekirdeğinde çalışır: dublaj-<job_id>.
Hiçbir anahtar çekirdeğe yazılmaz; çeviri Kaggle'da yerel NLLB ile yapılır.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

JOB_RE = re.compile(r"^[a-f0-9]{12}$")
LANG_RE = re.compile(r"^([a-z]{2,3})?$")
REPO = "https://github.com/FatihKoc81/VIDEO-OTOMASYON"

KERNEL = r'''
import base64, json, os, shutil, subprocess, sys
P = json.loads(base64.b64decode("__PARAMS__").decode("utf-8"))
W = "/tmp/dub"
OUT = "/kaggle/working/output"
os.makedirs(W, exist_ok=True); os.makedirs(OUT, exist_ok=True)

def sh(cmd, check=True, **kw):
    print("$", " ".join(cmd)[:200], flush=True)
    return subprocess.run(cmd, check=check, **kw)

try:
    sh(["bash", "-c", "apt-get -qq update && apt-get -qq install -y ffmpeg fonts-dejavu-core libgl1 > /dev/null"], check=False)
    sh(["git", "clone", "-q", "--depth", "1", "-b", P["ref"], P["repo"], W + "/repo"])
    sh([sys.executable, "-m", "pip", "install", "-q", "coqui-tts", "demucs==4.0.1", "faster-whisper==1.1.1",
        "edge-tts==7.0.0", "sentencepiece", "requests"])
    cmd = [sys.executable, "dub_clone.py", P["source_url"], "--izinli", "--whisper-model", "medium"]
    if P["source_lang"]:
        cmd += ["--source-lang", P["source_lang"]]
    if P["lipsync"]:
        ls, venv = W + "/LatentSync", W + "/ls-venv"
        sh([sys.executable, "-m", "pip", "install", "-q", "virtualenv"])
        sh([sys.executable, "-m", "virtualenv", "-q", venv])
        sh(["git", "clone", "-q", "--depth", "1", "https://github.com/bytedance/LatentSync", ls])
        sh([venv + "/bin/pip", "install", "-q", "-r", "requirements.txt", "huggingface_hub[cli]"], cwd=ls)
        sh([venv + "/bin/huggingface-cli", "download", "ByteDance/LatentSync-1.5", "latentsync_unet.pt",
            "whisper/tiny.pt", "--local-dir", "checkpoints"], cwd=ls)
        cmd += ["--lipsync", "--latentsync-dir", ls, "--latentsync-python", venv + "/bin/python"]
    env = dict(os.environ, COQUI_TOS_AGREED="1", DUB_OUTPUT_DIR=W + "/output")
    env.pop("DUB_LLM_API_KEY", None)
    sh(cmd, cwd=W + "/repo", env=env)
    for name in ("video_dublaj.mp4", "dublaj_tr.srt", "dublaj.json"):
        shutil.copy(W + "/output/" + name, OUT + "/" + name)
    print("DUBLAJ_TAMAM", flush=True)
except Exception as e:
    open(OUT + "/error.txt", "w").write(str(e)[:2000])
    raise
'''


def validate(job_id, source_url, source_lang, consent):
    if not JOB_RE.match(job_id or ""):
        raise ValueError("job_id 12 haneli küçük harf onaltılık olmalı.")
    u = urlparse(source_url or "")
    if u.scheme != "https" or not u.netloc or len(source_url) > 2000:
        raise ValueError("Kaynak video https:// ile başlayan bir bağlantı olmalı.")
    if not LANG_RE.match(source_lang or ""):
        raise ValueError("Kaynak dil kodu 2-3 küçük harf olmalı (ör. en) ya da boş.")
    if consent != "true":
        raise ValueError("Hak/izin ve XTTS ticari olmayan lisans onayı gerekli.")


def build(out, username, job_id, source_url, source_lang, lipsync, consent, ref="main"):
    validate(job_id, source_url, source_lang, consent)
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", username or ""):
        raise ValueError("KAGGLE_USERNAME eksik veya geçersiz.")
    slug = "dublaj-" + job_id
    params = {"repo": REPO, "ref": ref, "source_url": source_url, "source_lang": source_lang,
              "lipsync": lipsync == "true"}
    encoded = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "run.py").write_text(KERNEL.replace("__PARAMS__", encoded), encoding="utf-8")
    meta = {"id": "%s/%s" % (username, slug), "title": slug, "code_file": "run.py",
            "language": "python", "kernel_type": "script", "is_private": True,
            "enable_gpu": True, "enable_internet": True, "dataset_sources": [],
            "competition_sources": [], "kernel_sources": []}
    (out / "kernel-metadata.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta["id"]


def parse_status(text):
    t = (text or "").lower()
    for key, value in (("complete", "complete"), ("error", "error"), ("cancel", "cancelled"),
                       ("running", "running"), ("queued", "queued")):
        if key in t:
            return value
    return "unknown"


def wait(kernel_id, interval=60, limit_minutes=270, sleep=time.sleep, status_fn=None):
    def default_status():
        r = subprocess.run(["kaggle", "kernels", "status", kernel_id], capture_output=True,
                           text=True)
        return r.stdout + r.stderr
    status_fn = status_fn or default_status
    sleep(interval)  # yeni sürüm kuyruğa girsin
    for _ in range(int(limit_minutes * 60 / interval)):
        s = parse_status(status_fn())
        print("Kaggle durumu:", s, flush=True)
        if s in ("complete", "error", "cancelled"):
            return s
        sleep(interval)
    return "timeout"


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", default="kernel")
    w = sub.add_parser("wait")
    w.add_argument("kernel_id")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        e = os.environ
        kid = build(a.out, e.get("KAGGLE_USERNAME", ""), e.get("JOB_ID", ""),
                    e.get("SOURCE_URL", ""), e.get("SOURCE_LANG", ""), e.get("LIPSYNC", "false"),
                    e.get("CONSENT", ""), e.get("DUB_REF", "main"))
        print(kid)
        gh = e.get("GITHUB_OUTPUT")
        if gh:
            with open(gh, "a") as f:
                f.write("kernel_id=%s\n" % kid)
    else:
        s = wait(a.kernel_id)
        if s != "complete":
            print("Kaggle işi bitmedi: %s" % s, flush=True)
            sys.exit(1)


if __name__ == "__main__":
    main()
