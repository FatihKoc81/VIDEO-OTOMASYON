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

## Türkçe dublaj (`dub_pipeline.py`) — tamamen ücretsiz
Var olan bir videoyu Türkçeye dublajlar: ses çıkarma → yazıya dökme (faster-whisper) →
Türkçe çeviri (yerel NLLB-200, anahtar gerekmez) → Türkçe seslendirme (edge-tts
`tr-TR-AhmetNeural` / `tr-TR-EmelNeural`) → her satırı kendi zamanına oturtma (en çok 1.45×
hızlandırma) → orijinal sesi kısıp dublajı bindirme. Hiçbir ücretli API çağrılmaz; depodaki
`OPENAI_API_KEY` bu hatta kullanılmaz.
Çıktı: `video_dublaj.mp4` (Türkçe ses + altyazı izi), `dublaj_tr.srt`, `dublaj.json`.

- Telefon: GitHub → Actions → **Türkçe dublaj** → Run workflow → doğrudan MP4 bağlantısı.
- İsteğe bağlı, daha doğal çeviri: ücretsiz Groq anahtarını (console.groq.com, kart
  gerekmez) `DUB_LLM_API_KEY` secret'ı olarak ekle. Anahtar yoksa yerel çeviri kullanılır.
- Yerel: `pip install -r requirements-dub.txt && python dub_pipeline.py video.mp4`
- Testler (ağ kullanmaz): `python -m unittest discover -s tests -v`
- Yalnızca hakkına sahip olduğun veya dublaj izni olan videoları kullan.

## Oyuncunun kendi sesiyle dublaj + dudak senkronu (`dub_clone.py`, GPU)
Tamamen ücretsizdir. GPU gerektirir; ücretsiz **Kaggle** (GPU T4, haftada ~30 saat) veya **Colab** üzerinde
`dub_gpu.ipynb` not defteriyle çalışır.

1. Demucs: konuşma ile müzik/efekt ayrılır, müzik korunur.
2. Whisper: konuşma yazıya dökülür (GPU varsa GPU'da).
3. Konuşmacı ayrımı: her satırın XTTS ses imzası kosinüs benzerliğiyle gruplanır
   (`DUB_SPEAKER_THRESHOLD`, varsayılan 0.75); her oyuncunun en uzun temiz satırları (~24 sn)
   ses örneği olur.
4. Çeviri (yerel NLLB-200 veya isteğe bağlı ücretsiz Groq anahtarı), ardından XTTS-v2 ile her oyuncunun **kendi sesiyle** Türkçe.
5. LatentSync 1.5 ile dudaklar Türkçe sese uydurulur; başarısız olursa video orijinal
   dudaklarla tamamlanır ve uyarı verilir.
6. Müzik + Türkçe konuşma; köşede ve dosya etiketinde "Yapay zekâ ile dublajlandı".

Kısıtlar: XTTS-v2 **yalnızca ticari olmayan** kullanım içindir. `--izinli` olmadan çalışmaz:
yalnızca haklarına sahip olduğun ve ses sahiplerinin izin verdiği videolar. Dudak senkronu
yüzü net görünen, tek konuşanın olduğu çekimlerde iyi sonuç verir.

## APEX'ten otomatik dublaj (`dub-kaggle.yml`)
APEX uygulamasının **Dublaj** ekranı bu iş akışını başlatır. İş akışı `scripts/kaggle_job.py`
ile kullanıcının Kaggle hesabında özel bir GPU çekirdeği (`dublaj-<job_id>`) açar, bitmesini
bekler, videoyu `dublaj-<job_id>` artifact'i olarak 7 gün saklar. Çeviri Kaggle'da yerel NLLB ile
yapılır; hiçbir anahtar çekirdeğe yazılmaz.

Gerekli GitHub secret'ları: `KAGGLE_USERNAME`, `KAGGLE_KEY` (kaggle.com → Settings → API →
Create New Token; ücretsiz). Kaggle hesabında telefon doğrulaması yapılmış olmalı (GPU ve
internet için). İsteğe bağlı repository variable `DUB_REF` (varsayılan `main`).

## APEX'ten video üretimi (`generate-video.yml`)
APEX uygulamasında sağ üstteki **V** düğmesi Video panelini açar (eski NEXORA). Panel konu/talimat,
oran (9:16 / 16:9), süre ve ses seçip bu iş akışını `job_id` ile başlatır; iş `Video <job_id>` adıyla
bulunur, video `video-<job_id>` artifact'i olarak 7 gün saklanır.
`video_pipeline.py`: senaryo ücretsiz Groq'tan (`DUB_LLM_API_KEY`, yoksa `OPENAI_API_KEY`), seslendirme
edge-tts (ücretsiz), görüntüler Pixabay'den (`PIXABAY_API_KEY`; yoksa koyu arka plan + altyazı).
Otomatik yayın yoktur.
