"""Discord bot entrypoint and command registration."""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Awaitable, Callable, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from rapidfuzz import fuzz, process

from .catalog import CatalogClient
from .config import Settings, load_settings
from .database import Database
from .embeds import format_stock, format_wishlist, info_embed, rating_summary

_log = logging.getLogger(__name__)
QUICK_RATING_COOLDOWN_SECONDS = 24 * 60 * 60
STORE_POST_WINDOW_SECONDS = 60 * 60
DEFAULT_STORE_POST_LIMIT = 1
PREMIUM_STORE_POST_LIMIT = 3
PREMIUM_STORE_SKU_ID = 1_447_683_957_981_319_169


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
        for _, user_id, _, _ in await self.db.list_trade_posts():
            self.add_view(StorePostView(self.db, user_id))

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
        async def trademenu(interaction: discord.Interaction):
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

            _, score, count = await db.profile(target.id)
            stock = await db.get_stock(target.id)
            wishlist = await db.get_wishlist(target.id)
            embed = info_embed(
                f"🧾 Profile for {target.display_name}",
                description=rating_summary(score, count),
            )
            embed.add_field(name="Inventory", value=format_stock(stock), inline=False)
            embed.add_field(name="Wishlist", value=format_wishlist(wishlist), inline=False)
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

    def _has_store_premium(self, interaction: discord.Interaction) -> bool:
        entitlements = getattr(interaction, "entitlements", None) or []
        now = discord.utils.utcnow()

        for entitlement in entitlements:
            if getattr(entitlement, "sku_id", None) != PREMIUM_STORE_SKU_ID:
                continue

            ends_at = getattr(entitlement, "ends_at", None)
            if ends_at is None or ends_at > now:
                return True

        return False

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
        recent_posts, oldest_post = await db.store_post_window(
            interaction.guild.id, interaction.user.id, window_start
        )
        post_limit = (
            PREMIUM_STORE_POST_LIMIT
            if self._has_store_premium(interaction)
            else DEFAULT_STORE_POST_LIMIT
        )
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

        contact, score, count = await db.profile(interaction.user.id)
        stock = await db.get_stock(interaction.user.id)
        wishlist = await db.get_wishlist(interaction.user.id)

        description_lines = [rating_summary(score, count)]
        if contact:
            description_lines.append(f"📞 Contact: {contact}")
        description_lines.append(
            "Press **Start Trade** below or use `/trade start` to begin a DM with this trader."
        )
        embed = info_embed(
            f"🛒 Store post from {interaction.user.display_name}",
            "\n".join(description_lines),
        )
        embed.set_author(
            name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url
        )
        embed.add_field(name="Inventory", value=format_stock(stock), inline=False)
        embed.add_field(name="Wishlist", value=format_wishlist(wishlist), inline=False)
        if image:
            embed.set_image(url=image.url)

        view = StorePostView(db, interaction.user.id, interaction.user.display_name)
        previous_post = await db.get_trade_post(interaction.guild.id, interaction.user.id)
        if previous_post:
            prev_channel_id, prev_message_id = previous_post
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
            message = await channel.send(embed=embed, view=view)
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
            interaction.guild.id, interaction.user.id, channel.id, message.id
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

        _, avg_score, rating_count = await self.db.profile(partner.id)
        await interaction.response.send_message(
            embed=info_embed(
                "⭐ Kudos sent",
                (
                    f"You rated {partner.mention} {score} star(s).\n"
                    f"Their profile now shows: {rating_summary(avg_score, rating_count)}"
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


class TradeMenuView(discord.ui.View):
    def __init__(
        self,
        db: Database,
        store_post_handler: Callable[[discord.Interaction, Optional[discord.Attachment]], Awaitable[None]],
    ):
        super().__init__(timeout=600)
        self.db = db
        self._store_post_handler = store_post_handler

        action_select = discord.ui.Select(
            placeholder="Pick an action",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Stock • Add item",
                    value="stock_add",
                    emoji="🧺",
                    description="Add an item to your stock list.",
                ),
                discord.SelectOption(
                    label="Stock • Remove item",
                    value="stock_remove",
                    emoji="➖",
                    description="Remove an item from your stock list.",
                ),
                discord.SelectOption(
                    label="Stock • Clear all",
                    value="stock_clear",
                    emoji="🧹",
                    description="Delete every item from your stock.",
                ),
                discord.SelectOption(
                    label="Wishlist • Add item",
                    value="wishlist_add",
                    emoji="📌",
                    description="Add something you want.",
                ),
                discord.SelectOption(
                    label="Wishlist • Remove item",
                    value="wishlist_remove",
                    emoji="🗑️",
                    description="Remove something you no longer need.",
                ),
                discord.SelectOption(
                    label="Store • View lists",
                    value="view_lists",
                    emoji="📋",
                    description="Preview your stock and wishlist before posting.",
                ),
            ],
            row=0,
        )

        async def _on_action_select(interaction: discord.Interaction) -> None:
            await self.on_action_select(interaction, action_select)

        action_select.callback = _on_action_select
        self.add_item(action_select)

    async def _send_snapshot(self, interaction: discord.Interaction) -> None:
        stock = await self.db.get_stock(interaction.user.id)
        wishlist = await self.db.get_wishlist(interaction.user.id)
        embed = info_embed(
            "📋 Your inventory snapshot",
            "Quick view of your lists.",
        )
        embed.add_field(name="Inventory", value=format_stock(stock), inline=False)
        embed.add_field(name="Wishlist", value=format_wishlist(wishlist), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Post Store", style=discord.ButtonStyle.primary, emoji="🏪", row=1)
    async def poststore(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._store_post_handler(interaction, None)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.primary, emoji="🚪", row=1)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            embed=info_embed("Closed", "You can reopen /trademenu anytime."), view=None
        )

    async def on_action_select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        selection = select.values[0]
        if selection == "stock_add":
            await interaction.response.send_modal(StockAddModal(self.db))
        elif selection == "stock_remove":
            await interaction.response.send_modal(RemoveStockModal(self.db))
        elif selection == "stock_clear":
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
        elif selection == "wishlist_add":
            await interaction.response.send_modal(WishlistAddModal(self.db))
        elif selection == "wishlist_remove":
            await interaction.response.send_modal(RemoveWishlistModal(self.db))
        elif selection == "view_lists":
            await self._send_snapshot(interaction)


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
                _, score, rating_count = await db.profile(partner_id)
                trades = await db.trade_count(partner_id)
                trade_label = "trade" if trades == 1 else "trades"
                stats_line = (
                    f"\nTrader stats for <@{partner_id}>: {rating_summary(score, rating_count)}"
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


class StorePostView(BasePersistentView):
    def __init__(self, db: Database, poster_id: int, poster_name: str | None = None):
        super().__init__()
        self.db = db
        self.poster_id = poster_id
        self.poster_name = poster_name or ""
        self.start_trade.custom_id = f"store:start:{poster_id}"

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

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.rater_id:
            await interaction.response.send_message(
                embed=info_embed("🚫 Not your rating", "Only this trade participant can submit this rating."),
                ephemeral=True,
            )
            return False
        return True

    async def _handle_rating(self, interaction: discord.Interaction, score: int) -> None:
        recorded = await self.db.record_trade_rating(
            self.trade_id, self.rater_id, self.partner_id, score, self.role
        )
        self.disable_all_items()
        embed = info_embed(
            "⭐ Rating received" if recorded else "ℹ️ Rating already recorded",
            f"You rated <@{self.partner_id}> {score} star(s) for **{self.item}**."
            if recorded
            else "You have already submitted feedback for this trade.",
        )
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


def run_bot() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    bot = TraderBot(settings, Database(settings.database_path))
    bot.run(settings.discord_token)


if __name__ == "__main__":
    run_bot()
