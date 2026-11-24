from __future__ import annotations

import base64
import html
import io
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st
import streamlit.components.v1 as components

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

def escape_drawtext(value: str) -> str:
    txt = re.sub(r"\s+", " ", (value or "").strip())
    txt = txt.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:").replace("%", "\\%")
    return txt[:140]

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
    p = run_cmd([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate",
        "-of", "json", path
    ], timeout=60)
    if p.returncode:
        raise RuntimeError(p.stderr[-1800:] or "ffprobe could not inspect this file.")
    data = json.loads(p.stdout)
    streams = data.get("streams", [])
    v = next((x for x in streams if x.get("codec_type") == "video"), {})
    a = next((x for x in streams if x.get("codec_type") == "audio"), {})
    return {
        "duration": float(data.get("format", {}).get("duration", 0) or 0),
        "size": int(data.get("format", {}).get("size", 0) or 0),
        "width": int(v.get("width", 0) or 0),
        "height": int(v.get("height", 0) or 0),
        "video_codec": v.get("codec_name", "unknown"),
        "audio_codec": a.get("codec_name", "none"),
        "fps": v.get("r_frame_rate", "unknown"),
    }

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
        doc = Document(io.BytesIO(raw))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if name.endswith((".txt", ".md", ".csv")):
        return raw.decode("utf-8", errors="replace")
    if name.endswith((".png", ".jpg", ".jpeg", ".webp")):
        from PIL import Image
        import pytesseract
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

def render_fast_voice_reader(text: str, label: str = "🔊 Fast Readout (Zero Data)"):
    clean = (text or "").strip()
    if not clean:
        st.caption("Nothing to read yet.")
        return
    payload = json.dumps(clean[:3500], ensure_ascii=False)
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    safe_label = html.escape(label)
    components.html(
        f"""
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-family:system-ui,-apple-system,sans-serif;">
            <button id="fastSpeakBtn" style="background:#4f46e5;color:#fff;border:none;border-radius:8px;padding:8px 14px;font-weight:600;cursor:pointer;">
                {safe_label}
            </button>
            <button id="fastStopBtn" style="background:#1e293b;color:#f8fafc;border:1px solid #334155;border-radius:8px;padding:8px 12px;font-weight:600;cursor:pointer;">
                ⏹ Stop
            </button>
            <span id="fastTtsStatus" style="font-size:12.5px;color:#94a3b8;"></span>
        </div>
        <script>
        const txt = {payload};
        const stEl = document.getElementById('fastTtsStatus');
        function runSpeak() {{
            if (!('speechSynthesis' in window)) {{
                stEl.textContent = 'Speech synthesis unavailable in browser.';
                return;
            }}
            window.speechSynthesis.cancel();
            const u = new SpeechSynthesisUtterance(txt);
            u.rate = 1.15;
            u.onstart = () => stEl.textContent = 'Reading fast...';
            u.onend = () => stEl.textContent = 'Done.';
            window.speechSynthesis.speak(u);
        }}
        document.getElementById('fastSpeakBtn').onclick = runSpeak;
        document.getElementById('fastStopBtn').onclick = () => {{
            if ('speechSynthesis' in window) window.speechSynthesis.cancel();
            stEl.textContent = 'Stopped.';
        }};
        </script>
        """,
        height=46,
    )

# ----------------------------- NON-REPEATING WINDOW PICKER --------------
def overlaps_used(start: float, end: float, used_windows: list[list[float]], min_gap: float = 2.0) -> bool:
    for u_start, u_end in used_windows:
        if start < (u_end + min_gap) and end > (u_start - min_gap):
            return True
    return False

def pick_fresh_windows(
    duration: float,
    clip_seconds: float,
    count: int,
    used_windows: list[list[float]],
    prefer_8s_hook: bool = False,
    transcript: dict | None = None,
) -> list[tuple[float, float, str]]:
    """Pick 1-2 windows of desired length without repeating previously used ranges when possible."""
    if duration <= 0:
        return []
    count = max(1, min(2, int(count)))
    target_len = 8.0 if prefer_8s_hook else max(2.0, min(duration, float(clip_seconds)))
    target_len = min(duration, target_len)

    max_start = max(0.0, duration - target_len)
    if max_start <= 0.0:
        anchors = [0.0]
    else:
        fractions = [0.12, 0.34, 0.58, 0.78, 0.22, 0.68, 0.0, 0.90]
        random.shuffle(fractions)
        anchors = [round(max(0.0, min(max_start, f * max_start)), 1) for f in fractions]
        for _ in range(8):
            anchors.append(round(random.uniform(0.0, max_start), 1))

    segs = (transcript or {}).get("segments") or []
    picked: list[tuple[float, float, str]] = []

    def spoken_at(s_sec: float, e_sec: float) -> str:
        return " ".join(
            str(sg.get("text", "")).strip()
            for sg in segs
            if float(sg.get("end", 0)) >= s_sec and float(sg.get("start", 0)) <= e_sec
        ).strip()

    combined_used = list(used_windows)
    for anc in anchors:
        if len(picked) >= count:
            break
        s_val = round(max(0.0, min(max_start, anc)), 1)
        e_val = round(min(duration, s_val + target_len), 1)
        if e_val <= s_val:
            continue
        if not overlaps_used(s_val, e_val, combined_used, min_gap=2.0):
            picked.append((s_val, e_val, spoken_at(s_val, e_val)))
            combined_used.append([s_val, e_val])

    if len(picked) < count:
        for anc in anchors:
            if len(picked) >= count:
                break
            s_val = round(max(0.0, min(max_start, anc)), 1)
            e_val = round(min(duration, s_val + target_len), 1)
            if e_val <= s_val:
                continue
            if any(abs(s_val - ps) < 1.5 and abs(e_val - pe) < 1.5 for ps, pe, _ in picked):
                continue
            picked.append((s_val, e_val, spoken_at(s_val, e_val)))

    return picked[:count]

# ----------------------------- CAMPAIGN & CLIP INTELLIGENCE -------------
def heuristic_campaign_analysis(brief: str, project_name: str) -> dict:
    low = brief.lower()
    platforms = []
    for p_label, keys in [
        ("YouTube", ["youtube", "yt", "shorts"]),
        ("Instagram", ["instagram", "ig", "reels"]),
        ("TikTok", ["tiktok"]),
        ("Facebook", ["facebook", "fb"]),
    ]:
        if any(k in low for k in keys):
            platforms.append(p_label)
    if not platforms:
        platforms = ["YouTube", "Instagram"]

    wants_onscreen_captions = any(
        k in low for k in ["caption reading", "on-screen caption", "on screen caption", "subtitles", "captions", "text on screen", "writing on screen"]
    )
    wants_8s_hook = any(
        k in low for k in ["8s hook", "8-second hook", "8 sec hook", "eight second hook", "8 seconds hook", "first 8s", "first 8 seconds"]
    )
    wants_video_hook = wants_8s_hook or any(
        k in low for k in ["hook", "opening hook", "visual hook", "hook through video"]
    )

    hashtags = sorted(set(re.findall(r"#[A-Za-z0-9_]+", brief)))[:10]
    lines = [ln.strip(" -•*\t") for ln in brief.splitlines() if ln.strip()]
    must_include = [ln for ln in lines if any(w in ln.lower() for w in ["include", "must", "mention", "call to action", "cta", "show", "hook", "caption"])][:6]
    prohibited = [ln for ln in lines if any(w in ln.lower() for w in ["avoid", "don't", "do not", "never", "prohibited", "no "])][:6]
    caption_reqs = [ln for ln in lines if any(w in ln.lower() for w in ["caption", "hashtag", "tag", "description", "link"])][:6]

    return {
        "campaign_name": project_name.strip() or "Campaign Brief",
        "plain_english_summary": (
            f"Campaign targeting {', '.join(platforms)} based on {len(lines)} brief line(s). "
            f"On-screen captions: {'Required' if wants_onscreen_captions else 'Optional'}. "
            f"Hook rule: {'8-second hook' if wants_8s_hook else ('Video hook' if wants_video_hook else 'Standard opening')}."
        ),
        "deliverables": ["1 to 2 clips of custom length tailored to target platform"],
        "platforms": platforms,
        "requires_onscreen_captions": wants_onscreen_captions,
        "requires_8s_hook": wants_8s_hook,
        "requires_video_hook": wants_video_hook,
        "clip_length_seconds": {"min": 8 if wants_8s_hook else 15, "max": 60},
        "required_scenes_or_moments": must_include[:4],
        "must_include": must_include,
        "prohibited_content": prohibited,
        "branding_requirements": ["Include brand logo/handle if specified in campaign brief"],
        "caption_requirements": caption_reqs,
        "hashtags": hashtags,
        "links_and_submission_steps": [],
        "assets_to_request": [],
        "unclear_or_conflicting_points": [] if len(brief.strip()) >= 30 else ["Brief is short—add more details on hooks, on-screen captions, or rules for sharper alignment."],
        "pre_publish_checklist": [
            "Confirm top hook banner is clear (8s or full clip)",
            "Verify lower-third visible captions are legible and accurate",
            "Check no prohibited terms or visuals appear",
        ],
    }

def ai_campaign_analysis(brief: str, project_name: str = "") -> dict:
    client = get_openai_client()
    base_heur = heuristic_campaign_analysis(brief, project_name)
    if not client:
        return base_heur
    prompt = """You are a careful campaign operations analyst. Read the complete campaign brief.
Return ONLY valid JSON with this schema:
{
 "campaign_name": string, "plain_english_summary": string,
 "requires_onscreen_captions": boolean, "requires_8s_hook": boolean, "requires_video_hook": boolean,
 "deliverables": [string], "platforms": [string],
 "clip_length_seconds": {"min": number|null, "max": number|null},
 "required_scenes_or_moments": [string], "must_include": [string],
 "prohibited_content": [string], "branding_requirements": [string],
 "caption_requirements": [string], "hashtags": [string],
 "links_and_submission_steps": [string], "assets_to_request": [string],
 "unclear_or_conflicting_points": [string], "pre_publish_checklist": [string]
}
Do not invent requirements. If absent, use empty arrays/false/null. Distinguish explicit rules from recommendations.
"""
    response = client.chat.completions.create(
        model="gpt-4o-mini", temperature=0.1,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": brief[:45000]},
        ]
    )
    parsed = parse_json_response(response.choices[0].message.content or "{}")
    parsed.setdefault("requires_onscreen_captions", base_heur["requires_onscreen_captions"])
    parsed.setdefault("requires_8s_hook", base_heur["requires_8s_hook"])
    parsed.setdefault("requires_video_hook", base_heur["requires_video_hook"])
    return parsed

def evaluate_video_against_campaign(analysis: dict, video_info: dict, transcript: dict | None) -> dict:
    strengths = []
    gaps = []
    warnings = list(analysis.get("prohibited_content", []) or [])

    dur = float(video_info.get("duration", 0) or 0)
    w = int(video_info.get("width", 0) or 0)
    h = int(video_info.get("height", 0) or 0)
    if dur > 0:
        strengths.append(f"Video duration is {dur:.1f}s ({fmt_time(dur)}) at {w}x{h} (sharp rendering preserves detail or better).")
    if dur < 8:
        gaps.append("Source video is under 8 seconds; an 8s hook will use the full available clip length.")
    if analysis.get("requires_onscreen_captions"):
        strengths.append("Campaign asks for visible on-screen captions/writing — lower-third captions are enabled.")
    if analysis.get("requires_8s_hook"):
        strengths.append("Campaign asks for an 8-second hook — 8s top hook banner mode is ready.")
    elif analysis.get("requires_video_hook"):
        strengths.append("Campaign asks for a video hook — top hook banner framing is ready.")

    must_items = analysis.get("must_include", []) or []
    req_scenes = analysis.get("required_scenes_or_moments", []) or []
    t_text = ((transcript or {}).get("text") or "").lower()

    if transcript and t_text.strip():
        strengths.append(f"Speech transcript captured ({len(t_text.split())} words) for lower-third captions & timing.")
        for req in (must_items + req_scenes)[:5]:
            key_words = [kw for kw in re.findall(r"[a-z]{4,}", req.lower()) if kw not in {"with", "from", "that", "this", "your", "show", "must", "include", "hook", "caption"}]
            if key_words and any(kw in t_text for kw in key_words[:4]):
                strengths.append(f"Transcript shows alignment with: {req}")
            else:
                gaps.append(f"Verify spoken/visual coverage for requirement: {req}")
    else:
        gaps.append("No speech transcript yet—you can still pick fresh video segments of your chosen length and add custom hook/caption text.")

    return {
        "ready": True,
        "verdict": "Ready for Go-Ahead — Clip Extraction Unlocked",
        "strengths": strengths,
        "gaps": gaps,
        "warnings": warnings,
    }

def extract_audio(video_path: str) -> Path:
    audio_path = WORK_DIR / f"audio_{next(tempfile._get_candidate_names())}.mp3"
    p = run_cmd(["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio_path)], timeout=900)
    if p.returncode:
        raise RuntimeError(p.stderr[-1800:] or "Audio extraction failed.")
    if audio_path.stat().st_size > 24 * 1024 * 1024:
        raise RuntimeError("Extracted audio exceeds the transcription API file limit.")
    return audio_path

def transcribe_video(video_path: str) -> dict:
    client = get_openai_client()
    if not client:
        raise RuntimeError("OPENAI_API_KEY not set.")
    audio_path = extract_audio(video_path)
    try:
        with audio_path.open("rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1", file=f, response_format="verbose_json",
                timestamp_granularities=["segment"]
            )
        if isinstance(result, dict):
            return result
        if hasattr(result, "model_dump"):
            return result.model_dump()
        return {"text": getattr(result, "text", str(result)), "segments": []}
    finally:
        audio_path.unlink(missing_ok=True)

def make_contact_sheet(video_path: str, duration: float, max_frames: int = 12) -> Path:
    from PIL import Image, ImageDraw
    count = max(1, min(max_frames, int(duration // 12) + 1))
    interval = max(1.0, duration / count)
    paths = []
    for i in range(count):
        ts = min(max(0.0, duration - 0.1), i * interval)
        frame = WORK_DIR / f"frame_{next(tempfile._get_candidate_names())}.jpg"
        p = run_cmd(["ffmpeg", "-y", "-ss", str(ts), "-i", video_path, "-frames:v", "1", "-vf", "scale=320:-1", "-q:v", "5", str(frame)], timeout=90)
        if p.returncode == 0 and frame.exists():
            paths.append((ts, frame))
    if not paths:
        raise RuntimeError("Could not extract video preview frames.")
    tiles = []
    for ts, path in paths:
        im = Image.open(path).convert("RGB")
        draw = ImageDraw.Draw(im)
        draw.rectangle((0, 0, 110, 24), fill=(0, 0, 0))
        draw.text((7, 5), f"{ts:.1f}s", fill=(255, 255, 255))
        tiles.append(im)
    cols = 3
    rows = (len(tiles) + cols - 1) // cols
    w, h = 320, tiles[0].height
    sheet = Image.new("RGB", (cols * w, rows * (h + 26)), (18, 22, 32))
    draw = ImageDraw.Draw(sheet)
    for i, im in enumerate(tiles):
        x = (i % cols) * w
        y = (i // cols) * (h + 26)
        sheet.paste(im, (x, y))
        draw.text((x + 6, y + h + 5), f"Frame {i+1}", fill=(220, 225, 240))
    out = WORK_DIR / f"contact_{next(tempfile._get_candidate_names())}.jpg"
    sheet.save(out, quality=82)
    for _, p in paths:
        p.unlink(missing_ok=True)
    return out

def build_non_repeating_candidates(
    brief: str,
    transcript: dict | None,
    contact_sheet: Path | None,
    duration: float,
    target_platform: str,
    clip_count: int,
    desired_minutes: float,
    used_windows: list[list[float]],
    hook_mode: str = "Auto (8s if requested by campaign)",
) -> list[dict]:
    count = max(1, min(2, int(clip_count)))
    prefer_8s = ("8s" in hook_mode) or ("8s hook" in brief.lower() or "8-second hook" in brief.lower())
    clip_seconds = 8.0 if prefer_8s else max(3.0, float(desired_minutes) * 60.0)

    fresh_slots = pick_fresh_windows(
        duration=duration,
        clip_seconds=clip_seconds,
        count=count,
        used_windows=used_windows,
        prefer_8s_hook=prefer_8s,
        transcript=transcript,
    )

    client = get_openai_client()
    ai_meta_list: list[dict] = []
    if client and contact_sheet is not None:
        try:
            segs = (transcript or {}).get("segments", []) or []
            t_lines = [f"[{float(s.get('start',0)):.1f}-{float(s.get('end',0)):.1f}] {s.get('text','')}" for s in segs]
            t_text = "\n".join(t_lines)[:18000] or (transcript or {}).get("text", "")[:18000]
            img_b64 = base64.b64encode(contact_sheet.read_bytes()).decode("utf-8")
            prompt = f"""Given target platform {target_platform}, clip duration ~{clip_seconds:.1f}s ({desired_minutes:.2f} min), and previously used ranges {used_windows},
write short titles and hooks for {count} fresh clip(s). Return ONLY JSON:
{{"items":[{{"title":"short title","reason":"why it fits","campaign_match":"brief match","hook":"short hook banner text","confidence":"high|medium|low"}}]}}
BRIEF: {brief[:8000]}
TRANSCRIPT: {t_text}
"""
            resp = client.chat.completions.create(
                model="gpt-4o-mini", temperature=0.35,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img_b64}}
                ]}]
            )
            ai_meta_list = parse_json_response(resp.choices[0].message.content or "{}").get("items", []) or []
        except Exception:
            ai_meta_list = []

    results = []
    for idx, (s_sec, e_sec, spoken_txt) in enumerate(fresh_slots):
        ai_m = ai_meta_list[idx] if idx < len(ai_meta_list) else {}
        dur_actual = max(0.5, e_sec - s_sec)
        results.append({
            "title": ai_m.get("title") or f"{target_platform} Pick #{len(used_windows) + idx + 1} ({dur_actual/60:.2f}m)",
            "start": s_sec,
            "end": e_sec,
            "target_platform": target_platform,
            "reason": spoken_txt[:150] if spoken_txt else (ai_m.get("reason") or f"Fresh {dur_actual:.1f}s segment starting at {s_sec:.1f}s (no prior overlap)."),
            "campaign_match": ai_m.get("campaign_match") or "Non-repeating segment matched to requested length",
            "hook": ai_m.get("hook") or (spoken_txt[:80] if spoken_txt else "Don't miss this part!"),
            "risk": "",
            "confidence": ai_m.get("confidence") or "high",
        })
    return results

def generate_social_copy(
    platform: str,
    brief: str,
    analysis: dict | None,
    clip_info: dict | None,
    transcript_text: str,
    custom_instructions: str = "",
) -> dict:
    client = get_openai_client()
    tags = (analysis or {}).get("hashtags") or []
    clean_tags = " ".join(t if t.startswith("#") else f"#{t.replace(' ', '')}" for t in tags[:6])
    if not clean_tags:
        p_slug = re.sub(r"[^A-Za-z0-9]", "", platform) or "Social"
        clean_tags = f"#Campaign #{p_slug} #VideoHighlights"

    clip_title = (clip_info or {}).get("title") or "Featured Moment"
    clip_hook = (clip_info or {}).get("hook") or ""
    req_note = custom_instructions.strip()

    if not client:
        base_hook = clip_hook or f"{clip_title} — key moments from our campaign."
        steer_line = f"\n\nNote included: {req_note}" if req_note else ""
        caption = f"{base_hook}{steer_line}\n\n{clean_tags}".strip()
        summary_excerpt = (analysis or {}).get("plain_english_summary") or transcript_text[:280]
        description = (
            f"{clip_title} ({platform})\n\n"
            f"{summary_excerpt}\n\n"
            f"Campaign Focus: {req_note if req_note else 'Aligned with campaign brief & highlights.'}\n\n"
            f"{clean_tags}"
        ).strip()
        return {"caption": caption, "description": description}

    prompt = f"""Create a high-performing social media CAPTION and DESCRIPTION for platform: {platform}.
Clip info: {json.dumps(clip_info or {}, ensure_ascii=False)}
Campaign summary: {json.dumps(analysis or {}, ensure_ascii=False)[:4000]}
Transcript snippet: {transcript_text[:2500]}
User's specific instructions for what to put in the caption/description: {req_note or "None"}

Return ONLY valid JSON:
{{"caption": "punchy platform-ready caption with relevant hashtags", "description": "clear full description with context, key takeaways, call to action, and tags"}}
"""
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.35,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "You write crisp, accurate social captions and descriptions following explicit user instructions."},
            {"role": "user", "content": prompt},
        ]
    )
    parsed = parse_json_response(resp.choices[0].message.content or "{}")
    return {
        "caption": str(parsed.get("caption", "")).strip(),
        "description": str(parsed.get("description", "")).strip(),
    }

def build_lower_third_srt(
    transcript: dict | None,
    start: float,
    end: float,
    fallback_caption_text: str = "",
) -> str:
    def stamp(sec):
        ms = int(round(max(0.0, sec) * 1000))
        h, rem = divmod(ms, 3600000)
        m, rem = divmod(rem, 60000)
        s, ms = divmod(rem, 1000)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"

    clip_dur = max(0.5, end - start)
    rows = []
    idx = 1

    for seg in (transcript or {}).get("segments", []) or []:
        a, b = float(seg.get("start", 0)), float(seg.get("end", 0))
        if b <= start or a >= end:
            continue
        text = re.sub(r"\s+", " ", str(seg.get("text", ""))).strip()
        if not text:
            continue
        rel_a = max(0.0, a - start)
        rel_b = min(clip_dur, b - start)
        if rel_b <= rel_a + 0.15:
            continue
        rows.append(f"{idx}\n{stamp(rel_a)} --> {stamp(rel_b)}\n{text}\n")
        idx += 1

    if not rows:
        fb = re.sub(r"\s+", " ", (fallback_caption_text or "").strip())
        if fb:
            chunks = [fb[i:i+85] for i in range(0, min(len(fb), 340), 85)]
            step = clip_dur / max(1, len(chunks))
            for i, ch in enumerate(chunks):
                rows.append(f"{idx}\n{stamp(i * step)} --> {stamp((i + 1) * step)}\n{ch.strip()}\n")
                idx += 1

    return "\n".join(rows)

def resolve_sharp_dimensions(
    aspect: str,
    resolution: str,
    src_w: int,
    src_h: int,
) -> tuple[str | None, str | None, int, int]:
    aspect_crops = {
        "9:16 Vertical": "crop=ih*9/16:ih",
        "16:9 Landscape": "crop=iw:iw*9/16",
        "1:1 Square": r"crop=min(iw\,ih):min(iw\,ih)",
        "4:5 Portrait": "crop=ih*4/5:ih",
        "Keep original": None,
    }
    crop = aspect_crops.get(aspect)

    if aspect == "9:16 Vertical":
        cw, ch = int(src_h * 9 / 16), src_h
        default_4k = (2160, 3840)
        default_1k = (1080, 1920)
        default_720 = (720, 1280)
    elif aspect == "16:9 Landscape":
        cw, ch = src_w, int(src_w * 9 / 16)
        default_4k = (3840, 2160)
        default_1k = (1920, 1080)
        default_720 = (1280, 720)
    elif aspect == "1:1 Square":
        side = min(src_w, src_h)
        cw, ch = side, side
        default_4k = (2160, 2160)
        default_1k = (1080, 1080)
        default_720 = (720, 720)
    elif aspect == "4:5 Portrait":
        cw, ch = int(src_h * 4 / 5), src_h
        default_4k = (2160, 2700)
        default_1k = (1080, 1350)
        default_720 = (720, 900)
    else:
        cw, ch = src_w, src_h
        default_4k = (max(src_w, 3840), max(src_h, 2160))
        default_1k = (max(src_w, 1920), max(src_h, 1080))
        default_720 = (max(src_w, 1280), max(src_h, 720))

    cw = max(2, cw - (cw % 2))
    ch = max(2, ch - (ch % 2))

    if resolution == "Source or Better (Auto Sharp)":
        if max(cw, ch) >= 1080:
            tw, th = cw, ch
        else:
            tw, th = default_1k
    elif resolution == "4K Ultra Sharp":
        tw, th = default_4k
    elif resolution == "1080p Full HD":
        tw, th = default_1k
    elif resolution == "720p HD":
        tw, th = default_720
    else:
        tw, th = cw, ch

    return crop, f"{tw}:{th}", tw, th

def render_video(
    source: str,
    output: str,
    start: float,
    end: float,
    aspect: str,
    resolution: str,
    crf: int,
    logo: str | None = None,
    srt: str | None = None,
    hook_banner_text: str = "",
    hook_banner_duration: float = 8.0,
    src_w: int = 1920,
    src_h: int = 1080,
    caption_font_size: int = 24,
):
    crop, dims, out_w, out_h = resolve_sharp_dimensions(aspect, resolution, src_w, src_h)
    filters = []
    if crop:
        filters.append(crop)
    if dims:
        filters += [
            f"scale={dims}:force_original_aspect_ratio=decrease:flags=lanczos",
            f"pad={dims}:(ow-iw)/2:(oh-ih)/2:black",
        ]
    filters.append("unsharp=5:5:0.55:3:3:0.25")

    vf = ",".join(filters) if filters else "null"
    args = ["ffmpeg", "-y", "-ss", str(start), "-i", source]
    if logo:
        args += ["-i", logo]

    graph = [f"[0:v]{vf}[v0]"]
    current = "v0"

    clean_hook = escape_drawtext(hook_banner_text)
    if clean_hook:
        hook_font_px = max(22, int(out_h * 0.032))
        box_h = max(46, int(hook_font_px * 1.9))
        top_y = max(24, int(out_h * 0.06))
        clip_span = max(1.0, end - start)
        active_end = min(clip_span, max(1.5, float(hook_banner_duration)))
        draw_hook = (
            f"[{current}]drawbox=x=0:y={top_y - 8}:w=iw:h={box_h}:color=black@0.72:t=fill:"
            f"enable='between(t,0,{active_end:.2f})',"
            f"drawtext=text='{clean_hook}':fontcolor=white:fontsize={hook_font_px}:"
            f"x=(w-text_w)/2:y={top_y + (box_h - hook_font_px)//2 - 8}:"
            f"shadowcolor=black@0.85:shadowx=2:shadowy=2:"
            f"enable='between(t,0,{active_end:.2f})'[vhook]"
        )
        graph.append(draw_hook)
        current = "vhook"

    if logo:
        graph += ["[1:v]scale=iw*0.16:-1:flags=lanczos[logo]", f"[{current}][logo]overlay=W-w-24:24:format=auto[vlogo]"]
        current = "vlogo"

    if srt:
        srt_path = Path(srt).resolve().as_posix().replace(":", "\\:")
        style = (
            f"Alignment=2,FontSize={int(caption_font_size)},PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,BackColour=&H88000000,BorderStyle=3,Outline=2,Shadow=1,MarginV=48"
        )
        graph.append(f"[{current}]subtitles='{srt_path}':force_style='{style}'[vout]")
        current = "vout"

    args += [
        "-filter_complex", ";".join(graph),
        "-map", f"[{current}]",
        "-map", "0:a?",
        "-t", str(max(0.5, end - start)),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        output
    ]
    p = run_cmd(args, timeout=1800)
    if p.returncode:
        raise RuntimeError(p.stderr[-2500:] or "FFmpeg render failed.")

def init_state():
    defaults = {
        "brief": "",
        "brief_analysis": None,
        "campaign_feedback": None,
        "go_ahead_clips": False,
        "target_platform_choice": "YouTube Shorts / Reels",
        "desired_clip_count": 2,
        "desired_clip_minutes": 0.50,
        "used_clip_windows": [],
        "hook_mode_choice": "Auto (8s if requested by campaign)",
        "hook_banner_scope": "8-Second Top Banner (0–8s)",
        "burn_onscreen_captions": True,
        "onscreen_caption_size": 24,
        "source_video": None,
        "video_name": None,
        "video_info": None,
        "transcript": None,
        "highlights": [],
        "clips": [],
        "logo_path": None,
        "logo_name": None,
        "project_name": "My campaign",
        "brand_name": "",
        "brand_colour": "#FFFFFF",
        "caption_instructions": "",
        "social_caption": "",
        "social_description": "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()

# ----------------------------- HEADER & NAV -----------------------------
st.markdown(f"""<div class="hero">
<div style="color:#a5b4fc;font-weight:750;font-size:.76rem;letter-spacing:.13em">CAMPAIGN INTELLIGENCE · CUSTOM MINUTE LENGTHS · NON-REPEATING PICKS · TOP HOOK & LOWER-THIRD CAPTIONS</div>
<h1>🎬 {APP_NAME}</h1>
<p class="muted">Pick desired clip length in minutes + varied start points with zero repeat picks across runs $\rightarrow$ top hook banner + lower-third captions $\rightarrow$ caption/description + fast readout.</p>
<span class="pill">Length in minutes</span>
<span class="pill">No repeat picks</span>
<span class="pill">Top hook + lower-third captions</span>
<span class="pill">Fast voice readout</span>
</div>""", unsafe_allow_html=True)

if not ffmpeg_ready():
    st.error("FFmpeg and ffprobe are required for video processing. Streamlit Cloud installs them from packages.txt; install them locally and add bin to PATH.")

with st.sidebar:
    st.title("STUDIO")
    page = st.radio("Navigation", [
        "Campaign & Clip Workflow",
        "Editor & Templates",
        "Caption & Description Studio",
        "Brand Kit & Settings",
    ], label_visibility="collapsed")
    st.divider()
    st.metric("Go-Ahead Status", "Approved" if st.session_state.go_ahead_clips else "Pending Review")
    st.metric("Used Clip Ranges", len(st.session_state.used_clip_windows))
    st.metric("Generated Clips", len(st.session_state.clips))

# ----------------------------- PAGE 1: CAMPAIGN & CLIP WORKFLOW ---------
if page == "Campaign & Clip Workflow":
    st.subheader("Step 1 · Video + Campaign Requirements & Fit Feedback")
    left_col, right_col = st.columns([1.15, 1], gap="large")

    with left_col:
        st.text_input("Project / Campaign Name", key="project_name")
        video_up = st.file_uploader("Upload campaign video", type=["mp4", "mov", "mkv", "webm", "m4v"], key="wf_video_up")
        if video_up and video_up.size > MAX_UPLOAD_MB * 1024 * 1024:
            st.error(f"File exceeds {MAX_UPLOAD_MB} MB limit.")
        elif video_up and st.button("Load Video", type="primary", key="wf_load_video"):
            try:
                with st.spinner("Inspecting video..."):
                    vpath = save_upload(video_up)
                    vinfo = probe_video(str(vpath))
                    st.session_state.source_video = str(vpath)
                    st.session_state.video_name = video_up.name
                    st.session_state.video_info = vinfo
                    st.session_state.transcript = None
                    st.session_state.highlights = []
                    st.session_state.used_clip_windows = []
                    st.session_state.campaign_feedback = None
                    st.session_state.go_ahead_clips = False
                st.success(f"Loaded {video_up.name} ({fmt_time(vinfo['duration'])})")
            except Exception as exc:
                st.error(f"Video error: {exc}")

        if st.session_state.video_info:
            vi = st.session_state.video_info
            st.video(st.session_state.source_video)
            m1, m2, m3 = st.columns(3)
            m1.metric("Duration", fmt_time(vi["duration"]))
            m2.metric("Resolution", f"{vi['width']}x{vi['height']}")
            m3.metric("Audio", vi["audio_codec"])

    with right_col:
        brief_files = st.file_uploader(
            "Optional brief files (PDF, DOCX, TXT, MD, CSV, images)",
            type=["pdf", "docx", "txt", "md", "csv", "png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True,
            key="wf_brief_files"
        )
        if brief_files and st.button("Extract Brief Files", key="wf_extract_brief_files"):
            extracted = []
            for bf in brief_files:
                try:
                    extracted.append(f"--- {bf.name} ---\n{extract_document_text(bf)}")
                except Exception as exc:
                    st.warning(f"{bf.name}: {exc}")
            if extracted:
                st.session_state.brief = (st.session_state.brief + "\n\n" + "\n\n".join(extracted)).strip()
                st.rerun()

        st.text_area(
            "Paste campaign requirements",
            key="brief",
            height=175,
            placeholder="Example: Post on YouTube and Instagram. Include an 8-second hook, visible caption reading on screen, and CTA to check the link."
        )

        p_col, c_col, min_col = st.columns(3)
        with p_col:
            st.selectbox(
                "Target platform",
                ["YouTube Shorts / Reels", "YouTube Standard", "Instagram Feed / Reels", "TikTok"],
                key="target_platform_choice"
            )
        with c_col:
            st.radio("Clips count", [1, 2], horizontal=True, key="desired_clip_count")
        with min_col:
            st.number_input(
                "Clip length (minutes)",
                min_value=0.10,
                max_value=15.0,
                step=0.25,
                key="desired_clip_minutes"
            )

        h_col, cap_col = st.columns(2)
        with h_col:
            st.selectbox(
                "Hook strategy",
                ["Auto (8s if requested by campaign)", "8-Second Hook (8s)", "Video Hook (Opening / Highlight)"],
                key="hook_mode_choice"
            )
        with cap_col:
            st.checkbox(
                "Visible lower-third captions on video",
                key="burn_onscreen_captions"
            )

        st.selectbox(
            "Hook banner placement timing",
            ["8-Second Top Banner (0–8s)", "Full Clip Top Banner"],
            key="hook_banner_scope"
        )

        if st.button("🧠 Analyse Campaign & Video Fit", type="primary", use_container_width=True, key="wf_analyse_btn"):
            if not st.session_state.brief.strip():
                st.error("Enter campaign requirements first.")
            else:
                try:
                    with st.spinner("Analysing campaign requirements and inspecting video..."):
                        analysis = ai_campaign_analysis(st.session_state.brief, st.session_state.project_name)
                        st.session_state.brief_analysis = analysis

                        if analysis.get("requires_onscreen_captions"):
                            st.session_state.burn_onscreen_captions = True
                        if analysis.get("requires_8s_hook"):
                            st.session_state.hook_mode_choice = "8-Second Hook (8s)"
                            st.session_state.hook_banner_scope = "8-Second Top Banner (0–8s)"
                        elif analysis.get("requires_video_hook"):
                            st.session_state.hook_banner_scope = "Full Clip Top Banner"

                        if st.session_state.source_video and st.session_state.video_info:
                            try:
                                tr = transcribe_video(st.session_state.source_video)
                                st.session_state.transcript = tr
                            except Exception:
                                tr = None
                            sheet = make_contact_sheet(st.session_state.source_video, st.session_state.video_info["duration"], max_frames=9)
                            st.session_state.highlights = build_non_repeating_candidates(
                                brief=json.dumps(analysis, ensure_ascii=False),
                                transcript=tr,
                                contact_sheet=sheet,
                                duration=st.session_state.video_info["duration"],
                                target_platform=st.session_state.target_platform_choice,
                                clip_count=st.session_state.desired_clip_count,
                                desired_minutes=float(st.session_state.desired_clip_minutes),
                                used_windows=st.session_state.used_clip_windows,
                                hook_mode=st.session_state.hook_mode_choice,
                            )
                            st.session_state.campaign_feedback = evaluate_video_against_campaign(
                                analysis, st.session_state.video_info, st.session_state.transcript
                            )
                        else:
                            st.session_state.campaign_feedback = {
                                "ready": False,
                                "verdict": "Campaign Analysed — Upload Video to Check Fit & Extract Clips",
                                "strengths": [],
                                "gaps": ["Upload a campaign video on the left to match moments and extract clips."],
                                "warnings": analysis.get("prohibited_content", []) or [],
                            }
                    st.success("Campaign feedback ready.")
                except Exception as exc:
                    st.error(f"Analysis failed: {exc}")

    # Step 2: Campaign Feedback + Go-Ahead
    if st.session_state.brief_analysis:
        st.divider()
        st.subheader("Step 2 · Campaign Feedback & Go-Ahead")
        an = st.session_state.brief_analysis
        fb = st.session_state.campaign_feedback or {}

        st.info(f"**{an.get('campaign_name', 'Campaign')}:** {an.get('plain_english_summary', '')}")
        if fb.get("verdict"):
            st.success(f"**Fit Verdict:** {fb['verdict']}")

        f1, f2, f3 = st.columns(3)
        with f1:
            st.markdown("**What the Campaign Wants**")
            for item in (an.get("must_include") or an.get("deliverables") or [])[:5]:
                st.markdown(f"- {item}")
            for s_item in (fb.get("strengths") or [])[:4]:
                st.caption(f"✓ {s_item}")
        with f2:
            st.markdown("**Gaps / Things to Check**")
            for g_item in (fb.get("gaps") or an.get("unclear_or_conflicting_points") or [])[:5]:
                st.markdown(f"- {g_item}")
        with f3:
            st.markdown("**Restrictions & Checklist**")
            for w_item in (an.get("prohibited_content") or an.get("pre_publish_checklist") or [])[:5]:
                st.markdown(f"- {w_item}")

        go_col1, go_col2 = st.columns([1.2, 2])
        with go_col1:
            if st.button(
                "✅ Give Go-Ahead to Extract Clips",
                type="primary",
                disabled=not bool(st.session_state.video_info),
                use_container_width=True,
                key="wf_go_ahead_btn"
            ):
                if st.session_state.video_info:
                    st.session_state.highlights = build_non_repeating_candidates(
                        brief=st.session_state.brief,
                        transcript=st.session_state.transcript,
                        contact_sheet=None,
                        duration=st.session_state.video_info["duration"],
                        target_platform=st.session_state.target_platform_choice,
                        clip_count=st.session_state.desired_clip_count,
                        desired_minutes=float(st.session_state.desired_clip_minutes),
                        used_windows=st.session_state.used_clip_windows,
                        hook_mode=st.session_state.hook_mode_choice,
                    )
                st.session_state.go_ahead_clips = True
                st.rerun()

        with go_col2:
            if st.session_state.go_ahead_clips:
                st.success(f"Go-ahead granted! Target clip length: {float(st.session_state.desired_clip_minutes):.2f} min ({float(st.session_state.desired_clip_minutes)*60:.0f}s).")

    # Step 3: 1-2 Non-Repeating Clip Extraction
    if st.session_state.go_ahead_clips and st.session_state.video_info and ffmpeg_ready():
        st.divider()
        st.subheader(f"Step 3 · Extract 1–2 Fresh Clips ({st.session_state.target_platform_choice})")
        vi = st.session_state.video_info
        dur = float(vi["duration"])

        ctrl1, ctrl2, ctrl3 = st.columns([1.2, 1.2, 1])
        with ctrl1:
            st.number_input(
                "Desired clip length (minutes)",
                min_value=0.10,
                max_value=15.0,
                step=0.25,
                key="desired_clip_minutes"
            )
        active_minutes = float(st.session_state.desired_clip_minutes)

        with ctrl2:
            if st.button("🔄 Pick Fresh Non-Repeating Spots", use_container_width=True, key="pick_fresh_spots_btn"):
                st.session_state.highlights = build_non_repeating_candidates(
                    brief=st.session_state.brief,
                    transcript=st.session_state.transcript,
                    contact_sheet=None,
                    duration=dur,
                    target_platform=st.session_state.target_platform_choice,
                    clip_count=st.session_state.desired_clip_count,
                    desired_minutes=active_minutes,
                    used_windows=st.session_state.used_clip_windows,
                    hook_mode=st.session_state.hook_mode_choice,
                )
                st.rerun()
        with ctrl3:
            if st.button("🧹 Reset Used Ranges", use_container_width=True, key="reset_used_ranges_btn"):
                st.session_state.used_clip_windows = []
                st.session_state.highlights = build_non_repeating_candidates(
                    brief=st.session_state.brief,
                    transcript=st.session_state.transcript,
                    contact_sheet=None,
                    duration=dur,
                    target_platform=st.session_state.target_platform_choice,
                    clip_count=st.session_state.desired_clip_count,
                    desired_minutes=active_minutes,
                    used_windows=[],
                    hook_mode=st.session_state.hook_mode_choice,
                )
                st.rerun()

        if st.session_state.used_clip_windows:
            used_str = ", ".join(f"{w[0]:.1f}s–{w[1]:.1f}s" for w in st.session_state.used_clip_windows)
            st.caption(f"Already used ranges avoided automatically: {used_str}")

        cands = st.session_state.highlights or build_non_repeating_candidates(
            brief=st.session_state.brief,
            transcript=st.session_state.transcript,
            contact_sheet=None,
            duration=dur,
            target_platform=st.session_state.target_platform_choice,
            clip_count=st.session_state.desired_clip_count,
            desired_minutes=active_minutes,
            used_windows=st.session_state.used_clip_windows,
            hook_mode=st.session_state.hook_mode_choice,
        )
        cands = cands[:int(st.session_state.desired_clip_count)]

        q_col1, q_col2, q_col3 = st.columns(3)
        with q_col1:
            sharp_res = st.selectbox(
                "Sharpness / Resolution",
                ["Source or Better (Auto Sharp)", "4K Ultra Sharp", "1080p Full HD", "720p HD"],
                key="wf_sharp_res"
            )
        with q_col2:
            sharp_crf = st.slider("Visual detail CRF (16 = razor sharp)", 16, 24, 17, key="wf_sharp_crf")
        with q_col3:
            cap_size = st.slider("Lower-third caption size", 18, 40, int(st.session_state.onscreen_caption_size), key="wf_cap_size")

        chosen_windows = []
        run_tag = len(st.session_state.used_clip_windows)
        for idx, cand in enumerate(cands):
            with st.container(border=True):
                st.markdown(f"**Clip {idx + 1}: {cand.get('title', f'Highlight {idx+1}')}**")
                st.caption(f"AI Reason: {cand.get('reason', '')}")
                wc1, wc2, wc3 = st.columns([1, 1, 1.4])
                with wc1:
                    s_val = st.number_input(
                        f"Start (sec) — Clip {idx+1}",
                        0.0,
                        max(0.5, dur - 0.5),
                        float(cand["start"]),
                        step=0.5,
                        key=f"c_start_{run_tag}_{idx}"
                    )
                with wc2:
                    calc_end = min(dur, float(s_val) + active_minutes * 60.0)
                    e_val = st.number_input(
                        f"End (sec) — Clip {idx+1}",
                        0.5,
                        max(0.5, dur),
                        max(float(s_val) + 1.0, float(calc_end)),
                        step=0.5,
                        key=f"c_end_{run_tag}_{idx}"
                    )
                with wc3:
                    asp_val = st.selectbox(
                        f"Aspect — Clip {idx+1}",
                        ["9:16 Vertical", "16:9 Landscape", "1:1 Square", "4:5 Portrait", "Keep original"],
                        index=0 if ("Shorts" in st.session_state.target_platform_choice or "Reels" in st.session_state.target_platform_choice or "TikTok" in st.session_state.target_platform_choice) else 1,
                        key=f"c_asp_{run_tag}_{idx}"
                    )
                hook_txt = st.text_input(
                    f"Top hook banner text — Clip {idx+1}",
                    value=str(cand.get("hook") or ""),
                    key=f"c_hook_txt_{run_tag}_{idx}"
                )
                chosen_windows.append((idx + 1, cand.get("title", f"Clip_{idx+1}"), float(s_val), float(e_val), asp_val, hook_txt, cand))

        if st.button("✂️ Extract & Render Sharp Clips Now", type="primary", use_container_width=True, key="wf_extract_clips_btn"):
            rendered_now = []
            banner_dur = 8.0 if st.session_state.hook_banner_scope.startswith("8-Second") else 9999.0
            for num, title, s_v, e_v, asp_v, h_txt, cand_meta in chosen_windows:
                if e_v <= s_v:
                    st.warning(f"Clip {num}: End time must be greater than start time.")
                    continue
                out_file = WORK_DIR / f"{safe_name(title)}_clip{num}_{len(st.session_state.clips)+1}.mp4"
                srt_file = None
                if st.session_state.burn_onscreen_captions:
                    srt_txt = build_lower_third_srt(
                        transcript=st.session_state.transcript,
                        start=s_v,
                        end=e_v,
                        fallback_caption_text=st.session_state.caption_instructions or st.session_state.brief,
                    )
                    if srt_txt.strip():
                        srt_file = WORK_DIR / f"{safe_name(title)}_clip{num}.srt"
                        srt_file.write_text(srt_txt, encoding="utf-8")

                with st.spinner(f"Rendering sharp Clip {num} ({s_v:.1f}s – {e_v:.1f}s)..."):
                    render_video(
                        st.session_state.source_video,
                        str(out_file),
                        s_v,
                        e_v,
                        asp_v,
                        sharp_res,
                        sharp_crf,
                        logo=st.session_state.logo_path,
                        srt=str(srt_file) if srt_file else None,
                        hook_banner_text=h_txt,
                        hook_banner_duration=banner_dur,
                        src_w=int(vi.get("width") or 1920),
                        src_h=int(vi.get("height") or 1080),
                        caption_font_size=int(cap_size),
                    )
                st.session_state.used_clip_windows.append([round(s_v, 1), round(e_v, 1)])
                rec = {
                    "name": f"{title} ({st.session_state.target_platform_choice})",
                    "path": str(out_file),
                    "start": s_v,
                    "end": e_v,
                    "aspect": asp_v,
                    "resolution": sharp_res,
                    "meta": {**cand_meta, "hook": h_txt or cand_meta.get("hook", "")},
                }
                st.session_state.clips.append(rec)
                rendered_now.append(rec)

            if rendered_now:
                st.session_state.highlights = build_non_repeating_candidates(
                    brief=st.session_state.brief,
                    transcript=st.session_state.transcript,
                    contact_sheet=None,
                    duration=dur,
                    target_platform=st.session_state.target_platform_choice,
                    clip_count=st.session_state.desired_clip_count,
                    desired_minutes=active_minutes,
                    used_windows=st.session_state.used_clip_windows,
                    hook_mode=st.session_state.hook_mode_choice,
                )
                copy_pack = generate_social_copy(
                    platform=st.session_state.target_platform_choice,
                    brief=st.session_state.brief,
                    analysis=st.session_state.brief_analysis,
                    clip_info=rendered_now[0].get("meta"),
                    transcript_text=((st.session_state.transcript or {}).get("text") or ""),
                    custom_instructions=st.session_state.caption_instructions,
                )
                st.session_state.social_caption = copy_pack["caption"]
                st.session_state.social_description = copy_pack["description"]
                st.success(f"Extracted {len(rendered_now)} sharp clip(s) and queued fresh non-repeating picks for your next run.")

    if st.session_state.clips:
        st.divider()
        st.subheader("Extracted Clips")
        for idx, cl in enumerate(st.session_state.clips):
            with st.container(border=True):
                c_vid, c_dl = st.columns([2.2, 1])
                with c_vid:
                    st.markdown(f"**{idx + 1}. {cl['name']}**")
                    st.caption(f"{fmt_time(cl['start'])} – {fmt_time(cl['end'])} · {cl['aspect']} · {cl['resolution']}")
                    if Path(cl["path"]).exists():
                        st.video(cl["path"])
                with c_dl:
                    if Path(cl["path"]).exists():
                        with open(cl["path"], "rb") as fh:
                            st.download_button(
                                f"Download Clip {idx+1} (.MP4)",
                                fh,
                                file_name=Path(cl["path"]).name,
                                mime="video/mp4",
                                key=f"wf_dl_clip_{idx}",
                                use_container_width=True
                            )

# ----------------------------- PAGE 2: EDITOR & TEMPLATES -------------
elif page == "Editor & Templates":
    st.subheader("Manual & Sharp Clip Editor")
    if not st.session_state.video_info:
        st.info("Upload and load a video in 'Campaign & Clip Workflow' first.")
    else:
        vi = st.session_state.video_info
        st.video(st.session_state.source_video)
        a, b = st.columns(2)
        with a:
            start = st.number_input("Start (seconds)", 0.0, float(max(vi["duration"], 0.1)), 0.0, step=0.5)
            end = st.number_input("End (seconds)", 0.1, float(max(vi["duration"], 0.1)), min(30.0, float(max(vi["duration"], 0.1))), step=0.5)
            clip_name = st.text_input("Clip name", value=safe_name(Path(st.session_state.video_name or "clip").stem + "_edit"))
            hook_line = st.text_input("Top hook banner text", value="")
            hook_dur_opt = st.selectbox("Top hook duration", ["8s Banner", "Full Clip Banner"])
        with b:
            aspect = st.selectbox("Aspect ratio", ["9:16 Vertical", "16:9 Landscape", "1:1 Square", "4:5 Portrait", "Keep original"])
            resolution = st.selectbox("Sharpness / Resolution", ["Source or Better (Auto Sharp)", "4K Ultra Sharp", "1080p Full HD", "720p HD"])
            crf = st.slider("Visual detail CRF (lower = sharper)", 16, 24, 17)
            add_logo = st.checkbox("Include saved brand logo", value=bool(st.session_state.logo_path))
            add_sub = st.checkbox("Burn lower-third captions", value=True)

        if st.button("Render Sharp Clip", type="primary", use_container_width=True):
            if end <= start:
                st.error("End time must be greater than start time.")
            elif ffmpeg_ready():
                out = WORK_DIR / f"{safe_name(clip_name)}_{len(st.session_state.clips)+1}.mp4"
                srt_path = None
                if add_sub:
                    s_txt = build_lower_third_srt(
                        transcript=st.session_state.transcript,
                        start=start,
                        end=end,
                        fallback_caption_text=st.session_state.caption_instructions or st.session_state.brief,
                    )
                    if s_txt.strip():
                        srt_path = WORK_DIR / f"{safe_name(clip_name)}.srt"
                        srt_path.write_text(s_txt, encoding="utf-8")
                with st.spinner("Rendering sharp clip..."):
                    render_video(
                        st.session_state.source_video, str(out), start, end,
                        aspect, resolution, crf,
                        logo=st.session_state.logo_path if add_logo else None,
                        srt=str(srt_path) if srt_path else None,
                        hook_banner_text=hook_line,
                        hook_banner_duration=8.0 if hook_dur_opt.startswith("8s") else 9999.0,
                        src_w=int(vi.get("width") or 1920),
                        src_h=int(vi.get("height") or 1080),
                    )
                st.session_state.used_clip_windows.append([round(start, 1), round(end, 1)])
                st.session_state.clips.append({
                    "name": clip_name, "path": str(out), "start": start,
                    "end": end, "aspect": aspect, "resolution": resolution
                })
                st.success("Sharp clip rendered.")
                st.video(str(out))

# ----------------------------- PAGE 3: CAPTION & DESCRIPTION STUDIO -----
elif page == "Caption & Description Studio":
    st.subheader("Social Media Caption & Description Studio + Fast Readout")
    st.write("Tell ClipStream what you want in the caption/description, generate or edit it for any social platform, and use Fast Readout to hear it immediately.")

    cp1, cp2 = st.columns([1.2, 1])
    with cp1:
        copy_platform = st.selectbox(
            "Social platform",
            ["YouTube", "Instagram", "TikTok", "Facebook / LinkedIn"],
            key="copy_platform_select"
        )
    with cp2:
        clip_opts = ["General Campaign Video"] + [f"{i+1}. {c['name']}" for i, c in enumerate(st.session_state.clips)]
        chosen_clip_label = st.selectbox("Reference clip", clip_opts, key="copy_clip_ref")

    st.text_area(
        "What do you want in the caption & description?",
        key="caption_instructions",
        height=95,
        placeholder="Example: Highlight the clip takeaway, mention lower-third captions, include a clear CTA, and add campaign hashtags."
    )

    if st.button("✨ Generate / Update Caption & Description", type="primary", use_container_width=True, key="gen_copy_btn"):
        ref_clip = None
        if chosen_clip_label != "General Campaign Video":
            idx = clip_opts.index(chosen_clip_label) - 1
            if 0 <= idx < len(st.session_state.clips):
                ref_clip = st.session_state.clips[idx].get("meta") or {"title": st.session_state.clips[idx]["name"]}
        with st.spinner("Drafting caption and description..."):
            pack = generate_social_copy(
                platform=copy_platform,
                brief=st.session_state.brief,
                analysis=st.session_state.brief_analysis,
                clip_info=ref_clip,
                transcript_text=((st.session_state.transcript or {}).get("text") or ""),
                custom_instructions=st.session_state.caption_instructions,
            )
            st.session_state.social_caption = pack["caption"]
            st.session_state.social_description = pack["description"]
        st.success("Caption and description updated.")

    st.text_area("Caption (for social post)", key="social_caption", height=130)
    render_fast_voice_reader(st.session_state.social_caption, label="⚡ Fast Read Caption")

    st.text_area("Description (full details, links & tags)", key="social_description", height=190)
    render_fast_voice_reader(st.session_state.social_description, label="⚡ Fast Read Description")

    combined_copy = f"PLATFORM: {copy_platform}\n\nCAPTION:\n{st.session_state.social_caption}\n\nDESCRIPTION:\n{st.session_state.social_description}".strip()
    render_fast_voice_reader(combined_copy, label="🔊 Fast Read Both (Caption + Description)")
    st.download_button(
        "Download Caption & Description (.TXT)",
        data=combined_copy,
        file_name=f"clipstream_{safe_name(copy_platform)}_copy.txt",
        mime="text/plain",
        use_container_width=True
    )

# ----------------------------- PAGE 4: BRAND KIT & SETTINGS ------------
elif page == "Brand Kit & Settings":
    st.subheader("Brand Kit & AI Settings")
    logo = st.file_uploader("Upload brand logo (PNG/JPG/WEBP)", type=["png", "jpg", "jpeg", "webp"], key="bk_logo_up")
    if logo and st.button("Save Logo", key="bk_save_logo"):
        st.session_state.logo_path = str(save_upload(logo))
        st.session_state.logo_name = logo.name
        st.success("Brand logo saved for this session.")
    if st.session_state.logo_path and Path(st.session_state.logo_path).exists():
        st.image(st.session_state.logo_path, caption=st.session_state.logo_name or "Brand Logo", width=200)

    st.text_input("Brand Name", key="brand_name")
    st.text_input("Brand Accent Colour", key="brand_colour")
    st.divider()
    if get_openai_client():
        st.success("OPENAI_API_KEY active — full AI campaign + video analysis & copy generation enabled.")
    else:
        st.info("No OPENAI_API_KEY detected — smart local fallback mode is active for campaign triage, non-repeating picks, top hook banners, lower-third captions, and captions/descriptions.")