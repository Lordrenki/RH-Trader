"""Discord bot with only thread creation and reputation features."""
from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from .config import Settings, load_settings
from .database import Database, REP_CATEGORIES

_log = logging.getLogger(__name__)
REP_COOLDOWN_SECONDS = 30 * 60


def _format_duration(seconds: int) -> str:
    delta = timedelta(seconds=max(0, seconds))
    minutes, secs = divmod(int(delta.total_seconds()), 60)
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class TraderBot(commands.Bot):
    def __init__(self, settings: Settings, db: Database) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.db = db

    async def setup_hook(self) -> None:
        await self.db.setup()
        await self.add_core_commands()
        synced = await self.tree.sync()
        _log.info("Synced %s app command(s)", len(synced))

    async def add_core_commands(self) -> None:
        @self.tree.command(name="trade", description="Create a trade thread with another member")
        async def trade(interaction: discord.Interaction, user: discord.Member) -> None:
            if interaction.guild is None:
                await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
                return

            if user.bot:
                await interaction.response.send_message("You can't start a trade thread with a bot.", ephemeral=True)
                return

            if user.id == interaction.user.id:
                await interaction.response.send_message("You can't start a trade thread with yourself.", ephemeral=True)
                return

            channel = interaction.channel
            if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
                await interaction.response.send_message(
                    "Use `/trade @user` inside a server text channel.",
                    ephemeral=True,
                )
                return

            thread_name = f"trade-{interaction.user.display_name}-and-{user.display_name}"[:100]
            thread = await channel.create_thread(name=thread_name)
            if isinstance(thread, discord.Thread):
                import contextlib
                with contextlib.suppress(discord.HTTPException):
                    await thread.add_user(user)
                await thread.send(
                    f"{interaction.user.mention} {user.mention} Trade thread created. "
                    "Use `/rep` when you want to leave positive reputation."
                )

            await interaction.response.send_message(f"Thread created: {thread.mention}", ephemeral=True)

        rep_category_choices = [
            app_commands.Choice(name=name.capitalize(), value=name) for name in REP_CATEGORIES
        ]

        @self.tree.command(name="rep", description="Give positive reputation in one category")
        @app_commands.describe(user="Member receiving reputation", category="Rep category")
        @app_commands.choices(category=rep_category_choices)
        async def rep(
            interaction: discord.Interaction,
            user: discord.Member,
            category: app_commands.Choice[str],
        ) -> None:
            if user.id == interaction.user.id:
                await interaction.response.send_message("You can't rep yourself.", ephemeral=True)
                return
            if user.bot:
                await interaction.response.send_message("You can't rep a bot account.", ephemeral=True)
                return

            remaining = await self.db.get_pair_cooldown_remaining(
                interaction.user.id,
                user.id,
                REP_COOLDOWN_SECONDS,
            )
            if remaining > 0:
                await interaction.response.send_message(
                    (
                        f"You recently repped {user.mention}. "
                        f"Try again in `{_format_duration(remaining)}`."
                    ),
                    ephemeral=True,
                )
                return

            await self.db.add_reputation(interaction.user.id, user.id, category.value)
            await interaction.response.send_message(
                f"✅ Added +1 **{category.name}** reputation to {user.mention}."
            )

        @self.tree.command(name="profile", description="View rep profile")
        async def profile(interaction: discord.Interaction, user: discord.Member | None = None) -> None:
            target = user or interaction.user
            p = await self.db.get_profile(target.id)
            embed = discord.Embed(
                title=f"{target.display_name}'s Reputation",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Trading", value=str(p.trading), inline=True)
            embed.add_field(name="Knowledge", value=str(p.knowledge), inline=True)
            embed.add_field(name="Skill", value=str(p.skill), inline=True)
            embed.add_field(name="Overall", value=str(p.total), inline=False)
            await interaction.response.send_message(embed=embed)


def run_bot() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    bot = TraderBot(settings, Database(settings.database_path))
    bot.run(settings.discord_token)


if __name__ == "__main__":
    run_bot()
