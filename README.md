# VIDEO-OTOMASYON

Telefon üzerinden yönetilen, ücretsiz araçları önceleyen kısa video üretim hattı.

## Aşama 1 — güvenli render testi
- GitHub Actions üzerinde FFmpeg ile örnek MP4 üretir.
- Çıktıyı Actions artifact olarak saklar.
- Bu aşamada OpenAI, Pixabay, Metricool veya başka ücretli servislere istek göndermez.
- Otomatik saatlik yayınlama kapalıdır.

## Sonraki aşamalar
1. OpenAI ile Türkçe konu ve senaryo üretimi.
2. Pixabay API ile konuya uygun görüntü arama.
3. OpenAI TTS ile seslendirme.
4. FFmpeg ile dikey MP4 oluşturma.
5. Önce özel test çıktısı; kullanıcı onayından sonra Metricool üzerinden TikTok ve YouTube.

## Güvenlik
API anahtarları yalnızca GitHub Actions Secrets veya Activepieces bağlantılarında tutulmalıdır. Hiçbir anahtar kaynak koda, loglara veya README dosyasına yazılmamalıdır.

## Türkçe dublaj (`dub_pipeline.py`)
Var olan bir videoyu Türkçeye dublajlar: ses çıkarma → yazıya dökme (faster-whisper, runner
üzerinde, ücretsiz) → Türkçe çeviri (OpenAI uyumlu API) → Türkçe seslendirme (varsayılan
edge-tts `tr-TR-AhmetNeural`, ücretsiz; isteğe bağlı OpenAI TTS) → her satırı kendi zamanına
oturtma (gerekirse en çok 1.45× hızlandırma) → orijinal sesi kısıp dublajı bindirme.
Çıktı: `video_dublaj.mp4` (Türkçe ses + Türkçe altyazı izi), `dublaj_tr.srt`, `dublaj.json`.

- Telefon: GitHub → Actions → **Türkçe dublaj** → Run workflow → doğrudan MP4 bağlantısı.
- Çeviri anahtarı: `OPENAI_API_KEY` ya da `DUB_LLM_API_KEY` secret'ı. Ücretsiz bir OpenAI
  uyumlu sağlayıcı (ör. Groq) için `DUB_LLM_BASE_URL` ve `DUB_LLM_MODEL` repository
  variable'larını ayarla.
- Yerel: `pip install -r requirements-dub.txt && python dub_pipeline.py video.mp4`
- Testler (ağ kullanmaz): `python -m unittest discover -s tests -v`
- Yalnızca hakkına sahip olduğun veya dublaj izni olan videoları kullan.
