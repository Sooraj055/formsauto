"""Encrypted SQLite storage for profile data, registration attempts, and duplicate prevention."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from cryptography.fernet import Fernet, InvalidToken

from app.config import DB_PATH, KEY_PATH, logger
from app.models import CustomProfileField, HistoryEntry, UserProfile


def _coerce_fernet_key(secret: str) -> bytes:
    """Accept a Fernet key or any other stable secret.

    Render's generated environment values are not Fernet tokens. Deriving a
    key from the secret keeps the database readable across restarts as long
    as FORMRUSH_SECRET_KEY itself does not change.
    """
    raw = secret.encode("utf-8")
    try:
        Fernet(raw)
        return raw
    except Exception:
        return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())


def _default_seed_profile() -> UserProfile:
    """Return initial profile from environment variables or safe generic placeholders."""
    return UserProfile(
        full_name=os.getenv("FORMRUSH_DEFAULT_NAME", "Applicant Name"),
        mobile_number=os.getenv("FORMRUSH_DEFAULT_MOBILE", "9000000000"),
        email=os.getenv("FORMRUSH_DEFAULT_EMAIL", "user@example.com"),
        preferred_ticket_quantity=int(os.getenv("FORMRUSH_DEFAULT_TICKETS", "4")),
        place_city=os.getenv("FORMRUSH_DEFAULT_CITY", "Sample City"),
        address_locality=os.getenv("FORMRUSH_DEFAULT_ADDRESS", "Sample Locality"),
        pin_code=os.getenv("FORMRUSH_DEFAULT_PIN", "600001"),
        custom_fields=[],
    )


class SecureStorage:
    """Thread-safe encrypted SQLite persistence layer."""

    def __init__(self, db_path: Path = DB_PATH, key_path: Path = KEY_PATH) -> None:
        self.db_path = db_path
        self.key_path = key_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet = self._load_or_create_fernet()
        self._init_db()
        self._ensure_seed_profile()

    def _load_or_create_fernet(self) -> Fernet:
        env_key = os.getenv("FORMRUSH_SECRET_KEY", "").strip()
        if env_key:
            return Fernet(_coerce_fernet_key(env_key))
        if self.key_path.exists():
            raw = self.key_path.read_bytes().strip()
            return Fernet(raw)
        key = Fernet.generate_key()
        self.key_path.write_bytes(key)
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass
        return Fernet(key)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_store (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    encrypted_payload BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS registration_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT UNIQUE NOT NULL,
                    canonical_key TEXT NOT NULL,
                    url_hash TEXT NOT NULL,
                    form_title TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    total_questions INTEGER NOT NULL DEFAULT 0,
                    matched_count INTEGER NOT NULL DEFAULT 0,
                    filled_count INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    elapsed_ms INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_canonical ON registration_history(canonical_key)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _ensure_seed_profile(self) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM profile_store WHERE id = 1").fetchone()
            if row is None:
                self.save_profile(_default_seed_profile())
                logger.info("Initialized encrypted profile store.")

    def get_profile(self) -> UserProfile:
        with self._connect() as conn:
            row = conn.execute("SELECT encrypted_payload FROM profile_store WHERE id = 1").fetchone()
        if not row:
            profile = _default_seed_profile()
            self.save_profile(profile)
            return profile
        try:
            decrypted = self._fernet.decrypt(row["encrypted_payload"])
            data = json.loads(decrypted.decode("utf-8"))
            return UserProfile(**data)
        except (InvalidToken, Exception) as exc:
            logger.error("Failed to decrypt profile store: %s", type(exc).__name__)
            raise RuntimeError("Profile decryption failed. Verify FORMRUSH_SECRET_KEY.") from exc

    def save_profile(self, profile: UserProfile) -> UserProfile:
        payload = json.dumps(profile.model_dump(), ensure_ascii=False).encode("utf-8")
        encrypted = self._fernet.encrypt(payload)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO profile_store (id, encrypted_payload, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    encrypted_payload = excluded.encrypted_payload,
                    updated_at = excluded.updated_at
                """,
                (encrypted, now),
            )
            conn.commit()
        return profile

    def add_or_update_custom_field(self, label: str, value: str, aliases: list[str] | None = None) -> UserProfile:
        profile = self.get_profile()
        slug = "".join(ch if ch.isalnum() else "_" for ch in label.lower()).strip("_")[:48] or "custom_field"
        existing = next((f for f in profile.custom_fields if f.key == slug or f.label.lower() == label.lower()), None)
        if existing:
            existing.value = value
            if aliases:
                merged = list(dict.fromkeys([*existing.aliases, *aliases]))
                existing.aliases = merged
        else:
            profile.custom_fields.append(
                CustomProfileField(
                    key=slug,
                    label=label.strip(),
                    value=value.strip(),
                    aliases=aliases or [label.strip().lower()],
                )
            )
        return self.save_profile(profile)

    def record_attempt(
        self,
        run_id: str,
        canonical_key: str,
        form_url: str,
        form_title: str,
        status: str,
        total_questions: int,
        matched_count: int,
        filled_count: int,
        message: str,
        elapsed_ms: int,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        url_hash = hashlib.sha256(form_url.encode("utf-8")).hexdigest()[:16]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO registration_history (
                    run_id, canonical_key, url_hash, form_title, status,
                    total_questions, matched_count, filled_count, message,
                    elapsed_ms, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    form_title = CASE WHEN excluded.form_title != '' THEN excluded.form_title ELSE registration_history.form_title END,
                    status = excluded.status,
                    total_questions = excluded.total_questions,
                    matched_count = excluded.matched_count,
                    filled_count = excluded.filled_count,
                    message = excluded.message,
                    elapsed_ms = excluded.elapsed_ms,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    canonical_key,
                    url_hash,
                    form_title,
                    status,
                    total_questions,
                    matched_count,
                    filled_count,
                    message[:400],
                    elapsed_ms,
                    now,
                    now,
                ),
            )
            conn.commit()

    def get_previous_form_status(self, canonical_key: str) -> HistoryEntry | None:
        """Check if this form already has a submitted or uncertain/unknown attempt."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, run_id, canonical_key, form_title, status, total_questions,
                       matched_count, filled_count, message, elapsed_ms, created_at, updated_at
                FROM registration_history
                WHERE canonical_key = ? AND status IN ('submitted', 'uncertain_timeout', 'unknown')
                ORDER BY id DESC
                LIMIT 1
                """,
                (canonical_key,),
            ).fetchone()
        return HistoryEntry(**dict(row)) if row else None

    def list_history(self, limit: int = 20) -> list[HistoryEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, canonical_key, form_title, status, total_questions,
                       matched_count, filled_count, message, elapsed_ms, created_at, updated_at
                FROM registration_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [HistoryEntry(**dict(r)) for r in rows]

    # --- Optional Access PIN protection for public deployments ---

    def has_pin(self) -> bool:
        if os.getenv("FORMRUSH_ACCESS_PIN", "").strip():
            return True
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = 'pin_hash'").fetchone()
        return row is not None

    def verify_pin(self, pin: str) -> bool:
        env_pin = os.getenv("FORMRUSH_ACCESS_PIN", "").strip()
        if env_pin:
            return secrets.compare_digest(pin.strip(), env_pin)
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = 'pin_hash'").fetchone()
        if not row:
            return True
        salt, expected = row["value"].split(":", 1)
        digest = hashlib.pbkdf2_hmac("sha256", pin.strip().encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
        return secrets.compare_digest(digest, expected)

    def set_pin(self, new_pin: str | None) -> None:
        with self._connect() as conn:
            if not new_pin:
                conn.execute("DELETE FROM app_settings WHERE key = 'pin_hash'")
            else:
                salt = secrets.token_hex(16)
                digest = hashlib.pbkdf2_hmac(
                    "sha256", new_pin.strip().encode("utf-8"), salt.encode("utf-8"), 100_000
                ).hex()
                conn.execute(
                    """
                    INSERT INTO app_settings (key, value) VALUES ('pin_hash', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (f"{salt}:{digest}",),
                )
            conn.commit()
