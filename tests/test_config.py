import pytest

from clipsvalorant.config import ConfigError, load_config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("HENRIK_API_KEY", "RIOT_NAME", "RIOT_TAG", "RIOT_REGION", "DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    # Stop a real .env in the repo root from leaking into these tests.
    monkeypatch.setattr("clipsvalorant.config.load_dotenv", lambda *a, **k: None)


def test_missing_values_are_named(monkeypatch):
    with pytest.raises(ConfigError) as excinfo:
        load_config()
    message = str(excinfo.value)
    assert "HENRIK_API_KEY" in message
    assert "RIOT_NAME" in message
    assert "RIOT_TAG" in message


def test_api_key_optional_for_local_commands(monkeypatch):
    monkeypatch.setenv("RIOT_NAME", "PlayerOne")
    monkeypatch.setenv("RIOT_TAG", "NA1")
    cfg = load_config(require_api_key=False)
    assert cfg.riot_id == "PlayerOne#NA1"


def test_placeholder_key_is_rejected(monkeypatch):
    monkeypatch.setenv("HENRIK_API_KEY", "HDEV-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
    monkeypatch.setenv("RIOT_NAME", "PlayerOne")
    monkeypatch.setenv("RIOT_TAG", "NA1")
    with pytest.raises(ConfigError):
        load_config()


def test_leading_hash_on_tag_is_stripped(monkeypatch):
    monkeypatch.setenv("RIOT_NAME", "PlayerOne")
    monkeypatch.setenv("RIOT_TAG", "#NA1")
    assert load_config(require_api_key=False).riot_tag == "NA1"


def test_bad_region_rejected(monkeypatch):
    monkeypatch.setenv("RIOT_NAME", "PlayerOne")
    monkeypatch.setenv("RIOT_TAG", "NA1")
    monkeypatch.setenv("RIOT_REGION", "antarctica")
    with pytest.raises(ConfigError):
        load_config(require_api_key=False)
