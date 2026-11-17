import json, os, random, subprocess, textwrap
from pathlib import Path
import requests
OUT=Path("output"); OUT.mkdir(exist_ok=True)
OPENAI_KEY=os.environ["OPENAI_API_KEY"]; PIXABAY_KEY=os.environ["PIXABAY_API_KEY"]
def post(url, body, timeout=180):
    r=requests.post(url,json=body,headers={"Authorization":"Bearer "+OPENAI_KEY},timeout=timeout); r.raise_for_status(); return r
def make_story():
    prompt='Türkçe sosyal medya kısa videosu için özgün, doğrulanabilir ve görsel olarak anlatılabilir bir konu seç. 45-60 saniyelik anlatım yaz. İlk cümle ilk 2 saniyede güçlü merak uyandırsın; klişe giriş kullanma. Metin 105-130 kelime olsun, kısa ve konuşma dilinde cümleler kur. Her 1-2 cümlede yeni görsel kullanılabilecek somut sahne/nesne olsun. Sonda yorum yazdıracak tek soru sor. Ayrıca 1) 5-8 kelimelik vurucu video başlığı, 2) TikTok/YouTube için doğal kısa açıklama ve 3-5 alakalı hashtag, 3) Pixabay için 3-5 İngilizce arama kelimesi üret. Yalnızca JSON döndür: {"title":"başlık","script":"seslendirme metni","caption":"açıklama ve hashtagler","pixabay_query":"English search terms"}. Politik propaganda, doğrulanmamış iddia ve clickbait yalan kullanma.'
    j=post("https://api.openai.com/v1/chat/completions",{"model":"gpt-4o-mini","temperature":0.95,"messages":[{"role":"system","content":"Güvenilir Türkçe kısa video senaristisin. Sadece JSON üret."},{"role":"user","content":prompt}]}).json()
    raw=j["choices"][0]["message"]["content"].strip().strip(chr(96)).replace("json","",1).strip()
    obj=json.loads(raw)
    for k in ("title","script","caption","pixabay_query"):
        if not obj.get(k): raise ValueError("Eksik alan: "+k)
    return obj
def get_audio(script):
    r=post("https://api.openai.com/v1/audio/speech",{"model":"gpt-4o-mini-tts","voice":"onyx","format":"mp3","input":script,"instructions":"Türkçe konuş. Doğal, anlaşılır, tok erkek anlatıcı sesi kullan. Orta hızda ve net telaffuzla oku."})
    p=OUT/"voice.mp3"; p.write_bytes(r.content); return p
def find_clips(q):
    r=requests.get("https://pixabay.com/api/videos/",params={"key":PIXABAY_KEY,"q":q,"per_page":15,"safesearch":"true"},timeout=60); r.raise_for_status()
    hits=r.json().get("hits",[]); random.shuffle(hits); urls=[]
    for h in hits:
        v=h.get("videos",{}).get("medium") or h.get("videos",{}).get("small")
        if v and v.get("url"): urls.append(v["url"])
    if not urls: raise RuntimeError("Pixabay video bulamadı: "+q)
    return urls[:6]
def media_duration(p):
    x=subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration","-of","default=nw=1:nk=1",str(p)],text=True); return float(x.strip())
def stamp(t):
    n=int(t*1000); h=n//3600000; n%=3600000; m=n//60000; n%=60000; s=n//1000; n%=1000
    return "%02d:%02d:%02d,%03d"%(h,m,s,n)
def captions(script,total):
    words=script.split(); chunks=[" ".join(words[i:i+5]) for i in range(0,len(words),5)]; weights=[len(c.split()) for c in chunks]; denom=sum(weights); pos=0; out=[]
    for i,(c,w) in enumerate(zip(chunks,weights),1):
        end=total*sum(weights[:i])/denom; out += [str(i),stamp(pos)+" --> "+stamp(end),textwrap.fill(c,25), ""]; pos=end
    p=OUT/"captions.srt"; p.write_text("\n".join(out),encoding="utf-8"); return p
def main():
    story=make_story(); (OUT/"metadata.json").write_text(json.dumps(story,ensure_ascii=False,indent=2),encoding="utf-8")
    audio=get_audio(story["script"]); total=media_duration(audio); urls=find_clips(story["pixabay_query"])
    segments=[]; seglen=8.0; count=max(1,int(total//seglen)+1)
    for i in range(count):
        raw=OUT/("raw_%02d.mp4"%i); seg=OUT/("seg_%02d.mp4"%i)
        rr=requests.get(urls[i%len(urls)],timeout=120); rr.raise_for_status(); raw.write_bytes(rr.content)
        take=min(seglen,total-i*seglen)
        subprocess.run(["ffmpeg","-y","-stream_loop","-1","-i",str(raw),"-t",str(take),"-vf","scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,setsar=1","-an","-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p",str(seg)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); segments.append(seg)
    listing=OUT/"concat.txt"; listing.write_text("".join("file '"+str(p.resolve())+"'\n" for p in segments),encoding="utf-8")
    silent=OUT/"silent.mp4"; subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(listing),"-c","copy",str(silent)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    srt=captions(story["script"],total); final=OUT/"video.mp4"
    title_srt=OUT/"title.srt"; title_srt.write_text("1\\n00:00:00,000 --> 00:00:03,000\\n"+textwrap.fill(story["title"],22)+"\\n",encoding="utf-8")\n    vf="subtitles="+str(srt.resolve())+":force_style='FontName=DejaVu Sans,FontSize=38,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,BorderStyle=1,Outline=4,Shadow=1,Alignment=2,MarginV=190',subtitles="+str(title_srt.resolve())+":force_style='FontName=DejaVu Sans,FontSize=54,Bold=1,PrimaryColour=&H0000D7FF,OutlineColour=&H00000000,BorderStyle=1,Outline=5,Shadow=2,Alignment=8,MarginV=300'"
    subprocess.run(["ffmpeg","-y","-i",str(silent),"-i",str(audio),"-vf",vf,"-map","0:v:0","-map","1:a:0","-t",str(total),"-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p","-c:a","aac","-b:a","160k","-movflags","+faststart",str(final)],check=True)
    print(json.dumps({"title":story["title"],"caption":story["caption"],"query":story["pixabay_query"],"seconds":round(total,1)},ensure_ascii=False))
if __name__=="__main__": main()
