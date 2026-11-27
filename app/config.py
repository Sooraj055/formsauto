"""Application configuration, URL validation, and PII redaction helpers."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("FORMRUSH_DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "formrush.db"
KEY_PATH = DATA_DIR / ".secret.key"

FRONTEND_DIR = BASE_DIR / "frontend"

# Timeouts tuned for speed in first-come, first-served 8 PM registrations
PAGE_GOTO_TIMEOUT_MS = int(os.getenv("FORMRUSH_GOTO_TIMEOUT_MS", "15000"))
ACTION_TIMEOUT_MS = int(os.getenv("FORMRUSH_ACTION_TIMEOUT_MS", "5000"))
SUBMIT_TIMEOUT_MS = int(os.getenv("FORMRUSH_SUBMIT_TIMEOUT_MS", "15000"))
PAUSED_SESSION_TTL_SECONDS = int(os.getenv("FORMRUSH_SESSION_TTL", "600"))

ALLOWED_GOOGLE_FORM_HOSTS = {
    "docs.google.com",
    "forms.gle",
}


def is_local_test_url_allowed() -> bool:
    """Allow localhost URLs when running automated tests or local dev fixtures."""
    return os.getenv("FORMRUSH_ALLOW_LOCAL_URLS", "0") == "1"


def validate_google_form_url(raw_url: str) -> tuple[bool, str, str]:
    """Validate and normalize a Google Forms URL.

    Returns (is_valid, normalized_url, error_message).
    """
    if not raw_url or not isinstance(raw_url, str):
        return False, "", "Please paste a Google Forms URL."

    cleaned = raw_url.strip()
    # Extract URL if pasted from WhatsApp message containing surrounding text
    url_match = re.search(r"https?://[^\s<>\"']+", cleaned)
    if url_match:
        cleaned = url_match.group(0).rstrip(".,);!")

    try:
        parsed = urlparse(cleaned)
    except Exception:
        return False, "", "Invalid URL format."

    if parsed.scheme not in {"http", "https"}:
        return False, "", "URL must start with https:// or http://."

    host = (parsed.hostname or "").lower()
    if not host:
        return False, "", "URL is missing a hostname."

    if host in {"127.0.0.1", "localhost"}:
        if not is_local_test_url_allowed():
            return False, "", "Only public Google Forms URLs (docs.google.com or forms.gle) are supported."
        return True, cleaned, ""

    if host not in ALLOWED_GOOGLE_FORM_HOSTS:
        return (
            False,
            "",
            "Unsupported domain. Please enter a Google Forms link (docs.google.com/forms or forms.gle).",
        )

    if host == "docs.google.com" and "/forms/" not in parsed.path:
        return False, "", "This docs.google.com link does not point to a Google Form (/forms/...)."

    if host == "forms.gle" and len(parsed.path.strip("/")) < 3:
        return False, "", "Incomplete forms.gle short link."

    return True, cleaned, ""


def canonicalize_form_key(url: str) -> str:
    """Produce a stable canonical identifier for duplicate-submission tracking."""
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return url.strip()

    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")

    # Normalize /viewform or /formResponse suffix on docs.google.com
    m = re.match(r"^(/forms/d/(?:e/)?[^/]+)(?:/.*)?$", path)
    if m:
        path = m.group(1)

    return f"{host}{path}"


def redact_sensitive(text: str, values_to_redact: list[str] | None = None) -> str:
    """Scrub personal information from log lines or error messages."""
    if not text:
        return text
    sanitized = text
    if values_to_redact:
        for val in values_to_redact:
            v = (val or "").strip()
            if len(v) >= 3:
                sanitized = re.sub(re.escape(v), "[REDACTED]", sanitized, flags=re.IGNORECASE)
    # Also redact email addresses and 10-digit phone numbers generically
    sanitized = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", sanitized)
    sanitized = re.sub(r"\b\d{10}\b", "[REDACTED_PHONE]", sanitized)
    return sanitized


def configure_logging() -> logging.Logger:
    """Configure concise application logging without exposing personal data."""
    logging.basicConfig(
        level=os.getenv("FORMRUSH_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return logging.getLogger("formrush")


logger = configure_logging()
