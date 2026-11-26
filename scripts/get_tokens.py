"""Bir kerelik: TikTok ve YouTube için yenileme anahtarı (refresh token) alır. Telefonda Termux'ta çalışır.

  python scripts/get_tokens.py youtube  <CLIENT_ID> <CLIENT_SECRET>
  python scripts/get_tokens.py tiktok   <CLIENT_KEY> <CLIENT_SECRET> <REDIRECT_URI>

Betik bir bağlantı yazar; tarayıcıda aç, hesabınla izin ver, sonra adres çubuğundaki adresin
tamamını buraya yapıştır. Çıkan refresh token'ı GitHub → VIDEO-OTOMASYON → Settings → Secrets and
variables → Actions içine kaydet (YT_REFRESH_TOKEN / TIKTOK_REFRESH_TOKEN). Anahtarı kimseyle paylaşma.
"""
import secrets
import sys
import urllib.parse

import requests


def code_from(url: str) -> str:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url.strip()).query)
    if "code" not in q:
        sys.exit("Adreste code= yok: " + ",".join(q.get("error", ["?"])))
    return q["code"][0]


def youtube(cid: str, secret: str) -> None:
    redirect = "http://localhost"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "access_type": "offline",
        "prompt": "consent", "scope": "https://www.googleapis.com/auth/youtube.upload"})
    print("Aç:\n" + url + "\n\nİzin verince sayfa açılmayabilir; adres çubuğundaki adresi yapıştır:")
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "code": code_from(input("> ")), "client_id": cid, "client_secret": secret,
        "redirect_uri": redirect, "grant_type": "authorization_code"}, timeout=30).json()
    print("\nYT_REFRESH_TOKEN =", r.get("refresh_token") or r)


def tiktok(key: str, secret: str, redirect: str) -> None:
    url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
        "client_key": key, "response_type": "code", "scope": "user.info.basic,video.publish,video.upload",
        "redirect_uri": redirect, "state": secrets.token_hex(8)})
    print("Aç:\n" + url + "\n\nİzin verdikten sonra yönlendirilen adresi yapıştır:")
    r = requests.post("https://open.tiktokapis.com/v2/oauth/token/", headers={"Content-Type": "application/x-www-form-urlencoded"}, data={
        "client_key": key, "client_secret": secret, "code": code_from(input("> ")),
        "grant_type": "authorization_code", "redirect_uri": redirect}, timeout=30).json()
    print("\nTIKTOK_REFRESH_TOKEN =", r.get("refresh_token") or r)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "youtube":
        youtube(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 5 and sys.argv[1] == "tiktok":
        tiktok(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        sys.exit(__doc__)
