"""Tests for shared.env.load_app_env.

Pins the single .env contract every entry point shares: exported
environment variables always win over .env values, and .env only
fills in variables that are not already set.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shared.env import load_app_env

DOTENV_SECRET = "dotenv-secret-value-1234"
EXPORT_SECRET = "exported-secret-value-5678"


@pytest.fixture()
def dotenv_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(f"HUB_SECRET_KEY={DOTENV_SECRET}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HUB_SECRET_KEY", raising=False)
    return env_file


def test_exported_env_wins_over_dotenv(
    dotenv_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUB_SECRET_KEY", EXPORT_SECRET)
    assert load_app_env() is True
    assert os.environ["HUB_SECRET_KEY"] == EXPORT_SECRET


def test_dotenv_fills_unset_variable(dotenv_file: Path) -> None:
    assert load_app_env() is True
    assert os.environ["HUB_SECRET_KEY"] == DOTENV_SECRET


def test_missing_dotenv_file_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HUB_SECRET_KEY", raising=False)
    assert load_app_env() is False
    assert "HUB_SECRET_KEY" not in os.environ
