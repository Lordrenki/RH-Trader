"""RaiderMarket scraping helpers."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable

import aiohttp
from bs4 import BeautifulSoup

BROWSE_URL = "https://raidermarket.com/browse"


@dataclass(frozen=True)
class RaiderMarketItem:
    slug: str
    name: str
    trade_value: int | None
    game_value: int | None
    url: str


def _parse_int(value: str) -> int | None:
    digits = "".join(ch for ch in value if ch.isdigit())
    return int(digits) if digits else None


def _extract_labeled_value(text: str, label: str) -> int | None:
    if label not in text:
        return None
    _, after = text.split(label, 1)
    after = after.strip()
    first_token = after.split(" ", 1)[0] if after else ""
    return _parse_int(first_token)


def _extract_name(text: str) -> str:
    for marker in ("Game Value", "Trade Value"):
        if marker in text:
            return text.split(marker, 1)[0].strip()
    return text.strip()


def _extract_slug(href: str) -> str | None:
    match = re.match(r"^/item/([^/?#]+)", href)
    if not match:
        return None
    return match.group(1).strip()


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        return _parse_int(value)
    return None


def _extract_slug_from_record(record: dict[str, Any]) -> str | None:
    slug = record.get("slug") or record.get("itemSlug") or record.get("id")
    if isinstance(slug, str) and slug.strip():
        return slug.strip()
    url = record.get("url") or record.get("href")
    if isinstance(url, str):
        return _extract_slug(url.strip())
    nested = record.get("item") or record.get("itemData") or record.get("itemInfo")
    if isinstance(nested, dict):
        return _extract_slug_from_record(nested)
    return None


def _extract_name_from_record(record: dict[str, Any]) -> str | None:
    name = (
        record.get("name")
        or record.get("itemName")
        or record.get("displayName")
        or record.get("title")
    )
    if isinstance(name, str) and name.strip():
        return name.strip()
    nested = record.get("item") or record.get("itemData") or record.get("itemInfo")
    if isinstance(nested, dict):
        nested_name = nested.get("name") or nested.get("itemName") or nested.get("displayName")
        if isinstance(nested_name, str) and nested_name.strip():
            return nested_name.strip()
    return None


def _iter_item_records(data: Any) -> Iterable[dict[str, Any]]:
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from _iter_item_records(value)
    elif isinstance(data, list):
        for entry in data:
            yield from _iter_item_records(entry)


def _extract_metric_from_record(
    record: dict[str, Any],
    keys: Iterable[str],
    *,
    nested_keys: Iterable[str] = ("item", "itemData", "itemInfo", "values", "pricing"),
) -> int | None:
    for key in keys:
        value = record.get(key)
        if value is not None:
            return _coerce_int(value)
    for nested_key in nested_keys:
        nested = record.get(nested_key)
        if isinstance(nested, dict):
            for key in keys:
                value = nested.get(key)
                if value is not None:
                    return _coerce_int(value)
    return None


def _parse_items_from_json(data: Any) -> dict[str, RaiderMarketItem]:
    items: dict[str, RaiderMarketItem] = {}
    for record in _iter_item_records(data):
        slug = _extract_slug_from_record(record)
        name = _extract_name_from_record(record)
        if not slug or not name:
            continue
        trade_value = _extract_metric_from_record(
            record,
            (
                "tradeValue",
                "trade_value",
                "trade",
                "tradevalue",
                "tradeValueScrap",
                "trade_value_scrap",
                "tradeValueInScrap",
                "tradeValueRaw",
                "trade_value_raw",
                "tradeValueNumber",
                "tradeValueAmount",
            ),
        )
        game_value = _extract_metric_from_record(
            record,
            (
                "gameValue",
                "game_value",
                "game",
                "gamevalue",
                "gameValueScrap",
                "game_value_scrap",
                "gameValueInScrap",
                "gameValueRaw",
                "game_value_raw",
                "gameValueNumber",
                "gameValueAmount",
            ),
        )
        items.setdefault(
            slug,
            RaiderMarketItem(
                slug=slug,
                name=name,
                trade_value=trade_value,
                game_value=game_value,
                url=f"https://raidermarket.com/item/{slug}",
            ),
        )
    return items


def _parse_items_from_html_links(soup: BeautifulSoup) -> dict[str, RaiderMarketItem]:
    items: dict[str, RaiderMarketItem] = {}
    for link in soup.find_all("a", href=True):
        href = link.get("href", "").strip()
        slug = _extract_slug(href)
        if not slug:
            continue
        text = " ".join(link.get_text(" ", strip=True).split())
        if not text:
            continue
        name = _extract_name(text)
        trade_value = _extract_labeled_value(text, "Trade Value")
        game_value = _extract_labeled_value(text, "Game Value")
        items.setdefault(
            slug,
            RaiderMarketItem(
                slug=slug,
                name=name,
                trade_value=trade_value,
                game_value=game_value,
                url=f"https://raidermarket.com/item/{slug}",
            ),
        )
    return items


def _parse_items_from_embedded_json(soup: BeautifulSoup) -> dict[str, RaiderMarketItem]:
    items: dict[str, RaiderMarketItem] = {}
    for script in soup.find_all("script"):
        script_id = script.get("id")
        script_type = script.get("type")
        if script_id != "__NEXT_DATA__" and script_type != "application/json":
            continue
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items.update(_parse_items_from_json(payload))
    return items


def parse_browse_items(html: str) -> dict[str, RaiderMarketItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: dict[str, RaiderMarketItem] = {}
    items.update(_parse_items_from_html_links(soup))
    items.update(_parse_items_from_embedded_json(soup))
    return items


async def fetch_browse_items(
    session: aiohttp.ClientSession, *, timeout: float = 25.0
) -> dict[str, RaiderMarketItem]:
    async with session.get(BROWSE_URL, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        resp.raise_for_status()
        html = await resp.text()
    return parse_browse_items(html)


def format_trade_value_lines(
    items: Iterable[RaiderMarketItem],
    *,
    include_game_value: bool = True,
) -> list[str]:
    lines = []
    for item in items:
        trade_value = item.trade_value
        if not isinstance(trade_value, int) or trade_value <= 0:
            continue
        game_value = item.game_value
        trade_label = f"{trade_value:,}"
        game_label = f"{game_value:,}" if isinstance(game_value, int) else "N/A"
        line = f"💰 **[{item.name}]({item.url})** — Trade: **{trade_label}**"
        if include_game_value:
            line = f"{line}, Game: {game_label}"
        lines.append(line)
    return lines
