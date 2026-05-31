"""Discord bot with only thread creation and reputation features."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

import aiohttp

import discord
from discord.ext import commands, tasks

from .config import Settings, load_settings
from .blueprint_cache import load_blueprint_values, save_blueprint_values
from .database import Database
from .raider_market import fetch_browse_items, format_trade_value_lines

_log = logging.getLogger(__name__)
REP_COOLDOWN_SECONDS = 3 * 60 * 60
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


_REP_COMMAND_RE = re.compile(r"^\+rep[ \t]+<@!?(\d+)>(?=\s|$)")
_REP_CHECK_RE = re.compile(r"^rep[ \t]+<@!?(\d+)>(?=\s|$)")


def _extract_explicit_rep_target(content: str) -> tuple[str, int] | None:
    """Parse the legacy text reputation commands from a message body."""
    add_match = _REP_COMMAND_RE.match(content.strip())
    if add_match:
        return "+", int(add_match.group(1))

    check_match = _REP_CHECK_RE.match(content.strip())
    if check_match:
        return "check", int(check_match.group(1))

    return None


class TraderBot(commands.Bot):
    def __init__(self, settings: Settings, db: Database) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.db = db
        self._blueprint_message_ids: list[int] = []
        self._blueprint_loop_started = False

    async def setup_hook(self) -> None:
        await self.db.setup()
        self.tree.clear_commands(guild=None)
        synced = await self.tree.sync()
        _log.info("Cleared slash commands; %s app command(s) remain synced", len(synced))

    async def on_ready(self) -> None:
        if not self._blueprint_loop_started:
            self.blueprint_price_loop.start()
            self._blueprint_loop_started = True
        _log.info("Logged in as %s", self.user)

    @staticmethod
    def _eligible_rep_role_ids(total_rep: int) -> list[int]:
        return [role_id for threshold, role_id in TRADE_REP_ROLE_THRESHOLDS if total_rep >= threshold]

    async def _sync_rep_roles_for_member(
        self,
        member: discord.Member,
        total_rep: int,
        trials_rep: int,
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
            await member.add_roles(*roles_to_add, reason=f"Reached {total_rep} trading rep")
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
                    f"for reaching **{total_rep}** trading rep!"
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

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        parsed = _extract_explicit_rep_target(message.content)
        if parsed is None:
            return

        action, target_id = parsed
        target = next((member for member in message.mentions if member.id == target_id), None)
        if target is None:
            await message.reply("Please mention a server member directly.", mention_author=False)
            return

        if action == "check":
            await self._reply_with_rep_profile(message, target)
            return

        if not isinstance(message.author, discord.Member):
            await message.reply("Reputation can only be given inside a server.", mention_author=False)
            return

        await self._award_trading_rep_from_message(message, message.author, target)

    async def _award_trading_rep_from_message(
        self,
        message: discord.Message,
        rater: discord.Member,
        target: discord.Member,
    ) -> None:
        if target.id == rater.id:
            await message.reply("You can't rep yourself.", mention_author=False)
            return
        if target.bot:
            await message.reply("You can't rep a bot account.", mention_author=False)
            return

        remaining = await self.db.get_pair_cooldown_remaining(
            rater.id,
            target.id,
            REP_COOLDOWN_SECONDS,
        )
        if remaining > 0:
            await message.reply(
                (
                    f"You recently repped {target.mention}. "
                    f"Try again in `{_format_duration(remaining)}`."
                ),
                mention_author=False,
            )
            return

        await self.db.add_reputation(rater.id, target.id, "trading")
        profile = await self.db.get_profile(target.id)
        await message.reply(
            f"✅ Added +1 trading rep to {target.mention}. They now have **{profile.total}** trading rep.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False, replied_user=False),
        )

        roles_added = await self._sync_rep_roles_for_member(target, profile.total, 0)
        if roles_added:
            await self._send_rep_role_award_message(
                message.channel,
                target,
                roles_added,
                profile.total,
            )

    async def _reply_with_rep_profile(self, message: discord.Message, target: discord.Member) -> None:
        profile = await self.db.get_profile(target.id)
        embed = discord.Embed(
            title=f"🏆 {target.display_name}'s Trading Rep",
            description=f"{target.mention} has **{profile.total}** trading rep.",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Trading", value=str(profile.trading), inline=True)
        await message.reply(
            embed=embed,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False, replied_user=False),
        )

    async def post_blueprint_prices(self) -> int:
        if self.settings.blueprint_channel_id is None:
            return 0
        channel = self.get_channel(self.settings.blueprint_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return 0

        try:
            async with aiohttp.ClientSession() as session:
                items = await fetch_browse_items(session)
            blueprint_items = [
                item for item in items.values() if "blueprint" in item.name.lower()
            ]
        except Exception:
            _log.exception("Failed to fetch live blueprint values; falling back to cache")
            blueprint_items = []

        priced_blueprint_items = [
            item
            for item in blueprint_items
            if isinstance(item.trade_value, int) and item.trade_value > 0
        ]
        if priced_blueprint_items:
            save_blueprint_values(priced_blueprint_items)
            blueprint_items = priced_blueprint_items
        else:
            blueprint_items = load_blueprint_values()
        lines = format_trade_value_lines(blueprint_items, include_game_value=False)

        embed = discord.Embed(
            title="🛠️ ARC Raiders Blueprint Trade Values",
            description="Updated every 24 hours",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow(),
        )
        chunks: list[str] = []
        page = ""
        for line in lines:
            candidate = f"{page}\n{line}".strip()
            if len(candidate) > 900 and page:
                chunks.append(page)
                page = line
            else:
                page = candidate
        if page:
            chunks.append(page)
        if not chunks:
            embed.add_field(name="No data", value="Could not parse blueprint trade values.", inline=False)
            chunks = [""]

        sent = 0
        for idx, chunk in enumerate(chunks, start=1):
            local = embed.copy()
            local.add_field(name=f"Blueprints (Page {idx}/{len(chunks)})", value=chunk or "No rows", inline=False)
            if idx <= len(self._blueprint_message_ids):
                try:
                    msg = await channel.fetch_message(self._blueprint_message_ids[idx - 1])
                    await msg.edit(embed=local)
                    sent += 1
                    continue
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            msg = await channel.send(embed=local)
            self._blueprint_message_ids.append(msg.id)
            sent += 1
        return sent

    @tasks.loop(hours=24)
    async def blueprint_price_loop(self) -> None:
        try:
            await self.wait_until_ready()
            await self.post_blueprint_prices()
        except Exception:
            _log.exception("Failed to publish blueprint prices")


def run_bot() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    bot = TraderBot(settings, Database(settings.database_path))
    bot.run(settings.discord_token)


if __name__ == "__main__":
    run_bot()
