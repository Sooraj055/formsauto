# FormRush — Smart Google Forms Rapid Autofiller

Mobile-first web application built with **FastAPI**, **Playwright (Chromium)**, and **AES-encrypted SQLite** for rapid, first-come, first-served Google Forms registrations.

## Key Capabilities

- **Auto-Fill + Auto-Submit Mode**: Opens the Google Form in a warm headless Chromium instance, extracts questions and options, matches against your encrypted profile, fills every field, verifies population, clicks **Submit**, and confirms the Google Forms response screen without manual approval.
- **Dynamic Question Detection**: Supports short-answer, paragraph, multiple-choice radio buttons (`role="radiogroup"`), custom Google Forms dropdowns (`role="listbox"` and `<select>`), checkboxes (`role="checkbox"`), required indicators, and multi-page forms.
- **Intelligent Rule-Based Matching**: Instant (<1 ms) wording normalization, synonym matching, exclusion guards, and confidence thresholds — no external AI API latency at 8 PM.
- **Safe Pause & Resume**: Automatically pauses and requests your input only when a required question is missing, ambiguous, or a preferred option (such as ticket quantity `4`) is unavailable. Submitting your answer resumes the already-open form page without restarting.
- **Security & Duplicate Guard**:
  - Personal data is stored encrypted with Fernet (AES-128-CBC + HMAC-SHA256) in SQLite (`data/formrush.db`) and never embedded in frontend source files, URLs, logs, or error messages.
  - Duplicate submission guard prevents double submissions and blocks blind retries after uncertain timeouts.
  - Optional Access PIN lock (`FORMRUSH_ACCESS_PIN` or in-app Security setting) protects public cloud deployments.
  - Does not bypass CAPTCHAs, Google sign-in gates, or access controls; reports blockers with a manual browser link.

## Local Setup (Windows / macOS / Linux)

```bash
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium
```

Start the server:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` (or your machine's LAN IP on your Android phone, e.g. `http://192.168.1.x:8000`).

## Running the Test Suite

```bash
pytest -v
```

Tests verify:
- Rule-based question & option matching (`tests/test_matcher.py`)
- Encrypted profile storage, zero PII in frontend source, URL validation, PIN auth, and PII log redaction (`tests/test_storage.py`)
- End-to-end Playwright + FastAPI flows against realistic Google Forms DOM fixtures (`tests/test_e2e_forms.py`):
  - Full auto-fill + auto-submit without manual approval
  - Duplicate submission protection
  - Unexpected required question pause & live resume
  - Unavailable ticket quantity pause (never silently picks a different quantity)
  - Multi-page `Next` -> `Submit` flow
  - CAPTCHA blocker detection

## Cloud Deployment (HTTPS + Android Access)

Because FormRush runs headless Chromium on the backend, deploy using Docker on any container host with HTTPS enabled:

### Option 1: Render (Blueprint included)
1. Push this repository to GitHub/GitLab.
2. In Render, choose **New -> Blueprint** and select the repo (`render.yaml` is already configured with a persistent `/app/data` disk and `/api/health` check).
3. Set `FORMRUSH_ACCESS_PIN` in Render Environment Variables so only you can access your profile from Android.
4. Open the generated `https://<your-app>.onrender.com` URL on Android Chrome and tap **Add to Home screen** for 1-tap access before 8 PM.

### Option 2: Railway / Fly.io / VPS with Docker
```bash
docker build -t formrush .
docker run -d \
  -p 8000:8000 \
  -e FORMRUSH_SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
  -e FORMRUSH_ACCESS_PIN="your-secret-pin" \
  -v formrush_data:/app/data \
  formrush
```
Place behind a TLS reverse proxy (Caddy, Nginx + Certbot, or Cloudflare Tunnel) for HTTPS access on Android.

## Using FormRush at 8:00 PM

1. Open FormRush a minute before 8 PM (keeps your session unlocked and confirms **Browser Ready**).
2. Copy the Google Forms link from WhatsApp.
3. Tap **Paste** -> **Analyze / Start**.
4. If all required questions match confidently, FormRush fills, validates, submits, and shows the Google Forms confirmation automatically.
5. If a new or ambiguous required question appears, enter it in the **Action Needed** card and tap **Fill & Submit** (optionally toggle *Save this answer to profile* for future forms).
