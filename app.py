from __future__ import annotations

import base64
import html
import io
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

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
    return str(timedelta(seconds=max(0, int(round(seconds)))))

def minutes_to_seconds(minutes: float) -> float:
    return round(max(1.0, float(minutes) * 60.0), 1)

def default_aspect_for_platform(platform: str) -> str:
    p = (platform or "").lower()
    if any(k in p for k in ["shorts", "reels", "tiktok", "vertical"]):
        return "9:16 Vertical"
    return "16:9 Landscape"

def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")
    return (cleaned or "clip")[:55]

def escape_drawtext(value: str) -> str:
    txt = re.sub(r"\s+", " ", (value or "").strip())
    txt = txt.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:").replace("%", "\\%")
    return txt[:130]

def find_system_font() -> str | None:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return Path(p).resolve().as_posix().replace(":", "\\:")
    return None

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

def parse_json_response(content: str) -> dict:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("AI response did not contain valid JSON.")
    return json.loads(text[start:end+1])

def render_fast_voice_reader(text: str, label: str = "🔊 Fast Readout (Zero Data)", uid: str = "tts"):
    clean = (text or "").strip()
    if not clean:
        st.caption("Nothing to read yet.")
        return
    payload = json.dumps(clean[:3500], ensure_ascii=False)
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    safe_label = html.escape(label)
    safe_uid = re.sub(r"[^a-zA-Z0-9_]", "", uid) or "tts"
    components.html(
        f"""
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-family:system-ui,-apple-system,sans-serif;">
            <button id="speak_{safe_uid}" style="background:#4f46e5;color:#fff;border:none;border-radius:8px;padding:7px 13px;font-weight:600;cursor:pointer;font-size:13px;">
                {safe_label}
            </button>
            <button id="stop_{safe_uid}" style="background:#1e293b;color:#f8fafc;border:1px solid #334155;border-radius:8px;padding:7px 11px;font-weight:600;cursor:pointer;font-size:13px;">
                ⏹ Stop
            </button>
            <span id="status_{safe_uid}" style="font-size:12px;color:#94a3b8;"></span>
        </div>
        <script>
        const txt_{safe_uid} = {payload};
        const stEl_{safe_uid} = document.getElementById('status_{safe_uid}');
        document.getElementById('speak_{safe_uid}').onclick = () => {{
            if (!('speechSynthesis' in window)) {{
                stEl_{safe_uid}.textContent = 'Speech synthesis unavailable in browser.';
                return;
            }}
            window.speechSynthesis.cancel();
            const u = new SpeechSynthesisUtterance(txt_{safe_uid});
            u.rate = 1.15;
            u.onstart = () => stEl_{safe_uid}.textContent = 'Reading fast...';
            u.onend = () => stEl_{safe_uid}.textContent = 'Done.';
            window.speechSynthesis.speak(u);
        }};
        document.getElementById('stop_{safe_uid}').onclick = () => {{
            if ('speechSynthesis' in window) window.speechSynthesis.cancel();
            stEl_{safe_uid}.textContent = 'Stopped.';
        }};
        </script>
        """,
        height=44,
    )

# ----------------------------- NON-REPEATING WINDOW PICKER --------------
def overlaps_used(start: float, end: float, used_windows: list[list[float]], min_gap: float = 2.0) -> bool:
    for u_start, u_end in used_windows:
        if start < (u_end + min_gap) and end > (u_start - min_gap):
            return True
    return False

def transcript_slice(transcript: dict | None, start: float, end: float) -> str:
    segs = (transcript or {}).get("segments") or []
    parts = [
        str(sg.get("text", "")).strip()
        for sg in segs
        if float(sg.get("end", 0)) >= start and float(sg.get("start", 0)) <= end
    ]
    joined = " ".join(p for p in parts if p).strip()
    if joined:
        return joined
    full_txt = ((transcript or {}).get("text") or "").strip()
    return full_txt[:350]

def pick_fresh_windows(
    duration: float,
    desired_minutes: float,
    count: int,
    used_windows: list[list[float]],
    transcript: dict | None = None,
) -> list[tuple[float, float, str]]:
    """Pick 1-2 windows matching the exact requested minute length without repeating prior ranges."""
    if duration <= 0:
        return []
    count = max(1, min(2, int(count)))
    exact_len = round(min(duration, minutes_to_seconds(desired_minutes)), 1)

    max_start = round(max(0.0, duration - exact_len), 1)
    if max_start <= 0.0:
        anchors = [0.0]
    else:
        fractions = [0.08, 0.32, 0.56, 0.80, 0.20, 0.68, 0.0, 0.92]
        random.shuffle(fractions)
        anchors = [round(max(0.0, min(max_start, f * max_start)), 1) for f in fractions]
        for _ in range(8):
            anchors.append(round(random.uniform(0.0, max_start), 1))

    picked: list[tuple[float, float, str]] = []
    combined_used = list(used_windows)

    for anc in anchors:
        if len(picked) >= count:
            break
        s_val = round(max(0.0, min(max_start, anc)), 1)
        e_val = round(min(duration, s_val + exact_len), 1)
        if e_val <= s_val:
            continue
        if not overlaps_used(s_val, e_val, combined_used, min_gap=2.0):
            picked.append((s_val, e_val, transcript_slice(transcript, s_val, e_val)))
            combined_used.append([s_val, e_val])

    if len(picked) < count:
        for anc in anchors:
            if len(picked) >= count:
                break
            s_val = round(max(0.0, min(max_start, anc)), 1)
            e_val = round(min(duration, s_val + exact_len), 1)
            if e_val <= s_val:
                continue
            if any(abs(s_val - ps) < 1.5 and abs(e_val - pe) < 1.5 for ps, pe, _ in picked):
                continue
            picked.append((s_val, e_val, transcript_slice(transcript, s_val, e_val)))

    return picked[:count]

# ----------------------------- CAMPAIGN & CLIP INTELLIGENCE -------------
def heuristic_campaign_analysis(brief: str, project_name: str) -> dict:
    low = brief.lower()
    platforms = []
    for p_label, keys in [
        ("YouTube Shorts", ["youtube", "yt", "shorts"]),
        ("Instagram Reels", ["instagram", "ig", "reels"]),
        ("TikTok", ["tiktok"]),
        ("Facebook", ["facebook", "fb"]),
    ]:
        if any(k in low for k in keys):
            platforms.append(p_label)
    if not platforms:
        platforms = ["YouTube Shorts", "Instagram Reels"]

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
    caption_reqs = [ln for ln in lines if any(w in ln.lower() for w in ["caption", "hashtag", "tag", "description", "link", "mention"])][:6]

    return {
        "campaign_name": project_name.strip() or "Campaign Brief",
        "plain_english_summary": (
            f"Campaign targeting {', '.join(platforms)} based on {len(lines)} brief line(s). "
            f"On-screen captions: {'Required' if wants_onscreen_captions else 'Enabled'}. "
            f"Hook rule: {'8-second hook' if wants_8s_hook else ('Video hook' if wants_video_hook else 'Standard opening')}."
        ),
        "deliverables": ["1 to 2 vertical/platform clips matching selected minute length"],
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
        "unclear_or_conflicting_points": [] if len(brief.strip()) >= 30 else ["Brief is short—add more details on hooks, captions, or rules for sharper campaign alignment."],
        "pre_publish_checklist": [
            "Confirm vertical 9:16 framing for Shorts/Reels/TikTok",
            "Verify bottom captions are crisp and legible without blocking main subject",
            "Confirm caption & description include required campaign elements/tags",
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
    parsed.setdefault("caption_requirements", base_heur["caption_requirements"])
    parsed.setdefault("hashtags", base_heur["hashtags"])
    return parsed

def evaluate_video_against_campaign(analysis: dict, video_info: dict, transcript: dict | None) -> dict:
    strengths = []
    gaps = []
    warnings = list(analysis.get("prohibited_content", []) or [])

    dur = float(video_info.get("duration", 0) or 0)
    w = int(video_info.get("width", 0) or 0)
    h = int(video_info.get("height", 0) or 0)
    if dur > 0:
        strengths.append(f"Video duration is {dur:.1f}s ({fmt_time(dur)}) at {w}x{h}.")
    if dur < 8:
        gaps.append("Source video is under 8 seconds; hook banner timing will clamp to available clip length.")
    if analysis.get("caption_requirements") or analysis.get("hashtags"):
        strengths.append("Campaign caption/tag requirements detected—each short's caption generator will incorporate them.")
    if analysis.get("requires_8s_hook"):
        strengths.append("8-second hook banner rule detected.")
    elif analysis.get("requires_video_hook"):
        strengths.append("Video hook banner framing ready.")

    must_items = analysis.get("must_include", []) or []
    req_scenes = analysis.get("required_scenes_or_moments", []) or []
    t_text = ((transcript or {}).get("text") or "").lower()

    if transcript and t_text.strip():
        strengths.append(f"Speech transcript captured ({len(t_text.split())} words) for per-short captions & subtitles.")
        for req in (must_items + req_scenes)[:5]:
            key_words = [kw for kw in re.findall(r"[a-z]{4,}", req.lower()) if kw not in {"with", "from", "that", "this", "your", "show", "must", "include", "hook", "caption"}]
            if key_words and any(kw in t_text for kw in key_words[:4]):
                strengths.append(f"Transcript shows alignment with: {req}")
            else:
                gaps.append(f"Verify spoken/visual coverage for requirement: {req}")
    else:
        gaps.append("No speech transcript yet—you can still extract exact minute-length shorts and generate unique campaign-aligned captions per short.")

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

def make_contact_sheet(video_path: str, duration: float, max_frames: int = 9) -> Path:
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
) -> list[dict]:
    count = max(1, min(2, int(clip_count)))
    fresh_slots = pick_fresh_windows(
        duration=duration,
        desired_minutes=desired_minutes,
        count=count,
        used_windows=used_windows,
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
            prompt = f"""Given target platform {target_platform}, exact requested clip duration {desired_minutes:.2f} min ({minutes_to_seconds(desired_minutes):.1f}s), and previously used ranges {used_windows},
write distinct short titles and hooks for {count} fresh clip(s). Return ONLY JSON:
{{"items":[{{"title":"short title","reason":"why it fits","campaign_match":"brief match","hook":"short hook banner text","confidence":"high|medium|low"}}]}}
BRIEF: {brief[:8000]}
TRANSCRIPT: {t_text}
"""
            resp = client.chat.completions.create(
                model="gpt-4o-mini", temperature=0.45,
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
        dur_actual = round(max(0.5, e_sec - s_sec), 1)
        pick_num = len(used_windows) + idx + 1
        results.append({
            "title": ai_m.get("title") or f"{target_platform} Short #{pick_num} ({s_sec:.0f}s–{e_sec:.0f}s)",
            "start": s_sec,
            "end": e_sec,
            "target_platform": target_platform,
            "reason": spoken_txt[:150] if spoken_txt else (ai_m.get("reason") or f"Exact {desired_minutes:.2f} min ({dur_actual:.1f}s) segment at {s_sec:.1f}s–{e_sec:.1f}s."),
            "campaign_match": ai_m.get("campaign_match") or f"Exact {desired_minutes:.2f}m non-repeating segment",
            "hook": ai_m.get("hook") or (spoken_txt[:68] if spoken_txt else f"Highlight #{pick_num} — watch closely"),
            "risk": "",
            "confidence": ai_m.get("confidence") or "high",
        })
    return results

def build_campaign_caption_context(analysis: dict | None, brief: str) -> str:
    if not analysis:
        return brief.strip()[:2000]
    parts = []
    if analysis.get("plain_english_summary"):
        parts.append(f"Summary: {analysis['plain_english_summary']}")
    for key, label in [
        ("caption_requirements", "Caption/Description Requirements"),
        ("must_include", "Must Include"),
        ("hashtags", "Campaign Hashtags"),
        ("links_and_submission_steps", "Links / CTA / Submission"),
        ("prohibited_content", "Avoid / Prohibited"),
    ]:
        vals = analysis.get(key) or []
        if vals:
            parts.append(f"{label}: " + "; ".join(str(v) for v in vals[:8]))
    if brief.strip():
        parts.append(f"Raw Brief Notes: {brief.strip()[:1500]}")
    return "\n".join(parts)

def generate_social_copy(
    platform: str,
    brief: str,
    analysis: dict | None,
    clip_info: dict | None,
    transcript_text: str,
    custom_instructions: str = "",
    clip_index: int = 0,
) -> dict:
    client = get_openai_client()
    tags = (analysis or {}).get("hashtags") or []
    clean_tags = " ".join(t if t.startswith("#") else f"#{t.replace(' ', '')}" for t in tags[:6])
    if not clean_tags:
        p_slug = re.sub(r"[^A-Za-z0-9]", "", platform) or "Shorts"
        clean_tags = f"#Campaign #{p_slug} #Short{clip_index + 1}"

    clip_title = (clip_info or {}).get("title") or f"Short #{clip_index + 1}"
    clip_hook = (clip_info or {}).get("hook") or ""
    clip_start = (clip_info or {}).get("start")
    clip_end = (clip_info or {}).get("end")
    window_label = f" ({float(clip_start):.0f}s–{float(clip_end):.0f}s)" if clip_start is not None and clip_end is not None else ""
    req_note = custom_instructions.strip()
    camp_ctx = build_campaign_caption_context(analysis, brief)
    camp_reqs_list = (analysis or {}).get("caption_requirements") or (analysis or {}).get("must_include") or []
    camp_req_line = f"Campaign requirements: {'; '.join(str(x) for x in camp_reqs_list[:4])}." if camp_reqs_list else ""

    if not client:
        angle_lines = [
            f"{clip_hook or clip_title}{window_label}",
            f"What's in this short: {transcript_text[:140].strip()}" if transcript_text.strip() else f"Focused moment #{clip_index + 1} tailored for {platform}.",
            camp_req_line,
            f"Custom note: {req_note}" if req_note else "",
            clean_tags,
        ]
        caption = "\n".join(x for x in angle_lines if x).strip()
        description = (
            f"{clip_title}{window_label} — {platform}\n\n"
            f"{camp_ctx[:900]}\n\n"
            f"Clip Moment Notes: {transcript_text[:300].strip() if transcript_text.strip() else clip_hook}\n"
            f"{'Extra Instructions: ' + req_note + chr(10) if req_note else ''}\n"
            f"{clean_tags}"
        ).strip()
        return {"caption": caption, "description": description}

    prompt = f"""You are writing a UNIQUE social media CAPTION and DESCRIPTION specifically for ONE short clip (Short #{clip_index + 1}) on {platform}.
Make sure this short's caption is distinct from other shorts in the same batch, reflects this short's exact moment/transcript/hook, and satisfies every relevant campaign brief caption/content requirement.

THIS SHORT DETAILS:
{json.dumps(clip_info or {}, ensure_ascii=False)}
THIS SHORT'S TRANSCRIPT SEGMENT:
{transcript_text[:1800]}

CAMPAIGN BRIEF & CAPTION REQUIREMENTS:
{camp_ctx[:3500]}

EXTRA USER INSTRUCTIONS:
{req_note or "None"}

Return ONLY valid JSON:
{{"caption": "unique platform-ready caption for THIS short incorporating campaign caption requirements and hashtags", "description": "clear full description tailored to THIS short and campaign brief requirements"}}
"""
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.55,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "You write distinct, campaign-compliant social captions and descriptions for individual short video clips."},
            {"role": "user", "content": prompt},
        ]
    )
    parsed = parse_json_response(resp.choices[0].message.content or "{}")
    return {
        "caption": str(parsed.get("caption", "")).strip(),
        "description": str(parsed.get("description", "")).strip(),
    }

def wrap_caption_lines(text: str, max_chars: int = 34, max_lines: int = 2) -> str:
    words = re.sub(r"\s+", " ", (text or "").strip()).split()
    if not words:
        return ""
    lines: list[str] = []
    cur = ""
    for w in words:
        candidate = f"{cur} {w}".strip() if cur else w
        if len(candidate) <= max_chars:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            cur = w[:max_chars]
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return "\n".join(lines[:max_lines])

def build_caption_cues(
    transcript: dict | None,
    start: float,
    end: float,
    fallback_text: str = "",
) -> list[tuple[float, float, str]]:
    """Build clean timed lower-third caption cues relative to [start, end]."""
    clip_dur = max(0.5, round(end - start, 2))
    cues: list[tuple[float, float, str]] = []

    for seg in (transcript or {}).get("segments", []) or []:
        a, b = float(seg.get("start", 0)), float(seg.get("end", 0))
        if b <= start or a >= end:
            continue
        wrapped = wrap_caption_lines(str(seg.get("text", "")), max_chars=34, max_lines=2)
        if not wrapped:
            continue
        rel_a = round(max(0.0, a - start), 2)
        rel_b = round(min(clip_dur, b - start), 2)
        if rel_b > rel_a + 0.15:
            cues.append((rel_a, rel_b, wrapped))

    if not cues:
        fb = re.sub(r"\s+", " ", (fallback_text or "").strip())
        if fb:
            words = fb.split()
            chunks: list[str] = []
            buf: list[str] = []
            for w in words:
                buf.append(w)
                if len(" ".join(buf)) >= 56:
                    chunks.append(wrap_caption_lines(" ".join(buf), max_chars=34, max_lines=2))
                    buf = []
            if buf:
                chunks.append(wrap_caption_lines(" ".join(buf), max_chars=34, max_lines=2))
            chunks = [c for c in chunks if c][:5]
            step = clip_dur / max(1, len(chunks))
            for i, ch in enumerate(chunks):
                cues.append((round(i * step, 2), round(min(clip_dur, (i + 1) * step), 2), ch))

    return cues[:24]

def resolve_sharp_dimensions(
    aspect: str,
    resolution: str,
    src_w: int,
    src_h: int,
) -> tuple[str | None, str | None, int, int]:
    src_w = max(2, int(src_w or 1920))
    src_h = max(2, int(src_h or 1080))

    if aspect == "9:16 Vertical":
        # Crop vertical slice if source is wide, or if source is already portrait/square, crop/scale cleanly to 9:16
        if src_w / src_h > 9 / 16:
            crop = "crop=ih*9/16:ih:(iw-ih*9/16)/2:0"
        else:
            crop = "crop=iw:min(ih\\,iw*16/9):0:(ih-min(ih\\,iw*16/9))/2"
        default_4k = (2160, 3840)
        default_1k = (1080, 1920)
        default_720 = (720, 1280)
        cw, ch = default_1k
    elif aspect == "16:9 Landscape":
        if src_w / src_h < 16 / 9:
            crop = "crop=iw:iw*9/16:0:(ih-iw*9/16)/2"
        else:
            crop = "crop=min(iw\\,ih*16/9):ih:(iw-min(iw\\,ih*16/9))/2:0"
        default_4k = (3840, 2160)
        default_1k = (1920, 1080)
        default_720 = (1280, 720)
        cw, ch = default_1k
    elif aspect == "1:1 Square":
        crop = "crop=min(iw\\,ih):min(iw\\,ih):(iw-min(iw\\,ih))/2:(ih-min(iw\\,ih))/2"
        default_4k = (2160, 2160)
        default_1k = (1080, 1080)
        default_720 = (720, 720)
        cw, ch = default_1k
    elif aspect == "4:5 Portrait":
        if src_w / src_h > 4 / 5:
            crop = "crop=ih*4/5:ih:(iw-ih*4/5)/2:0"
        else:
            crop = "crop=iw:iw*5/4:0:(ih-iw*5/4)/2"
        default_4k = (2160, 2700)
        default_1k = (1080, 1350)
        default_720 = (720, 900)
        cw, ch = default_1k
    else:
        crop = None
        default_4k = (max(src_w, 3840), max(src_h, 2160))
        default_1k = (max(src_w, 1920), max(src_h, 1080))
        default_720 = (max(src_w, 1280), max(src_h, 720))
        cw, ch = src_w, src_h

    if resolution == "Match Source (Zero Resize Loss)" and aspect == "Keep original":
        tw, th = src_w, src_h
    elif resolution == "4K Ultra Sharp":
        tw, th = default_4k
    elif resolution == "1080p Full HD":
        tw, th = default_1k
    elif resolution == "720p HD":
        tw, th = default_720
    else:
        # "Source or Better (Auto Sharp)" or vertical/aspect target
        tw, th = default_1k if aspect != "Keep original" else (cw, ch)

    tw = max(2, int(tw) - (int(tw) % 2))
    th = max(2, int(th) - (int(th) % 2))
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
    cues: list[tuple[float, float, str]] | None = None,
    hook_banner_text: str = "",
    hook_banner_duration: float = 8.0,
    src_w: int = 1920,
    src_h: int = 1080,
    caption_font_size: int = 28,
    encode_preset: str = "medium",
    normalize_audio: bool = True,
):
    if not source or not Path(source).exists():
        raise RuntimeError("Source video file is no longer in temporary storage. Please re-upload and load your video.")

    clip_duration = max(0.5, round(float(end) - float(start), 1))
    crop, dims, out_w, out_h = resolve_sharp_dimensions(aspect, resolution, src_w, src_h)

    graph: list[str] = []
    current = "0:v"

    if crop:
        graph.append(f"[{current}]{crop},setsar=1[vcrop]")
        current = "vcrop"

    if dims:
        graph.append(f"[{current}]scale={dims}:flags=lanczos,setsar=1[vscale]")
        current = "vscale"

    graph.append(f"[{current}]unsharp=5:5:0.35:3:3:0.15[vsharp]")
    current = "vsharp"

    font_opt = ""
    sys_font = find_system_font()
    if sys_font:
        font_opt = f":fontfile='{sys_font}'"

    clean_hook = escape_drawtext(hook_banner_text)
    if clean_hook:
        hook_font_px = max(20, int(out_h * 0.025))
        box_h = max(40, int(hook_font_px * 1.7))
        top_y = max(24, int(out_h * 0.055))
        active_end = min(clip_duration, max(1.0, float(hook_banner_duration)))
        draw_hook = (
            f"[{current}]drawbox=x=0:y={top_y - 6}:w=iw:h={box_h}:color=black@0.68:t=fill:"
            f"enable='between(t,0,{active_end:.2f})',"
            f"drawtext=text='{clean_hook}'{font_opt}:fontcolor=white:fontsize={hook_font_px}:"
            f"x=(w-text_w)/2:y={top_y + (box_h - hook_font_px)//2 - 6}:"
            f"box=1:boxcolor=black@0.45:boxborderw=6:"
            f"enable='between(t,0,{active_end:.2f})'[vhook]"
        )
        graph.append(draw_hook)
        current = "vhook"

    has_logo = bool(logo and Path(logo).exists())
    if has_logo:
        graph += [
            "[1:v]scale=iw*0.14:-1:flags=lanczos[logo]",
            f"[{current}][logo]overlay=W-w-20:20:format=auto[vlogo]"
        ]
        current = "vlogo"

    if cues:
        # Render crisp, legible lower-third captions near bottom with dark pill backing
        sub_px = max(18, min(46, int(caption_font_size)))
        bottom_margin = max(48, int(out_h * 0.085))
        for c_idx, (c_start, c_end, c_text) in enumerate(cues):
            lines_list = [escape_drawtext(ln) for ln in c_text.splitlines() if ln.strip()][:2]
            if not lines_list:
                continue
            line_step = int(sub_px * 1.25)
            total_block_h = line_step * len(lines_list)
            base_y = out_h - bottom_margin - total_block_h
            sub_filters = []
            for l_i, ln_esc in enumerate(lines_list):
                y_pos = base_y + (l_i * line_step)
                sub_filters.append(
                    f"drawtext=text='{ln_esc}'{font_opt}:fontcolor=white:fontsize={sub_px}:"
                    f"x=(w-text_w)/2:y={y_pos}:"
                    f"box=1:boxcolor=black@0.78:boxborderw=8:"
                    f"enable='between(t,{c_start:.2f},{c_end:.2f})'"
                )
            out_tag = f"vsub{c_idx}"
            graph.append(f"[{current}]{','.join(sub_filters)}[{out_tag}]")
            current = out_tag

    args = ["ffmpeg", "-y", "-ss", str(round(float(start), 2)), "-i", source]
    if has_logo:
        args += ["-i", logo]

    audio_map = ["-map", "0:a?"]
    if normalize_audio:
        audio_map += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]

    args += [
        "-filter_complex", ";".join(graph),
        "-map", f"[{current}]",
        *audio_map,
        "-t", str(clip_duration),
        "-c:v", "libx264",
        "-preset", encode_preset,
        "-crf", str(int(crf)),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "256k",
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
        "step3_clip_minutes": 0.50,
        "used_clip_windows": [],
        "hook_mode_choice": "Standard Clip Length (Exact Minutes)",
        "hook_banner_scope": "8-Second Top Banner (0–8s)",
        "burn_onscreen_captions": True,
        "onscreen_caption_size": 28,
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
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()

# ----------------------------- HEADER & NAV -----------------------------
st.markdown(f"""<div class="hero">
<div style="color:#a5b4fc;font-weight:750;font-size:.76rem;letter-spacing:.13em">9:16 VERTICAL SHORTS · PER-SHORT CAMPAIGN CAPTIONS · CRISP BOTTOM CAPTIONS · SHARP AUDIO/VIDEO</div>
<h1>🎬 {APP_NAME}</h1>
<p class="muted">Vertical 9:16 shorts for YouTube/Reels/TikTok + exact minute length + zero repeat picks + clear bottom captions + dedicated caption/description generator per short.</p>
<span class="pill">9:16 Vertical Shorts</span>
<span class="pill">Unique caption per short</span>
<span class="pill">Campaign-aware copy</span>
<span class="pill">Clear bottom captions</span>
</div>""", unsafe_allow_html=True)

if not ffmpeg_ready():
    st.error("FFmpeg and ffprobe are required for video processing. Streamlit Cloud installs them from packages.txt; install them locally and add bin to PATH.")

with st.sidebar:
    st.title("STUDIO")
    page = st.radio("Navigation", [
        "Campaign & Clip Workflow",
        "Editor & Templates",
        "Brand Kit & Settings",
    ], label_visibility="collapsed", key="nav_page_radio")
    st.divider()
    st.metric("Go-Ahead Status", "Approved" if st.session_state.go_ahead_clips else "Pending Review")
    st.metric("Used Clip Ranges", len(st.session_state.used_clip_windows))
    st.metric("Generated Shorts", len(st.session_state.clips))

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

        if st.session_state.video_info and st.session_state.source_video and Path(st.session_state.source_video).exists():
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
            placeholder="Example: Post vertical YouTube Shorts & Instagram Reels. Mention promo code or link, include hashtags, and keep captions clear."
        )

        p_col, c_col, min_col = st.columns(3)
        with p_col:
            st.selectbox(
                "Target platform",
                ["YouTube Shorts / Reels", "Instagram Feed / Reels", "TikTok", "YouTube Standard"],
                key="target_platform_choice"
            )
        with c_col:
            st.radio("Shorts count", [1, 2], horizontal=True, key="desired_clip_count")
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
                ["Standard Clip Length (Exact Minutes)", "8-Second Hook Banner (0–8s)", "Video Hook (Opening / Highlight)"],
                key="hook_mode_choice"
            )
        with cap_col:
            st.checkbox(
                "Clear bottom captions on video",
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
                        st.session_state.step3_clip_minutes = float(st.session_state.desired_clip_minutes)

                        if analysis.get("requires_onscreen_captions"):
                            st.session_state.burn_onscreen_captions = True
                        if analysis.get("requires_8s_hook"):
                            st.session_state.hook_banner_scope = "8-Second Top Banner (0–8s)"
                        elif analysis.get("requires_video_hook"):
                            st.session_state.hook_banner_scope = "Full Clip Top Banner"

                        if st.session_state.source_video and st.session_state.video_info and Path(st.session_state.source_video).exists():
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
                            )
                            st.session_state.campaign_feedback = evaluate_video_against_campaign(
                                analysis, st.session_state.video_info, st.session_state.transcript
                            )
                        else:
                            st.session_state.campaign_feedback = {
                                "ready": False,
                                "verdict": "Campaign Analysed — Upload Video to Check Fit & Extract Shorts",
                                "strengths": [],
                                "gaps": ["Upload a campaign video on the left to match moments and extract shorts."],
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
            st.markdown("**Caption & Content Requirements**")
            cap_reqs = (an.get("caption_requirements") or []) + [f"Tag: {t}" for t in (an.get("hashtags") or [])[:5]]
            for c_item in cap_reqs[:5]:
                st.markdown(f"- {c_item}")
            for g_item in (fb.get("gaps") or [])[:3]:
                st.caption(f"• {g_item}")
        with f3:
            st.markdown("**Restrictions & Checklist**")
            for w_item in (an.get("prohibited_content") or an.get("pre_publish_checklist") or [])[:5]:
                st.markdown(f"- {w_item}")

        go_col1, go_col2 = st.columns([1.2, 2])
        with go_col1:
            if st.button(
                "✅ Give Go-Ahead to Extract Shorts",
                type="primary",
                disabled=not bool(st.session_state.video_info),
                use_container_width=True,
                key="wf_go_ahead_btn"
            ):
                st.session_state.step3_clip_minutes = float(st.session_state.desired_clip_minutes)
                if st.session_state.video_info:
                    st.session_state.highlights = build_non_repeating_candidates(
                        brief=st.session_state.brief,
                        transcript=st.session_state.transcript,
                        contact_sheet=None,
                        duration=st.session_state.video_info["duration"],
                        target_platform=st.session_state.target_platform_choice,
                        clip_count=st.session_state.desired_clip_count,
                        desired_minutes=float(st.session_state.step3_clip_minutes),
                        used_windows=st.session_state.used_clip_windows,
                    )
                st.session_state.go_ahead_clips = True
                st.rerun()

        with go_col2:
            if st.session_state.go_ahead_clips:
                exact_s = minutes_to_seconds(float(st.session_state.step3_clip_minutes))
                st.success(f"Go-ahead granted! Exact clip duration: {float(st.session_state.step3_clip_minutes):.2f} min ({exact_s:.1f}s).")

    # Step 3: 1-2 Non-Repeating Clip Extraction
    if st.session_state.go_ahead_clips and st.session_state.video_info and ffmpeg_ready():
        st.divider()
        st.subheader(f"Step 3 · Extract 1–2 Fresh Shorts ({st.session_state.target_platform_choice})")
        vi = st.session_state.video_info
        dur = max(1.0, float(vi["duration"]))

        ctrl1, ctrl2, ctrl3 = st.columns([1.2, 1.2, 1])
        with ctrl1:
            st.number_input(
                "Exact clip length (minutes)",
                min_value=0.10,
                max_value=15.0,
                step=0.25,
                key="step3_clip_minutes"
            )
        active_minutes = float(st.session_state.step3_clip_minutes)
        exact_target_sec = round(min(dur, minutes_to_seconds(active_minutes)), 1)

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
                )
                st.rerun()

        st.caption(f"Target length per short: **{active_minutes:.2f} min = {exact_target_sec:.1f}s** · Default aspect for {st.session_state.target_platform_choice}: **{default_aspect_for_platform(st.session_state.target_platform_choice)}**")
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
        )
        cands = cands[:int(st.session_state.desired_clip_count)]

        q_col1, q_col2, q_col3, q_col4 = st.columns(4)
        with q_col1:
            sharp_res = st.selectbox(
                "Output Resolution",
                ["1080p Full HD", "Source or Better (Auto Sharp)", "4K Ultra Sharp", "720p HD", "Match Source (Zero Resize Loss)"],
                key="wf_sharp_res"
            )
        with q_col2:
            sharp_crf = st.slider("Quality CRF (15 = crispest)", 15, 22, 16, key="wf_sharp_crf")
        with q_col3:
            encode_preset = st.selectbox("Encode speed/quality", ["medium", "slow", "fast"], key="wf_encode_preset")
        with q_col4:
            cap_size = st.slider("On-screen caption size", 20, 42, int(st.session_state.onscreen_caption_size), key="wf_cap_size")

        asp_choices = ["9:16 Vertical", "16:9 Landscape", "1:1 Square", "4:5 Portrait", "Keep original"]
        default_asp = default_aspect_for_platform(st.session_state.target_platform_choice)
        default_asp_idx = asp_choices.index(default_asp) if default_asp in asp_choices else 0

        chosen_windows = []
        run_tag = len(st.session_state.used_clip_windows)
        max_start_limit = max(0.0, dur - 0.5)
        for idx, cand in enumerate(cands):
            with st.container(border=True):
                st.markdown(f"**Short {idx + 1}: {cand.get('title', f'Short {idx+1}')}**")
                st.caption(f"Moment: {cand.get('reason', '')}")
                wc1, wc2, wc3 = st.columns([1, 1, 1.4])
                with wc1:
                    init_s = round(min(max_start_limit, max(0.0, float(cand["start"]))), 1)
                    s_val = st.number_input(
                        f"Start (sec) — Short {idx+1}",
                        min_value=0.0,
                        max_value=max(0.5, max_start_limit),
                        value=init_s,
                        step=0.5,
                        key=f"c_start_{run_tag}_{idx}"
                    )
                with wc2:
                    exact_end = round(min(dur, float(s_val) + exact_target_sec), 1)
                    e_val = st.number_input(
                        f"End (sec) — Short {idx+1}",
                        min_value=0.5,
                        max_value=max(0.5, dur),
                        value=max(min(dur, float(s_val) + 0.5), exact_end),
                        step=0.5,
                        key=f"c_end_{run_tag}_{idx}"
                    )
                with wc3:
                    asp_val = st.selectbox(
                        f"Aspect — Short {idx+1}",
                        asp_choices,
                        index=default_asp_idx,
                        key=f"c_asp_{run_tag}_{idx}"
                    )
                hook_txt = st.text_input(
                    f"Top hook banner text — Short {idx+1}",
                    value=str(cand.get("hook") or ""),
                    key=f"c_hook_txt_{run_tag}_{idx}"
                )
                chosen_windows.append((idx + 1, cand.get("title", f"Short_{idx+1}"), float(s_val), float(e_val), asp_val, hook_txt, cand))

        if st.button("✂️ Extract & Render Sharp Shorts Now", type="primary", use_container_width=True, key="wf_extract_clips_btn"):
            try:
                rendered_now = []
                banner_dur = 8.0 if st.session_state.hook_banner_scope.startswith("8-Second") else 9999.0
                for num, title, s_v, e_v, asp_v, h_txt, cand_meta in chosen_windows:
                    if e_v <= s_v:
                        st.warning(f"Short {num}: End time must be greater than start time.")
                        continue
                    clip_num_global = len(st.session_state.clips) + 1
                    out_file = WORK_DIR / f"{safe_name(title)}_short{clip_num_global}.mp4"
                    cues = []
                    if st.session_state.burn_onscreen_captions:
                        cues = build_caption_cues(
                            transcript=st.session_state.transcript,
                            start=s_v,
                            end=e_v,
                            fallback_text=st.session_state.caption_instructions or cand_meta.get("hook") or st.session_state.brief,
                        )

                    with st.spinner(f"Rendering {asp_v} Short {num} ({s_v:.1f}s – {e_v:.1f}s = {round(e_v - s_v, 1):.1f}s)..."):
                        render_video(
                            st.session_state.source_video,
                            str(out_file),
                            s_v,
                            e_v,
                            asp_v,
                            sharp_res,
                            sharp_crf,
                            logo=st.session_state.logo_path,
                            cues=cues if cues else None,
                            hook_banner_text=h_txt,
                            hook_banner_duration=banner_dur,
                            src_w=int(vi.get("width") or 1920),
                            src_h=int(vi.get("height") or 1080),
                            caption_font_size=int(cap_size),
                            encode_preset=encode_preset,
                            normalize_audio=True,
                        )
                    st.session_state.used_clip_windows.append([round(s_v, 1), round(e_v, 1)])
                    clip_meta = {
                        **cand_meta,
                        "title": title,
                        "start": s_v,
                        "end": e_v,
                        "aspect": asp_v,
                        "hook": h_txt or cand_meta.get("hook", ""),
                        "transcript_excerpt": transcript_slice(st.session_state.transcript, s_v, e_v),
                    }
                    # Generate a unique campaign-aligned caption right away for this short
                    init_copy = generate_social_copy(
                        platform=st.session_state.target_platform_choice,
                        brief=st.session_state.brief,
                        analysis=st.session_state.brief_analysis,
                        clip_info=clip_meta,
                        transcript_text=clip_meta["transcript_excerpt"],
                        custom_instructions=st.session_state.caption_instructions,
                        clip_index=clip_num_global - 1,
                    )
                    rec = {
                        "name": f"{title} ({asp_v})",
                        "path": str(out_file),
                        "start": s_v,
                        "end": e_v,
                        "aspect": asp_v,
                        "resolution": sharp_res,
                        "meta": clip_meta,
                        "caption": init_copy["caption"],
                        "description": init_copy["description"],
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
                    )
                    st.success(f"Extracted {len(rendered_now)} sharp short(s) with unique campaign captions ready below.")
                    st.rerun()
            except Exception as exc:
                st.error(f"Clip render error: {exc}")

    # Step 4: Generated Shorts + Dedicated Per-Short Caption Generator
    if st.session_state.clips:
        st.divider()
        st.subheader("Step 4 · Generated Shorts & Per-Short Campaign Caption Generator")
        st.caption("Each short has its own independent caption & description tailored to that short's moment and your campaign brief.")

        st.text_area(
            "Optional extra caption/CTA note for shorts (applied when you click Get Caption for a Short)",
            key="caption_instructions",
            height=75,
            placeholder="Example: Highlight the discount code, mention the link in bio/description, and keep it punchy for YouTube Shorts."
        )

        if st.button("✨ Generate Unique Campaign Captions for All Shorts", key="gen_all_short_captions_btn"):
            with st.spinner("Generating tailored campaign captions for each short..."):
                for i, cl in enumerate(st.session_state.clips):
                    c_meta = cl.get("meta") or {"title": cl["name"], "start": cl["start"], "end": cl["end"]}
                    t_seg = c_meta.get("transcript_excerpt") or transcript_slice(st.session_state.transcript, cl["start"], cl["end"])
                    pack = generate_social_copy(
                        platform=st.session_state.target_platform_choice,
                        brief=st.session_state.brief,
                        analysis=st.session_state.brief_analysis,
                        clip_info=c_meta,
                        transcript_text=t_seg,
                        custom_instructions=st.session_state.caption_instructions,
                        clip_index=i,
                    )
                    st.session_state.clips[i]["caption"] = pack["caption"]
                    st.session_state.clips[i]["description"] = pack["description"]
            st.success("Updated unique campaign captions for every short.")
            st.rerun()

        for idx, cl in enumerate(st.session_state.clips):
            with st.container(border=True):
                c_vid, c_cap = st.columns([1.25, 1.35], gap="large")
                actual_sec = round(float(cl["end"]) - float(cl["start"]), 1)
                with c_vid:
                    st.markdown(f"**Short {idx + 1}: {cl['name']}**")
                    st.caption(f"{fmt_time(cl['start'])} – {fmt_time(cl['end'])} ({actual_sec:.1f}s) · {cl['aspect']} · {cl['resolution']}")
                    if Path(cl["path"]).exists():
                        st.video(cl["path"])
                        with open(cl["path"], "rb") as fh:
                            st.download_button(
                                f"⬇ Download Short {idx+1} (.MP4)",
                                fh,
                                file_name=Path(cl["path"]).name,
                                mime="video/mp4",
                                key=f"wf_dl_clip_{idx}",
                                use_container_width=True
                            )
                with c_cap:
                    st.markdown("**Caption & Description for This Short**")
                    if st.button(
                        f"✨ Get Caption for Short {idx+1} (Campaign-Matched)",
                        type="primary",
                        use_container_width=True,
                        key=f"get_cap_short_{idx}"
                    ):
                        c_meta = cl.get("meta") or {"title": cl["name"], "start": cl["start"], "end": cl["end"]}
                        t_seg = c_meta.get("transcript_excerpt") or transcript_slice(st.session_state.transcript, cl["start"], cl["end"])
                        with st.spinner(f"Writing campaign-matched caption for Short {idx+1}..."):
                            pack = generate_social_copy(
                                platform=st.session_state.target_platform_choice,
                                brief=st.session_state.brief,
                                analysis=st.session_state.brief_analysis,
                                clip_info=c_meta,
                                transcript_text=t_seg,
                                custom_instructions=st.session_state.caption_instructions,
                                clip_index=idx,
                            )
                            st.session_state.clips[idx]["caption"] = pack["caption"]
                            st.session_state.clips[idx]["description"] = pack["description"]
                        st.rerun()

                    edited_cap = st.text_area(
                        f"Short {idx+1} Caption",
                        value=cl.get("caption", ""),
                        height=110,
                        key=f"short_cap_area_{idx}"
                    )
                    edited_desc = st.text_area(
                        f"Short {idx+1} Description",
                        value=cl.get("description", ""),
                        height=135,
                        key=f"short_desc_area_{idx}"
                    )
                    st.session_state.clips[idx]["caption"] = edited_cap
                    st.session_state.clips[idx]["description"] = edited_desc

                    render_fast_voice_reader(
                        f"{edited_cap}\n\n{edited_desc}".strip(),
                        label=f"🔊 Fast Read Short {idx+1} Copy",
                        uid=f"short_{idx}"
                    )

# ----------------------------- PAGE 2: EDITOR & TEMPLATES -------------
elif page == "Editor & Templates":
    st.subheader("Manual & Sharp Clip Editor")
    if not st.session_state.video_info:
        st.info("Upload and load a video in 'Campaign & Clip Workflow' first.")
    else:
        vi = st.session_state.video_info
        dur_ed = max(1.0, float(vi["duration"]))
        if st.session_state.source_video and Path(st.session_state.source_video).exists():
            st.video(st.session_state.source_video)
        a, b = st.columns(2)
        with a:
            start = st.number_input("Start (seconds)", min_value=0.0, max_value=max(0.5, dur_ed - 0.5), value=0.0, step=0.5, key="ed_start_sec")
            end = st.number_input("End (seconds)", min_value=0.5, max_value=dur_ed, value=min(30.0, dur_ed), step=0.5, key="ed_end_sec")
            clip_name = st.text_input("Clip name", value=safe_name(Path(st.session_state.video_name or "clip").stem + "_edit"), key="ed_clip_name")
            hook_line = st.text_input("Top hook banner text", value="", key="ed_hook_line")
            hook_dur_opt = st.selectbox("Top hook duration", ["8s Banner", "Full Clip Banner"], key="ed_hook_dur")
        with b:
            aspect = st.selectbox("Aspect ratio", ["9:16 Vertical", "16:9 Landscape", "1:1 Square", "4:5 Portrait", "Keep original"], key="ed_aspect")
            resolution = st.selectbox("Output Resolution", ["1080p Full HD", "Source or Better (Auto Sharp)", "4K Ultra Sharp", "720p HD", "Match Source (Zero Resize Loss)"], key="ed_resolution")
            crf = st.slider("Quality CRF (15 = crispest)", 15, 22, 16, key="ed_crf")
            add_logo = st.checkbox("Include saved brand logo", value=bool(st.session_state.logo_path), key="ed_add_logo")
            add_sub = st.checkbox("Clear bottom captions", value=True, key="ed_add_sub")

        if st.button("Render Sharp Clip", type="primary", use_container_width=True, key="ed_render_btn"):
            if end <= start:
                st.error("End time must be greater than start time.")
            elif ffmpeg_ready():
                try:
                    clip_idx = len(st.session_state.clips) + 1
                    out = WORK_DIR / f"{safe_name(clip_name)}_{clip_idx}.mp4"
                    cues = []
                    if add_sub:
                        cues = build_caption_cues(
                            transcript=st.session_state.transcript,
                            start=start,
                            end=end,
                            fallback_text=st.session_state.caption_instructions or hook_line or st.session_state.brief,
                        )
                    with st.spinner("Rendering sharp clip..."):
                        render_video(
                            st.session_state.source_video, str(out), start, end,
                            aspect, resolution, crf,
                            logo=st.session_state.logo_path if add_logo else None,
                            cues=cues if cues else None,
                            hook_banner_text=hook_line,
                            hook_banner_duration=8.0 if hook_dur_opt.startswith("8s") else 9999.0,
                            src_w=int(vi.get("width") or 1920),
                            src_h=int(vi.get("height") or 1080),
                            caption_font_size=int(st.session_state.onscreen_caption_size),
                            normalize_audio=True,
                        )
                    st.session_state.used_clip_windows.append([round(start, 1), round(end, 1)])
                    c_meta = {
                        "title": clip_name,
                        "start": start,
                        "end": end,
                        "aspect": aspect,
                        "hook": hook_line,
                        "transcript_excerpt": transcript_slice(st.session_state.transcript, start, end),
                    }
                    pack = generate_social_copy(
                        platform=st.session_state.target_platform_choice,
                        brief=st.session_state.brief,
                        analysis=st.session_state.brief_analysis,
                        clip_info=c_meta,
                        transcript_text=c_meta["transcript_excerpt"],
                        custom_instructions=st.session_state.caption_instructions,
                        clip_index=clip_idx - 1,
                    )
                    st.session_state.clips.append({
                        "name": f"{clip_name} ({aspect})", "path": str(out), "start": start,
                        "end": end, "aspect": aspect, "resolution": resolution,
                        "meta": c_meta, "caption": pack["caption"], "description": pack["description"],
                    })
                    st.success("Sharp clip rendered.")
                    st.video(str(out))
                except Exception as exc:
                    st.error(f"Render error: {exc}")

# ----------------------------- PAGE 3: BRAND KIT & SETTINGS ------------
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
        st.success("OPENAI_API_KEY active — full AI campaign + per-short caption generation enabled.")
    else:
        st.info("No OPENAI_API_KEY detected — smart local mode is active for campaign triage, non-repeating picks, clear bottom captions, and per-short campaign-aware captions.")