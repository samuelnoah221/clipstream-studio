from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse
from datetime import timedelta

import streamlit as st

APP_NAME = "ClipStream Studio"
MAX_UPLOAD_MB = 500
WORK_DIR = Path(tempfile.gettempdir()) / "clipstream_studio"
WORK_DIR.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title=APP_NAME, page_icon="🎬", layout="wide")

# ----------------------------- UI ---------------------------------------
st.markdown("""
<style>
:root{color-scheme:dark}
.stApp{background:#090c13;color:#f4f5fa}
[data-testid="stHeader"]{background:rgba(9,12,19,.92)}
.block-container{max-width:1550px;padding-top:1.25rem;padding-bottom:3rem}
[data-testid="stMetric"]{background:#151b29;border:1px solid #2b354b;padding:13px;border-radius:14px}
[data-testid="stVerticalBlockBorderWrapper"]{background:#111827;border:1px solid #293449;border-radius:15px}
.stButton>button,.stDownloadButton>button{border-radius:10px;font-weight:650;min-height:2.45rem}
[data-testid="stSidebar"]{background:#0f1623}
.hero{padding:25px;border:1px solid #34405a;border-radius:20px;background:linear-gradient(115deg,#211d3b,#141c2d 58%,#30203b);margin-bottom:1.1rem}
.hero h1{margin:0;letter-spacing:-1.2px;font-size:2.25rem}
.muted{color:#a5b0c4;font-size:.9rem}
.pill{display:inline-block;border:1px solid #46516d;color:#c7d2fe;border-radius:30px;padding:4px 10px;font-size:.78rem;margin:4px 5px 0 0}
.panel{background:#151b29;border:1px solid #2b354b;border-radius:14px;padding:15px}
</style>
""", unsafe_allow_html=True)

# ----------------------------- CORE HELPERS -----------------------------
def run_cmd(args: list[str], timeout: int | None = None):
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)

def ffmpeg_ready() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

def fmt_time(seconds: float) -> str:
    return str(timedelta(seconds=max(0, int(seconds))))

def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")
    return (cleaned or "clip")[:55]

def save_upload(uploaded, suffix: str | None = None) -> Path:
    ext = suffix or Path(uploaded.name).suffix.lower() or ".bin"
    path = WORK_DIR / f"{next(tempfile._get_candidate_names())}{ext}"
    uploaded.seek(0)
    with path.open("wb") as handle:
        while True:
            chunk = uploaded.read(8 * 1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    return path

def probe_video(path: str) -> dict:
    p = run_cmd(["ffprobe", "-v", "error", "-show_entries",
                 "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate",
                 "-of", "json", path], timeout=60)
    if p.returncode:
        raise RuntimeError(p.stderr[-1800:] or "ffprobe could not inspect this file.")
    data = json.loads(p.stdout)
    streams = data.get("streams", [])
    v = next((x for x in streams if x.get("codec_type") == "video"), {})
    a = next((x for x in streams if x.get("codec_type") == "audio"), {})
    return {"duration": float(data.get("format", {}).get("duration", 0) or 0),
            "size": int(data.get("format", {}).get("size", 0) or 0),
            "width": int(v.get("width", 0) or 0), "height": int(v.get("height", 0) or 0),
            "video_codec": v.get("codec_name", "unknown"), "audio_codec": a.get("codec_name", "none"),
            "fps": v.get("r_frame_rate", "unknown")}

def get_openai_client():
    key = None
    try:
        key = st.secrets.get("OPENAI_API_KEY")
    except Exception:
        pass
    key = key or os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=key)
    except Exception as exc:
        raise RuntimeError(f"OpenAI client could not be initialized: {exc}")

def extract_document_text(uploaded) -> str:
    name, raw = uploaded.name.lower(), uploaded.getvalue()
    if name.endswith(".pdf"):
        import fitz
        doc = fitz.open(stream=raw, filetype="pdf")
        return "\n\n".join(page.get_text() for page in doc).strip()
    if name.endswith(".docx"):
        from docx import Document
        import io
        doc = Document(io.BytesIO(raw))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if name.endswith((".txt", ".md", ".csv")):
        return raw.decode("utf-8", errors="replace")
    if name.endswith((".png", ".jpg", ".jpeg", ".webp")):
        from PIL import Image
        import pytesseract, io
        return pytesseract.image_to_string(Image.open(io.BytesIO(raw))).strip()
    raise ValueError("Unsupported file type. Use PDF, DOCX, TXT, MD, CSV or an image.")

def public_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith((".local", ".internal")):
            return False
        infos = socket.getaddrinfo(host, None)
        return all(ipaddress.ip_address(x[4][0]).is_global for x in infos)
    except Exception:
        return False

def read_public_page(url: str) -> tuple[str, str]:
    if not public_http_url(url):
        raise ValueError("Only public HTTP/HTTPS webpages can be read.")
    import requests
    from bs4 import BeautifulSoup
    response = requests.get(url, timeout=20, headers={"User-Agent": "ClipStreamStudio/1.0"}, allow_redirects=True)
    if not public_http_url(response.url):
        raise ValueError("The link redirected to a non-public address.")
    response.raise_for_status()
    if "text/html" not in response.headers.get("content-type", "").lower():
        raise ValueError("This link does not return a readable HTML webpage.")
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else response.url
    main = soup.find("main") or soup.find("article") or soup.body or soup
    content = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    return title, content[:35000]

def parse_json_response(content: str) -> dict:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("AI response did not contain valid JSON.")
    return json.loads(text[start:end+1])

def ai_campaign_analysis(brief: str) -> dict:
    client = get_openai_client()
    if not client:
        raise RuntimeError("Add OPENAI_API_KEY to Streamlit secrets or the environment to use AI analysis.")
    prompt = """You are a careful campaign operations analyst. Read the complete campaign brief.
Return ONLY valid JSON with this schema:
{
 "campaign_name": string, "plain_english_summary": string,
 "deliverables": [string], "platforms": [string],
 "clip_length_seconds": {"min": number|null, "max": number|null},
 "required_scenes_or_moments": [string], "must_include": [string],
 "prohibited_content": [string], "branding_requirements": [string],
 "caption_requirements": [string], "hashtags": [string],
 "links_and_submission_steps": [string], "assets_to_request": [string],
 "unclear_or_conflicting_points": [string], "pre_publish_checklist": [string]
}
Do not invent requirements. If absent, use empty arrays/null. Distinguish explicit rules from recommendations.
"""
    response = client.chat.completions.create(
        model="gpt-4o-mini", temperature=0.1,
        response_format={"type": "json_object"},
        messages=[{"role":"system","content":prompt},
                  {"role":"user","content":brief[:45000]}])
    return parse_json_response(response.choices[0].message.content or "{}")

def extract_audio(video_path: str) -> Path:
    audio_path = WORK_DIR / f"audio_{next(tempfile._get_candidate_names())}.mp3"
    p = run_cmd(["ffmpeg","-y","-i",video_path,"-vn","-ac","1","-ar","16000","-b:a","48k",str(audio_path)], timeout=900)
    if p.returncode:
        raise RuntimeError(p.stderr[-1800:] or "Audio extraction failed.")
    if audio_path.stat().st_size > 24 * 1024 * 1024:
        raise RuntimeError("Extracted audio exceeds the transcription API file limit. Use a shorter source video or split it into sections.")
    return audio_path

def transcribe_video(video_path: str) -> dict:
    client = get_openai_client()
    if not client:
        raise RuntimeError("Add OPENAI_API_KEY to enable AI transcription.")
    audio_path = extract_audio(video_path)
    try:
        with audio_path.open("rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1", file=f, response_format="verbose_json",
                timestamp_granularities=["segment"])
        if isinstance(result, dict):
            data = result
        elif hasattr(result, "model_dump"):
            data = result.model_dump()
        else:
            data = {"text": getattr(result, "text", str(result)), "segments": []}
        return data
    finally:
        audio_path.unlink(missing_ok=True)

def make_contact_sheet(video_path: str, duration: float, max_frames: int = 12):
    from PIL import Image, ImageDraw
    count = max(1, min(max_frames, int(duration // 12) + 1))
    interval = max(1, duration / count)
    paths = []
    for i in range(count):
        ts = min(max(0, duration - 0.1), i * interval)
        frame = WORK_DIR / f"frame_{next(tempfile._get_candidate_names())}.jpg"
        p = run_cmd(["ffmpeg","-y","-ss",str(ts),"-i",video_path,"-frames:v","1","-vf","scale=320:-1","-q:v","5",str(frame)], timeout=90)
        if p.returncode == 0 and frame.exists():
            paths.append((ts, frame))
    if not paths:
        raise RuntimeError("Could not extract video preview frames.")
    tiles = []
    for ts, path in paths:
        im = Image.open(path).convert("RGB")
        draw = ImageDraw.Draw(im)
        draw.rectangle((0,0,110,24), fill=(0,0,0))
        draw.text((7,5), f"{ts:.1f}s", fill=(255,255,255))
        tiles.append(im)
    cols = 3
    rows = (len(tiles)+cols-1)//cols
    w,h = 320, tiles[0].height
    sheet = Image.new("RGB",(cols*w,rows*(h+26)),(18,22,32))
    draw = ImageDraw.Draw(sheet)
    for i,im in enumerate(tiles):
        x=(i%cols)*w; y=(i//cols)*(h+26)
        sheet.paste(im,(x,y))
        draw.text((x+6,y+h+5),f"Frame {i+1}",fill=(220,225,240))
    out = WORK_DIR / f"contact_{next(tempfile._get_candidate_names())}.jpg"
    sheet.save(out, quality=82)
    for _,p in paths: p.unlink(missing_ok=True)
    return out

def ai_find_highlights(brief: str, transcript: dict, contact_sheet: Path, duration: float) -> list[dict]:
    client = get_openai_client()
    if not client:
        raise RuntimeError("Add OPENAI_API_KEY to enable AI video understanding.")
    segments = transcript.get("segments", []) or []
    transcript_lines = []
    for s in segments:
        transcript_lines.append(f"[{float(s.get('start',0)):.1f}-{float(s.get('end',0)):.1f}] {s.get('text','')}")
    transcript_text = "\n".join(transcript_lines)[:24000] or transcript.get("text","")[:24000]
    image_data = base64.b64encode(contact_sheet.read_bytes()).decode("utf-8")
    prompt = f"""You are an expert short-form video editor. Use the campaign brief and timestamped transcript to recommend the strongest real moments.
The contact sheet shows sampled video frames with timestamps. Do not claim to see details not visible. Use transcript timecodes as the main timing guide.
Return ONLY JSON: {{"highlights":[{{"title":"short title","start":0.0,"end":20.0,"reason":"why it fits","campaign_match":"which requirement it satisfies","hook":"opening hook idea","risk":"any compliance concern or empty string","confidence":"high|medium|low"}}]}}
Rules: 3 to 8 suggestions if enough usable content; times must be within 0 and {duration:.1f}; end after start; aim for 12-60 seconds unless brief says otherwise; do not fabricate scenes, dialogue, endorsements or positive reactions; flag uncertain moments; avoid prohibited content from brief. Rank best first.
CAMPAIGN BRIEF:
{brief[:14000]}
TIMESTAMPED TRANSCRIPT:
{transcript_text}
"""
    response = client.chat.completions.create(
        model="gpt-4o-mini", temperature=0.2,
        response_format={"type":"json_object"},
        messages=[{"role":"user","content":[{"type":"text","text":prompt},
                    {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,"+image_data}}]}])
    data = parse_json_response(response.choices[0].message.content or "{}")
    highlights = data.get("highlights", [])
    cleaned = []
    for item in highlights:
        try:
            start = max(0.0, float(item["start"]))
            end = min(float(duration), float(item["end"]))
            if end > start and start < duration:
                cleaned.append({**item,"start":start,"end":end})
        except Exception:
            continue
    return cleaned[:8]

def make_srt(transcript: dict, start: float, end: float) -> str:
    rows = []
    def stamp(sec):
        ms = int(round(max(0,sec)*1000))
        h, rem = divmod(ms,3600000); m, rem = divmod(rem,60000); s, ms = divmod(rem,1000)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"
    idx=1
    for seg in transcript.get("segments",[]) or []:
        a,b=float(seg.get("start",0)),float(seg.get("end",0))
        if b <= start or a >= end: continue
        text = re.sub(r"\s+"," ",str(seg.get("text",""))).strip()
        if not text: continue
        rows.append(f"{idx}\n{stamp(max(0,a-start))} --> {stamp(min(end,b)-start)}\n{text}\n")
        idx+=1
    return "\n".join(rows)

def render_video(source: str, output: str, start: float, end: float, aspect: str,
                 resolution: str, crf: int, logo: str | None = None, srt: str | None = None):
    aspect_filters = {
        "9:16 Vertical": ("crop=ih*9/16:ih","1080:1920"),
        "16:9 Landscape": ("crop=iw:iw*9/16","1920:1080"),
        "1:1 Square": (r"crop=min(iw\,ih):min(iw\,ih)","1080:1080"),
        "4:5 Portrait": ("crop=ih*4/5:ih","1080:1350"),
        "Keep original": (None,None)
    }
    crop, dims = aspect_filters[aspect]
    filters=[]
    if crop: filters.append(crop)
    if resolution != "Source" and dims:
        if resolution == "720p":
            dims = {"1080:1920":"720:1280","1920:1080":"1280:720","1080:1080":"720:720","1080:1350":"720:900"}[dims]
        filters += [f"scale={dims}:force_original_aspect_ratio=decrease:flags=lanczos",f"pad={dims}:(ow-iw)/2:(oh-ih)/2"]
    vf=",".join(filters) if filters else "null"
    args=["ffmpeg","-y","-ss",str(start),"-i",source]
    if logo: args += ["-i",logo]
    graph=[f"[0:v]{vf}[v0]"]; current="v0"
    if logo:
        graph += ["[1:v]scale=iw*0.18:-1[logo]","[v0][logo]overlay=W-w-24:24:format=auto[v1]"]
        current="v1"
    if srt:
        srt_path = Path(srt).resolve().as_posix().replace(":","\\:")
        graph.append(f"[{current}]subtitles='{srt_path}'[vout]")
        current="vout"
    args += ["-filter_complex",";".join(graph),"-map",f"[{current}]","-map","0:a?","-t",str(end-start),
             "-c:v","libx264","-preset","veryfast","-crf",str(crf),"-pix_fmt","yuv420p",
             "-c:a","aac","-b:a","160k","-movflags","+faststart",output]
    p=run_cmd(args,timeout=1800)
    if p.returncode: raise RuntimeError(p.stderr[-2500:] or "FFmpeg render failed.")

def init_state():
    defaults={
        "brief":"","brief_analysis":None,"source_video":None,"video_name":None,"video_info":None,
        "transcript":None,"highlights":[],"clips":[],"logo_path":None,"logo_name":None,
        "project_name":"My campaign","brand_name":"","brand_colour":"#FFFFFF",
        "selected_highlights":[],"social_status":{}
    }
    for k,v in defaults.items():
        if k not in st.session_state: st.session_state[k]=v
init_state()

# ----------------------------- AUTH -------------------------------------
def auth_enabled():
    try: return bool(st.secrets.get("auth"))
    except Exception: return False

if auth_enabled():
    try:
        if not st.user.is_logged_in:
            st.markdown('<div class="hero"><h1>Welcome to ClipStream Studio</h1><p class="muted">Sign in to access your workspace.</p></div>',unsafe_allow_html=True)
            if st.button("Sign in with your account",type="primary"): st.login()
            st.stop()
        with st.sidebar:
            st.caption(f"Signed in: {getattr(st.user,'name','User')}")
            if st.button("Sign out"): st.logout()
    except Exception as exc:
        st.error(f"Authentication setup error: {exc}")
        st.stop()
else:
    with st.sidebar:
        st.caption("Local / single-session mode")
        st.info("Configure OIDC in Streamlit secrets to enable real app sign-in.")

# ----------------------------- HEADER -----------------------------------
st.markdown(f"""<div class="hero">
<div style="color:#a5b4fc;font-weight:750;font-size:.76rem;letter-spacing:.13em">CAMPAIGN INTELLIGENCE · AI EDITING · CREATOR WORKFLOW</div>
<h1>🎬 {APP_NAME}</h1><p class="muted">From campaign instructions to reviewed, platform-ready video exports.</p>
<span class="pill">AI campaign reader</span><span class="pill">AI highlight finder</span><span class="pill">Auto captions</span><span class="pill">Multi-platform exports</span>
</div>""",unsafe_allow_html=True)

if not ffmpeg_ready():
    st.error("FFmpeg and ffprobe are required for video processing. Streamlit Cloud installs them from packages.txt; install them locally and add the bin folder to PATH on Windows.")

with st.sidebar:
    st.title("STUDIO")
    page=st.radio("Navigation",[
        "Overview","Campaign Intelligence","Media Library","AI Clip Finder",
        "Editor & Templates","Brand Kit","Export & Publish","Settings"
    ],label_visibility="collapsed")
    st.divider()
    st.metric("Configured upload limit",f"{MAX_UPLOAD_MB} MB")
    st.caption("Large video processing still depends on hosting RAM, CPU, disk and time limits.")

# ----------------------------- OVERVIEW ---------------------------------
if page=="Overview":
    st.subheader("Production overview")
    a,b,c,d=st.columns(4)
    a.metric("Campaign brief","Analysed" if st.session_state.brief_analysis else "Waiting")
    b.metric("Source video","Loaded" if st.session_state.video_info else "Waiting")
    c.metric("AI highlights",len(st.session_state.highlights))
    d.metric("Rendered exports",len(st.session_state.clips))
    st.markdown("### Your production pipeline")
    cols=st.columns(4)
    for col,(num,title,desc) in zip(cols,[
        ("01","Campaign","Paste the brief, upload documents and add campaign links."),
        ("02","Media","Upload footage, brand logo and supporting assets."),
        ("03","AI editing","Transcribe, inspect sampled frames and rank relevant moments."),
        ("04","Publish","Render platform versions, review compliance and download.")
    ]):
        with col:
            st.markdown(f'<div class="panel"><div style="color:#a5b4fc;font-weight:800">{num}</div><b>{title}</b><p class="muted">{desc}</p></div>',unsafe_allow_html=True)
    if st.session_state.brief_analysis:
        st.success("Campaign analysis is ready.")
    else: st.info("Start in Campaign Intelligence. The campaign brief should be analysed before choosing clips.")

# ----------------------------- CAMPAIGN ---------------------------------
elif page=="Campaign Intelligence":
    st.subheader("1 · Campaign intelligence")
    st.write("Provide the complete campaign instructions first. ClipStream will convert them into a structured production brief.")
    st.text_input("Project / campaign name",key="project_name")
    brief_files=st.file_uploader("Upload brief files (PDF, DOCX, TXT, MD, CSV or screenshots)",type=["pdf","docx","txt","md","csv","png","jpg","jpeg","webp"],accept_multiple_files=True)
    if brief_files and st.button("Extract uploaded brief files"):
        parts=[]
        for f in brief_files:
            try: parts.append(f"--- {f.name} ---\n{extract_document_text(f)}")
            except Exception as e: st.warning(f"{f.name}: {e}")
        if parts:
            st.session_state.brief=(st.session_state.brief+"\n\n"+"\n\n".join(parts)).strip()
            st.success("Brief text extracted.")
    st.text_area("Paste campaign requirements",key="brief",height=260,placeholder="Paste all campaign rules, deliverables, deadlines, example links, do/don't instructions and submission requirements.")
    links=st.text_area("Public campaign links (one URL per line)",height=90,placeholder="https://...")
    if st.button("Read public campaign links",disabled=not links.strip()):
        collected=[]
        with st.spinner("Reading public webpages..."):
            for url in [x.strip() for x in links.splitlines() if x.strip()][:8]:
                try:
                    title,body=read_public_page(url)
                    collected.append(f"--- WEBPAGE: {title} ({url}) ---\n{body}")
                    st.success(f"Read {title}")
                except Exception as e: st.warning(f"Could not read {url}: {e}")
        if collected:
            st.session_state.brief=(st.session_state.brief+"\n\n"+"\n\n".join(collected)).strip()
            st.rerun()
    if st.button("🧠 Analyse complete campaign with AI",type="primary",use_container_width=True):
        if len(st.session_state.brief.strip())<30: st.error("Add the full campaign brief first.")
        else:
            try:
                with st.spinner("AI is extracting deliverables, restrictions, assets and publishing requirements..."):
                    st.session_state.brief_analysis=ai_campaign_analysis(st.session_state.brief)
                st.success("Campaign analysis completed.")
            except Exception as e: st.error(str(e))
    result=st.session_state.brief_analysis
    if result:
        st.divider()
        st.subheader(result.get("campaign_name") or "Campaign summary")
        st.write(result.get("plain_english_summary",""))
        p1,p2,p3=st.columns(3)
        p1.metric("Deliverables",len(result.get("deliverables",[])))
        p2.metric("Platforms",len(result.get("platforms",[])))
        p3.metric("Assets requested",len(result.get("assets_to_request",[])))
        sections=[
            ("Deliverables", "deliverables"),("Platforms","platforms"),
            ("Required scenes and moments","required_scenes_or_moments"),
            ("Must include","must_include"),("Prohibited content","prohibited_content"),
            ("Branding requirements","branding_requirements"),("Captions and hashtags","caption_requirements"),
            ("Links and submission steps","links_and_submission_steps"),("Assets to provide","assets_to_request"),
            ("Unclear points to confirm","unclear_or_conflicting_points"),("Pre-publish checklist","pre_publish_checklist")
        ]
        for title,key in sections:
            values=result.get(key,[]) or []
            with st.expander(f"{title} ({len(values)})",expanded=key in ("assets_to_request","unclear_or_conflicting_points")):
                if values:
                    for value in values: st.markdown(f"- {value}")
                else: st.caption("No explicit information detected in the brief.")
        st.download_button("Download structured campaign plan",json.dumps(result,indent=2),file_name="clipstream_campaign_plan.json",mime="application/json")

# ----------------------------- MEDIA LIBRARY ----------------------------
elif page=="Media Library":
    st.subheader("2 · Media library")
    st.write("Upload the original campaign video and the logo or creative assets required by the brief.")
    if st.session_state.brief_analysis:
        assets=st.session_state.brief_analysis.get("assets_to_request",[]) or []
        if assets:
            st.markdown("**AI asset request**")
            for x in assets: st.write("• "+x)
    video=st.file_uploader("Upload source campaign video",type=["mp4","mov","mkv","webm","m4v"])
    if video and video.size>MAX_UPLOAD_MB*1024*1024:
        st.error("The configured application limit is 500 MB.")
    elif video and st.button("Load and inspect source video",type="primary"):
        try:
            with st.spinner("Saving and inspecting source..."):
                path=save_upload(video)
                info=probe_video(str(path))
                st.session_state.source_video=str(path)
                st.session_state.video_name=video.name
                st.session_state.video_info=info
                st.session_state.transcript=None
                st.session_state.highlights=[]
                st.session_state.clips=[]
            st.success("Source video loaded.")
        except Exception as e: st.error(f"Could not load video: {e}")
    logo=st.file_uploader("Upload campaign logo (transparent PNG recommended)",type=["png","jpg","jpeg","webp"],key="library_logo")
    if logo and st.button("Save logo to Brand Kit"):
        st.session_state.logo_path=str(save_upload(logo))
        st.session_state.logo_name=logo.name
        st.success("Logo saved for this workspace session.")
    extras=st.file_uploader("Additional product images / reference photos",type=["png","jpg","jpeg","webp"],accept_multiple_files=True)
    if extras: st.caption(f"{len(extras)} reference image(s) selected. Logo overlay is supported in rendering; product-image scene insertion is not automated.")
    if st.session_state.video_info:
        i=st.session_state.video_info
        st.video(st.session_state.source_video)
        a,b,c,d=st.columns(4)
        a.metric("Duration",fmt_time(i["duration"]))
        b.metric("Resolution",f"{i['width']} × {i['height']}")
        c.metric("Video codec",i["video_codec"])
        d.metric("Size",f"{i['size']/1024/1024:.1f} MB")
        st.caption(f"File: {st.session_state.video_name} · Audio: {i['audio_codec']} · FPS: {i['fps']}")

# ----------------------------- AI CLIP FINDER ---------------------------
elif page=="AI Clip Finder":
    st.subheader("3 · AI video understanding and highlight finder")
    if not st.session_state.brief_analysis: st.warning("Analyse the campaign brief first.")
    if not st.session_state.video_info: st.warning("Upload your source video in Media Library first.")
    st.write("ClipStream transcribes the audio, samples video frames, compares the content with campaign rules and recommends time-coded highlights.")
    if st.session_state.video_info and st.session_state.brief_analysis:
        duration=st.session_state.video_info["duration"]
        interval=st.slider("Visual sampling density (seconds between sampled moments)",5,30,12)
        if st.button("🔎 Analyse video and find campaign moments",type="primary",use_container_width=True):
            try:
                with st.spinner("Step 1/3 · Transcribing video audio..."):
                    transcript=transcribe_video(st.session_state.source_video)
                    st.session_state.transcript=transcript
                with st.spinner("Step 2/3 · Sampling visual scenes..."):
                    sheet=make_contact_sheet(st.session_state.source_video,duration,max_frames=max(4,min(20,int(duration/interval)+1)))
                with st.spinner("Step 3/3 · Matching transcript and visuals to campaign rules..."):
                    brief=json.dumps(st.session_state.brief_analysis,ensure_ascii=False)
                    st.session_state.highlights=ai_find_highlights(brief,transcript,sheet,duration)
                st.success(f"Analysis complete. {len(st.session_state.highlights)} candidate moments found.")
                st.image(str(sheet),caption="Sampled video frames used for visual context",use_container_width=True)
            except Exception as e: st.error(f"Video analysis could not complete: {e}")
        if st.session_state.transcript:
            with st.expander("View speech transcript"):
                st.text(st.session_state.transcript.get("text","")[:25000])
        if st.session_state.highlights:
            st.markdown("### Recommended moments")
            for idx,h in enumerate(st.session_state.highlights):
                with st.container(border=True):
                    top,select=st.columns([5,1])
                    with top:
                        st.markdown(f"**{idx+1}. {h.get('title','Suggested clip')}**")
                        st.caption(f"{fmt_time(h['start'])} – {fmt_time(h['end'])} · {(h['end']-h['start']):.1f} seconds · Confidence: {h.get('confidence','not stated')}")
                    with select:
                        checked=st.checkbox("Select",key=f"highlight_{idx}",value=idx==0)
                        if checked and idx not in st.session_state.selected_highlights: st.session_state.selected_highlights.append(idx)
                        if not checked and idx in st.session_state.selected_highlights: st.session_state.selected_highlights.remove(idx)
                    st.write(h.get("reason",""))
                    st.caption("Campaign match: "+str(h.get("campaign_match","Not specified")))
                    if h.get("hook"): st.caption("Hook: "+h["hook"])
                    if h.get("risk"): st.warning("Review concern: "+h["risk"])
            st.info("AI suggestions are candidates, not guaranteed campaign approvals. Preview and check the complete context before publishing.")

# ----------------------------- EDITOR -----------------------------------
elif page=="Editor & Templates":
    st.subheader("4 · Editor and platform templates")
    if not st.session_state.video_info:
        st.info("Load a video in Media Library.")
    else:
        info=st.session_state.video_info
        st.video(st.session_state.source_video)
        ai=st.session_state.highlights
        options=["Manual timestamps"]+[f"{i+1}. {h.get('title','Highlight')} ({h['start']:.1f}s–{h['end']:.1f}s)" for i,h in enumerate(ai)]
        choice=st.selectbox("Choose an AI recommendation or manual edit",options)
        if choice!="Manual timestamps":
            idx=options.index(choice)-1
            chosen=ai[idx]
            start_default=float(chosen["start"]); end_default=float(chosen["end"])
        else:
            start_default=0.0; end_default=min(30.0,info["duration"])
        a,b=st.columns(2)
        with a:
            start=st.number_input("Clip start (seconds)",0.0,float(max(info["duration"],.1)),float(start_default),step=.5)
            end=st.number_input("Clip end (seconds)",0.1,float(max(info["duration"],.1)),float(max(end_default,.1)),step=.5)
            clip_name=st.text_input("Clip name",value=safe_name(Path(st.session_state.video_name).stem+"_clip"))
        with b:
            template=st.selectbox("Platform template",["TikTok / Instagram Reels / YouTube Shorts (9:16)","YouTube landscape (16:9)","Facebook feed (1:1)","Instagram portrait (4:5)","Original aspect ratio"])
            aspect_map={"TikTok / Instagram Reels / YouTube Shorts (9:16)":"9:16 Vertical","YouTube landscape (16:9)":"16:9 Landscape","Facebook feed (1:1)":"1:1 Square","Instagram portrait (4:5)":"4:5 Portrait","Original aspect ratio":"Keep original"}
            aspect=aspect_map[template]
            resolution=st.selectbox("Output resolution",["1080p","720p","Source"])
            crf=st.slider("Encoding quality (lower = higher quality)",16,28,19)
            add_logo=st.checkbox("Apply saved campaign logo",value=bool(st.session_state.logo_path))
            add_captions=st.checkbox("Generate and burn subtitles from transcript",value=bool(st.session_state.transcript),disabled=not bool(st.session_state.transcript))
        if st.button("Render selected clip",type="primary",use_container_width=True):
            if end<=start or end>info["duration"]: st.error("Choose valid timestamps within the source duration.")
            else:
                out=WORK_DIR/f"{safe_name(clip_name)}_{len(st.session_state.clips)+1}_{safe_name(aspect)}.mp4"
                srt_path=None
                try:
                    if add_captions:
                        srt=make_srt(st.session_state.transcript or {},start,end)
                        if srt.strip():
                            srt_path=WORK_DIR/f"{safe_name(clip_name)}.srt"
                            srt_path.write_text(srt,encoding="utf-8")
                    with st.spinner("Rendering final MP4..."):
                        render_video(st.session_state.source_video,str(out),start,end,aspect,resolution,crf,
                                     st.session_state.logo_path if add_logo else None,str(srt_path) if srt_path else None)
                    st.session_state.clips.append({"name":clip_name,"path":str(out),"start":start,"end":end,"aspect":aspect,"resolution":resolution})
                    st.success("Clip rendered.")
                    st.video(str(out))
                except Exception as e: st.error(f"Render failed: {e}")
        if st.session_state.highlights:
            st.markdown("#### Batch render selected AI highlights")
            selected=[i for i in st.session_state.selected_highlights if i<len(ai)]
            st.caption(f"{len(selected)} highlight(s) selected. Batch rendering uses the current platform template, resolution and logo/caption choices.")
            if st.button("Render all selected highlights",disabled=not selected):
                progress=st.progress(0)
                for n,idx in enumerate(selected):
                    h=ai[idx]
                    out=WORK_DIR/f"{safe_name(h.get('title','highlight'))}_{len(st.session_state.clips)+1}.mp4"
                    try:
                        srt_path=None
                        if add_captions:
                            srt=make_srt(st.session_state.transcript or {},h["start"],h["end"])
                            if srt.strip():
                                srt_path=WORK_DIR/f"{safe_name(h.get('title','clip'))}_{n}.srt"
                                srt_path.write_text(srt,encoding="utf-8")
                        render_video(st.session_state.source_video,str(out),h["start"],h["end"],aspect,resolution,crf,
                                     st.session_state.logo_path if add_logo else None,str(srt_path) if srt_path else None)
                        st.session_state.clips.append({"name":h.get("title","AI highlight"),"path":str(out),"start":h["start"],"end":h["end"],"aspect":aspect,"resolution":resolution})
                    except Exception as e: st.error(f"{h.get('title')}: {e}")
                    progress.progress((n+1)/len(selected))
                st.success("Batch rendering finished.")

# ----------------------------- BRAND KIT --------------------------------
elif page=="Brand Kit":
    st.subheader("Brand Kit")
    logo=st.file_uploader("Upload or replace brand logo",type=["png","jpg","jpeg","webp"])
    if logo and st.button("Save brand logo"):
        st.session_state.logo_path=str(save_upload(logo))
        st.session_state.logo_name=logo.name
        st.success("Logo saved.")
    if st.session_state.logo_path and Path(st.session_state.logo_path).exists():
        st.image(st.session_state.logo_path,caption=st.session_state.logo_name or "Current logo",width=220)
    st.text_input("Brand / campaign name",key="brand_name")
    st.text_input("Primary brand colour (HEX)",key="brand_colour")
    st.caption("Logo overlay is supported in video exports. Animated brand kits, multiple logo placements and custom font packs are future extensions.")

# ----------------------------- EXPORT & PUBLISH --------------------------
elif page=="Export & Publish":
    st.subheader("5 · Export and publishing workspace")
    st.write("Review rendered videos, download platform-ready files and open the official platform publishing tools.")
    if not st.session_state.clips: st.info("Render clips in Editor & Templates first.")
    for i,clip in enumerate(st.session_state.clips):
        with st.container(border=True):
            left,right=st.columns([2,1])
            with left:
                st.markdown(f"**{clip['name']}**")
                st.caption(f"{fmt_time(clip['start'])}–{fmt_time(clip['end'])} · {clip['aspect']} · {clip['resolution']}")
                if Path(clip["path"]).exists(): st.video(clip["path"])
            with right:
                if Path(clip["path"]).exists():
                    with open(clip["path"],"rb") as f:
                        st.download_button("Download MP4",f,file_name=Path(clip["path"]).name,mime="video/mp4",key=f"export_{i}",use_container_width=True)
                    st.caption(f"{Path(clip['path']).stat().st_size/1024/1024:.1f} MB")
    st.divider()
    st.markdown("### Social publishing destinations")
    platforms=[
        ("YouTube Studio","https://studio.youtube.com/","Upload videos, titles, descriptions and thumbnails."),
        ("TikTok Studio","https://www.tiktok.com/tiktokstudio","Upload and schedule eligible TikTok content."),
        ("Instagram","https://www.instagram.com/","Publish Reels and posts through the Instagram app or supported tools."),
        ("Meta Business Suite","https://business.facebook.com/","Manage Facebook Pages and Instagram professional accounts.")
    ]
    cols=st.columns(2)
    for col,(name,url,desc) in zip(cols*2,platforms):
        with col:
            st.markdown(f"**{name}**")
            st.caption(desc)
            st.link_button(f"Open {name}",url,use_container_width=True)
    st.warning("This build exports videos and opens official publishing destinations. It does not yet authenticate to or publish directly through social platform APIs. Direct publishing needs separate developer apps, OAuth consent, platform permissions and often review/approval. Do not enter social passwords into ClipStream.")

# ----------------------------- SETTINGS ----------------------------------
elif page=="Settings":
    st.subheader("Settings and integrations")
    st.markdown("### AI configuration")
    if get_openai_client():
        st.success("OpenAI API key detected. AI campaign analysis and video analysis are enabled.")
    else:
        st.warning("OpenAI API key not detected. Add OPENAI_API_KEY to Streamlit secrets or an environment variable.")
    st.code('OPENAI_API_KEY = "your-api-key"',language="toml")
    st.caption("For Streamlit Cloud, add this in App settings → Secrets. Never commit API keys to GitHub.")
    st.markdown("### App authentication")
    if auth_enabled():
        st.success("Streamlit OIDC authentication settings detected.")
    else:
        st.info("App sign-in is optional and not configured. Configure an OpenID Connect provider under Streamlit [auth] secrets to enable real sign-in/sign-out. This app does not store plain-text passwords.")
    st.markdown("### Current platform integration status")
    st.write("- Video rendering: FFmpeg")
    st.write("- Campaign understanding: OpenAI API")
    st.write("- Audio transcription: OpenAI Whisper API")
    st.write("- Visual scene sampling: FFmpeg + image analysis")
    st.write("- OCR: Tesseract")
    st.write("- Translation: optional deep-translator service")
    st.write("- Social publishing: export + official platform links; direct API publishing not configured")
    st.markdown("### Important operating limits")
    st.write("- 500 MB is the configured upload limit, not a guarantee that the host can process every file of that size.")
    st.write("- AI calls cost money according to your API usage and video length.")
    st.write("- Workspace files and results are temporary in this release; persistent cloud storage and background workers are needed for production-scale use.")
    st.write("- AI highlights can be wrong. Preview clips and check the entire campaign brief before submission.")

st.divider()
st.caption("ClipStream Studio · Use only content you own or are authorised to edit. Human-review campaign compliance before publishing.")
