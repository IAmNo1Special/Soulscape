"""Encrypted key vault for BYO LLM provider keys.

Ciphertext-only at rest: provider keys are encrypted with AES-256-GCM
under a deployment master key and stored as (nonce, ciphertext) blobs.
Plaintext never touches the database, logs, or exceptions.

Master key sourcing (first match wins):
  1. ``KEY_VAULT_MASTER_KEY`` env var. Accepted formats, any of which
     must decode to at least 32 bytes (decode order: base64, then hex,
     then raw UTF-8 — so an all-hex-digit string reads as base64 unless
     it fails base64 validation):
       - base64 of >= 32 bytes
       - hex of >= 32 bytes
       - raw UTF-8 of >= 32 characters
  2. ``HUB_SECRET_KEY`` env var, stretched with HKDF-SHA256
     (info ``b"soulscape-key-vault/v1"``) to 32 bytes.
If neither yields a key, every vault operation fails closed with
``KeyVaultMisconfigured`` (mapped to 503 by the router).

Supported providers (allowlist, validated on upload): openai, anthropic,
google.

Decryption is confined to ``use_key()``: the only call path that ever
holds plaintext. It yields a ``bytearray`` (not ``str``) and zeroes the
buffer on exit. Best-effort caveat: CPython may keep transient copies in
the AESGCM decrypt output before it is copied into the bytearray; there
is no plaintext caching, no plaintext in return values, and key material
never appears in log records or exception messages.

Custody: a tamer manages only their own keys. Operators may manage any
tamer's keys by passing an explicit ``tamer_id``; tamers always resolve
to their own identity.
"""

import base64
import binascii
import contextlib
import logging
import os
import re
import secrets
import time
from typing import Iterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import database

MASTER_KEY_ENV = "KEY_VAULT_MASTER_KEY"
HKDF_INFO = b"soulscape-key-vault/v1"
MIN_MASTER_KEY_BYTES = 32
NONCE_BYTES = 12
KEY_ID_PREFIX = "kvk_"
PROVIDERS = ("openai", "anthropic", "google")
MIN_KEY_LENGTH = 8
MAX_LABEL_LENGTH = 64


class KeyVaultError(Exception):
    pass


class KeyVaultMisconfigured(KeyVaultError):
    pass


class KeyNotFound(KeyVaultError):
    pass


_master_key_cache: bytes | None = None


def reset_master_key_cache() -> None:
    global _master_key_cache
    _master_key_cache = None


def _decode_master_key(value: str) -> bytes:
    value = value.strip()
    candidates: list[bytes] = []
    try:
        candidates.append(base64.b64decode(value, validate=True))
    except (binascii.Error, ValueError):
        pass
    try:
        candidates.append(bytes.fromhex(value))
    except ValueError:
        pass
    candidates.append(value.encode("utf-8"))
    for candidate in candidates:
        if len(candidate) >= MIN_MASTER_KEY_BYTES:
            return candidate
    raise KeyVaultMisconfigured(
        f"{MASTER_KEY_ENV} must decode to at least {MIN_MASTER_KEY_BYTES} bytes"
    )


def get_master_key() -> bytes:
    global _master_key_cache
    if _master_key_cache is not None:
        return _master_key_cache
    explicit = os.environ.get(MASTER_KEY_ENV)
    if explicit:
        _master_key_cache = _decode_master_key(explicit)
        return _master_key_cache
    hub_secret = os.environ.get("HUB_SECRET_KEY")
    if not hub_secret:
        raise KeyVaultMisconfigured(
            "Key vault misconfigured: neither "
            f"{MASTER_KEY_ENV} nor HUB_SECRET_KEY is set"
        )
    _master_key_cache = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=HKDF_INFO,
    ).derive(hub_secret.encode("utf-8"))
    return _master_key_cache


def encrypt_key(plaintext: str) -> tuple[bytes, bytes]:
    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = AESGCM(get_master_key()).encrypt(
        nonce, plaintext.encode("utf-8"), None
    )
    return nonce, ciphertext


def decrypt_key(nonce: bytes, ciphertext: bytes) -> bytes:
    try:
        return AESGCM(get_master_key()).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise KeyVaultError("Decryption failed") from exc


def new_key_id() -> str:
    return KEY_ID_PREFIX + secrets.token_urlsafe(16)


def store_key(
    tamer_id: str,
    key_id: str,
    provider: str,
    label: str,
    last4: str,
    nonce: bytes,
    ciphertext: bytes,
    created_at: float | None = None,
) -> None:
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO llm_keys "
            "(key_id, tamer_id, provider, label, last4, nonce, ciphertext, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key_id,
                tamer_id,
                provider,
                label,
                last4,
                nonce,
                ciphertext,
                created_at if created_at is not None else time.time(),
            ),
        )
        conn.commit()


def get_key_metadata(
    tamer_id: str, key_id: str, include_revoked: bool = True
) -> dict | None:
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT key_id, tamer_id, provider, label, last4, created_at, "
            "rotated_at, revoked_at, superseded_by FROM llm_keys "
            "WHERE key_id = ? AND tamer_id = ?",
            (key_id, tamer_id),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        if not include_revoked and data["revoked_at"] is not None:
            return None
        return data


def list_key_metadata(tamer_id: str) -> list[dict]:
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT key_id, tamer_id, provider, label, last4, created_at, "
            "rotated_at, revoked_at, superseded_by FROM llm_keys "
            "WHERE tamer_id = ? ORDER BY created_at",
            (tamer_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def _active_key_row(tamer_id: str, provider: str) -> dict | None:
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT nonce, ciphertext FROM llm_keys "
            "WHERE tamer_id = ? AND provider = ? AND revoked_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (tamer_id, provider),
        ).fetchone()
        return dict(row) if row else None


def rotate_key(
    tamer_id: str,
    old_key_id: str,
    new_key_id: str,
    label: str,
    last4: str,
    nonce: bytes,
    ciphertext: bytes,
) -> None:
    now = time.time()
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT key_id FROM llm_keys "
                "WHERE key_id = ? AND tamer_id = ? AND revoked_at IS NULL",
                (old_key_id, tamer_id),
            )
            if cursor.fetchone() is None:
                raise KeyNotFound(old_key_id)
            cursor.execute(
                "INSERT INTO llm_keys "
                "(key_id, tamer_id, provider, label, last4, nonce, "
                "ciphertext, created_at) "
                "SELECT ?, tamer_id, provider, ?, ?, ?, ?, ? FROM llm_keys "
                "WHERE key_id = ? AND tamer_id = ?",
                (
                    new_key_id,
                    label,
                    last4,
                    nonce,
                    ciphertext,
                    now,
                    old_key_id,
                    tamer_id,
                ),
            )
            cursor.execute(
                "UPDATE llm_keys SET revoked_at = ?, rotated_at = ?, "
                "superseded_by = ? WHERE key_id = ? AND revoked_at IS NULL",
                (now, now, new_key_id, old_key_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def revoke_key(tamer_id: str, key_id: str) -> dict | None:
    now = time.time()
    with database.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE llm_keys SET revoked_at = ? "
            "WHERE key_id = ? AND tamer_id = ? AND revoked_at IS NULL",
            (now, key_id, tamer_id),
        )
        conn.commit()
    return get_key_metadata(tamer_id, key_id)


@contextlib.contextmanager
def use_key(tamer_id: str, provider: str) -> Iterator[bytearray]:
    row = _active_key_row(tamer_id, provider)
    if row is None:
        raise KeyNotFound(f"No active {provider} key for tamer")
    plaintext = decrypt_key(bytes(row["nonce"]), bytes(row["ciphertext"]))
    buf = bytearray(plaintext)
    del plaintext
    try:
        yield buf
    finally:
        for i in range(len(buf)):
            buf[i] = 0


class SecretRedactionFilter(logging.Filter):
    _PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
        (re.compile(r"sk-[A-Za-z0-9_\-]{10,}"), "[REDACTED]"),
        (re.compile(r"AIza[A-Za-z0-9_\-]{20,}"), "[REDACTED]"),
        (re.compile(r"xox[baprs]-[A-Za-z0-9_\-]{6,}"), "[REDACTED]"),
        (
            re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)(['\"]?)([^\s'\",;]+)"),
            r"\1\2[REDACTED]",
        ),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for pattern, replacement in self._PATTERNS:
            message = pattern.sub(replacement, message)
        record.msg = message
        record.args = None
        return True


def install_redaction_filter() -> SecretRedactionFilter:
    root = logging.getLogger()
    for existing in root.filters:
        if isinstance(existing, SecretRedactionFilter):
            return existing
    redactor = SecretRedactionFilter()
    root.addFilter(redactor)
    return redactor
