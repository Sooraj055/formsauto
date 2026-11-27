"""Tests for encrypted profile persistence, PII redaction, and duplicate guard."""

from pathlib import Path

from app.config import FRONTEND_DIR, redact_sensitive, validate_google_form_url
from app.storage import SecureStorage


def test_encrypted_profile_seed_and_no_plaintext_in_db(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FORMRUSH_DEFAULT_NAME", "Test User")
    monkeypatch.setenv("FORMRUSH_DEFAULT_MOBILE", "9876543210")
    monkeypatch.setenv("FORMRUSH_DEFAULT_EMAIL", "testuser@example.com")
    monkeypatch.setenv("FORMRUSH_DEFAULT_TICKETS", "4")
    monkeypatch.setenv("FORMRUSH_DEFAULT_CITY", "Cochin")
    monkeypatch.setenv("FORMRUSH_DEFAULT_ADDRESS", "Sample Locality")
    monkeypatch.setenv("FORMRUSH_DEFAULT_PIN", "682001")

    db_file = tmp_path / "test.db"
    key_file = tmp_path / ".secret.key"
    storage = SecureStorage(db_path=db_file, key_path=key_file)

    profile = storage.get_profile()
    assert profile.full_name == "Test User"
    assert profile.mobile_number == "9876543210"
    assert profile.email == "testuser@example.com"
    assert profile.preferred_ticket_quantity == 4
    assert profile.place_city == "Cochin"
    assert profile.address_locality == "Sample Locality"
    assert profile.pin_code == "682001"
    assert not hasattr(profile, "theatre_preference")

    raw_db = db_file.read_bytes()
    assert b"Test User" not in raw_db
    assert b"9876543210" not in raw_db
    assert b"testuser@example.com" not in raw_db
    assert b"Sample Locality" not in raw_db


def test_frontend_contains_zero_pii() -> None:
    sensitive = ["Test User", "9876543210", "testuser@example.com", "Sample Locality"]
    for path in FRONTEND_DIR.iterdir():
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            for token in sensitive:
                assert token.lower() not in content.lower(), f"PII '{token}' found in {path.name}"


def test_url_validation_and_whatsapp_extraction() -> None:
    ok, norm, _ = validate_google_form_url(
        "Registrations open! Fill fast: https://docs.google.com/forms/d/e/1FAIpQLScTestForm/viewform?usp=sf_link"
    )
    assert ok
    assert norm == "https://docs.google.com/forms/d/e/1FAIpQLScTestForm/viewform?usp=sf_link"

    ok_short, norm_short, _ = validate_google_form_url("https://forms.gle/abc123XYZ")
    assert ok_short
    assert norm_short == "https://forms.gle/abc123XYZ"

    bad, _, err = validate_google_form_url("https://evil.example.com/phishing")
    assert not bad
    assert "Unsupported domain" in err


def test_non_fernet_secret_is_stable_across_reopen(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FORMRUSH_SECRET_KEY", "render-generated-secret-not-a-fernet-key")
    db_file = tmp_path / "stable.db"
    key_file = tmp_path / ".unused.key"
    first = SecureStorage(db_path=db_file, key_path=key_file)
    saved = first.get_profile()
    saved.full_name = "Persisted Name"
    first.save_profile(saved)

    second = SecureStorage(db_path=db_file, key_path=key_file)
    assert second.get_profile().full_name == "Persisted Name"


def test_pii_redaction_and_pin_auth(tmp_path: Path) -> None:
    msg = "Failed for Test User at 9876543210 (testuser@example.com)"
    redacted = redact_sensitive(msg, ["Test User", "9876543210", "testuser@example.com"])
    assert "Test User" not in redacted
    assert "9876543210" not in redacted
    assert "testuser@example.com" not in redacted

    storage = SecureStorage(db_path=tmp_path / "pin.db", key_path=tmp_path / ".pin.key")
    assert not storage.has_pin()
    storage.set_pin("8080")
    assert storage.has_pin()
    assert storage.verify_pin("8080")
    assert not storage.verify_pin("0000")
    storage.set_pin(None)
    assert not storage.has_pin()
