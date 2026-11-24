from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from datetime import timedelta

import streamlit as st

APP_DIR = Path(__file__).parent
WORK_DIR = Path(tempfile.gettempdir()) / "clipstream_jobs"
WORK_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = 500

st.set_page_config(page_title="ClipStream Studio", page_icon="🎬", layout="wide")

st.markdown("""
<style>
:root { color-scheme: dark; }
.stApp { background: #0b0b10; }
[data-testid="stHeader"] { background: rgba(11,11,16,.85); }
.block-container { padding-top: 1.6rem; max-width: 1500px; }
[data-testid="stMetric"] { background:#171722; border:1px solid #29293a; padding:14px; border-radius:14px; }
div[data-testid="stVerticalBlockBorderWrapper"] { background:#14141d; border:1px solid #29293a; border-radius:16px; }
.stButton>button, .stDownloadButton>button { border-radius:10px; font-weight:650; }
.small-muted { color:#9ca3af; font-size:.88rem; }
.hero { padding:20px 22px; border:1px solid #303047; border-radius:18px; background:linear-gradient(115deg,#19172b,#14141d 55%,#211329); }
.hero h1 { margin:0; letter-spacing:-1px; }
</style>
""", unsafe_allow_html=True)


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def run_cmd(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def probe_video(path: str) -> dict:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate,bit_rate", "-of", "json", path]
    p = run_cmd(cmd)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-1500:] or "Could not inspect this video.")
    import json
    data = json.loads(p.stdout)
    streams = data.get("streams", [])
    video = next((x for x in streams if x.get("codec_type") == "video"), {})
    audio = next((x for x in streams if x.get("codec_type") == "audio"), {})
    return {
        "duration": float(data.get("format", {}).get("duration", 0) or 0),
        "size": int(data.get("format", {}).get("size", 0) or 0),
        "width": int(video.get("width", 0) or 0),
        "height": int(video.get("height", 0) or 0),
        "video_codec": video.get("codec_name", "unknown"),
        "audio_codec": audio.get("codec_name", "none"),
        "fps": video.get("r_frame_rate", "unknown"),
    }


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return str(timedelta(seconds=seconds))


def save_uploaded(uploaded, suffix: str) -> Path:
    target = WORK_DIR / f"{next(tempfile._get_candidate_names())}{suffix}"
    uploaded.seek(0)
    with target.open("wb") as out:
        while True:
            chunk = uploaded.read(8 * 1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    return target


def extract_text_from_image(image_file) -> str:
    try:
        from PIL import Image
        import pytesseract
        image = Image.open(image_file)
        return pytesseract.image_to_string(image).strip()
    except Exception as exc:
        raise RuntimeError("Image text extraction is unavailable. Check that Tesseract OCR is installed.") from exc


def translate_to_english(text: str) -> tuple[str, str]:
    if not text.strip():
        return "", "No text to translate."
    try:
        from deep_translator import GoogleTranslator
        translated = GoogleTranslator(source="auto", target="en").translate(text)
        return translated or text, "Translation service used (internet connection required)."
    except Exception:
        return text, "Automatic translation was unavailable. Original text is shown; check your connection or translate manually."


def campaign_presets() -> dict:
    return {
        "Puffco / GTA stream": """ELIGIBLE: genuine positive first-hit and in-session reactions to the device; natural excitement while playing GTA; reactions to jars and first impressions; big hits/clouds; visibly impressed moments from the creator or chat; educational tips, lore and banter from the Hash Butler; positive Puffco sponsor mentions; GTA x Puffco crossover moments.\n\nNOT ELIGIBLE: complaints, bad hits or low vapor; negative sponsorship framing; highly offensive content; off-stream content; posts from before campaign launch; clips with no Puffco device, jar or shoutout; gambling such as slots or blackjack; botted or artificially inflated views.\n\nReview context carefully. Do not infer a positive reaction from an unclear moment. Human approval is required before submission.""",
        "Bichou / Miami streams": """CAMPAIGN: Bichou — dedicated accounts only. Clip only Bichou's Miami streams and replays. Eligible platforms: TikTok, Instagram, YouTube and X. The account bio must contain the platform-specific campaign link obtained from Whop / Risor Tracker. Add the supplied KICK overlay to every video. Include #bichou in the description. At least 50% of views must come from France and engagement must be at least 1.0%. Submit the clip through Whop Content Rewards.\n\nDO NOT: make Bichou or another creator look ridiculous; use content outside the Miami streams; use bots, purchased views, likes, comments or followers, self-engagement, paid boosts or ads. Fake engagement can cause rejection and removal from the campaign.\n\nPAYMENT TERMS IN THE PROVIDED BRIEF: $1 per 1,000 views; $5 minimum payout; $75 maximum payout. Verify current campaign terms in Whop before posting.""",
        "Custom campaign": "",
    }


def build_filter(aspect: str, resolution: str) -> str | None:
    crop = {
        "9:16 Vertical": "crop=ih*9/16:ih",
        "16:9 Landscape": "crop=iw:iw*9/16",
        "1:1 Square": r"crop=min(iw\,ih):min(iw\,ih)",
        "4:5 Portrait": "crop=ih*4/5:ih",
        "Keep original": None,
    }[aspect]
    filters = []
    if crop:
        filters.append(crop)
    if resolution == "1080p target" and aspect != "Keep original":
        dims = {"9:16 Vertical": "1080:1920", "16:9 Landscape": "1920:1080", "1:1 Square": "1080:1080", "4:5 Portrait": "1080:1350"}[aspect]
        filters.append(f"scale={dims}:force_original_aspect_ratio=decrease:flags=lanczos")
        filters.append(f"pad={dims}:(ow-iw)/2:(oh-ih)/2")
    elif resolution == "720p target" and aspect != "Keep original":
        dims = {"9:16 Vertical": "720:1280", "16:9 Landscape": "1280:720", "1:1 Square": "720:720", "4:5 Portrait": "720:900"}[aspect]
        filters.append(f"scale={dims}:force_original_aspect_ratio=decrease:flags=lanczos")
        filters.append(f"pad={dims}:(ow-iw)/2:(oh-ih)/2")
    return ",".join(filters) if filters else None


def render_clip(source: str, output: str, start: float, end: float, aspect: str,
                resolution: str, quality: int, watermark: str | None = None,
                subtitles: str | None = None, burn_subtitles: bool = False) -> None:
    if end <= start:
        raise ValueError("Clip end must be after clip start.")
    vf = build_filter(aspect, resolution)
    args = ["ffmpeg", "-y", "-ss", str(start), "-i", source]
    if watermark:
        args += ["-i", watermark]
    filters = []
    if vf:
        filters.append(f"[0:v]{vf}[base]")
    else:
        filters.append("[0:v]null[base]")
    current = "base"
    if watermark:
        filters.append(f"[1:v]scale=iw*0.18:-1[logo]")
        filters.append(f"[{current}][logo]overlay=W-w-24:24:format=auto[vlogo]")
        current = "vlogo"
    if subtitles and burn_subtitles:
        safe_sub = subtitles.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        filters.append(f"[{current}]subtitles='{safe_sub}'[vout]")
        current = "vout"
    if filters:
        args += ["-filter_complex", ";".join(filters), "-map", f"[{current}]", "-map", "0:a?", "-t", str(end-start)]
    else:
        args += ["-t", str(end-start)]
    args += ["-c:v", "libx264", "-preset", "medium", "-crf", str(quality), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", output]
    p = run_cmd(args)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "FFmpeg failed")[-3500:])


def ensure_state():
    defaults = {"video_path": None, "video_name": None, "video_info": None, "clips": [], "brief_text": "", "brief_english": "", "source_brief": "", "watermark_path": None, "srt_path": None}
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

ensure_state()

st.markdown('<div class="hero"><div style="color:#a5b4fc;font-weight:700;font-size:.78rem;letter-spacing:.12em">CREATOR WORKFLOW • VIDEO INTELLIGENCE</div><h1>🎬 ClipStream Studio</h1><div class="small-muted">Turn long-form streams into polished, campaign-aware short clips.</div></div>', unsafe_allow_html=True)

if not have_ffmpeg():
    st.error("FFmpeg is not installed. Install it locally or deploy with the included packages.txt file.")

with st.sidebar:
    st.title("⚙️ Workspace")
    page = st.radio("Workspace", ["Clipper Studio", "Campaign Brief & Translation", "Compliance Review", "Export Library"])
    st.divider()
    st.caption("Processing limits")
    st.metric("Maximum upload", f"{MAX_UPLOAD_MB} MB")
    st.caption("Actual limit also depends on Streamlit hosting, available RAM, disk space and processing time.")
    st.divider()
    st.caption("Quality tip: source-quality export avoids unnecessary upscaling. Re-encoding cannot recover detail that was not in the original video.")

if page == "Clipper Studio":
    left, right = st.columns([1.55, 1], gap="large")
    with left:
        st.subheader("1. Upload your source video")
        uploaded = st.file_uploader("Choose a video (MP4, MOV, MKV, WEBM)", type=["mp4", "mov", "mkv", "webm", "m4v"], help="Maximum configured size: 500 MB. Large uploads may still be restricted by the hosting provider.")
        if uploaded:
            if uploaded.size > MAX_UPLOAD_MB * 1024 * 1024:
                st.error("This file exceeds the 500 MB application limit.")
            elif st.button("Load video into workspace", type="primary"):
                suffix = Path(uploaded.name).suffix.lower() or ".mp4"
                with st.spinner("Saving and inspecting video..."):
                    path = save_uploaded(uploaded, suffix)
                    try:
                        info = probe_video(str(path))
                        st.session_state.video_path = str(path)
                        st.session_state.video_name = uploaded.name
                        st.session_state.video_info = info
                        st.session_state.clips = []
                        st.success("Video loaded successfully.")
                    except Exception as exc:
                        path.unlink(missing_ok=True)
                        st.error(f"Could not read this video: {exc}")
        if st.session_state.video_path and Path(st.session_state.video_path).exists():
            info = st.session_state.video_info
            st.video(st.session_state.video_path)
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Duration", fmt_time(info["duration"]))
            m2.metric("Resolution", f'{info["width"]} × {info["height"]}')
            m3.metric("Video codec", info["video_codec"])
            m4.metric("Frame rate", info["fps"])
            st.caption(f"Source: {st.session_state.video_name} • {info['size']/1024/1024:.1f} MB • Audio: {info['audio_codec']}")
        else:
            st.info("Upload a video to activate the timeline and clipping controls.")

    with right:
        st.subheader("2. Create a clip")
        if st.session_state.video_info:
            duration = st.session_state.video_info["duration"]
            start = st.number_input("Start time (seconds)", min_value=0.0, max_value=float(max(duration, 0.1)), value=0.0, step=1.0)
            end_default = min(45.0, duration) if duration else 45.0
            end = st.number_input("End time (seconds)", min_value=0.1, max_value=float(max(duration, 0.1)), value=float(max(end_default, 0.1)), step=1.0)
            st.caption(f"Selected duration: {max(0, end-start):.1f} seconds")
            clip_title = st.text_input("Clip name", value=f"{Path(st.session_state.video_name).stem}_clip")
            aspect = st.selectbox("Output format", ["9:16 Vertical", "16:9 Landscape", "1:1 Square", "4:5 Portrait", "Keep original"])
            resolution = st.selectbox("Output resolution", ["Source quality (no target upscale)", "1080p target", "720p target"])
            if resolution == "Source quality (no target upscale)":
                resolution = "Source"
            quality = st.slider("Encoding quality (lower is better)", 16, 28, 18, help="CRF 18 is high quality. CRF 16 creates larger files; CRF 23 is a smaller compromise.")
            logo_upload = st.file_uploader("Optional campaign logo / overlay image", type=["png", "jpg", "jpeg", "webp"], key="logo_upload")
            if logo_upload and st.button("Save overlay image"):
                logo = save_uploaded(logo_upload, Path(logo_upload.name).suffix.lower())
                st.session_state.watermark_path = str(logo)
                st.success("Overlay image saved for exports.")
            srt_upload = st.file_uploader("Optional subtitles (.srt)", type=["srt"], key="srt_upload")
            if srt_upload and st.button("Save subtitle file"):
                srt = save_uploaded(srt_upload, ".srt")
                st.session_state.srt_path = str(srt)
                st.success("Subtitle file saved.")
            burn = st.checkbox("Burn subtitles into the video", value=False, disabled=not bool(st.session_state.srt_path))
            if st.button("✂️ Render actual video clip", type="primary", use_container_width=True):
                if end <= start:
                    st.error("End time must be greater than start time.")
                else:
                    out_path = WORK_DIR / f"{re.sub(r'[^a-zA-Z0-9_-]+','_',clip_title)[:45]}_{len(st.session_state.clips)+1}.mp4"
                    with st.spinner("Rendering clip with FFmpeg. Please keep this page open..."):
                        try:
                            render_clip(st.session_state.video_path, str(out_path), start, end, aspect,
                                        "Source" if resolution == "Source" else resolution, quality,
                                        st.session_state.watermark_path, st.session_state.srt_path, burn)
                            st.session_state.clips.append({"name": clip_title, "path": str(out_path), "start": start, "end": end, "duration": end-start, "format": aspect})
                            st.success(f"Clip rendered: {out_path.stat().st_size/1024/1024:.1f} MB")
                        except Exception as exc:
                            st.error(f"Rendering failed: {exc}")
        else:
            st.info("Load a source video first. You will then be able to set precise in/out timestamps.")

elif page == "Campaign Brief & Translation":
    st.subheader("Campaign brief reader")
    st.write("Paste campaign instructions or upload a screenshot/document image. The app extracts readable text and can translate it into English.")
    preset = st.selectbox("Load an example campaign", list(campaign_presets().keys()))
    if st.button("Load campaign example"):
        st.session_state.source_brief = campaign_presets()[preset]
        st.session_state.brief_text = st.session_state.source_brief
    brief_image = st.file_uploader("Upload campaign screenshot / image", type=["png", "jpg", "jpeg", "webp"])
    if brief_image and st.button("Extract text from image (OCR)"):
        try:
            extracted = extract_text_from_image(brief_image)
            st.session_state.source_brief = extracted
            st.session_state.brief_text = extracted
            st.success("Text extracted from image.")
        except Exception as exc:
            st.error(str(exc))
    brief = st.text_area("Campaign instructions", value=st.session_state.brief_text, height=260, placeholder="Paste the complete campaign brief here...")
    st.session_state.brief_text = brief
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🌐 Translate brief into English", type="primary", use_container_width=True):
            with st.spinner("Detecting language and translating..."):
                translated, note = translate_to_english(brief)
                st.session_state.brief_english = translated
                st.info(note)
    with c2:
        if st.button("Use original as English", use_container_width=True):
            st.session_state.brief_english = brief
    if st.session_state.brief_english:
        st.markdown("#### English version")
        st.text_area("Translated campaign brief", value=st.session_state.brief_english, height=300)
        st.download_button("Download English brief (.txt)", st.session_state.brief_english, file_name="campaign_brief_english.txt", mime="text/plain")

elif page == "Compliance Review":
    st.subheader("Campaign compliance checklist")
    st.caption("This is a review assistant, not an automatic approval system. Confirm every item against the official campaign brief.")
    options = list(campaign_presets().keys())
    campaign = st.selectbox("Campaign", options)
    rules = campaign_presets()[campaign]
    st.text_area("Campaign rules reference", value=rules, height=220, disabled=True)
    st.markdown("#### Review the selected clip")
    selected = st.selectbox("Clip to review", [x["name"] for x in st.session_state.clips] or ["No rendered clips yet"])
    if campaign.startswith("Puffco"):
        checks = ["Clip is from the correct Ben / Puffco GTA stream", "Puffco device, jar or clear sponsor reference is visible or audible", "Moment is genuinely positive and not misleading", "No bad-hit complaint or negative sponsor framing", "No gambling content", "No highly offensive content", "Clip is from after campaign launch", "No botted or purchased engagement is being used"]
    elif campaign.startswith("Bichou"):
        checks = ["Clip comes from Bichou's Miami stream or replay", "Dedicated Bichou account is being used", "Correct campaign link is in the account bio", "KICK overlay is present", "#bichou is in the caption", "Clip does not ridicule Bichou or another creator", "Posted only to an eligible platform", "No fake engagement, paid boosts or ads", "France audience and 1% engagement targets will be monitored"]
    else:
        checks = ["Clip matches the campaign's eligible content", "Clip avoids all prohibited content", "Correct branding and overlays are present", "Caption and account requirements are satisfied", "Source and campaign dates have been verified", "No artificial engagement is being used"]
    checked = []
    for item in checks:
        checked.append(st.checkbox(item, key=f"check_{campaign}_{item}"))
    if checks:
        score = sum(checked)
        st.progress(score / len(checks), text=f"{score} of {len(checks)} review items confirmed")
        if score == len(checks):
            st.success("All checklist items marked complete. This is not a guarantee of campaign acceptance.")
        else:
            st.warning("Resolve the unchecked items before publishing or submitting.")
    st.text_area("Reviewer notes", placeholder="Record uncertain moments, timestamps, or reasons for rejection...")

elif page == "Export Library":
    st.subheader("Rendered clips")
    if not st.session_state.clips:
        st.info("Your rendered clips will appear here.")
    for i, clip in enumerate(st.session_state.clips):
        with st.container(border=True):
            a, b = st.columns([2, 1])
            with a:
                st.markdown(f"**{clip['name']}**")
                st.caption(f"{fmt_time(clip['start'])} – {fmt_time(clip['end'])} • {clip['duration']:.1f}s • {clip['format']}")
                if Path(clip["path"]).exists():
                    st.video(clip["path"])
            with b:
                if Path(clip["path"]).exists():
                    with open(clip["path"], "rb") as f:
                        st.download_button("⬇️ Download MP4", f, file_name=Path(clip["path"]).name, mime="video/mp4", key=f"download_{i}", use_container_width=True)
                    st.caption(f"{Path(clip['path']).stat().st_size/1024/1024:.1f} MB")
                else:
                    st.error("Temporary file is no longer available. Render the clip again.")

st.divider()
st.caption("ClipStream Studio • Use only video and assets you own or are authorized to edit. Campaign eligibility and platform rules must be checked before publishing.")
