from __future__ import annotations

import gc
import json
import re
import shutil
import subprocess
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import streamlit as st

APP_NAME = "ClipStream Studio"
MAX_UPLOAD_MB = 2048
WORK_DIR = Path(tempfile.gettempdir()) / "clipstream_studio"
WORK_DIR.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title=APP_NAME, page_icon="🎬", layout="wide")

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
.hero{padding:24px;border:1px solid #34405a;border-radius:20px;background:linear-gradient(115deg,#211d3b,#141c2d 58%,#30203b);margin-bottom:1.1rem}
.hero h1{margin:0;letter-spacing:-1.2px;font-size:2.2rem}
.muted{color:#a5b0c4;font-size:.9rem}
.pill{display:inline-block;border:1px solid #46516d;color:#c7d2fe;border-radius:30px;padding:4px 10px;font-size:.78rem;margin:4px 5px 0 0}
</style>
""", unsafe_allow_html=True)


def run_cmd(args: list[str], timeout: int | None = None):
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)


def ffmpeg_ready() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def fmt_time(seconds: float) -> str:
    return str(timedelta(seconds=max(0, int(round(seconds)))))


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")
    return (cleaned or "clip")[:55]


def save_upload(uploaded) -> Path:
    ext = Path(uploaded.name).suffix.lower() or ".mp4"
    path = WORK_DIR / f"{next(tempfile._get_candidate_names())}_{int(time.time())}{ext}"
    total_bytes = max(1, int(getattr(uploaded, "size", 0) or 0))
    written = 0
    chunk_size = 16 * 1024 * 1024
    last_pct = -5

    bar = st.progress(0, text="Saving video to disk: 0%")
    uploaded.seek(0)
    with path.open("wb") as handle:
        while True:
            chunk = uploaded.read(chunk_size)
            if not chunk:
                break
            handle.write(chunk)
            written += len(chunk)
            pct = min(100, int((written / total_bytes) * 100))
            if pct >= last_pct + 5 or pct == 100:
                last_pct = pct
                bar.progress(
                    pct,
                    text=f"Saving video to disk: {pct}% ({written / (1024*1024):.1f} / {total_bytes / (1024*1024):.1f} MB)",
                )
    bar.progress(100, text="Video saved to disk: 100%")
    gc.collect()
    return path


def probe_video(path: str) -> dict:
    p = run_cmd([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,size:stream=codec_type,codec_name,width,height",
        "-of", "json", path,
    ], timeout=60)
    if p.returncode:
        raise RuntimeError(p.stderr[-1500:] or "ffprobe failed.")
    data = json.loads(p.stdout)
    streams = data.get("streams", [])
    v = next((x for x in streams if x.get("codec_type") == "video"), {})
    return {
        "duration": float(data.get("format", {}).get("duration", 0) or 0),
        "size": int(data.get("format", {}).get("size", 0) or 0),
        "width": int(v.get("width", 0) or 0),
        "height": int(v.get("height", 0) or 0),
        "video_codec": v.get("codec_name", "unknown"),
    }


def break_video_into_parts(source: str, duration: float, num_parts: int) -> list[dict]:
    """Breaks a video into 3 or 5 parts using fast stream copy with safe fallback."""
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
        chunk_dur = round(min(seg_len, dur - start_s), 2)
        if chunk_dur <= 0.2:
            continue

        out_p = WORK_DIR / f"{stem}_part{i+1}of{n}_{next(tempfile._get_candidate_names())}{ext}"
        cmd = [
            "ffmpeg", "-y", "-ss", str(start_s), "-i", str(src_path),
            "-t", str(chunk_dur), "-c", "copy", "-avoid_negative_ts", "make_zero",
            str(out_p),
        ]
        res = run_cmd(cmd, timeout=600)

        if res.returncode or not out_p.exists() or out_p.stat().st_size == 0:
            out_p.unlink(missing_ok=True)
            res = run_cmd([
                "ffmpeg", "-y", "-ss", str(start_s), "-i", str(src_path),
                "-t", str(chunk_dur), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
                "-c:a", "aac", "-b:a", "128k", str(out_p),
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


def clip_part_segment(
    part_path: str,
    output_path: str,
    start_sec: float,
    end_sec: float,
    aspect: str,
    resolution: str,
    preset: str = "ultrafast",
    crf: int = 21,
):
    """Clips a segment from one of the broken parts."""
    if not Path(part_path).exists():
        raise RuntimeError("Selected part file is missing.")
    dur = max(0.5, round(float(end_sec) - float(start_sec), 2))
    vf_filters: list[str] = []

    if aspect == "9:16 Vertical":
        vf_filters.append("crop=min(iw\\,ih*9/16):min(ih\\,iw*16/9)")
    elif aspect == "16:9 Landscape":
        vf_filters.append("crop=min(iw\\,ih*16/9):min(ih\\,iw*9/16)")
    elif aspect == "1:1 Square":
        vf_filters.append("crop=min(iw\\,ih):min(iw\\,ih)")
    elif aspect == "4:5 Portrait":
        vf_filters.append("crop=min(iw\\,ih*4/5):min(ih\\,iw*5/4)")

    if resolution == "1080p Full HD":
        scale_map = {
            "9:16 Vertical": "1080:1920",
            "16:9 Landscape": "1920:1080",
            "1:1 Square": "1080:1080",
            "4:5 Portrait": "1080:1350",
        }
        vf_filters.append(f"scale={scale_map.get(aspect, '1920:1080')}:flags=bilinear,setsar=1")
    elif resolution == "720p HD":
        scale_map = {
            "9:16 Vertical": "720:1280",
            "16:9 Landscape": "1280:720",
            "1:1 Square": "720:720",
            "4:5 Portrait": "720:900",
        }
        vf_filters.append(f"scale={scale_map.get(aspect, '1280:720')}:flags=bilinear,setsar=1")
    elif vf_filters:
        vf_filters.append("setsar=1")

    cmd = [
        "ffmpeg", "-y", "-ss", str(round(float(start_sec), 2)), "-i", part_path,
        "-t", str(dur),
    ]
    if vf_filters:
        cmd += ["-vf", ",".join(vf_filters)]
    cmd += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(int(crf)),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", output_path,
    ]
    res = run_cmd(cmd, timeout=1800)
    if res.returncode or not Path(output_path).exists():
        raise RuntimeError(res.stderr[-1800:] or "Clipping failed.")


# ----------------------------- SESSION STATE ----------------------------
for k, v in {
    "source_video": None,
    "video_name": None,
    "video_info": None,
    "broken_parts": [],
    "saved_clips": [],
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ----------------------------- HEADER & WORKFLOW ------------------------
st.markdown(f"""<div class="hero">
<div style="color:#a5b4fc;font-weight:750;font-size:.76rem;letter-spacing:.13em">BREAK INTO 3 OR 5 PARTS FIRST · CLIP ANY PART LATER</div>
<h1>🎬 {APP_NAME}</h1>
<p class="muted">Break your video into 3 or 5 parts first, then clip any part later whenever you want.</p>
<span class="pill">3 or 5 parts breaker</span>
<span class="pill">Clip later anytime</span>
<span class="pill">Fast stream-copy split</span>
</div>""", unsafe_allow_html=True)

if not ffmpeg_ready():
    st.error("FFmpeg and ffprobe are required. Add `ffmpeg` to `packages.txt` on Streamlit Cloud or install it locally.")

st.subheader("Step 1 · Load Video & Break Into 3 or 5 Parts")
up = st.file_uploader("Upload video", type=["mp4", "mov", "mkv", "webm", "m4v"], key="breaker_video_up")

if up and up.size > MAX_UPLOAD_MB * 1024 * 1024:
    st.error(f"File exceeds {MAX_UPLOAD_MB} MB limit.")
elif up and st.button("Load Video", type="primary", key="load_video_btn"):
    try:
        vpath = save_upload(up)
        with st.spinner("Reading video metadata..."):
            vinfo = probe_video(str(vpath))
            st.session_state.source_video = str(vpath)
            st.session_state.video_name = up.name
            st.session_state.video_info = vinfo
            st.session_state.broken_parts = []
        st.success(f"Loaded {up.name} ({fmt_time(vinfo['duration'])})")
    except Exception as exc:
        st.error(f"Load failed: {exc}")

if st.session_state.video_info and st.session_state.source_video and Path(st.session_state.source_video).exists():
    vi = st.session_state.video_info
    size_mb = round((vi.get("size") or Path(st.session_state.source_video).stat().st_size) / (1024 * 1024), 1)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Duration", fmt_time(vi["duration"]))
    c2.metric("Resolution", f"{vi['width']}x{vi['height']}")
    c3.metric("File Size", f"{size_mb} MB")
    c4.metric("Parts Ready", len(st.session_state.broken_parts))

    b_col1, b_col2 = st.columns([1, 2])
    with b_col1:
        num_parts = st.radio("Break into how many parts?", [3, 5], horizontal=True, key="num_parts_choice")
    with b_col2:
        if st.button(f"✂️ Break Video into {num_parts} Parts", type="primary", use_container_width=True, key="break_parts_btn"):
            try:
                st.session_state.broken_parts = break_video_into_parts(
                    st.session_state.source_video, float(vi["duration"]), int(num_parts)
                )
                st.success(f"Broken into {len(st.session_state.broken_parts)} parts! Pick any part below to clip now or later.")
            except Exception as exc:
                st.error(f"Break failed: {exc}")

# Step 2: Pick Any Part & Clip It Later
if st.session_state.broken_parts:
    st.divider()
    st.subheader("Step 2 · Pick a Part & Clip It Later")

    part_labels = [
        f"{p['label']} ({fmt_time(p['info']['duration'])} · {round(p['info']['size']/(1024*1024), 1)} MB)"
        for p in st.session_state.broken_parts
    ]
    selected_idx = st.selectbox(
        "Choose part to clip now or later",
        options=list(range(len(part_labels))),
        format_func=lambda i: part_labels[i],
        key="selected_part_idx",
    )
    active_part = st.session_state.broken_parts[int(selected_idx)]
    part_dur = max(1.0, float(active_part["info"]["duration"]))

    st.caption(
        f"Source window: {fmt_time(active_part['start_in_source'])} – {fmt_time(active_part['end_in_source'])} "
        f"| Part length: {part_dur:.1f}s"
    )

    e1, e2, e3 = st.columns(3)
    with e1:
        clip_start = st.number_input("Clip start (sec)", min_value=0.0, max_value=max(0.5, part_dur - 0.5), value=0.0, step=1.0, key="part_clip_start")
    with e2:
        default_end = min(part_dur, float(clip_start) + min(45.0, part_dur))
        clip_end = st.number_input("Clip end (sec)", min_value=0.5, max_value=part_dur, value=max(0.5, default_end), step=1.0, key="part_clip_end")
    with e3:
        clip_name = st.text_input(
            "Clip name",
            value=f"{safe_name(st.session_state.video_name or 'video')}_p{active_part['part_num']}_clip",
            key="part_clip_name",
        )

    o1, o2, o3 = st.columns(3)
    with o1:
        clip_aspect = st.selectbox("Aspect", ["Keep original", "9:16 Vertical", "16:9 Landscape", "1:1 Square", "4:5 Portrait"], key="part_clip_aspect")
    with o2:
        clip_res = st.selectbox("Resolution", ["Match Part", "720p HD", "1080p Full HD"], key="part_clip_res")
    with o3:
        clip_preset = st.selectbox("Speed preset", ["ultrafast", "veryfast", "fast"], key="part_clip_preset")

    if st.button("🎬 Clip Selected Part Now", type="primary", use_container_width=True, key="clip_selected_part_btn"):
        if clip_end <= clip_start:
            st.error("Clip end must be greater than clip start.")
        else:
            try:
                out_file = WORK_DIR / f"{safe_name(clip_name)}_{len(st.session_state.saved_clips)+1}.mp4"
                with st.spinner(f"Clipping {active_part['label']} ({clip_start:.1f}s – {clip_end:.1f}s)..."):
                    clip_part_segment(
                        part_path=active_part["path"],
                        output_path=str(out_file),
                        start_sec=float(clip_start),
                        end_sec=float(clip_end),
                        aspect=clip_aspect,
                        resolution=clip_res,
                        preset=clip_preset,
                        crf=21,
                    )
                st.session_state.saved_clips.append({
                    "name": out_file.name,
                    "part": active_part["label"],
                    "path": str(out_file),
                })
                st.success(f"Saved clip: {out_file.name}")
                st.rerun()
            except Exception as exc:
                st.error(f"Clip failed: {exc}")

    with st.expander("All Broken Parts (Download Any Part Directly)"):
        for p in st.session_state.broken_parts:
            p_mb = round(p["info"]["size"] / (1024 * 1024), 1)
            st.write(f"**{p['label']}** — {fmt_time(p['info']['duration'])} ({p_mb} MB)")
            if Path(p["path"]).exists():
                with open(p["path"], "rb") as fh:
                    st.download_button(f"Download {p['label']}", fh, file_name=Path(p["path"]).name, key=f"dl_part_{p['part_num']}")

# Step 3: Clipped Outputs
if st.session_state.saved_clips:
    st.divider()
    st.subheader("Step 3 · Clipped Outputs")
    for idx, sc in enumerate(st.session_state.saved_clips):
        if Path(sc["path"]).exists():
            with st.container(border=True):
                st.markdown(f"**{sc['name']}** ({sc['part']})")
                st.video(sc["path"])
                with open(sc["path"], "rb") as fh:
                    st.download_button(f"⬇ Download {sc['name']}", fh, file_name=sc["name"], key=f"dl_saved_clip_{idx}")