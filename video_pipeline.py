"""Türkçe kısa video üretimi (APEX'in Video panelinden veya Actions'tan elle).

Konu/talimat → senaryo (ücretsiz Groq, yoksa OpenAI) → seslendirme (edge-tts, ücretsiz) →
Pixabay görüntüleri (anahtar yoksa düz arka plan) → altyazılı MP4 (9:16 veya 16:9).

Ortam değişkenleri (hepsi isteğe bağlı):
  VIDEO_PROMPT    konu veya talimat; boşsa ilgi çekici bir konu seçilir
  VIDEO_ASPECT    "9:16" (varsayılan) veya "16:9"
  VIDEO_DURATION  saniye, 15–180 (varsayılan 60)
  VIDEO_VOICE     "erkek" (varsayılan) veya "kadın"
  DUB_LLM_API_KEY / GROQ_API_KEY, OPENAI_API_KEY, PIXABAY_API_KEY
"""
import asyncio, json, os, random, re, subprocess, textwrap
from pathlib import Path
import requests

OUT = Path("output")
VOICES = {"erkek": "tr-TR-AhmetNeural", "kadın": "tr-TR-EmelNeural", "kadin": "tr-TR-EmelNeural"}
WORDS_PER_SECOND = 2.1


def settings(env=os.environ):
    aspect = "16:9" if env.get("VIDEO_ASPECT", "").strip() == "16:9" else "9:16"
    try:
        duration = int(float(env.get("VIDEO_DURATION") or 60))
    except ValueError:
        duration = 60
    duration = max(15, min(180, duration))
    voice = VOICES.get(env.get("VIDEO_VOICE", "").strip().lower(), VOICES["erkek"])
    prompt = (env.get("VIDEO_PROMPT") or "").strip()[:1500]
    return {"prompt": prompt, "aspect": aspect, "duration": duration, "voice": voice}


def frame_size(aspect):
    return (1920, 1080) if aspect == "16:9" else (1080, 1920)


def word_range(duration):
    target = round(duration * WORDS_PER_SECOND)
    return max(25, round(target * 0.85)), max(30, round(target * 1.05))


def story_prompt(cfg):
    lo, hi = word_range(cfg["duration"])
    topic = cfg["prompt"] or "Tarih, teknoloji, genel kültür, otomobil veya ilginç bilgiler arasından özgün, doğrulanabilir bir konu seç."
    shape = "yatay (16:9) YouTube videosu" if cfg["aspect"] == "16:9" else "dikey (9:16) kısa video (TikTok, Reels, Shorts)"
    return (
        f"Konu / talimat: {topic}\n\n"
        f"Bunun için {cfg['duration']} saniyelik Türkçe {shape} hazırla. Anlatım {lo}-{hi} kelime olsun; "
        "ilk cümle ilk 2 saniyede merak uyandırsın, klişe giriş kullanma; kısa, konuşma dilinde cümleler kur; "
        "her 1-2 cümlede görüntülenebilecek somut bir sahne olsun; sonda yorum yazdıracak tek soru sor. "
        "Doğrulanmamış iddia, politik propaganda ve yalan clickbait kullanma. Yalnızca JSON döndür: "
        '{"title":"5-8 kelimelik başlık","script":"seslendirme metni","caption":"kısa açıklama ve 3-5 hashtag",'
        '"pixabay_query":"3-5 English search words"}'
    )


def parse_story(raw):
    """Reads the model's JSON even when it is wrapped in a ``` block or has text around it."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        raise ValueError("Senaryo JSON değil")
    obj = json.loads(m.group(0))
    for k in ("title", "script", "caption", "pixabay_query"):
        if not str(obj.get(k) or "").strip():
            raise ValueError("Eksik alan: " + k)
    return {k: str(obj[k]).strip() for k in ("title", "script", "caption", "pixabay_query")}


def llm_lanes(env=os.environ):
    lanes = []
    groq = (env.get("DUB_LLM_API_KEY") or env.get("GROQ_API_KEY") or "").strip()
    if groq:
        lanes.append(("https://api.groq.com/openai/v1", env.get("VIDEO_LLM_MODEL", "openai/gpt-oss-120b"), groq))
    openai = (env.get("OPENAI_API_KEY") or "").strip()
    if openai:
        lanes.append(("https://api.openai.com/v1", "gpt-4o-mini", openai))
    return lanes


def make_story(cfg):
    lanes = llm_lanes()
    if not lanes:
        raise RuntimeError("Senaryo için DUB_LLM_API_KEY (ücretsiz Groq) veya OPENAI_API_KEY secret'ı gerekli.")
    messages = [{"role": "system", "content": "Güvenilir Türkçe video senaristisin. Sadece JSON üret."},
                {"role": "user", "content": story_prompt(cfg)}]
    last = None
    for base, model, key in lanes:
        for _ in range(2):
            try:
                r = requests.post(base + "/chat/completions", headers={"Authorization": "Bearer " + key},
                                  json={"model": model, "temperature": 0.9, "messages": messages}, timeout=120)
                r.raise_for_status()
                return parse_story(r.json()["choices"][0]["message"]["content"])
            except Exception as e:  # next try / next lane
                last = e
    raise RuntimeError("Senaryo üretilemedi: " + type(last).__name__)


def get_audio(script, voice):
    import edge_tts
    p = OUT / "voice.mp3"
    asyncio.run(edge_tts.Communicate(script, voice, rate="+4%").save(str(p)))
    return p


def find_clips(q, aspect):
    key = (os.environ.get("PIXABAY_API_KEY") or "").strip()
    if not key:
        return []
    params = {"key": key, "q": q, "per_page": 20, "safesearch": "true"}
    try:
        r = requests.get("https://pixabay.com/api/videos/", params=params, timeout=60)
        r.raise_for_status()
        hits = r.json().get("hits", [])
        if not hits and " " in q:  # broader search with the first two words
            params["q"] = " ".join(q.split()[:2])
            hits = requests.get("https://pixabay.com/api/videos/", params=params, timeout=60).json().get("hits", [])
    except Exception:
        return []
    random.shuffle(hits)
    urls = []
    for h in hits:
        v = h.get("videos", {}).get("medium") or h.get("videos", {}).get("small")
        if v and v.get("url"):
            urls.append(v["url"])
    return urls[:8]


def media_duration(p):
    x = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(p)], text=True)
    return float(x.strip())


def stamp(t):
    n = int(t * 1000); h = n // 3600000; n %= 3600000; m = n // 60000; n %= 60000; s = n // 1000; n %= 1000
    return "%02d:%02d:%02d,%03d" % (h, m, s, n)


def captions_srt(script, total, words_per_line=5, wrap=25):
    words = script.split()
    chunks = [" ".join(words[i:i + words_per_line]) for i in range(0, len(words), words_per_line)]
    weights = [len(c.split()) for c in chunks]
    denom = sum(weights) or 1
    pos, out = 0.0, []
    for i, c in enumerate(chunks, 1):
        end = total * sum(weights[:i]) / denom
        out += [str(i), stamp(pos) + " --> " + stamp(end), textwrap.fill(c, wrap), ""]
        pos = end
    return "\n".join(out)


def scale_filter(w, h):
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps=30,setsar=1"


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    OUT.mkdir(exist_ok=True)
    cfg = settings()
    w, h = frame_size(cfg["aspect"])
    story = make_story(cfg)
    (OUT / "metadata.json").write_text(json.dumps({**story, "aspect": cfg["aspect"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    audio = get_audio(story["script"], cfg["voice"])
    total = media_duration(audio)
    urls = find_clips(story["pixabay_query"], cfg["aspect"])
    silent = OUT / "silent.mp4"
    if urls:
        segments, seglen = [], 6.0
        for i in range(max(1, int(total // seglen) + 1)):
            raw, seg = OUT / ("raw_%02d.mp4" % i), OUT / ("seg_%02d.mp4" % i)
            if not raw.exists():
                rr = requests.get(urls[i % len(urls)], timeout=120); rr.raise_for_status(); raw.write_bytes(rr.content)
            take = min(seglen, total - i * seglen)
            if take <= 0.05:
                break
            run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(raw), "-t", str(take), "-vf", scale_filter(w, h), "-an",
                 "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(seg)])
            segments.append(seg)
        listing = OUT / "concat.txt"
        listing.write_text("".join("file '" + str(p.resolve()) + "'\n" for p in segments), encoding="utf-8")
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(silent)])
    else:  # no Pixabay key or no hits: dark background, captions carry the video
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=0x0d1020:s={w}x{h}:r=30", "-t", str(total),
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(silent)])
    vertical = cfg["aspect"] == "9:16"
    srt = OUT / "captions.srt"; srt.write_text(captions_srt(story["script"], total, 5 if vertical else 8, 25 if vertical else 45), encoding="utf-8")
    title_srt = OUT / "title.srt"
    title_srt.write_text("1\n00:00:00,000 --> 00:00:03,000\n" + textwrap.fill(story["title"], 22 if vertical else 40) + "\n", encoding="utf-8")
    size, tsize, margin, tmargin = (38, 54, 190, 300) if vertical else (24, 36, 60, 80)
    vf = ("subtitles=" + str(srt.resolve()) + f":force_style='FontName=DejaVu Sans,FontSize={size},Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,BorderStyle=1,Outline=4,Shadow=1,Alignment=2,MarginV={margin}',"
          "subtitles=" + str(title_srt.resolve()) + f":force_style='FontName=DejaVu Sans,FontSize={tsize},Bold=1,PrimaryColour=&H0000D7FF,OutlineColour=&H00000000,BorderStyle=1,Outline=5,Shadow=2,Alignment=8,MarginV={tmargin}'")
    subprocess.run(["ffmpeg", "-y", "-i", str(silent), "-i", str(audio), "-vf", vf, "-map", "0:v:0", "-map", "1:a:0", "-t", str(total),
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                    str(OUT / "video.mp4")], check=True)
    print(json.dumps({"title": story["title"], "caption": story["caption"], "aspect": cfg["aspect"], "seconds": round(total, 1), "clips": len(urls)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
