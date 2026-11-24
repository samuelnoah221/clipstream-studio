from __future__ import annotations

import gc
import hashlib
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
WORKSPACES_ROOT = WORK_DIR / "workspaces"
WORKSPACES_ROOT.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title=APP_NAME, page_icon="🎬", layout="wide")

# ----------------------------- UI & THEME -------------------------------
st.markdown("""
<style>
:root{color-scheme:dark}
.stApp{background:#090c13;color:#f4f5fa}
[data-testid="stHeader"]{background:rgba(9,12,19,.92)}
.block-container{max-width:1550px;padding-top:1.1rem;padding-bottom:3rem}
[data-testid="stMetric"]{background:#151b29;border:1px solid #2b354b;padding:13px;border-radius:14px}
[data-testid="stVerticalBlockBorderWrapper"]{background:#111827;border:1px solid #293449;border-radius:15px}
.stButton>button,.stDownloadButton>button{border-radius:10px;font-weight:650;min-height:2.45rem}
[data-testid="stSidebar"]{background:#0f1623}
.hero{padding:22px;border:1px solid #34405a;border-radius:20px;background:linear-gradient(115deg,#211d3b,#141c2d 58%,#30203b);margin-bottom:1.1rem}
.hero h1{margin:0;letter-spacing:-1.2px;font-size:2.1rem}
.muted{color:#a5b0c4;font-size:.9rem}
.pill{display:inline-block;border:1px solid #46516d;color:#c7d2fe;border-radius:30px;padding:4px 10px;font-size:.78rem;margin:4px 5px 0 0}
</style>
""", unsafe_allow_html=True)

CAMPAIGN_HOOK_BANK = [
    "His first hit said everything",
    "Zero hesitation—straight to it",
    "Watch his reaction right now",
    "Bro did NOT expect that hit",
    "Being high on GTA hits different",
    "Bro locked in on a new level",
    "The high just kicked in on GTA",
    "GTA moments hit harder like this",
    "One whiff and chat went crazy",
    "Opening the jar is priceless",
    "Smelling jars before GTA chaos",
    "Look at the cloud off that hit",
    "How is that vapor still going?!",
    "Chat went wild on that blow",
    "Ben and chat hyped off that",
    "Hash Butler dropping real game",
    "Puffco + GTA undefeated combo",
    "Best banter with Hash Butler",
    "GTA chaos meets a big rip",
]

FIT_MODES = [
    "Fit All (Full Frame + Ambient Blur)",
    "Fit All (Pure Full Frame)",
    "Fill & Crop (9:16 Zoom)",
    "Letterbox Fit",
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
    if any(k in p for k in ["shorts", "reels", "tiktok", "vertical", "ig", "instagram", "youtube", "facebook"]):
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


# ----------------------------- WORKSPACE & IP / ACCOUNT SAVE ---------
def detect_client_ip() -> str:
    """Best-effort client IP from Streamlit headers with demo fallback."""
    try:
        headers = getattr(st.context, "headers", None)
        if headers:
            for h_name in ("x-forwarded-for", "x-real-ip", "cf-connecting-ip", "remote-addr"):
                raw_val = headers.get(h_name)
                if raw_val:
                    first_ip = str(raw_val).split(",")[0].strip()
                    if first_ip:
                        return first_ip
    except Exception:
        pass
    return "local-demo-ip"


def compute_workspace_key(account_handle: str, client_ip: str) -> str:
    clean_acct = re.sub(r"[^a-zA-Z0-9@._-]+", "_", (account_handle or "").strip().lower()).strip("_")
    clean_ip = re.sub(r"[^a-zA-Z0-9._:-]+", "_", (client_ip or "demo").strip())
    seed = clean_acct if clean_acct else f"ip_{clean_ip}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    prefix = safe_name(clean_acct or clean_ip)[:24]
    return f"{prefix}_{digest}"


def workspace_dir(ws_key: str) -> Path:
    d = WORKSPACES_ROOT / safe_name(ws_key)
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_saved_workspaces() -> list[dict]:
    items: list[dict] = []
    if not WORKSPACES_ROOT.exists():
        return items
    for sub in sorted(WORKSPACES_ROOT.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if not sub.is_dir():
            continue
        state_file = sub / "workspace_state.json"
        label = sub.name
        saved_at = ""
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                proj = (data.get("project_name") or "").strip()
                acct = (data.get("account_handle") or "").strip()
                saved_at = data.get("saved_at") or ""
                bits = [b for b in [proj, acct, sub.name[:16]] if b]
                label = " · ".join(bits)
            except Exception:
                pass
        size_mb = round(sum(f.stat().st_size for f in sub.rglob("*") if f.is_file()) / (1024 * 1024), 1)
        items.append({"key": sub.name, "path": sub, "label": label, "saved_at": saved_at, "size_mb": size_mb})
    return items


def clear_workspace_data(ws_key: str, reset_session: bool = True) -> None:
    wdir = workspace_dir(ws_key)
    shutil.rmtree(wdir, ignore_errors=True)
    wdir.mkdir(parents=True, exist_ok=True)
    gc.collect()
    if reset_session:
        st.session_state["brief"] = ""
        st.session_state["brief_analysis"] = None
        st.session_state["campaign_feedback"] = None
        st.session_state["go_ahead_clips"] = False
        st.session_state["used_clip_windows"] = []
        st.session_state["source_video"] = None
        st.session_state["video_name"] = None
        st.session_state["video_info"] = None
        st.session_state["broken_parts"] = []
        st.session_state["transcript"] = None
        st.session_state["auto_candidates"] = []
        st.session_state["clips"] = []
        st.session_state["logo_path"] = None
        st.session_state["logo_name"] = None
        st.session_state["voiceover_audio_path"] = None
        st.session_state["voiceover_audio_name"] = None
        st.session_state["project_name"] = "My campaign"
        st.session_state["brand_name"] = ""
        st.session_state["caption_instructions"] = ""
        st.session_state["last_saved_at"] = f"Cleared ({time.strftime('%H:%M:%S')})"


def save_workspace_state(ws_key: str | None = None) -> Path:
    key = ws_key or st.session_state.get("workspace_key") or compute_workspace_key(
        st.session_state.get("account_handle", ""),
        st.session_state.get("detected_ip", "local-demo-ip"),
    )
    wdir = workspace_dir(key)
    state_file = wdir / "workspace_state.json"

    fit_val = st.session_state.get("vertical_fit_mode", "Fit All (Full Frame + Ambient Blur)")
    if fit_val not in FIT_MODES:
        fit_val = "Fit All (Full Frame + Ambient Blur)"

    payload = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "workspace_key": key,
        "account_handle": st.session_state.get("account_handle", ""),
        "detected_ip": st.session_state.get("detected_ip", "local-demo-ip"),
        "project_name": st.session_state.get("project_name", "My campaign"),
        "brief": st.session_state.get("brief", ""),
        "brief_analysis": st.session_state.get("brief_analysis"),
        "campaign_feedback": st.session_state.get("campaign_feedback"),
        "go_ahead_clips": bool(st.session_state.get("go_ahead_clips", False)),
        "target_platform_choice": st.session_state.get("target_platform_choice", "YouTube + IG + Facebook"),
        "auto_clip_count": int(st.session_state.get("auto_clip_count", 2)),
        "desired_clip_minutes": float(st.session_state.get("desired_clip_minutes", 0.50)),
        "auto_clip_minutes": float(st.session_state.get("auto_clip_minutes", 0.50)),
        "used_clip_windows": st.session_state.get("used_clip_windows", []),
        "hook_banner_scope": st.session_state.get("hook_banner_scope", "8-Second Top Banner (0–8s)"),
        "burn_subtitles_chk_v2": bool(st.session_state.get("burn_subtitles_chk_v2", False)),
        "subtitle_language": st.session_state.get("subtitle_language", "English (Default)"),
        "onscreen_caption_size": int(st.session_state.get("onscreen_caption_size", 28)),
        "vertical_fit_mode": fit_val,
        "mute_original_audio": bool(st.session_state.get("mute_original_audio", False)),
        "voiceover_source_mode": st.session_state.get("voiceover_source_mode", "Uploaded Voice Track"),
        "voiceover_script_text": st.session_state.get("voiceover_script_text", ""),
        "voiceover_audio_path": st.session_state.get("voiceover_audio_path"),
        "voiceover_audio_name": st.session_state.get("voiceover_audio_name"),
        "source_video": st.session_state.get("source_video"),
        "video_name": st.session_state.get("video_name"),
        "video_info": st.session_state.get("video_info"),
        "broken_parts": st.session_state.get("broken_parts", []),
        "transcript": st.session_state.get("transcript"),
        "auto_candidates": st.session_state.get("auto_candidates", []),
        "clips": list(st.session_state.get("clips") or []),
        "logo_path": st.session_state.get("logo_path"),
        "logo_name": st.session_state.get("logo_name"),
        "brand_name": st.session_state.get("brand_name", ""),
        "brand_colour": st.session_state.get("brand_colour", "#FFFFFF"),
        "caption_instructions": st.session_state.get("caption_instructions", ""),
    }
    state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return state_file


def load_workspace_state(ws_key: str) -> bool:
    state_file = workspace_dir(ws_key) / "workspace_state.json"
    if not state_file.exists():
        return False
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return False

    for field in [
        "project_name", "brief", "brief_analysis", "campaign_feedback",
        "go_ahead_clips", "target_platform_choice", "desired_clip_minutes",
        "auto_clip_minutes", "used_clip_windows", "hook_banner_scope",
        "burn_subtitles_chk_v2", "subtitle_language", "onscreen_caption_size",
        "vertical_fit_mode", "mute_original_audio", "voiceover_source_mode",
        "voiceover_script_text", "voiceover_audio_name",
        "video_name", "video_info", "broken_parts", "transcript",
        "auto_candidates", "logo_name", "brand_name", "brand_colour",
        "caption_instructions",
    ]:
        if field in data and data[field] is not None:
            st.session_state[field] = data[field]

    if st.session_state.get("vertical_fit_mode") not in FIT_MODES:
        st.session_state["vertical_fit_mode"] = "Fit All (Full Frame + Ambient Blur)"

    cnt = max(1, min(6, int(data.get("auto_clip_count", 2))))
    st.session_state["auto_clip_count"] = cnt
    st.session_state["auto_clip_count_top"] = cnt
    st.session_state["auto_clip_count_step3"] = cnt

    src_v = data.get("source_video")
    st.session_state["source_video"] = src_v if (src_v and Path(src_v).exists()) else None

    vo_p = data.get("voiceover_audio_path")
    st.session_state["voiceover_audio_path"] = vo_p if (vo_p and Path(vo_p).exists()) else None

    logo_p = data.get("logo_path")
    st.session_state["logo_path"] = logo_p if (logo_p and Path(logo_p).exists()) else None

    valid_clips = []
    for cl in data.get("clips") or []:
        p = cl.get("path")
        if p and Path(p).exists():
            valid_clips.append(cl)
    st.session_state["clips"] = valid_clips
    st.session_state["last_saved_at"] = data.get("saved_at", "Restored")
    return True


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
    ws_dir = workspace_dir(st.session_state.get("workspace_key", "default"))
    ext = suffix or Path(uploaded.name).suffix.lower() or ".bin"
    path = ws_dir / f"{next(tempfile._get_candidate_names())}_{int(time.time())}{ext}"
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

    ws_dir = workspace_dir(st.session_state.get("workspace_key", "default"))
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

        out_p = ws_dir / f"{stem}_part{i+1}of{n}_{next(tempfile._get_candidate_names())}{ext}"
        cmd = [
            "ffmpeg", "-y", "-ss", str(start_s), "-i", str(src_path),
            "-t", str(chunk_dur), "-c", "copy", "-avoid_negative_ts", "make_zero",
            str(out_p),
        ]
        res = run_cmd(cmd, timeout=300)

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
                "-t", str(chunk_dur), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(out_p),
            ], timeout=600)
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


def synthesize_voiceover_file(script_text: str) -> Path | None:
    """Generates a voiceover audio file inside the active workspace if a voice engine or API is available."""
    clean_txt = re.sub(r"\s+", " ", (script_text or "").strip())
    if not clean_txt:
        return None
    ws_dir = workspace_dir(st.session_state.get("workspace_key", "default"))
    out_wav = ws_dir / f"vo_{next(tempfile._get_candidate_names())}.wav"

    client = get_openai_client()
    if client is not None:
        try:
            speech_resp = client.audio.speech.create(
                model="gpt-4o-mini-tts",
                voice="alloy",
                input=clean_txt[:2000],
            )
            raw_bytes = speech_resp.read() if hasattr(speech_resp, "read") else bytes(speech_resp)
            if raw_bytes:
                tmp_mp3 = ws_dir / f"vo_raw_{next(tempfile._get_candidate_names())}.mp3"
                tmp_mp3.write_bytes(raw_bytes)
                conv = run_cmd([
                    "ffmpeg", "-y", "-i", str(tmp_mp3),
                    "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(out_wav),
                ], timeout=120)
                tmp_mp3.unlink(missing_ok=True)
                if conv.returncode == 0 and out_wav.exists() and out_wav.stat().st_size > 0:
                    return out_wav
        except Exception:
            pass

    if shutil.which("espeak"):
        p = run_cmd(["espeak", "-v", "en", "-w", str(out_wav), clean_txt[:1500]], timeout=60)
        if p.returncode == 0 and out_wav.exists() and out_wav.stat().st_size > 0:
            return out_wav

    return None


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
            u.lang = 'en-US';
            u.rate = 1.08;
            u.onstart = () => stEl_{safe_uid}.textContent = 'Reading English voiceover...';
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
            "Hash Butler dropping real game",
            "Best banter with Hash Butler",
            "GTA chaos meets a big rip",
        ]
        return butler_hooks[clip_index % len(butler_hooks)]

    if any(w in low_all for w in ["puffco", "proxy", "pivot", "dab", "cloud", "vapor", "hit"]):
        puffco_hooks = [
            "His first hit said everything",
            "Look at the cloud off that hit",
            "Zero hesitation—straight to it",
            "How is that vapor still going?!",
        ]
        return puffco_hooks[clip_index % len(puffco_hooks)]

    if any(w in low_all for w in ["jar", "smell", "hash", "alien", "tech"]):
        jar_hooks = [
            "One whiff and chat went crazy",
            "Opening the jar is priceless",
            "Bro locked in on a new level",
        ]
        return jar_hooks[clip_index % len(jar_hooks)]

    if any(w in low_all for w in ["gta", "game", "high"]):
        gta_hooks = [
            "Being high on GTA hits different",
            "Bro locked in on a new level",
            "The high just kicked in on GTA",
            "GTA moments hit harder like this",
        ]
        return gta_hooks[clip_index % len(gta_hooks)]

    clean_speech = re.sub(r"\s+", " ", (spoken_text or "").strip())
    if clean_speech and len(clean_speech.split()) >= 3:
        wds = clean_speech.split()[:5]
        return (" ".join(wds) + ("..." if len(clean_speech.split()) > 5 else ""))[:44]

    return CAMPAIGN_HOOK_BANK[clip_index % len(CAMPAIGN_HOOK_BANK)]


def build_stitched_peak_moments(
    duration: float,
    desired_minutes: float,
    used_windows: list[list[float]],
    transcript: dict | None = None,
    seed_offset: int = 0,
) -> list[dict]:
    """Picks 3-4 short peak sub-moments across the video whose durations sum to desired_minutes."""
    total_target = round(max(1.0, min(duration, minutes_to_seconds(desired_minutes))), 1)
    if duration <= 1.0 or total_target <= 0:
        end_s = round(max(0.5, duration), 1)
        return [{"start": 0.0, "end": end_s, "text": transcript_slice(transcript, 0.0, end_s)}]

    num_parts = 3 if total_target <= 30.0 else 4
    raw_weights = [1.3, 0.85, 0.95, 1.1][:num_parts]
    w_sum = sum(raw_weights)
    part_durs = [max(2.0, round((w / w_sum) * total_target, 1)) for w in raw_weights]
    diff = round(total_target - sum(part_durs), 1)
    part_durs[-1] = round(max(1.5, part_durs[-1] + diff), 1)

    max_start_for_each = [max(0.0, duration - d) for d in part_durs]
    rng = random.Random(int(time.time() * 100) + seed_offset * 37)
    base_fracs = [0.12, 0.36, 0.62, 0.84][:num_parts]
    rng.shuffle(base_fracs)

    moments: list[dict] = []
    combined_used = list(used_windows)

    for idx, p_dur in enumerate(part_durs):
        max_s = max_start_for_each[idx]
        if max_s <= 0.0:
            s_val = 0.0
            e_val = round(min(duration, p_dur), 1)
            moments.append({"start": s_val, "end": e_val, "text": transcript_slice(transcript, s_val, e_val)})
            continue

        candidate_starts = [
            round(max(0.0, min(max_s, base_fracs[idx] * max_s)), 1),
            round(max(0.0, min(max_s, ((idx + 0.35 + (seed_offset * 0.19)) % 0.96) * max_s)), 1),
        ]
        for _ in range(6):
            candidate_starts.append(round(rng.uniform(0.0, max_s), 1))

        chosen_s = None
        for cs in candidate_starts:
            ce = round(min(duration, cs + p_dur), 1)
            if ce <= cs:
                continue
            already_in_clip = any(cs < (m["end"] + 2.0) and ce > (m["start"] - 2.0) for m in moments)
            if not already_in_clip and not overlaps_used(cs, ce, combined_used, min_gap=1.5):
                chosen_s = cs
                break

        if chosen_s is None:
            chosen_s = round(max(0.0, min(max_s, (idx / max(1, num_parts)) * max_s)), 1)

        chosen_e = round(min(duration, chosen_s + p_dur), 1)
        if chosen_e <= chosen_s:
            chosen_e = round(min(duration, chosen_s + 1.0), 1)

        moments.append({
            "start": chosen_s,
            "end": chosen_e,
            "text": transcript_slice(transcript, chosen_s, chosen_e),
        })
        combined_used.append([chosen_s, chosen_e])

    return moments


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
    """Builds stitched multi-moment auto clip candidates (each clip joins multiple peak spots)."""
    count = max(1, min(6, int(clip_count)))
    results: list[dict] = []
    combined_used = list(used_windows)

    for idx in range(count):
        pick_num = len(used_windows) + idx + 1
        moments = build_stitched_peak_moments(
            duration=duration,
            desired_minutes=desired_minutes,
            used_windows=combined_used,
            transcript=transcript,
            seed_offset=pick_num,
        )
        for m in moments:
            combined_used.append([m["start"], m["end"]])

        stitched_dur = round(sum(max(0.1, m["end"] - m["start"]) for m in moments), 1)
        all_spoken = " ".join(m["text"] for m in moments if m.get("text")).strip()
        span_summary = ", ".join(f"{m['start']:.0f}s–{m['end']:.0f}s" for m in moments)
        good_hook = pick_good_hook(all_spoken, brief=brief, clip_index=pick_num - 1)

        results.append({
            "title": f"Peak Stitch #{pick_num} ({len(moments)} moments)",
            "moments": moments,
            "start": moments[0]["start"] if moments else 0.0,
            "end": moments[-1]["end"] if moments else stitched_dur,
            "stitched_duration": stitched_dur,
            "mode": "Auto Peak Stitch",
            "target_platform": target_platform,
            "reason": all_spoken[:160] if all_spoken else f"Stitched peak spots: {span_summary} ({stitched_dur:.1f}s total).",
            "campaign_match": f"Stitched {len(moments)} peak moments ({stitched_dur:.1f}s)",
            "hook": str(good_hook)[:44],
            "risk": "",
            "confidence": "high",
        })

    if contact_sheet is not None and contact_sheet.exists():
        contact_sheet.unlink(missing_ok=True)

    return results


# ----------------------------- CAMPAIGN & CLIP INTELLIGENCE -------------
def heuristic_campaign_analysis(brief: str, project_name: str) -> dict:
    low = brief.lower()
    platforms = []
    for p_label, keys in [
        ("YouTube", ["youtube", "yt", "shorts"]),
        ("Instagram", ["instagram", "ig", "reels"]),
        ("Facebook", ["facebook", "fb"]),
    ]:
        if any(k in low for k in keys):
            platforms.append(p_label)
    if not platforms:
        platforms = ["YouTube", "Instagram", "Facebook"]

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
            f"Campaign across {', '.join(platforms)} ({len(lines)} brief line(s)). "
            f"Full-frame 9:16 fit + multi-moment peak stitching + optional Section 5 English burn-in subtitles + Stage 6 voiceover ready."
        ),
        "deliverables": ["Multi-moment peak stitch clips with optional English subtitles & voiceover for YouTube, IG & Facebook"],
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
            "Confirm full video fits inside 9:16 cleanly with non-blocking English subtitles & hook",
            "Verify peak moments are stitched cleanly into one clip",
            "Check YouTube, Instagram, and Facebook captions/tags",
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
        strengths.append(f"Video duration is {dur:.1f}s ({fmt_time(dur)}) at {w}x{h} — clean 9:16 fit + multi-moment stitch ready.")
    if analysis.get("caption_requirements") or analysis.get("hashtags"):
        strengths.append("Campaign caption/tag requirements detected—YouTube, IG & Facebook copy will incorporate them.")
    if analysis.get("requires_8s_hook"):
        strengths.append("8-second hook banner rule detected.")
    elif analysis.get("requires_video_hook"):
        strengths.append("Video hook banner framing ready.")

    must_items = analysis.get("must_include", []) or []
    req_scenes = analysis.get("required_scenes_or_moments", []) or []
    t_text = ((transcript or {}).get("text") or "").lower()

    if transcript and t_text.strip():
        strengths.append(f"Speech transcript captured ({len(t_text.split())} words) for optional Section 5 English burn-in subtitles & .SRT.")
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
        gaps.append("No speech transcript yet—add OPENAI_API_KEY in Streamlit Secrets/env for automatic speech transcription.")

    return {
        "ready": True,
        "verdict": "Ready for Go-Ahead — 9:16 Fit, Optional English Subtitles & Stage 6 Voiceover Ready",
        "strengths": strengths,
        "gaps": gaps,
        "warnings": warnings,
    }


def extract_audio(video_path: str) -> Path:
    ws_dir = workspace_dir(st.session_state.get("workspace_key", "default"))
    audio_path = ws_dir / f"audio_{next(tempfile._get_candidate_names())}.mp3"
    p = run_cmd([
        "ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio_path),
    ], timeout=600)
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
    ws_dir = workspace_dir(st.session_state.get("workspace_key", "default"))
    count = max(1, min(max_frames, int(duration // 20) + 1))
    interval = max(1.0, duration / count)
    paths: list[tuple[float, Path]] = []
    try:
        for i in range(count):
            ts = min(max(0.0, duration - 0.1), i * interval)
            frame = ws_dir / f"frame_{next(tempfile._get_candidate_names())}.jpg"
            p = run_cmd([
                "ffmpeg", "-y", "-ss", str(ts), "-i", video_path,
                "-frames:v", "1", "-vf", "scale=240:-1", "-q:v", "6", str(frame),
            ], timeout=60)
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
        out = ws_dir / f"contact_{next(tempfile._get_candidate_names())}.jpg"
        sheet.save(out, quality=80)
        return out
    finally:
        for _, p in paths:
            p.unlink(missing_ok=True)
        gc.collect()


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


def generate_multi_platform_copy(
    brief: str,
    analysis: dict | None,
    clip_info: dict | None,
    transcript_text: str,
    custom_instructions: str = "",
    clip_index: int = 0,
) -> dict:
    """Generates tailored copy for YouTube, Instagram, and Facebook for a single clip."""
    client = get_openai_client()
    tags = (analysis or {}).get("hashtags") or []
    clean_tags = " ".join(t if t.startswith("#") else f"#{t.replace(' ', '')}" for t in tags[:6])
    if not clean_tags:
        clean_tags = f"#Shorts #Reels #Campaign #Clip{clip_index + 1}"

    clip_title = (clip_info or {}).get("title") or f"Clip #{clip_index + 1}"
    clip_hook = (clip_info or {}).get("hook") or ""
    moments = (clip_info or {}).get("moments") or []
    if moments:
        window_label = " (" + " + ".join(f"{float(m['start']):.0f}s–{float(m['end']):.0f}s" for m in moments) + ")"
    else:
        clip_start = (clip_info or {}).get("start")
        clip_end = (clip_info or {}).get("end")
        window_label = f" ({float(clip_start):.0f}s–{float(clip_end):.0f}s)" if clip_start is not None and clip_end is not None else ""

    req_note = custom_instructions.strip()
    camp_ctx = build_campaign_caption_context(analysis, brief)
    camp_reqs_list = (analysis or {}).get("caption_requirements") or (analysis or {}).get("must_include") or []
    camp_req_line = f"Campaign rules: {'; '.join(str(x) for x in camp_reqs_list[:4])}." if camp_reqs_list else ""
    moment_line = transcript_text[:150].strip() if transcript_text.strip() else f"Peak highlights #{clip_index + 1}"

    if not client:
        yt_title = f"{clip_hook or clip_title}{window_label} #Shorts"[:95]
        yt_desc = (
            f"{clip_hook or clip_title}{window_label}\n\n"
            f"Highlights: {moment_line}\n"
            f"{camp_req_line}\n"
            f"{'Note: ' + req_note + chr(10) if req_note else ''}\n"
            f"{clean_tags}"
        ).strip()

        ig_caption = (
            f"{clip_hook or clip_title}\n"
            f"{moment_line}\n"
            f"{camp_req_line}\n"
            f"{'Note: ' + req_note + chr(10) if req_note else ''}\n"
            f"{clean_tags}"
        ).strip()

        fb_post = (
            f"{clip_hook or clip_title}{window_label} — {moment_line}\n\n"
            f"{camp_req_line}\n"
            f"{'Note: ' + req_note + chr(10) if req_note else ''}\n"
            f"{clean_tags}"
        ).strip()

        return {
            "youtube": f"{yt_title}\n\n{yt_desc}",
            "instagram": ig_caption,
            "facebook": fb_post,
            "caption": ig_caption,
            "description": yt_desc,
        }

    prompt = f"""You are writing tailored social copy for ONE video clip (Clip #{clip_index + 1}) across THREE platforms:
1) YouTube (Shorts/Video title + description with searchable keywords & hashtags)
2) Instagram (Reels/Feed caption with hook, engagement line, and hashtags)
3) Facebook (Reels/Post caption with conversational framing & hashtags)

THIS CLIP DETAILS:
{json.dumps(clip_info or {}, ensure_ascii=False)}
THIS CLIP'S TRANSCRIPT SEGMENTS:
{transcript_text[:1800]}

CAMPAIGN BRIEF & CAPTION REQUIREMENTS:
{camp_ctx[:3500]}

EXTRA USER INSTRUCTIONS:
{req_note or "None"}

Return ONLY valid JSON:
{{
  "youtube": "YouTube title + description ready to paste",
  "instagram": "Instagram caption ready to paste",
  "facebook": "Facebook post/reel caption ready to paste"
}}
"""
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.55,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "You write distinct, platform-native social copy for YouTube, Instagram, and Facebook."},
            {"role": "user", "content": prompt},
        ],
    )
    parsed = parse_json_response(resp.choices[0].message.content or "{}")
    yt = str(parsed.get("youtube", "")).strip()
    ig = str(parsed.get("instagram", "")).strip()
    fb = str(parsed.get("facebook", "")).strip()
    return {
        "youtube": yt,
        "instagram": ig,
        "facebook": fb,
        "caption": ig or yt,
        "description": yt or fb,
    }


def normalize_english_subtitle_text(text: str) -> str:
    """Cleans spoken or scripted text into crisp, readable English subtitle text."""
    cleaned = re.sub(r"[^\x20-\x7E\u2014\u2013\u2018\u2019\u201c\u201d]", " ", str(text or ""))
    cleaned = cleaned.replace("\u2014", " — ").replace("\u2013", " - ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def wrap_text_lines(text: str, max_chars: int = 26, max_lines: int = 2) -> list[str]:
    """Wraps text cleanly into bounded lines so neither hooks nor subtitles overflow frame width."""
    words = normalize_english_subtitle_text(text).split()
    if not words:
        return []
    lines: list[str] = []
    cur = ""
    for w in words:
        piece = w[:max_chars] if len(w) > max_chars else w
        candidate = f"{cur} {piece}".strip() if cur else piece
        if len(candidate) <= max_chars:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
                if len(lines) >= max_lines:
                    return lines[:max_lines]
            cur = piece
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines[:max_lines]


def wrap_caption_lines(text: str, max_chars: int = 26, max_lines: int = 2) -> str:
    return "\n".join(wrap_text_lines(text, max_chars=max_chars, max_lines=max_lines))


def build_caption_cues(
    transcript: dict | None,
    start: float,
    end: float,
    fallback_text: str = "",
    max_chars: int = 26,
) -> list[tuple[float, float, str]]:
    """Builds time-synced English on-screen subtitle cues matching spoken words inside [start, end]."""
    clip_dur = max(0.5, round(end - start, 2))
    cues: list[tuple[float, float, str]] = []

    raw_segs = (transcript or {}).get("segments") or []
    overlapping = []
    for sg in raw_segs:
        a = float(sg.get("start", 0.0) or 0.0)
        b = float(sg.get("end", 0.0) or 0.0)
        txt = normalize_english_subtitle_text(str(sg.get("text", "")))
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
            if len(words) <= 5:
                chunks.append(cl)
            else:
                for i in range(0, len(words), 5):
                    chunks.append(" ".join(words[i:i + 5]))

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
        clean_speech = normalize_english_subtitle_text(fallback_text)
        if clean_speech:
            words = clean_speech.split()
            phrases = [" ".join(words[i:i + 5]) for i in range(0, len(words), 5)]
            n_p = max(1, len(phrases))
            step = clip_dur / n_p
            for i, ph in enumerate(phrases):
                b_start = round(i * step, 2)
                b_end = round(min(clip_dur, (i + 1) * step), 2)
                wrapped = wrap_caption_lines(ph, max_chars=max_chars, max_lines=2)
                if wrapped and b_end > b_start + 0.12:
                    cues.append((b_start, b_end, wrapped))

    return cues[:40]


def build_stitched_caption_cues(
    transcript: dict | None,
    moments: list[dict],
    max_chars: int = 26,
) -> list[tuple[float, float, str]]:
    """Maps spoken English cues across each stitched sub-moment onto the unified concatenated timeline."""
    stitched_cues: list[tuple[float, float, str]] = []
    timeline_offset = 0.0
    for m in moments:
        m_start = float(m["start"])
        m_end = float(m["end"])
        m_dur = max(0.5, round(m_end - m_start, 2))
        local_cues = build_caption_cues(
            transcript=transcript,
            start=m_start,
            end=m_end,
            fallback_text=m.get("text") or "",
            max_chars=max_chars,
        )
        for c_a, c_b, c_txt in local_cues:
            stitched_cues.append((
                round(timeline_offset + c_a, 2),
                round(timeline_offset + min(m_dur, c_b), 2),
                c_txt,
            ))
        timeline_offset = round(timeline_offset + m_dur, 2)
    return stitched_cues[:40]


def write_srt_file(cues: list[tuple[float, float, str]], clip_duration: float, srt_path: Path) -> Path:
    """Writes valid SRT subtitle blocks clamped cleanly inside [0, clip_duration]."""
    blocks: list[str] = []
    idx = 1
    for c_start, c_end, c_text in cues:
        a_sec = max(0.0, min(clip_duration, float(c_start)))
        b_sec = max(a_sec + 0.15, min(clip_duration, float(c_end)))
        clean_lines = "\n".join(
            normalize_english_subtitle_text(ln)
            for ln in (c_text or "").splitlines()
            if normalize_english_subtitle_text(ln)
        )
        if not clean_lines:
            continue
        blocks.append(f"{idx}\n{fmt_srt_time(a_sec)} --> {fmt_srt_time(b_sec)}\n{clean_lines}\n")
        idx += 1

    srt_path.write_text("\n".join(blocks), encoding="utf-8")
    return srt_path


def resolve_target_dimensions(
    aspect: str,
    resolution: str,
    src_w: int,
    src_h: int,
) -> tuple[int, int]:
    src_w = max(2, int(src_w or 1280))
    src_h = max(2, int(src_h or 720))

    if aspect == "9:16 Vertical":
        default_4k = (2160, 3840)
        default_1k = (1080, 1920)
        default_720 = (720, 1280)
    elif aspect == "16:9 Landscape":
        default_4k = (3840, 2160)
        default_1k = (1920, 1080)
        default_720 = (1280, 720)
    elif aspect == "1:1 Square":
        default_4k = (2160, 2160)
        default_1k = (1080, 1080)
        default_720 = (720, 720)
    elif aspect == "4:5 Portrait":
        default_4k = (2160, 2700)
        default_1k = (1080, 1350)
        default_720 = (720, 900)
    else:
        default_4k = (max(src_w, 3840), max(src_h, 2160))
        default_1k = (max(src_w, 1920), max(src_h, 1080))
        default_720 = (max(src_w, 1280), max(src_h, 720))

    if resolution == "Match Source (Zero Resize Loss)" and aspect == "Keep original":
        tw, th = src_w, src_h
    elif resolution == "4K Ultra Sharp":
        tw, th = default_4k
    elif resolution == "1080p Full HD":
        tw, th = default_1k
    elif resolution == "720p HD":
        tw, th = default_720
    else:
        tw, th = default_720 if aspect != "Keep original" else (src_w, src_h)

    tw = max(2, int(tw) - (int(tw) % 2))
    th = max(2, int(th) - (int(th) % 2))
    return tw, th


def build_frame_stage_filter(
    in_label: str,
    out_w: int,
    out_h: int,
    aspect: str,
    out_label: str,
    fit_mode: str = "Fit All (Full Frame + Ambient Blur)",
) -> str:
    """
    - 'Fit All (Full Frame + Ambient Blur)': fills the full out_w x out_h canvas with a softly blurred
      background version of the video and overlays 100% of the uncropped frame centered with zero black bars.
    - 'Fit All (Pure Full Frame)': scales 100% of the frame cleanly to out_w x out_h with zero cropping.
    - 'Fill & Crop (9:16 Zoom)': scales to fill out_w x out_h and center-crops excess width/height.
    - 'Letterbox Fit': scales to fit inside out_w x out_h with a dark charcoal pad.
    """
    if fit_mode == "Fill & Crop (9:16 Zoom)":
        return (
            f"[{in_label}]scale={out_w}:{out_h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={out_w}:{out_h}:(iw-{out_w})/2:(ih-{out_h})/2,setsar=1[{out_label}]"
        )
    if fit_mode == "Letterbox Fit":
        return (
            f"[{in_label}]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=#090c13,setsar=1[{out_label}]"
        )
    if fit_mode == "Fit All (Pure Full Frame)":
        return (
            f"[{in_label}]scale={out_w}:{out_h}:flags=lanczos,setsar=1[{out_label}]"
        )
    # Default: Fit All (Full Frame + Ambient Blur)
    bg_lbl = f"{out_label}_bg"
    fg_lbl = f"{out_label}_fg"
    return (
        f"[{in_label}]split[{bg_lbl}_raw][{fg_lbl}_raw];"
        f"[{bg_lbl}_raw]scale={out_w}:{out_h}:force_original_aspect_ratio=increase:flags=fast_bilinear,"
        f"crop={out_w}:{out_h}:(iw-{out_w})/2:(ih-{out_h})/2,boxblur=24:4,eq=brightness=-0.08:saturation=0.9,setsar=1[{bg_lbl}];"
        f"[{fg_lbl}_raw]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos,setsar=1[{fg_lbl}];"
        f"[{bg_lbl}][{fg_lbl}]overlay=(W-w)/2:(H-h)/2:shortest=1[{out_label}]"
    )


def append_overlays_and_subs(
    graph: list[str],
    current_label: str,
    clip_duration: float,
    out_w: int,
    out_h: int,
    logo: str | None,
    cues: list[tuple[float, float, str]] | None,
    hook_banner_text: str,
    hook_banner_duration: float,
    caption_font_size: int,
    srt_output_path: str | None,
    temp_text_files: list[Path],
) -> tuple[str, str | None]:
    current = current_label
    srt_file: Path | None = None
    font_opt = ""
    sys_font = find_system_font()
    if sys_font:
        font_opt = f":fontfile='{sys_font}'"

    # Section 5: Non-blocking top hook banner placed high in upper margin
    hook_lines = wrap_text_lines(hook_banner_text, max_chars=24, max_lines=2)
    if hook_lines:
        hook_font_px = max(20, min(36, int(out_h * 0.025)))
        hook_step = int(hook_font_px * 1.28)
        top_y = max(28, int(out_h * 0.06))
        active_end = min(clip_duration, max(1.0, float(hook_banner_duration)))
        hook_filters: list[str] = []

        for h_i, h_line in enumerate(hook_lines):
            hook_txt_path = WORK_DIR / f"hook_{next(tempfile._get_candidate_names())}.txt"
            hook_txt_path.write_text(h_line, encoding="utf-8")
            temp_text_files.append(hook_txt_path)
            hook_file_esc = escape_ffmpeg_path(hook_txt_path)
            y_hook = top_y + (h_i * hook_step)
            hook_filters.append(
                f"drawtext=textfile='{hook_file_esc}'{font_opt}:fontcolor=white:fontsize={hook_font_px}:"
                f"x=max(24\\,(w-text_w)/2):y={y_hook}:"
                f"box=1:boxcolor=black@0.72:boxborderw=8:"
                f"enable='between(t,0,{active_end:.2f})'"
            )

        graph.append(f"[{current}]{','.join(hook_filters)}[vhook]")
        current = "vhook"

    has_logo = bool(logo and Path(logo).exists())
    if has_logo:
        graph += [
            "[1:v]scale=iw*0.12:-1:flags=bilinear[logo]",
            f"[{current}][logo]overlay=W-w-20:20:format=auto[vlogo]",
        ]
        current = "vlogo"

    # Section 5: Optional English burn-in subtitles parked cleanly at bottom without blocking action
    if cues:
        if srt_output_path:
            srt_file = write_srt_file(cues, clip_duration, Path(srt_output_path))

        scale_ratio = out_h / 1280.0
        sub_px = max(20, min(38, int(round(int(caption_font_size) * max(0.8, scale_ratio)))))
        bottom_margin = max(48, min(int(out_h * 0.10), int(round(out_h * 0.065))))
        line_step = int(sub_px * 1.26)
        sub_filters: list[str] = []

        for c_start, c_end, c_text in cues[:40]:
            c_a = max(0.0, min(clip_duration, float(c_start)))
            c_b = max(c_a + 0.15, min(clip_duration, float(c_end)))
            lines_list = wrap_text_lines(" ".join(c_text.splitlines()), max_chars=26, max_lines=2)
            if not lines_list:
                continue
            total_block_h = line_step * len(lines_list)
            base_y = out_h - bottom_margin - total_block_h
            for l_i, raw_ln in enumerate(lines_list):
                cue_file = WORK_DIR / f"cue_{next(tempfile._get_candidate_names())}.txt"
                cue_file.write_text(raw_ln, encoding="utf-8")
                temp_text_files.append(cue_file)
                cue_file_esc = escape_ffmpeg_path(cue_file)
                y_pos = max(24, base_y + (l_i * line_step))
                sub_filters.append(
                    f"drawtext=textfile='{cue_file_esc}'{font_opt}:fontcolor=white:fontsize={sub_px}:"
                    f"x=max(24\\,(w-text_w)/2):y={y_pos}:"
                    f"box=1:boxcolor=black@0.68:boxborderw=7:"
                    f"enable='between(t,{c_a:.2f},{c_b:.2f})'"
                )

        if sub_filters:
            graph.append(f"[{current}]{','.join(sub_filters)}[vsub]")
            current = "vsub"

    return current, (str(srt_file) if srt_file and srt_file.exists() else None)


def build_audio_stage(
    graph: list[str],
    inputs: list[str],
    input_spec: str,
    clip_duration: float,
    mute_original: bool = False,
    voiceover_path: str | None = None,
    normalize_audio: bool = False,
) -> tuple[str, bool]:
    """
    Stage 6 audio builder:
    - If mute_original is True: silences original video audio and uses voiceover audio (or clean silent track).
    - If mute_original is False: keeps original video audio and mixes in voiceover if provided.
    """
    has_vo = bool(voiceover_path and Path(voiceover_path).exists())
    vo_idx = len(inputs)
    if has_vo:
        inputs += ["-i", str(voiceover_path)]

    if mute_original:
        if has_vo:
            graph.append(
                f"[{vo_idx}:a]atrim=0:{clip_duration:.2f},asetpts=PTS-STARTPTS,"
                f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=1.15[aout]"
            )
            return "[aout]", True
        graph.append(
            f"anullsrc=r=44100:cl=stereo:d={clip_duration:.2f}[aout]"
        )
        return "[aout]", True

    if has_vo:
        graph.append(
            f"[{input_spec}]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=0.25[orig_a];"
            f"[{vo_idx}:a]atrim=0:{clip_duration:.2f},asetpts=PTS-STARTPTS,"
            f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=1.15[vo_a];"
            f"[orig_a][vo_a]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
        return "[aout]", True

    if normalize_audio:
        graph.append(
            f"[{input_spec}]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
            f"loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
        )
        return "[aout]", True

    return "", False


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
    src_w: int = 1280,
    src_h: int = 720,
    caption_font_size: int = 28,
    encode_preset: str = "ultrafast",
    normalize_audio: bool = False,
    clip_index: int = 0,
    srt_output_path: str | None = None,
    fit_mode: str = "Fit All (Full Frame + Ambient Blur)",
    mute_original: bool = False,
    voiceover_path: str | None = None,
) -> str | None:
    if not source or not Path(source).exists():
        raise RuntimeError("Source or broken-part video file is missing. Load or break the video again.")

    clip_duration = max(0.5, round(float(end) - float(start), 1))
    out_w, out_h = resolve_target_dimensions(aspect, resolution, src_w, src_h)

    graph: list[str] = [build_frame_stage_filter("0:v", out_w, out_h, aspect, "vstage", fit_mode=fit_mode)]
    inputs: list[str] = ["-ss", str(round(float(start), 2)), "-i", source]
    has_logo = bool(logo and Path(logo).exists())
    if has_logo:
        inputs += ["-i", str(logo)]

    temp_text_files: list[Path] = []
    try:
        current, saved_srt = append_overlays_and_subs(
            graph=graph,
            current_label="vstage",
            clip_duration=clip_duration,
            out_w=out_w,
            out_h=out_h,
            logo=logo,
            cues=cues,
            hook_banner_text=hook_banner_text,
            hook_banner_duration=hook_banner_duration,
            caption_font_size=caption_font_size,
            srt_output_path=srt_output_path,
            temp_text_files=temp_text_files,
        )

        a_label, custom_audio = build_audio_stage(
            graph=graph,
            inputs=inputs,
            input_spec="0:a?",
            clip_duration=clip_duration,
            mute_original=mute_original,
            voiceover_path=voiceover_path,
            normalize_audio=normalize_audio,
        )

        args = ["ffmpeg", "-y", *inputs]
        if custom_audio:
            args += ["-filter_complex", ";".join(graph), "-map", f"[{current}]", "-map", a_label]
        else:
            args += ["-filter_complex", ";".join(graph), "-map", f"[{current}]", "-map", "0:a?"]

        args += [
            "-t", str(clip_duration),
            "-c:v", "libx264",
            "-preset", encode_preset,
            "-crf", str(int(crf)),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            output,
        ]
        p = run_cmd(args, timeout=600)
        if p.returncode or not Path(output).exists():
            raise RuntimeError(p.stderr[-2500:] or "FFmpeg render failed.")
        return saved_srt
    finally:
        for tf in temp_text_files:
            tf.unlink(missing_ok=True)
        gc.collect()


def render_stitched_clip(
    source: str,
    output: str,
    moments: list[dict],
    aspect: str,
    resolution: str,
    crf: int,
    logo: str | None = None,
    cues: list[tuple[float, float, str]] | None = None,
    hook_banner_text: str = "",
    hook_banner_duration: float = 8.0,
    src_w: int = 1280,
    src_h: int = 720,
    caption_font_size: int = 28,
    encode_preset: str = "ultrafast",
    srt_output_path: str | None = None,
    fit_mode: str = "Fit All (Full Frame + Ambient Blur)",
    mute_original: bool = False,
    voiceover_path: str | None = None,
) -> str | None:
    """Trims multiple peak moments from source and concatenates them into ONE single clip."""
    if not source or not Path(source).exists():
        raise RuntimeError("Source video file is missing.")
    if not moments:
        raise RuntimeError("No peak moments selected to stitch.")

    out_w, out_h = resolve_target_dimensions(aspect, resolution, src_w, src_h)
    graph: list[str] = []
    concat_inputs: list[str] = []
    total_dur = 0.0

    for idx, m in enumerate(moments):
        s_v = round(max(0.0, float(m["start"])), 2)
        e_v = round(max(s_v + 0.5, float(m["end"])), 2)
        seg_d = round(e_v - s_v, 2)
        total_dur = round(total_dur + seg_d, 2)

        v_trim = f"vt{idx}"
        v_stage = f"vs{idx}"
        a_trim = f"at{idx}"
        graph.append(
            f"[0:v]trim=start={s_v:.2f}:end={e_v:.2f},setpts=PTS-STARTPTS[{v_trim}]"
        )
        graph.append(build_frame_stage_filter(v_trim, out_w, out_h, aspect, v_stage, fit_mode=fit_mode))
        graph.append(
            f"[0:a]atrim=start={s_v:.2f}:end={e_v:.2f},asetpts=PTS-STARTPTS,aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[{a_trim}]"
        )
        concat_inputs.append(f"[{v_stage}][{a_trim}]")

    n_seg = len(moments)
    graph.append(
        f"{''.join(concat_inputs)}concat=n={n_seg}:v=1:a=1[vconcat][aconcat]"
    )

    inputs: list[str] = ["-i", source]
    has_logo = bool(logo and Path(logo).exists())
    if has_logo:
        inputs += ["-i", str(logo)]

    temp_text_files: list[Path] = []
    try:
        current, saved_srt = append_overlays_and_subs(
            graph=graph,
            current_label="vconcat",
            clip_duration=max(0.5, total_dur),
            out_w=out_w,
            out_h=out_h,
            logo=logo,
            cues=cues,
            hook_banner_text=hook_banner_text,
            hook_banner_duration=hook_banner_duration,
            caption_font_size=caption_font_size,
            srt_output_path=srt_output_path,
            temp_text_files=temp_text_files,
        )

        a_label, custom_audio = build_audio_stage(
            graph=graph,
            inputs=inputs,
            input_spec="aconcat",
            clip_duration=max(0.5, total_dur),
            mute_original=mute_original,
            voiceover_path=voiceover_path,
            normalize_audio=False,
        )

        args = ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(graph), "-map", f"[{current}]"]
        args += ["-map", a_label] if custom_audio else ["-map", "[aconcat]"]
        args += [
            "-c:v", "libx264",
            "-preset", encode_preset,
            "-crf", str(int(crf)),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            output,
        ]
        p = run_cmd(args, timeout=600)
        if p.returncode or not Path(output).exists():
            raise RuntimeError(p.stderr[-2500:] or "FFmpeg stitched render failed.")
        return saved_srt
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
    ip_now = detect_client_ip()
    defaults = {
        "detected_ip": ip_now,
        "account_handle": "",
        "workspace_key": compute_workspace_key("", ip_now),
        "workspace_loaded_once": False,
        "last_saved_at": None,
        "brief": "",
        "brief_analysis": None,
        "campaign_feedback": None,
        "go_ahead_clips": False,
        "target_platform_choice": "YouTube + IG + Facebook",
        "auto_clip_count": 2,
        "auto_clip_count_top": 2,
        "auto_clip_count_step3": 2,
        "desired_clip_minutes": 0.50,
        "auto_clip_minutes": 0.50,
        "used_clip_windows": [],
        "hook_mode_choice": "Standard Clip Length (Exact Minutes)",
        "hook_banner_scope": "8-Second Top Banner (0–8s)",
        "pending_hook_banner_scope": None,
        "burn_subtitles_chk_v2": False,
        "subtitle_language": "English (Default)",
        "onscreen_caption_size": 28,
        "vertical_fit_mode": "Fit All (Full Frame + Ambient Blur)",
        "mute_original_audio": False,
        "voiceover_source_mode": "Uploaded Voice Track",
        "voiceover_script_text": "",
        "voiceover_audio_path": None,
        "voiceover_audio_name": None,
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

    if st.session_state.get("vertical_fit_mode") not in FIT_MODES:
        st.session_state["vertical_fit_mode"] = "Fit All (Full Frame + Ambient Blur)"

    if not st.session_state.get("workspace_loaded_once"):
        st.session_state["workspace_loaded_once"] = True
        load_workspace_state(st.session_state["workspace_key"])


init_state()

# ----------------------------- HEADER & NAV -----------------------------
st.markdown(f"""<div class="hero">
<div style="color:#a5b4fc;font-weight:750;font-size:.76rem;letter-spacing:.13em">YOUTUBE + IG + FACEBOOK · CLEAN 9:16 FIT · SECTION 5 ENGLISH SUBTITLES · STAGE 6 MUTE & VOICEOVER</div>
<h1>🎬 {APP_NAME}</h1>
<p class="muted">Full-frame 9:16 fit without black bars or sloppy stretching, multi-moment peak stitching, optional Section 5 English burn-in subtitles, and Stage 6 mute + voiceover inside.</p>
<span class="pill">Clean 9:16 Fit</span>
<span class="pill">Section 5: Optional English Subtitles</span>
<span class="pill">Stage 6: Mute & Voiceover Inside</span>
<span class="pill">Clear & Manage Saved Work</span>
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
    st.markdown("**Saved Work (IP / Sign-In Demo)**")
    st.caption(f"Detected IP: `{st.session_state.get('detected_ip', 'local-demo-ip')}`")
    st.text_input(
        "Google / Demo Sign-In ID (optional)",
        key="account_handle",
        placeholder="e.g. demo@gmail.com or client-a",
    )
    active_ws_key = compute_workspace_key(
        st.session_state.get("account_handle", ""),
        st.session_state.get("detected_ip", "local-demo-ip"),
    )
    st.session_state["workspace_key"] = active_ws_key
    st.caption(f"Workspace ID: `{active_ws_key}`")

    ws_b1, ws_b2 = st.columns(2)
    with ws_b1:
        if st.button("💾 Save Work", use_container_width=True, key="save_ws_btn"):
            save_workspace_state(active_ws_key)
            st.session_state["last_saved_at"] = time.strftime("%H:%M:%S")
            st.success("Saved!")
    with ws_b2:
        if st.button("📂 Load Saved", use_container_width=True, key="load_ws_btn"):
            if load_workspace_state(active_ws_key):
                st.success("Loaded!")
                st.rerun()
            else:
                st.info("No saved work for this ID/IP yet.")

    if st.button("🧹 Clear Current Saved Work", use_container_width=True, key="clear_cur_ws_btn"):
        clear_workspace_data(active_ws_key, reset_session=True)
        st.success("Cleared saved work & reset current session.")
        st.rerun()

    saved_items = list_saved_workspaces()
    if saved_items:
        with st.expander(f"Manage Saved Workspaces ({len(saved_items)})"):
            for item in saved_items[:10]:
                st.caption(f"**{item['label']}** ({item['size_mb']} MB)")
                c_ld, c_rm = st.columns(2)
                with c_ld:
                    if st.button("Load", key=f"ws_ld_{item['key']}", use_container_width=True):
                        st.session_state["workspace_key"] = item["key"]
                        load_workspace_state(item["key"])
                        st.rerun()
                with c_rm:
                    if st.button("Delete", key=f"ws_rm_{item['key']}", use_container_width=True):
                        is_cur = item["key"] == active_ws_key
                        clear_workspace_data(item["key"], reset_session=is_cur)
                        shutil.rmtree(item["path"], ignore_errors=True)
                        st.rerun()

    if st.session_state.get("last_saved_at"):
        st.caption(f"Last saved: `{st.session_state['last_saved_at']}`")

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
                    save_workspace_state()
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
                        save_workspace_state()
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
                save_workspace_state()

        st.text_area(
            "Paste campaign requirements",
            key="brief",
            height=175,
            placeholder="Example: Post across YouTube Shorts, Instagram Reels & Facebook. Stitch peak moments, fit 9:16 cleanly, optional English burn-in subtitles, voiceover.",
        )

        p_col, c_col, min_col = st.columns(3)
        with p_col:
            st.selectbox(
                "Target channels",
                ["YouTube + IG + Facebook", "YouTube Shorts / Video", "Instagram Reels / Feed", "Facebook Reels / Post"],
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
                "Section 5: Burn-in English subtitles (Optional)",
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
                                    with st.spinner("Transcribing spoken words for optional English subtitles..."):
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
                                "gaps": ["Upload a campaign video on the left to pick peak moments and stitch clips."],
                                "warnings": analysis.get("prohibited_content", []) or [],
                            }
                    save_workspace_state()
                    st.success("Campaign feedback ready and saved.")
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
            st.markdown("**YouTube · IG · Facebook Requirements**")
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
                save_workspace_state()

        with go_col2:
            if st.session_state.go_ahead_clips:
                exact_s = minutes_to_seconds(float(st.session_state.auto_clip_minutes))
                st.success(f"Go-ahead granted! Each stitched clip totals ~{float(st.session_state.auto_clip_minutes):.2f} min ({exact_s:.1f}s) with clean 9:16 fit.")

    # Step 3: Auto-Clipper + Section 5 (Optional English Subtitles) + Stage 6 (Mute & Voiceover Inside)
    if st.session_state.go_ahead_clips and st.session_state.video_info and ffmpeg_ready():
        st.divider()
        st.subheader("Step 3 · Auto-Clipper (Peak Stitch + Clean 9:16 Fit)")
        vi = st.session_state.video_info
        dur = max(1.0, float(vi["duration"]))
        use_subs = bool(st.session_state.get("burn_subtitles_chk_v2", False))

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
                "How many stitched clips",
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
            if st.button("🔄 Pick Different Peak Spots", use_container_width=True, key="pick_fresh_spots_btn"):
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
                save_workspace_state()
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
                save_workspace_state()

        default_asp = default_aspect_for_platform(st.session_state.target_platform_choice)
        st.caption(
            f"Each clip stitches peak sub-moments summing to **{active_minutes:.2f} min ({exact_target_sec:.1f}s)** · "
            f"Aspect: **{default_asp}**."
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
                ["720p HD", "1080p Full HD", "4K Ultra Sharp", "Match Source (Zero Resize Loss)"],
                key="wf_sharp_res",
            )
        with q_col2:
            fit_mode = st.selectbox(
                "9:16 Frame Fit",
                FIT_MODES,
                key="vertical_fit_mode",
            )
        with q_col3:
            sharp_crf = st.slider("Quality CRF (18–26)", 18, 26, 22, key="wf_sharp_crf")
        with q_col4:
            encode_preset = st.selectbox("Encode speed", ["ultrafast", "veryfast", "fast"], key="wf_encode_preset")

        # Section 5: Optional English Burn-In Subtitles
        with st.container(border=True):
            st.markdown("### Section 5 · Optional English Burn-In Subtitles (Non-Blocking)")
            s5_c1, s5_c2, s5_c3 = st.columns([1.3, 1.0, 1.2])
            with s5_c1:
                use_subs = st.checkbox(
                    "Enable Burn-In Subtitles (Optional)",
                    key="burn_subtitles_chk_v2",
                )
            with s5_c2:
                st.selectbox(
                    "Subtitle Language",
                    ["English (Default)"],
                    key="subtitle_language",
                )
            with s5_c3:
                cap_size = st.slider(
                    "Subtitle size (non-blocking lower zone)",
                    20, 36, int(st.session_state.onscreen_caption_size),
                    key="wf_cap_size",
                )
            st.caption(
                "When enabled, English subtitles are generated and burned cleanly in the lower margin without blocking the video or top hook. Leave unchecked for clean un-subtitled clips."
            )

        # Stage 6: Mute Original Video Override + Voiceover Inside
        with st.container(border=True):
            st.markdown("### Stage 6 · Mute Video Override & Voiceover Inside")
            s6_c1, s6_c2 = st.columns([1.0, 1.4])
            with s6_c1:
                mute_orig_wf = st.checkbox(
                    "Mute original video audio",
                    key="mute_original_audio",
                )
                vo_mode_wf = st.radio(
                    "Voiceover source inside video",
                    ["Uploaded Voice Track", "Readout Script / Hook Voiceover"],
                    key="voiceover_source_mode",
                )
            with s6_c2:
                if vo_mode_wf == "Uploaded Voice Track":
                    vo_up = st.file_uploader(
                        "Upload voiceover audio (MP3, WAV, M4A, AAC, OGG)",
                        type=["mp3", "wav", "m4a", "aac", "ogg"],
                        key="wf_voiceover_up",
                    )
                    if vo_up:
                        saved_vo = save_upload(vo_up)
                        st.session_state["voiceover_audio_path"] = str(saved_vo)
                        st.session_state["voiceover_audio_name"] = vo_up.name
                    if st.session_state.get("voiceover_audio_path") and Path(st.session_state["voiceover_audio_path"]).exists():
                        st.caption(f"Active voiceover file: **{st.session_state.get('voiceover_audio_name') or Path(st.session_state['voiceover_audio_path']).name}**")
                        st.audio(st.session_state["voiceover_audio_path"])
                else:
                    vo_script = st.text_area(
                        "Voiceover script to read inside the video",
                        key="voiceover_script_text",
                        height=80,
                        placeholder="Type what you want read over the muted/clipped video...",
                    )
                    render_fast_voice_reader(
                        vo_script or (st.session_state.brief[:500]),
                        label="🔊 Preview Voiceover Readout",
                        uid="stage6_wf_vo",
                    )

        for idx, cand in enumerate(cands):
            moments = cand.get("moments") or build_stitched_peak_moments(
                duration=dur,
                desired_minutes=active_minutes,
                used_windows=st.session_state.used_clip_windows,
                transcript=st.session_state.transcript,
                seed_offset=idx + 1,
            )
            cand["moments"] = moments
            stitched_sec = round(sum(max(0.1, float(m["end"]) - float(m["start"])) for m in moments), 1)
            chain_str = "  ➜  ".join(
                f"`{fmt_time(m['start'])}`–`{fmt_time(m['end'])}` ({round(float(m['end']) - float(m['start']), 1):.1f}s)"
                for m in moments
            )
            st.info(
                f"**{cand.get('title', f'Peak Stitch #{idx+1}')}** — Total: **{stitched_sec:.1f}s** (`{active_minutes:.2f} min`) · "
                f"Fitted Hook: *{cand.get('hook', '')}*\n\nStitched spots: {chain_str}"
            )

        if st.button("⚡ Run Auto Peak Stitch & Render Clips", type="primary", use_container_width=True, key="wf_extract_clips_btn"):
            try:
                if use_subs and not st.session_state.transcript and get_openai_client():
                    try:
                        with st.spinner("Transcribing spoken audio for optional English subtitles..."):
                            st.session_state.transcript = transcribe_video(st.session_state.source_video)
                    except Exception:
                        pass

                active_vo_path: str | None = None
                if st.session_state.get("voiceover_source_mode") == "Uploaded Voice Track":
                    p_vo = st.session_state.get("voiceover_audio_path")
                    if p_vo and Path(p_vo).exists():
                        active_vo_path = p_vo
                else:
                    script_src = (st.session_state.get("voiceover_script_text") or "").strip()
                    if script_src:
                        gen_vo = synthesize_voiceover_file(script_src)
                        if gen_vo and gen_vo.exists():
                            active_vo_path = str(gen_vo)
                        else:
                            st.info("No local/API TTS voice engine found on server—muted/audio mix applied; use Fast Readout button for instant browser voice playback.")

                ws_dir = workspace_dir(st.session_state.get("workspace_key", "default"))
                rendered_now = []
                banner_dur = 8.0 if st.session_state.hook_banner_scope.startswith("8-Second") else 9999.0
                render_prog = st.progress(0, text=f"Rendering stitched clips: 0/{len(cands)}")

                for idx, cand in enumerate(cands):
                    moments = cand.get("moments") or []
                    if not moments:
                        continue
                    clip_num_global = len(st.session_state.clips) + 1
                    base_stem = f"{safe_name(cand.get('title', f'Peak_Stitch_{clip_num_global}'))}_stitch{clip_num_global}"
                    out_file = ws_dir / f"{base_stem}.mp4"
                    srt_file = ws_dir / f"{base_stem}.srt"

                    stitched_sec = round(sum(max(0.1, float(m["end"]) - float(m["start"])) for m in moments), 1)
                    all_spoken = normalize_english_subtitle_text(" ".join(
                        m.get("text") or transcript_slice(st.session_state.transcript, float(m["start"]), float(m["end"]))
                        for m in moments
                    ))
                    final_hook = normalize_english_subtitle_text((cand.get("hook") or "").strip()) or pick_good_hook(all_spoken, brief=st.session_state.brief, clip_index=clip_num_global - 1)

                    wrap_chars = 26 if default_asp in {"9:16 Vertical", "4:5 Portrait", "1:1 Square"} else 36
                    cues = build_stitched_caption_cues(
                        transcript=st.session_state.transcript,
                        moments=moments,
                        max_chars=wrap_chars,
                    ) if use_subs else []

                    with st.spinner(f"Stitching {len(moments)} peak moments into Clip {clip_num_global} ({stitched_sec:.1f}s) in {default_asp}..."):
                        saved_srt = render_stitched_clip(
                            source=st.session_state.source_video,
                            output=str(out_file),
                            moments=moments,
                            aspect=default_asp,
                            resolution=sharp_res,
                            crf=sharp_crf,
                            logo=st.session_state.logo_path,
                            cues=cues if cues else None,
                            hook_banner_text=final_hook,
                            hook_banner_duration=banner_dur,
                            src_w=int(vi.get("width") or 1280),
                            src_h=int(vi.get("height") or 720),
                            caption_font_size=int(cap_size),
                            encode_preset=encode_preset,
                            srt_output_path=str(srt_file) if use_subs else None,
                            fit_mode=fit_mode,
                            mute_original=bool(st.session_state.get("mute_original_audio", False)),
                            voiceover_path=active_vo_path,
                        )

                    for m in moments:
                        st.session_state.used_clip_windows.append([round(float(m["start"]), 1), round(float(m["end"]), 1)])

                    clip_meta = {
                        **cand,
                        "mode": "Auto Peak Stitch",
                        "title": cand.get("title", f"Peak Stitch #{clip_num_global}"),
                        "moments": moments,
                        "start": float(moments[0]["start"]),
                        "end": round(float(moments[0]["start"]) + stitched_sec, 1),
                        "aspect": default_asp,
                        "fit_mode": fit_mode,
                        "muted": bool(st.session_state.get("mute_original_audio", False)),
                        "hook": final_hook,
                        "transcript_excerpt": all_spoken,
                    }
                    copy_pack = generate_multi_platform_copy(
                        brief=st.session_state.brief,
                        analysis=st.session_state.brief_analysis,
                        clip_info=clip_meta,
                        transcript_text=all_spoken,
                        custom_instructions=st.session_state.caption_instructions,
                        clip_index=clip_num_global - 1,
                    )
                    rec = {
                        "name": f"{clip_meta['title']} ({default_asp})",
                        "path": str(out_file),
                        "srt_path": saved_srt,
                        "start": float(moments[0]["start"]),
                        "end": round(float(moments[0]["start"]) + stitched_sec, 1),
                        "aspect": default_asp,
                        "resolution": sharp_res,
                        "meta": clip_meta,
                        "youtube": copy_pack["youtube"],
                        "instagram": copy_pack["instagram"],
                        "facebook": copy_pack["facebook"],
                        "caption": copy_pack["caption"],
                        "description": copy_pack["description"],
                    }
                    st.session_state.clips.append(rec)
                    rendered_now.append(rec)
                    render_prog.progress(
                        int(((idx + 1) / max(1, len(cands))) * 100),
                        text=f"Rendering stitched clips: {idx + 1}/{len(cands)}",
                    )

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
                    save_workspace_state()
                    st.success(f"Stitched {len(rendered_now)} peak-moment clip(s) at ~{active_minutes:.2f} min each in {default_asp} ({fit_mode}).")
            except Exception as exc:
                st.error(f"Auto-clip stitch error: {exc}")

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
                    int(bp_info.get("width") or 1280),
                    int(bp_info.get("height") or 720),
                ))
        if st.session_state.source_video and Path(st.session_state.source_video).exists() and st.session_state.video_info:
            vi_ed = st.session_state.video_info
            part_sources.append((
                f"Full Video: {st.session_state.video_name or 'Source'} ({fmt_time(vi_ed.get('duration', 0))})",
                st.session_state.source_video,
                max(1.0, float(vi_ed.get("duration") or 1.0)),
                int(vi_ed.get("width") or 1280),
                int(vi_ed.get("height") or 720),
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
            fit_mode_ed = st.selectbox("9:16 Frame Fit", FIT_MODES, key="ed_fit_mode")
            resolution = st.selectbox("Output Resolution", ["720p HD", "1080p Full HD", "4K Ultra Sharp", "Match Source (Zero Resize Loss)"], key="ed_resolution")
            crf = st.slider("Quality CRF (18–26, 22 = fast & crisp)", 18, 26, 22, key="ed_crf")
            add_logo = st.checkbox("Include saved brand logo", value=bool(st.session_state.logo_path), key="ed_add_logo")
            add_sub = st.checkbox("Section 5: Burn English subtitles + export .SRT (Optional)", value=False, key="ed_add_sub")
            mute_ed = st.checkbox("Stage 6: Mute original video audio", value=bool(st.session_state.get("mute_original_audio", False)), key="ed_mute_orig")

        if st.button("Render Manual Clip (Start → End)", type="primary", use_container_width=True, key="ed_render_btn"):
            if end <= start:
                st.error("End timestamp must be greater than start timestamp.")
            elif ffmpeg_ready():
                try:
                    ws_dir = workspace_dir(st.session_state.get("workspace_key", "default"))
                    clip_idx = len(st.session_state.clips) + 1
                    base_stem = f"{safe_name(clip_name)}_manual{clip_idx}"
                    out = ws_dir / f"{base_stem}.mp4"
                    srt_out = ws_dir / f"{base_stem}.srt"
                    spoken_now = normalize_english_subtitle_text(transcript_slice(st.session_state.transcript, start, end))
                    final_hook = normalize_english_subtitle_text((hook_line or "").strip()) or pick_good_hook(spoken_now, brief=st.session_state.brief, clip_index=clip_idx - 1)
                    wrap_chars = 26 if aspect in {"9:16 Vertical", "4:5 Portrait", "1:1 Square"} else 36
                    cues = []
                    if add_sub:
                        cues = build_caption_cues(
                            transcript=st.session_state.transcript,
                            start=start,
                            end=end,
                            fallback_text=spoken_now,
                            max_chars=wrap_chars,
                        )
                    vo_ed_path = st.session_state.get("voiceover_audio_path")
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
                            srt_output_path=str(srt_out) if add_sub else None,
                            fit_mode=fit_mode_ed,
                            mute_original=bool(mute_ed),
                            voiceover_path=vo_ed_path if (vo_ed_path and Path(vo_ed_path).exists()) else None,
                        )
                    st.session_state.used_clip_windows.append([round(start, 1), round(end, 1)])
                    c_meta = {
                        "mode": "Manual Clip",
                        "title": clip_name,
                        "source": src_label,
                        "start": start,
                        "end": end,
                        "aspect": aspect,
                        "fit_mode": fit_mode_ed,
                        "muted": bool(mute_ed),
                        "hook": final_hook,
                        "transcript_excerpt": spoken_now,
                    }
                    copy_pack = generate_multi_platform_copy(
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
                        "meta": c_meta,
                        "youtube": copy_pack["youtube"],
                        "instagram": copy_pack["instagram"],
                        "facebook": copy_pack["facebook"],
                        "caption": copy_pack["caption"],
                        "description": copy_pack["description"],
                    })
                    save_workspace_state()
                    st.success("Manual clip rendered and saved.")
                    st.video(str(out))
                except Exception as exc:
                    st.error(f"Render error: {exc}")

# ----------------------------- PAGE 3: BRAND KIT & SETTINGS ------------
elif page == "Brand Kit & Settings":
    st.subheader("Brand Kit, Saved Workspaces & AI Settings")
    logo = st.file_uploader("Upload brand logo (PNG/JPG/WEBP)", type=["png", "jpg", "jpeg", "webp"], key="bk_logo_up")
    if logo and st.button("Save Logo", key="bk_save_logo"):
        st.session_state.logo_path = str(save_upload(logo))
        st.session_state.logo_name = logo.name
        save_workspace_state()
        st.success("Brand logo saved for this workspace.")
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
    st.subheader("Generated Clips · YouTube + IG + Facebook Copy Pack")
    st.text_area(
        "Optional extra caption/CTA note for clips",
        key="caption_instructions",
        height=75,
    )

    act_col1, act_col2, act_col3 = st.columns([1.4, 1.0, 0.8])
    with act_col1:
        if st.button("✨ Generate YouTube + IG + Facebook Copy for All Clips", key="gen_all_short_captions_btn", use_container_width=True):
            with st.spinner("Generating YouTube, Instagram & Facebook copy for each clip..."):
                for i, cl in enumerate(st.session_state.clips):
                    c_meta = cl.get("meta") or {"title": cl["name"], "start": cl["start"], "end": cl["end"]}
                    t_seg = c_meta.get("transcript_excerpt") or transcript_slice(st.session_state.transcript, cl["start"], cl["end"])
                    pack = generate_multi_platform_copy(
                        brief=st.session_state.brief,
                        analysis=st.session_state.brief_analysis,
                        clip_info=c_meta,
                        transcript_text=t_seg,
                        custom_instructions=st.session_state.caption_instructions,
                        clip_index=i,
                    )
                    st.session_state.clips[i].update(pack)
            save_workspace_state()
            st.success("Updated YouTube, Instagram & Facebook copy for every clip.")
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
    with act_col3:
        if st.button("🗑️ Clear All Clips", key="clear_all_clips_btn", use_container_width=True):
            for cl in st.session_state.clips:
                for k_f in ("path", "srt_path"):
                    fp = cl.get(k_f)
                    if fp:
                        Path(fp).unlink(missing_ok=True)
            st.session_state.clips = []
            save_workspace_state()
            st.rerun()

    for idx, cl in enumerate(st.session_state.clips):
        with st.container(border=True):
            c_vid, c_cap = st.columns([1.2, 1.4], gap="large")
            actual_sec = round(float(cl["end"]) - float(cl["start"]), 1)
            c_meta = cl.get("meta") or {}
            clip_mode = c_meta.get("mode") or "Clip"
            moments_list = c_meta.get("moments") or []
            with c_vid:
                st.markdown(f"**Clip {idx + 1} [{clip_mode}]: {cl['name']}**")
                mute_tag = " · Muted Audio" if c_meta.get("muted") else ""
                if moments_list:
                    m_txt = " + ".join(f"{float(m['start']):.0f}s–{float(m['end']):.0f}s" for m in moments_list)
                    st.caption(f"Stitched: {m_txt} ({actual_sec:.1f}s total) · {cl['aspect']} · {cl['resolution']}{mute_tag}")
                else:
                    st.caption(f"{fmt_time(cl['start'])} → {fmt_time(cl['end'])} ({actual_sec:.1f}s) · {cl['aspect']} · {cl['resolution']}{mute_tag}")
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
                st.markdown("**YouTube · Instagram · Facebook Copy**")
                if st.button(
                    f"✨ Refresh YT + IG + FB Copy for Clip {idx+1}",
                    type="primary",
                    use_container_width=True,
                    key=f"get_cap_short_{idx}",
                ):
                    t_seg = c_meta.get("transcript_excerpt") or transcript_slice(st.session_state.transcript, cl["start"], cl["end"])
                    with st.spinner(f"Writing YT, IG & FB copy for Clip {idx+1}..."):
                        pack = generate_multi_platform_copy(
                            brief=st.session_state.brief,
                            analysis=st.session_state.brief_analysis,
                            clip_info=c_meta,
                            transcript_text=t_seg,
                            custom_instructions=st.session_state.caption_instructions,
                            clip_index=idx,
                        )
                        st.session_state.clips[idx].update(pack)
                        save_workspace_state()

                t_yt, t_ig, t_fb = st.tabs(["YouTube", "Instagram", "Facebook"])
                with t_yt:
                    yt_val = st.text_area(
                        f"YouTube Title & Description (Clip {idx+1})",
                        value=cl.get("youtube") or cl.get("description", ""),
                        height=145,
                        key=f"yt_area_{idx}",
                    )
                    st.session_state.clips[idx]["youtube"] = yt_val
                    st.session_state.clips[idx]["description"] = yt_val
                with t_ig:
                    ig_val = st.text_area(
                        f"Instagram Caption (Clip {idx+1})",
                        value=cl.get("instagram") or cl.get("caption", ""),
                        height=145,
                        key=f"ig_area_{idx}",
                    )
                    st.session_state.clips[idx]["instagram"] = ig_val
                    st.session_state.clips[idx]["caption"] = ig_val
                with t_fb:
                    fb_val = st.text_area(
                        f"Facebook Caption (Clip {idx+1})",
                        value=cl.get("facebook") or cl.get("caption", ""),
                        height=145,
                        key=f"fb_area_{idx}",
                    )
                    st.session_state.clips[idx]["facebook"] = fb_val

                render_fast_voice_reader(
                    f"YouTube: {cl.get('youtube', '')}\nInstagram: {cl.get('instagram', '')}\nFacebook: {cl.get('facebook', '')}".strip(),
                    label=f"🔊 Fast Read Clip {idx+1} Copy",
                    uid=f"short_{idx}",
                )