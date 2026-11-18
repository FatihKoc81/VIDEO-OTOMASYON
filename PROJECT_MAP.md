# VIDEO-OTOMASYON — Proje Haritası

> İlk harita: 18 Kasım 2026. README ve depo kökü esas alınmıştır.

## Projenin amacı
Telefon üzerinden yönetilen, ücretsiz araçları önceleyen kısa video üretim hattı.

## Hedeflenen üretim akışı
Konu/senaryo → stok görüntü arama → seslendirme → FFmpeg ile dikey video → özel test çıktısı → kullanıcı onayı → Metricool ile TikTok/YouTube yayını.

## Kök dosyalar
- `video_pipeline.py`: video üretim hattının Python giriş/iş akışı dosyası.
- `requirements.txt`: Python bağımlılık listesi.
- `README.md`: mevcut aşama ve sonraki geliştirme adımları.
- `privacy.html`: gizlilik sayfası.
- `terms.html`: kullanım koşulları.
- `tiktokxlgARSIGPoU1yUu57exovIpuJSzr6AIo.txt`: TikTok alan adı doğrulama dosyası; dosya adı gizli anahtar anlamına gelmez, içeriği yayın öncesi kontrol edilmeli.
- `.github/`: GitHub Actions otomasyonu.

## Mevcut durum (README'ye göre)
- GitHub Actions üzerinde FFmpeg ile örnek MP4 üretimi.
- Çıktı Actions artifact olarak saklanır.
- Aşama 1 ücretli API çağrısı yapmaz.
- Saatlik otomatik yayınlama kapalıdır.

## Sonraki aşamalar
1. Türkçe konu ve senaryo üretimi.
2. Pixabay API ile görüntü arama.
3. TTS seslendirme.
4. Dikey MP4 render.
5. Özel test çıktısı ve kullanıcı onayı sonrası Metricool üzerinden yayın.

## Güvenlik
API anahtarları GitHub Actions Secrets veya güvenli bağlantılarda tutulmalı; kaynak koda/loglara yazılmamalı.

## Harita güncelleme kuralı
Yeni üretim adımı, servis, webhook, yayın hedefi veya zamanlayıcı eklendiğinde bu dosyayı güncelle.
