from __future__ import annotations

import base64
import gc
import html
import io
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from datetime import timedelta
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

APP_NAME = "ClipStream Studio"
MAX_UPLOAD_MB = 2048
WORK_DIR = Path(tempfile.gettempdir()) / "clipstream_studio"
WORK_DIR.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title=APP_NAME, page_icon="🎬", layout="wide")

# ----------------------------- UI & THEME -------------------------------
st.markdown("""
<style>
:root{color-scheme:dark}
.stApp{background:#090c13;color:#f4f5fa}
[data-testid="stHeader"]{background:rgba(9,12,19,.92)}
.block-container{max-width:1550px;padding-top:1.2rem;padding-bottom:3rem}
[data-testid="stMetric"]{background:#151b29;border:1px solid #2b354b;padding:13px;border-radius:14px}
[data-testid="stVerticalBlockBorderWrapper"]{background:#111827;border:1px solid #293449;border-radius:15px}
.stButton>button,.stDownloadButton>button{border-radius:10px;font-weight:650;min-height:2.45rem}
[data-testid="stSidebar"]{background:#0f1623}
.hero{padding:24px;border:1px solid #34405a;border-radius:20px;background:linear-gradient(115deg,#211d3b,#141c2d 58%,#30203b);margin-bottom:1.1rem}
.hero h1{margin:0;letter-spacing:-1.2px;font-size:2.2rem}
.muted{color:#a5b0c4;font-size:.9rem}
.pill{display:inline-block;border:1px solid #46516d;color:#c7d2fe;border-radius:30px;padding:4px 10px;font-size:.78rem;margin:4px 5px 0 0}
</style>
""", unsafe_allow_html=True)

CAMPAIGN_HOOK_BANK = [
    "His first hit on the Puffco said everything",
    "Zero hesitation—straight to a massive rip",
    "Watch his reaction the second he hits it",
    "Bro did NOT expect it to hit like that",
    "Being high playing GTA hits completely different",
    "Bro is locked into GTA on a whole new level",
    "You can tell the high just kicked in on GTA",
    "GTA moments hit way harder like this",
    "One whiff of the jar and chat went crazy",
    "His reaction opening the hash jar is priceless",
    "Smelling the jars before diving back into GTA",
    "Nah look at the cloud off that hit",
    "How is the vapor still going?!",
    "Chat went wild the second he blew that out",
    "Ben and chat both got hyped off that moment",
    "The Hash Butler dropping real game mid-stream",
    "Puffco + GTA is an undefeated stream combo",
    "Best banter with the Hash Butler yet",
    "When GTA chaos meets a Puffco rip",
]

# ----------------------------- CORE HELPERS -----------------------------
def run_cmd(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)


def ffmpeg_ready() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def fmt_time(seconds: float) -> str:
    return str(timedelta(seconds=max(0, int(round(seconds)))))


def fmt_srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000.0)))
    hours = total_ms // 3_600_000
    rem = total_ms % 3_600_000
    minutes = rem // 60_000
    rem = rem % 60_000
    secs = rem // 1000
    millis = rem % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


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


def escape_ffmpeg_path(p: Path) -> str:
    resolved = p.resolve().as_posix()
    resolved = resolved.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return resolved


def find_system_font() -> str | None:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
    ]
    for candidate in candidates:
        p = Path(candidate)
        if p.exists():
            return escape_ffmpeg_path(p)
    return None


def build_zip_bytes(items: list[tuple[str, str]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        used_names: set[str] = set()
        for arcname, fpath in items:
            src = Path(fpath)
            if not src.exists() or not src.is_file():
                continue
            clean_arc = safe_name(Path(arcname).stem) + (Path(arcname).suffix or src.suffix or ".mp4")
            while clean_arc in used_names:
                clean_arc = f"{Path(clean_arc).stem}_dup{Path(clean_arc).suffix}"
            used_names.add(clean_arc)
            zf.write(src, arcname=clean_arc)
    return buf.getvalue()


def attach_uploader_percentage_bar(max_mb: int = 2048):
    """Attaches a live 0-100% file-read meter directly to Streamlit's st.file_uploader input."""
    components.html(
        f"""
        <div id="cs_pct_box" style="font-family:system-ui,-apple-system,sans-serif;background:#151b29;border:1px solid #2b354b;border-radius:12px;padding:11px 13px;color:#f4f5fa;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;font-size:13px;">
            <span id="cs_pct_lbl">Waiting for file selection in uploader below...</span>
            <strong id="cs_pct_val" style="color:#a5b4fc;">0%</strong>
          </div>
          <div style="width:100%;height:9px;background:#090c13;border-radius:999px;overflow:hidden;border:1px solid #293449;">
            <div id="cs_pct_fill" style="width:0%;height:100%;background:linear-gradient(90deg,#6366f1,#22c55e);transition:width 0.1s ease;"></div>
          </div>
          <div id="cs_pct_meta" style="margin-top:5px;font-size:12px;color:#94a3b8;">Max size: {max_mb} MB</div>
        </div>
        <script>
          (function() {{
            const parentDoc = window.parent ? window.parent.document : document;
            let boundInput = null;
            let activeJob = 0;

            function scanFile(file) {{
              if (!file) return;
              const jobId = ++activeJob;
              const lbl = document.getElementById("cs_pct_lbl");
              const val = document.getElementById("cs_pct_val");
              const fill = document.getElementById("cs_pct_fill");
              const meta = document.getElementById("cs_pct_meta");

              const total = Math.max(1, file.size || 1);
              const totalMb = (total / (1024 * 1024)).toFixed(1);
              const step = 16 * 1024 * 1024;
              let offset = 0;

              lbl.textContent = "Reading " + file.name + "...";
              meta.textContent = "0.0 / " + totalMb + " MB";

              function readNext() {{
                if (jobId !== activeJob) return;
                if (offset >= total) {{
                  fill.style.width = "100%";
                  val.textContent = "100%";
                  meta.textContent = totalMb + " / " + totalMb + " MB ready — click Load Video";
                  lbl.textContent = "Selected: " + file.name;
                  return;
                }}
                const slice = file.slice(offset, offset + step);
                const reader = new FileReader();
                reader.onload = function(ev) {{
                  if (jobId !== activeJob) return;
                  const got = (ev.target && ev.target.result && ev.target.result.byteLength) || slice.size;
                  offset = Math.min(total, offset + got);
                  const pct = Math.min(100, Math.round((offset / total) * 100));
                  fill.style.width = pct + "%";
                  val.textContent = pct + "%";
                  meta.textContent = (offset / (1024 * 1024)).toFixed(1) + " / " + totalMb + " MB";
                  requestAnimationFrame(readNext);
                }};
                reader.readAsArrayBuffer(slice);
              }}
              readNext();
            }}

            function bindUploader() {{
              const inputs = parentDoc.querySelectorAll('[data-testid="stFileUploader"] input[type="file"]');
              if (!inputs || inputs.length === 0) return;
              const target = inputs[0];
              if (target !== boundInput) {{
                boundInput = target;
                target.addEventListener("change", function() {{
                  if (target.files && target.files[0]) {{
                    scanFile(target.files[0]);
                  }}
                }});
              }}
              if (target.files && target.files[0] && document.getElementById("cs_pct_val").textContent === "0%") {{
                scanFile(target.files[0]);
              }}
            }}

            bindUploader();
            const observer = new MutationObserver(bindUploader);
            observer.observe(parentDoc.body, {{ childList: true, subtree: true }});
            setInterval(bindUploader, 900);
          }})();
        </script>
        """,
        height=96,
    )


def save_upload(uploaded, suffix: str | None = None, show_progress: bool = False) -> Path:
    ext = suffix or Path(uploaded.name).suffix.lower() or ".bin"
    path = WORK_DIR / f"{next(tempfile._get_candidate_names())}_{int(time.time())}{ext}"
    total_bytes = max(1, int(getattr(uploaded, "size", 0) or 0))
    written = 0
    chunk_size = 16 * 1024 * 1024
    last_pct = -5

    bar = st.progress(0, text="Loading video to disk: 0%") if show_progress else None
    uploaded.seek(0)
    with path.open("wb") as handle:
        while True:
            chunk = uploaded.read(chunk_size)
            if not chunk:
                break
            handle.write(chunk)
            written += len(chunk)
            pct = min(100, int((written / total_bytes) * 100))
            if bar is not None and (pct >= last_pct + 5 or pct == 100):
                last_pct = pct
                bar.progress(
                    pct,
                    text=f"Loading video to disk: {pct}% ({written / (1024*1024):.1f} / {total_bytes / (1024*1024):.1f} MB)",
                )
    if bar is not None:
        bar.progress(100, text="Video loaded to disk: 100%")
    gc.collect()
    return path


def probe_video(path: str) -> dict:
    p = run_cmd([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate",
        "-of", "json", path,
    ], timeout=60)
    if p.returncode:
        raise RuntimeError(p.stderr[-1800:] or "ffprobe could not inspect this file.")
    data = json.loads(p.stdout or "{}")
    streams = data.get("streams", [])
    v = next((x for x in streams if x.get("codec_type") == "video"), {})
    a = next((x for x in streams if x.get("codec_type") == "audio"), {})
    raw_size = data.get("format", {}).get("size")
    file_size = int(raw_size) if raw_size is not None else (Path(path).stat().st_size if Path(path).exists() else 0)
    return {
        "duration": float(data.get("format", {}).get("duration", 0) or 0),
        "size": file_size,
        "width": int(v.get("width", 0) or 0),
        "height": int(v.get("height", 0) or 0),
        "video_codec": v.get("codec_name", "unknown"),
        "audio_codec": a.get("codec_name", "none"),
        "fps": v.get("r_frame_rate", "unknown"),
    }


def break_video_into_parts(source: str, duration: float, num_parts: int) -> list[dict]:
    """Breaks a video into 3 or 5 parts quickly via stream copy with safe re-encode fallback."""
    src_path = Path(source)
    if not src_path.exists():
        raise RuntimeError("Source video file is missing.")

    n = 5 if int(num_parts) >= 5 else 3
    dur = max(1.0, float(duration))
    seg_len = dur / n
    stem = safe_name(src_path.stem)
    ext = src_path.suffix if src_path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".m4v"} else ".mp4"

    parts_out: list[dict] = []
    prog = st.progress(0, text=f"Breaking video into {n} parts: 0/{n}")

    for i in range(n):
        start_s = round(i * seg_len, 2)
        chunk_dur = round(min(seg_len, max(0.5, dur - start_s)), 2)
        if i > 0 and start_s >= dur - 0.25:
            continue

        out_p = WORK_DIR / f"{stem}_part{i+1}of{n}_{next(tempfile._get_candidate_names())}{ext}"
        cmd = [
            "ffmpeg", "-y", "-ss", str(start_s), "-i", str(src_path),
            "-t", str(chunk_dur), "-c", "copy", "-avoid_negative_ts", "make_zero",
            str(out_p),
        ]
        res = run_cmd(cmd, timeout=600)

        needs_fallback = (
            res.returncode != 0
            or not out_p.exists()
            or out_p.stat().st_size == 0
        )
        if not needs_fallback:
            try:
                chk = probe_video(str(out_p))
                if chk["duration"] <= 0.1 or chk["width"] <= 0:
                    needs_fallback = True
            except Exception:
                needs_fallback = True

        if needs_fallback:
            out_p.unlink(missing_ok=True)
            res = run_cmd([
                "ffmpeg", "-y", "-ss", str(start_s), "-i", str(src_path),
                "-t", str(chunk_dur), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(out_p),
            ], timeout=1800)
            if res.returncode or not out_p.exists():
                raise RuntimeError(res.stderr[-1500:] or f"Failed creating Part {i+1}.")

        p_info = probe_video(str(out_p))
        parts_out.append({
            "part_num": i + 1,
            "total_parts": n,
            "label": f"Part {i+1} of {n}",
            "path": str(out_p),
            "start_in_source": start_s,
            "end_in_source": round(start_s + chunk_dur, 2),
            "info": p_info,
        })
        prog.progress(int(((i + 1) / n) * 100), text=f"Breaking video into {n} parts: {i+1}/{n}")

    gc.collect()
    return parts_out


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
        with fitz.open(stream=raw, filetype="pdf") as doc:
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
        with Image.open(io.BytesIO(raw)) as im:
            return pytesseract.image_to_string(im).strip()
    raise ValueError("Unsupported file type. Use PDF, DOCX, TXT, MD, CSV or an image.")


def parse_json_response(content: str) -> dict:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("AI response did not contain valid JSON.")
    return json.loads(text[start:end + 1])


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


# ----------------------------- AUTO-CLIPPING HELPERS -----------------
def overlaps_used(start: float, end: float, used_windows: list[list[float]], min_gap: float = 2.0) -> bool:
    for u_start, u_end in used_windows:
        if start < (u_end + min_gap) and end > (u_start - min_gap):
            return True
    return False


def transcript_slice(transcript: dict | None, start: float, end: float) -> str:
    segs = (transcript or {}).get("segments") or []
    parts = []
    for sg in segs:
        s_a = float(sg.get("start", 0.0) or 0.0)
        s_b = float(sg.get("end", 0.0) or 0.0)
        if s_b > start and s_a < end:
            txt = re.sub(r"\s+", " ", str(sg.get("text", "")).strip())
            if txt:
                parts.append(txt)
    joined = " ".join(parts).strip()
    if joined:
        return joined
    full_txt = ((transcript or {}).get("text") or "").strip()
    return full_txt[:350]


def pick_good_hook(spoken_text: str, brief: str = "", clip_index: int = 0) -> str:
    low_all = f"{spoken_text} {brief}".lower()

    if any(w in low_all for w in ["butler", "rayquan", "tray"]):
        butler_hooks = [
            "The Hash Butler dropping real game mid-stream",
            "Best banter with the Hash Butler yet",
            "When GTA chaos meets a Puffco rip",
        ]
        return butler_hooks[clip_index % len(butler_hooks)]

    if any(w in low_all for w in ["puffco", "proxy", "pivot", "dab", "cloud", "vapor", "hit"]):
        puffco_hooks = [
            "His first hit on the Puffco said everything",
            "Nah look at the cloud off that hit",
            "Zero hesitation—straight to a massive rip",
            "How is the vapor still going?!",
        ]
        return puffco_hooks[clip_index % len(puffco_hooks)]

    if any(w in low_all for w in ["jar", "smell", "hash", "alien", "tech"]):
        jar_hooks = [
            "One whiff of the jar and chat went crazy",
            "His reaction opening the hash jar is priceless",
            "Bro is locked into GTA on a whole new level",
        ]
        return jar_hooks[clip_index % len(jar_hooks)]

    if any(w in low_all for w in ["gta", "game", "high"]):
        gta_hooks = [
            "Being high playing GTA hits completely different",
            "Bro is locked into GTA on a whole new level",
            "You can tell the high just kicked in on GTA",
            "GTA moments hit way harder like this",
        ]
        return gta_hooks[clip_index % len(gta_hooks)]

    clean_speech = re.sub(r"\s+", " ", (spoken_text or "").strip())
    if clean_speech and len(clean_speech.split()) >= 3:
        wds = clean_speech.split()[:8]
        return (" ".join(wds) + ("..." if len(clean_speech.split()) > 8 else ""))[:80]

    return CAMPAIGN_HOOK_BANK[clip_index % len(CAMPAIGN_HOOK_BANK)]


def pick_auto_clip_windows(
    duration: float,
    desired_minutes: float,
    count: int,
    used_windows: list[list[float]],
    transcript: dict | None = None,
) -> list[tuple[float, float, str]]:
    """Picks distinct spots across the video, each sized to desired_minutes."""
    if duration <= 0:
        return []
    count = max(1, min(6, int(count)))
    exact_len = round(min(duration, minutes_to_seconds(desired_minutes)), 1)
    max_start = round(max(0.0, duration - exact_len), 1)

    if max_start <= 0.0:
        end_s = round(min(duration, exact_len), 1)
        return [(0.0, end_s, transcript_slice(transcript, 0.0, end_s))]

    even_anchors = [
        round((i / max(1, count)) * max_start, 1)
        for i in range(count)
    ]
    extra_fractions = [0.08, 0.28, 0.48, 0.68, 0.86, 0.16, 0.38, 0.58, 0.78, 0.94, 0.0]
    random.shuffle(extra_fractions)
    anchors = even_anchors + [round(max(0.0, min(max_start, f * max_start)), 1) for f in extra_fractions]
    for _ in range(10):
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
        for idx in range(count):
            if len(picked) >= count:
                break
            s_val = round((idx / max(1, count)) * max_start, 1)
            e_val = round(min(duration, s_val + exact_len), 1)
            if e_val > s_val and not any(abs(s_val - ps) < 1.0 and abs(e_val - pe) < 1.0 for ps, pe, _ in picked):
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
        "deliverables": ["Auto-clipped segments matching desired minute length or manual range clips"],
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
            "Verify burned-in bottom subtitles fit cleanly inside the frame",
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
        ],
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
        strengths.append("Campaign caption/tag requirements detected—each clip's caption generator will incorporate them.")
    if analysis.get("requires_8s_hook"):
        strengths.append("8-second hook banner rule detected.")
    elif analysis.get("requires_video_hook"):
        strengths.append("Video hook banner framing ready.")

    must_items = analysis.get("must_include", []) or []
    req_scenes = analysis.get("required_scenes_or_moments", []) or []
    t_text = ((transcript or {}).get("text") or "").lower()

    if transcript and t_text.strip():
        strengths.append(f"Speech transcript captured ({len(t_text.split())} words) for SRT & bottom subtitles.")
        for req in (must_items + req_scenes)[:5]:
            key_words = [
                kw for kw in re.findall(r"[a-z]{4,}", req.lower())
                if kw not in {"with", "from", "that", "this", "your", "show", "must", "include", "hook", "caption"}
            ]
            if key_words and any(kw in t_text for kw in key_words[:4]):
                strengths.append(f"Transcript shows alignment with: {req}")
            else:
                gaps.append(f"Verify spoken/visual coverage for requirement: {req}")
    else:
        gaps.append("No speech transcript yet—add OPENAI_API_KEY in Streamlit Secrets/env so subtitles transcribe exact spoken words.")

    return {
        "ready": True,
        "verdict": "Ready for Go-Ahead — Auto-Clipping & Manual Clipping Unlocked",
        "strengths": strengths,
        "gaps": gaps,
        "warnings": warnings,
    }


def extract_audio(video_path: str) -> Path:
    audio_path = WORK_DIR / f"audio_{next(tempfile._get_candidate_names())}.mp3"
    p = run_cmd([
        "ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio_path),
    ], timeout=900)
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
                timestamp_granularities=["segment"],
            )
        if isinstance(result, dict):
            return result
        if hasattr(result, "model_dump"):
            return result.model_dump()
        return {"text": getattr(result, "text", str(result)), "segments": []}
    finally:
        audio_path.unlink(missing_ok=True)


def make_contact_sheet(video_path: str, duration: float, max_frames: int = 6) -> Path:
    from PIL import Image, ImageDraw
    count = max(1, min(max_frames, int(duration // 20) + 1))
    interval = max(1.0, duration / count)
    paths: list[tuple[float, Path]] = []
    try:
        for i in range(count):
            ts = min(max(0.0, duration - 0.1), i * interval)
            frame = WORK_DIR / f"frame_{next(tempfile._get_candidate_names())}.jpg"
            p = run_cmd([
                "ffmpeg", "-y", "-ss", str(ts), "-i", video_path,
                "-frames:v", "1", "-vf", "scale=240:-1", "-q:v", "6", str(frame),
            ], timeout=90)
            if p.returncode == 0 and frame.exists():
                paths.append((ts, frame))
        if not paths:
            raise RuntimeError("Could not extract video preview frames.")
        tiles = []
        for ts, path in paths:
            with Image.open(path) as raw_im:
                im = raw_im.convert("RGB")
            draw = ImageDraw.Draw(im)
            draw.rectangle((0, 0, 95, 22), fill=(0, 0, 0))
            draw.text((6, 4), f"{ts:.1f}s", fill=(255, 255, 255))
            tiles.append(im)
        cols = 3
        rows = (len(tiles) + cols - 1) // cols
        w, h = 240, tiles[0].height
        sheet = Image.new("RGB", (cols * w, rows * (h + 24)), (18, 22, 32))
        draw = ImageDraw.Draw(sheet)
        for i, im in enumerate(tiles):
            x = (i % cols) * w
            y = (i // cols) * (h + 24)
            sheet.paste(im, (x, y))
            draw.text((x + 6, y + h + 4), f"Frame {i+1}", fill=(220, 225, 240))
        out = WORK_DIR / f"contact_{next(tempfile._get_candidate_names())}.jpg"
        sheet.save(out, quality=80)
        return out
    finally:
        for _, p in paths:
            p.unlink(missing_ok=True)
        gc.collect()


def build_auto_clip_candidates(
    brief: str,
    transcript: dict | None,
    contact_sheet: Path | None,
    duration: float,
    target_platform: str,
    clip_count: int,
    desired_minutes: float,
    used_windows: list[list[float]],
) -> list[dict]:
    count = max(1, min(6, int(clip_count)))
    fresh_slots = pick_auto_clip_windows(
        duration=duration,
        desired_minutes=desired_minutes,
        count=count,
        used_windows=used_windows,
        transcript=transcript,
    )

    client = get_openai_client()
    ai_meta_list: list[dict] = []
    if client and contact_sheet is not None and contact_sheet.exists():
        try:
            segs = (transcript or {}).get("segments", []) or []
            t_lines = [f"[{float(s.get('start',0)):.1f}-{float(s.get('end',0)):.1f}] {s.get('text','')}" for s in segs]
            t_text = "\n".join(t_lines)[:12000] or (transcript or {}).get("text", "")[:12000]
            img_b64 = base64.b64encode(contact_sheet.read_bytes()).decode("utf-8")
            prompt = f"""Given target platform {target_platform}, exact requested clip duration {desired_minutes:.2f} min ({minutes_to_seconds(desired_minutes):.1f}s), and previously used ranges {used_windows},
write distinct short titles and high-retention hooks based on what is actually said/shown in each window for {count} fresh auto-clipped segment(s). Return ONLY JSON:
{{"items":[{{"title":"short title","reason":"why it fits","campaign_match":"brief match","hook":"punchy hook line from speech/moment","confidence":"high|medium|low"}}]}}
BRIEF: {brief[:6000]}
TRANSCRIPT: {t_text}
"""
            resp = client.chat.completions.create(
                model="gpt-4o-mini", temperature=0.45,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img_b64}},
                ]}],
            )
            ai_meta_list = parse_json_response(resp.choices[0].message.content or "{}").get("items", []) or []
        except Exception:
            ai_meta_list = []
        finally:
            contact_sheet.unlink(missing_ok=True)

    results = []
    for idx, (s_sec, e_sec, spoken_txt) in enumerate(fresh_slots):
        ai_m = ai_meta_list[idx] if idx < len(ai_meta_list) else {}
        dur_actual = round(max(0.5, e_sec - s_sec), 1)
        pick_num = len(used_windows) + idx + 1
        good_hook = ai_m.get("hook") or pick_good_hook(spoken_txt, brief=brief, clip_index=pick_num - 1)
        results.append({
            "title": ai_m.get("title") or f"Auto Clip #{pick_num} ({s_sec:.0f}s–{e_sec:.0f}s)",
            "start": s_sec,
            "end": e_sec,
            "mode": "Auto Clip",
            "target_platform": target_platform,
            "reason": spoken_txt[:150] if spoken_txt else (ai_m.get("reason") or f"Exact {desired_minutes:.2f} min ({dur_actual:.1f}s) auto-cut segment at {s_sec:.1f}s–{e_sec:.1f}s."),
            "campaign_match": ai_m.get("campaign_match") or f"Exact {desired_minutes:.2f}m auto-clipped spot",
            "hook": str(good_hook)[:85],
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
        clean_tags = f"#Campaign #{p_slug} #Clip{clip_index + 1}"

    clip_title = (clip_info or {}).get("title") or f"Clip #{clip_index + 1}"
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
            f"What's in this clip: {transcript_text[:140].strip()}" if transcript_text.strip() else f"Focused moment #{clip_index + 1} tailored for {platform}.",
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

    prompt = f"""You are writing a UNIQUE social media CAPTION and DESCRIPTION specifically for ONE video clip (Clip #{clip_index + 1}) on {platform}.
Make sure this clip's caption is distinct from other clips in the same batch, reflects this clip's exact moment/transcript/hook, and satisfies every relevant campaign brief caption/content requirement.

THIS CLIP DETAILS:
{json.dumps(clip_info or {}, ensure_ascii=False)}
THIS CLIP'S TRANSCRIPT SEGMENT:
{transcript_text[:1800]}

CAMPAIGN BRIEF & CAPTION REQUIREMENTS:
{camp_ctx[:3500]}

EXTRA USER INSTRUCTIONS:
{req_note or "None"}

Return ONLY valid JSON:
{{"caption": "unique platform-ready caption for THIS clip incorporating campaign caption requirements and hashtags", "description": "clear full description tailored to THIS clip and campaign brief requirements"}}
"""
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.55,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "You write distinct, campaign-compliant social captions and descriptions for individual video clips."},
            {"role": "user", "content": prompt},
        ],
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
                if len(lines) >= max_lines:
                    return "\n".join(lines)
            cur = w[:max_chars]
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return "\n".join(lines[:max_lines])


def build_caption_cues(
    transcript: dict | None,
    start: float,
    end: float,
    fallback_text: str = "",
    max_chars: int = 34,
) -> list[tuple[float, float, str]]:
    """Builds time-synced subtitle cues strictly from actual spoken words."""
    clip_dur = max(0.5, round(end - start, 2))
    cues: list[tuple[float, float, str]] = []

    raw_segs = (transcript or {}).get("segments") or []
    overlapping = []
    for sg in raw_segs:
        a = float(sg.get("start", 0.0) or 0.0)
        b = float(sg.get("end", 0.0) or 0.0)
        txt = re.sub(r"\s+", " ", str(sg.get("text", "")).strip())
        if not txt or b <= start or a >= end:
            continue
        rel_a = round(max(0.0, a - start), 2)
        rel_b = round(min(clip_dur, b - start), 2)
        if rel_b > rel_a + 0.15:
            overlapping.append((rel_a, rel_b, txt))

    for rel_a, rel_b, txt in overlapping:
        clauses = [p.strip() for p in re.split(r"(?<=[.!?])\s+|,\s+|\s+-\s+|\s+—\s+", txt) if p.strip()]
        chunks: list[str] = []
        for cl in clauses:
            words = cl.split()
            if len(words) <= 6:
                chunks.append(cl)
            else:
                for i in range(0, len(words), 6):
                    chunks.append(" ".join(words[i:i + 6]))

        total_words = sum(max(1, len(c.split())) for c in chunks) or 1
        span = rel_b - rel_a
        cursor = rel_a
        for ch in chunks:
            w_share = max(1, len(ch.split())) / total_words
            ch_dur = max(0.35, span * w_share)
            ch_end = round(min(rel_b, cursor + ch_dur), 2)
            wrapped = wrap_caption_lines(ch, max_chars=max_chars, max_lines=2)
            if wrapped and ch_end > cursor + 0.12:
                cues.append((round(cursor, 2), ch_end, wrapped))
            cursor = ch_end

    if not cues:
        clean_speech = re.sub(r"\s+", " ", (fallback_text or "").strip())
        if clean_speech:
            words = clean_speech.split()
            phrases = [" ".join(words[i:i + 6]) for i in range(0, len(words), 6)]
            n_p = max(1, len(phrases))
            step = clip_dur / n_p
            for i, ph in enumerate(phrases):
                b_start = round(i * step, 2)
                b_end = round(min(clip_dur, (i + 1) * step), 2)
                wrapped = wrap_caption_lines(ph, max_chars=max_chars, max_lines=2)
                if wrapped and b_end > b_start + 0.12:
                    cues.append((b_start, b_end, wrapped))

    return cues[:64]


def write_srt_file(cues: list[tuple[float, float, str]], clip_duration: float, srt_path: Path) -> Path:
    """Writes valid SRT subtitle blocks clamped cleanly inside [0, clip_duration]."""
    blocks: list[str] = []
    idx = 1
    for c_start, c_end, c_text in cues:
        a_sec = max(0.0, min(clip_duration, float(c_start)))
        b_sec = max(a_sec + 0.15, min(clip_duration, float(c_end)))
        clean_lines = "\n".join(
            re.sub(r"\s+", " ", ln).strip()
            for ln in (c_text or "").splitlines()
            if ln.strip()
        )
        if not clean_lines:
            continue
        blocks.append(f"{idx}\n{fmt_srt_time(a_sec)} --> {fmt_srt_time(b_sec)}\n{clean_lines}\n")
        idx += 1

    srt_path.write_text("\n".join(blocks), encoding="utf-8")
    return srt_path


def resolve_sharp_dimensions(
    aspect: str,
    resolution: str,
    src_w: int,
    src_h: int,
    clip_index: int = 0,
) -> tuple[str | None, str | None, int, int]:
    src_w = max(2, int(src_w or 1920))
    src_h = max(2, int(src_h or 1080))
    pan_phase = (clip_index % 3) * 0.5

    if aspect == "9:16 Vertical":
        if src_w / src_h > 9 / 16:
            crop = f"crop=ih*9/16:ih:(iw-ih*9/16)*({pan_phase:.2f}+0.15*sin(t*0.6)):0"
        else:
            crop = f"crop=iw:min(ih\\,iw*16/9):0:(ih-min(ih\\,iw*16/9))*{pan_phase:.2f}"
        default_4k = (2160, 3840)
        default_1k = (1080, 1920)
        default_720 = (720, 1280)
        cw, ch = default_1k
    elif aspect == "16:9 Landscape":
        if src_w / src_h < 16 / 9:
            crop = f"crop=iw:iw*9/16:0:(ih-iw*9/16)*({pan_phase:.2f}+0.1*sin(t*0.5))"
        else:
            crop = f"crop=min(iw\\,ih*16/9):ih:(iw-min(iw\\,ih*16/9))*({pan_phase:.2f}+0.15*sin(t*0.5)):0"
        default_4k = (3840, 2160)
        default_1k = (1920, 1080)
        default_720 = (1280, 720)
        cw, ch = default_1k
    elif aspect == "1:1 Square":
        crop = f"crop=min(iw\\,ih):min(iw\\,ih):(iw-min(iw\\,ih))*({pan_phase:.2f}+0.12*sin(t*0.5)):(ih-min(iw\\,ih))*0.45"
        default_4k = (2160, 2160)
        default_1k = (1080, 1080)
        default_720 = (720, 720)
        cw, ch = default_1k
    elif aspect == "4:5 Portrait":
        if src_w / src_h > 4 / 5:
            crop = f"crop=ih*4/5:ih:(iw-ih*4/5)*({pan_phase:.2f}+0.15*sin(t*0.5)):0"
        else:
            crop = f"crop=iw:iw*5/4:0:(ih-iw*5/4)*{pan_phase:.2f}"
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
    caption_font_size: int = 30,
    encode_preset: str = "veryfast",
    normalize_audio: bool = False,
    clip_index: int = 0,
    srt_output_path: str | None = None,
) -> str | None:
    if not source or not Path(source).exists():
        raise RuntimeError("Source or broken-part video file is missing. Load or break the video again.")

    clip_duration = max(0.5, round(float(end) - float(start), 1))
    crop, dims, out_w, out_h = resolve_sharp_dimensions(
        aspect, resolution, src_w, src_h, clip_index=clip_index
    )

    graph: list[str] = []
    current = "0:v"

    if crop:
        graph.append(f"[{current}]{crop},setsar=1[vcrop]")
        current = "vcrop"

    if dims:
        graph.append(f"[{current}]scale={dims}:flags=bilinear,setsar=1[vscale]")
        current = "vscale"

    font_opt = ""
    sys_font = find_system_font()
    if sys_font:
        font_opt = f":fontfile='{sys_font}'"

    temp_text_files: list[Path] = []
    srt_file: Path | None = None
    try:
        clean_hook = (hook_banner_text or "").strip()
        if clean_hook:
            hook_txt_path = WORK_DIR / f"hook_{next(tempfile._get_candidate_names())}.txt"
            hook_txt_path.write_text(clean_hook[:120], encoding="utf-8")
            temp_text_files.append(hook_txt_path)
            hook_file_esc = escape_ffmpeg_path(hook_txt_path)

            hook_font_px = max(20, int(out_h * 0.025))
            box_h = max(40, int(hook_font_px * 1.7))
            top_y = max(24, int(out_h * 0.055))
            active_end = min(clip_duration, max(1.0, float(hook_banner_duration)))
            draw_hook = (
                f"[{current}]drawbox=x=0:y={top_y - 6}:w=iw:h={box_h}:color=black@0.68:t=fill:"
                f"enable='between(t,0,{active_end:.2f})',"
                f"drawtext=textfile='{hook_file_esc}'{font_opt}:fontcolor=white:fontsize={hook_font_px}:"
                f"x=(w-text_w)/2:y={top_y + (box_h - hook_font_px)//2 - 6}:"
                f"box=1:boxcolor=black@0.45:boxborderw=6:"
                f"enable='between(t,0,{active_end:.2f})'[vhook]"
            )
            graph.append(draw_hook)
            current = "vhook"

        has_logo = bool(logo and Path(logo).exists())
        if has_logo:
            graph += [
                "[1:v]scale=iw*0.14:-1:flags=bilinear[logo]",
                f"[{current}][logo]overlay=W-w-20:20:format=auto[vlogo]",
            ]
            current = "vlogo"

        if cues:
            if srt_output_path:
                srt_file = write_srt_file(cues, clip_duration, Path(srt_output_path))

            scale_ratio = out_h / 1080.0
            sub_px = max(20, min(52, int(round(int(caption_font_size) * max(0.85, scale_ratio)))))
            bottom_margin = max(68, min(int(out_h * 0.22), int(round(out_h * 0.095))))
            line_step = int(sub_px * 1.32)
            sub_filters: list[str] = []

            for c_start, c_end, c_text in cues[:64]:
                c_a = max(0.0, min(clip_duration, float(c_start)))
                c_b = max(c_a + 0.15, min(clip_duration, float(c_end)))
                lines_list = [
                    re.sub(r"\s+", " ", ln).strip()
                    for ln in (c_text or "").splitlines()
                    if ln.strip()
                ][:2]
                if not lines_list:
                    continue
                total_block_h = line_step * len(lines_list)
                base_y = out_h - bottom_margin - total_block_h
                for l_i, raw_ln in enumerate(lines_list):
                    cue_file = WORK_DIR / f"cue_{next(tempfile._get_candidate_names())}.txt"
                    cue_file.write_text(raw_ln, encoding="utf-8")
                    temp_text_files.append(cue_file)
                    cue_file_esc = escape_ffmpeg_path(cue_file)
                    y_pos = max(16, base_y + (l_i * line_step))
                    sub_filters.append(
                        f"drawtext=textfile='{cue_file_esc}'{font_opt}:fontcolor=white:fontsize={sub_px}:"
                        f"x=(w-text_w)/2:y={y_pos}:"
                        f"box=1:boxcolor=black@0.82:boxborderw=10:"
                        f"enable='between(t,{c_a:.2f},{c_b:.2f})'"
                    )

            if sub_filters:
                graph.append(f"[{current}]{','.join(sub_filters)}[vsub]")
                current = "vsub"

        args = ["ffmpeg", "-y", "-ss", str(round(float(start), 2)), "-i", source]
        if has_logo:
            args += ["-i", str(logo)]

        audio_map = ["-map", "0:a?"]
        if normalize_audio:
            audio_map += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]

        if graph:
            args += ["-filter_complex", ";".join(graph), "-map", f"[{current}]"]
        else:
            args += ["-map", "0:v"]

        args += [
            *audio_map,
            "-t", str(clip_duration),
            "-c:v", "libx264",
            "-preset", encode_preset,
            "-crf", str(int(crf)),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "160k",
            "-movflags", "+faststart",
            output,
        ]
        p = run_cmd(args, timeout=1800)
        if p.returncode or not Path(output).exists():
            raise RuntimeError(p.stderr[-2500:] or "FFmpeg render failed.")
        return str(srt_file) if srt_file and srt_file.exists() else None
    finally:
        for tf in temp_text_files:
            tf.unlink(missing_ok=True)
        gc.collect()


def sync_auto_clip_count(source_key: str):
    val = max(1, min(6, int(st.session_state.get(source_key, 2))))
    st.session_state["auto_clip_count"] = val
    st.session_state["auto_clip_count_top"] = val
    st.session_state["auto_clip_count_step3"] = val


def init_state():
    defaults = {
        "brief": "",
        "brief_analysis": None,
        "campaign_feedback": None,
        "go_ahead_clips": False,
        "target_platform_choice": "YouTube Shorts / Reels",
        "auto_clip_count": 2,
        "auto_clip_count_top": 2,
        "auto_clip_count_step3": 2,
        "desired_clip_minutes": 0.50,
        "auto_clip_minutes": 0.50,
        "used_clip_windows": [],
        "hook_mode_choice": "Standard Clip Length (Exact Minutes)",
        "hook_banner_scope": "8-Second Top Banner (0–8s)",
        "pending_hook_banner_scope": None,
        "burn_subtitles_chk_v2": True,
        "onscreen_caption_size": 30,
        "source_video": None,
        "video_name": None,
        "video_info": None,
        "broken_parts": [],
        "breaker_part_count": 3,
        "transcript": None,
        "auto_candidates": [],
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
<div style="color:#a5b4fc;font-weight:750;font-size:.76rem;letter-spacing:.13em">AUTO-CLIPPING (EXACT MINUTES) · MANUAL CLIPPING (START → END) · SRT + SUBTITLES</div>
<h1>🎬 {APP_NAME}</h1>
<p class="muted">Auto-clip different spots across your video in exact minutes, or manually clip from one timestamp to another.</p>
<span class="pill">Auto-clip different spots</span>
<span class="pill">Exact minute length</span>
<span class="pill">Manual start → end clipper</span>
<span class="pill">Burned subtitles + .SRT</span>
</div>""", unsafe_allow_html=True)

if not ffmpeg_ready():
    st.error("FFmpeg and ffprobe are required for video processing. Add `ffmpeg` to `packages.txt` on Streamlit Cloud or install it locally.")

with st.sidebar:
    st.title("STUDIO")
    page = st.radio("Navigation", [
        "Campaign & Auto-Clipper",
        "Manual Clipper (Start → End)",
        "Brand Kit & Settings",
    ], label_visibility="collapsed", key="nav_page_radio")
    st.divider()
    st.metric("Go-Ahead Status", "Approved" if st.session_state.go_ahead_clips else "Pending Review")
    st.metric("Broken Parts Ready", len(st.session_state.broken_parts))
    st.metric("Generated Clips", len(st.session_state.clips))

# ----------------------------- PAGE 1: CAMPAIGN & AUTO-CLIPPER --------
if page == "Campaign & Auto-Clipper":
    st.subheader("Step 1 · Video (Single Uploader + Live % + 3/5-Part Breaker) + Campaign Requirements")
    left_col, right_col = st.columns([1.15, 1], gap="large")

    with left_col:
        st.text_input("Project / Campaign Name", key="project_name")
        attach_uploader_percentage_bar(MAX_UPLOAD_MB)
        video_up = st.file_uploader("Upload campaign video", type=["mp4", "mov", "mkv", "webm", "m4v"], key="wf_video_up")
        if video_up and video_up.size > MAX_UPLOAD_MB * 1024 * 1024:
            st.error(f"File exceeds {MAX_UPLOAD_MB} MB limit.")
        elif video_up and st.button("Load Video", type="primary", key="wf_load_video"):
            try:
                vpath = save_upload(video_up, show_progress=True)
                with st.spinner("Inspecting video metadata..."):
                    vinfo = probe_video(str(vpath))
                    st.session_state.source_video = str(vpath)
                    st.session_state.video_name = video_up.name
                    st.session_state.video_info = vinfo
                    st.session_state.broken_parts = []
                    st.session_state.transcript = None
                    st.session_state.auto_candidates = []
                    st.session_state.used_clip_windows = []
                    st.session_state.campaign_feedback = None
                    st.session_state.go_ahead_clips = False
                st.success(f"Loaded {video_up.name} ({fmt_time(vinfo['duration'])})")
            except Exception as exc:
                st.error(f"Video error: {exc}")

        if st.session_state.video_info and st.session_state.source_video and Path(st.session_state.source_video).exists():
            vi = st.session_state.video_info
            size_mb = round((vi.get("size") or Path(st.session_state.source_video).stat().st_size) / (1024 * 1024), 1)
            if size_mb < 200:
                st.video(st.session_state.source_video)
            else:
                st.info(f"Large file ({size_mb} MB) saved to disk—inline preview paused to save RAM. Break into 3 or 5 parts below.")
            m1, m2, m3 = st.columns(3)
            m1.metric("Duration", fmt_time(vi["duration"]))
            m2.metric("Resolution", f"{vi['width']}x{vi['height']}")
            m3.metric("File Size", f"{size_mb} MB")

            if ffmpeg_ready() and float(vi["duration"]) >= 1.5:
                st.radio("Break video into how many parts?", [3, 5], horizontal=True, key="breaker_part_count")
                if st.button(f"✂️ Break Video into {st.session_state.breaker_part_count} Parts", use_container_width=True, key="break_3_5_parts_btn"):
                    try:
                        st.session_state.broken_parts = break_video_into_parts(
                            st.session_state.source_video,
                            float(vi["duration"]),
                            int(st.session_state.breaker_part_count),
                        )
                        st.success(f"Created {len(st.session_state.broken_parts)} parts! Clip any part in Manual Clipper anytime.")
                    except Exception as exc:
                        st.error(f"Break error: {exc}")

            if st.session_state.broken_parts:
                with st.expander(f"Broken Parts Ready ({len(st.session_state.broken_parts)} parts) — Download or Clip Later"):
                    zip_items = [
                        (f"{safe_name(st.session_state.video_name or 'video')}_part{p['part_num']}{Path(p['path']).suffix}", p["path"])
                        for p in st.session_state.broken_parts if Path(p["path"]).exists()
                    ]
                    if zip_items:
                        st.download_button(
                            f"📦 Download All {len(zip_items)} Parts (.ZIP)",
                            build_zip_bytes(zip_items),
                            file_name=f"{safe_name(st.session_state.video_name or 'video')}_{len(zip_items)}_parts.zip",
                            mime="application/zip",
                            key="wf_dl_all_parts_zip",
                            use_container_width=True,
                        )
                    for p in st.session_state.broken_parts:
                        p_mb = round(p["info"]["size"] / (1024 * 1024), 1)
                        st.write(f"**{p['label']}**: {fmt_time(p['start_in_source'])} – {fmt_time(p['end_in_source'])} ({fmt_time(p['info']['duration'])} · {p_mb} MB)")
                        if Path(p["path"]).exists():
                            with open(p["path"], "rb") as fh:
                                st.download_button(f"⬇ Download {p['label']}", fh, file_name=Path(p["path"]).name, key=f"wf_dl_part_{p['part_num']}")

    with right_col:
        brief_files = st.file_uploader(
            "Optional brief files (PDF, DOCX, TXT, MD, CSV, images)",
            type=["pdf", "docx", "txt", "md", "csv", "png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True,
            key="wf_brief_files",
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

        st.text_area(
            "Paste campaign requirements",
            key="brief",
            height=175,
            placeholder="Example: Post vertical YouTube Shorts & Instagram Reels. Include hooks, bottom subtitles + SRT file, and hashtags.",
        )

        p_col, c_col, min_col = st.columns(3)
        with p_col:
            st.selectbox(
                "Target platform",
                ["YouTube Shorts / Reels", "Instagram Feed / Reels", "TikTok", "YouTube Standard"],
                key="target_platform_choice",
            )
        with c_col:
            st.number_input(
                "Auto-clip count",
                min_value=1,
                max_value=6,
                step=1,
                key="auto_clip_count_top",
                on_change=sync_auto_clip_count,
                args=("auto_clip_count_top",),
            )
        with min_col:
            st.number_input(
                "Desired clip length (minutes)",
                min_value=0.10,
                max_value=15.0,
                step=0.25,
                key="desired_clip_minutes",
            )

        h_col, cap_col = st.columns(2)
        with h_col:
            st.selectbox(
                "Hook strategy",
                ["Standard Clip Length (Exact Minutes)", "8-Second Hook Banner (0–8s)", "Video Hook (Opening / Highlight)"],
                key="hook_mode_choice",
            )
        with cap_col:
            burn_captions_enabled = st.checkbox(
                "Burn bottom subtitles + export .SRT",
                key="burn_subtitles_chk_v2",
            )

        pending_scope = st.session_state.get("pending_hook_banner_scope")
        if pending_scope in {"8-Second Top Banner (0–8s)", "Full Clip Top Banner"}:
            st.session_state["hook_banner_scope"] = pending_scope
            st.session_state["pending_hook_banner_scope"] = None

        st.selectbox(
            "Hook banner placement timing",
            ["8-Second Top Banner (0–8s)", "Full Clip Top Banner"],
            key="hook_banner_scope",
        )

        active_count = int(st.session_state.get("auto_clip_count", 2))

        if st.button("🧠 Analyse Campaign & Video Fit", type="primary", use_container_width=True, key="wf_analyse_btn"):
            if not st.session_state.brief.strip():
                st.error("Enter campaign requirements first.")
            else:
                try:
                    with st.spinner("Analysing campaign requirements and inspecting video..."):
                        analysis = ai_campaign_analysis(st.session_state.brief, st.session_state.project_name)
                        st.session_state.brief_analysis = analysis
                        st.session_state.auto_clip_minutes = float(st.session_state.desired_clip_minutes)

                        if analysis.get("requires_8s_hook"):
                            st.session_state["pending_hook_banner_scope"] = "8-Second Top Banner (0–8s)"
                        elif analysis.get("requires_video_hook"):
                            st.session_state["pending_hook_banner_scope"] = "Full Clip Top Banner"

                        if st.session_state.source_video and st.session_state.video_info and Path(st.session_state.source_video).exists():
                            tr = None
                            if get_openai_client():
                                try:
                                    with st.spinner("Transcribing spoken words for bottom subtitles & SRT..."):
                                        tr = transcribe_video(st.session_state.source_video)
                                        st.session_state.transcript = tr
                                except Exception as t_exc:
                                    st.warning(f"Transcription skipped: {t_exc}")
                            sheet = make_contact_sheet(st.session_state.source_video, st.session_state.video_info["duration"], max_frames=6)
                            st.session_state.auto_candidates = build_auto_clip_candidates(
                                brief=json.dumps(analysis, ensure_ascii=False),
                                transcript=tr,
                                contact_sheet=sheet,
                                duration=st.session_state.video_info["duration"],
                                target_platform=st.session_state.target_platform_choice,
                                clip_count=active_count,
                                desired_minutes=float(st.session_state.desired_clip_minutes),
                                used_windows=st.session_state.used_clip_windows,
                            )
                            st.session_state.campaign_feedback = evaluate_video_against_campaign(
                                analysis, st.session_state.video_info, st.session_state.transcript
                            )
                        else:
                            st.session_state.campaign_feedback = {
                                "ready": False,
                                "verdict": "Campaign Analysed — Upload Video to Check Fit & Auto-Clip",
                                "strengths": [],
                                "gaps": ["Upload a campaign video on the left to match moments and auto-clip."],
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
                "✅ Give Go-Ahead to Auto-Clip",
                type="primary",
                disabled=not bool(st.session_state.video_info),
                use_container_width=True,
                key="wf_go_ahead_btn",
            ):
                st.session_state.auto_clip_minutes = float(st.session_state.desired_clip_minutes)
                if st.session_state.video_info:
                    st.session_state.auto_candidates = build_auto_clip_candidates(
                        brief=st.session_state.brief,
                        transcript=st.session_state.transcript,
                        contact_sheet=None,
                        duration=st.session_state.video_info["duration"],
                        target_platform=st.session_state.target_platform_choice,
                        clip_count=int(st.session_state.get("auto_clip_count", 2)),
                        desired_minutes=float(st.session_state.auto_clip_minutes),
                        used_windows=st.session_state.used_clip_windows,
                    )
                st.session_state.go_ahead_clips = True

        with go_col2:
            if st.session_state.go_ahead_clips:
                exact_s = minutes_to_seconds(float(st.session_state.auto_clip_minutes))
                st.success(f"Go-ahead granted! Auto-clip target: {float(st.session_state.auto_clip_minutes):.2f} min ({exact_s:.1f}s) per clip.")

    # Step 3: True Auto-Clipping (Different spots across video, exact desired minutes)
    if st.session_state.go_ahead_clips and st.session_state.video_info and ffmpeg_ready():
        st.divider()
        st.subheader("Step 3 · Auto-Clipper (Different Spots · Exact Desired Minutes)")
        vi = st.session_state.video_info
        dur = max(1.0, float(vi["duration"]))
        use_subs = bool(st.session_state.get("burn_subtitles_chk_v2", True))

        if use_subs and not st.session_state.transcript and not get_openai_client():
            st.warning("No transcript available because `OPENAI_API_KEY` is not set—bottom subtitles will use transcript text only if available rather than live word-by-word speech.")

        ctrl1, ctrl2, ctrl3, ctrl4 = st.columns([1.1, 1.0, 1.2, 1.0])
        with ctrl1:
            st.number_input(
                "Desired clip minutes",
                min_value=0.10,
                max_value=15.0,
                step=0.25,
                key="auto_clip_minutes",
            )
        with ctrl2:
            st.number_input(
                "How many auto clips",
                min_value=1,
                max_value=6,
                step=1,
                key="auto_clip_count_step3",
                on_change=sync_auto_clip_count,
                args=("auto_clip_count_step3",),
            )
        active_minutes = float(st.session_state.auto_clip_minutes)
        active_count = int(st.session_state.get("auto_clip_count", 2))
        exact_target_sec = round(min(dur, minutes_to_seconds(active_minutes)), 1)

        with ctrl3:
            if st.button("🔄 Pick Different Spots", use_container_width=True, key="pick_fresh_spots_btn"):
                st.session_state.auto_candidates = build_auto_clip_candidates(
                    brief=st.session_state.brief,
                    transcript=st.session_state.transcript,
                    contact_sheet=None,
                    duration=dur,
                    target_platform=st.session_state.target_platform_choice,
                    clip_count=active_count,
                    desired_minutes=active_minutes,
                    used_windows=st.session_state.used_clip_windows,
                )
        with ctrl4:
            if st.button("🧹 Reset Used Spots", use_container_width=True, key="reset_used_ranges_btn"):
                st.session_state.used_clip_windows = []
                st.session_state.auto_candidates = build_auto_clip_candidates(
                    brief=st.session_state.brief,
                    transcript=st.session_state.transcript,
                    contact_sheet=None,
                    duration=dur,
                    target_platform=st.session_state.target_platform_choice,
                    clip_count=active_count,
                    desired_minutes=active_minutes,
                    used_windows=[],
                )

        st.caption(
            f"Each auto-clipped segment will be **{active_minutes:.2f} min ({exact_target_sec:.1f}s)** from distinct spots in the video · "
            f"Default aspect for {st.session_state.target_platform_choice}: **{default_aspect_for_platform(st.session_state.target_platform_choice)}**"
        )

        cands = st.session_state.auto_candidates or build_auto_clip_candidates(
            brief=st.session_state.brief,
            transcript=st.session_state.transcript,
            contact_sheet=None,
            duration=dur,
            target_platform=st.session_state.target_platform_choice,
            clip_count=active_count,
            desired_minutes=active_minutes,
            used_windows=st.session_state.used_clip_windows,
        )
        cands = cands[:active_count]

        q_col1, q_col2, q_col3, q_col4 = st.columns(4)
        with q_col1:
            sharp_res = st.selectbox(
                "Output Resolution",
                ["1080p Full HD", "720p HD", "4K Ultra Sharp", "Source or Better (Auto Sharp)", "Match Source (Zero Resize Loss)"],
                key="wf_sharp_res",
            )
        with q_col2:
            sharp_crf = st.slider("Quality CRF (15 = crispest)", 15, 22, 18, key="wf_sharp_crf")
        with q_col3:
            encode_preset = st.selectbox("Encode speed/quality", ["veryfast", "fast", "ultrafast"], key="wf_encode_preset")
        with q_col4:
            cap_size = st.slider("Bottom subtitle size", 22, 44, int(st.session_state.onscreen_caption_size), key="wf_cap_size")

        default_asp = default_aspect_for_platform(st.session_state.target_platform_choice)

        max_start_limit = max(0.0, dur - 0.5)
        auto_windows_to_render: list[tuple[int, str, float, float, str, str, dict]] = []
        for idx, cand in enumerate(cands):
            s_val = round(min(max_start_limit, max(0.0, float(cand["start"]))), 1)
            e_val = round(min(dur, s_val + exact_target_sec), 1)
            if e_val <= s_val:
                e_val = round(min(dur, s_val + 0.5), 1)
            spoken_now = transcript_slice(st.session_state.transcript, s_val, e_val)
            hook_txt = cand.get("hook") or pick_good_hook(spoken_now, brief=st.session_state.brief, clip_index=idx)
            seg_dur = round(e_val - s_val, 1)
            st.info(
                f"**Auto Spot {idx + 1}: {cand.get('title', f'Auto Clip {idx+1}')}** — "
                f"`{fmt_time(s_val)}` → `{fmt_time(e_val)}` (**{seg_dur:.1f}s** / `{active_minutes:.2f} min`) · "
                f"Hook: *{hook_txt}*"
            )
            auto_windows_to_render.append((idx + 1, cand.get("title", f"Auto_Clip_{idx+1}"), s_val, e_val, default_asp, hook_txt, cand))

        if st.button("⚡ Run Auto-Clipper (Render All Distinct Spots Now)", type="primary", use_container_width=True, key="wf_extract_clips_btn"):
            try:
                if use_subs and not st.session_state.transcript and get_openai_client():
                    try:
                        with st.spinner("Transcribing speech for bottom subtitles & SRT..."):
                            st.session_state.transcript = transcribe_video(st.session_state.source_video)
                    except Exception:
                        pass

                rendered_now = []
                banner_dur = 8.0 if st.session_state.hook_banner_scope.startswith("8-Second") else 9999.0
                for num, title, s_v, e_v, asp_v, h_txt, cand_meta in auto_windows_to_render:
                    if e_v <= s_v:
                        continue
                    clip_num_global = len(st.session_state.clips) + 1
                    base_stem = f"{safe_name(title)}_auto{clip_num_global}"
                    out_file = WORK_DIR / f"{base_stem}.mp4"
                    srt_file = WORK_DIR / f"{base_stem}.srt"
                    spoken_now = transcript_slice(st.session_state.transcript, s_v, e_v)
                    final_hook = (h_txt or "").strip() or pick_good_hook(spoken_now, brief=st.session_state.brief, clip_index=clip_num_global - 1)

                    wrap_chars = 26 if asp_v in {"9:16 Vertical", "4:5 Portrait", "1:1 Square"} else 38
                    cues = []
                    if use_subs:
                        cues = build_caption_cues(
                            transcript=st.session_state.transcript,
                            start=s_v,
                            end=e_v,
                            fallback_text=spoken_now,
                            max_chars=wrap_chars,
                        )

                    with st.spinner(f"Auto-clipping spot {num}: {s_v:.1f}s – {e_v:.1f}s ({round(e_v - s_v, 1):.1f}s)..."):
                        saved_srt = render_video(
                            st.session_state.source_video,
                            str(out_file),
                            s_v,
                            e_v,
                            asp_v,
                            sharp_res,
                            sharp_crf,
                            logo=st.session_state.logo_path,
                            cues=cues if cues else None,
                            hook_banner_text=final_hook,
                            hook_banner_duration=banner_dur,
                            src_w=int(vi.get("width") or 1920),
                            src_h=int(vi.get("height") or 1080),
                            caption_font_size=int(cap_size),
                            encode_preset=encode_preset,
                            normalize_audio=False,
                            clip_index=clip_num_global - 1,
                            srt_output_path=str(srt_file),
                        )
                    st.session_state.used_clip_windows.append([round(s_v, 1), round(e_v, 1)])
                    clip_meta = {
                        **cand_meta,
                        "mode": "Auto Clip",
                        "title": title,
                        "start": s_v,
                        "end": e_v,
                        "aspect": asp_v,
                        "hook": final_hook,
                        "transcript_excerpt": spoken_now,
                    }
                    init_copy = generate_social_copy(
                        platform=st.session_state.target_platform_choice,
                        brief=st.session_state.brief,
                        analysis=st.session_state.brief_analysis,
                        clip_info=clip_meta,
                        transcript_text=spoken_now,
                        custom_instructions=st.session_state.caption_instructions,
                        clip_index=clip_num_global - 1,
                    )
                    rec = {
                        "name": f"{title} ({asp_v})",
                        "path": str(out_file),
                        "srt_path": saved_srt,
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
                    st.session_state.auto_candidates = build_auto_clip_candidates(
                        brief=st.session_state.brief,
                        transcript=st.session_state.transcript,
                        contact_sheet=None,
                        duration=dur,
                        target_platform=st.session_state.target_platform_choice,
                        clip_count=active_count,
                        desired_minutes=active_minutes,
                        used_windows=st.session_state.used_clip_windows,
                    )
                    st.success(f"Auto-clipped {len(rendered_now)} distinct segment(s) at {active_minutes:.2f} min each.")
            except Exception as exc:
                st.error(f"Auto-clip render error: {exc}")

# ----------------------------- PAGE 2: MANUAL CLIPPER (START -> END) --
elif page == "Manual Clipper (Start → End)":
    st.subheader("Manual Clipper — Clip From One Timestamp/Length to Another")
    if not st.session_state.video_info and not st.session_state.broken_parts:
        st.info("Upload and load a video in 'Campaign & Auto-Clipper' first.")
    else:
        part_sources: list[tuple[str, str, float, int, int]] = []
        for bp in st.session_state.broken_parts:
            if Path(bp["path"]).exists():
                bp_info = bp.get("info") or {}
                part_sources.append((
                    f"{bp['label']} ({fmt_time(bp_info.get('duration', 0))} · {round((bp_info.get('size') or 0)/(1024*1024), 1)} MB)",
                    bp["path"],
                    max(1.0, float(bp_info.get("duration") or 1.0)),
                    int(bp_info.get("width") or 1920),
                    int(bp_info.get("height") or 1080),
                ))
        if st.session_state.source_video and Path(st.session_state.source_video).exists() and st.session_state.video_info:
            vi_ed = st.session_state.video_info
            part_sources.append((
                f"Full Video: {st.session_state.video_name or 'Source'} ({fmt_time(vi_ed.get('duration', 0))})",
                st.session_state.source_video,
                max(1.0, float(vi_ed.get("duration") or 1.0)),
                int(vi_ed.get("width") or 1920),
                int(vi_ed.get("height") or 1080),
            ))

        sel_src_idx = st.selectbox(
            "Choose broken part or full video to manually clip",
            options=list(range(len(part_sources))),
            format_func=lambda i: part_sources[i][0],
            key="ed_source_choice",
        )
        src_label, active_src_path, dur_ed, src_w_ed, src_h_ed = part_sources[int(sel_src_idx)]

        a, b = st.columns(2)
        with a:
            start = st.number_input("Start timestamp (seconds)", min_value=0.0, max_value=max(0.5, dur_ed - 0.5), value=0.0, step=0.5, key="ed_start_sec")
            end = st.number_input("End timestamp (seconds)", min_value=0.5, max_value=dur_ed, value=min(30.0, dur_ed), step=0.5, key="ed_end_sec")
            st.caption(f"Manual span: **{fmt_time(start)} → {fmt_time(end)}** (**{max(0.0, round(float(end) - float(start), 1)):.1f}s**)")
            clip_name = st.text_input("Clip name", value=safe_name(Path(st.session_state.video_name or "clip").stem + "_manual"), key="ed_clip_name")
            spoken_ed = transcript_slice(st.session_state.transcript, float(start), float(end))
            hook_line = st.text_input("Top hook banner text", value=pick_good_hook(spoken_ed, brief=st.session_state.brief, clip_index=0), key="ed_hook_line")
            hook_dur_opt = st.selectbox("Top hook duration", ["8s Banner", "Full Clip Banner"], key="ed_hook_dur")
        with b:
            aspect = st.selectbox("Aspect ratio", ["9:16 Vertical", "16:9 Landscape", "1:1 Square", "4:5 Portrait", "Keep original"], key="ed_aspect")
            resolution = st.selectbox("Output Resolution", ["1080p Full HD", "720p HD", "4K Ultra Sharp", "Source or Better (Auto Sharp)", "Match Source (Zero Resize Loss)"], key="ed_resolution")
            crf = st.slider("Quality CRF (15 = crispest)", 15, 22, 18, key="ed_crf")
            add_logo = st.checkbox("Include saved brand logo", value=bool(st.session_state.logo_path), key="ed_add_logo")
            add_sub = st.checkbox("Burn bottom subtitles + export .SRT", value=True, key="ed_add_sub")

        if st.button("Render Manual Clip (Start → End)", type="primary", use_container_width=True, key="ed_render_btn"):
            if end <= start:
                st.error("End timestamp must be greater than start timestamp.")
            elif ffmpeg_ready():
                try:
                    clip_idx = len(st.session_state.clips) + 1
                    base_stem = f"{safe_name(clip_name)}_manual{clip_idx}"
                    out = WORK_DIR / f"{base_stem}.mp4"
                    srt_out = WORK_DIR / f"{base_stem}.srt"
                    spoken_now = transcript_slice(st.session_state.transcript, start, end)
                    final_hook = (hook_line or "").strip() or pick_good_hook(spoken_now, brief=st.session_state.brief, clip_index=clip_idx - 1)
                    wrap_chars = 26 if aspect in {"9:16 Vertical", "4:5 Portrait", "1:1 Square"} else 38
                    cues = []
                    if add_sub:
                        cues = build_caption_cues(
                            transcript=st.session_state.transcript,
                            start=start,
                            end=end,
                            fallback_text=spoken_now,
                            max_chars=wrap_chars,
                        )
                    with st.spinner(f"Rendering manual clip ({start:.1f}s → {end:.1f}s) from {src_label}..."):
                        saved_srt = render_video(
                            active_src_path, str(out), start, end,
                            aspect, resolution, crf,
                            logo=st.session_state.logo_path if add_logo else None,
                            cues=cues if cues else None,
                            hook_banner_text=final_hook,
                            hook_banner_duration=8.0 if hook_dur_opt.startswith("8s") else 9999.0,
                            src_w=src_w_ed,
                            src_h=src_h_ed,
                            caption_font_size=int(st.session_state.onscreen_caption_size),
                            normalize_audio=False,
                            clip_index=clip_idx - 1,
                            srt_output_path=str(srt_out),
                        )
                    st.session_state.used_clip_windows.append([round(start, 1), round(end, 1)])
                    c_meta = {
                        "mode": "Manual Clip",
                        "title": clip_name,
                        "source": src_label,
                        "start": start,
                        "end": end,
                        "aspect": aspect,
                        "hook": final_hook,
                        "transcript_excerpt": spoken_now,
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
                        "name": f"{clip_name} ({aspect})", "path": str(out), "srt_path": saved_srt,
                        "start": start, "end": end, "aspect": aspect, "resolution": resolution,
                        "meta": c_meta, "caption": pack["caption"], "description": pack["description"],
                    })
                    st.success("Manual clip rendered.")
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
        st.success("OPENAI_API_KEY active.")
    else:
        st.info("No OPENAI_API_KEY detected — local campaign/caption fallback mode active.")

# ----------------------------- SHARED GENERATED CLIPS SECTION --------
if st.session_state.clips:
    st.divider()
    st.subheader("Generated Clips (Auto & Manual) + Per-Clip Campaign Captions")
    st.text_area(
        "Optional extra caption/CTA note for clips",
        key="caption_instructions",
        height=75,
    )

    act_col1, act_col2 = st.columns([1.4, 1])
    with act_col1:
        if st.button("✨ Generate Unique Campaign Captions for All Clips", key="gen_all_short_captions_btn", use_container_width=True):
            with st.spinner("Generating tailored campaign captions for each clip..."):
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
            st.success("Updated unique campaign captions for every clip.")
    with act_col2:
        short_zip_items = []
        for i, cl in enumerate(st.session_state.clips):
            if Path(cl["path"]).exists():
                short_zip_items.append((f"clip_{i+1}_{safe_name(cl['name'])}.mp4", cl["path"]))
            if cl.get("srt_path") and Path(cl["srt_path"]).exists():
                short_zip_items.append((f"clip_{i+1}_{safe_name(cl['name'])}.srt", cl["srt_path"]))
        if short_zip_items:
            st.download_button(
                "📦 Download All Clips & SRTs (.ZIP)",
                build_zip_bytes(short_zip_items),
                file_name=f"{safe_name(st.session_state.project_name)}_clips_srt.zip",
                mime="application/zip",
                key="wf_dl_all_shorts_zip",
                use_container_width=True,
            )

    for idx, cl in enumerate(st.session_state.clips):
        with st.container(border=True):
            c_vid, c_cap = st.columns([1.25, 1.35], gap="large")
            actual_sec = round(float(cl["end"]) - float(cl["start"]), 1)
            clip_mode = (cl.get("meta") or {}).get("mode") or "Clip"
            with c_vid:
                st.markdown(f"**Clip {idx + 1} [{clip_mode}]: {cl['name']}**")
                st.caption(f"{fmt_time(cl['start'])} → {fmt_time(cl['end'])} ({actual_sec:.1f}s) · {cl['aspect']} · {cl['resolution']}")
                if Path(cl["path"]).exists():
                    st.video(cl["path"])
                    dl_c1, dl_c2 = st.columns(2)
                    with dl_c1:
                        with open(cl["path"], "rb") as fh:
                            st.download_button(
                                f"⬇ Clip {idx+1} (.MP4)",
                                fh,
                                file_name=Path(cl["path"]).name,
                                mime="video/mp4",
                                key=f"wf_dl_clip_{idx}",
                                use_container_width=True,
                            )
                    with dl_c2:
                        if cl.get("srt_path") and Path(cl["srt_path"]).exists():
                            with open(cl["srt_path"], "rb") as sf:
                                st.download_button(
                                    f"📄 Subtitles {idx+1} (.SRT)",
                                    sf,
                                    file_name=Path(cl["srt_path"]).name,
                                    mime="application/x-subrip",
                                    key=f"wf_dl_srt_{idx}",
                                    use_container_width=True,
                                )
            with c_cap:
                st.markdown("**Caption & Description for This Clip**")
                if st.button(
                    f"✨ Get Caption for Clip {idx+1} (Campaign-Matched)",
                    type="primary",
                    use_container_width=True,
                    key=f"get_cap_short_{idx}",
                ):
                    c_meta = cl.get("meta") or {"title": cl["name"], "start": cl["start"], "end": cl["end"]}
                    t_seg = c_meta.get("transcript_excerpt") or transcript_slice(st.session_state.transcript, cl["start"], cl["end"])
                    with st.spinner(f"Writing campaign-matched caption for Clip {idx+1}..."):
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

                edited_cap = st.text_area(
                    f"Clip {idx+1} Caption",
                    value=cl.get("caption", ""),
                    height=110,
                    key=f"short_cap_area_{idx}",
                )
                edited_desc = st.text_area(
                    f"Clip {idx+1} Description",
                    value=cl.get("description", ""),
                    height=135,
                    key=f"short_desc_area_{idx}",
                )
                st.session_state.clips[idx]["caption"] = edited_cap
                st.session_state.clips[idx]["description"] = edited_desc

                render_fast_voice_reader(
                    f"{edited_cap}\n\n{edited_desc}".strip(),
                    label=f"🔊 Fast Read Clip {idx+1} Copy",
                    uid=f"short_{idx}",
                )