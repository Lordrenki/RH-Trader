from pathlib import Path

from rh_trader.blueprint_cache import load_blueprint_values, save_blueprint_values
from rh_trader.raider_market import RaiderMarketItem


def test_save_and_load_blueprint_values(tmp_path: Path) -> None:
    cache_path = tmp_path / "blueprint_cache.json"
    items = [
        RaiderMarketItem(
            slug="alpha-blueprint",
            name="Alpha Blueprint",
            trade_value=1234,
            game_value=800,
            url="https://raidermarket.com/item/alpha-blueprint",
        )
    ]

    save_blueprint_values(items, cache_path)
    loaded = load_blueprint_values(cache_path)

    assert loaded == items


def test_load_blueprint_values_handles_invalid_json(tmp_path: Path) -> None:
    cache_path = tmp_path / "blueprint_cache.json"
    cache_path.write_text("not-json", encoding="utf-8")

    assert load_blueprint_values(cache_path) == []
