"""Settings loaded from the environment (and an optional .env file)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    seats_aero_api_key: str
    database_path: Path
    # Optional so award-only features work before a SerpApi account exists.
    serpapi_api_key: str | None = None
    # ntfy alerts. The topic acts as a password on the public server, so keep it secret.
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"


def load_settings(env_file: Path | str | None = ".env") -> Settings:
    """Read settings, loading `env_file` first if given.

    Variables already set in the real environment win over the file.
    """
    if env_file is not None:
        load_dotenv(env_file)

    api_key = os.environ.get("SEATS_AERO_API_KEY", "").strip()
    if not api_key:
        raise ConfigError(
            "SEATS_AERO_API_KEY is not set. Copy .env.example to .env and fill it in."
        )

    return Settings(
        seats_aero_api_key=api_key,
        database_path=Path(os.environ.get("DATABASE_PATH", "flight_optimizer.db")),
        serpapi_api_key=os.environ.get("SERPAPI_API_KEY", "").strip() or None,
        ntfy_topic=os.environ.get("NTFY_TOPIC", "").strip() or None,
        ntfy_server=os.environ.get("NTFY_SERVER", "").strip().rstrip("/") or "https://ntfy.sh",
    )
