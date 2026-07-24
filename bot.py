from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("voice-level-bot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0") or 0)
LEVEL_UP_CHANNEL_ID = int(os.getenv("LEVEL_UP_CHANNEL_ID", "0") or 0)

BASE_HOURS = float(os.getenv("BASE_HOURS", "1.0"))
HOURS_PER_LEVEL = float(os.getenv("HOURS_PER_LEVEL", "0.25"))
TICK_SECONDS = max(15, int(os.getenv("TICK_SECONDS", "60")))
MIN_HUMANS_IN_VC = max(1, int(os.getenv("MIN_HUMANS_IN_VC", "2")))
REQUIRE_UNMUTED = os.getenv("REQUIRE_UNMUTED", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing.")
if BASE_HOURS <= 0:
    raise RuntimeError("BASE_HOURS must be greater than 0.")
if HOURS_PER_LEVEL < 0:
    raise RuntimeError("HOURS_PER_LEVEL cannot be negative.")

SUPERSCRIPT_TABLE = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
SUPERSCRIPT_SUFFIX_RE = re.compile(r"\s*[⁰¹²³⁴⁵⁶⁷⁸⁹]+$")
NICKNAME_MAX_LENGTH = 32

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS voice_levels (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    total_voice_seconds BIGINT NOT NULL DEFAULT 0,
    level INTEGER NOT NULL DEFAULT 0,
    nickname_level INTEGER NOT NULL DEFAULT -1,
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);

CREATE INDEX IF NOT EXISTS voice_levels_guild_total_idx
ON voice_levels (guild_id, total_voice_seconds DESC);
"""

UPSERT_ACTIVE_SQL = """
WITH incoming AS (
    SELECT *
    FROM UNNEST($1::BIGINT[], $2::BIGINT[])
         AS t(guild_id, user_id)
)
INSERT INTO voice_levels (
    guild_id,
    user_id,
    total_voice_seconds,
    level,
    nickname_level,
    last_active_at
)
SELECT guild_id, user_id, $3, 0, -1, NOW()
FROM incoming
ON CONFLICT (guild_id, user_id)
DO UPDATE SET
    total_voice_seconds = voice_levels.total_voice_seconds + EXCLUDED.total_voice_seconds,
    last_active_at = NOW()
RETURNING guild_id, user_id, total_voice_seconds, level, nickname_level;
"""


def normalize_neon_dsn(dsn: str) -> str:
    """Remove options asyncpg may not understand while preserving sslmode."""
    parts = urlsplit(dsn)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "channel_binding"]
    if not any(key == "sslmode" for key, _ in query):
        query.append(("sslmode", "require"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def seconds_required_for_next_level(level: int) -> int:
    hours = BASE_HOURS + (HOURS_PER_LEVEL * level)
    return max(1, round(hours * 3600))


def cumulative_seconds_for_level(level: int) -> int:
    """Total qualifying voice time required to reach `level`."""
    if level <= 0:
        return 0
    base_seconds = BASE_HOURS * 3600
    step_seconds = HOURS_PER_LEVEL * 3600
    total = (base_seconds * level) + (step_seconds * level * (level - 1) / 2)
    return max(0, round(total))


def level_from_total_seconds(total_seconds: int) -> int:
    """Solve the linear level curve and correct for rounding at boundaries."""
    total_seconds = max(0, total_seconds)
    base_seconds = BASE_HOURS * 3600
    step_seconds = HOURS_PER_LEVEL * 3600

    if step_seconds == 0:
        level = int(total_seconds // base_seconds)
    else:
        # C(L) = (step/2)L^2 + (base-step/2)L
        b = base_seconds - (step_seconds / 2)
        level = int(math.floor((-b + math.sqrt((b * b) + (2 * step_seconds * total_seconds))) / step_seconds))

    while cumulative_seconds_for_level(level + 1) <= total_seconds:
        level += 1
    while level > 0 and cumulative_seconds_for_level(level) > total_seconds:
        level -= 1
    return level


def progress_for_total(total_seconds: int) -> tuple[int, int, int]:
    level = level_from_total_seconds(total_seconds)
    level_start = cumulative_seconds_for_level(level)
    progress = max(0, total_seconds - level_start)
    required = seconds_required_for_next_level(level)
    return level, progress, required


def superscript_number(number: int) -> str:
    return str(max(0, number)).translate(SUPERSCRIPT_TABLE)


def remove_level_suffix(name: str) -> str:
    cleaned = SUPERSCRIPT_SUFFIX_RE.sub("", name).strip()
    return cleaned or "Member"


def nickname_with_level(member: discord.Member, level: int) -> str:
    current_name = member.nick or member.global_name or member.name
    base_name = remove_level_suffix(current_name)
    suffix = f" {superscript_number(level)}"
    max_base_length = max(1, NICKNAME_MAX_LENGTH - len(suffix))
    return f"{base_name[:max_base_length].rstrip()}{suffix}"


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def progress_bar(progress: int, required: int, width: int = 12) -> str:
    ratio = 0 if required <= 0 else min(1.0, max(0.0, progress / required))
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


def member_is_eligible(member: discord.Member) -> bool:
    if member.bot or member.voice is None or member.voice.channel is None:
        return False
    state = member.voice
    if state.self_deaf or state.deaf:
        return False
    if REQUIRE_UNMUTED and (state.self_mute or state.mute):
        return False
    return True


class VoiceLevelBot(commands.Bot):
    pool: asyncpg.Pool

    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.voice_states = True

        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self._last_tick_monotonic: float | None = None
        self._nickname_edits: set[tuple[int, int]] = set()

    async def setup_hook(self) -> None:
        dsn = normalize_neon_dsn(DATABASE_URL)

        for attempt in range(1, 9):
            try:
                self.pool = await asyncpg.create_pool(
                    dsn=dsn,
                    min_size=1,
                    max_size=5,
                    command_timeout=30,
                    # Neon pooled connections use PgBouncer transaction mode.
                    statement_cache_size=0,
                )
                async with self.pool.acquire() as connection:
                    await connection.execute(SCHEMA_SQL)
                break
            except Exception:
                if attempt == 8:
                    raise
                delay = min(30, 2**attempt)
                log.exception("Database connection failed; retrying in %s seconds", delay)
                await asyncio.sleep(delay)

        if TEST_GUILD_ID:
            guild = discord.Object(id=TEST_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %s commands to test guild %s", len(synced), TEST_GUILD_ID)
        else:
            synced = await self.tree.sync()
            log.info("Synced %s global commands", len(synced))

        self.voice_time_loop.start()

    async def close(self) -> None:
        if self.voice_time_loop.is_running():
            self.voice_time_loop.cancel()
        if hasattr(self, "pool"):
            await self.pool.close()
        await super().close()

    async def get_or_create_stats(self, guild_id: int, user_id: int) -> asyncpg.Record:
        return await self.pool.fetchrow(
            """
            INSERT INTO voice_levels (guild_id, user_id)
            VALUES ($1, $2)
            ON CONFLICT (guild_id, user_id) DO UPDATE
            SET guild_id = EXCLUDED.guild_id
            RETURNING *;
            """,
            guild_id,
            user_id,
        )

    async def apply_level_nickname(self, member: discord.Member, level: int) -> bool:
        guild = member.guild
        me = guild.me
        if me is None or not me.guild_permissions.manage_nicknames:
            return False
        if member == guild.owner or member.top_role >= me.top_role:
            return False

        new_nickname = nickname_with_level(member, level)
        if member.nick == new_nickname:
            return True

        key = (guild.id, member.id)
        self._nickname_edits.add(key)
        try:
            await member.edit(nick=new_nickname, reason=f"Voice level updated to {level}")
            return True
        except discord.Forbidden:
            log.warning("Cannot edit nickname for %s in %s due to permissions/role hierarchy", member.id, guild.id)
            return False
        except discord.HTTPException:
            log.exception("Discord rejected nickname update for %s in %s", member.id, guild.id)
            return False
        finally:
            await asyncio.sleep(0.2)
            self._nickname_edits.discard(key)

    async def announce_level_up(self, member: discord.Member, old_level: int, new_level: int) -> None:
        if LEVEL_UP_CHANNEL_ID == 0 or new_level <= old_level:
            return
        channel = member.guild.get_channel(LEVEL_UP_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(
                f"🎉 {member.mention} reached **Level {new_level}** in voice activity!"
            )
        except discord.HTTPException:
            log.exception("Could not send level-up message in guild %s", member.guild.id)

    async def process_level_changes(self, rows: Iterable[asyncpg.Record]) -> None:
        level_updates: list[tuple[int, int, int]] = []
        nickname_candidates: list[tuple[int, int, int, int, int]] = []

        for row in rows:
            new_level = level_from_total_seconds(row["total_voice_seconds"])
            old_level = row["level"]
            nickname_level = row["nickname_level"]

            if new_level != old_level:
                level_updates.append((new_level, row["guild_id"], row["user_id"]))
            if new_level != nickname_level:
                nickname_candidates.append(
                    (row["guild_id"], row["user_id"], old_level, new_level, nickname_level)
                )

        if level_updates:
            await self.pool.executemany(
                """
                UPDATE voice_levels
                SET level = $1
                WHERE guild_id = $2 AND user_id = $3;
                """,
                level_updates,
            )

        for guild_id, user_id, old_level, new_level, _nickname_level in nickname_candidates:
            guild = self.get_guild(guild_id)
            member = guild.get_member(user_id) if guild else None
            if member is None:
                continue

            success = await self.apply_level_nickname(member, new_level)
            if success:
                await self.pool.execute(
                    """
                    UPDATE voice_levels
                    SET nickname_level = $1
                    WHERE guild_id = $2 AND user_id = $3;
                    """,
                    new_level,
                    guild_id,
                    user_id,
                )
            await self.announce_level_up(member, old_level, new_level)

    @tasks.loop(seconds=TICK_SECONDS)
    async def voice_time_loop(self) -> None:
        now = time.monotonic()
        if self._last_tick_monotonic is None:
            self._last_tick_monotonic = now
            return

        elapsed = max(1, min(round(now - self._last_tick_monotonic), TICK_SECONDS * 3))
        self._last_tick_monotonic = now

        active_pairs: list[tuple[int, int]] = []

        for guild in self.guilds:
            channels: list[discord.abc.Connectable] = [*guild.voice_channels, *guild.stage_channels]
            for channel in channels:
                if guild.afk_channel and channel.id == guild.afk_channel.id:
                    continue

                eligible_members = [member for member in channel.members if member_is_eligible(member)]
                if len(eligible_members) < MIN_HUMANS_IN_VC:
                    continue

                active_pairs.extend((guild.id, member.id) for member in eligible_members)

        if not active_pairs:
            return

        # Remove duplicates without changing order.
        active_pairs = list(dict.fromkeys(active_pairs))
        guild_ids = [pair[0] for pair in active_pairs]
        user_ids = [pair[1] for pair in active_pairs]

        try:
            rows = await self.pool.fetch(UPSERT_ACTIVE_SQL, guild_ids, user_ids, elapsed)
            await self.process_level_changes(rows)
        except Exception:
            log.exception("Voice-time update failed")

    @voice_time_loop.before_loop
    async def before_voice_time_loop(self) -> None:
        await self.wait_until_ready()


bot = VoiceLevelBot()


@bot.event
async def on_ready() -> None:
    assert bot.user is not None
    log.info("Logged in as %s (%s), connected to %s guild(s)", bot.user, bot.user.id, len(bot.guilds))


@bot.event
async def on_member_join(member: discord.Member) -> None:
    if member.bot:
        return
    row = await bot.get_or_create_stats(member.guild.id, member.id)
    level = level_from_total_seconds(row["total_voice_seconds"])
    if await bot.apply_level_nickname(member, level):
        await bot.pool.execute(
            """
            UPDATE voice_levels SET level = $1, nickname_level = $1
            WHERE guild_id = $2 AND user_id = $3;
            """,
            level,
            member.guild.id,
            member.id,
        )


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    if before.nick == after.nick or after.bot:
        return
    if (after.guild.id, after.id) in bot._nickname_edits:
        return

    row = await bot.pool.fetchrow(
        "SELECT level FROM voice_levels WHERE guild_id = $1 AND user_id = $2;",
        after.guild.id,
        after.id,
    )
    if row is None:
        return

    expected = nickname_with_level(after, row["level"])
    if after.nick != expected:
        if await bot.apply_level_nickname(after, row["level"]):
            await bot.pool.execute(
                """
                UPDATE voice_levels SET nickname_level = $1
                WHERE guild_id = $2 AND user_id = $3;
                """,
                row["level"],
                after.guild.id,
                after.id,
            )


@bot.tree.command(name="rank", description="Show voice level and progress.")
@app_commands.guild_only()
@app_commands.describe(member="The member to view; leave blank for yourself")
async def rank(interaction: discord.Interaction, member: discord.Member | None = None) -> None:
    assert interaction.guild is not None
    target = member or interaction.user
    if not isinstance(target, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    row = await bot.get_or_create_stats(interaction.guild.id, target.id)
    total = row["total_voice_seconds"]
    level, progress, required = progress_for_total(total)
    remaining = max(0, required - progress)

    embed = discord.Embed(title=f"{target.display_name}'s Voice Rank", color=discord.Color.blurple())
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Level", value=f"**{level}** {superscript_number(level)}", inline=True)
    embed.add_field(name="Total voice time", value=format_duration(total), inline=True)
    embed.add_field(
        name="Progress",
        value=(
            f"`{progress_bar(progress, required)}`\n"
            f"{format_duration(progress)} / {format_duration(required)}\n"
            f"**{format_duration(remaining)} remaining**"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="leaderboard", description="Show the top voice-level members.")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    rows = await bot.pool.fetch(
        """
        SELECT user_id, total_voice_seconds
        FROM voice_levels
        WHERE guild_id = $1
        ORDER BY total_voice_seconds DESC
        LIMIT 10;
        """,
        interaction.guild.id,
    )

    if not rows:
        await interaction.response.send_message(
            "No voice activity has been recorded yet.",
            ephemeral=True,
        )
        return

    medals = ["🥇", "🥈", "🥉"]
    lines: list[str] = []
    for index, row in enumerate(rows, start=1):
        member = interaction.guild.get_member(row["user_id"])
        name = member.mention if member else f"User `{row['user_id']}`"
        level = level_from_total_seconds(row["total_voice_seconds"])
        prefix = medals[index - 1] if index <= 3 else f"**{index}.**"
        lines.append(
            f"{prefix} {name} — Level **{level}** · {format_duration(row['total_voice_seconds'])}"
        )

    embed = discord.Embed(
        title="Voice Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="syncnickname", description="Reapply your current superscript level nickname.")
@app_commands.guild_only()
async def sync_nickname(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    row = await bot.get_or_create_stats(interaction.guild.id, interaction.user.id)
    level = level_from_total_seconds(row["total_voice_seconds"])
    success = await bot.apply_level_nickname(interaction.user, level)
    if success:
        await bot.pool.execute(
            """
            UPDATE voice_levels SET level = $1, nickname_level = $1
            WHERE guild_id = $2 AND user_id = $3;
            """,
            level,
            interaction.guild.id,
            interaction.user.id,
        )
        await interaction.response.send_message("Your level nickname was updated.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "I could not edit your nickname. Put my bot role above your highest role and give it Manage Nicknames.",
            ephemeral=True,
        )


@bot.tree.command(name="syncallnicknames", description="Reapply level nicknames for tracked members.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def sync_all_nicknames(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    await interaction.response.defer(ephemeral=True, thinking=True)

    rows = await bot.pool.fetch(
        "SELECT user_id, total_voice_seconds FROM voice_levels WHERE guild_id = $1;",
        interaction.guild.id,
    )
    updated = 0
    skipped = 0

    for row in rows:
        member = interaction.guild.get_member(row["user_id"])
        if member is None:
            skipped += 1
            continue
        level = level_from_total_seconds(row["total_voice_seconds"])
        if await bot.apply_level_nickname(member, level):
            updated += 1
            await bot.pool.execute(
                """
                UPDATE voice_levels SET level = $1, nickname_level = $1
                WHERE guild_id = $2 AND user_id = $3;
                """,
                level,
                interaction.guild.id,
                member.id,
            )
        else:
            skipped += 1

    await interaction.followup.send(
        f"Updated **{updated}** nickname(s). Skipped **{skipped}** member(s).",
        ephemeral=True,
    )


@sync_all_nicknames.error
async def sync_all_nicknames_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the Manage Server permission to use this command."
    else:
        log.exception("syncallnicknames command failed", exc_info=error)
        message = "That command failed. Check the bot logs for details."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
