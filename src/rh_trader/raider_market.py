"""RaiderMarket scraping helpers."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

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


def parse_browse_items(html: str) -> dict[str, RaiderMarketItem]:
    soup = BeautifulSoup(html, "html.parser")
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
