"""Oyuncunun kendi sesiyle Türkçe dublaj + dudak senkronu (GPU: Kaggle / Colab).

Akış: kaynak → Demucs ile konuşma / arka plan ayrımı → konuşmanın yazıya dökülmesi
→ konuşmacı gruplama (XTTS ses imzası) → Türkçe çeviri → her konuşmacının kendi sesiyle
Türkçe seslendirme (XTTS-v2, ses klonu) → zamanlama → isteğe bağlı LatentSync dudak senkronu
→ arka plan + Türkçe konuşma + "Yapay zekâ ile dublajlandı" etiketi.

Lisans: XTTS-v2 modeli Coqui Public Model License altındadır, YALNIZCA ticari olmayan kullanım.
İzin: Yalnızca hakkına sahip olduğun ve sesi kopyalanan kişilerin izin verdiği videolar
(`--izinli` olmadan çalışmaz). Üretilen videolar yapay zekâ etiketi taşır; yayımlarken
platformun "yapay zekâ ile oluşturuldu/değiştirildi" seçeneğini de işaretle.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import wave
from pathlib import Path

import dub_pipeline as dp

SPEAKER_THRESHOLD = float(os.environ.get("DUB_SPEAKER_THRESHOLD", "0.75"))
MIN_EMBED_SECONDS = 1.0
XTTS_CHAR_LIMIT = 200


# ---------------------------------------------------------------- saf yardımcılar (testli)
def split_text(text, limit=XTTS_CHAR_LIMIT):
    """XTTS'in Türkçe karakter sınırı için metni cümle / virgül / kelime sınırından böler."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return [text] if text else []
    parts = re.split(r"(?<=[.!?…;])\s+", text)
    out = []
    for p in parts:
        while len(p) > limit:
            cut = max(p.rfind(", ", 0, limit), p.rfind(" ", 0, limit))
            cut = cut if cut > 0 else limit
            out.append(p[:cut].strip(" ,"))
            p = p[cut:].strip(" ,")
        if out and len(out[-1]) + 1 + len(p) <= limit:
            out[-1] = out[-1] + " " + p
        elif p:
            out.append(p)
    return out


def cluster_speakers(embeddings, threshold=SPEAKER_THRESHOLD):
    """Ses imzalarını kosinüs benzerliğiyle gruplar. None (çok kısa parça) öncekine katılır."""
    import numpy as np

    labels, cents = [], []
    for e in embeddings:
        if e is None:
            labels.append(labels[-1] if labels else 0)
            continue
        e = np.asarray(e, dtype=float).ravel()
        e = e / (np.linalg.norm(e) or 1.0)
        sims = [float(c @ e / (np.linalg.norm(c) or 1.0)) for c in cents]
        if sims and max(sims) >= threshold:
            k = int(np.argmax(sims))
            cents[k] = cents[k] + e
        else:
            k = len(cents)
            cents.append(e)
        labels.append(k)
    return labels


def pick_references(segments, speaker, min_len=1.5, max_total=24.0):
    """Konuşmacının en uzun, temiz parçalarından toplam ~max_total sn örnek seçer."""
    mine = [i for i, s in enumerate(segments) if s.speaker == speaker]
    good = [i for i in mine if segments[i].end - segments[i].start >= min_len] or mine
    good.sort(key=lambda i: segments[i].end - segments[i].start, reverse=True)
    out, total = [], 0.0
    for i in good:
        if total >= max_total:
            break
        out.append(i)
        total += segments[i].end - segments[i].start
    return sorted(out)


def require_consent(flag):
    if not flag:
        raise SystemExit(
            "Ses klonu için --izinli gerekli: videonun haklarına sahip olduğunu ve sesi "
            "kopyalanan kişilerin izin verdiğini onayla.")


# ---------------------------------------------------------------- ses ayrımı
def separate(video, workdir):
    workdir.mkdir(parents=True, exist_ok=True)
    mixwav = workdir / "mix44k.wav"
    dp.run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "2", "-ar", "44100",
            "-c:a", "pcm_s16le", str(mixwav)])
    dp.run([sys.executable, "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs",
            "-o", str(workdir), str(mixwav)])
    stem = workdir / "htdemucs" / mixwav.stem
    return stem / "vocals.wav", stem / "no_vocals.wav"


def cut(src, dst, start, end, rate=22050):
    dp.run(["ffmpeg", "-y", "-ss", "%.3f" % start, "-to", "%.3f" % end, "-i", str(src),
            "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le", str(dst)])
    return dst


# ---------------------------------------------------------------- XTTS ses klonu
class XttsVoices:
    def __init__(self, device=None):
        import torch
        from TTS.api import TTS

        self.torch = torch
        device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device) \
            .synthesizer.tts_model
        self.latents = {}

    def embed(self, wav_paths):
        gpt, spk = self.model.get_conditioning_latents(audio_path=[str(p) for p in wav_paths])
        return gpt, spk

    def signature(self, wav_path):
        _, spk = self.embed([wav_path])
        return spk.detach().float().cpu().numpy().ravel()

    def speak(self, text, dst, speaker):
        import numpy as np

        gpt, spk = self.latents[speaker]
        audio = []
        for chunk in split_text(text):
            with self.torch.inference_mode():
                out = self.model.inference(chunk, "tr", gpt, spk, temperature=0.7,
                                           enable_text_splitting=False)
            audio.append(np.asarray(out["wav"], dtype=np.float32))
            audio.append(np.zeros(int(24000 * 0.12), dtype=np.float32))
        pcm = (np.clip(np.concatenate(audio), -1, 1) * 32767).astype("<i2")
        with wave.open(str(dst), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm.tobytes())


def assign_speakers(segments, vocals, voices, workdir):
    sigs = []
    for i, s in enumerate(segments):
        if s.end - s.start < MIN_EMBED_SECONDS:
            sigs.append(None)
            continue
        sigs.append(voices.signature(cut(vocals, workdir / ("sig_%04d.wav" % i), s.start, s.end)))
    for s, k in zip(segments, cluster_speakers(sigs)):
        s.speaker = k
    for k in sorted({s.speaker for s in segments}):
        refs = [cut(vocals, workdir / ("ref_%d_%04d.wav" % (k, i)),
                    segments[i].start, segments[i].end)
                for i in pick_references(segments, k)]
        voices.latents[k] = voices.embed(refs)
    return len(voices.latents)


# ---------------------------------------------------------------- dudak senkronu
def lipsync(video, voice_wav, dst, latentsync_dir, python_bin):
    """LatentSync ile dudakları Türkçe sese uydurur. Başarısız olursa None döner."""
    audio16 = Path(dst).with_suffix(".16k.wav")
    dp.run(["ffmpeg", "-y", "-i", str(voice_wav), "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(audio16)])
    cmd = [python_bin, "-m", "scripts.inference",
           "--unet_config_path", os.environ.get("DUB_LS_CONFIG", "configs/unet/stage2.yaml"),
           "--inference_ckpt_path", "checkpoints/latentsync_unet.pt",
           "--inference_steps", os.environ.get("DUB_LS_STEPS", "20"),
           "--guidance_scale", "1.5",
           "--video_path", str(Path(video).resolve()),
           "--audio_path", str(audio16.resolve()),
           "--video_out_path", str(Path(dst).resolve())]
    r = subprocess.run(cmd, cwd=latentsync_dir, capture_output=True, text=True)
    if r.returncode != 0 or not Path(dst).exists():
        tail = "\n".join((r.stderr or r.stdout).strip().splitlines()[-8:])
        print("UYARI: dudak senkronu başarısız, dudaklar orijinal kalacak.\n" + dp.redact(tail),
              flush=True)
        return None
    return Path(dst)


# ---------------------------------------------------------------- giriş
def main(argv=None):
    ap = argparse.ArgumentParser(description="Oyuncunun kendi sesiyle Türkçe dublaj.")
    ap.add_argument("source")
    ap.add_argument("--izinli", action="store_true",
                    help="Videonun haklarına sahibim ve ses sahipleri izin verdi")
    ap.add_argument("--source-lang", default="")
    ap.add_argument("--whisper-model", default="medium")
    ap.add_argument("--lipsync", action="store_true")
    ap.add_argument("--latentsync-dir", default="LatentSync")
    ap.add_argument("--latentsync-python", default=sys.executable)
    ap.add_argument("--burn-subtitles", action="store_true")
    a = ap.parse_args(argv)
    require_consent(a.izinli)

    out = dp.OUT
    out.mkdir(parents=True, exist_ok=True)
    work = out / "clone"
    video = dp.fetch_source(a.source)
    total = dp.media_duration(video)
    if total > dp.MAX_SECONDS:
        raise RuntimeError("Video %.0f sn; sınır %.0f sn (DUB_MAX_SECONDS)." % (total, dp.MAX_SECONDS))
    if not dp.has_audio(video):
        raise RuntimeError("Kaynak videoda ses yok.")

    print("1/6 Konuşma ve arka plan ayrılıyor (Demucs)…", flush=True)
    vocals, background = separate(video, work)
    print("2/6 Yazıya dökülüyor (Whisper %s)…" % a.whisper_model, flush=True)
    segments, lang = dp.transcribe(dp.extract_wav(vocals, work / "vocals16k.wav"),
                                   a.whisper_model, a.source_lang)
    if not segments:
        raise RuntimeError("Videoda konuşma algılanmadı.")
    segments = dp.merge_short(segments)

    print("3/6 Konuşmacılar ayrılıyor, ses örnekleri alınıyor (XTTS)…", flush=True)
    voices = XttsVoices()
    n_speakers = assign_speakers(segments, vocals, voices, work / "refs")
    print("    %d parça, %d konuşmacı, dil: %s" % (len(segments), n_speakers, lang), flush=True)

    print("4/6 Türkçeye çevriliyor…", flush=True)
    if lang == "tr":
        for s in segments:
            s.tr = s.text
    else:
        dp.translate(segments, lang)

    print("5/6 Oyuncuların sesiyle Türkçe seslendiriliyor…", flush=True)
    track, places, durs = dp.voice_track(
        segments, total, lambda text, dst, spk: voices.speak(text, dst, spk), None,
        work / "tts", voice_of=lambda s: s.speaker, ext="wav")
    srt = dp.write_srt(segments, places, durs, out / "dublaj_tr.srt")

    picture = video
    synced = False
    if a.lipsync:
        print("6/6 Dudak senkronu (LatentSync)…", flush=True)
        ls = lipsync(video, track, work / "lipsync.mp4", a.latentsync_dir, a.latentsync_python)
        if ls:
            picture, synced = ls, True
    else:
        print("6/6 Dudak senkronu kapalı.", flush=True)

    final = dp.mix(picture, track, srt, out / "video_dublaj.mp4", burn=a.burn_subtitles,
                   background=background, label=dp.AI_LABEL)
    (out / "dublaj.json").write_text(json.dumps({
        "ai_generated": True, "voice_clone": "xtts_v2", "lipsync": synced,
        "source_language": lang, "speakers": n_speakers, "seconds": round(total, 1),
        "lines": [{"start": round(p, 2), "speaker": s.speaker, "src": s.text, "tr": s.tr}
                  for s, p in zip(segments, places)],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"video": str(final), "speakers": n_speakers, "lipsync": synced},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
