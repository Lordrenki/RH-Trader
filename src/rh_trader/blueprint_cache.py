"""Persistence helpers for blueprint trade values."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

from .raider_market import RaiderMarketItem


DEFAULT_BLUEPRINT_CACHE_PATH = Path("data/blueprint_trade_values.json")


def save_blueprint_values(items: list[RaiderMarketItem], path: Path = DEFAULT_BLUEPRINT_CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": [asdict(item) for item in items],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_blueprint_values(path: Path = DEFAULT_BLUEPRINT_CACHE_PATH) -> list[RaiderMarketItem]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    raw_items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        return []

    items: list[RaiderMarketItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        slug = raw.get("slug")
        name = raw.get("name")
        url = raw.get("url")
        if not isinstance(slug, str) or not isinstance(name, str) or not isinstance(url, str):
            continue
        trade_value = raw.get("trade_value")
        game_value = raw.get("game_value")
        items.append(
            RaiderMarketItem(
                slug=slug,
                name=name,
                trade_value=trade_value if isinstance(trade_value, int) else None,
                game_value=game_value if isinstance(game_value, int) else None,
                url=url,
            )
        )
    return items
