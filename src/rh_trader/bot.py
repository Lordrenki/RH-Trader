"""Discord bot entrypoint and command registration."""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .config import Settings, load_settings
from .database import Database
from .embeds import format_stock, format_wishlist, info_embed, rating_summary

_log = logging.getLogger(__name__)


def _can_view_other(interaction: discord.Interaction, target: discord.User | discord.Member) -> bool:
    if interaction.user.id == target.id:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.manage_guild or perms.administrator or perms.manage_messages))


class TraderBot(commands.Bot):
    """Discord bot that exposes trading slash commands."""

    def __init__(self, settings: Settings, db: Database) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix=commands.when_mentioned_or("!"), intents=intents)
        self.settings = settings
        self.db = db

    async def setup_hook(self) -> None:
        await self.db.setup()
        self.tree.add_command(StockGroup(self.db))
        self.tree.add_command(TradeGroup(self.db))
        self.tree.add_command(WishlistGroup(self.db))
        await self.add_misc_commands()
        await self.tree.sync()
        _log.info("Slash commands synced")

    async def add_misc_commands(self) -> None:
        db = self.db
        @self.tree.command(description="Search community inventories for an item")
        @app_commands.describe(term="Keyword to search for")
        async def search(interaction: discord.Interaction, term: str):
            results = await db.search_stock(term)
            description = "\n".join(
                f"🔍 <@{user_id}> has **{item}** (x{qty})" for user_id, item, qty in results
            ) or "No matching items found."
            embed = info_embed("🔎 Search results", description)
            await interaction.response.send_message(embed=embed)

        @self.tree.command(description="Show a trading profile")
        @app_commands.describe(user="Optionally view another member's profile")
        async def profile(interaction: discord.Interaction, user: Optional[discord.Member] = None):
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

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        await super().on_message(message)
        if not isinstance(message.channel, discord.DMChannel):
            return
        trade = await self.db.latest_open_trade_for_user(message.author.id)
        if not trade:
            return
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

    @app_commands.command(name="view", description="View stock for you or another member")
    @app_commands.describe(user="Member to view; requires manager permissions")
    async def view(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
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
    @app_commands.describe(item="Item name")
    async def remove(self, interaction: discord.Interaction, item: str):
        deleted = await self.db.remove_stock(interaction.user.id, item)
        message = "Item removed." if deleted else "Item not found in your stock."
        embed = info_embed("🧹 Stock cleanup", message)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="clear", description="Clear all items from your stock list")
    async def clear(self, interaction: discord.Interaction):
        await self.db.clear_stock(interaction.user.id)
        embed = info_embed("🗑️ Stock cleared", "Your inventory list is now empty.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class TradeGroup(app_commands.Group):
    def __init__(self, db: Database):
        super().__init__(name="trade", description="Manage trades and ratings")
        self.db = db

    @app_commands.command(name="start", description="Open a trade and move the conversation to DMs")
    @app_commands.describe(partner="Partner involved in the trade", item="Item or service being traded", role="Are you the seller or buyer?")
    @app_commands.choices(
        role=[
            app_commands.Choice(name="Seller", value="seller"),
            app_commands.Choice(name="Buyer", value="buyer"),
        ]
    )
    async def start(
        self,
        interaction: discord.Interaction,
        partner: discord.Member,
        item: str,
        role: app_commands.Choice[str],
    ):
        if partner.id == interaction.user.id:
            await interaction.response.send_message(
                embed=info_embed("⚠️ Invalid trade", "You cannot open a trade with yourself."),
                ephemeral=True,
            )
            return

        seller_id = interaction.user.id if role.value == "seller" else partner.id
        buyer_id = partner.id if role.value == "seller" else interaction.user.id
        trade_id = await self.db.create_trade(seller_id, buyer_id, item)
        await interaction.response.send_message(
            embed=info_embed(
                "🤝 Trade opened",
                f"Trade #{trade_id} created with {partner.mention} for **{item}**. Check your DMs to continue.",
            ),
            ephemeral=True,
        )
        await send_trade_invites(interaction.client, self.db, trade_id, item, seller_id, buyer_id)


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
    @app_commands.describe(user="Member to view; requires manager permissions")
    async def view(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
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
    @app_commands.describe(item="Item to remove")
    async def remove(self, interaction: discord.Interaction, item: str):
        removed = await self.db.remove_wishlist(interaction.user.id, item)
        message = "Wishlist item removed." if removed else "Item not found on your wishlist."
        embed = info_embed("🧹 Wishlist cleanup", message)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def send_trade_invites(
    bot: commands.Bot, db: Database, trade_id: int, item: str, seller_id: int, buyer_id: int
) -> None:
    for user_id in (seller_id, buyer_id):
        role_label = "Seller" if user_id == seller_id else "Buyer"
        partner_id = buyer_id if user_id == seller_id else seller_id
        try:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            await user.send(
                embed=info_embed(
                    f"🤝 Trade #{trade_id} started",
                    (
                        f"You are the **{role_label}** for **{item}** with <@{partner_id}>.\n"
                        "Reply in this DM to send messages to your partner."
                    ),
                ),
                view=TradeView(db, trade_id, seller_id, buyer_id, item),
            )
        except discord.HTTPException:
            _log.warning("Failed to DM user %s for trade %s", user_id, trade_id)


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
                        f"You traded with <@{partner_id}> as the {role_value}."
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


class TradeView(BasePersistentView):
    def __init__(self, db: Database, trade_id: int, seller_id: int, buyer_id: int, item: str):
        super().__init__()
        self.db = db
        self.trade_id = trade_id
        self.seller_id = seller_id
        self.buyer_id = buyer_id
        self.item = item

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in {self.seller_id, self.buyer_id}:
            await interaction.response.send_message(
                embed=info_embed("🚫 Not your trade", "Only participants can manage this trade."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Mark Trade Completed", style=discord.ButtonStyle.green, emoji="✅")
    async def complete_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        updated = await self.db.complete_trade(self.trade_id)
        if not updated:
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
        await send_rating_prompts(interaction.client, self.db, self.trade_id, self.item, self.seller_id, self.buyer_id)


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
