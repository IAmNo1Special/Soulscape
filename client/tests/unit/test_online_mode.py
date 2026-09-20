"""Unit tests for the explicit offline/online client mode (issue #15).

Covers: mode setting default and round-trip, HUB_URL no longer acting as
a mode switch, Hub-address resolution, store selection, online/offline
persistence routing, secret stripping on the disk path, and JSON
hardening (corrupt-file quarantine, .bak on overwrite).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

import pytest

from client.core import stores as stores_pkg
from client.core.stores.local_store import LocalStore
from client.core.stores.remote_store import RemoteStore
from client.system import persistence


@pytest.fixture
def appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("HUB_URL", raising=False)
    return tmp_path


class TestModeSetting:
    def test_default_mode_is_offline(self, appdata):
        settings = persistence.load_settings()
        assert settings["mode"] == "offline"
        assert persistence.get_client_mode() == "offline"

    def test_mode_roundtrip(self, appdata):
        settings = persistence.load_settings()
        settings["mode"] = "online"
        assert persistence.save_settings(settings) is True
        assert persistence.get_client_mode() == "online"

    def test_invalid_mode_falls_back_to_offline(self, appdata):
        settings = persistence.load_settings()
        settings["mode"] = "bogus"
        persistence.save_settings(settings)
        assert persistence.get_client_mode() == "offline"

    def test_env_hub_url_is_not_a_mode_switch(self, appdata, monkeypatch):
        monkeypatch.setenv("HUB_URL", "http://localhost:9785")
        assert persistence.get_client_mode() == "offline"

    def test_resolve_hub_url_prefers_env(self, appdata, monkeypatch):
        monkeypatch.setenv("HUB_URL", "http://hub.example:9785")
        assert persistence.resolve_hub_url() == "http://hub.example:9785"

    def test_resolve_hub_url_falls_back_to_setting(self, appdata):
        settings = persistence.load_settings()
        settings["hub_url"] = "http://custom:9999"
        persistence.save_settings(settings)
        assert persistence.resolve_hub_url() == "http://custom:9999"


class TestStoreSelection:
    def test_offline_gets_local_store(self, appdata):
        assert isinstance(stores_pkg.get_default_store(), LocalStore)

    def test_online_gets_remote_store(self, appdata, monkeypatch):
        monkeypatch.setattr(persistence, "get_client_mode", lambda: "online")
        assert isinstance(stores_pkg.get_default_store(), RemoteStore)


class TestPersistenceRouting:
    def test_offline_saves_to_local_json(self, appdata):
        souls = [{"soul_id": "a", "name": "A"}]
        assert persistence.save_souls(souls) is True
        assert persistence.get_souls_file().exists()
        loaded = persistence.load_souls()
        assert [s["soul_id"] for s in loaded] == ["a"]

    def test_online_save_posts_to_hub(self, appdata, monkeypatch):
        monkeypatch.setattr(persistence, "get_client_mode", lambda: "online")
        sent: dict = {}

        class FakeClient:
            async def post_souls(self, souls, owner_id=""):
                sent["souls"] = souls
                sent["owner_id"] = owner_id
                return {"status": "success"}

        monkeypatch.setattr("client.system.network.client.NetworkClient", FakeClient)
        assert persistence.save_souls([{"soul_id": "a", "name": "A"}]) is True
        assert sent["souls"] == [{"soul_id": "a", "name": "A"}]
        assert not persistence.get_souls_file().exists()

    def test_online_load_reads_from_hub(self, appdata, monkeypatch):
        monkeypatch.setattr(persistence, "get_client_mode", lambda: "online")

        class FakeClient:
            async def get_souls(self):
                return [{"soul_id": "hub-soul"}]

        monkeypatch.setattr("client.system.network.client.NetworkClient", FakeClient)
        loaded = persistence.load_souls()
        assert [s["soul_id"] for s in loaded] == ["hub-soul"]


class TestSecretsNeverHitDisk:
    def test_save_souls_strips_secrets(self, appdata):
        secret = "topsecret-uuid-1234-abcdef"
        souls = [
            {"soul_id": "a", "secret": secret, "name": "A"},
            {"soul_id": "b", "secret": "other-secret", "name": "B"},
        ]
        assert persistence.save_souls(souls) is True
        raw = persistence.get_souls_file().read_bytes()
        assert secret.encode() not in raw
        assert b"other-secret" not in raw
        assert b'"secret"' not in raw
        data = json.loads(raw)
        assert all("secret" not in soul for soul in data)

    def test_loaded_souls_regenerate_secret(self, appdata):
        from client.core.soul.soul import Soul

        souls = [{"soul_id": "a", "secret": "disk-must-not-keep", "name": "A"}]
        persistence.save_souls(souls)
        loaded = persistence.load_souls()
        soul = Soul.from_dict(
            data=loaded[0],
            soul_registry=[],
            screen_width=100,
            screen_height=100,
            local_instance_id="tester",
        )
        try:
            assert soul.secret != "disk-must-not-keep"
            assert soul.secret
        finally:
            soul.stop()


class TestJsonHardening:
    def test_corrupt_souls_json_is_quarantined(self, appdata):
        souls_file = persistence.get_souls_file()
        souls_file.parent.mkdir(parents=True, exist_ok=True)
        souls_file.write_text("{not valid json", encoding="utf-8")
        assert persistence.load_souls() == []
        quarantined = list(souls_file.parent.glob("souls.json.corrupt-*"))
        assert len(quarantined) == 1
        assert not souls_file.exists()

    def test_corrupt_settings_falls_back_to_defaults(self, appdata):
        settings_file = persistence.get_settings_file()
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text("[broken", encoding="utf-8")
        settings = persistence.load_settings()
        assert settings["mode"] == "offline"
        assert settings["opacity"] == 100
        assert list(settings_file.parent.glob("settings.json.corrupt-*"))

    def test_overwrite_keeps_bak(self, appdata):
        persistence.save_settings({"opacity": 10, "mode": "offline"})
        first = persistence.get_settings_file().read_text(encoding="utf-8")
        persistence.save_settings({"opacity": 20, "mode": "offline"})
        bak = persistence.get_settings_file().with_name("settings.json.bak")
        assert bak.read_text(encoding="utf-8") == first

    def test_souls_overwrite_keeps_bak(self, appdata):
        persistence.save_souls([{"soul_id": "a"}])
        first = persistence.get_souls_file().read_text(encoding="utf-8")
        persistence.save_souls([{"soul_id": "b"}])
        bak = persistence.get_souls_file().with_name("souls.json.bak")
        assert bak.read_text(encoding="utf-8") == first


class TestLocalStoreHardening(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        if self._old_appdata is None:
            self.addCleanup(os.environ.pop, "APPDATA", None)
        else:
            self.addCleanup(os.environ.__setitem__, "APPDATA", self._old_appdata)

    async def test_corrupt_json_returns_empty_and_quarantines(self):
        store = LocalStore()
        path = store._get_file("marketplace.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{nope", encoding="utf-8")
        assert await store.load_marketplace() == {}
        assert list(path.parent.glob("marketplace.json.corrupt-*"))

    async def test_save_roundtrip_and_bak(self):
        store = LocalStore()
        assert await store.save_marketplace({"listings": []}) is True
        assert await store.load_marketplace() == {"listings": []}
        path = store._get_file("marketplace.json")
        first = path.read_text(encoding="utf-8")
        assert await store.save_marketplace({"listings": [1]}) is True
        bak = path.with_name("marketplace.json.bak")
        assert bak.read_text(encoding="utf-8") == first


class TestOnlineModeDoesNotTouchLocalStores(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        if self._old_appdata is None:
            self.addCleanup(os.environ.pop, "APPDATA", None)
        else:
            self.addCleanup(os.environ.__setitem__, "APPDATA", self._old_appdata)
        self._old_mode = persistence.get_client_mode

    def tearDown(self):
        persistence.get_client_mode = self._old_mode

    async def test_online_get_default_store_never_returns_local(self):
        persistence.get_client_mode = lambda: "online"
        store = stores_pkg.get_default_store()
        assert not isinstance(store, LocalStore)
        assert isinstance(store, RemoteStore)
