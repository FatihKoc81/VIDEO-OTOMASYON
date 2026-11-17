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
