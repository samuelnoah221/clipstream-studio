# ClipStream Studio

A Streamlit video clipping workspace with FFmpeg rendering, campaign brief OCR/translation, and human-reviewed compliance checklists.

## Features
- Upload videos up to the configured 500 MB Streamlit application limit.
- Inspect video duration, dimensions, codecs and frame rate with FFprobe.
- Create real MP4 clips from start/end timestamps.
- Export vertical, landscape, square, portrait, or original aspect ratio.
- Optional image overlay and SRT subtitle burn-in.
- Read campaign brief text from screenshots using Tesseract OCR.
- Translate campaign text into English using Deep Translator (internet required).
- Campaign-specific review checklist presets for Puffco/GTA and Bichou/Miami.

## Requirements
- Python 3.10+
- FFmpeg and FFprobe
- Tesseract OCR (only for screenshot OCR)

## Run locally on Windows PowerShell
```powershell
cd clipstream_studio
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```
Install FFmpeg and Tesseract, and ensure both executables are on PATH. Then:
```powershell
streamlit run app.py
```

## Deploy on Streamlit Community Cloud
1. Push this folder to a GitHub repository.
2. In Streamlit Community Cloud, create an app from that repository.
3. Set the main file path to `app.py` (or `clipstream_studio/app.py` if this folder is nested).
4. The `packages.txt` file installs FFmpeg and Tesseract on supported Streamlit build environments.
5. `.streamlit/config.toml` sets Streamlit's application upload limit to 500 MB.

## Important hosting limitation
`maxUploadSize = 500` configures Streamlit's upload limit; it does **not** guarantee that a hosted instance can reliably accept, retain, or process a 500 MB video. Hosting proxy limits, available RAM, temporary disk, CPU and execution time also apply. For dependable 500 MB production uploads, use object storage and a separate background video worker (for example, S3-compatible storage plus a job worker) rather than relying on one small web container.

## Quality notes
- The renderer uses H.264, AAC, CRF 18 and `+faststart` by default.
- Lower CRF means higher quality and larger files.
- Re-encoding can introduce generation loss. It cannot restore detail missing from the source.
- Aspect-ratio conversion crops the frame. Review the result before publishing.

## Scope and limitations
This first release performs real timestamp-based clipping and rendering. Campaign compliance is a checklist for human review, not guaranteed AI classification. The translation and OCR tools require their respective dependencies; translation also requires internet access. Do not use fake engagement or violate platform/campaign rules.
