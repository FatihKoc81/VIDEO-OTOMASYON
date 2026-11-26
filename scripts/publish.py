"""output/video.mp4'yi sahibin kendi TikTok, Instagram Reels ve YouTube Shorts hesaplarına yükler.

Yalnızca bu iş akışının aynı çalışmada ürettiği videoyu yükler (başka dosya kabul etmez) ve
sessiz videoyu reddeder. Her platform kendi secret'ları varsa denenir, yoksa atlanır:

  TikTok   TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, TIKTOK_REFRESH_TOKEN  (TIKTOK_PRIVACY isteğe bağlı)
  YouTube  YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN               (YT_PRIVACY, varsayılan public)
  Reels    IG_ACCESS_TOKEN (IG_USER_ID isteğe bağlı; IG_GRAPH_BASE varsayılan graph.instagram.com)

Hiçbir anahtar loglanmaz. Sonuç output/publish.json'a yazılır. Yapılandırılmış bir platform
başarısız olursa çıkış kodu 1'dir (video artifact olarak yine saklanır).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

OUT = Path("output")
VIDEO = OUT / "video.mp4"
TIKTOK = "https://open.tiktokapis.com"
UA = {"User-Agent": "video-otomasyon/1.0"}


class PublishError(Exception):
    pass


def safe(text: str) -> str:
    """Hata metninden token benzeri dizgileri siler."""
    text = re.sub(r"(access_token|refresh_token|client_secret|token)=([^&\s\"']+)", r"\1=***", str(text))
    text = re.sub(r"\b(act|rft|EAA|IGQ|IGAA|ya29)\.?[A-Za-z0-9_\-\.]{8,}", "***", text)
    return text[:300]


def configured(env=os.environ) -> list[str]:
    have = lambda *k: all((env.get(x) or "").strip() for x in k)
    out = []
    if have("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REFRESH_TOKEN"):
        out.append("tiktok")
    if have("IG_ACCESS_TOKEN"):
        out.append("instagram")
    if have("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"):
        out.append("youtube")
    return out


def wanted(env=os.environ) -> list[str]:
    """PUBLISH_TARGETS (ör. "tiktok,youtube") verilirse yalnız onlar; yoksa yapılandırılanların hepsi."""
    raw = (env.get("PUBLISH_TARGETS") or "").strip().lower()
    have = configured(env)
    if not raw or raw == "all":
        return have
    pick = [p.strip() for p in raw.split(",") if p.strip()]
    return [p for p in have if p in pick]


# --- video kontrolü -------------------------------------------------------------------------

def has_sound(path: Path) -> tuple[bool, float]:
    """(ses akışı var ve duyulur mu, ortalama ses dB)."""
    streams = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                              "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    if not streams:
        return False, -999.0
    log = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return parse_mean_volume(log)


def parse_mean_volume(ffmpeg_log: str) -> tuple[bool, float]:
    m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", ffmpeg_log)
    if not m or m.group(1) == "-inf":
        return False, -999.0
    db = float(m.group(1))
    return db > -50.0, db


def duration_of(path: Path) -> float:
    x = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                 "default=nw=1:nk=1", str(path)], text=True)
    return float(x.strip())


# --- metinler -------------------------------------------------------------------------------

def texts(meta: dict) -> dict:
    title = " ".join(str(meta.get("title") or "").split()) or "Kısa video"
    caption = str(meta.get("caption") or title).strip()
    note = "\n\nBu video yapay zekâ ile üretildi; görüntüler stok."
    yt_title = re.sub(r"[<>]", "", title)[:90].strip()
    if "#shorts" not in yt_title.lower():
        yt_title = (yt_title + " #Shorts")[:100]
    tags = [t.lstrip("#") for t in re.findall(r"#\w+", caption)][:10]
    return {
        "tiktok": (caption + note)[:2200],
        "instagram": (caption + note)[:2200],
        "yt_title": yt_title,
        "yt_description": re.sub(r"[<>]", "", caption + note)[:4900],
        "yt_tags": tags,
    }


# --- TikTok (Content Posting API, Direct Post) ----------------------------------------------

def tiktok_chunks(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) TikTok kuralına göre: tek parça <= 64 MB, değilse 10 MB parçalar."""
    if size <= 64 * 1024 * 1024:
        return size, 1
    chunk = 10 * 1024 * 1024
    return chunk, size // chunk  # son parça kalanı da taşır (en çok 128 MB)


def pick_privacy(options: list[str], want: str) -> str:
    if want and want in options:
        return want
    for p in ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"):
        if p in options:
            return p
    raise PublishError("TikTok gizlilik seçeneği yok")


def tiktok_publish(path: Path, caption: str, env=os.environ, http=requests) -> dict:
    tok = http.post(TIKTOK + "/v2/oauth/token/", headers={**UA, "Content-Type": "application/x-www-form-urlencoded"}, data={
        "client_key": env["TIKTOK_CLIENT_KEY"], "client_secret": env["TIKTOK_CLIENT_SECRET"],
        "grant_type": "refresh_token", "refresh_token": env["TIKTOK_REFRESH_TOKEN"]}, timeout=30).json()
    access = tok.get("access_token")
    if not access:
        raise PublishError("TikTok token yenilenemedi: " + safe(tok.get("error_description") or tok.get("error") or "?"))
    auth = {**UA, "Authorization": "Bearer " + access, "Content-Type": "application/json; charset=UTF-8"}
    info = http.post(TIKTOK + "/v2/post/publish/creator_info/query/", headers=auth, json={}, timeout=30).json()
    if (info.get("error") or {}).get("code") not in (None, "ok"):
        raise PublishError("TikTok hesap bilgisi: " + safe(info["error"].get("message") or info["error"]["code"]))
    data = info.get("data") or {}
    privacy = pick_privacy(data.get("privacy_level_options") or [], (env.get("TIKTOK_PRIVACY") or "PUBLIC_TO_EVERYONE").strip())
    size = path.stat().st_size
    chunk, count = tiktok_chunks(size)
    init = http.post(TIKTOK + "/v2/post/publish/video/init/", headers=auth, json={
        "post_info": {"title": caption, "privacy_level": privacy, "disable_duet": False, "disable_comment": False,
                      "disable_stitch": False, "video_cover_timestamp_ms": 1000, "is_aigc": True},
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count},
    }, timeout=30).json()
    if (init.get("error") or {}).get("code") not in (None, "ok"):
        raise PublishError("TikTok başlatma: " + safe(init["error"].get("message") or init["error"]["code"]))
    publish_id, upload_url = init["data"]["publish_id"], init["data"]["upload_url"]
    with open(path, "rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            body = f.read(end - start + 1)
            r = http.put(upload_url, data=body, timeout=300, headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(body)), "Content-Range": f"bytes {start}-{end}/{size}"})
            if r.status_code not in (200, 201, 206):
                raise PublishError(f"TikTok yükleme HTTP {r.status_code}")
    status = "PROCESSING_UPLOAD"
    for _ in range(40):
        time.sleep(6)
        s = http.post(TIKTOK + "/v2/post/publish/status/fetch/", headers=auth, json={"publish_id": publish_id}, timeout=30).json()
        status = (s.get("data") or {}).get("status") or status
        if status == "PUBLISH_COMPLETE":
            break
        if status == "FAILED":
            raise PublishError("TikTok işleme hatası: " + safe((s.get("data") or {}).get("fail_reason") or "?"))
    return {"ok": True, "id": publish_id, "privacy": privacy, "status": status}


# --- Instagram Reels (Instagram API, resumable upload) --------------------------------------

def instagram_publish(path: Path, caption: str, env=os.environ, http=requests) -> dict:
    base = (env.get("IG_GRAPH_BASE") or "https://graph.instagram.com/v21.0").rstrip("/")
    token = env["IG_ACCESS_TOKEN"].strip()
    user = (env.get("IG_USER_ID") or "").strip()
    if not user:
        me = http.get(base + "/me", params={"fields": "user_id", "access_token": token}, timeout=30).json()
        user = str(me.get("user_id") or me.get("id") or "")
        if not user:
            raise PublishError("Instagram hesabı okunamadı: " + safe((me.get("error") or {}).get("message") or "?"))
    c = http.post(f"{base}/{user}/media", data={"media_type": "REELS", "upload_type": "resumable", "caption": caption,
                                                "share_to_feed": "true", "access_token": token}, timeout=30).json()
    if not c.get("id") or not c.get("uri"):
        raise PublishError("Reels kapsayıcısı: " + safe((c.get("error") or {}).get("message") or "?"))
    size = path.stat().st_size
    with open(path, "rb") as f:
        r = http.post(c["uri"], data=f.read(), timeout=600, headers={
            "Authorization": "OAuth " + token, "offset": "0", "file_size": str(size), "Content-Type": "application/octet-stream"})
    if r.status_code != 200:
        raise PublishError(f"Reels yükleme HTTP {r.status_code}")
    code = "IN_PROGRESS"
    for _ in range(60):
        time.sleep(5)
        st = http.get(f"{base}/{c['id']}", params={"fields": "status_code,status", "access_token": token}, timeout=30).json()
        code = st.get("status_code") or code
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise PublishError("Reels işleme: " + safe(st.get("status") or code))
    if code != "FINISHED":
        raise PublishError("Reels işleme zaman aşımı")
    p = http.post(f"{base}/{user}/media_publish", data={"creation_id": c["id"], "access_token": token}, timeout=60).json()
    if not p.get("id"):
        raise PublishError("Reels yayını: " + safe((p.get("error") or {}).get("message") or "?"))
    return {"ok": True, "id": p["id"]}


# --- YouTube Shorts (Data API v3, resumable upload) -----------------------------------------

def youtube_publish(path: Path, t: dict, env=os.environ, http=requests) -> dict:
    tok = http.post("https://oauth2.googleapis.com/token", data={
        "client_id": env["YT_CLIENT_ID"], "client_secret": env["YT_CLIENT_SECRET"],
        "refresh_token": env["YT_REFRESH_TOKEN"], "grant_type": "refresh_token"}, timeout=30).json()
    access = tok.get("access_token")
    if not access:
        raise PublishError("YouTube token yenilenemedi: " + safe(tok.get("error_description") or tok.get("error") or "?"))
    size = path.stat().st_size
    privacy = (env.get("YT_PRIVACY") or "public").strip()
    meta = {"snippet": {"title": t["yt_title"], "description": t["yt_description"], "tags": t["yt_tags"],
                        "categoryId": "22", "defaultLanguage": "tr", "defaultAudioLanguage": "tr"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True}}
    start = http.post("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
                      headers={"Authorization": "Bearer " + access, "Content-Type": "application/json; charset=UTF-8",
                               "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
                      data=json.dumps(meta), timeout=30)
    loc = start.headers.get("Location") or start.headers.get("location")
    if start.status_code != 200 or not loc:
        raise PublishError(f"YouTube yükleme başlatılamadı (HTTP {start.status_code})")
    with open(path, "rb") as f:
        r = http.put(loc, data=f.read(), headers={"Content-Type": "video/mp4", "Content-Length": str(size)}, timeout=600)
    if r.status_code not in (200, 201):
        raise PublishError(f"YouTube yükleme HTTP {r.status_code}")
    return {"ok": True, "id": r.json().get("id"), "privacy": privacy}


# --- giriş ----------------------------------------------------------------------------------

def main() -> int:
    targets = wanted()
    result: dict = {"targets": targets, "results": {}}
    if not targets:
        result["skipped"] = "Hiçbir platform secret'ı yok; yayın atlandı."
        print(json.dumps(result, ensure_ascii=False))
        (OUT / "publish.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    if not VIDEO.exists():
        print("Video yok; yayın yapılmadı.", file=sys.stderr)
        return 1
    audible, db = has_sound(VIDEO)
    seconds = duration_of(VIDEO)
    result.update({"meanVolumeDb": db, "seconds": round(seconds, 1)})
    if not audible or seconds < 5:
        result["blocked"] = "Video sessiz veya çok kısa; hiçbir hesaba yüklenmedi."
        (OUT / "publish.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        return 1
    meta = json.loads((OUT / "metadata.json").read_text(encoding="utf-8"))
    t = texts(meta)
    jobs = {"tiktok": lambda: tiktok_publish(VIDEO, t["tiktok"]),
            "instagram": lambda: instagram_publish(VIDEO, t["instagram"]),
            "youtube": lambda: youtube_publish(VIDEO, t)}
    failed = False
    for name in targets:
        try:
            result["results"][name] = jobs[name]()
        except Exception as e:  # her platform bağımsız
            failed = True
            msg = str(e) if isinstance(e, PublishError) else type(e).__name__
            result["results"][name] = {"ok": False, "error": safe(msg)}
    (OUT / "publish.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
