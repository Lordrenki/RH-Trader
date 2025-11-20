"""Configuration helpers for the bot."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass
class Settings:
    """Runtime settings loaded from the environment."""

    discord_token: str
    database_path: str = "data/trader.db"


def load_settings() -> Settings:
    """Load settings from environment variables.

    The function will read a local `.env` file when present.
    """

    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is required to run the bot")

    db_path = os.getenv("TRADER_DB_PATH", "data/trader.db")
    return Settings(discord_token=token, database_path=db_path)
