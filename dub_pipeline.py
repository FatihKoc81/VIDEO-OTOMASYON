"""Türkçe dublaj hattı.

Akış: kaynak video → ses çıkarma (FFmpeg) → yazıya dökme (faster-whisper, yerel, ücretsiz)
→ Türkçeye çeviri (yerel NLLB-200, ücretsiz; isteğe bağlı ücretsiz API anahtarı) → Türkçe
seslendirme (edge-tts, ücretsiz
; hiçbir ücretli servis kullanılmaz) → zamanlamaya oturtma → orijinal sesi kısıp dublajı bindirme → MP4.

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
TRANSLATE_BATCH = 20  # Groq ücretsiz katmanının dakikalık token sınırına sığar


@dataclass
class Segment:
    start: float
    end: float
    text: str
    tr: str = ""
    speaker: int = 0


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

    model = None
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            model = WhisperModel(model_size, device="cuda", compute_type="float16")
    except Exception as e:  # GPU kitaplıkları uyumsuzsa CPU ile devam et
        print("UYARI: Whisper GPU açılamadı, CPU kullanılıyor: %s" % redact(e), flush=True)
    model = model or WhisperModel(model_size, device="cpu", compute_type="int8")
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
    key = os.environ.get("DUB_LLM_API_KEY")
    if not key:
        raise RuntimeError("API ile çeviri için DUB_LLM_API_KEY gerekli.")
    base = os.environ.get("DUB_LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    model = os.environ.get("DUB_LLM_MODEL", "openai/gpt-oss-120b")
    return base, model, key


# Whisper dil kodu → NLLB-200 dil kodu (yerel, ücretsiz çeviri)
NLLB_CODES = {
    "en": "eng_Latn", "de": "deu_Latn", "fr": "fra_Latn", "es": "spa_Latn", "it": "ita_Latn",
    "pt": "por_Latn", "nl": "nld_Latn", "pl": "pol_Latn", "sv": "swe_Latn", "ro": "ron_Latn",
    "ru": "rus_Cyrl", "uk": "ukr_Cyrl", "el": "ell_Grek", "ar": "arb_Arab", "fa": "pes_Arab",
    "he": "heb_Hebr", "hi": "hin_Deva", "ur": "urd_Arab", "ja": "jpn_Jpan", "ko": "kor_Hang",
    "zh": "zho_Hans", "id": "ind_Latn", "az": "azj_Latn", "ka": "kat_Geor", "bg": "bul_Cyrl",
    "kk": "kaz_Cyrl", "uz": "uzn_Latn", "ku": "kmr_Latn", "sq": "als_Latn", "bs": "bos_Latn",
}
NLLB_MODEL = os.environ.get("DUB_NLLB_MODEL", "facebook/nllb-200-distilled-600M")


def choose_translator(env=None):
    """Bütçe yok: anahtar verilmedikçe yerel NLLB kullanılır, hiçbir ücretli API'ye gidilmez."""
    env = os.environ if env is None else env
    mode = env.get("DUB_TRANSLATOR", "auto")
    if mode not in ("auto", "local", "llm"):
        raise ValueError("DUB_TRANSLATOR auto, local veya llm olmalı.")
    if mode == "auto":
        return "llm" if env.get("DUB_LLM_API_KEY") else "local"
    return mode


def translate_local(segments, source_lang, batch=16):
    code = NLLB_CODES.get(source_lang)
    if not code:
        raise RuntimeError("Yerel çeviri '%s' dilini desteklemiyor; --source-lang ver veya "
                           "DUB_LLM_API_KEY ile ücretsiz bir API anahtarı kullan." % source_lang)
    import torch
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(NLLB_MODEL, src_lang=code)
    model = AutoModelForSeq2SeqLM.from_pretrained(NLLB_MODEL).to(device).eval()
    target = tok.convert_tokens_to_ids("tur_Latn")
    for start in range(0, len(segments), batch):
        part = segments[start:start + batch]
        enc = tok([s.text for s in part], return_tensors="pt", padding=True, truncation=True,
                  max_length=400).to(device)
        with torch.inference_mode():
            out = model.generate(**enc, forced_bos_token_id=target, num_beams=4,
                                 max_new_tokens=256)
        for s, tr in zip(part, tok.batch_decode(out, skip_special_tokens=True)):
            s.tr = tr.strip() or s.text
    del model
    if device == "cuda":
        torch.cuda.empty_cache()
    return segments


def translate_any(segments, source_lang, llm_fn=None, local_fn=None):
    llm_fn = llm_fn or translate
    local_fn = local_fn or translate_local
    if choose_translator() == "llm":
        try:
            return llm_fn(segments, source_lang)
        except (RuntimeError, ValueError, KeyError) as e:
            print("UYARI: API ile çeviri olmadı (%s); yerel çeviriye geçiliyor." % redact(e),
                  flush=True)
    return local_fn(segments, source_lang)


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


def retry_wait(status, retry_after, attempt):
    """429/5xx için bekleme süresi (sn); None = yeniden deneme yok."""
    if status != 429 and status < 500:
        return None
    try:
        wait = float(retry_after)
    except (TypeError, ValueError):
        wait = 5.0 * (2 ** attempt)
    return min(max(wait, 1.0), 60.0)


def chat(messages, timeout=180, attempts=5, sleep=None):
    import time

    sleep = sleep or time.sleep
    base, model, key = llm_config()
    body = {"model": model, "temperature": 0.3, "messages": messages}
    for attempt in range(attempts):
        try:
            r = requests.post(base + "/chat/completions", json=body, timeout=timeout,
                              headers={"Authorization": "Bearer " + key})
        except requests.RequestException as e:
            raise RuntimeError("Çeviri isteği başarısız: " + redact(e)) from None
        wait = retry_wait(r.status_code, r.headers.get("retry-after"), attempt)
        if wait is not None and attempt + 1 < attempts:
            print("Çeviri API'si HTTP %d, %.0f sn sonra yeniden deneniyor…"
                  % (r.status_code, wait), flush=True)
            sleep(wait)
            continue
        if r.status_code >= 400:
            raise RuntimeError("Çeviri isteği başarısız: HTTP %d" % r.status_code)
        return r.json()["choices"][0]["message"]["content"] or ""
    raise RuntimeError("Çeviri isteği başarısız.")


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


def voice_track(segments, total, tts_fn, voice, workdir, voice_of=None, ext="mp3"):
    """voice_of(segment) verilirse her satırın sesi ondan alınır (ör. konuşmacıya göre)."""
    workdir.mkdir(parents=True, exist_ok=True)
    clips, durs = [], []
    for i, s in enumerate(segments):
        raw = workdir / ("tts_%04d.%s" % (i, ext))
        tts_fn(s.tr, raw, voice_of(s) if voice_of else voice)
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


AI_LABEL = "Yapay zekâ ile dublajlandı"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _filter_path(p):
    return str(Path(p).resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def mix(video, track, srt, dst, original_volume=0.12, burn=False, background=None,
        label=None):
    """Görüntü + Türkçe ses (+ ayrıştırılmış müzik/efekt veya kısılmış orijinal ses).

    background: konuşmasız arka plan sesi (Demucs). Verilirse orijinal ses kullanılmaz.
    label: verilirse köşeye küçük, kalıcı bir yazı eklenir (yapay zekâ etiketi).
    """
    cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(track)]
    if background:
        cmd += ["-i", str(background)]
        fc = ("[2:a]aresample=48000[bg];[1:a]aresample=48000[fg];"
              "[bg][fg]amix=inputs=2:duration=first:normalize=0[a]")
    elif has_audio(video) and original_volume > 0:
        fc = ("[0:a]aresample=48000,volume=%.3f[bg];[1:a]aresample=48000[fg];"
              "[bg][fg]amix=inputs=2:duration=first:normalize=0[a]" % original_volume)
    else:
        fc = "[1:a]aresample=48000[a]"
    vf = []
    if burn:
        style = ("FontName=DejaVu Sans,FontSize=16,Bold=1,PrimaryColour=&H00FFFFFF,"
                 "OutlineColour=&H00101010,BorderStyle=1,Outline=2,Alignment=2,MarginV=40")
        vf.append("subtitles='%s':force_style='%s'" % (_filter_path(srt), style))
    if label:
        font = ("fontfile='%s':" % _filter_path(FONT)) if Path(FONT).exists() else ""
        vf.append("drawtext=%stext='%s':fontcolor=white@0.85:fontsize=h/34:"
                  "box=1:boxcolor=black@0.45:boxborderw=8:x=w*0.03:y=h*0.03" % (font, label))
    if vf:
        fc += ";[0:v]" + ",".join(vf) + "[v]"
        cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    else:
        n = 3 if background else 2
        cmd += ["-i", str(srt), "-filter_complex", fc, "-map", "0:v:0", "-map", "[a]",
                "-map", "%d:s:0" % n, "-c:v", "copy", "-c:s", "mov_text",
                "-metadata:s:s:0", "language=tur"]
    cmd += ["-c:a", "aac", "-b:a", "160k", "-metadata:s:a:0", "language=tur",
            "-metadata", "comment=" + AI_LABEL, "-t", "%.3f" % media_duration(video), "-movflags", "+faststart", str(dst)]
    run(cmd)
    return dst


# ---------------------------------------------------------------- giriş
def main(argv=None):
    ap = argparse.ArgumentParser(description="Videoyu Türkçeye dublajlar.")
    ap.add_argument("source", help="Video dosyası veya doğrudan MP4 bağlantısı")
    ap.add_argument("--source-lang", default=os.environ.get("DUB_SOURCE_LANG", ""),
                    help="Kaynak dil kodu (ör. en); boşsa otomatik algılanır")
    ap.add_argument("--whisper-model", default=os.environ.get("DUB_WHISPER_MODEL", "small"))
    ap.add_argument("--voice", default=os.environ.get("DUB_VOICE", ""))
    ap.add_argument("--original-volume", type=float,
                    default=float(os.environ.get("DUB_ORIGINAL_VOLUME", "0.12")))
    ap.add_argument("--burn-subtitles", action="store_true",
                    default=os.environ.get("DUB_BURN_SUBTITLES", "") == "true")
    a = ap.parse_args(argv)

    OUT.mkdir(parents=True, exist_ok=True)
    voice = a.voice or "tr-TR-AhmetNeural"
    tts_fn = tts_edge

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
        translate_any(segments, lang)

    track, places, durs = voice_track(segments, total, tts_fn, voice, OUT / "parts")
    srt = write_srt(segments, places, durs, OUT / "dublaj_tr.srt")
    final = mix(video, track, srt, OUT / "video_dublaj.mp4", a.original_volume,
                a.burn_subtitles)
    (OUT / "dublaj.json").write_text(json.dumps({
        "source_language": lang, "segments": len(segments), "seconds": round(total, 1),
        "tts": "edge", "voice": voice,
        "lines": [{"start": round(p, 2), "src": s.text, "tr": s.tr}
                  for s, p in zip(segments, places)],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"video": str(final), "language": lang, "segments": len(segments),
                      "seconds": round(total, 1)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
