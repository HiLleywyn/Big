from __future__ import annotations

import pytest

from bigbot.config import ConfigurationError, load_app_config, load_settings


def test_defaults_are_safe_and_ai_is_optional(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (
        "DISCORD_TOKEN",
        "X_BEARER_TOKEN",
        "OPENROUTER_API_KEY",
        "OPENROUTER_API_KEY_FILE",
        "BIG_GUILD_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert settings.discord_token is None
    assert settings.openrouter_api_key is None
    assert settings.ai_web_search
    assert settings.ai_zdr
    assert settings.openrouter_model == "deepseek/deepseek-v4-flash-0731"
    assert settings.related_story_limit == 8


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


def test_invalid_related_story_limit_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIG_RELATED_STORY_LIMIT", "21")
    with pytest.raises(ConfigurationError, match="BIG_RELATED_STORY_LIMIT"):
        load_settings()


def test_openrouter_key_can_be_loaded_from_secret_file(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    key_file = tmp_path / "openrouter.secret"
    key_file.write_text("private-test-value\n", encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY_FILE", str(key_file))

    assert load_settings().openrouter_api_key == "private-test-value"


def test_yaml_feed_configuration(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
guild_id: 1
forum_channel_id: 2
clustering:
  threshold: 0.7
  window_hours: 48
feeds:
  - name: Wire
    publisher: Wire Service
    url: https://example.com/rss
    summarization_enabled: false
    default_tags: [World]
""",
        encoding="utf-8",
    )
    config = load_app_config(path)
    assert config.clustering.threshold == 0.7
    assert config.clustering.window_hours == 48
    assert config.feeds[0].default_tags == ("World",)
    assert not config.feeds[0].summarization_enabled


def test_yaml_retention_configuration(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
retention:
  clear_after_days: 14
  action: delete
  batch_size: 10
""",
        encoding="utf-8",
    )
    config = load_app_config(path)
    assert config.retention.clear_after_days == 14
    assert config.retention.action == "delete"
    assert config.retention.batch_size == 10


def test_yaml_rejects_invalid_retention_action(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
retention:
  clear_after_days: 14
  action: vaporize
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match=r"retention\.action"):
        load_app_config(path)


def test_yaml_rejects_string_boolean(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
update_behavior:
  post_major_updates: "false"
feeds:
  - name: Wire
    url: https://example.com/rss
    interval_seconds: 30
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="true or false"):
        load_app_config(path)
