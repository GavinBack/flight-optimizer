from pathlib import Path

import pytest

from flight_optimizer.config import ConfigError, load_settings


def test_loads_key_and_default_database_path(monkeypatch):
    monkeypatch.setenv("SEATS_AERO_API_KEY", "abc123")
    monkeypatch.delenv("DATABASE_PATH", raising=False)

    settings = load_settings(env_file=None)

    assert settings.seats_aero_api_key == "abc123"
    assert settings.database_path == Path("flight_optimizer.db")


def test_serpapi_key_is_optional(monkeypatch):
    monkeypatch.setenv("SEATS_AERO_API_KEY", "abc123")
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    assert load_settings(env_file=None).serpapi_api_key is None

    monkeypatch.setenv("SERPAPI_API_KEY", "  ")
    assert load_settings(env_file=None).serpapi_api_key is None

    monkeypatch.setenv("SERPAPI_API_KEY", "serp-key")
    assert load_settings(env_file=None).serpapi_api_key == "serp-key"


def test_database_path_can_be_overridden(monkeypatch):
    monkeypatch.setenv("SEATS_AERO_API_KEY", "abc123")
    monkeypatch.setenv("DATABASE_PATH", "custom.db")

    assert load_settings(env_file=None).database_path == Path("custom.db")


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_or_blank_key_raises(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SEATS_AERO_API_KEY", raising=False)
    else:
        monkeypatch.setenv("SEATS_AERO_API_KEY", value)

    with pytest.raises(ConfigError, match="SEATS_AERO_API_KEY"):
        load_settings(env_file=None)


def test_reads_env_file(monkeypatch, tmp_path):
    # load_dotenv writes to os.environ directly; setenv-then-delenv makes
    # monkeypatch remove whatever it sets when the test ends.
    monkeypatch.setenv("SEATS_AERO_API_KEY", "placeholder")
    monkeypatch.delenv("SEATS_AERO_API_KEY")
    env_file = tmp_path / ".env"
    env_file.write_text("SEATS_AERO_API_KEY=from-file\n")

    assert load_settings(env_file=env_file).seats_aero_api_key == "from-file"


def test_the_claude_advisor_is_optional_and_its_model_can_be_chosen(monkeypatch):
    monkeypatch.setenv("SEATS_AERO_API_KEY", "abc123")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

    settings = load_settings(env_file=None)
    assert settings.anthropic_api_key is None and settings.anthropic_model == "claude-sonnet-5"

    monkeypatch.setenv("ANTHROPIC_API_KEY", " sk-test ")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5")
    settings = load_settings(env_file=None)
    assert settings.anthropic_api_key == "sk-test" and settings.anthropic_model == "claude-opus-5"
