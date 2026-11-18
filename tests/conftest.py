import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_payload() -> dict:
    return json.loads((FIXTURES / "match_v4_sample.json").read_text(encoding="utf-8"))


@pytest.fixture
def sample_match(sample_payload):
    from clipsvalorant.models import Match

    return Match.model_validate(sample_payload["data"])


@pytest.fixture
def dm_match():
    """A respawn mode (Deathmatch), where per-round kill counts are meaningless."""
    import json

    from clipsvalorant.models import Match

    payload = json.loads(
        (FIXTURES / "match_v4_deathmatch.json").read_text(encoding="utf-8")
    )
    return Match.model_validate(payload["data"])


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A Config pointing at a throwaway database, with no API key needed."""
    from clipsvalorant.config import Config

    return Config(
        henrik_api_key="",
        riot_name="TestMe",
        riot_tag="0001",
        region="eu",
        db_path=tmp_path / "test.db",
    )
