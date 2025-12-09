"""Embed builder utilities for consistent formatting."""
from __future__ import annotations

from typing import Iterable, Tuple

import discord


def info_embed(title: str, description: str | None = None, *, color: int = 0x2b2d31) -> discord.Embed:
    embed = discord.Embed(title=title, description=description or "", color=color)
    embed.set_footer(text="Scrap Market • Made with ♡ by Kuro")
    return embed


def format_stock(fields: Iterable[Tuple[str, int]]) -> str:
    return "\n".join(f"**{item}** — {qty} in stock" for item, qty in fields) or "No items listed yet."


def format_wishlist(entries: Iterable[Tuple[str, str]]) -> str:
    lines = []
    for item, note in entries:
        suffix = f" — {note}" if note else ""
        lines.append(f"**{item}**{suffix}")
    return "\n".join(lines) or "No wishlist entries yet."


def format_offers(entries: Iterable[Tuple[int, str, int, str]]) -> str:
    lines = []
    for user_id, item, qty, details in entries:
        suffix = f" — {details}" if details else ""
        lines.append(f"💰 <@{user_id}>: **{item}** x{qty}{suffix}")
    return "\n".join(lines) or "No offers posted yet."


def format_requests(entries: Iterable[Tuple[int, str, int, str]]) -> str:
    lines = []
    for user_id, item, qty, details in entries:
        suffix = f" — {details}" if details else ""
        lines.append(f"📢 <@{user_id}> wants **{item}** x{qty}{suffix}")
    return "\n".join(lines) or "No requests posted yet."


def rating_summary(
    score: float,
    count: int,
    *,
    premium_boost: bool = False,
    boost_percent: float = 0.05,
) -> str:
    if count == 0:
        return "No ratings yet"

    adjusted_score = score
    if premium_boost:
        adjusted_score = min(5.0, score * (1 + boost_percent))
    suffix = " (Premium boost)" if premium_boost else ""

    return f"⭐ {adjusted_score:.2f} average from {count} ratings{suffix}"


def response_summary(
    score: float,
    count: int,
    *,
    premium_boost: bool = False,
    boost_percent: float = 0.05,
) -> str:
    if count == 0:
        return "⚡ No response data yet"

    adjusted_score = score
    if premium_boost:
        adjusted_score = min(10.0, score * (1 + boost_percent))
    suffix = " (Premium boost)" if premium_boost else ""

    return f"⚡ {adjusted_score:.1f}/10 response speed{suffix}"
