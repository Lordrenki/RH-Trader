"""Discord bot entrypoint and command registration."""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .config import Settings, load_settings
from .database import Database
from .embeds import (
    format_offers,
    format_requests,
    format_stock,
    format_wishlist,
    info_embed,
    rating_summary,
)

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

        @self.tree.command(description="Post an item you have available")
        @app_commands.describe(item="Item name", quantity="Number available", details="Extra details like price")
        async def offer(interaction: discord.Interaction, item: str, quantity: int = 1, details: str = ""):
            await db.add_offer(interaction.user.id, item, max(1, quantity), details)
            embed = info_embed("💰 Offer posted", f"Added **{item}** x{max(1, quantity)} to offers.")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(description="Post an item you want to obtain")
        @app_commands.describe(item="Item name", quantity="Number requested", details="Extra details or trade terms")
        async def request(interaction: discord.Interaction, item: str, quantity: int = 1, details: str = ""):
            await db.add_request(interaction.user.id, item, max(1, quantity), details)
            embed = info_embed("📢 Request posted", f"Requested **{item}** x{max(1, quantity)}.")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(description="Search community inventories for an item")
        @app_commands.describe(term="Keyword to search for")
        async def search(interaction: discord.Interaction, term: str):
            results = await db.search_stock(term)
            description = "\n".join(
                f"🔍 <@{user_id}> has **{item}** (x{qty})" for user_id, item, qty in results
            ) or "No matching items found."
            embed = info_embed("🔎 Search results", description)
            await interaction.response.send_message(embed=embed)

        @self.tree.command(description="Set contact information others can see")
        @app_commands.describe(contact="DM tag, social handle, or preferred contact method")
        async def contact(interaction: discord.Interaction, contact: str):
            await db.set_contact(interaction.user.id, contact)
            embed = info_embed("📇 Contact saved", f"Contact preference updated to: {contact}")
            await interaction.response.send_message(embed=embed, ephemeral=True)

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

            contact, score, count = await db.profile(target.id)
            stock = await db.get_stock(target.id)
            wishlist = await db.get_wishlist(target.id)
            embed = info_embed(
                f"🧾 Profile for {target.display_name}",
                description=rating_summary(score, count),
            )
            if contact:
                embed.add_field(name="Contact", value=contact, inline=False)
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

    @app_commands.command(name="rate", description="Rate a completed trade partner")
    @app_commands.describe(user="User to rate", score="Score between 1 and 5")
    async def rate(self, interaction: discord.Interaction, user: discord.Member, score: int):
        if score < 1 or score > 5:
            await interaction.response.send_message(
                embed=info_embed("⚠️ Invalid rating", "Score must be between 1 and 5."),
                ephemeral=True,
            )
            return
        await self.db.record_rating(user.id, score)
        embed = info_embed("⭐ Rating recorded", f"Thank you! {user.mention} received a {score}-star rating.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="complete", description="Mark a trade as completed")
    @app_commands.describe(trade_id="Trade identifier", partner="Partner involved", item="Item traded")
    async def complete(
        self, interaction: discord.Interaction, trade_id: int, partner: discord.Member, item: str
    ):
        await self.db.set_trade_status(trade_id, "completed", create_if_missing=(interaction.user.id, partner.id, item))
        embed = info_embed(
            "✅ Trade completed",
            f"Trade #{trade_id} with {partner.mention} for **{item}** marked complete.",
        )
        await interaction.response.send_message(embed=embed)


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


def run_bot() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    bot = TraderBot(settings, Database(settings.database_path))
    bot.run(settings.discord_token)


if __name__ == "__main__":
    run_bot()
