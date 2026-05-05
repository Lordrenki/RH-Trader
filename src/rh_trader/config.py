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
    catalog_base_url: str = "https://ardb.app"
    blueprint_guild_id: int | None = None
    blueprint_channel_id: int | None = None


def load_settings() -> Settings:
    """Load settings from environment variables.

    The function will read a local `.env` file when present.
    """

    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is required to run the bot")

    db_path = os.getenv("TRADER_DB_PATH", "data/trader.db")
    catalog_base_url = os.getenv("CATALOG_BASE_URL", "https://ardb.app")
    guild_id = os.getenv("BLUEPRINT_GUILD_ID")
    channel_id = os.getenv("BLUEPRINT_CHANNEL_ID")
    return Settings(
        discord_token=token,
        database_path=db_path,
        catalog_base_url=catalog_base_url,
        blueprint_guild_id=int(guild_id) if guild_id else None,
        blueprint_channel_id=int(channel_id) if channel_id else None,
    )
