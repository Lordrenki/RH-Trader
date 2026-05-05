"""Metaforge ARC Raiders pricing helpers."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

import aiohttp
from bs4 import BeautifulSoup

BLUEPRINTS_URL = "https://metaforge.app/arc-raiders"
BLUEPRINT_KEYWORD = "blueprint"


@dataclass(frozen=True)
class BlueprintPrice:
    name: str
    median_price: float


def _to_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"[^0-9.,]", "", value).replace(",", "")
    if not normalized:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def _iter_records(payload: Any):
    if isinstance(payload, dict):
        yield payload
        for v in payload.values():
            yield from _iter_records(v)
    elif isinstance(payload, list):
        for entry in payload:
            yield from _iter_records(entry)


def _extract_prices_from_json(payload: Any) -> dict[str, BlueprintPrice]:
    found: dict[str, BlueprintPrice] = {}
    for record in _iter_records(payload):
        if not isinstance(record, dict):
            continue
        name = record.get("name") or record.get("itemName") or record.get("title")
        if not isinstance(name, str) or BLUEPRINT_KEYWORD not in name.lower():
            continue

        median = (
            record.get("median")
            or record.get("medianPrice")
            or record.get("marketMedian")
            or record.get("sellMedian")
            or record.get("sellPriceMedian")
            or record.get("price")
        )
        value = _to_float(median)
        if value is None or value <= 0:
            continue
        found.setdefault(name.strip(), BlueprintPrice(name=name.strip(), median_price=value))
    return found


def parse_blueprint_prices(html: str) -> list[BlueprintPrice]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, BlueprintPrice] = {}

    for script in soup.find_all("script"):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        if script.get("type") in {"application/json", "application/ld+json"} or "__NEXT_DATA__" in raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            found.update(_extract_prices_from_json(payload))

    for row in soup.find_all(["tr", "li", "div"]):
        text = " ".join(row.get_text(" ", strip=True).split())
        if BLUEPRINT_KEYWORD not in text.lower():
            continue
        money = re.search(r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text)
        if not money:
            continue
        price = _to_float(money.group(1))
        if price is None:
            continue
        name = text.split("$", 1)[0].split("-")[0].strip()
        if name:
            found.setdefault(name, BlueprintPrice(name=name, median_price=price))

    return sorted(found.values(), key=lambda x: x.median_price, reverse=True)


async def fetch_blueprint_prices(session: aiohttp.ClientSession) -> list[BlueprintPrice]:
    async with session.get(BLUEPRINTS_URL, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        html = await resp.text()
    return parse_blueprint_prices(html)


def build_price_embed_chunks(prices: list[BlueprintPrice], chunk_size: int = 25) -> list[str]:
    lines = [f"`{idx:>2}.` **{item.name}** — `${item.median_price:,.0f}`" for idx, item in enumerate(prices, start=1)]
    return ["\n".join(lines[i:i + chunk_size]) for i in range(0, len(lines), chunk_size)]
