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
TRADE_REP_ROLE_THRESHOLDS = (
    (10, 1495238466731249665),
    (25, 1495238526621716681),
    (50, 1495238582762344518),
    (100, 1495238632825683968),
)
NEW_ACCOUNT_ROLE_ID = 1497024245211988028
NEW_ACCOUNT_AGE_DAYS = 30


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

    @staticmethod
    def _eligible_rep_role_ids(total_rep: int) -> list[int]:
        return [role_id for threshold, role_id in TRADE_REP_ROLE_THRESHOLDS if total_rep >= threshold]

    async def _sync_rep_roles_for_member(
        self,
        member: discord.Member,
        total_rep: int,
    ) -> list[discord.Role]:
        role_ids = self._eligible_rep_role_ids(total_rep)
        if not role_ids:
            return []

        existing_ids = {role.id for role in member.roles}
        roles_to_add = [
            role
            for role_id in role_ids
            if role_id not in existing_ids and (role := member.guild.get_role(role_id)) is not None
        ]
        if not roles_to_add:
            return []

        try:
            await member.add_roles(*roles_to_add, reason=f"Reached {total_rep} total positive trade rep")
        except (discord.Forbidden, discord.HTTPException):
            _log.exception("Failed to add rep roles to member %s", member.id)
            return []
        return roles_to_add

    async def _send_rep_role_award_message(
        self,
        channel: discord.abc.Messageable,
        member: discord.Member,
        roles_added: list[discord.Role],
        total_rep: int,
    ) -> None:
        role_names = ", ".join(f"`{discord.utils.escape_mentions(role.name)}`" for role in roles_added)
        try:
            await channel.send(
                (
                    f"🎉 {member.mention} unlocked {role_names} "
                    f"for reaching **{total_rep}** positive trade rep!"
                ),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except (discord.Forbidden, discord.HTTPException):
            _log.exception("Failed to send rep role award message for member %s", member.id)

    async def _grant_new_account_role_if_needed(self, member: discord.Member) -> bool:
        account_age = discord.utils.utcnow() - member.created_at
        if account_age.days >= NEW_ACCOUNT_AGE_DAYS:
            return False

        role = member.guild.get_role(NEW_ACCOUNT_ROLE_ID)
        if role is None or role in member.roles:
            return False

        try:
            await member.add_roles(
                role,
                reason=f"Account age under {NEW_ACCOUNT_AGE_DAYS} days at join",
            )
        except (discord.Forbidden, discord.HTTPException):
            _log.exception("Failed to add new-account role to member %s", member.id)
            return False
        return True

    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        await self._grant_new_account_role_if_needed(member)

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
            profile = await self.db.get_profile(user.id)
            roles_added = await self._sync_rep_roles_for_member(user, profile.total)
            if roles_added and interaction.channel is not None:
                await self._send_rep_role_award_message(interaction.channel, user, roles_added, profile.total)

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

        @self.tree.command(
            name="sync_rep_roles",
            description="One-time backfill: grant rep milestone roles to existing members",
        )
        async def sync_rep_roles(interaction: discord.Interaction) -> None:
            if interaction.guild is None or not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message(
                    "This command can only be used in a server.",
                    ephemeral=True,
                )
                return

            if not interaction.user.guild_permissions.manage_roles:
                await interaction.response.send_message(
                    "You need the **Manage Roles** permission to run this command.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            scanned = 0
            awarded = 0
            async for member in interaction.guild.fetch_members(limit=None):
                if member.bot:
                    continue
                scanned += 1
                profile = await self.db.get_profile(member.id)
                roles_added = await self._sync_rep_roles_for_member(member, profile.total)
                awarded += len(roles_added)

            await interaction.followup.send(
                f"Done. Scanned **{scanned}** members and awarded **{awarded}** role(s).",
                ephemeral=True,
            )

        @self.tree.command(
            name="checkage",
            description="Remove the new-account role if your account is 30+ days old",
        )
        async def checkage(interaction: discord.Interaction) -> None:
            if interaction.guild is None or not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message(
                    "This command can only be used in a server.",
                    ephemeral=True,
                )
                return

            role = interaction.guild.get_role(NEW_ACCOUNT_ROLE_ID)
            if role is None:
                await interaction.response.send_message(
                    "The new-account role is not configured in this server.",
                    ephemeral=True,
                )
                return

            member = interaction.user
            account_age_days = (discord.utils.utcnow() - member.created_at).days
            if account_age_days < NEW_ACCOUNT_AGE_DAYS:
                days_left = NEW_ACCOUNT_AGE_DAYS - account_age_days
                await interaction.response.send_message(
                    f"Your account is still too new. Try again in **{days_left}** day(s).",
                    ephemeral=True,
                )
                return

            if role not in member.roles:
                await interaction.response.send_message(
                    "You don't currently have the new-account role.",
                    ephemeral=True,
                )
                return

            try:
                await member.remove_roles(role, reason="Account age verified via /checkage")
            except (discord.Forbidden, discord.HTTPException):
                await interaction.response.send_message(
                    "I couldn't remove the role due to missing permissions.",
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                "✅ Your account is old enough now, so the new-account role has been removed.",
                ephemeral=True,
            )


def run_bot() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    bot = TraderBot(settings, Database(settings.database_path))
    bot.run(settings.discord_token)


if __name__ == "__main__":
    run_bot()
