"""Discord bot entrypoint and command registration."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Awaitable, Callable, List, Optional, Tuple

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
    rating_summary,
    response_summary,
)

_log = logging.getLogger(__name__)
QUICK_RATING_COOLDOWN_SECONDS = 24 * 60 * 60
STORE_POST_WINDOW_SECONDS = 60 * 60
DEFAULT_STORE_POST_LIMIT = 1
DEFAULT_STORE_LISTING_LIMIT = 10
DEFAULT_EMBED_COLOR = 0x2B2D31
PREMIUM_EMBED_COLOR = 0xFFD700
EMBED_FIELD_CHAR_LIMIT = 1000
REVIEW_CHAR_LIMIT = 300
PREMIUM_BADGE_URL = (
    "https://cdn.discordapp.com/attachments/1431560702518104175/1447739322022498364/"
    "discotools-xyz-icon.png?ex=6938b7d0&is=69376650&hm=bd0daea439bc5d7622d4b7008ba08ba8f5e44f30a79394a23dd58f5f5a07a3e6"
)


@dataclass(frozen=True)
class StoreTierBenefits:
    """Benefit limits for a specific premium tier."""

    name: str
    rank: int
    post_limit: int
    listing_limit: int


#: Mapping of premium SKU IDs to tier benefits. Tiers scale from Premium
#: Trader (Tier 1) through Elite Trader (Tier 2) and Expert Trader (Tier 3).
STORE_TIER_BY_SKU: dict[int, StoreTierBenefits] = {
    1_447_683_957_981_319_169: StoreTierBenefits(
        "Premium Trader", rank=1, post_limit=3, listing_limit=25
    ),
    1_447_725_003_956_293_724: StoreTierBenefits(
        "Elite Trader", rank=2, post_limit=4, listing_limit=35
    ),
    1_447_725_110_529_102_005: StoreTierBenefits(
        "Expert Trader", rank=3, post_limit=5, listing_limit=50
    ),
}

#: Consumable SKU that enables a single global store post across all servers.
GLOBAL_STORE_POST_SKU = 1_447_725_322_802_823_299


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


async def build_store_embeds(
    db: Database,
    user_id: int,
    display_name: str,
    *,
    avatar_url: str | None,
    listing_limit: int,
    store_tier_name: str | None,
    is_premium: bool,
    badge_url: str | None,
    image_url: str | None,
) -> list[discord.Embed]:
    contact, score, count, response_score, response_count, _, _, stored_premium = await db.profile(
        user_id
    )
    latest_review = await db.latest_review_for_user(user_id)
    stock = await db.get_stock(user_id)
    wishlist = await db.get_wishlist(user_id)

    listing_limit = max(1, listing_limit)
    stock_pages = _paginate_field_entries(stock, format_stock, listing_limit)
    wishlist_pages = _paginate_field_entries(wishlist, format_wishlist, listing_limit)
    total_pages = max(len(stock_pages), len(wishlist_pages), 1)

    embeds: list[discord.Embed] = []
    rating_line = rating_summary(score, count)
    response_line = response_summary(response_score, response_count)
    descriptor_lines = [f"{rating_line} • {response_line}"]
    if store_tier_name:
        descriptor_lines.append(f"🏅 {store_tier_name}")
    if contact:
        descriptor_lines.append(f"📞 Contact: {contact}")

    premium_flag = is_premium or bool(stored_premium)

    color = PREMIUM_EMBED_COLOR if premium_flag else DEFAULT_EMBED_COLOR
    author_label = (
        f"{display_name} • {store_tier_name}" if store_tier_name else display_name
    )

    for idx in range(total_pages):
        stock_value = stock_pages[idx] if idx < len(stock_pages) else format_stock([])
        wishlist_value = (
            wishlist_pages[idx] if idx < len(wishlist_pages) else format_wishlist([])
        )
        embed = discord.Embed(
            title=f"🛒 {display_name}'s Store",
            description="\n".join(descriptor_lines),
            color=color,
        )
        if avatar_url:
            embed.set_author(name=author_label, icon_url=avatar_url)
        else:
            embed.set_author(name=author_label)
        thumbnail_url = badge_url or (PREMIUM_BADGE_URL if premium_flag else None)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        embed.add_field(name="📦 Inventory", value=stock_value, inline=False)
        embed.add_field(name="🎯 Wishlist", value=wishlist_value, inline=False)
        if latest_review:
            reviewer_id, review_text, _ = latest_review
            review_value = f"{review_text}\n— <@{reviewer_id}>"
            embed.add_field(
                name="📝 Latest review", value=review_value, inline=False
            )
        if image_url:
            embed.set_image(url=image_url)
        embed.set_footer(text="RH-Trader • Made with ♡ by Kuro")
        embeds.append(embed)

    return embeds


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
        super().__init__(command_prefix=commands.when_mentioned_or("!"), intents=intents)
        self.settings = settings
        self.db = db
        self.catalog = catalog or CatalogClient(settings.catalog_base_url)

    async def setup_hook(self) -> None:
        await self.db.setup()
        self.tree.add_command(StockGroup(self.db))
        self.tree.add_command(TradeGroup(self.db))
        self.tree.add_command(WishlistGroup(self.db))
        await self.add_misc_commands()
        await self.register_persistent_views()
        await self.tree.sync()
        _log.info("Slash commands synced")

    async def register_persistent_views(self) -> None:
        for _, user_id, _, _, listing_limit, tier_name, is_premium, image_url in await self.db.list_trade_posts():
            try:
                user = self.get_user(user_id) or await self.fetch_user(user_id)
                display_name = user.display_name
                avatar_url = user.display_avatar.url
            except discord.HTTPException:
                display_name = f"User {user_id}"
                avatar_url = None

            embeds = await build_store_embeds(
                self.db,
                user_id,
                display_name,
                avatar_url=avatar_url,
                listing_limit=listing_limit or DEFAULT_STORE_LISTING_LIMIT,
                store_tier_name=tier_name or None,
                is_premium=bool(is_premium),
                badge_url=PREMIUM_BADGE_URL if is_premium else None,
                image_url=image_url or None,
            )

            view = StorePostView(
                self.db,
                user_id,
                listing_limit=listing_limit or DEFAULT_STORE_LISTING_LIMIT,
                store_tier_name=tier_name or None,
                is_premium=bool(is_premium),
                badge_url=PREMIUM_BADGE_URL if is_premium else None,
                image_url=image_url or None,
            )
            view.set_page_count(len(embeds))
            self.add_view(view)

        for trade_id, seller_id, buyer_id, item, status in await self.db.list_trades_by_status(
            {"pending", "open"}
        ):
            self.add_view(
                TradeView(
                    self.db, trade_id, seller_id, buyer_id, item, is_seller=True, status=status
                )
            )
            self.add_view(
                TradeView(
                    self.db,
                    trade_id,
                    seller_id,
                    buyer_id,
                    item,
                    is_seller=False,
                    status=status,
                )
            )

        for trade_id, seller_id, buyer_id, item, _ in await self.db.list_trades_by_status(
            {"completed"}
        ):
            self.add_view(
                RatingView(self.db, trade_id, seller_id, buyer_id, "seller", item)
            )
            self.add_view(
                RatingView(self.db, trade_id, buyer_id, seller_id, "buyer", item)
            )

    async def close(self) -> None:
        await self.catalog.close()
        await super().close()

    async def add_misc_commands(self) -> None:
        db = self.db
        @self.tree.command(description="Search community inventories or wishlists for an item")
        @app_commands.describe(
            item="Keyword to search for",
            location="Choose whether to search inventory or wishlist entries",
        )
        @app_commands.choices(
            location=[
                app_commands.Choice(name="Stock", value="stock"),
                app_commands.Choice(name="Wishlist", value="wishlist"),
            ]
        )
        async def search(
            interaction: discord.Interaction, item: str, location: app_commands.Choice[str]
        ):
            async def _filter_guild_members(entries: list[tuple[int, ...]]):
                guild = interaction.guild
                if guild is None:
                    return entries

                filtered: list[tuple[int, ...]] = []
                for entry in entries:
                    user_id = entry[0]
                    member = guild.get_member(user_id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(user_id)
                        except discord.HTTPException:
                            member = None

                    if member is not None:
                        filtered.append(entry)

                return filtered

            if location.value == "wishlist":
                results = await db.search_wishlist(item)
                results = await _filter_guild_members(results)
                description = "\n".join(
                    f"🔍 <@{user_id}> wants **{item}**" + (f" — {note}" if note else "")
                    for user_id, item, note in results
                ) or "No matching wishlist items found from members of this server."
            else:
                results = await db.search_stock(item)
                results = await _filter_guild_members(results)
                description = "\n".join(
                    f"🔍 <@{user_id}> has **{item}** (x{qty})" for user_id, item, qty in results
                ) or "No matching stock items found from members of this server."

            embed = info_embed("🔎 Search results", description)
            await interaction.response.send_message(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False, replied_user=False
                ),
            )

        @self.tree.command(description="Open a quick trading control panel")
        async def storemenu(interaction: discord.Interaction):
            embed = info_embed(
                "🧰 Trade menu",
                (
                    "Use the buttons below to update stock and wishlist entries or share a store post"
                    " without typing slash commands."
                ),
            )
            await interaction.response.send_message(
                embed=embed,
                view=TradeMenuView(self.db, self._handle_store_post),
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
                score,
                count,
                response_score,
                response_count,
                timezone,
                bio,
                stored_premium,
            ) = await db.profile(target.id)
            trades = await db.trade_count(target.id)
            reviews = await db.recent_reviews_for_user(target.id, 3)

            is_self = target.id == interaction.user.id
            premium_tier = self._has_store_premium(interaction) if is_self else None
            if premium_tier is not None:
                await db.set_premium_status(target.id, True)
            premium_flag = bool(premium_tier) or bool(stored_premium)

            trade_label = "trade" if trades == 1 else "trades"
            description_lines = [
                rating_summary(score, count),
                f"🤝 {trades} {trade_label} completed",
                response_summary(response_score, response_count),
                f"💎 Status: {'Premium' if premium_flag else 'Standard user'}",
            ]

            embed = info_embed(
                f"🧾 Profile for {target.display_name}",
                description="\n".join(description_lines),
            )
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

        @self.tree.command(description="View top rated traders")
        async def leaderboard(interaction: discord.Interaction):
            rows = await db.leaderboard()
            if not rows:
                await interaction.response.send_message(
                    embed=info_embed("🏆 Leaderboard", "No ratings yet."),
                )
                return
            description = "\n".join(
                f"{idx+1}. <@{user_id}> — {rating_summary(score, count)}" for idx, (user_id, score, count) in enumerate(rows)
            )
            await interaction.response.send_message(embed=info_embed("🏆 Leaderboard", description))

        @self.tree.command(description="Set the store post channel for this server")
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.describe(channel="Channel where /poststore submissions will be sent")
        async def set_trade_channel(
            interaction: discord.Interaction, channel: discord.TextChannel
        ):
            if interaction.guild is None:
                await interaction.response.send_message(
                    embed=info_embed("🌐 Guild only", "This command can only be used inside a server."),
                    ephemeral=True,
                )
                return

            await db.set_trade_channel(interaction.guild.id, channel.id)
            await interaction.response.send_message(
                embed=info_embed(
                    "✅ Store channel saved",
                    f"Store posts will be sent to {channel.mention}.",
                ),
                ephemeral=True,
            )

        @self.tree.command(name="poststore", description="Post your store to the server board")
        @app_commands.describe(image="Optional image to showcase your items")
        async def poststore(
            interaction: discord.Interaction, image: Optional[discord.Attachment] = None
        ):
            await self._handle_store_post(interaction, image)

    def _has_store_premium(
        self, interaction: discord.Interaction
    ) -> StoreTierBenefits | None:
        entitlements = getattr(interaction, "entitlements", None) or []
        now = discord.utils.utcnow()
        best_tier: StoreTierBenefits | None = None

        for entitlement in entitlements:
            tier = STORE_TIER_BY_SKU.get(getattr(entitlement, "sku_id", None))
            if tier is None:
                continue

            ends_at = getattr(entitlement, "ends_at", None)
            if ends_at is not None and ends_at <= now:
                continue

            if best_tier is None or tier.rank > best_tier.rank:
                best_tier = tier

        return best_tier

    async def _handle_store_post(
        self, interaction: discord.Interaction, image: Optional[discord.Attachment]
    ) -> None:
        db = self.db
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=info_embed(
                    "🌐 Guild only", "You can only post store listings inside a server."
                ),
                ephemeral=True,
            )
            return

        channel_id = await db.get_trade_channel(interaction.guild.id)
        if channel_id is None:
            await interaction.response.send_message(
                embed=info_embed(
                    "⚙️ Store channel not configured",
                    "An admin needs to run /set_trade_channel to pick where store posts go.",
                ),
                ephemeral=True,
            )
            return

        channel = interaction.client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(channel_id)
            except discord.HTTPException:
                channel = None

        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                embed=info_embed(
                    "🚫 Channel unavailable",
                    "I can't find the configured store channel. Please ask an admin to set it again.",
                ),
                ephemeral=True,
            )
            return

        if image and image.content_type and not image.content_type.startswith("image"):
            await interaction.response.send_message(
                embed=info_embed(
                    "🖼️ Invalid image",
                    "The attachment must be an image file (PNG, JPG, GIF).",
                ),
                ephemeral=True,
            )
            return

        await db.trim_store_posts(int(time.time()) - STORE_POST_WINDOW_SECONDS * 2)

        now_ts = int(time.time())
        window_start = now_ts - STORE_POST_WINDOW_SECONDS
        store_tier = self._has_store_premium(interaction)
        await db.set_premium_status(interaction.user.id, bool(store_tier))
        recent_posts, oldest_post = await db.store_post_window(
            interaction.guild.id, interaction.user.id, window_start
        )
        post_limit = store_tier.post_limit if store_tier else DEFAULT_STORE_POST_LIMIT
        if recent_posts >= post_limit:
            retry_after = STORE_POST_WINDOW_SECONDS
            if oldest_post:
                retry_after = max(0, STORE_POST_WINDOW_SECONDS - (now_ts - oldest_post))
            await interaction.response.send_message(
                embed=info_embed(
                    "⏳ Store cooldown",
                    (
                        f"You've reached your store post limit ({post_limit} per hour). "
                        f"Try again in {_format_duration(int(retry_after))}."
                    ),
                ),
                ephemeral=True,
            )
            return

        listing_limit = (
            store_tier.listing_limit if store_tier else DEFAULT_STORE_LISTING_LIMIT
        )
        badge_url = PREMIUM_BADGE_URL if store_tier else None
        image_url = image.url if image else None
        embeds = await build_store_embeds(
            db,
            interaction.user.id,
            interaction.user.display_name,
            avatar_url=interaction.user.display_avatar.url,
            listing_limit=listing_limit,
            store_tier_name=store_tier.name if store_tier else None,
            is_premium=bool(store_tier),
            badge_url=badge_url,
            image_url=image_url,
        )

        view = StorePostView(
            db,
            interaction.user.id,
            interaction.user.display_name,
            listing_limit=listing_limit,
            store_tier_name=store_tier.name if store_tier else None,
            is_premium=bool(store_tier),
            badge_url=badge_url,
            image_url=image_url,
        )
        view.set_page_count(len(embeds))
        previous_post = await db.get_trade_post(interaction.guild.id, interaction.user.id)
        if previous_post:
            prev_channel_id, prev_message_id, *_ = previous_post
            prev_channel = interaction.client.get_channel(prev_channel_id)
            if prev_channel is None:
                try:
                    prev_channel = await interaction.client.fetch_channel(prev_channel_id)
                except discord.HTTPException:
                    prev_channel = None

            if isinstance(prev_channel, discord.TextChannel):
                try:
                    message_to_delete = await prev_channel.fetch_message(prev_message_id)
                    await message_to_delete.delete()
                except discord.NotFound:
                    await db.delete_trade_post(interaction.guild.id, interaction.user.id)
                except discord.HTTPException:
                    _log.warning(
                        "Failed to delete previous store post %s for user %s in guild %s",
                        prev_message_id,
                        interaction.user.id,
                        interaction.guild.id,
            )

        try:
            message = await channel.send(embed=embeds[0], view=view)
        except discord.HTTPException:
            await interaction.response.send_message(
                embed=info_embed(
                    "🚫 Cannot post",
                    f"I don't have permission to send messages in {channel.mention}.",
                ),
                ephemeral=True,
            )
            return

        await db.record_store_post(interaction.guild.id, interaction.user.id, now_ts)
        await interaction.response.send_message(
            embed=info_embed(
                "🏪 Store posted",
                f"Your store has been shared in {channel.mention}.",
            ),
            ephemeral=True,
        )
        await db.save_trade_post(
            interaction.guild.id,
            interaction.user.id,
            channel.id,
            message.id,
            listing_limit=listing_limit,
            store_tier_name=store_tier.name if store_tier else None,
            is_premium=bool(store_tier),
            image_url=image_url,
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        await super().on_message(message)
        if not isinstance(message.channel, discord.DMChannel):
            return
        trade = await self.db.get_active_trade_for_user(message.author.id)
        if not trade:
            open_trades = await self.db.list_open_trades_for_user(message.author.id)
            if not open_trades:
                return
            if len(open_trades) > 1:
                await message.channel.send(
                    embed=info_embed(
                        "🎯 Pick an active trade",
                        (
                            "You have multiple open trades. Use the **Set Active Trade** button"
                            " on the trade card you want to chat about so I know who to DM."
                        ),
                    ),
                    reference=message,
                )
                return
            trade = open_trades[0]
            await self.db.set_active_trade(message.author.id, trade[0])
        trade_id, seller_id, buyer_id, item, _ = trade
        partner_id = buyer_id if message.author.id == seller_id else seller_id
        try:
            partner = self.get_user(partner_id) or await self.fetch_user(partner_id)
        except discord.HTTPException:
            _log.warning("Failed to fetch partner %s for trade %s", partner_id, trade_id)
            return

        content = message.content.strip()
        attachment_lines = [f"{attachment.filename}: {attachment.url}" for attachment in message.attachments]
        payload = content or "[No message content]"
        if attachment_lines:
            payload += "\nAttachments:\n" + "\n".join(attachment_lines)

        try:
            await partner.send(
                embed=info_embed(
                    f"📨 Trade #{trade_id} update",
                    f"Message from <@{message.author.id}> regarding **{item}**:\n{payload}",
                )
            )
            await message.add_reaction("📨")
        except discord.HTTPException:
            _log.warning("Failed to relay message for trade %s", trade_id)


class StockGroup(app_commands.Group):
    def __init__(self, db: Database):
        super().__init__(name="stock", description="Manage your stock list")
        self.db = db

    @app_commands.command(name="add", description="Add an item to your stock list")
    @app_commands.describe(item="Item name", quantity="How many you have")
    async def add(self, interaction: discord.Interaction, item: str, quantity: int = 1):
        qty = max(1, quantity)
        await self.db.add_stock(interaction.user.id, item, qty)
        embed = info_embed("📦 Stock updated", f"Added **{item}** x{qty} to your inventory.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="change", description="Change the quantity for an item in your stock list"
    )
    @app_commands.describe(
        item="Item to update (fuzzy matched against your stock)",
        quantity="New quantity you have",
    )
    async def change(self, interaction: discord.Interaction, item: str, quantity: int):
        stock = await self.db.get_stock(interaction.user.id)
        if not stock:
            await interaction.response.send_message(
                embed=info_embed("No stock found", "Add something first to change it."),
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
                    "I couldn't find anything that looks like that in your stock.",
                ),
                ephemeral=True,
            )
            return

        best_name = match[0]
        current_qty = next(qty for name, qty in stock if name == best_name)
        new_qty = max(0, quantity)
        await self.db.update_stock_quantity(interaction.user.id, best_name, new_qty)

        if new_qty == 0:
            message = f"Removed **{best_name}** from your stock."
        else:
            message = f"Updated **{best_name}** to x{new_qty} (was x{current_qty})."

        await interaction.response.send_message(
            embed=info_embed("📦 Stock updated", message),
            ephemeral=True,
        )

    @app_commands.command(name="view", description="View stock for you or another member")
    @app_commands.describe(user="Member to view")
    async def view(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        target = user or interaction.user
        if not _can_view_other(interaction, target):
            await interaction.response.send_message(
                embed=info_embed("🚫 Permission denied", "You can only view your own stock."),
                ephemeral=True,
            )
            return
        items = await self.db.get_stock(target.id)
        embed = info_embed(f"📦 Inventory for {target.display_name}", format_stock(items))
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="remove", description="Remove an item from your stock list")
    @app_commands.describe(item="Item to remove (fuzzy matched against your stock)")
    async def remove(self, interaction: discord.Interaction, item: str):
        stock = await self.db.get_stock(interaction.user.id)
        if not stock:
            await interaction.response.send_message(
                embed=info_embed("No stock found", "Add something first to remove it."),
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
                    "I couldn't find anything that looks like that in your stock.",
                ),
                ephemeral=True,
            )
            return

        best_name = match[0]
        removed = await self.db.remove_stock(interaction.user.id, best_name)
        message = (
            f"Removed **{best_name}** from your stock." if removed else "Item not found anymore."
        )
        await interaction.response.send_message(
            embed=info_embed("🧹 Stock cleanup", message),
            ephemeral=True,
        )

    @app_commands.command(name="clear", description="Clear all items from your stock list")
    async def clear(self, interaction: discord.Interaction):
        await self.db.clear_stock(interaction.user.id)
        embed = info_embed("🗑️ Stock cleared", "Your inventory list is now empty.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class TradeGroup(app_commands.Group):
    def __init__(self, db: Database):
        super().__init__(name="trade", description="Manage trades and ratings")
        self.db = db

    @app_commands.command(
        name="kudos", description="Give someone a quick star rating outside a trade"
    )
    @app_commands.describe(
        partner="Member you want to rate", score="Number of stars to award (1-5)"
    )
    async def kudos(
        self,
        interaction: discord.Interaction,
        partner: discord.Member,
        score: app_commands.Range[int, 1, 5],
    ):
        if partner.id == interaction.user.id:
            await interaction.response.send_message(
                embed=info_embed(
                    "🚫 Invalid target", "You cannot give kudos to yourself."
                ),
                ephemeral=True,
            )
            return

        recorded, retry_after = await self.db.record_quick_rating(
            interaction.user.id,
            partner.id,
            score,
            QUICK_RATING_COOLDOWN_SECONDS,
        )
        if not recorded:
            wait_label = _format_duration(int(retry_after or 0))
            await interaction.response.send_message(
                embed=info_embed(
                    "⏳ On cooldown",
                    f"You recently rated {partner.mention}. You can send another kudos in {wait_label}.",
                ),
                ephemeral=True,
            )
            return

        (
            _,
            avg_score,
            rating_count,
            response_score,
            response_count,
            *_,
        ) = await self.db.profile(partner.id)
        await interaction.response.send_message(
            embed=info_embed(
                "⭐ Kudos sent",
                (
                    f"You rated {partner.mention} {score} star(s).\n"
                    f"Their profile now shows: {rating_summary(avg_score, rating_count)}"
                    f" • {response_summary(response_score, response_count)}"
                ),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="start", description="Open a trade and move the conversation to DMs")
    @app_commands.describe(partner="Partner involved in the trade", item="Item or service being traded")
    async def start(
        self,
        interaction: discord.Interaction,
        partner: discord.Member,
        item: str,
    ):
        await start_trade_flow(interaction, self.db, interaction.user, partner, item)


class WishlistGroup(app_commands.Group):
    def __init__(self, db: Database):
        super().__init__(name="wishlist", description="Track items you want")
        self.db = db

    @app_commands.command(name="add", description="Add an item to your wishlist")
    @app_commands.describe(item="Item to add", note="Optional note like target price")
    async def add(self, interaction: discord.Interaction, item: str, note: str = ""):
        await self.db.add_wishlist(interaction.user.id, item, note)
        embed = info_embed("🎯 Wishlist updated", f"Added **{item}** to your wishlist.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="view", description="View wishlist for you or another member")
    @app_commands.describe(user="Member to view")
    async def view(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
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


class StockAddModal(discord.ui.Modal):
    def __init__(self, db: Database):
        super().__init__(title="Add to stock")
        self.db = db
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
        if not item_name:
            await interaction.response.send_message(
                embed=info_embed("⚠️ Item required", "Please enter an item name."),
                ephemeral=True,
            )
            return

        await self.db.add_stock(interaction.user.id, item_name, qty)
        embed = info_embed("📦 Stock updated", f"Added **{item_name}** x{qty} to your inventory.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class WishlistAddModal(discord.ui.Modal):
    def __init__(self, db: Database):
        super().__init__(title="Add to wishlist")
        self.db = db
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
        if not item_name:
            await interaction.response.send_message(
                embed=info_embed("⚠️ Item required", "Please enter an item name."),
                ephemeral=True,
            )
            return

        await self.db.add_wishlist(interaction.user.id, item_name, note)
        embed = info_embed(
            "🎯 Wishlist updated",
            f"Added **{item_name}** to your wishlist." + (f" Note: {note}" if note else ""),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RemoveStockModal(discord.ui.Modal):
    def __init__(self, db: Database):
        super().__init__(title="Remove from stock")
        self.db = db
        self.item_input = discord.ui.TextInput(
            label="Stock item",
            placeholder="What do you want to remove?",
            max_length=100,
        )
        self.add_item(self.item_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        stock = await self.db.get_stock(interaction.user.id)
        if not stock:
            await interaction.response.send_message(
                embed=info_embed("No stock found", "Add something first to remove it."),
                ephemeral=True,
            )
            return

        term = self.item_input.value.strip()
        candidates = [name for name, _ in stock]
        match = process.extractOne(term, candidates, scorer=fuzz.WRatio)
        if not match or match[1] < 60:
            await interaction.response.send_message(
                embed=info_embed(
                    "🔍 No close match",
                    "I couldn't find anything that looks like that in your stock.",
                ),
                ephemeral=True,
            )
            return

        best_name = match[0]
        removed = await self.db.remove_stock(interaction.user.id, best_name)
        message = (
            f"Removed **{best_name}** from your stock." if removed else "Item not found anymore."
        )
        await interaction.response.send_message(
            embed=info_embed("🧹 Stock cleanup", message),
            ephemeral=True,
        )


class RemoveWishlistModal(discord.ui.Modal):
    def __init__(self, db: Database):
        super().__init__(title="Remove from wishlist")
        self.db = db
        self.item_input = discord.ui.TextInput(
            label="Wishlist item",
            placeholder="What do you want to drop?",
            max_length=100,
        )
        self.add_item(self.item_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        wishlist = await self.db.get_wishlist(interaction.user.id)
        if not wishlist:
            await interaction.response.send_message(
                embed=info_embed("No wishlist items", "Add items before removing them."),
                ephemeral=True,
            )
            return

        term = self.item_input.value.strip()
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


class TradeMenuView(discord.ui.View):
    def __init__(
        self,
        db: Database,
        store_post_handler: Callable[[discord.Interaction, Optional[discord.Attachment]], Awaitable[None]],
    ):
        super().__init__(timeout=600)
        self.db = db
        self._store_post_handler = store_post_handler

    async def _send_snapshot(self, interaction: discord.Interaction) -> None:
        stock = await self.db.get_stock(interaction.user.id)
        wishlist = await self.db.get_wishlist(interaction.user.id)
        embed = info_embed(
            "📋 Your inventory snapshot",
            "Quick view of your lists.",
        )
        embed.add_field(name="📦 Inventory", value=format_stock(stock), inline=False)
        embed.add_field(name="🎯 Wishlist", value=format_wishlist(wishlist), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _confirm_clear_stock(self, interaction: discord.Interaction) -> None:
        async def confirm(inter: discord.Interaction) -> None:
            await self.db.clear_stock(inter.user.id)
            await inter.response.edit_message(
                embed=info_embed("🧹 Stock cleared", "Your inventory list is now empty."),
                view=None,
            )

        view = ConfirmClearView(confirm)
        await interaction.response.send_message(
            embed=info_embed("Confirm", "This will remove all stock entries."),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Add Stock", style=discord.ButtonStyle.primary, emoji="🧺", row=0)
    async def stock_add(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(StockAddModal(self.db))

    @discord.ui.button(label="Remove Stock", style=discord.ButtonStyle.secondary, emoji="➖", row=0)
    async def stock_remove(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(RemoveStockModal(self.db))

    @discord.ui.button(label="Clear Stock", style=discord.ButtonStyle.danger, emoji="🧹", row=0)
    async def stock_clear(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._confirm_clear_stock(interaction)

    @discord.ui.button(label="View Lists", style=discord.ButtonStyle.secondary, emoji="📋", row=1)
    async def view_lists(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._send_snapshot(interaction)

    @discord.ui.button(label="Add Wishlist", style=discord.ButtonStyle.primary, emoji="📌", row=1)
    async def wishlist_add(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(WishlistAddModal(self.db))

    @discord.ui.button(
        label="Remove Wishlist", style=discord.ButtonStyle.secondary, emoji="🗑️", row=1
    )
    async def wishlist_remove(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(RemoveWishlistModal(self.db))

    @discord.ui.button(label="Set Bio", style=discord.ButtonStyle.secondary, emoji="✍️", row=2)
    async def set_bio(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(BioModal(self.db))

    @discord.ui.button(label="Set Time Zone", style=discord.ButtonStyle.secondary, emoji="🕰️", row=2)
    async def set_timezone(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(TimezoneModal(self.db))

    @discord.ui.button(label="Post Store", style=discord.ButtonStyle.primary, emoji="🏪", row=2)
    async def poststore(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._store_post_handler(interaction, None)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.primary, emoji="🚪", row=2)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            embed=info_embed("Closed", "You can reopen /storemenu anytime."), view=None
        )


async def send_trade_invites(
    bot: commands.Bot,
    db: Database,
    trade_id: int,
    item: str,
    seller_id: int,
    buyer_id: int,
    *,
    status: str = "pending",
) -> List[int]:
    failed_ids: List[int] = []
    for user_id in (seller_id, buyer_id):
        partner_id = buyer_id if user_id == seller_id else seller_id
        try:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            is_seller = user_id == seller_id
            stats_line = ""
            if is_seller and status == "pending":
                (
                    _,
                    score,
                    rating_count,
                    response_score,
                    response_count,
                    *_,
                ) = await db.profile(partner_id)
                trades = await db.trade_count(partner_id)
                trade_label = "trade" if trades == 1 else "trades"
                stats_line = (
                    f"\nTrader stats for <@{partner_id}>: {rating_summary(score, rating_count)}"
                    f" • {response_summary(response_score, response_count)}"
                    f" • {trades} {trade_label} completed."
                )
            pending_note = (
                "\nPress **Accept Trade** to start or **Reject Trade** to decline."
                if is_seller and status == "pending"
                else "\nWaiting for your partner to accept the trade."
            )
            await user.send(
                embed=info_embed(
                    f"🤝 Trade #{trade_id} started",
                    (
                        f"Trade for **{item}** with <@{partner_id}>.\n"
                        "Reply in this DM to send messages to your partner."
                        f"{pending_note}"
                        f"{stats_line}"
                    ),
                ),
                view=TradeView(
                    db,
                    trade_id,
                    seller_id,
                    buyer_id,
                    item,
                    is_seller=is_seller,
                    status=status,
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            failed_ids.append(user_id)
            _log.warning("Failed to DM user %s for trade %s", user_id, trade_id)
    return failed_ids


async def start_trade_flow(
    interaction: discord.Interaction,
    db: Database,
    initiator: discord.abc.User,
    partner: discord.abc.User,
    item: str,
) -> None:
    await interaction.response.defer(ephemeral=True)

    cleaned_item = item.strip()
    if not cleaned_item:
        await interaction.followup.send(
            embed=info_embed("⚠️ Invalid item", "Please provide an item to trade."),
            ephemeral=True,
        )
        return

    if partner.id == initiator.id:
        await interaction.followup.send(
            embed=info_embed("⚠️ Invalid trade", "You cannot open a trade with yourself."),
            ephemeral=True,
        )
        return

    seller_id = partner.id
    buyer_id = initiator.id
    trade_id = await db.create_trade(seller_id, buyer_id, cleaned_item)
    failed_dm_ids = await send_trade_invites(
        interaction.client, db, trade_id, cleaned_item, seller_id, buyer_id
    )
    if failed_dm_ids:
        await db.delete_trade(trade_id)
        targets = " and ".join(f"<@{user_id}>" for user_id in failed_dm_ids)
        await interaction.followup.send(
            embed=info_embed(
                "🚫 Cannot start trade",
                (
                    f"I couldn't DM {targets}. They may have privacy settings or blocks enabled.\n"
                    "The trade was not started."
                ),
            ),
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        embed=info_embed(
            "🤝 Trade opened",
            f"Trade #{trade_id} created with {partner.mention} for **{cleaned_item}**. Check your DMs to continue.",
        ),
        ephemeral=True,
    )


async def send_rating_prompts(
    bot: commands.Bot, db: Database, trade_id: int, item: str, seller_id: int, buyer_id: int
) -> None:
    for user_id in (seller_id, buyer_id):
        partner_id = buyer_id if user_id == seller_id else seller_id
        role_value = "seller" if user_id == seller_id else "buyer"
        try:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            await user.send(
                embed=info_embed(
                    "⭐ Rate your partner",
                    (
                        f"Trade #{trade_id} for **{item}** is complete.\n"
                        f"You traded with <@{partner_id}>."
                    ),
                ),
                view=RatingView(db, trade_id, user_id, partner_id, role_value, item),
            )
        except discord.HTTPException:
            _log.warning("Failed to send rating prompt to %s for trade %s", user_id, trade_id)


class BasePersistentView(discord.ui.View):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("timeout", None)
        super().__init__(*args, **kwargs)

    def disable_all_items(self) -> None:
        for item in self.children:
            item.disabled = True


class TradeRequestModal(discord.ui.Modal):
    def __init__(self, db: Database, partner: discord.abc.User):
        super().__init__(title="Start a trade")
        self.db = db
        self.partner = partner
        self.item_input = discord.ui.TextInput(
            label="Item you want to trade", placeholder="Ex: Halo Outfit"
        )
        self.add_item(self.item_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await start_trade_flow(
            interaction, self.db, interaction.user, self.partner, self.item_input.value
        )


class ReviewModal(discord.ui.Modal):
    def __init__(
        self, db: Database, trade_id: int, reviewer_id: int, target_id: int, item: str
    ):
        super().__init__(title="Leave a review")
        self.db = db
        self.trade_id = trade_id
        self.reviewer_id = reviewer_id
        self.target_id = target_id
        self.item = item
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
                "Make sure you've rated your partner before submitting a review.",
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class StorePostView(BasePersistentView):
    def __init__(
        self,
        db: Database,
        poster_id: int,
        poster_name: str | None = None,
        *,
        listing_limit: int = DEFAULT_STORE_LISTING_LIMIT,
        store_tier_name: str | None = None,
        is_premium: bool = False,
        badge_url: str | None = None,
        image_url: str | None = None,
    ):
        super().__init__()
        self.db = db
        self.poster_id = poster_id
        self.poster_name = poster_name or ""
        self.listing_limit = listing_limit or DEFAULT_STORE_LISTING_LIMIT
        self.store_tier_name = store_tier_name or None
        self.is_premium = is_premium
        self.badge_url = badge_url
        self.image_url = image_url
        self.current_page = 0
        self.page_count = 1
        self.start_trade.custom_id = f"store:start:{poster_id}"
        self.previous_page.custom_id = f"store:page:{poster_id}:prev"
        self.next_page.custom_id = f"store:page:{poster_id}:next"
        self._sync_nav_buttons()

    def set_page_count(self, count: int) -> None:
        self.page_count = max(1, count)
        self._sync_nav_buttons()

    def _sync_nav_buttons(self) -> None:
        disable_nav = self.page_count <= 1
        self.previous_page.disabled = disable_nav
        self.next_page.disabled = disable_nav

    async def _load_pages(self, interaction: discord.Interaction) -> list[discord.Embed]:
        try:
            user = interaction.client.get_user(self.poster_id) or await interaction.client.fetch_user(
                self.poster_id
            )
            avatar_url = user.display_avatar.url
            display_name = user.display_name
        except discord.HTTPException:
            avatar_url = None
            display_name = self.poster_name or f"User {self.poster_id}"

        embeds = await build_store_embeds(
            self.db,
            self.poster_id,
            display_name,
            avatar_url=avatar_url,
            listing_limit=self.listing_limit,
            store_tier_name=self.store_tier_name,
            is_premium=self.is_premium,
            badge_url=self.badge_url,
            image_url=self.image_url,
        )
        self.set_page_count(len(embeds))
        return embeds

    async def _change_page(self, interaction: discord.Interaction, delta: int) -> None:
        embeds = await self._load_pages(interaction)
        total = len(embeds) or 1
        self.current_page = (self.current_page + delta) % total
        self._sync_nav_buttons()
        await interaction.response.edit_message(embed=embeds[self.current_page], view=self)

    @discord.ui.button(label="Start Trade", style=discord.ButtonStyle.primary, emoji="🤝")
    async def start_trade(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id == self.poster_id:
            await interaction.response.send_message(
                embed=info_embed("⚠️ Invalid trade", "You cannot start a trade with yourself."),
                ephemeral=True,
            )
            return

        try:
            partner = interaction.client.get_user(self.poster_id) or await interaction.client.fetch_user(
                self.poster_id
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                embed=info_embed(
                    "🚫 User unavailable",
                    "I couldn't contact the trader. Please try again later.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(TradeRequestModal(self.db, partner))

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.page_count <= 1:
            await interaction.response.defer(ephemeral=True)
            return
        await self._change_page(interaction, -1)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.page_count <= 1:
            await interaction.response.defer(ephemeral=True)
            return
        await self._change_page(interaction, 1)


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
    ):
        super().__init__()
        self.db = db
        self.trade_id = trade_id
        self.seller_id = seller_id
        self.buyer_id = buyer_id
        self.item = item
        self.is_seller = is_seller
        self.status = status
        role_label = "seller" if is_seller else "buyer"
        base_custom_id = f"trade:{trade_id}:{role_label}"
        self.set_active_button.custom_id = f"{base_custom_id}:active"
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
        self.set_active_button.disabled = not open_status
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

    @discord.ui.button(label="Set Active Trade", style=discord.ButtonStyle.primary, emoji="🎯")
    async def set_active_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        trade = await self._get_trade(interaction)
        if trade is None:
            return
        if self.status != "open":
            await interaction.response.send_message(
                embed=info_embed(
                    "⏳ Waiting for acceptance",
                    "Your partner must accept the trade before you can set it as active.",
                ),
                ephemeral=True,
            )
            return

        saved = await self.db.set_active_trade(interaction.user.id, self.trade_id)
        if not saved:
            await interaction.response.send_message(
                embed=info_embed(
                    "🚫 Trade not active",
                    "I couldn't mark this trade as active. Make sure it's still open.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=info_embed(
                "🎯 Active trade set",
                "I'll forward your DM replies to this trade partner.",
            ),
            ephemeral=True,
        )

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
        await self.db.clear_active_trade(self.seller_id, self.trade_id)
        await self.db.clear_active_trade(self.buyer_id, self.trade_id)
        await send_rating_prompts(interaction.client, self.db, self.trade_id, self.item, self.seller_id, self.buyer_id)

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
        await self.db.clear_active_trade(self.seller_id, self.trade_id)
        await self.db.clear_active_trade(self.buyer_id, self.trade_id)

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

        await self.db.set_active_trade(self.seller_id, self.trade_id)
        await self.db.set_active_trade(self.buyer_id, self.trade_id)

        await interaction.response.send_message(
            embed=info_embed(
                "✅ Trade accepted",
                "You can now chat in this DM and mark the trade active when ready.",
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
                        "Use the buttons below to set this as your active DM thread or close it."
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


class RatingView(BasePersistentView):
    def __init__(
        self, db: Database, trade_id: int, rater_id: int, partner_id: int, role: str, item: str
    ) -> None:
        super().__init__()
        self.db = db
        self.trade_id = trade_id
        self.rater_id = rater_id
        self.partner_id = partner_id
        self.role = role
        self.item = item
        prefix = f"rating:{trade_id}:{rater_id}:{partner_id}:{role}"
        self.rate_one.custom_id = f"{prefix}:1"
        self.rate_two.custom_id = f"{prefix}:2"
        self.rate_three.custom_id = f"{prefix}:3"
        self.rate_four.custom_id = f"{prefix}:4"
        self.rate_five.custom_id = f"{prefix}:5"
        self.leave_review_button.custom_id = f"{prefix}:review"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.rater_id:
            await interaction.response.send_message(
                embed=info_embed("🚫 Not your rating", "Only this trade participant can submit this rating."),
                ephemeral=True,
            )
            return False
        return True

    def _disable_rating_buttons(self) -> None:
        for button in (
            self.rate_one,
            self.rate_two,
            self.rate_three,
            self.rate_four,
            self.rate_five,
        ):
            button.disabled = True

    async def _handle_rating(self, interaction: discord.Interaction, score: int) -> None:
        recorded = await self.db.record_trade_rating(
            self.trade_id, self.rater_id, self.partner_id, score, self.role
        )
        self._disable_rating_buttons()
        embed = info_embed(
            "⭐ Rating received" if recorded else "ℹ️ Rating already recorded",
            f"You rated <@{self.partner_id}> {score} star(s) for **{self.item}**."
            if recorded
            else "You have already submitted feedback for this trade.",
        )
        if recorded:
            embed.description += "\nTap **Leave Review** to share a short note about this trade (optional)."
        else:
            embed.description += "\nYou can still use **Leave Review** to update your written feedback."
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="1", style=discord.ButtonStyle.gray)
    async def rate_one(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._handle_rating(interaction, 1)

    @discord.ui.button(label="2", style=discord.ButtonStyle.gray)
    async def rate_two(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._handle_rating(interaction, 2)

    @discord.ui.button(label="3", style=discord.ButtonStyle.primary)
    async def rate_three(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._handle_rating(interaction, 3)

    @discord.ui.button(label="4", style=discord.ButtonStyle.primary)
    async def rate_four(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._handle_rating(interaction, 4)

    @discord.ui.button(label="5", style=discord.ButtonStyle.success)
    async def rate_five(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._handle_rating(interaction, 5)

    @discord.ui.button(
        label="Leave Review (optional)",
        style=discord.ButtonStyle.secondary,
        emoji="📝",
        row=1,
    )
    async def leave_review_button(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ):
        has_rating = await self.db.has_trade_rating(self.trade_id, self.rater_id)
        if not has_rating:
            await interaction.response.send_message(
                embed=info_embed(
                    "⭐ Rate first",
                    "Please submit a star rating before leaving a review.",
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
            )
        )


def run_bot() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    bot = TraderBot(settings, Database(settings.database_path))
    bot.run(settings.discord_token)


if __name__ == "__main__":
    run_bot()
