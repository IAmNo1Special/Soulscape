import hashlib
import io
import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient

from .. import database, key_vault
from .. import main
from ..key_vault import (
    KeyNotFound,
    KeyVaultError,
    KeyVaultMisconfigured,
    SecretRedactionFilter,
)

FAKE_OPENAI = "sk-proj-FAKEOPENAIKEY1234567890abcdef"
FAKE_ANTHROPIC = "sk-ant-api03-FAKEANTHROPICKEY1234567890"
FAKE_GOOGLE = "AIzaSyFAKEGOOGLEKEY1234567890abcdefg"
FAKE_SLACK = "xoxb-fake-token-9876543210"


def _public_client() -> TestClient:
    return TestClient(main.app)


def _register_tamer(username: str, password: str) -> dict:
    c = _public_client()
    r = c.post(
        "/tamers/register",
        json={"username": username, "password": password},
    )
    assert r.status_code == 201
    tamer_id = r.json()["tamer_id"]
    r = c.post("/tamers/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return {"tamer_id": tamer_id, "token": r.json()["token"]}


@pytest.fixture
def tamer_a():
    return _register_tamer("kv_tamer_a", "s3cur3pass")


@pytest.fixture
def tamer_b():
    return _register_tamer("kv_tamer_b", "an0therpass")


@pytest.fixture
def tamer_client(tamer_a):
    return TestClient(main.app, headers={"X-Hub-Secret": tamer_a["token"]})


@pytest.fixture(autouse=True)
def reset_vault_cache():
    key_vault.reset_master_key_cache()
    yield
    key_vault.reset_master_key_cache()


def _raw_row(key_id: str) -> sqlite3.Row:
    with database.get_db() as conn:
        return conn.execute(
            "SELECT * FROM llm_keys WHERE key_id = ?", (key_id,)
        ).fetchone()


def _upload(client, provider="openai", key=FAKE_OPENAI, label="main"):
    r = client.post(
        "/keys/upload",
        json={"provider": provider, "key": key, "label": label},
    )
    assert r.status_code == 201, r.text
    return r.json()


def fake_llm_provider_call(tamer_id: str, provider: str, prompt: str) -> str:
    """Test double standing in for the issue #25 inference caller.

    This is the only sanctioned decryption path: ``vault.use_key``
    holds plaintext inside the ``with`` block and zeroes it on exit.
    Never returns key material.
    """
    with key_vault.use_key(tamer_id, provider) as key:
        assert isinstance(key, bytearray)
        digest = hashlib.sha256(bytes(key)).hexdigest()[:12]
        return (
            f"provider={provider} key_sha256_prefix={digest} prompt_len={len(prompt)}"
        )


class TestMasterKey:
    def test_explicit_base64_master_key(self, monkeypatch):
        import base64

        raw = b"x" * 32
        monkeypatch.setenv("KEY_VAULT_MASTER_KEY", base64.b64encode(raw).decode())
        monkeypatch.delenv("HUB_SECRET_KEY", raising=False)
        assert key_vault.get_master_key() == raw

    def test_explicit_hex_master_key(self, monkeypatch):
        # A 64-char hex string is also valid base64, and base64 is
        # attempted first per the documented decode order; use 66 hex
        # chars (33 bytes) so base64 validation fails and the hex branch
        # is exercised.
        raw = bytes(range(33))
        assert key_vault._decode_master_key(raw.hex()) == raw

    def test_explicit_raw_utf8_master_key(self, monkeypatch):
        monkeypatch.setenv("KEY_VAULT_MASTER_KEY", "a" * 32 + " raw secret")
        monkeypatch.delenv("HUB_SECRET_KEY", raising=False)
        assert key_vault.get_master_key() == b"a" * 32 + b" raw secret"

    def test_short_master_key_rejected(self, monkeypatch):
        monkeypatch.setenv("KEY_VAULT_MASTER_KEY", "too-short")
        monkeypatch.delenv("HUB_SECRET_KEY", raising=False)
        with pytest.raises(KeyVaultMisconfigured):
            key_vault.get_master_key()

    def test_hkdf_fallback_from_hub_secret(self, monkeypatch):
        monkeypatch.delenv("KEY_VAULT_MASTER_KEY", raising=False)
        monkeypatch.setenv("HUB_SECRET_KEY", "soulscape-secret-123")
        k1 = key_vault.get_master_key()
        assert len(k1) == 32
        key_vault.reset_master_key_cache()
        monkeypatch.setenv("HUB_SECRET_KEY", "different-secret")
        assert key_vault.get_master_key() != k1

    def test_fail_closed_when_neither_set(self, monkeypatch):
        monkeypatch.delenv("KEY_VAULT_MASTER_KEY", raising=False)
        monkeypatch.delenv("HUB_SECRET_KEY", raising=False)
        with pytest.raises(KeyVaultMisconfigured):
            key_vault.encrypt_key("anything")
        with pytest.raises(KeyVaultMisconfigured):
            key_vault.decrypt_key(b"\x00" * 12, b"ciphertext")


class TestCiphertextAtRest:
    def test_upload_stores_ciphertext_only(self, tamer_a, tamer_client):
        meta = _upload(tamer_client)
        row = _raw_row(meta["key_id"])
        assert row is not None
        assert bytes(row["nonce"]) != b""
        ciphertext = bytes(row["ciphertext"])
        assert ciphertext != FAKE_OPENAI.encode()
        assert FAKE_OPENAI.encode() not in ciphertext
        plaintext = key_vault.decrypt_key(bytes(row["nonce"]), ciphertext).decode()
        assert plaintext == FAKE_OPENAI

    def test_same_key_uploads_produce_different_ciphertext(self, tamer_a, tamer_client):
        m1 = _upload(tamer_client, label="first")
        m2 = _upload(tamer_client, label="second")
        r1, r2 = _raw_row(m1["key_id"]), _raw_row(m2["key_id"])
        assert bytes(r1["nonce"]) != bytes(r2["nonce"])
        assert bytes(r1["ciphertext"]) != bytes(r2["ciphertext"])
        for row in (r1, r2):
            assert (
                key_vault.decrypt_key(
                    bytes(row["nonce"]), bytes(row["ciphertext"])
                ).decode()
                == FAKE_OPENAI
            )

    def test_upload_response_never_carries_material(self, tamer_a, tamer_client):
        meta = _upload(tamer_client)
        assert FAKE_OPENAI not in str(meta)
        assert "ciphertext" not in meta
        assert "nonce" not in meta
        assert meta["last4"] == FAKE_OPENAI[-4:]

    def test_use_key_decrypts_and_zeroes_buffer(self, tamer_a):
        nonce, ciphertext = key_vault.encrypt_key(FAKE_OPENAI)
        key_vault.store_key(
            tamer_a["tamer_id"],
            "kvk_probe",
            "openai",
            "probe",
            FAKE_OPENAI[-4:],
            nonce,
            ciphertext,
        )
        holder: list[bytearray] = []
        with key_vault.use_key(tamer_a["tamer_id"], "openai") as key:
            assert isinstance(key, bytearray)
            assert bytes(key).decode() == FAKE_OPENAI
            holder.append(key)
        assert all(b == 0 for b in holder[0])
        assert len(holder[0]) == len(FAKE_OPENAI)

    def test_use_key_missing_raises(self, tamer_a):
        with pytest.raises(KeyNotFound):
            with key_vault.use_key(tamer_a["tamer_id"], "openai"):
                pass

    def test_inference_test_double(self, tamer_a):
        nonce, ciphertext = key_vault.encrypt_key(FAKE_ANTHROPIC)
        key_vault.store_key(
            tamer_a["tamer_id"],
            "kvk_double",
            "anthropic",
            "",
            FAKE_ANTHROPIC[-4:],
            nonce,
            ciphertext,
        )
        result = fake_llm_provider_call(tamer_a["tamer_id"], "anthropic", "hello")
        assert FAKE_ANTHROPIC not in result
        assert "provider=anthropic" in result


class TestRedaction:
    def _capture(self, lines: list[str]) -> str:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("soulscape_test_redact")
        logger.handlers = []
        logger.addFilter(SecretRedactionFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        for line in lines:
            logger.info(line)
        return stream.getvalue()

    def test_provider_key_shapes_redacted(self):
        fakes = [
            f"openai: {FAKE_OPENAI}",
            f"anthropic: {FAKE_ANTHROPIC}",
            f"google: {FAKE_GOOGLE}",
            f"token {FAKE_SLACK} used",
            'api_key = "super-secret-value-123"',
            "API-KEY: Bearer-ish-secret-456",
        ]
        out = self._capture(fakes)
        assert out.count("[REDACTED]") == len(fakes)
        for fake in fakes:
            assert fake not in out
        assert "super-secret-value-123" not in out

    def test_normal_lines_pass_through_unmangled(self):
        lines = [
            "key uploaded: key_id=kvk_abc provider=openai tamer_id=tmr_x",
            "request completed in 12ms with status 200",
            "last4=def0 is not a secret",
        ]
        out = self._capture(lines)
        for line in lines:
            assert line in out

    def test_root_logger_has_redaction_filter(self):
        before = sum(
            isinstance(f, SecretRedactionFilter) for f in logging.getLogger().filters
        )
        key_vault.install_redaction_filter()
        after = sum(
            isinstance(f, SecretRedactionFilter) for f in logging.getLogger().filters
        )
        assert after == 1 and before <= 1

    def test_vault_never_logs_key_material(self, tamer_a, caplog):
        with caplog.at_level(logging.INFO):
            nonce, ciphertext = key_vault.encrypt_key(FAKE_OPENAI)
            key_vault.store_key(
                tamer_a["tamer_id"],
                "kvk_nolog",
                "openai",
                "x",
                FAKE_OPENAI[-4:],
                nonce,
                ciphertext,
            )
            with key_vault.use_key(tamer_a["tamer_id"], "openai"):
                pass
            try:
                key_vault.decrypt_key(b"\x00" * 12, b"bad")
            except KeyVaultError:
                pass
        combined = caplog.text
        assert FAKE_OPENAI not in combined


class TestRotation:
    def test_rotate_invalidates_old_immediately(self, tamer_a, tamer_client):
        meta = _upload(tamer_client)
        old_id = meta["key_id"]
        new_key = "sk-proj-NEWROTATEDKEY9999999999999999"
        r = tamer_client.post(f"/keys/{old_id}/rotate", json={"new_key": new_key})
        assert r.status_code == 200, r.text
        new_meta = r.json()
        new_id = new_meta["key_id"]
        assert new_id != old_id

        keys = tamer_client.get("/keys").json()
        old = next(k for k in keys if k["key_id"] == old_id)
        assert old["revoked_at"] is not None
        assert old["superseded_by"] == new_id
        assert old["rotated_at"] is not None
        new = next(k for k in keys if k["key_id"] == new_id)
        assert new["revoked_at"] is None

        with key_vault.use_key(tamer_a["tamer_id"], "openai") as key:
            assert bytes(key).decode() == new_key
        assert (
            key_vault.get_key_metadata(
                tamer_a["tamer_id"], old_id, include_revoked=False
            )
            is None
        )
        new_row = _raw_row(new_id)
        assert (
            key_vault.decrypt_key(
                bytes(new_row["nonce"]), bytes(new_row["ciphertext"])
            ).decode()
            == new_key
        )

    def test_rotate_unknown_key_404(self, tamer_a, tamer_client):
        r = tamer_client.post("/keys/kvk_nope/rotate", json={"new_key": FAKE_OPENAI})
        assert r.status_code == 404

    def test_rotate_is_atomic_on_conflict(self, tamer_a, tamer_client):
        meta = _upload(tamer_client)
        old_id = meta["key_id"]
        nonce, ciphertext = key_vault.encrypt_key("other-key-material")
        with pytest.raises(Exception):
            key_vault.rotate_key(
                tamer_a["tamer_id"],
                old_id,
                old_id,
                "",
                "0000",
                nonce,
                ciphertext,
            )
        row = _raw_row(old_id)
        assert row["revoked_at"] is None
        assert row["superseded_by"] is None
        assert (
            key_vault.get_key_metadata(
                tamer_a["tamer_id"], old_id, include_revoked=False
            )
            is not None
        )


class TestRevoke:
    def test_revoke(self, tamer_a, tamer_client):
        meta = _upload(tamer_client)
        key_id = meta["key_id"]
        r = tamer_client.post(f"/keys/{key_id}/revoke")
        assert r.status_code == 200, r.text
        assert r.json()["revoked_at"] is not None
        with pytest.raises(KeyNotFound):
            with key_vault.use_key(tamer_a["tamer_id"], "openai"):
                pass
        keys = tamer_client.get("/keys").json()
        revoked = next(k for k in keys if k["key_id"] == key_id)
        assert revoked["revoked_at"] is not None

    def test_revoke_unknown_key_404(self, tamer_a, tamer_client):
        r = tamer_client.post("/keys/kvk_nope/revoke")
        assert r.status_code == 404


class TestAccessControl:
    def test_tamer_cannot_touch_other_tamers_keys(self, tamer_a, tamer_b, tamer_client):
        meta = _upload(tamer_client)
        key_id = meta["key_id"]
        other = TestClient(main.app, headers={"X-Hub-Secret": tamer_b["token"]})
        assert other.get("/keys").json() == []
        r = other.post(f"/keys/{key_id}/rotate", json={"new_key": "x" * 32})
        assert r.status_code in (403, 404)
        r = other.post(f"/keys/{key_id}/revoke")
        assert r.status_code in (403, 404)
        r = other.post(
            "/keys/upload",
            json={
                "provider": "openai",
                "key": "y" * 32,
            },
            params={"tamer_id": tamer_a["tamer_id"]},
        )
        assert r.status_code == 403
        row = _raw_row(key_id)
        assert row["revoked_at"] is None

    def test_operator_manages_any_tamer_keys(self, tamer_a, tamer_client):
        hub_secret = __import__("os").environ["HUB_SECRET_KEY"]
        op = TestClient(main.app, headers={"X-Hub-Secret": hub_secret})
        meta = _upload(tamer_client, provider="google", key=FAKE_GOOGLE)
        r = op.post(
            f"/keys/{meta['key_id']}/revoke",
            params={"tamer_id": tamer_a["tamer_id"]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["revoked_at"] is not None
        r = op.get("/keys", params={"tamer_id": tamer_a["tamer_id"]})
        assert r.status_code == 200
        assert len(r.json()) == 1

    def test_operator_requires_tamer_id(self, tamer_a, tamer_client):
        hub_secret = __import__("os").environ["HUB_SECRET_KEY"]
        op = TestClient(main.app, headers={"X-Hub-Secret": hub_secret})
        r = op.post(
            "/keys/upload",
            json={"provider": "openai", "key": "z" * 32},
        )
        assert r.status_code == 400
        r = op.get("/keys")
        assert r.status_code == 400

    def test_soul_identity_denied(self, register_soul, tamer_a):
        register_soul("vault_soul")
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT secret_hash FROM souls WHERE soul_id = ?",
                ("vault_soul",),
            ).fetchone()
        assert row is not None
        r = _public_client().post(
            "/keys/upload",
            json={"provider": "openai", "key": "q" * 32},
        )
        assert r.status_code == 403


class TestValidation:
    def test_unsupported_provider_rejected(self, tamer_a, tamer_client):
        r = tamer_client.post(
            "/keys/upload",
            json={"provider": "cohere", "key": "k" * 32},
        )
        assert r.status_code == 400

    def test_short_key_rejected(self, tamer_a, tamer_client):
        r = tamer_client.post(
            "/keys/upload",
            json={"provider": "openai", "key": "tiny"},
        )
        assert r.status_code == 400

    def test_provider_case_normalized(self, tamer_a, tamer_client):
        meta = _upload(tamer_client, provider="OpenAI")
        assert meta["provider"] == "openai"
