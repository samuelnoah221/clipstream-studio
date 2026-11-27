# ClipStream Studio — Consolidated Final Build

## Main features
- Campaign-first workspace with campaign name, pasted requirements and uploaded briefs.
- Reads accessible public HTML campaign links and extracts text from PDF, DOCX, TXT, MD, CSV and image files.
- AI campaign analysis: deliverables, target platforms, clip length, required moments, restrictions, branding, hashtags, links, assets and pre-publish checklist.
- Video upload and metadata inspection.
- AI video analysis: audio transcription using OpenAI Whisper, sampled video frames using FFmpeg, and visual/transcript matching using an OpenAI vision-capable chat model.
- Time-coded highlight suggestions with rationale and compliance concerns.
- Manual editing and batch rendering of selected AI highlights.
- Platform aspect-ratio templates, logo overlay, generated subtitle burn-in and MP4 export.
- Brand Kit, review checklist and publishing workspace with official platform destinations.
- Optional Streamlit OpenID Connect authentication.

## Setup on Windows
1. Install FFmpeg and add its `bin` folder to PATH. Confirm `ffmpeg -version` and `ffprobe -version`.
2. Install Tesseract OCR and ensure `tesseract.exe` is on PATH if you need OCR from images.
3. Open PowerShell in this folder and create a virtual environment:
   `py -3.12 -m venv .venv`
4. Activate:
   `.venv\Scripts\Activate.ps1`
5. Install:
   `python -m pip install --upgrade pip`
   `pip install -r requirements.txt`
6. Set your OpenAI key for this PowerShell session:
   `$env:OPENAI_API_KEY="YOUR_API_KEY"`
7. Run:
   `streamlit run app.py`

## Streamlit Community Cloud
- Put app.py, requirements.txt, packages.txt and .streamlit/config.toml at the repository root.
- Add `OPENAI_API_KEY = "your-key"` in App settings → Secrets. Do not commit secrets to GitHub.
- Select app.py as the main file.

## Authentication
Real sign-in/sign-out is optional. Configure a Streamlit-supported OpenID Connect provider under `[auth]` in Streamlit secrets. The app deliberately does not store or manage plain-text passwords.

## Social publishing
This version renders and exports platform-specific videos and links to the official publishing dashboards. Direct publishing is not switched on: each social platform requires its own developer app, OAuth flow, scopes, review and API-specific publishing implementation.

## Important production limitations
- AI features require an OpenAI API key and incur API charges.
- The app samples up to 20 frames for visual context; this is not exhaustive frame-by-frame understanding.
- AI timestamps and campaign interpretation require human review.
- Streamlit Community Cloud's 500 MB upload setting does not guarantee processing capacity for every 500 MB video.
- Files are stored in temporary workspace storage. For a commercial multi-user deployment, add persistent object storage, a database, background workers, job queues, rate limits, and per-user access controls.
# ClipStream Studio — External SRT + Workspace Isolation Upgrade

## What's new
- Upload a full-length `.srt` beside the loaded source video.
- Auto-extracted stitched clips crop and retime subtitle cues from the exact source moments, including non-contiguous stitched moments.
- Manual clips use the matching section of the full-video SRT.
- Each extracted clip has an optional clip-specific `.srt` uploader for externally generated subtitles timed from 00:00.
- Section 4 is now a separate **Videos Extracted** page, away from upload/extraction controls.
- Workspaces are generated per Streamlit browser session, not from IP address or a typed email. The old global saved-workspace browser is removed.

## Important isolation note
This is session isolation, not a verified Google sign-in system. A real account-based login needs an identity provider (OIDC/Auth0/Google OAuth) and authenticated user ID. Streamlit Community Cloud local files are ephemeral; users should download finished clips and subtitle files.

## Deploy
Replace `app.py` and `packages.txt` in the existing project folder, keep `requirements.txt` and Streamlit secrets unchanged, then commit and push.
