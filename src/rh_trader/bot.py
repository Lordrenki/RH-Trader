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
SCAM_COMMAND_ROLE_ID = 1367584510656385045


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
        class ScamReportModal(discord.ui.Modal, title="Add Scam Report"):
            embark_id = discord.ui.TextInput(
                label="Embark ID",
                placeholder="User#1234",
                required=True,
                max_length=64,
            )

            def __init__(
                self,
                bot: TraderBot,
                reported_user: discord.Member,
                requested_by: discord.abc.User,
            ) -> None:
                super().__init__()
                self.bot = bot
                self.reported_user = reported_user
                self.requested_by = requested_by

            async def on_submit(self, interaction: discord.Interaction) -> None:
                embark_id_value = str(self.embark_id.value).strip()
                if "#" not in embark_id_value or len(embark_id_value.split("#", 1)[0]) == 0:
                    await interaction.response.send_message(
                        "Please provide a valid Embark ID in the format `User#1234`.",
                        ephemeral=True,
                    )
                    return

                inserted, normalized = await self.bot.db.add_scam_report(
                    discord_user_id=self.reported_user.id,
                    embark_id=embark_id_value,
                    added_by_discord_user_id=self.requested_by.id,
                )
                if inserted:
                    await interaction.response.send_message(
                        (
                            f"🚨 Added **{self.reported_user.mention}** to the scam database "
                            f"with Embark ID `{embark_id_value}`."
                        ),
                        ephemeral=True,
                    )
                    return

                await interaction.response.send_message(
                    f"That Embark ID is already in the scam database as `{normalized}`.",
                    ephemeral=True,
                )

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

        @self.tree.command(name="scam", description="Add a user to the scam database")
        @app_commands.describe(user="Discord user to flag")
        async def scam(interaction: discord.Interaction, user: discord.Member) -> None:
            if interaction.guild is None or not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message(
                    "This command can only be used in a server.",
                    ephemeral=True,
                )
                return

            if not any(role.id == SCAM_COMMAND_ROLE_ID for role in interaction.user.roles):
                await interaction.response.send_message(
                    "You don't have permission to use this command.",
                    ephemeral=True,
                )
                return

            if user.bot:
                await interaction.response.send_message("You can't flag a bot account.", ephemeral=True)
                return
            await interaction.response.send_modal(
                ScamReportModal(bot=self, reported_user=user, requested_by=interaction.user)
            )

        @self.tree.command(name="check", description="Check an Embark ID against scam history")
        @app_commands.describe(embark_id="Embark ID to check, e.g. RaiderPro#4821")
        async def check(interaction: discord.Interaction, embark_id: str) -> None:
            report = await self.db.get_scam_report_by_embark_id(embark_id)
            if report is None:
                await interaction.response.send_message(
                    f"✅ No scam record found for `{embark_id.strip()}`.",
                    ephemeral=True,
                )
                return

            discord_user_id, stored_embark_id, added_by_id, _ = report
            await interaction.response.send_message(
                (
                    f"⚠️ **Fraud history found** for `{stored_embark_id}`.\n"
                    f"Linked Discord user: <@{discord_user_id}>\n"
                    f"Reported by: <@{added_by_id}>"
                ),
                ephemeral=True,
            )


def run_bot() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    bot = TraderBot(settings, Database(settings.database_path))
    bot.run(settings.discord_token)


if __name__ == "__main__":
    run_bot()
