"""Türkçe dublaj hattı.

Akış: kaynak video → ses çıkarma (FFmpeg) → yazıya dökme (faster-whisper, yerel, ücretsiz)
→ Türkçeye çeviri (OpenAI uyumlu sohbet API'si) → Türkçe seslendirme (edge-tts ücretsiz
veya OpenAI TTS) → zamanlamaya oturtma → orijinal sesi kısıp dublajı bindirme → MP4.

Yalnızca hakkına sahip olduğunuz veya dublaj izni olan videoları kullanın.
Anahtarlar yalnızca ortam değişkenlerinden okunur; hiçbir anahtar loglanmaz.
"""
import argparse
import asyncio
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests

OUT = Path(os.environ.get("DUB_OUTPUT_DIR", "output"))
SAMPLE_RATE = 24000
MAX_SPEEDUP = float(os.environ.get("DUB_MAX_SPEEDUP", "1.45"))
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
MAX_SECONDS = float(os.environ.get("DUB_MAX_SECONDS", "900"))
TRANSLATE_BATCH = 30


@dataclass
class Segment:
    start: float
    end: float
    text: str
    tr: str = ""


# ---------------------------------------------------------------- yardımcılar
def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def media_duration(path):
    x = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)], text=True)
    return float(x.strip())


def has_audio(path):
    x = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", str(path)], text=True)
    return bool(x.strip())


def stamp(t):
    n = int(round(max(0.0, t) * 1000))
    h, n = divmod(n, 3600000)
    m, n = divmod(n, 60000)
    s, n = divmod(n, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, n)


def redact(text):
    """Hata metinlerinden anahtar benzeri dizileri temizler."""
    return re.sub(r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+\S+|key=[^&\s]+)", "[gizli]", str(text))


# ---------------------------------------------------------------- 1) kaynak
def fetch_source(source):
    p = Path(source)
    if p.exists():
        return p
    if not re.match(r"^https?://", source):
        raise ValueError("Kaynak bir dosya yolu ya da doğrudan http(s) video bağlantısı olmalı.")
    dst = OUT / "source.mp4"
    with requests.get(source, stream=True, timeout=120) as r:
        r.raise_for_status()
        size = 0
        with dst.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("Kaynak video 500 MB sınırını aşıyor.")
                f.write(chunk)
    return dst


def extract_wav(video, dst):
    run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(dst)])
    return dst


# ---------------------------------------------------------------- 2) yazıya dökme
def transcribe(wav, model_size="small", language=None):
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segs, info = model.transcribe(str(wav), language=language or None, vad_filter=True,
                                  beam_size=5)
    out = [Segment(s.start, s.end, s.text.strip()) for s in segs if s.text.strip()]
    return out, info.language


def merge_short(segments, min_len=1.2, max_gap=0.35, max_len=12.0):
    """Çok kısa parçaları komşusuyla birleştirir; çeviri ve seslendirme daha doğal olur."""
    out = []
    for s in segments:
        if out:
            prev = out[-1]
            short = (prev.end - prev.start) < min_len or (s.end - s.start) < min_len
            if short and s.start - prev.end <= max_gap and s.end - prev.start <= max_len:
                out[-1] = Segment(prev.start, s.end, (prev.text + " " + s.text).strip())
                continue
        out.append(Segment(s.start, s.end, s.text))
    return out


# ---------------------------------------------------------------- 3) çeviri
def llm_config():
    key = os.environ.get("DUB_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("Çeviri için DUB_LLM_API_KEY veya OPENAI_API_KEY gerekli.")
    base = os.environ.get("DUB_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("DUB_LLM_MODEL", "gpt-4o-mini")
    return base, model, key


def build_translate_messages(items, source_lang):
    payload = [{"i": i, "t": t, "sn": round(d, 1)} for i, t, d in items]
    system = ("Profesyonel dublaj çevirmenisin. Metinleri doğal, konuşma dilinde Türkçeye çevir. "
              "Her satır verilen süre (sn, saniye) içinde rahat okunabilecek uzunlukta olsun; "
              "gerekirse kısalt ama anlamı koru. Özel isimleri değiştirme. Yalnızca JSON döndür.")
    user = ("Kaynak dil: %s. Aşağıdaki her öğeyi çevir ve aynı 'i' ile döndür. Biçim: "
            '{"items":[{"i":0,"tr":"..."}]}\n%s' % (source_lang or "bilinmiyor",
                                                    json.dumps(payload, ensure_ascii=False)))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_translation(raw, expected_ids):
    raw = raw.strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("Çeviri yanıtında JSON yok.")
    obj = json.loads(m.group(0))
    got = {int(x["i"]): str(x.get("tr", "")).strip() for x in obj.get("items", [])}
    missing = [i for i in expected_ids if not got.get(i)]
    if missing:
        raise ValueError("Çeviride eksik satırlar: %s" % missing[:10])
    return {i: got[i] for i in expected_ids}


def chat(messages, timeout=180):
    base, model, key = llm_config()
    body = {"model": model, "temperature": 0.3, "messages": messages}
    try:
        r = requests.post(base + "/chat/completions", json=body, timeout=timeout,
                          headers={"Authorization": "Bearer " + key})
        r.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError("Çeviri isteği başarısız: " + redact(e)) from None
    return r.json()["choices"][0]["message"]["content"]


def translate(segments, source_lang, chat_fn=chat):
    for start in range(0, len(segments), TRANSLATE_BATCH):
        batch = segments[start:start + TRANSLATE_BATCH]
        items = [(start + k, s.text, s.end - s.start) for k, s in enumerate(batch)]
        ids = [i for i, _, _ in items]
        try:
            result = parse_translation(chat_fn(build_translate_messages(items, source_lang)), ids)
        except ValueError:
            # Toplu yanıt bozuksa satır satır dene.
            result = {}
            for item in items:
                result.update(parse_translation(
                    chat_fn(build_translate_messages([item], source_lang)), [item[0]]))
        for i in ids:
            segments[i].tr = result[i]
    return segments


# ---------------------------------------------------------------- 4) seslendirme
def tts_edge(text, dst, voice):
    import edge_tts

    async def go():
        await edge_tts.Communicate(text, voice, proxy=os.environ.get("HTTPS_PROXY")).save(str(dst))
    asyncio.run(go())


def tts_openai(text, dst, voice):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OpenAI TTS için OPENAI_API_KEY gerekli.")
    body = {"model": "gpt-4o-mini-tts", "voice": voice, "response_format": "mp3", "input": text,
            "instructions": "Türkçe konuş. Doğal, akıcı bir dublaj sanatçısı gibi oku."}
    try:
        r = requests.post("https://api.openai.com/v1/audio/speech", json=body, timeout=180,
                          headers={"Authorization": "Bearer " + key})
        r.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError("OpenAI TTS başarısız: " + redact(e)) from None
    Path(dst).write_bytes(r.content)


def speed_for(duration, slot, max_speed=MAX_SPEEDUP):
    """Seslendirme yuvasına sığmak için gereken hızlandırma (1.0 = değişmez)."""
    if slot <= 0 or duration <= slot:
        return 1.0
    return min(max_speed, duration / slot)


def build_timeline(starts, durations):
    """Her klibin başlangıcını hesaplar; taşan klip sonrakileri iter, üst üste binme olmaz."""
    cursor, out = 0.0, []
    for s, d in zip(starts, durations):
        at = max(s, cursor)
        out.append(at)
        cursor = at + d
    return out


def to_wav(src, dst, speed=1.0):
    af = "aresample=%d" % SAMPLE_RATE
    if speed > 1.001:
        af = "atempo=%.3f,%s" % (speed, af)
    run(["ffmpeg", "-y", "-i", str(src), "-af", af, "-ac", "1", "-c:a", "pcm_s16le", str(dst)])
    return dst


def silence(dst, seconds):
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=%d:cl=mono" % SAMPLE_RATE,
         "-t", "%.3f" % seconds, "-c:a", "pcm_s16le", str(dst)])
    return dst


def voice_track(segments, total, tts_fn, voice, workdir):
    workdir.mkdir(parents=True, exist_ok=True)
    clips, durs = [], []
    for i, s in enumerate(segments):
        raw = workdir / ("tts_%04d.mp3" % i)
        tts_fn(s.tr, raw, voice)
        slot_end = segments[i + 1].start if i + 1 < len(segments) else total
        speed = speed_for(media_duration(raw), slot_end - s.start)
        wav = to_wav(raw, workdir / ("tts_%04d.wav" % i), speed)
        clips.append(wav)
        durs.append(media_duration(wav))
    places = build_timeline([s.start for s in segments], durs)
    parts, cursor = [], 0.0
    for i, (clip, at, d) in enumerate(zip(clips, places, durs)):
        if at - cursor > 0.01:
            parts.append(silence(workdir / ("gap_%04d.wav" % i), at - cursor))
        parts.append(clip)
        cursor = at + d
    if total - cursor > 0.01:
        parts.append(silence(workdir / "gap_end.wav", total - cursor))
    listing = workdir / "concat.txt"
    listing.write_text("".join("file '%s'\n" % p.resolve() for p in parts), encoding="utf-8")
    track = OUT / "dublaj.wav"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c:a", "pcm_s16le", str(track)])
    return track, places, durs


# ---------------------------------------------------------------- 5) birleştirme
def write_srt(segments, places, durs, dst):
    lines = []
    for n, (s, at, d) in enumerate(zip(segments, places, durs), 1):
        lines += [str(n), stamp(at) + " --> " + stamp(at + d), s.tr, ""]
    Path(dst).write_text("\n".join(lines), encoding="utf-8")
    return dst


def mix(video, track, srt, dst, original_volume=0.12, burn=False):
    inputs = ["-i", str(video), "-i", str(track)]
    if has_audio(video) and original_volume > 0:
        fc = ("[0:a]aresample=48000,volume=%.3f[bg];[1:a]aresample=48000[fg];"
              "[bg][fg]amix=inputs=2:duration=first:normalize=0[a]" % original_volume)
    else:
        fc = "[1:a]aresample=48000[a]"
    cmd = ["ffmpeg", "-y", *inputs]
    if burn:
        style = ("FontName=DejaVu Sans,FontSize=16,Bold=1,PrimaryColour=&H00FFFFFF,"
                 "OutlineColour=&H00101010,BorderStyle=1,Outline=2,Alignment=2,MarginV=40")
        sub = str(Path(srt).resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        fc += ";[0:v]subtitles='%s':force_style='%s'[v]" % (sub, style)
        cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-i", str(srt), "-filter_complex", fc, "-map", "0:v:0", "-map", "[a]",
                "-map", "2:s:0", "-c:v", "copy", "-c:s", "mov_text",
                "-metadata:s:s:0", "language=tur"]
    cmd += ["-c:a", "aac", "-b:a", "160k", "-metadata:s:a:0", "language=tur",
            "-movflags", "+faststart", str(dst)]
    run(cmd)
    return dst


# ---------------------------------------------------------------- giriş
def main(argv=None):
    ap = argparse.ArgumentParser(description="Videoyu Türkçeye dublajlar.")
    ap.add_argument("source", help="Video dosyası veya doğrudan MP4 bağlantısı")
    ap.add_argument("--source-lang", default=os.environ.get("DUB_SOURCE_LANG", ""),
                    help="Kaynak dil kodu (ör. en); boşsa otomatik algılanır")
    ap.add_argument("--whisper-model", default=os.environ.get("DUB_WHISPER_MODEL", "small"))
    ap.add_argument("--tts", choices=["edge", "openai"], default=os.environ.get("DUB_TTS", "edge"))
    ap.add_argument("--voice", default=os.environ.get("DUB_VOICE", ""))
    ap.add_argument("--original-volume", type=float,
                    default=float(os.environ.get("DUB_ORIGINAL_VOLUME", "0.12")))
    ap.add_argument("--burn-subtitles", action="store_true",
                    default=os.environ.get("DUB_BURN_SUBTITLES", "") == "true")
    a = ap.parse_args(argv)

    OUT.mkdir(parents=True, exist_ok=True)
    voice = a.voice or ("tr-TR-AhmetNeural" if a.tts == "edge" else "onyx")
    tts_fn = tts_edge if a.tts == "edge" else tts_openai

    video = fetch_source(a.source)
    total = media_duration(video)
    if total > MAX_SECONDS:
        raise RuntimeError("Video %.0f sn; sınır %.0f sn (DUB_MAX_SECONDS)." % (total, MAX_SECONDS))
    if not has_audio(video):
        raise RuntimeError("Kaynak videoda ses yok; dublajlanacak konuşma bulunamadı.")

    wav = extract_wav(video, OUT / "source_16k.wav")
    segments, lang = transcribe(wav, a.whisper_model, a.source_lang)
    if not segments:
        raise RuntimeError("Videoda konuşma algılanmadı.")
    segments = merge_short(segments)
    print("Algılanan dil: %s, %d parça" % (lang, len(segments)), flush=True)

    if lang == "tr":
        for s in segments:
            s.tr = s.text
    else:
        translate(segments, lang)

    track, places, durs = voice_track(segments, total, tts_fn, voice, OUT / "parts")
    srt = write_srt(segments, places, durs, OUT / "dublaj_tr.srt")
    final = mix(video, track, srt, OUT / "video_dublaj.mp4", a.original_volume,
                a.burn_subtitles)
    (OUT / "dublaj.json").write_text(json.dumps({
        "source_language": lang, "segments": len(segments), "seconds": round(total, 1),
        "tts": a.tts, "voice": voice,
        "lines": [{"start": round(p, 2), "src": s.text, "tr": s.tr}
                  for s, p in zip(segments, places)],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"video": str(final), "language": lang, "segments": len(segments),
                      "seconds": round(total, 1)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
