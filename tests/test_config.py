from __future__ import annotations

import pytest

from bigbot.config import ConfigurationError, load_settings


def test_defaults_are_safe_and_ai_is_optional(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (
        "DISCORD_TOKEN",
        "X_BEARER_TOKEN",
        "OPENROUTER_API_KEY",
        "BIG_GUILD_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert settings.discord_token is None
    assert settings.openrouter_api_key is None
    assert settings.ai_web_search
    assert settings.ai_zdr
    assert settings.openrouter_model == "openrouter/auto"


def test_run_requires_discord_token(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    with pytest.raises(ConfigurationError, match="DISCORD_TOKEN"):
        load_settings(require_discord=True)


def test_invalid_boolean_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIG_AI_ZDR", "sometimes")
    with pytest.raises(ConfigurationError, match="true or false"):
        load_settings()
