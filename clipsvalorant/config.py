"""Configuration, loaded from a .env file at the repo root or from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

VALID_REGIONS = {"eu", "na", "ap", "kr", "latam", "br"}


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE pairs from .env into os.environ without clobbering real env vars."""
    path = path or REPO_ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Config:
    henrik_api_key: str
    riot_name: str
    riot_tag: str
    region: str
    db_path: Path
    twitch_client_id: str = ""
    twitch_client_secret: str = ""

    @property
    def has_twitch(self) -> bool:
        return bool(self.twitch_client_id and self.twitch_client_secret)

    @property
    def riot_id(self) -> str:
        return f"{self.riot_name}#{self.riot_tag}"


def _resolve_db_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def load_config(*, require_api_key: bool = True) -> Config:
    """Build a Config, raising ConfigError with an actionable message on anything missing.

    Pass require_api_key=False for commands that only touch the local database.
    """
    load_dotenv()

    missing: list[str] = []
    key = os.environ.get("HENRIK_API_KEY", "").strip()
    name = os.environ.get("RIOT_NAME", "").strip()
    tag = os.environ.get("RIOT_TAG", "").strip().lstrip("#")
    region = os.environ.get("RIOT_REGION", "eu").strip().lower()
    db_raw = os.environ.get("DB_PATH", "data/clipsvalorant.db").strip()

    if require_api_key and (not key or key.startswith("HDEV-xxxx")):
        missing.append("HENRIK_API_KEY")
    if not name:
        missing.append("RIOT_NAME")
    if not tag:
        missing.append("RIOT_TAG")

    if missing:
        raise ConfigError(
            "Missing configuration: "
            + ", ".join(missing)
            + f"\n\nCopy .env.example to .env and fill it in:\n  cp .env.example .env"
            f"\n\nThe file lives at {REPO_ROOT / '.env'}"
        )

    if region not in VALID_REGIONS:
        raise ConfigError(
            f"RIOT_REGION={region!r} is not one of {sorted(VALID_REGIONS)}"
        )

    return Config(
        henrik_api_key=key,
        riot_name=name,
        riot_tag=tag,
        region=region,
        db_path=_resolve_db_path(db_raw),
        twitch_client_id=os.environ.get("TWITCH_CLIENT_ID", "").strip(),
        twitch_client_secret=os.environ.get("TWITCH_CLIENT_SECRET", "").strip(),
    )
