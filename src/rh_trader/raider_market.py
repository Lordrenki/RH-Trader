"""RaiderMarket scraping helpers."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable

import aiohttp
from bs4 import BeautifulSoup

HOME_URL = "https://raidermarket.com"
BROWSE_URL = f"{HOME_URL}/browse"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; RH-Trader/0.1; "
        "+https://github.com/rh-trader/rh-trader)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


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


def _extract_labeled_value(text: str, labels: str | Iterable[str]) -> int | None:
    if isinstance(labels, str):
        labels = (labels,)
    for label in labels:
        pattern = rf"{re.escape(label)}\s*:?\s*\$?\s*([0-9][0-9,]*)"
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _parse_int(match.group(1))
    return None


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.split())
    cleaned = re.sub(
        r"^(?:common|uncommon|rare|epic|legendary)\s+\d+\s*[×x]\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\bBlueprint\s+Blueprint\b", "Blueprint", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _extract_name(text: str) -> str:
    for marker in ("Game Value", "Trade Value", "Market Value", "View Details"):
        match = re.search(re.escape(marker), text, flags=re.IGNORECASE)
        if match:
            return _clean_name(text[: match.start()])
    return _clean_name(text)


def _extract_slug(href: str) -> str | None:
    match = re.match(r"^(?:https?://(?:www\.)?raidermarket\.com)?/item/([^/?#]+)", href)
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
                "marketValue",
                "market_value",
                "market",
                "marketvalue",
                "marketValueScrap",
                "marketValueInScrap",
                "marketValueRaw",
                "marketValueNumber",
                "marketValueAmount",
                "value",
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
        trade_value = _extract_labeled_value(text, ("Trade Value", "Market Value"))
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


def _decode_next_flight_string(value: str) -> str:
    try:
        decoded = json.loads(f"[{value}]")
    except json.JSONDecodeError:
        return value
    return "".join(part for part in decoded if isinstance(part, str))


def _parse_items_from_script_text(raw: str) -> dict[str, RaiderMarketItem]:
    items: dict[str, RaiderMarketItem] = {}
    stripped = raw.strip()
    if not stripped:
        return items

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        payload = None
    if payload is not None:
        items.update(_parse_items_from_json(payload))

    # Next.js App Router pages often stream HTML/JSON inside self.__next_f.push([...])
    # string payloads. Decode those chunks and run both parsers over the resulting text.
    flight_chunks = re.findall(
        r'self\.__next_f\.push\(\[\s*\d+\s*,\s*((?:"(?:\\.|[^"\\])*")+)\s*\]\)',
        raw,
        flags=re.DOTALL,
    )
    for chunk in flight_chunks:
        decoded = _decode_next_flight_string(chunk)
        if not decoded:
            continue
        items.update(_parse_items_from_json(decoded))
        if "/item/" in decoded or "Market Value" in decoded or "Trade Value" in decoded:
            items.update(parse_browse_items(decoded))

    return items


def _parse_items_from_embedded_json(soup: BeautifulSoup) -> dict[str, RaiderMarketItem]:
    items: dict[str, RaiderMarketItem] = {}
    for script in soup.find_all("script"):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        script_items = _parse_items_from_script_text(raw)
        items.update(script_items)
    return items


def parse_browse_items(html: str) -> dict[str, RaiderMarketItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: dict[str, RaiderMarketItem] = {}
    items.update(_parse_items_from_html_links(soup))
    items.update(_parse_items_from_embedded_json(soup))
    return items


async def _fetch_items_from_url(
    session: aiohttp.ClientSession, url: str, *, timeout: float
) -> dict[str, RaiderMarketItem]:
    async with session.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        resp.raise_for_status()
        html = await resp.text()
    return parse_browse_items(html)


async def fetch_browse_items(
    session: aiohttp.ClientSession, *, timeout: float = 25.0
) -> dict[str, RaiderMarketItem]:
    items = await _fetch_items_from_url(session, BROWSE_URL, timeout=timeout)
    if items:
        return items

    # The browse page can be client-rendered while the home page still exposes the
    # high-value cards in static markup. Falling back keeps the Discord post useful
    # instead of publishing an empty embed.
    return await _fetch_items_from_url(session, HOME_URL, timeout=timeout)


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
