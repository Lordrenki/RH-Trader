"""Discord bot entrypoint and command registration."""
from __future__ import annotations

import asyncio
import contextlib
import re
import unicodedata
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from rapidfuzz import fuzz, process

from .catalog import CatalogClient
from .config import Settings, load_settings
from .database import Database
from .embeds import (
    format_stock,
    format_wishlist,
    info_embed,
    rep_level_summary,
)
from .raider_market import fetch_browse_items, format_trade_value_lines

_log = logging.getLogger(__name__)
QUICK_RATING_COOLDOWN_SECONDS = 24 * 60 * 60
DEFAULT_LISTING_LIMIT = 50
DEFAULT_EMBED_COLOR = 0x2B2D31
PREMIUM_EMBED_COLOR = 0xFFD700
EMBED_FIELD_CHAR_LIMIT = 1000
REVIEW_CHAR_LIMIT = 300
TRADE_INACTIVITY_WARNING_SECONDS = 12 * 60 * 60
TRADE_INACTIVITY_CLOSE_SECONDS = 24 * 60 * 60
REP_LEVEL_ROLE_ID = 1_433_701_792_721_666_128
ADMIN_ROLE_ID = 927_355_923_364_720_651
REP_LEVEL_ROLE_THRESHOLD = 5
RAIDERMARKET_REFRESH_SECONDS = 15 * 60
RAIDERMARKET_TOP_COUNT = 25
PREMIUM_BADGE_URL = (
    "https://cdn.discordapp.com/attachments/1431560702518104175/1447739322022498364/"
    "discotools-xyz-icon.png?ex=6938b7d0&is=69376650&hm=bd0daea439bc5d7622d4b7008ba08ba8f5e44f30a79394a23dd58f5f5a07a3e6"
)



def _format_duration(seconds: int) -> str:
    delta = timedelta(seconds=seconds)
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if days:
        return f"{days} day(s)"
    if hours:
        return f"{hours} hour(s)"
    if minutes:
        return f"{minutes} minute(s)"
    return f"{secs} second(s)"


def _can_view_other(interaction: discord.Interaction, target: discord.User | discord.Member) -> bool:
    # Allow anyone to view another member's data.
    return True


def _trim_preview(text: str, limit: int = 200) -> str:
    trimmed = text.strip()
    if len(trimmed) <= limit:
        return trimmed
    return f"{trimmed[: max(0, limit - 3)].rstrip()}..."


def _extract_rep_reason(content: str, target_id: int) -> str:
    cleaned = re.sub(r"(?:^|\s)[+-]\s*rep\b", " ", content, flags=re.IGNORECASE)
    cleaned = re.sub(rf"<@!?{target_id}>", " ", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned.strip()


def _paginate_field_entries(entries: list, formatter, per_page: int) -> list[str]:
    """Split entries into field-friendly pages respecting Discord limits."""

    if not entries:
        return [formatter([])]

    chunk_size = max(1, per_page)
    pages: list[str] = []
    start = 0
    while start < len(entries):
        end = min(start + chunk_size, len(entries))
        while end > start:
            formatted = formatter(entries[start:end])
            if len(formatted) <= EMBED_FIELD_CHAR_LIMIT or end - start == 1:
                pages.append(formatted)
                start = end
                break
            end -= 1
    return pages


async def _maybe_assign_rep_role(
    guild: discord.Guild, member: discord.Member | None, rep_level: int
) -> None:
    if rep_level < REP_LEVEL_ROLE_THRESHOLD:
        return

    role = guild.get_role(REP_LEVEL_ROLE_ID)
    if role is None:
        _log.warning("Rep role %s not found in guild %s", REP_LEVEL_ROLE_ID, guild.id)
        return

    if member is None:
        _log.warning("Unable to resolve member for rep role assignment in guild %s", guild.id)
        return

    if role in member.roles:
        return

    try:
        await member.add_roles(role, reason="Reached rep level 5")
    except discord.HTTPException:
        _log.warning("Failed to assign rep role %s to member %s", role.id, member.id)


def _listing_limit_for_interaction(interaction: discord.Interaction) -> int:
    """Return how many stock/wishlist entries a user may store."""
    return DEFAULT_LISTING_LIMIT


async def _enforce_listing_limit(
    interaction: discord.Interaction, current_count: int, list_name: str
) -> bool:
    """Guard against adding more entries than the user is allowed."""

    limit = _listing_limit_for_interaction(interaction)
    if current_count < limit:
        return False

    await _send_interaction_message(
        interaction,
        embed=info_embed(
            "🚫 Limit reached",
            (
                f"You can only save up to {limit} {list_name} items right now."
            ),
        ),
        ephemeral=True,
    )
    return True


async def _send_interaction_message(
    interaction: discord.Interaction, /, **kwargs: Any
) -> None:
    """Send or follow up depending on whether the interaction is already acknowledged."""

    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
        return

    await interaction.response.send_message(**kwargs)


async def _remove_participants_and_close_thread(
    thread: discord.Thread, seller_id: int, buyer_id: int, *, reason: str
) -> None:
    for removal_id in (seller_id, buyer_id):
        removal_member = thread.guild.get_member(removal_id) if thread.guild else None
        try:
            await thread.remove_user(removal_member or discord.Object(id=removal_id))
        except discord.HTTPException:
            _log.warning("Failed to remove %s from trade thread %s", removal_id, thread.id)

    try:
        await thread.edit(archived=True, locked=True)
    except discord.HTTPException:
        pass
    try:
        await thread.delete(reason=reason)
    except discord.HTTPException:
        pass


DISPLAY_NAME_CACHE_TTL = timedelta(minutes=20)
_DISPLAY_NAME_CACHE: dict[int, tuple[str, datetime]] = {}


async def _lookup_display_name(client: discord.Client, user_id: int) -> str:
    now = datetime.now(timezone.utc)
    cached = _DISPLAY_NAME_CACHE.get(user_id)
    if cached:
        cached_name, expires_at = cached
        if expires_at > now:
            return cached_name

    user = client.get_user(user_id)
    if user is None:
        try:
            user = await client.fetch_user(user_id)
        except discord.HTTPException:
            return f"User {user_id}"
        _DISPLAY_NAME_CACHE[user_id] = (
            user.display_name,
            now + DISPLAY_NAME_CACHE_TTL,
        )
        return user.display_name
    display_name = user.display_name
    _DISPLAY_NAME_CACHE[user_id] = (display_name, now + DISPLAY_NAME_CACHE_TTL)
    return display_name


def _normalize_raider_market_slug(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("http"):
        cleaned = cleaned.split("/item/", 1)[-1]
    if cleaned.startswith("/item/"):
        cleaned = cleaned.split("/item/", 1)[-1]
    return cleaned.strip().strip("/").lower()


def _parse_raider_market_watchlist(raw: str | None) -> list[str]:
    if not raw:
        return []
    entries = re.split(r"[,\n]+", raw)
    seen: set[str] = set()
    results: list[str] = []
    for entry in entries:
        slug = _normalize_raider_market_slug(entry)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        results.append(slug)
    return results


def _chunk_lines(lines: list[str], *, max_chars: int = 3900) -> list[list[str]]:
    if not lines:
        return [[]]
    chunks: list[list[str]] = []
    current: list[str] = []
    total = 0
    for line in lines:
        additional = len(line) + (1 if current else 0)
        if current and total + additional > max_chars:
            chunks.append(current)
            current = [line]
            total = len(line)
        else:
            current.append(line)
            total += additional
    if current:
        chunks.append(current)
    return chunks


def _has_admin_role(member: discord.Member) -> bool:
    return any(role.id == ADMIN_ROLE_ID for role in member.roles)


class TraderBot(commands.Bot):
    """Discord bot that exposes trading slash commands."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        catalog: CatalogClient | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.members = True
        # Enable privileged intent to avoid missing intent warnings and allow
        # message content when needed for context-aware features.
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned_or("!"), intents=intents)
        self.settings = settings
        self.db = db
        self.catalog = catalog or CatalogClient(settings.catalog_base_url)
        self.stock_actions = StockGroup(self.db, self._send_alert_notifications)
        self.wishlist_actions = WishlistGroup(self.db)
        self.alert_actions = AlertGroup(self.db, self._alert_limit)
        self._inactivity_task: asyncio.Task | None = None
        self._raidermarket_task: asyncio.Task | None = None
        self._raidermarket_session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        await self.db.setup()
        self.tree.add_command(self.stock_actions)
        self.tree.add_command(self.wishlist_actions)
        self.tree.add_command(self.alert_actions)
        await self.add_misc_commands()
        await self.tree.sync()
        _log.info("Slash commands synced")
        self._inactivity_task = self.loop.create_task(self._inactivity_watcher())
        self._raidermarket_task = self.loop.create_task(self._raidermarket_watcher())

    async def close(self) -> None:
        await self.catalog.close()
        if self._inactivity_task:
            self._inactivity_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._inactivity_task
        if self._raidermarket_task:
            self._raidermarket_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._raidermarket_task
        if self._raidermarket_session and not self._raidermarket_session.closed:
            await self._raidermarket_session.close()
        await super().close()

    async def on_message(self, message: discord.Message) -> None:
        if isinstance(message.channel, discord.Thread):
            await self.db.record_trade_activity(message.channel.id)

        if not message.author.bot:
            await self._maybe_handle_message_rep(message)

        await super().on_message(message)

    async def _maybe_handle_message_rep(self, message: discord.Message) -> None:
        if message.guild is None:
            return

        normalized_content = unicodedata.normalize("NFKC", message.content.strip())
        match = re.search(r"(?:^|\s)([+-])\s*rep\b", normalized_content, re.IGNORECASE)
        if not match:
            return
        if not message.mentions:
            await message.channel.send(
                embed=info_embed(
                    "🚫 Missing mention",
                    "Please mention a user, e.g. `+rep @user` or `-rep @user`.",
                )
            )
            return

        sign = match.group(1)
        target_user = message.mentions[0]
        target_id = target_user.id
        if target_id == message.author.id:
            await message.channel.send(
                embed=info_embed("🚫 Invalid target", "You cannot give rep to yourself.")
            )
            return

        if sign == "-":
            reason = _extract_rep_reason(normalized_content, target_id)
            if not reason:
                await message.channel.send(
                    embed=info_embed(
                        "🚫 Missing reason",
                        "Include a reason when sending -rep, e.g. `-rep @user reason`.",
                    )
                )
                return

            preview = _trim_preview(reason, limit=200)
            prompt = info_embed(
                "Confirm -rep",
                (
                    "Are you sure you want to -rep this user? Falsely repping someone could "
                    "result in removal from trading.\n"
                    f"**Target:** <@{target_id}>\n"
                    f"**Reason:** {preview}"
                ),
            )

            async def confirm(interaction: discord.Interaction) -> None:
                recorded, retry_after = await self._record_message_rep(
                    message, target_id, sign
                )
                if recorded:
                    await interaction.response.send_message(
                        embed=info_embed("✅ -rep confirmed", "Your -rep was submitted."),
                        ephemeral=True,
                    )
                    return
                wait_label = _format_duration(int(retry_after or 0))
                await interaction.response.send_message(
                    embed=info_embed(
                        "⏳ On cooldown",
                        f"You recently rated <@{target_id}>. You can send another rep in {wait_label}.",
                    ),
                    ephemeral=True,
                )

            async def cancel(interaction: discord.Interaction) -> None:
                await interaction.response.send_message(
                    embed=info_embed("Cancelled", "Your -rep was not submitted."),
                    ephemeral=True,
                )

            view = ConfirmNegativeRepView(
                message.author.id, on_confirm=confirm, on_cancel=cancel
            )
            try:
                await message.author.send(embed=prompt, view=view)
                await message.channel.send(
                    embed=info_embed(
                        "📬 Check your DMs",
                        "I sent you a confirmation prompt to finalize this -rep.",
                    )
                )
            except discord.HTTPException:
                await message.channel.send(embed=prompt, view=view)
            return

        await self._record_message_rep(message, target_id, sign)

    async def _record_message_rep(
        self, message: discord.Message, target_id: int, sign: str
    ) -> tuple[bool, int | None]:
        score = 1 if sign == "+" else -1
        recorded, retry_after = await self.db.record_quick_rating(
            message.author.id,
            target_id,
            score,
            QUICK_RATING_COOLDOWN_SECONDS,
        )
        if not recorded:
            wait_label = _format_duration(int(retry_after or 0))
            await message.channel.send(
                embed=info_embed(
                    "⏳ On cooldown",
                    f"You recently rated <@{target_id}>. You can send another rep in {wait_label}.",
                )
            )
            return False, retry_after

        (
            _,
            rep_level,
            rep_positive,
            rep_negative,
            *_,
            stored_premium,
        ) = await self.db.profile(target_id)
        premium_flag = bool(stored_premium)
        action_label = "+rep" if sign == "+" else "-rep"
        target_member = message.guild.get_member(target_id)
        if target_member is None:
            try:
                target_member = await message.guild.fetch_member(target_id)
            except discord.HTTPException:
                target_member = None
        target_label = target_member.display_name if target_member else f"<@{target_id}>"
        await _maybe_assign_rep_role(message.guild, target_member, rep_level)
        await message.channel.send(
            embed=info_embed(
                "🏅 Rep updated",
                (
                    f"You sent {action_label} to <@{target_id}>.\n"
                    f"{target_label} is Rep Level {rep_level}.\n"
                    f"{rep_level_summary(rep_level, rep_positive, rep_negative, premium_boost=premium_flag)}"
                ),
            )
        )
        return True, None

    async def add_misc_commands(self) -> None:
        db = self.db

        @self.tree.command(name="rep", description="Check someone's rep summary")
        @app_commands.describe(user="Member you want to check")
        async def rep(
            interaction: discord.Interaction, user: Optional[discord.User] = None
        ):
            target = user or interaction.user
            if not _can_view_other(interaction, target):
                await interaction.response.send_message(
                    embed=info_embed("🚫 Permission denied", "You can only view your own rep."),
                    ephemeral=True,
                )
                return

            (
                _,
                rep_level,
                rep_positive,
                rep_negative,
                *_,
                stored_premium,
            ) = await db.profile(target.id)
            premium_flag = bool(stored_premium)
            await interaction.response.send_message(
                embed=info_embed(
                    f"🏅 Rep for {target.display_name}",
                    rep_level_summary(
                        rep_level,
                        rep_positive,
                        rep_negative,
                        premium_boost=premium_flag,
                    ),
                )
            )

        @self.tree.command(name="editrep", description="Manually adjust a user's rep level")
        @app_commands.describe(user="Member whose rep level you want to change")
        @app_commands.describe(level="New rep level (0-200)")
        async def editrep(
            interaction: discord.Interaction,
            user: discord.Member,
            level: app_commands.Range[int, 0, 200],
        ):
            if interaction.guild is None:
                await interaction.response.send_message(
                    embed=info_embed("🌐 Guild only", "This command can only be used inside a server."),
                    ephemeral=True,
                )
                return

            member = interaction.user
            if not isinstance(member, discord.Member) or not any(
                role.id == ADMIN_ROLE_ID for role in member.roles
            ):
                await interaction.response.send_message(
                    embed=info_embed(
                        "🚫 Missing permissions",
                        "You need the admin role to edit rep levels.",
                    ),
                    ephemeral=True,
                )
                return

            await db.set_rep_level(user.id, int(level))
            (
                _,
                rep_level,
                rep_positive,
                rep_negative,
                *_,
                stored_premium,
            ) = await db.profile(user.id)
            premium_flag = bool(stored_premium)
            await _maybe_assign_rep_role(interaction.guild, user, rep_level)
            await interaction.response.send_message(
                embed=info_embed(
                    f"✅ Rep updated for {user.display_name}",
                    rep_level_summary(
                        rep_level,
                        rep_positive,
                        rep_negative,
                        premium_boost=premium_flag,
                    ),
                ),
                ephemeral=True,
            )

        @self.tree.command(description="Search community inventories or wishlists for an item")
        @app_commands.describe(
            item="Keyword to search for",
            location="Choose whether to search inventory or wishlist entries",
        )
        @app_commands.choices(
            location=[
                app_commands.Choice(name="Inventory", value="stock"),
                app_commands.Choice(name="Wishlist", value="wishlist"),
            ]
        )
        async def search(
            interaction: discord.Interaction, item: str, location: app_commands.Choice[str]
        ):
            async def _resolve_guild_members(entries: list[tuple[int, ...]]):
                guild = interaction.guild
                if guild is None:
                    return []

                resolved: list[tuple[discord.Member, tuple[int, ...]]] = []
                for entry in entries:
                    user_id = entry[0]
                    member = guild.get_member(user_id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(user_id)
                        except discord.HTTPException:
                            member = None

                    if member is not None:
                        resolved.append((member, entry))

                return resolved

            if location.value == "wishlist":
                results = await db.search_wishlist(item)
                results = await _resolve_guild_members(results)
                lines = [
                    (
                        f"🔍 {member.display_name} wants **{item}**"
                        + (f" — {note}" if note else "")
                    )
                    for member, (_, item, note) in results
                ]
                description = (
                    "\n".join(lines)
                    if lines
                    else "No matching wishlist items found from members of this server."
                )
            else:
                results = await db.search_stock(item)
                results = await _resolve_guild_members(results)
                lines = [
                    (
                        f"🔍 {member.display_name} has **{item}** (x{qty})"
                    )
                    for member, (_, item, qty) in results
                ]
                description = (
                    "\n".join(lines)
                    if lines
                    else "No matching inventory items found from members of this server."
                )

            embed = info_embed("🔎 Search results", description)
            await interaction.response.send_message(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False, replied_user=False
                ),
            )

        @self.tree.command(name="inventory", description="Open a quick inventory control panel")
        async def inventory(interaction: discord.Interaction):
            embed = info_embed(
                "🧰 Inventory menu",
                (
                    "Manage your inventory from one place. Add items, adjust quantities, and"
                    " clean up your list without typing slash commands."
                ),
            )
            await interaction.response.send_message(
                embed=embed,
                view=InventoryMenuView(
                    self.db,
                    self.stock_actions,
                ),
                ephemeral=True,
            )

        @self.tree.command(description="Show a trading profile")
        @app_commands.describe(user="Optionally view another member's profile")
        async def profile(interaction: discord.Interaction, user: Optional[discord.User] = None):
            target = user or interaction.user
            if not _can_view_other(interaction, target):
                await interaction.response.send_message(
                    embed=info_embed("🚫 Permission denied", "You can only view your own profile."),
                    ephemeral=True,
                )
                return

            (
                _,
                rep_level,
                rep_positive,
                rep_negative,
                timezone,
                bio,
                stored_premium,
            ) = await db.profile(target.id)
            trades = await db.trade_count(target.id)
            reviews = await db.recent_reviews_for_user(target.id, 3)

            is_self = target.id == interaction.user.id
            premium_flag = bool(stored_premium)

            trade_label = "trade" if trades == 1 else "trades"
            description_lines = [
                rep_level_summary(
                    rep_level,
                    rep_positive,
                    rep_negative,
                    premium_boost=premium_flag,
                    show_premium_boost_text=False,
                ),
                f"🤝 {trades} {trade_label} completed",
                f"💎 Status: {'Premium trader' if premium_flag else 'Standard trader'}",
            ]

            embed = info_embed(
                f"🧾 Profile for {target.display_name}",
                description="\n".join(description_lines),
            )
            if premium_flag:
                embed.set_thumbnail(url=PREMIUM_BADGE_URL)
            embed.add_field(
                name="🕰️ Time zone",
                value=timezone or "Not set",
                inline=False,
            )
            embed.add_field(
                name="✍️ Bio",
                value=bio or "No bio set yet.",
                inline=False,
            )

            if reviews:
                review_lines = []
                for reviewer_id, review_text, _ in reviews:
                    review_lines.append(f"• {review_text}\n— <@{reviewer_id}>")
                reviews_value = "\n\n".join(review_lines)
            else:
                reviews_value = "No reviews yet."

            embed.add_field(name="📝 Recent reviews", value=reviews_value, inline=False)
            await interaction.response.send_message(embed=embed)

        @self.tree.command(description="View top rep levels")
        async def leaderboard(interaction: discord.Interaction):
            rows = await db.leaderboard()
            if not rows:
                await interaction.response.send_message(
                    embed=info_embed("🏆 Leaderboard", "No rep yet."),
                )
                return
            description = "\n".join(
                f"{idx+1}. <@{user_id}> — {rep_level_summary(level, rep_positive, rep_negative, premium_boost=is_premium, show_premium_boost_text=False)}"
                for idx, (user_id, level, rep_positive, rep_negative, is_premium) in enumerate(rows)
            )
            await interaction.response.send_message(embed=info_embed("🏆 Leaderboard", description))

        @self.tree.command(name="starttrade", description="Open a private thread to trade with someone")
        @app_commands.describe(partner="Person you want to trade with", item="What you're trading")
        async def starttrade(
            interaction: discord.Interaction,
            partner: discord.Member,
            item: app_commands.Range[str, 3, 100] = "Custom trade",
        ):
            if interaction.guild is None:
                await interaction.response.send_message(
                    embed=info_embed(
                        "🌐 Guild only",
                        "Start trade threads inside a server so I can add everyone to it.",
                    ),
                    ephemeral=True,
                )
                return

            if partner.id == interaction.user.id:
                await interaction.response.send_message(
                    embed=info_embed(
                        "🚫 Invalid target", "You cannot start a trade thread with yourself."
                    ),
                    ephemeral=True,
                )
                return

            await self._open_trade_thread(
                interaction,
                seller_id=partner.id,
                buyer_id=interaction.user.id,
                item=item,
                initiator_id=interaction.user.id,
            )

        @self.tree.command(
            description="Set the channel where trade threads should be created for this server"
        )
        @app_commands.describe(channel="Channel where trade threads will be opened")
        async def set_trade_thread_channel(
            interaction: discord.Interaction, channel: discord.TextChannel
        ):
            if interaction.guild is None:
                await interaction.response.send_message(
                    embed=info_embed(
                        "🌐 Guild only", "This command can only be used inside a server."
                    ),
                    ephemeral=True,
                )
                return

            perms = getattr(interaction.user, "guild_permissions", None)
            if not (perms and (perms.manage_guild or perms.administrator)):
                await interaction.response.send_message(
                    embed=info_embed(
                        "🚫 Missing permissions",
                        "You need Manage Server or Administrator permissions to set the trade thread channel.",
                    ),
                    ephemeral=True,
                )
                return

            await db.set_trade_thread_channel(interaction.guild.id, channel.id)
            await interaction.response.send_message(
                embed=info_embed(
                    "✅ Trade thread channel saved",
                    f"New trade threads will be created in {channel.mention}.",
                ),
                ephemeral=True,
            )

        @self.tree.command(
            name="trade_values_watchlist",
            description="Set the RaiderMarket watchlist panel for this server",
        )
        @app_commands.describe(
            channel="Channel to post RaiderMarket trade values",
            watchlist="Optional comma-separated RaiderMarket item slugs or item URLs",
        )
        async def trade_values_watchlist(
            interaction: discord.Interaction,
            watchlist: Optional[str] = None,
            channel: Optional[discord.TextChannel] = None,
        ):
            if interaction.guild is None:
                await interaction.response.send_message(
                    embed=info_embed(
                        "🌐 Guild only", "This command can only be used inside a server."
                    ),
                    ephemeral=True,
                )
                return

            member = interaction.user if isinstance(interaction.user, discord.Member) else None
            if member is None or not _has_admin_role(member):
                await interaction.response.send_message(
                    embed=info_embed(
                        "🚫 Missing permissions",
                        (
                            "You need the admin role to update the watchlist.\n"
                            f"Role required: <@&{ADMIN_ROLE_ID}>."
                        ),
                    ),
                    ephemeral=True,
                )
                return

            target_channel = channel or interaction.channel
            if not isinstance(target_channel, discord.TextChannel):
                await interaction.response.send_message(
                    embed=info_embed(
                        "🚫 Invalid channel",
                        "Please choose a text channel for the RaiderMarket panel.",
                    ),
                    ephemeral=True,
                )
                return

            panel = await db.get_raidermarket_panel(interaction.guild.id)
            slugs = _parse_raider_market_watchlist(watchlist)
            if panel is None or panel[1] != target_channel.id:
                if panel is not None and panel[1] != target_channel.id:
                    old_channel = self.get_channel(panel[1])
                    if old_channel is None:
                        with contextlib.suppress(discord.HTTPException):
                            old_channel = await self.fetch_channel(panel[1])
                    if isinstance(old_channel, (discord.TextChannel, discord.Thread)):
                        old_message_id = panel[2]
                        old_extra_ids = panel[4]
                        with contextlib.suppress(discord.HTTPException, discord.NotFound):
                            old_message = await old_channel.fetch_message(old_message_id)
                            await old_message.delete()
                        for extra_id in old_extra_ids:
                            with contextlib.suppress(
                                discord.HTTPException, discord.NotFound
                            ):
                                extra_message = await old_channel.fetch_message(extra_id)
                                await extra_message.delete()
                placeholder = await target_channel.send(
                    "Setting up RaiderMarket trade values…"
                )
                await db.upsert_raidermarket_panel(
                    interaction.guild.id,
                    target_channel.id,
                    placeholder.id,
                    slugs,
                )
                response_title = "✅ RaiderMarket panel saved"
            else:
                await db.update_raidermarket_watchlist(interaction.guild.id, slugs)
                response_title = "✅ Watchlist updated"
            status_line = (
                f"Tracking {len(slugs)} item(s) now."
                if slugs
                else "Tracking top trade values automatically."
            )
            await interaction.response.send_message(
                embed=info_embed(response_title, status_line),
                ephemeral=True,
            )
            await self._refresh_raidermarket_panels(
                target_guild_id=interaction.guild.id
            )

    async def _inactivity_watcher(self) -> None:
        await self.wait_until_ready()
        try:
            while not self.is_closed():
                try:
                    await self._process_inactive_threads()
                except Exception:
                    _log.exception("Error while monitoring inactive trade threads")
                await asyncio.sleep(300)
        except asyncio.CancelledError:
            return

    async def _process_inactive_threads(self) -> None:
        now = int(time.time())
        warning_cutoff = now - TRADE_INACTIVITY_WARNING_SECONDS
        close_cutoff = now - TRADE_INACTIVITY_CLOSE_SECONDS
        trades = await self.db.list_active_trade_threads()

        for (
            trade_id,
            thread_id,
            seller_id,
            buyer_id,
            _item,
            last_activity,
            warning_sent,
        ) in trades:
            if last_activity is None:
                continue

            thread = self.get_channel(thread_id)
            if thread is None:
                try:
                    thread = await self.fetch_channel(thread_id)
                except discord.HTTPException:
                    thread = None

            if not isinstance(thread, discord.Thread):
                continue

            if last_activity <= close_cutoff:
                message = info_embed(
                    "⌛ Trade cancelled",
                    (
                        "This trade thread was closed after 24 hours without activity.\n"
                        "The trade has been cancelled and the thread will be cleaned up."
                    ),
                )
                try:
                    await thread.send(
                        content=f"<@{seller_id}> <@{buyer_id}>",
                        embed=message,
                        allowed_mentions=discord.AllowedMentions(
                            users=True, roles=False, everyone=False, replied_user=False
                        ),
                    )
                except discord.HTTPException:
                    _log.warning(
                        "Failed to notify trade %s about inactivity closure", trade_id
                    )

                await self.db.cancel_trade(trade_id)
                await self.db.clear_active_trade(seller_id, trade_id)
                await self.db.clear_active_trade(buyer_id, trade_id)
                await _remove_participants_and_close_thread(
                    thread,
                    seller_id,
                    buyer_id,
                    reason="Trade inactive for 24 hours",
                )
                continue

            if warning_sent:
                continue

            if last_activity <= warning_cutoff:
                warning_embed = info_embed(
                    "⏳ Inactivity warning",
                    (
                        "No one has spoken in this trade thread for 12 hours."
                        " I'll cancel the trade and close the thread after another 12 hours"
                        " of inactivity."
                    ),
                )
                try:
                    await thread.send(
                        content=f"<@{seller_id}> <@{buyer_id}>",
                        embed=warning_embed,
                        allowed_mentions=discord.AllowedMentions(
                            users=True, roles=False, everyone=False, replied_user=False
                        ),
                    )
                except discord.HTTPException:
                    _log.warning(
                        "Failed to send inactivity warning for trade %s", trade_id
                    )

                await self.db.mark_inactivity_warning_sent(trade_id)

    def _get_raidermarket_session(self) -> aiohttp.ClientSession:
        if self._raidermarket_session is None or self._raidermarket_session.closed:
            self._raidermarket_session = aiohttp.ClientSession(
                headers={
                    "User-Agent": "RH-Trader RaiderMarket Tracker (https://raidermarket.com/browse)"
                }
            )
        return self._raidermarket_session

    async def _raidermarket_watcher(self) -> None:
        await self.wait_until_ready()
        try:
            while not self.is_closed():
                try:
                    await self._refresh_raidermarket_panels()
                except Exception:
                    _log.exception("Error while updating RaiderMarket trade values")
                await asyncio.sleep(RAIDERMARKET_REFRESH_SECONDS)
        except asyncio.CancelledError:
            return

    async def _refresh_raidermarket_panels(
        self, *, target_guild_id: int | None = None
    ) -> None:
        panels = await self.db.list_raidermarket_panels()
        if target_guild_id is not None:
            panels = [panel for panel in panels if panel[0] == target_guild_id]
        if not panels:
            return

        session = self._get_raidermarket_session()
        try:
            items = await fetch_browse_items(session)
        except Exception:
            _log.exception("Failed to fetch RaiderMarket browse data")
            return

        updated_at = datetime.now(timezone.utc)
        for guild_id, channel_id, message_id, watchlist, extra_message_ids in panels:
            await self._update_raidermarket_panel(
                guild_id,
                channel_id,
                message_id,
                watchlist,
                extra_message_ids,
                items,
                updated_at,
            )

    async def _update_raidermarket_panel(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        watchlist: list[str],
        extra_message_ids: list[int],
        items: dict[str, Any],
        updated_at: datetime,
    ) -> None:
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                channel = None

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            _log.warning(
                "Raidermarket panel channel %s not found for guild %s", channel_id, guild_id
            )
            return

        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            for extra_id in extra_message_ids:
                with contextlib.suppress(discord.HTTPException):
                    extra_message = await channel.fetch_message(extra_id)
                    await extra_message.delete()
            await self.db.clear_raidermarket_panel(guild_id)
            _log.info(
                "Raidermarket panel message missing for guild %s, clearing settings",
                guild_id,
            )
            return
        except discord.HTTPException:
            _log.warning(
                "Unable to fetch raidermarket panel message %s in guild %s",
                message_id,
                guild_id,
            )
            return

        embeds = self._build_raidermarket_embeds(items, watchlist, updated_at)
        embed_batches = [
            embeds[index : index + 10] for index in range(0, len(embeds), 10)
        ]
        try:
            await message.edit(embeds=embed_batches[0], content=None)
        except discord.HTTPException:
            _log.warning(
                "Failed to edit raidermarket panel message %s in guild %s",
                message_id,
                guild_id,
            )
            return

        new_extra_ids: list[int] = []
        for idx, batch in enumerate(embed_batches[1:]):
            existing_id = extra_message_ids[idx] if idx < len(extra_message_ids) else None
            if existing_id is not None:
                try:
                    extra_message = await channel.fetch_message(existing_id)
                    await extra_message.edit(embeds=batch, content=None)
                    new_extra_ids.append(existing_id)
                    continue
                except discord.HTTPException:
                    pass

            try:
                extra_message = await channel.send(embeds=batch)
                new_extra_ids.append(extra_message.id)
            except discord.HTTPException:
                _log.warning(
                    "Failed to send extra raidermarket panel message in guild %s",
                    guild_id,
                )

        stale_count_start = len(embed_batches) - 1
        for stale_id in extra_message_ids[stale_count_start:]:
            with contextlib.suppress(discord.HTTPException):
                stale_message = await channel.fetch_message(stale_id)
                await stale_message.delete()

        if new_extra_ids != extra_message_ids:
            await self.db.update_raidermarket_panel_messages(
                guild_id,
                channel.id,
                message.id,
                new_extra_ids,
            )

    def _build_raidermarket_embeds(
        self,
        items: dict[str, Any],
        watchlist: list[str],
        updated_at: datetime,
    ) -> list[discord.Embed]:
        description_lines: list[str] = []

        if watchlist:
            chosen = []
            for slug in watchlist:
                item = items.get(slug)
                if item is not None and isinstance(item.trade_value, int) and item.trade_value > 0:
                    chosen.append(item)
            description_lines.extend(format_trade_value_lines(chosen))
            title = "RaiderMarket Trade Values (Watchlist)"
        else:
            ranked = sorted(
                (
                    entry
                    for entry in items.values()
                    if isinstance(entry.trade_value, int) and entry.trade_value > 0
                ),
                key=lambda entry: entry.trade_value,
                reverse=True,
            )
            top_items = ranked[:RAIDERMARKET_TOP_COUNT]
            description_lines.extend(format_trade_value_lines(top_items))
            title = f"RaiderMarket Top {len(top_items)} Trade Values"

        if not description_lines:
            description_lines = ["No items with trade values found on RaiderMarket."]

        timestamp = updated_at.strftime("%Y-%m-%d %H:%M UTC")
        chunks = _chunk_lines(description_lines)
        total_chunks = len(chunks)
        embeds: list[discord.Embed] = []
        for idx, chunk in enumerate(chunks, start=1):
            page_title = title if total_chunks == 1 else f"{title} (Page {idx}/{total_chunks})"
            embed = discord.Embed(
                title=page_title,
                description="\n".join(chunk),
                color=DEFAULT_EMBED_COLOR,
            )
            embed.set_footer(text=f"Last updated: {timestamp}")
            embeds.append(embed)
        return embeds

    def _alert_limit(self, interaction: discord.Interaction) -> int:
        return 20

    async def _open_trade_thread(
        self,
        interaction: discord.Interaction,
        *,
        seller_id: int,
        buyer_id: int,
        item: str,
        initiator_id: int,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=info_embed(
                    "🌐 Guild only",
                    "Trade threads can only be created inside a server.",
                ),
                ephemeral=True,
            )
            return

        channel_id = await self.db.get_trade_thread_channel(interaction.guild.id)
        if channel_id is None:
            await interaction.response.send_message(
                embed=info_embed(
                    "🚫 Thread channel missing",
                    "An admin needs to run /set_trade_thread_channel to choose where trade threads start.",
                ),
                ephemeral=True,
            )
            return

        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                channel = None

        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                embed=info_embed(
                    "🚫 Thread channel missing",
                    "I can't access the configured trade thread channel. Please double-check it.",
                ),
                ephemeral=True,
            )
            return

        seller = interaction.guild.get_member(seller_id) or await interaction.guild.fetch_member(
            seller_id
        )
        buyer = interaction.guild.get_member(buyer_id) or await interaction.guild.fetch_member(
            buyer_id
        )

        thread_name = f"trade-{buyer.display_name}-with-{seller.display_name}"
        try:
            thread = await channel.create_thread(
                name=thread_name[:90],
                type=discord.ChannelType.private_thread,
                invitable=False,
                reason="New trade initiated",
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                embed=info_embed(
                    "❌ Could not create thread",
                    f"I couldn't start a trade thread in {channel.mention}.",
                ),
                ephemeral=True,
            )
            return

        failed_additions: list[int] = []
        for member in (seller, buyer):
            try:
                await thread.add_user(member)
            except discord.HTTPException as exc:
                _log.warning(
                    "Failed to add %s to trade thread %s: %s", member.id, thread.id, exc
                )
                failed_additions.append(member.id)

        trade_id = await self.db.create_trade(seller_id, buyer_id, item, thread_id=thread.id)
        await self.db.accept_trade(trade_id, seller_id)

        intro = info_embed(
            "🤝 Trade room opened",
            (
                f"<@{buyer_id}> wants to trade with <@{seller_id}> for **{item}**.\n"
                "Use this thread to chat and press **Complete Trade** when you're done "
                "or **Cancel Trade** if plans change."
            ),
        )
        view = TradeThreadView(
            self.db,
            trade_id=trade_id,
            seller_id=seller_id,
            buyer_id=buyer_id,
            initiator_id=initiator_id,
            item=item,
        )
        self.add_view(view)
        try:
            await thread.send(content=f"<@{seller_id}> <@{buyer_id}>", embed=intro, view=view)
        except discord.HTTPException:
            _log.warning("Failed to send intro message to trade thread %s", thread.id)

        description = (
            f"I've opened {thread.mention} for you. I'll clean it up when the trade is closed."
        )
        if failed_additions:
            users = ", ".join(f"<@{member_id}>" for member_id in failed_additions)
            description += (
                "\n⚠️ I couldn't add "
                f"{users} to the thread. Make sure they can view {channel.mention} "
                "and that I have permission to manage private threads there."
            )

        await interaction.response.send_message(
            embed=info_embed("✅ Trade thread ready", description),
            ephemeral=True,
        )

    async def _send_alert_notifications(
        self, poster: discord.abc.User | discord.Member, stock: list[Tuple[str, int]]
    ) -> None:
        items = [name for name, _ in stock]
        matches = await self.db.matching_alerts_for_items(items)
        aggregated: dict[int, list[Tuple[str, str]]] = {}

        for user_id, alert_item, matched_item in matches:
            if user_id == poster.id:
                continue
            aggregated.setdefault(user_id, []).append((alert_item, matched_item))

        if not aggregated:
            return

        contact, rep_level, rep_positive, rep_negative, _, _, stored_premium = await self.db.profile(
            poster.id
        )
        trades = await self.db.trade_count(poster.id)
        premium_flag = bool(stored_premium)
        profile_lines = [
            rep_level_summary(rep_level, rep_positive, rep_negative, premium_boost=premium_flag),
            f"🤝 {trades} trade{'s' if trades != 1 else ''} completed",
        ]
        if contact:
            profile_lines.append(f"📞 Contact: {contact}")

        color = PREMIUM_EMBED_COLOR if premium_flag else DEFAULT_EMBED_COLOR

        for target_id, pairs in aggregated.items():
            try:
                target = self.get_user(target_id) or await self.fetch_user(target_id)
            except discord.HTTPException:
                _log.warning("Failed to load alert recipient %s", target_id)
                continue

            matched_lines = []
            for alert_item, matched_item in pairs:
                if alert_item.lower() == matched_item.lower():
                    matched_lines.append(f"• **{matched_item}**")
                else:
                    matched_lines.append(
                        f"• **{matched_item}** (matched alert: **{alert_item}** )"
                    )
            default_item = pairs[0][1]

            embed = discord.Embed(
                title="🔔 Item alert matched",
                description=(
                    f"<@{poster.id}> just updated their inventory with item(s) you're watching."
                ),
                color=color,
            )
            embed.add_field(
                name="Matched items", value="\n".join(matched_lines), inline=False
            )
            embed.add_field(
                name="Trader profile", value="\n".join(profile_lines), inline=False
            )
            embed.set_author(
                name=getattr(poster, "display_name", str(poster)),
                icon_url=getattr(poster.display_avatar, "url", None),
            )

            try:
                await target.send(
                    embed=embed,
                    content=(
                        "Interested? Reach out in your server's trade channel to coordinate."
                    ),
                )
            except discord.HTTPException:
                _log.warning(
                    "Failed to send alert notification for %s to %s", default_item, target_id
                )


class StockGroup(app_commands.Group):
    def __init__(
        self,
        db: Database,
        alert_notifier: Callable[
            [discord.abc.User | discord.Member, list[Tuple[str, int]]], Awaitable[None]
        ]
        | None = None,
    ):
        super().__init__(name="stock", description="Manage your inventory list")
        self.db = db
        self._alert_notifier = alert_notifier

    @app_commands.command(name="add", description="Add an item to your inventory list")
    @app_commands.describe(item="Item name", quantity="How many you have")
    async def add(self, interaction: discord.Interaction, item: str, quantity: int = 1):
        qty = max(1, quantity)
        item_name = item.strip()
        if not item_name:
            await _send_interaction_message(
                interaction,
                embed=info_embed("⚠️ Item required", "Please enter an item name."),
                ephemeral=True,
            )
            return

        stock = await self.db.get_stock(interaction.user.id)
        match_note = ""
        if stock:
            names = [name for name, _ in stock]
            match = process.extractOne(item_name, names, scorer=fuzz.WRatio)
            if match and match[1] >= 90:
                if match[0].lower() != item_name.lower():
                    match_note = f" (matched **{match[0]}**)"
                item_name = match[0]

        if not any(name.lower() == item_name.lower() for name, _ in stock):
            if await _enforce_listing_limit(interaction, len(stock), "inventory"):
                return

        await self.db.add_stock(interaction.user.id, item_name, qty)
        embed = info_embed(
            "📦 Inventory updated",
            f"Added **{item_name}** x{qty} to your inventory.{match_note}",
        )
        await _send_interaction_message(interaction, embed=embed, ephemeral=True)
        if self._alert_notifier:
            stock = await self.db.get_stock(interaction.user.id)
            await self._alert_notifier(interaction.user, stock)

    @app_commands.command(
        name="change",
        description="Change the quantity for an item in your inventory list",
    )
    @app_commands.describe(
        item="Item to update (fuzzy matched against your inventory)",
        quantity="New quantity you have",
    )
    async def change(self, interaction: discord.Interaction, item: str, quantity: int):
        stock = await self.db.get_stock(interaction.user.id)
        if not stock:
            await interaction.response.send_message(
                embed=info_embed("No inventory found", "Add something first to change it."),
                ephemeral=True,
            )
            return

        term = item.strip()
        if not term:
            await interaction.response.send_message(
                embed=info_embed("⚠️ Item required", "Please enter an item name."),
                ephemeral=True,
            )
            return

        names = [name for name, _ in stock]
        match = process.extractOne(term, names, scorer=fuzz.WRatio)
        if not match or match[1] < 60:
            await interaction.response.send_message(
                embed=info_embed(
                    "🔍 No close match",
                    "I couldn't find anything that looks like that in your inventory.",
                ),
                ephemeral=True,
            )
            return

        best_name = match[0]
        current_qty = next(qty for name, qty in stock if name == best_name)
        new_qty = max(0, quantity)
        await self.db.update_stock_quantity(interaction.user.id, best_name, new_qty)

        if new_qty == 0:
            message = f"Removed **{best_name}** from your inventory."
        else:
            message = f"Updated **{best_name}** to x{new_qty} (was x{current_qty})."

        await interaction.response.send_message(
            embed=info_embed("📦 Inventory updated", message),
            ephemeral=True,
        )
        if self._alert_notifier and new_qty > 0:
            stock = await self.db.get_stock(interaction.user.id)
            await self._alert_notifier(interaction.user, stock)

    async def show(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        target = user or interaction.user
        if not _can_view_other(interaction, target):
            await interaction.response.send_message(
                embed=info_embed("🚫 Permission denied", "You can only view your own inventory."),
                ephemeral=True,
            )
            return
        items = await self.db.get_stock(target.id)
        embed = info_embed(f"📦 Inventory for {target.display_name}", format_stock(items))
        await interaction.response.send_message(embed=embed)

    async def clear_list(self, interaction: discord.Interaction):
        await self.db.clear_stock(interaction.user.id)
        embed = info_embed("🗑️ Inventory cleared", "Your inventory list is now empty.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="view", description="View inventory for you or another member")
    @app_commands.describe(user="Member to view")
    async def view(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        await self.show(interaction, user)

    @app_commands.command(name="remove", description="Remove an item from your inventory list")
    @app_commands.describe(item="Item to remove (fuzzy matched against your inventory)")
    async def remove(self, interaction: discord.Interaction, item: str):
        stock = await self.db.get_stock(interaction.user.id)
        if not stock:
            await interaction.response.send_message(
                embed=info_embed("No inventory found", "Add something first to remove it."),
                ephemeral=True,
            )
            return

        term = item.strip()
        if not term:
            await interaction.response.send_message(
                embed=info_embed("⚠️ Item required", "Please enter an item name."),
                ephemeral=True,
            )
            return

        candidates = [name for name, _ in stock]
        match = process.extractOne(term, candidates, scorer=fuzz.WRatio)
        if not match or match[1] < 60:
            await interaction.response.send_message(
                embed=info_embed(
                    "🔍 No close match",
                    "I couldn't find anything that looks like that in your inventory.",
                ),
                ephemeral=True,
            )
            return

        best_name = match[0]
        removed = await self.db.remove_stock(interaction.user.id, best_name)
        message = (
            f"Removed **{best_name}** from your inventory."
            if removed
            else "Item not found anymore."
        )
        await interaction.response.send_message(
            embed=info_embed("🧹 Inventory cleanup", message),
            ephemeral=True,
        )

    @app_commands.command(name="clear", description="Clear all items from your inventory list")
    async def clear(self, interaction: discord.Interaction):
        await self.clear_list(interaction)


class WishlistGroup(app_commands.Group):
    def __init__(self, db: Database):
        super().__init__(name="wishlist", description="Track items you want")
        self.db = db

    @app_commands.command(name="add", description="Add an item to your wishlist")
    @app_commands.describe(item="Item to add", note="Optional note like target price")
    async def add(self, interaction: discord.Interaction, item: str, note: str = ""):
        item_name = item.strip()
        if not item_name:
            await interaction.response.send_message(
                embed=info_embed("⚠️ Item required", "Please enter an item name."),
                ephemeral=True,
            )
            return

        wishlist = await self.db.get_wishlist(interaction.user.id)
        if not any(name.lower() == item_name.lower() for name, _ in wishlist):
            if await _enforce_listing_limit(interaction, len(wishlist), "wishlist"):
                return

        await self.db.add_wishlist(interaction.user.id, item_name, note)
        embed = info_embed("🎯 Wishlist updated", f"Added **{item_name}** to your wishlist.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def show(
        self, interaction: discord.Interaction, user: Optional[discord.User] = None
    ):
        target = user or interaction.user
        if not _can_view_other(interaction, target):
            await interaction.response.send_message(
                embed=info_embed("🚫 Permission denied", "You can only view your own wishlist."),
                ephemeral=True,
            )
            return
        entries = await self.db.get_wishlist(target.id)
        embed = info_embed(f"🎯 Wishlist for {target.display_name}", format_wishlist(entries))
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="view", description="View wishlist for you or another member")
    @app_commands.describe(user="Member to view")
    async def view(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        await self.show(interaction, user)

    @app_commands.command(name="remove", description="Remove an item from your wishlist")
    @app_commands.describe(item="Item to remove (fuzzy matched against your wishlist)")
    async def remove(self, interaction: discord.Interaction, item: str):
        wishlist = await self.db.get_wishlist(interaction.user.id)
        if not wishlist:
            await interaction.response.send_message(
                embed=info_embed("No wishlist items", "Add items before removing them."),
                ephemeral=True,
            )
            return

        term = item.strip()
        if not term:
            await interaction.response.send_message(
                embed=info_embed("⚠️ Item required", "Please enter an item name."),
                ephemeral=True,
            )
            return

        candidates = [name for name, _ in wishlist]
        match = process.extractOne(term, candidates, scorer=fuzz.WRatio)
        if not match or match[1] < 60:
            await interaction.response.send_message(
                embed=info_embed(
                    "🔍 No close match",
                    "I couldn't find anything that looks like that on your wishlist.",
                ),
                ephemeral=True,
            )
            return

        best_name = match[0]
        removed = await self.db.remove_wishlist(interaction.user.id, best_name)
        message = (
            f"Removed **{best_name}** from your wishlist." if removed else "Item not found anymore."
        )
        await interaction.response.send_message(
            embed=info_embed("🧹 Wishlist cleanup", message),
            ephemeral=True,
        )


class AlertGroup(app_commands.Group):
    def __init__(self, db: Database, limit_resolver: Callable[[discord.Interaction], int]):
        super().__init__(name="alerts", description="Get notified when items show up")
        self.db = db
        self._limit_resolver = limit_resolver

    @app_commands.command(name="add", description="Add a new alert item")
    @app_commands.describe(item="Item name to watch for")
    async def add(self, interaction: discord.Interaction, item: str):
        cleaned = item.strip()
        if not cleaned:
            await interaction.response.send_message(
                embed=info_embed("⚠️ Item required", "Please enter an item name."),
                ephemeral=True,
            )
            return

        current_alerts = await self.db.get_alerts(interaction.user.id)
        limit = self._limit_resolver(interaction)
        if cleaned.lower() not in {entry.lower() for entry in current_alerts} and len(
            current_alerts
        ) >= limit:
            await interaction.response.send_message(
                embed=info_embed(
                    "🚫 Alert limit reached",
                    f"You can track up to **{limit}** item(s) with your current tier.",
                ),
                ephemeral=True,
            )
            return

        await self.db.add_alert(interaction.user.id, cleaned)
        await interaction.response.send_message(
            embed=info_embed("🔔 Alert saved", f"I'll DM you when **{cleaned}** shows up."),
            ephemeral=True,
        )

    async def show(self, interaction: discord.Interaction):
        alerts = await self.db.get_alerts(interaction.user.id)
        if not alerts:
            await interaction.response.send_message(
                embed=info_embed("🔔 Alerts", "You don't have any alerts yet."),
                ephemeral=True,
            )
            return

        description = "\n".join(f"• {entry}" for entry in alerts)
        await interaction.response.send_message(
            embed=info_embed("🔔 Alerts", description), ephemeral=True
        )

    @app_commands.command(name="view", description="See your alert list")
    async def view(self, interaction: discord.Interaction):
        await self.show(interaction)

    @app_commands.command(name="remove", description="Delete an alert item")
    @app_commands.describe(item="Item to remove (fuzzy matched against your alerts)")
    async def remove(self, interaction: discord.Interaction, item: str):
        alerts = await self.db.get_alerts(interaction.user.id)
        if not alerts:
            await interaction.response.send_message(
                embed=info_embed("No alerts found", "Add an alert before removing one."),
                ephemeral=True,
            )
            return

        term = item.strip()
        match = process.extractOne(term, alerts, scorer=fuzz.WRatio)
        if not match or match[1] < 60:
            await interaction.response.send_message(
                embed=info_embed(
                    "🔍 No close match",
                    "I couldn't find anything that looks like that in your alerts.",
                ),
                ephemeral=True,
            )
            return

        best_name = match[0]
        removed = await self.db.remove_alert(interaction.user.id, best_name)
        message = (
            f"Removed **{best_name}** from your alerts."
            if removed
            else "Alert not found anymore."
        )
        await interaction.response.send_message(
            embed=info_embed("🧹 Alerts updated", message),
            ephemeral=True,
        )


class ConfirmClearView(discord.ui.View):
    def __init__(self, on_confirm: Callable[[discord.Interaction], Awaitable[None]]):
        super().__init__(timeout=60)
        self._on_confirm = on_confirm

    @discord.ui.button(label="Yes, clear it", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._on_confirm(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            embed=info_embed("Inventory unchanged", "No items were removed."), view=None
        )


class ConfirmNegativeRepView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        *,
        on_confirm: Callable[[discord.Interaction], Awaitable[None]],
        on_cancel: Callable[[discord.Interaction], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=60)
        self._author_id = author_id
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message(
                embed=info_embed("🚫 Not your rep", "Only the original sender can confirm this."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Yes, -rep", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._on_confirm(interaction)
        self.disable_all_items()
        with contextlib.suppress(discord.HTTPException):
            if interaction.message:
                await interaction.message.edit(view=self)

    @discord.ui.button(label="No, cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._on_cancel(interaction)
        self.disable_all_items()
        with contextlib.suppress(discord.HTTPException):
            if interaction.message:
                await interaction.message.edit(view=self)


class StockAddModal(discord.ui.Modal):
    def __init__(self, handler: StockGroup):
        super().__init__(title="Add to inventory")
        self._handler = handler
        self.item_input = discord.ui.TextInput(
            label="Item",
            placeholder="Enter the item name",
            max_length=100,
        )
        self.quantity_input = discord.ui.TextInput(
            label="Quantity",
            placeholder="1",
            default="1",
            max_length=5,
        )
        self.add_item(self.item_input)
        self.add_item(self.quantity_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_qty = (self.quantity_input.value or "1").strip()
        try:
            qty = int(raw_qty)
        except ValueError:
            qty = 1
        qty = max(1, qty)
        item_name = self.item_input.value.strip()
        await self._handler.add.callback(self._handler, interaction, item_name, qty)


class WishlistAddModal(discord.ui.Modal):
    def __init__(self, handler: WishlistGroup):
        super().__init__(title="Add to wishlist")
        self._handler = handler
        self.item_input = discord.ui.TextInput(
            label="Item",
            placeholder="Enter the item name",
            max_length=100,
        )
        self.note_input = discord.ui.TextInput(
            label="Note (optional)",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=200,
            placeholder="Target price or extra details",
        )
        self.add_item(self.item_input)
        self.add_item(self.note_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        item_name = self.item_input.value.strip()
        note = (self.note_input.value or "").strip()
        await self._handler.add.callback(self._handler, interaction, item_name, note)


class RemoveStockModal(discord.ui.Modal):
    def __init__(self, handler: StockGroup):
        super().__init__(title="Remove from inventory")
        self._handler = handler
        self.item_input = discord.ui.TextInput(
            label="Inventory item",
            placeholder="What do you want to remove?",
            max_length=100,
        )
        self.add_item(self.item_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._handler.remove.callback(
            self._handler, interaction, (self.item_input.value or "").strip()
        )


class StockChangeModal(discord.ui.Modal):
    def __init__(self, handler: StockGroup):
        super().__init__(title="Update inventory quantity")
        self._handler = handler
        self.item_input = discord.ui.TextInput(
            label="Inventory item",
            placeholder="Which item should be updated?",
            max_length=100,
        )
        self.quantity_input = discord.ui.TextInput(
            label="New quantity",
            placeholder="0 to remove",
            max_length=5,
        )
        self.add_item(self.item_input)
        self.add_item(self.quantity_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_qty = (self.quantity_input.value or "").strip()
        try:
            qty = int(raw_qty)
        except ValueError:
            qty = 1
        await self._handler.change.callback(
            self._handler, interaction, (self.item_input.value or "").strip(), qty
        )


class RemoveWishlistModal(discord.ui.Modal):
    def __init__(self, handler: WishlistGroup):
        super().__init__(title="Remove from wishlist")
        self._handler = handler
        self.item_input = discord.ui.TextInput(
            label="Wishlist item",
            placeholder="What do you want to drop?",
            max_length=100,
        )
        self.add_item(self.item_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._handler.remove.callback(
            self._handler, interaction, (self.item_input.value or "").strip()
        )


class AlertAddModal(discord.ui.Modal):
    def __init__(self, handler: AlertGroup):
        super().__init__(title="Add alert")
        self._handler = handler
        self.item_input = discord.ui.TextInput(
            label="Item to watch",
            placeholder="Enter the item name",
            max_length=100,
        )
        self.add_item(self.item_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._handler.add.callback(self._handler, interaction, self.item_input.value)


class AlertRemoveModal(discord.ui.Modal):
    def __init__(self, handler: AlertGroup):
        super().__init__(title="Remove alert")
        self._handler = handler
        self.item_input = discord.ui.TextInput(
            label="Alert item",
            placeholder="Which alert should be removed?",
            max_length=100,
        )
        self.add_item(self.item_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._handler.remove.callback(self._handler, interaction, self.item_input.value)


class BioModal(discord.ui.Modal):
    def __init__(self, db: Database):
        super().__init__(title="Update your bio")
        self.db = db
        self.bio_input = discord.ui.TextInput(
            label="Short bio",
            style=discord.TextStyle.paragraph,
            max_length=200,
            required=False,
            placeholder="Tell others who you are as a trader",
        )
        self.add_item(self.bio_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bio = (self.bio_input.value or "").strip()
        await self.db.set_bio(interaction.user.id, bio)
        message = "Bio cleared." if not bio else "Bio updated."
        await interaction.response.send_message(
            embed=info_embed("✍️ Bio saved", message), ephemeral=True
        )


class TimezoneModal(discord.ui.Modal):
    def __init__(self, db: Database):
        super().__init__(title="Set your time zone")
        self.db = db
        self.timezone_input = discord.ui.TextInput(
            label="Time zone",
            placeholder="e.g., UTC-5 / EST",
            max_length=50,
            required=False,
        )
        self.add_item(self.timezone_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        timezone = (self.timezone_input.value or "").strip()
        await self.db.set_timezone(interaction.user.id, timezone)
        message = "Time zone cleared." if not timezone else f"Time zone set to {timezone}."
        await interaction.response.send_message(
            embed=info_embed("🕰️ Time zone saved", message), ephemeral=True
        )


class InventoryMenuView(discord.ui.View):
    def __init__(self, db: Database, stock_handler: StockGroup):
        super().__init__(timeout=600)
        self.db = db
        self._stock_handler = stock_handler

    async def _confirm_clear_stock(self, interaction: discord.Interaction) -> None:
        async def confirm(inter: discord.Interaction) -> None:
            await self._stock_handler.clear_list(inter)
            if inter.message:
                try:
                    await inter.followup.edit_message(
                        inter.message.id,
                        embed=info_embed(
                            "🧹 Inventory cleared", "Your inventory list is now empty."
                        ),
                        view=None,
                    )
                except discord.HTTPException:
                    pass

        view = ConfirmClearView(confirm)
        await interaction.response.send_message(
            embed=info_embed("Confirm", "This will remove all inventory entries."),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="View Inventory", style=discord.ButtonStyle.secondary, emoji="📦", row=0)
    async def stock_view(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._stock_handler.show(interaction, None)

    @discord.ui.button(label="Add Item", style=discord.ButtonStyle.primary, emoji="🧺", row=0)
    async def stock_add(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(StockAddModal(self._stock_handler))

    @discord.ui.button(label="Adjust Quantity", style=discord.ButtonStyle.primary, emoji="🧮", row=0)
    async def stock_change(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(StockChangeModal(self._stock_handler))

    @discord.ui.button(label="Remove Item", style=discord.ButtonStyle.secondary, emoji="➖", row=1)
    async def stock_remove(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(RemoveStockModal(self._stock_handler))

    @discord.ui.button(label="Clear Inventory", style=discord.ButtonStyle.danger, emoji="🧹", row=1)
    async def stock_clear(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._confirm_clear_stock(interaction)

    @discord.ui.button(label="Set Bio", style=discord.ButtonStyle.secondary, emoji="✍️", row=2)
    async def set_bio(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(BioModal(self.db))

    @discord.ui.button(label="Set Time Zone", style=discord.ButtonStyle.secondary, emoji="🕰️", row=2)
    async def set_timezone(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(TimezoneModal(self.db))

    @discord.ui.button(label="Close", style=discord.ButtonStyle.primary, emoji="🚪", row=2)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            embed=info_embed("Closed", "You can reopen /inventory anytime."), view=None
        )


async def send_rep_prompts(
    bot: commands.Bot,
    db: Database,
    trade_id: int,
    item: str,
    rater_id: int,
    partner_id: int,
    role_value: str,
    *,
    thread: discord.Thread | None,
) -> None:
    if thread is None:
        _log.warning("Skipping rep prompts for trade %s because no thread was provided", trade_id)
        return

    rep_view = RepFeedbackView(db, trade_id, rater_id, partner_id, role_value, item)
    bot.add_view(rep_view)
    try:
        await thread.send(
            content=f"<@{rater_id}>",
            embed=info_embed(
                "🏅 Rep your partner",
                (
                    f"Trade #{trade_id} for **{item}** is complete.\n"
                    "Send +rep or -rep and optionally leave a review before this thread closes."
                ),
            ),
            view=rep_view,
        )
    except discord.HTTPException:
        _log.warning("Failed to send in-thread rep prompt to %s for trade %s", rater_id, trade_id)


class BasePersistentView(discord.ui.View):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("timeout", None)
        super().__init__(*args, **kwargs)

    def disable_all_items(self) -> None:
        for item in self.children:
            item.disabled = True


class TradeThreadView(BasePersistentView):
    def __init__(
        self,
        db: Database,
        *,
        trade_id: int,
        seller_id: int,
        buyer_id: int,
        initiator_id: int,
        item: str,
    ) -> None:
        super().__init__()
        self.db = db
        self.trade_id = trade_id
        self.seller_id = seller_id
        self.buyer_id = buyer_id
        self.initiator_id = initiator_id
        self.item = item
        self.complete_button.custom_id = f"trade:threadcomplete:{trade_id}"
        self.cancel_button.custom_id = f"trade:threadcancel:{trade_id}"

    async def _remove_participants_and_close_thread(
        self, thread: discord.Thread, *, reason: str
    ) -> None:
        await _remove_participants_and_close_thread(
            thread, self.seller_id, self.buyer_id, reason=reason
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in {self.seller_id, self.buyer_id} and not getattr(
            interaction.user.guild_permissions, "manage_messages", False
        ):
            await interaction.response.send_message(
                embed=info_embed(
                    "🚫 Not your trade",
                    "Only the two traders or a moderator can manage this trade.",
                ),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Cancel Trade", style=discord.ButtonStyle.danger, emoji="🛑")
    async def cancel_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        cancelled = await self.db.cancel_trade(self.trade_id)
        if not cancelled:
            await interaction.response.send_message(
                embed=info_embed(
                    "ℹ️ Trade already closed",
                    f"Trade #{self.trade_id} is already marked finished.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=info_embed(
                "🚫 Trade cancelled",
                "The trade has been cancelled and the thread will be cleaned up.",
            ),
        )

        try:
            if isinstance(interaction.channel, discord.Thread):
                await self._remove_participants_and_close_thread(
                    interaction.channel, reason="Trade cancelled"
                )
        finally:
            self.disable_all_items()
            try:
                if interaction.message:
                    await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Complete Trade", style=discord.ButtonStyle.success, emoji="✅")
    async def complete_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        completed = await self.db.complete_trade(self.trade_id)
        if not completed:
            await interaction.response.send_message(
                embed=info_embed(
                    "ℹ️ Trade already closed",
                    f"Trade #{self.trade_id} is already marked finished.",
                ),
                ephemeral=True,
            )
            return

        try:
            if isinstance(interaction.channel, discord.Thread):
                guild = interaction.guild
                reviewer_id = self.initiator_id
                partner_id = self.buyer_id if reviewer_id == self.seller_id else self.seller_id

                async def finish_feedback(result_interaction: discord.Interaction) -> None:
                    if not isinstance(result_interaction.channel, discord.Thread):
                        return

                    await self._remove_participants_and_close_thread(
                        result_interaction.channel, reason="Trade closed"
                    )

                prompt = info_embed(
                    "🏅 Rep your partner",
                    (
                        f"Trade #{self.trade_id} for **{self.item}** is complete.\n"
                        "Send +rep or -rep and optionally leave a review before this thread closes."
                    ),
                )
                prompt.set_footer(text="You'll be removed automatically after submitting feedback.")
                rep_view = RepFeedbackView(
                    self.db,
                    self.trade_id,
                    reviewer_id,
                    partner_id,
                    "buyer" if reviewer_id == self.buyer_id else "seller",
                    self.item,
                    on_finish=finish_feedback,
                )
                interaction.client.add_view(rep_view)
                try:
                    await interaction.channel.send(
                        content=f"<@{reviewer_id}>", embed=prompt, view=rep_view
                    )
                except discord.HTTPException:
                    _log.warning(
                        "Failed to send in-thread rep prompt for trade %s", self.trade_id
                    )
        finally:
            self.disable_all_items()
            try:
                if interaction.message:
                    await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass

        await interaction.response.send_message(
            embed=info_embed(
                "✅ Trade closed",
                (
                    "Please leave +rep or -rep in this thread to finish closing it. I'll "
                    "remove everyone once feedback is submitted."
                ),
            ),
        )


class ReviewModal(discord.ui.Modal):
    def __init__(
        self,
        db: Database,
        trade_id: int,
        reviewer_id: int,
        target_id: int,
        item: str,
        *,
        on_complete: Callable[[discord.Interaction], Awaitable[None]] | None = None,
    ):
        super().__init__(title="Leave a review")
        self.db = db
        self.trade_id = trade_id
        self.reviewer_id = reviewer_id
        self.target_id = target_id
        self.item = item
        self._on_complete = on_complete
        self.review_input = discord.ui.TextInput(
            label="Share your experience",
            style=discord.TextStyle.long,
            placeholder="What stood out about this trade?",
            max_length=REVIEW_CHAR_LIMIT,
        )
        self.add_item(self.review_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        saved = await self.db.record_trade_review(
            self.trade_id, self.reviewer_id, self.target_id, self.review_input.value
        )
        if saved:
            embed = info_embed(
                "📝 Review saved",
                f"Thanks! Your review for <@{self.target_id}> was recorded.",
            )
        else:
            embed = info_embed(
                "⚠️ Review not saved",
                "Make sure you've sent +rep or -rep before submitting a review.",
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        if saved and self._on_complete is not None:
            await self._on_complete(interaction)


class TradeView(BasePersistentView):
    def __init__(
        self,
        db: Database,
        trade_id: int,
        seller_id: int,
        buyer_id: int,
        item: str,
        *,
        is_seller: bool,
        status: str = "pending",
        initiator_id: int | None = None,
    ):
        super().__init__()
        self.db = db
        self.trade_id = trade_id
        self.seller_id = seller_id
        self.buyer_id = buyer_id
        self.item = item
        self.is_seller = is_seller
        self.initiator_id = initiator_id or buyer_id
        self.status = status
        role_label = "seller" if is_seller else "buyer"
        base_custom_id = f"trade:{trade_id}:{role_label}"
        self.complete_button.custom_id = f"{base_custom_id}:complete"
        self.cancel_button.custom_id = f"{base_custom_id}:cancel"
        self.accept_button.custom_id = f"{base_custom_id}:accept"
        self.reject_button.custom_id = f"{base_custom_id}:reject"
        self._configure_buttons()

    def _configure_buttons(self) -> None:
        pending = self.status == "pending"
        open_status = self.status == "open"
        closed = self.status in {"completed", "cancelled", "rejected"}

        # Buttons that should only work after acceptance
        self.complete_button.disabled = not open_status
        self.cancel_button.disabled = closed
        self.accept_button.disabled = not (pending and self.is_seller)
        self.reject_button.disabled = not (pending and self.is_seller)

    async def _get_trade(self, interaction: discord.Interaction) -> Tuple[int, int, int, str, str] | None:
        trade = await self.db.get_trade(self.trade_id)
        if not trade:
            await interaction.response.send_message(
                embed=info_embed(
                    "Trade unavailable",
                    f"Trade #{self.trade_id} could not be found. Please start a new trade.",
                ),
                ephemeral=True,
            )
            return None

        _, seller_id, buyer_id, _, status = trade
        self.seller_id = seller_id
        self.buyer_id = buyer_id
        self.status = status
        self._configure_buttons()

        if interaction.user.id not in {seller_id, buyer_id}:
            await interaction.response.send_message(
                embed=info_embed("🚫 Not your trade", "Only participants can manage this trade."),
                ephemeral=True,
            )
            return None

        if status in {"cancelled", "completed", "rejected"}:
            self.disable_all_items()
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass

            status_label = status
            await interaction.response.send_message(
                embed=info_embed(
                    "ℹ️ Trade closed",
                    f"Trade #{self.trade_id} is already {status_label}.",
                ),
                ephemeral=True,
            )
            return None

        return trade

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self._get_trade(interaction) is not None

    @discord.ui.button(label="Mark Trade Completed", style=discord.ButtonStyle.green, emoji="✅")
    async def complete_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        trade = await self._get_trade(interaction)
        if trade is None:
            return
        if self.status != "open":
            await interaction.response.send_message(
                embed=info_embed(
                    "⏳ Waiting for acceptance",
                    "Your partner must accept the trade before completing it.",
                ),
                ephemeral=True,
            )
            return

        updated = await self.db.complete_trade(self.trade_id)
        if not updated:
            self.disable_all_items()
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass
            await interaction.response.send_message(
                embed=info_embed("Trade already completed", f"Trade #{self.trade_id} is already marked done."),
                ephemeral=True,
            )
            return
        self.disable_all_items()
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
        await interaction.response.send_message(
            embed=info_embed("✅ Trade completed", f"Trade #{self.trade_id} for **{self.item}** is now complete."),
            ephemeral=True,
        )
        rater_id = self.initiator_id
        partner_id = self.buyer_id if rater_id == self.seller_id else self.seller_id
        role_value = "seller" if rater_id == self.seller_id else "buyer"
        await send_rep_prompts(
            interaction.client,
            self.db,
            self.trade_id,
            self.item,
            rater_id,
            partner_id,
            role_value,
            thread=interaction.channel if isinstance(interaction.channel, discord.Thread) else None,
        )

    @discord.ui.button(label="Cancel Trade", style=discord.ButtonStyle.danger, emoji="🛑")
    async def cancel_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        trade = await self._get_trade(interaction)
        if trade is None:
            return

        cancelled = await self.db.cancel_trade(self.trade_id)
        if not cancelled:
            self.disable_all_items()
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass
            await interaction.response.send_message(
                embed=info_embed(
                    "Trade already closed", f"Trade #{self.trade_id} is already completed or cancelled."
                ),
                ephemeral=True,
            )
            return

        self.disable_all_items()
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
        await interaction.response.send_message(
            embed=info_embed(
                "🚫 Trade cancelled", f"Trade #{self.trade_id} for **{self.item}** has been cancelled."
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Accept Trade", style=discord.ButtonStyle.success, emoji="✅")
    async def accept_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        trade = await self._get_trade(interaction)
        if trade is None:
            return

        if interaction.user.id != self.seller_id:
            await interaction.response.send_message(
                embed=info_embed(
                    "🚫 Trade partner only", "Only your trade partner can accept this trade."
                ),
                ephemeral=True,
            )
            return

        accepted = await self.db.accept_trade(self.trade_id, self.seller_id)
        if not accepted:
            await interaction.response.send_message(
                embed=info_embed(
                    "❌ Cannot accept",
                    "This trade may have already been accepted, rejected, or cancelled.",
                ),
                ephemeral=True,
            )
            return

        self.status = "open"
        self._configure_buttons()
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass

        await interaction.response.send_message(
            embed=info_embed(
                "✅ Trade accepted",
                "You can now coordinate in this DM thread and update the trade status here.",
            ),
            ephemeral=True,
        )

        try:
            partner = interaction.client.get_user(self.buyer_id) or await interaction.client.fetch_user(
                self.buyer_id
            )
            await partner.send(
                embed=info_embed(
                    f"✅ Trade #{self.trade_id} accepted",
                    (
                        f"<@{self.seller_id}> accepted the trade for **{self.item}**.\n"
                        "Use the buttons below to manage the trade status."
                    ),
                ),
                view=TradeView(
                    self.db,
                    self.trade_id,
                    self.seller_id,
                    self.buyer_id,
                    self.item,
                    is_seller=False,
                    status="open",
                    initiator_id=self.buyer_id,
                ),
            )
        except discord.HTTPException:
            _log.warning("Failed to notify buyer %s about accepted trade %s", self.buyer_id, self.trade_id)

    @discord.ui.button(label="Reject Trade", style=discord.ButtonStyle.secondary, emoji="🚫")
    async def reject_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        trade = await self._get_trade(interaction)
        if trade is None:
            return

        if interaction.user.id != self.seller_id:
            await interaction.response.send_message(
                embed=info_embed(
                    "🚫 Trade partner only", "Only your trade partner can reject this trade."
                ),
                ephemeral=True,
            )
            return

        rejected = await self.db.reject_trade(self.trade_id, self.seller_id)
        if not rejected:
            await interaction.response.send_message(
                embed=info_embed(
                    "❌ Cannot reject",
                    "This trade may have already been accepted, rejected, or cancelled.",
                ),
                ephemeral=True,
            )
            return

        self.status = "rejected"
        self.disable_all_items()
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass

        await interaction.response.send_message(
            embed=info_embed(
                "🚫 Trade rejected", f"You rejected trade #{self.trade_id} for **{self.item}**."
            ),
            ephemeral=True,
        )

        try:
            partner = interaction.client.get_user(self.buyer_id) or await interaction.client.fetch_user(
                self.buyer_id
            )
            await partner.send(
                embed=info_embed(
                    f"🚫 Trade #{self.trade_id} rejected",
                    f"Your partner declined the trade for **{self.item}**.",
                )
            )
        except discord.HTTPException:
            _log.warning("Failed to notify buyer %s about rejected trade %s", self.buyer_id, self.trade_id)


class RepFeedbackView(BasePersistentView):
    def __init__(
        self,
        db: Database,
        trade_id: int,
        rater_id: int,
        partner_id: int,
        role: str,
        item: str,
        *,
        on_finish: Callable[[discord.Interaction], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self.db = db
        self.trade_id = trade_id
        self.rater_id = rater_id
        self.partner_id = partner_id
        self.role = role
        self.item = item
        self.on_finish = on_finish
        self.rep_submitted = False
        self.review_submitted = False
        self.feedback_finished = False
        prefix = f"rep:{trade_id}:{rater_id}:{partner_id}:{role}"
        self.rep_positive_button.custom_id = f"{prefix}:plus"
        self.rep_negative_button.custom_id = f"{prefix}:minus"
        self.leave_review_button.custom_id = f"{prefix}:review"
        self.finish_button.custom_id = f"{prefix}:finish"
        self.finish_button.disabled = on_finish is None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.rater_id:
            await interaction.response.send_message(
                embed=info_embed("🚫 Not your feedback", "Only this trade participant can submit rep."),
                ephemeral=True,
            )
            return False
        return True

    def _disable_rep_buttons(self) -> None:
        for button in (self.rep_positive_button, self.rep_negative_button):
            button.disabled = True

    async def _handle_rep(self, interaction: discord.Interaction, score: int) -> None:
        recorded = await self.db.record_trade_rep(
            self.trade_id, self.rater_id, self.partner_id, score, self.role
        )
        if recorded:
            self.rep_submitted = True
            if self.finish_button and self.finish_button.disabled:
                self.finish_button.disabled = False
        else:
            self.rep_submitted = await self.db.has_trade_rep(self.trade_id, self.rater_id)
            if self.rep_submitted and self.finish_button and self.finish_button.disabled:
                self.finish_button.disabled = False

        self._disable_rep_buttons()
        action_label = "+rep" if score > 0 else "-rep"
        (
            _,
            rep_level,
            rep_positive,
            rep_negative,
            *_,
        ) = await self.db.profile(self.partner_id)
        guild = interaction.guild
        if guild is not None:
            partner_member = guild.get_member(self.partner_id)
            if partner_member is None:
                try:
                    partner_member = await guild.fetch_member(self.partner_id)
                except discord.HTTPException:
                    partner_member = None
            await _maybe_assign_rep_role(guild, partner_member, rep_level)
        embed = info_embed(
            "🏅 Rep received" if recorded else "ℹ️ Rep already recorded",
            (
                f"You sent {action_label} to <@{self.partner_id}> for **{self.item}**.\n"
                f"They are Rep Level {rep_level}."
            )
            if recorded
            else "You have already submitted feedback for this trade.",
        )
        if recorded:
            embed.description += "\nTap **Leave Review** to share a short note about this trade (optional)."
            embed.description += (
                f"\n{rep_level_summary(rep_level, rep_positive, rep_negative)}"
            )
        else:
            embed.description += "\nYou can still use **Leave Review** to update your written feedback."
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="+rep", style=discord.ButtonStyle.success)
    async def rep_positive_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._handle_rep(interaction, 1)

    @discord.ui.button(label="-rep", style=discord.ButtonStyle.danger)
    async def rep_negative_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._handle_rep(interaction, -1)

    @discord.ui.button(
        label="Leave Review (optional)",
        style=discord.ButtonStyle.secondary,
        emoji="📝",
        row=1,
    )
    async def leave_review_button(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ):
        has_rep = await self.db.has_trade_rep(self.trade_id, self.rater_id)
        if not has_rep:
            await interaction.response.send_message(
                embed=info_embed(
                    "🏅 Send rep first",
                    "Please submit +rep or -rep before leaving a review.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            ReviewModal(
                self.db,
                self.trade_id,
                self.rater_id,
                self.partner_id,
                self.item,
                on_complete=self._complete_feedback,
            )
        )

    async def _complete_feedback(self, interaction: discord.Interaction) -> None:
        if self.feedback_finished or self.on_finish is None:
            return
        if not self.rep_submitted:
            return

        self.feedback_finished = True
        self.disable_all_items()
        try:
            if interaction.message:
                await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass

        await self.on_finish(interaction)

    @discord.ui.button(label="Finish Feedback", style=discord.ButtonStyle.success, row=2)
    async def finish_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.on_finish is None:
            await interaction.response.defer(ephemeral=True)
            return

        if not self.rep_submitted:
            await interaction.response.send_message(
                embed=info_embed("🏅 Send rep first", "Submit +rep or -rep before closing this trade."),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=info_embed("✅ Feedback complete", "Thanks! Closing out this trade thread."),
            ephemeral=True,
        )
        await self._complete_feedback(interaction)


def run_bot() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    bot = TraderBot(settings, Database(settings.database_path))
    bot.run(settings.discord_token)


if __name__ == "__main__":
    run_bot()
