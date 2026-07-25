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

if not hasattr(discord.ui, "LayoutView"):
    raise RuntimeError(
        "The inline Nickname Panel requires discord.py 2.6 or newer. "
        "Update your dependency to discord.py>=2.6."
    )

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("voice-level-bot")


def env_flag(name: str, default: bool = False) -> bool:
    fallback = "true" if default else "false"
    return os.getenv(name, fallback).strip().lower() in {"1", "true", "yes", "on"}


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0") or 0)
LEVEL_UP_CHANNEL_ID = int(os.getenv("LEVEL_UP_CHANNEL_ID", "0") or 0)

BASE_HOURS = float(os.getenv("BASE_HOURS", "1.0"))
HOURS_PER_LEVEL = float(os.getenv("HOURS_PER_LEVEL", "0.25"))
TICK_SECONDS = max(15, int(os.getenv("TICK_SECONDS", "60")))
# Use 1 to count a member who is alone in voice. Set this to 2 to require company.
MIN_HUMANS_IN_VC = max(1, int(os.getenv("MIN_HUMANS_IN_VC", "1")))
REQUIRE_UNMUTED = env_flag("REQUIRE_UNMUTED", default=False)
SYNC_NICKNAMES_ON_START = env_flag("SYNC_NICKNAMES_ON_START", default=True)

# Default Unicode badge entries. These seed each server's database.
# Custom Discord emoji codes are intentionally not supported in nicknames.
DEFAULT_EMOJI_UNLOCKS = (
    # Levels 0, 1, and 2 unlock immediately, then badges unlock every 5 levels.
    "0=⚪,1=🟣,2=🔴,5=🟢,10=🟤,15=⚫,20=🔵,25=🟡,30=🟠,"
    "35=⭐,40=💎,45=⚡,50=🌙,55=🪐,60=👑,65=🌟,70=🏆,"
    "75=🐉,80=💠,85=🦅,90=☄️,95=🔱,100=🌌"
)


def parse_emoji_unlocks(raw: str) -> tuple[tuple[int, str], ...]:
    unlocks: list[tuple[int, str]] = []
    seen_emojis: set[str] = set()

    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise RuntimeError(
                "EMOJI_UNLOCKS entries must use level=emoji, such as 5=⭐."
            )

        level_text, emoji = entry.split("=", 1)
        try:
            required_level = int(level_text.strip())
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid emoji unlock level: {level_text!r}."
            ) from exc

        emoji = emoji.strip()
        if required_level < 0:
            raise RuntimeError("Emoji unlock levels cannot be negative.")
        if not emoji:
            raise RuntimeError("Every emoji unlock entry needs an emoji.")
        if len(emoji) > 8:
            raise RuntimeError(
                f"Emoji badge {emoji!r} is too long for a nickname prefix."
            )
        if emoji in seen_emojis:
            raise RuntimeError(f"Emoji badge {emoji!r} is listed more than once.")

        seen_emojis.add(emoji)
        unlocks.append((required_level, emoji))

    if not unlocks:
        raise RuntimeError("EMOJI_UNLOCKS must contain at least one level=emoji entry.")

    return tuple(sorted(unlocks, key=lambda item: (item[0], item[1])))


DEFAULT_PARSED_EMOJI_UNLOCKS = parse_emoji_unlocks(DEFAULT_EMOJI_UNLOCKS)


def parse_bulk_emoji_unlocks(raw: str) -> tuple[tuple[int, str], ...]:
    """Parse a copy/paste block containing one level=emoji entry per line.

    Commas and semicolons are also accepted, so both of these work:

    0=⚪
    5=⭐

    0=⚪, 5=⭐
    """
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[;\n]+", ",", normalized)

    try:
        unlocks = parse_emoji_unlocks(normalized)
    except RuntimeError as exc:
        message = str(exc).replace("EMOJI_UNLOCKS", "Emoji list")
        raise ValueError(message) from exc

    if len(unlocks) > 100:
        raise ValueError("The emoji list can contain at most 100 entries.")

    return unlocks

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
# Remove either │Name⁰ or Badge│Name⁰ when rebuilding a managed nickname.
MANAGED_PREFIX_RE = re.compile(r"^\s*[^│]{0,16}│\s*")
NICKNAME_PREFIX = "│"
BOT_NICKNAME_PREFIX = "🤖│"
NICKNAME_MAX_LENGTH = 32
# Empty selection means "automatically use the highest unlocked badge".
# This private sentinel is used only when a member deliberately removes a badge.
NO_EMOJI_SELECTION = "__none__"

# (required_level, display_emoji, nickname_badge)
EmojiUnlock = tuple[int, str, str]


def format_emoji_unlock_block(unlocks: Iterable[EmojiUnlock]) -> str:
    """Return a paste-ready level=emoji block for the bulk editor."""
    return "\n".join(
        f"{required_level}={emoji}"
        for required_level, emoji, _nickname_badge in unlocks
    )

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS voice_levels (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    total_voice_seconds BIGINT NOT NULL DEFAULT 0,
    level INTEGER NOT NULL DEFAULT 0,
    nickname_level INTEGER NOT NULL DEFAULT -1,
    selected_emoji TEXT NOT NULL DEFAULT '',
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);

ALTER TABLE voice_levels
ADD COLUMN IF NOT EXISTS selected_emoji TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS voice_levels_guild_total_idx
ON voice_levels (guild_id, total_voice_seconds DESC);

CREATE TABLE IF NOT EXISTS emoji_unlocks (
    guild_id BIGINT NOT NULL,
    emoji TEXT NOT NULL,
    nickname_badge TEXT NOT NULL DEFAULT '',
    required_level INTEGER NOT NULL CHECK (required_level >= 0),
    PRIMARY KEY (guild_id, emoji)
);

ALTER TABLE emoji_unlocks
ADD COLUMN IF NOT EXISTS nickname_badge TEXT NOT NULL DEFAULT '';

-- Remove old Discord custom-emoji-code rows and selections. Nicknames can
-- only display normal Unicode emoji characters.
UPDATE voice_levels
SET selected_emoji = ''
WHERE selected_emoji ~ '^<a?:[A-Za-z0-9_]{2,32}:[0-9]{15,22}>$';

DELETE FROM emoji_unlocks
WHERE emoji ~ '^<a?:[A-Za-z0-9_]{2,32}:[0-9]{15,22}>$'
   OR nickname_badge ~ '^<a?:[A-Za-z0-9_]{2,32}:[0-9]{15,22}>$';

-- Unicode entries use the same character in menus and nicknames.
UPDATE emoji_unlocks
SET nickname_badge = emoji;

CREATE INDEX IF NOT EXISTS emoji_unlocks_guild_level_idx
ON emoji_unlocks (guild_id, required_level, emoji);

CREATE TABLE IF NOT EXISTS bot_migrations (
    guild_id BIGINT NOT NULL,
    migration_key TEXT NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, migration_key)
);
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
RETURNING guild_id, user_id, total_voice_seconds, level, nickname_level, selected_emoji;
"""

ENSURE_MEMBER_SQL = """
INSERT INTO voice_levels (guild_id, user_id)
VALUES ($1, $2)
ON CONFLICT (guild_id, user_id) DO NOTHING;
"""


def normalize_neon_dsn(dsn: str) -> str:
    """Remove options asyncpg may not understand while preserving SSL."""
    parts = urlsplit(dsn)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query)
        if key != "channel_binding"
    ]
    if not any(key == "sslmode" for key, _ in query):
        query.append(("sslmode", "require"))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def seconds_required_for_next_level(level: int) -> int:
    hours = BASE_HOURS + (HOURS_PER_LEVEL * level)
    return max(1, round(hours * 3600))


def cumulative_seconds_for_level(level: int) -> int:
    """Total qualifying voice time required to reach a level."""
    if level <= 0:
        return 0
    base_seconds = BASE_HOURS * 3600
    step_seconds = HOURS_PER_LEVEL * 3600
    total = (base_seconds * level) + (
        step_seconds * level * (level - 1) / 2
    )
    return max(0, round(total))


def level_from_total_seconds(total_seconds: int) -> int:
    """Solve the linear level curve and correct rounding at boundaries."""
    total_seconds = max(0, int(total_seconds))
    base_seconds = BASE_HOURS * 3600
    step_seconds = HOURS_PER_LEVEL * 3600

    if step_seconds == 0:
        level = int(total_seconds // base_seconds)
    else:
        # C(L) = (step/2)L^2 + (base-step/2)L
        b = base_seconds - (step_seconds / 2)
        level = int(
            math.floor(
                (-b + math.sqrt((b * b) + (2 * step_seconds * total_seconds)))
                / step_seconds
            )
        )

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


def clean_base_name(name: str) -> str:
    """Remove the bot's old emoji/bar prefix and level without duplicating them."""
    cleaned = SUPERSCRIPT_SUFFIX_RE.sub("", name).strip()
    cleaned = MANAGED_PREFIX_RE.sub("", cleaned).strip()
    return cleaned or "Member"


def is_custom_emoji_code(value: str) -> bool:
    """Return True for raw Discord custom emoji markup such as <:name:id>."""
    value = value.strip()
    return bool(re.fullmatch(r"<a?:[A-Za-z0-9_]{2,32}:\d{15,22}>", value))


def validate_display_emoji(emoji: str) -> str:
    """Validate a short Unicode emoji used in menus and nicknames."""
    emoji = emoji.strip()
    if not emoji:
        raise ValueError("The emoji cannot be empty.")
    if NICKNAME_PREFIX in emoji:
        raise ValueError(f"The emoji cannot contain `{NICKNAME_PREFIX}`.")
    if is_custom_emoji_code(emoji) or emoji.startswith(("<:", "<a:")):
        raise ValueError(
            "Discord Unicode emojis are disabled for this bot. "
            "Use a normal Unicode emoji such as `⚪`, `💎`, or `👑`."
        )
    if len(emoji) > 8:
        raise ValueError("The emoji must be 8 characters or fewer.")
    return emoji


def validate_nickname_badge(badge: str) -> str:
    """Validate the Unicode emoji that appears before the nickname bar."""
    return validate_display_emoji(badge)


def emoji_required_level(
    unlocks: tuple[EmojiUnlock, ...],
    emoji: str,
) -> int | None:
    return next(
        (
            required_level
            for required_level, configured, _nickname_badge in unlocks
            if configured == emoji
        ),
        None,
    )


def emoji_nickname_badge(
    unlocks: tuple[EmojiUnlock, ...],
    emoji: str,
) -> str:
    return next(
        (
            nickname_badge
            for _required_level, configured, nickname_badge in unlocks
            if configured == emoji
        ),
        "",
    ).strip()


def unlocked_emojis(
    level: int,
    unlocks: tuple[EmojiUnlock, ...],
) -> tuple[EmojiUnlock, ...]:
    """Return every configured emoji badge unlocked at the supplied level."""
    return tuple(
        (required_level, emoji, nickname_badge)
        for required_level, emoji, nickname_badge in unlocks
        if level >= required_level
    )


def valid_selected_emoji(
    selected_emoji: str | None,
    level: int,
    unlocks: tuple[EmojiUnlock, ...],
) -> str:
    """Resolve the equipped badge, automatically choosing one when unset."""
    emoji = (selected_emoji or "").strip()

    if emoji == NO_EMOJI_SELECTION:
        return ""

    # Existing members normally have an empty database value. Automatically use
    # the highest-level badge they have unlocked so a badge appears immediately.
    if not emoji:
        available = unlocked_emojis(level, unlocks)
        return available[-1][1] if available else ""

    required_level = emoji_required_level(unlocks, emoji)
    if required_level is None or level < required_level:
        return ""
    return emoji


def next_emoji_unlock(
    level: int,
    unlocks: tuple[EmojiUnlock, ...],
) -> EmojiUnlock | None:
    for required_level, emoji, nickname_badge in unlocks:
        if required_level > level:
            return required_level, emoji, nickname_badge
    return None


def nickname_with_level(
    member: discord.Member,
    level: int,
    selected_emoji: str = "",
    unlocks: tuple[EmojiUnlock, ...] = (),
) -> str:
    """Return Badge│ExampleName⁰, or │ExampleName⁰ with no equipped badge."""
    current_name = member.nick or member.global_name or member.name
    base_name = clean_base_name(current_name)
    suffix = superscript_number(level)
    display_emoji = valid_selected_emoji(selected_emoji, level, unlocks)
    nickname_badge = emoji_nickname_badge(unlocks, display_emoji)
    prefix = f"{nickname_badge}{NICKNAME_PREFIX}"
    max_base_length = max(
        1,
        NICKNAME_MAX_LENGTH - len(prefix) - len(suffix),
    )
    trimmed_name = base_name[:max_base_length].rstrip()
    return f"{prefix}{trimmed_name}{suffix}"


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
        self._startup_sync_started = False
        self._emoji_unlock_cache: dict[int, tuple[EmojiUnlock, ...]] = {}

    async def setup_hook(self) -> None:
        dsn = normalize_neon_dsn(DATABASE_URL)

        for attempt in range(1, 9):
            try:
                self.pool = await asyncpg.create_pool(
                    dsn=dsn,
                    min_size=1,
                    max_size=5,
                    command_timeout=30,
                    # Neon pooled URLs use PgBouncer transaction mode.
                    statement_cache_size=0,
                )
                async with self.pool.acquire() as connection:
                    await connection.execute(SCHEMA_SQL)
                break
            except Exception:
                if attempt == 8:
                    raise
                delay = min(30, 2**attempt)
                log.exception(
                    "Database connection failed; retrying in %s seconds",
                    delay,
                )
                await asyncio.sleep(delay)

        # Register persistent buttons used by public emoji control-panel messages.
        self.add_view(EmojiControlPanelView())

        if TEST_GUILD_ID:
            guild = discord.Object(id=TEST_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info(
                "Synced %s commands to test guild %s",
                len(synced),
                TEST_GUILD_ID,
            )
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

    async def get_emoji_unlocks(
        self,
        guild_id: int,
    ) -> tuple[EmojiUnlock, ...]:
        """Load Unicode unlocks and migrate old custom codes once per server."""
        cached = self._emoji_unlock_cache.get(guild_id)
        if cached is not None:
            return cached

        migration_key = "unicode_emoji_cleanup_v1"
        migrated = await self.pool.fetchval(
            "SELECT 1 FROM bot_migrations "
            "WHERE guild_id = $1 AND migration_key = $2;",
            guild_id,
            migration_key,
        )

        if not migrated:
            async with self.pool.acquire() as connection:
                async with connection.transaction():
                    await connection.execute(
                        "UPDATE voice_levels SET selected_emoji = '' "
                        "WHERE guild_id = $1 AND selected_emoji ~ "
                        "'^<a?:[A-Za-z0-9_]{2,32}:[0-9]{15,22}>$';",
                        guild_id,
                    )
                    await connection.execute(
                        "DELETE FROM emoji_unlocks WHERE guild_id = $1 AND ("
                        "emoji ~ '^<a?:[A-Za-z0-9_]{2,32}:[0-9]{15,22}>$' OR "
                        "nickname_badge ~ '^<a?:[A-Za-z0-9_]{2,32}:[0-9]{15,22}>$');",
                        guild_id,
                    )
                    # Restore the original Unicode schedule if custom replacements
                    # removed any of its entries.
                    await connection.executemany(
                        """
                        INSERT INTO emoji_unlocks (
                            guild_id,
                            emoji,
                            nickname_badge,
                            required_level
                        )
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (guild_id, emoji) DO NOTHING;
                        """,
                        [
                            (guild_id, emoji, emoji, required_level)
                            for required_level, emoji in DEFAULT_PARSED_EMOJI_UNLOCKS
                        ],
                    )
                    await connection.execute(
                        """
                        INSERT INTO bot_migrations (guild_id, migration_key)
                        VALUES ($1, $2)
                        ON CONFLICT (guild_id, migration_key) DO NOTHING;
                        """,
                        guild_id,
                        migration_key,
                    )

        rows = await self.pool.fetch(
            """
            SELECT required_level, emoji, nickname_badge
            FROM emoji_unlocks
            WHERE guild_id = $1
            ORDER BY required_level ASC, emoji ASC;
            """,
            guild_id,
        )

        # Brand-new guilds may not have run any older migration path.
        if not rows:
            await self.reset_emoji_unlocks(guild_id)
            rows = await self.pool.fetch(
                """
                SELECT required_level, emoji, nickname_badge
                FROM emoji_unlocks
                WHERE guild_id = $1
                ORDER BY required_level ASC, emoji ASC;
                """,
                guild_id,
            )

        unlocks: list[EmojiUnlock] = []
        repairs: list[tuple[str, int, str]] = []
        for row in rows:
            required_level = int(row["required_level"])
            emoji = str(row["emoji"]).strip()
            if is_custom_emoji_code(emoji):
                continue
            nickname_badge = str(row["nickname_badge"] or "").strip()
            if nickname_badge != emoji:
                nickname_badge = emoji
                repairs.append((emoji, guild_id, emoji))
            unlocks.append((required_level, emoji, nickname_badge))

        if repairs:
            await self.pool.executemany(
                "UPDATE emoji_unlocks SET nickname_badge = $1 "
                "WHERE guild_id = $2 AND emoji = $3;",
                repairs,
            )

        result = tuple(unlocks)
        self._emoji_unlock_cache[guild_id] = result
        return result

    def invalidate_emoji_unlocks(self, guild_id: int) -> None:
        self._emoji_unlock_cache.pop(guild_id, None)

    async def set_emoji_unlock(
        self,
        guild_id: int,
        emoji: str,
        required_level: int,
    ) -> str:
        emoji = validate_display_emoji(emoji)
        if required_level < 0:
            raise ValueError("The required level cannot be negative.")

        await self.pool.execute(
            """
            INSERT INTO emoji_unlocks (
                guild_id,
                emoji,
                nickname_badge,
                required_level
            )
            VALUES ($1, $2, $2, $3)
            ON CONFLICT (guild_id, emoji)
            DO UPDATE SET
                nickname_badge = EXCLUDED.emoji,
                required_level = EXCLUDED.required_level;
            """,
            guild_id,
            emoji,
            required_level,
        )
        self.invalidate_emoji_unlocks(guild_id)
        return emoji

    async def remove_emoji_unlock(self, guild_id: int, emoji: str) -> bool:
        emoji = emoji.strip()
        result = await self.pool.execute(
            "DELETE FROM emoji_unlocks WHERE guild_id = $1 AND emoji = $2;",
            guild_id,
            emoji,
        )
        removed = result.endswith("1")
        if removed:
            await self.pool.execute(
                """
                UPDATE voice_levels
                SET selected_emoji = ''
                WHERE guild_id = $1 AND selected_emoji = $2;
                """,
                guild_id,
                emoji,
            )
            self.invalidate_emoji_unlocks(guild_id)
        return removed

    async def reset_emoji_unlocks(self, guild_id: int) -> None:
        default_emojis = [emoji for _, emoji in DEFAULT_PARSED_EMOJI_UNLOCKS]
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM emoji_unlocks WHERE guild_id = $1;",
                    guild_id,
                )
                await connection.executemany(
                    """
                    INSERT INTO emoji_unlocks (
                        guild_id,
                        emoji,
                        nickname_badge,
                        required_level
                    )
                    VALUES ($1, $2, $3, $4);
                    """,
                    [
                        (guild_id, emoji, emoji, required_level)
                        for required_level, emoji in DEFAULT_PARSED_EMOJI_UNLOCKS
                    ],
                )
                await connection.execute(
                    """
                    UPDATE voice_levels
                    SET selected_emoji = ''
                    WHERE guild_id = $1
                      AND NOT (selected_emoji = ANY($2::TEXT[]));
                    """,
                    guild_id,
                    default_emojis,
                )
        self.invalidate_emoji_unlocks(guild_id)

    async def replace_all_emoji_unlocks(
        self,
        guild_id: int,
        unlocks: tuple[tuple[int, str], ...],
    ) -> None:
        """Atomically replace every configured emoji unlock for one server."""
        if not unlocks:
            raise ValueError("The emoji list must contain at least one entry.")

        emojis = [emoji for _required_level, emoji in unlocks]
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM emoji_unlocks WHERE guild_id = $1;",
                    guild_id,
                )
                await connection.executemany(
                    """
                    INSERT INTO emoji_unlocks (
                        guild_id,
                        emoji,
                        nickname_badge,
                        required_level
                    )
                    VALUES ($1, $2, $2, $3);
                    """,
                    [
                        (guild_id, emoji, required_level)
                        for required_level, emoji in unlocks
                    ],
                )
                # Keep equipped emojis that still exist. Clear removed emojis, while
                # preserving a deliberate no-badge selection.
                await connection.execute(
                    """
                    UPDATE voice_levels
                    SET selected_emoji = ''
                    WHERE guild_id = $1
                      AND selected_emoji <> ''
                      AND selected_emoji <> $2
                      AND NOT (selected_emoji = ANY($3::TEXT[]));
                    """,
                    guild_id,
                    NO_EMOJI_SELECTION,
                    emojis,
                )

        self.invalidate_emoji_unlocks(guild_id)

    async def get_or_create_stats(
        self,
        guild_id: int,
        user_id: int,
    ) -> asyncpg.Record:
        row = await self.pool.fetchrow(
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
        if row is None:
            raise RuntimeError("The database did not return the member's level row.")
        return row

    async def ensure_guild_rows(self, guild: discord.Guild) -> None:
        pairs = [(guild.id, member.id) for member in guild.members if not member.bot]
        if not pairs:
            return
        await self.pool.executemany(ENSURE_MEMBER_SQL, pairs)

    async def apply_bot_member_nickname(self, member: discord.Member) -> str:
        """Give any editable bot the nickname format 🤖│BotName."""
        if not member.bot:
            return "not_bot"

        guild = member.guild
        me = guild.me
        if me is None:
            return "missing_member"

        # This bot may edit its own nickname. Other bots can only be renamed when
        # this bot has Manage Nicknames and its highest role is above theirs.
        if member.id == me.id:
            if not (
                me.guild_permissions.change_nickname
                or me.guild_permissions.manage_nicknames
            ):
                return "missing_change_nickname"
        else:
            if not me.guild_permissions.manage_nicknames:
                return "missing_manage_nicknames"
            if member.top_role >= me.top_role:
                return "role_hierarchy"

        base_name = clean_base_name(
            member.nick or member.global_name or member.name
        )
        max_name_length = max(
            1,
            NICKNAME_MAX_LENGTH - len(BOT_NICKNAME_PREFIX),
        )
        desired = f"{BOT_NICKNAME_PREFIX}{base_name[:max_name_length].rstrip()}"
        if member.nick == desired:
            return "unchanged"

        key = (guild.id, member.id)
        self._nickname_edits.add(key)
        try:
            await member.edit(
                nick=desired,
                reason="Apply the robot nickname prefix to a bot account",
            )
            # Avoid nickname rate-limit bursts when several bots are synced.
            await asyncio.sleep(0.25)
            return "updated"
        except discord.Forbidden:
            log.warning(
                "Cannot update bot nickname for %s in guild %s due to permissions or role hierarchy",
                member.id,
                guild.id,
            )
            return "forbidden"
        except discord.HTTPException:
            log.exception(
                "Discord rejected bot nickname update for %s in guild %s",
                member.id,
                guild.id,
            )
            return "http_error"
        finally:
            self._nickname_edits.discard(key)

    async def apply_bot_nickname(self, guild: discord.Guild) -> str:
        """Compatibility helper that formats this bot's own nickname."""
        member = guild.me
        if member is None:
            return "missing_member"
        return await self.apply_bot_member_nickname(member)

    async def sync_guild_bot_nicknames(
        self,
        guild: discord.Guild,
    ) -> tuple[int, int, int]:
        """Apply 🤖│ to every editable bot account in the server."""
        updated = 0
        unchanged = 0
        skipped = 0

        for member in guild.members:
            if not member.bot:
                continue

            result = await self.apply_bot_member_nickname(member)
            if result == "updated":
                updated += 1
            elif result == "unchanged":
                unchanged += 1
            else:
                skipped += 1

        return updated, unchanged, skipped

    async def apply_level_nickname(
        self,
        member: discord.Member,
        level: int,
        selected_emoji: str | None = None,
    ) -> str:
        """Apply a nickname and return updated, unchanged, or a skip reason."""
        if member.bot:
            return "bot"

        guild = member.guild
        me = guild.me
        if me is None or not me.guild_permissions.manage_nicknames:
            return "missing_manage_nicknames"
        if member == guild.owner:
            return "server_owner"
        if member.top_role >= me.top_role:
            return "role_hierarchy"

        if selected_emoji is None:
            selected_emoji = await self.pool.fetchval(
                "SELECT selected_emoji FROM voice_levels "
                "WHERE guild_id = $1 AND user_id = $2;",
                guild.id,
                member.id,
            )

        unlocks = await self.get_emoji_unlocks(guild.id)
        equipped_emoji = valid_selected_emoji(selected_emoji, level, unlocks)
        if (
            selected_emoji
            and selected_emoji != NO_EMOJI_SELECTION
            and not equipped_emoji
        ):
            # Clear badges that are no longer configured or are not unlocked.
            # An empty value then falls back to the highest unlocked badge.
            await self.pool.execute(
                "UPDATE voice_levels SET selected_emoji = '' "
                "WHERE guild_id = $1 AND user_id = $2;",
                guild.id,
                member.id,
            )

        new_nickname = nickname_with_level(
            member,
            level,
            equipped_emoji,
            unlocks,
        )
        if member.nick == new_nickname:
            return "unchanged"

        key = (guild.id, member.id)
        self._nickname_edits.add(key)
        try:
            await member.edit(
                nick=new_nickname,
                reason=f"Voice level nickname updated to {level}",
            )
            # A small delay avoids Discord nickname rate-limit bursts during mass sync.
            await asyncio.sleep(0.25)
            return "updated"
        except discord.Forbidden:
            log.warning(
                "Cannot edit nickname for %s in %s due to permissions or role hierarchy",
                member.id,
                guild.id,
            )
            return "forbidden"
        except discord.HTTPException:
            log.exception(
                "Discord rejected nickname update for %s in %s",
                member.id,
                guild.id,
            )
            return "http_error"
        finally:
            self._nickname_edits.discard(key)

    async def mark_nickname_synced(
        self,
        guild_id: int,
        user_id: int,
        level: int,
    ) -> None:
        await self.pool.execute(
            """
            UPDATE voice_levels
            SET level = $1, nickname_level = $1
            WHERE guild_id = $2 AND user_id = $3;
            """,
            level,
            guild_id,
            user_id,
        )

    async def sync_guild_nicknames(
        self,
        guild: discord.Guild,
    ) -> tuple[int, int, int]:
        """Format every editable human and bot account in the server."""
        await self.ensure_guild_rows(guild)
        rows = await self.pool.fetch(
            """
            SELECT user_id, total_voice_seconds, selected_emoji
            FROM voice_levels
            WHERE guild_id = $1;
            """,
            guild.id,
        )
        stats = {
            row["user_id"]: (row["total_voice_seconds"], row["selected_emoji"])
            for row in rows
        }

        updated = 0
        unchanged = 0
        skipped = 0

        for member in guild.members:
            if member.bot:
                result = await self.apply_bot_member_nickname(member)
                if result == "updated":
                    updated += 1
                elif result == "unchanged":
                    unchanged += 1
                else:
                    skipped += 1
                continue

            total_seconds, selected_emoji = stats.get(member.id, (0, ""))
            level = level_from_total_seconds(total_seconds)
            result = await self.apply_level_nickname(
                member,
                level,
                selected_emoji,
            )

            if result == "updated":
                updated += 1
                await self.mark_nickname_synced(guild.id, member.id, level)
            elif result == "unchanged":
                unchanged += 1
                await self.mark_nickname_synced(guild.id, member.id, level)
            else:
                skipped += 1

        return updated, unchanged, skipped

    async def startup_nickname_sync(self) -> None:
        await self.wait_until_ready()
        for guild in self.guilds:
            try:
                updated, unchanged, skipped = await self.sync_guild_nicknames(guild)
                log.info(
                    "Startup nickname sync for %s: %s updated, %s unchanged, %s skipped",
                    guild.id,
                    updated,
                    unchanged,
                    skipped,
                )
            except Exception:
                log.exception("Startup nickname sync failed for guild %s", guild.id)

    async def announce_level_up(
        self,
        member: discord.Member,
        old_level: int,
        new_level: int,
    ) -> None:
        if LEVEL_UP_CHANNEL_ID == 0 or new_level <= old_level:
            return

        channel = member.guild.get_channel(LEVEL_UP_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return

        unlocks = await self.get_emoji_unlocks(member.guild.id)
        newly_unlocked = [
            emoji
            for required_level, emoji, _nickname_badge in unlocks
            if old_level < required_level <= new_level
        ]
        message = (
            f"🎉 {member.mention} reached **Level {new_level}** in voice activity!"
        )
        if newly_unlocked:
            message += (
                "\n🔓 New nickname emoji unlocked: "
                + " ".join(newly_unlocked)
                + " — use `/emoji equip`."
            )

        try:
            await channel.send(message)
        except discord.HTTPException:
            log.exception(
                "Could not send level-up message in guild %s",
                member.guild.id,
            )

    async def process_level_changes(
        self,
        rows: Iterable[asyncpg.Record],
    ) -> None:
        level_updates: list[tuple[int, int, int]] = []
        nickname_candidates: list[tuple[int, int, int, int, str]] = []

        for row in rows:
            new_level = level_from_total_seconds(row["total_voice_seconds"])
            old_level = row["level"]
            nickname_level = row["nickname_level"]

            if new_level != old_level:
                level_updates.append((new_level, row["guild_id"], row["user_id"]))
            if new_level != nickname_level:
                nickname_candidates.append(
                    (
                        row["guild_id"],
                        row["user_id"],
                        old_level,
                        new_level,
                        row["selected_emoji"],
                    )
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

        for (
            guild_id,
            user_id,
            old_level,
            new_level,
            selected_emoji,
        ) in nickname_candidates:
            guild = self.get_guild(guild_id)
            member = guild.get_member(user_id) if guild else None
            if member is None:
                continue

            result = await self.apply_level_nickname(
                member,
                new_level,
                selected_emoji,
            )
            if result in {"updated", "unchanged"}:
                await self.mark_nickname_synced(guild_id, user_id, new_level)
            await self.announce_level_up(member, old_level, new_level)

    @tasks.loop(seconds=TICK_SECONDS)
    async def voice_time_loop(self) -> None:
        now = time.monotonic()
        if self._last_tick_monotonic is None:
            self._last_tick_monotonic = now
            return

        elapsed = max(
            1,
            min(round(now - self._last_tick_monotonic), TICK_SECONDS * 3),
        )
        self._last_tick_monotonic = now

        active_pairs: list[tuple[int, int]] = []

        for guild in self.guilds:
            channels: list[discord.abc.Connectable] = [
                *guild.voice_channels,
                *guild.stage_channels,
            ]
            for channel in channels:
                if guild.afk_channel and channel.id == guild.afk_channel.id:
                    continue

                eligible_members = [
                    member
                    for member in channel.members
                    if member_is_eligible(member)
                ]
                if len(eligible_members) < MIN_HUMANS_IN_VC:
                    continue

                active_pairs.extend(
                    (guild.id, member.id) for member in eligible_members
                )

        if not active_pairs:
            return

        active_pairs = list(dict.fromkeys(active_pairs))
        guild_ids = [pair[0] for pair in active_pairs]
        user_ids = [pair[1] for pair in active_pairs]

        try:
            rows = await self.pool.fetch(
                UPSERT_ACTIVE_SQL,
                guild_ids,
                user_ids,
                elapsed,
            )
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
    log.info(
        "Logged in as %s (%s), connected to %s guild(s)",
        bot.user,
        bot.user.id,
        len(bot.guilds),
    )

    for guild in bot.guilds:
        try:
            updated, unchanged, skipped = await bot.sync_guild_bot_nicknames(guild)
            log.info(
                "Bot nickname sync for %s: %s updated, %s unchanged, %s skipped",
                guild.id,
                updated,
                unchanged,
                skipped,
            )
        except Exception:
            log.exception("Bot nickname sync failed for guild %s", guild.id)

    if SYNC_NICKNAMES_ON_START and not bot._startup_sync_started:
        bot._startup_sync_started = True
        asyncio.create_task(bot.startup_nickname_sync())


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    try:
        await bot.sync_guild_bot_nicknames(guild)
    except Exception:
        log.exception("Initial bot nickname sync failed for guild %s", guild.id)
    if SYNC_NICKNAMES_ON_START:
        try:
            await bot.sync_guild_nicknames(guild)
        except Exception:
            log.exception("Initial nickname sync failed for guild %s", guild.id)


@bot.event
async def on_member_join(member: discord.Member) -> None:
    if member.bot:
        await bot.apply_bot_member_nickname(member)
        return

    row = await bot.get_or_create_stats(member.guild.id, member.id)
    level = level_from_total_seconds(row["total_voice_seconds"])
    result = await bot.apply_level_nickname(
        member,
        level,
        row["selected_emoji"],
    )
    if result in {"updated", "unchanged"}:
        await bot.mark_nickname_synced(member.guild.id, member.id, level)


@bot.event
async def on_member_update(
    before: discord.Member,
    after: discord.Member,
) -> None:
    if after.bot:
        if before.nick == after.nick:
            return
        if (after.guild.id, after.id) in bot._nickname_edits:
            return
        await bot.apply_bot_member_nickname(after)
        return
    if before.nick == after.nick:
        return
    if (after.guild.id, after.id) in bot._nickname_edits:
        return

    row = await bot.get_or_create_stats(after.guild.id, after.id)
    level = level_from_total_seconds(row["total_voice_seconds"])
    selected_emoji = row["selected_emoji"]
    unlocks = await bot.get_emoji_unlocks(after.guild.id)
    expected = nickname_with_level(after, level, selected_emoji, unlocks)

    if after.nick != expected:
        result = await bot.apply_level_nickname(
            after,
            level,
            selected_emoji,
        )
        if result in {"updated", "unchanged"}:
            await bot.mark_nickname_synced(after.guild.id, after.id, level)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if member.bot or before.channel == after.channel:
        return
    if after.channel is None:
        return

    # Make sure a member who existed before the bot was installed gets a row
    # and level-0 nickname as soon as they join voice.
    row = await bot.get_or_create_stats(member.guild.id, member.id)
    level = level_from_total_seconds(row["total_voice_seconds"])
    result = await bot.apply_level_nickname(
        member,
        level,
        row["selected_emoji"],
    )
    if result in {"updated", "unchanged"}:
        await bot.mark_nickname_synced(member.guild.id, member.id, level)


@bot.tree.command(name="rank", description="Show voice level and progress.")
@app_commands.guild_only()
@app_commands.describe(member="The member to view; leave blank for yourself")
async def rank(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
) -> None:
    assert interaction.guild is not None
    target = member or interaction.user
    if not isinstance(target, discord.Member):
        await interaction.response.send_message(
            "This command only works in a server.",
            ephemeral=True,
        )
        return

    row = await bot.get_or_create_stats(interaction.guild.id, target.id)
    total = int(row["total_voice_seconds"])
    level, progress, required = progress_for_total(total)
    remaining = max(0, required - progress)
    unlocks = await bot.get_emoji_unlocks(interaction.guild.id)
    selected_emoji = valid_selected_emoji(row["selected_emoji"], level, unlocks)

    # Reapply the nickname whenever rank is checked so the displayed level stays current.
    nickname_result = await bot.apply_level_nickname(
        target,
        level,
        selected_emoji,
    )
    if nickname_result in {"updated", "unchanged"}:
        await bot.mark_nickname_synced(interaction.guild.id, target.id, level)

    embed = discord.Embed(
        title=f"{clean_base_name(target.display_name)}'s Voice Rank",
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(
        name="Level",
        value=f"**{level}**  ·  `{superscript_number(level)}`",
        inline=True,
    )
    embed.add_field(
        name="Total voice time",
        value=f"**{format_duration(total)}**",
        inline=True,
    )
    embed.add_field(
        name="Nickname",
        value=f"`{nickname_with_level(target, level, selected_emoji, unlocks)}`",
        inline=False,
    )

    next_unlock = next_emoji_unlock(level, unlocks)
    unlocked = unlocked_emojis(level, unlocks)
    badge_lines = [
        f"Equipped: **{selected_emoji or 'None'}**",
        "Unlocked: "
        + (" ".join(emoji for _, emoji, _nickname_badge in unlocked) or "None yet"),
    ]
    if next_unlock is not None:
        badge_lines.append(
            f"Next unlock: {next_unlock[1]} at **Level {next_unlock[0]}**"
        )
    embed.add_field(
        name="Nickname emojis",
        value="\n".join(badge_lines),
        inline=False,
    )

    embed.add_field(
        name=f"Progress to Level {level + 1}",
        value=(
            f"`{progress_bar(progress, required)}`\n"
            f"**{format_duration(progress)}** / {format_duration(required)}\n"
            f"**{format_duration(remaining)} remaining**"
        ),
        inline=False,
    )
    embed.set_footer(
        text=(
            f"Voice counts every {TICK_SECONDS}s · "
            f"Minimum humans in channel: {MIN_HUMANS_IN_VC}"
        )
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="leaderboard",
    description="Show the top voice-level members.",
)
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    await bot.ensure_guild_rows(interaction.guild)

    rows = await bot.pool.fetch(
        """
        SELECT user_id, total_voice_seconds
        FROM voice_levels
        WHERE guild_id = $1
        ORDER BY total_voice_seconds DESC, user_id ASC
        LIMIT 10;
        """,
        interaction.guild.id,
    )

    if not rows:
        await interaction.response.send_message(
            "No members are available for the leaderboard yet.",
            ephemeral=True,
        )
        return

    medals = ["🥇", "🥈", "🥉"]
    lines: list[str] = []
    for index, row in enumerate(rows, start=1):
        member = interaction.guild.get_member(row["user_id"])
        name = member.mention if member else f"User `{row['user_id']}`"
        total = int(row["total_voice_seconds"])
        level = level_from_total_seconds(total)
        prefix = medals[index - 1] if index <= 3 else f"**{index}.**"
        lines.append(
            f"{prefix} {name} — **Level {level}** `{superscript_number(level)}` "
            f"· {format_duration(total)}"
        )

    embed = discord.Embed(
        title="Voice Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="syncnickname",
    description="Reapply your │name and superscript voice level.",
)
@app_commands.guild_only()
async def sync_nickname(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command only works in a server.",
            ephemeral=True,
        )
        return

    row = await bot.get_or_create_stats(
        interaction.guild.id,
        interaction.user.id,
    )
    level = level_from_total_seconds(row["total_voice_seconds"])
    unlocks = await bot.get_emoji_unlocks(interaction.guild.id)
    selected_emoji = valid_selected_emoji(row["selected_emoji"], level, unlocks)
    result = await bot.apply_level_nickname(
        interaction.user,
        level,
        selected_emoji,
    )

    if result in {"updated", "unchanged"}:
        await bot.mark_nickname_synced(
            interaction.guild.id,
            interaction.user.id,
            level,
        )
        await interaction.response.send_message(
            f"Your nickname is now "
            f"`{nickname_with_level(interaction.user, level, selected_emoji, unlocks)}`.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "I could not edit your nickname. Give the bot **Manage Nicknames** "
            "and place its role above your highest role. Discord does not allow "
            "bots to rename the server owner.",
            ephemeral=True,
        )


@bot.tree.command(
    name="syncallnicknames",
    description="Apply nickname formatting to every editable member and bot.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def sync_all_nicknames(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    await interaction.response.defer(ephemeral=True, thinking=True)

    updated, unchanged, skipped = await bot.sync_guild_nicknames(
        interaction.guild
    )

    await interaction.followup.send(
        f"Updated **{updated}** member(s), already correct: **{unchanged}**, "
        f"skipped: **{skipped}**.\n"
        "Skipped accounts are normally the server owner, bots or members whose "
        "highest role is equal to or above this bot's role.",
        ephemeral=True,
    )


emoji_group = app_commands.Group(
    name="emoji",
    description="View, equip, or remove unlocked nickname emoji badges.",
)


async def emoji_badge_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None:
        return []

    try:
        row = await bot.get_or_create_stats(
            interaction.guild_id,
            interaction.user.id,
        )
    except Exception:
        log.exception("Emoji autocomplete could not read member stats")
        return []

    level = level_from_total_seconds(row["total_voice_seconds"])
    unlocks = await bot.get_emoji_unlocks(interaction.guild_id)
    current_lower = current.casefold().strip()
    choices: list[app_commands.Choice[str]] = []

    for required_level, emoji, _nickname_badge in unlocked_emojis(level, unlocks):
        label = f"{emoji} — Level {required_level}"
        if current_lower and current_lower not in label.casefold():
            continue
        choices.append(app_commands.Choice(name=label, value=emoji))

    return choices[:25]


@emoji_group.command(
    name="list",
    description="See nickname emojis and the levels needed to unlock them.",
)
@app_commands.guild_only()
async def emoji_list(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command only works in a server.",
            ephemeral=True,
        )
        return

    row = await bot.get_or_create_stats(
        interaction.guild.id,
        interaction.user.id,
    )
    level = level_from_total_seconds(row["total_voice_seconds"])
    unlocks = await bot.get_emoji_unlocks(interaction.guild.id)
    selected_emoji = valid_selected_emoji(row["selected_emoji"], level, unlocks)

    lines: list[str] = []
    for required_level, emoji, _nickname_badge in unlocks:
        unlocked = level >= required_level
        status = "✅ Unlocked" if unlocked else "🔒 Locked"
        equipped = " — **equipped**" if emoji == selected_emoji else ""
        lines.append(
            f"{emoji} — Level **{required_level}** — {status}{equipped}"
        )

    embed = discord.Embed(
        title="Nickname Emoji Unlocks",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Your level", value=f"**{level}**", inline=True)
    embed.add_field(
        name="Equipped",
        value=selected_emoji or "None",
        inline=True,
    )
    embed.set_footer(
        text=(
            "The highest unlocked badge is used automatically. Use /emoji equip "
            "to keep a specific badge, or /emoji remove to hide badges."
        )
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@emoji_group.command(
    name="equip",
    description="Equip a nickname emoji you have unlocked.",
)
@app_commands.guild_only()
@app_commands.describe(badge="Choose one of your unlocked emoji badges")
@app_commands.autocomplete(badge=emoji_badge_autocomplete)
async def emoji_equip(
    interaction: discord.Interaction,
    badge: str,
) -> None:
    assert interaction.guild is not None
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command only works in a server.",
            ephemeral=True,
        )
        return

    badge = badge.strip()
    unlocks = await bot.get_emoji_unlocks(interaction.guild.id)
    required_level = emoji_required_level(unlocks, badge)
    if required_level is None:
        await interaction.response.send_message(
            "That emoji is not in the unlock list. Use `/emoji list` and select "
            "a badge from the suggestions.",
            ephemeral=True,
        )
        return

    row = await bot.get_or_create_stats(
        interaction.guild.id,
        interaction.user.id,
    )
    level = level_from_total_seconds(row["total_voice_seconds"])
    if level < required_level:
        await interaction.response.send_message(
            f"{badge} unlocks at **Level {required_level}**. "
            f"You are currently **Level {level}**.",
            ephemeral=True,
        )
        return

    await bot.pool.execute(
        "UPDATE voice_levels SET selected_emoji = $1 "
        "WHERE guild_id = $2 AND user_id = $3;",
        badge,
        interaction.guild.id,
        interaction.user.id,
    )
    result = await bot.apply_level_nickname(
        interaction.user,
        level,
        badge,
    )

    if result in {"updated", "unchanged"}:
        await bot.mark_nickname_synced(
            interaction.guild.id,
            interaction.user.id,
            level,
        )
        await interaction.response.send_message(
            f"Equipped {badge}. Your nickname is now "
            f"`{nickname_with_level(interaction.user, level, badge, unlocks)}`.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"{badge} was saved, but I could not update your nickname. "
            "Give the bot **Manage Nicknames** and place its role above yours.",
            ephemeral=True,
        )


@emoji_group.command(
    name="remove",
    description="Remove the emoji badge from your nickname.",
)
@app_commands.guild_only()
async def emoji_remove(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command only works in a server.",
            ephemeral=True,
        )
        return

    row = await bot.get_or_create_stats(
        interaction.guild.id,
        interaction.user.id,
    )
    level = level_from_total_seconds(row["total_voice_seconds"])
    unlocks = await bot.get_emoji_unlocks(interaction.guild.id)
    await bot.pool.execute(
        "UPDATE voice_levels SET selected_emoji = $1 "
        "WHERE guild_id = $2 AND user_id = $3;",
        NO_EMOJI_SELECTION,
        interaction.guild.id,
        interaction.user.id,
    )
    result = await bot.apply_level_nickname(
        interaction.user,
        level,
        NO_EMOJI_SELECTION,
    )

    if result in {"updated", "unchanged"}:
        await bot.mark_nickname_synced(
            interaction.guild.id,
            interaction.user.id,
            level,
        )
        await interaction.response.send_message(
            f"Emoji removed. Your nickname is now "
            f"`{nickname_with_level(interaction.user, level, NO_EMOJI_SELECTION, unlocks)}`.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "Your emoji selection was removed, but I could not update your "
            "nickname because of Discord role or nickname permissions.",
            ephemeral=True,
        )


bot.tree.add_command(emoji_group)


emoji_admin_group = app_commands.Group(
    name="emojiadmin",
    description="View or manage this server's level-unlocked nickname emojis.",
)


class EmojiChoiceSelect(discord.ui.Select):
    """Private member-specific dropdown opened from the public control panel."""

    def __init__(
        self,
        guild_id: int,
        user_id: int,
        unlocks: tuple[EmojiUnlock, ...],
        selected_emoji: str,
    ) -> None:
        self.guild_id = guild_id
        self.user_id = user_id

        # Discord allows at most 25 select options. Prefer the newest/highest
        # unlocked badges if a server configured more than that.
        visible_unlocks = list(unlocks)[-25:]
        options = [
            discord.SelectOption(
                label=f"{emoji}  Level {required_level}",
                value=emoji,
                description=(
                    "Currently displayed on your nickname"
                    if selected_emoji == emoji
                    else "Lock this badge to your nickname"
                ),
                default=(selected_emoji == emoji),
            )
            for required_level, emoji, _nickname_badge in visible_unlocks
        ]
        super().__init__(
            placeholder="💠 Select a badge from your vault",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="voicelevels:emoji_panel:choice",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await interaction.response.send_message(
                "This emoji menu belongs to a different server.",
                ephemeral=True,
            )
            return
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Open the control panel yourself to change your nickname emoji.",
                ephemeral=True,
            )
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This control only works inside a server.",
                ephemeral=True,
            )
            return

        badge = self.values[0].strip()
        row = await bot.get_or_create_stats(self.guild_id, self.user_id)
        level = level_from_total_seconds(row["total_voice_seconds"])
        unlocks = await bot.get_emoji_unlocks(self.guild_id)
        required_level = emoji_required_level(unlocks, badge)

        if required_level is None or level < required_level:
            await interaction.response.send_message(
                "That badge is no longer available or has not been unlocked yet.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        await bot.pool.execute(
            "UPDATE voice_levels SET selected_emoji = $1 "
            "WHERE guild_id = $2 AND user_id = $3;",
            badge,
            self.guild_id,
            self.user_id,
        )
        result = await bot.apply_level_nickname(
            interaction.user,
            level,
            badge,
        )
        if result in {"updated", "unchanged"}:
            await bot.mark_nickname_synced(self.guild_id, self.user_id, level)
            embed = discord.Embed(
                title="✦ Badge Locked In",
                description=(
                    f"{badge} is now your selected nickname badge.\n"
                    "It will stay equipped until you choose another badge or turn "
                    "**Auto Upgrade** back on."
                ),
                color=discord.Color.from_rgb(63, 196, 126),
            )
            embed.add_field(name="Auto Upgrade", value="⏸️ **OFF**", inline=True)
            embed.add_field(
                name="Updated Nickname",
                value=f"`{nickname_with_level(interaction.user, level, badge, unlocks)}`",
                inline=False,
            )
        else:
            embed = discord.Embed(
                title="Badge Saved — Nickname Not Updated",
                description=(
                    f"Your {badge} selection was saved, but Discord blocked the nickname "
                    "change. Give the bot **Manage Nicknames** and place its role above "
                    "your highest role."
                ),
                color=discord.Color.orange(),
            )

        await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=None,
        )


class EmojiChoiceView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        user_id: int,
        unlocks: tuple[EmojiUnlock, ...],
        selected_emoji: str,
    ) -> None:
        super().__init__(timeout=180)
        self.add_item(
            EmojiChoiceSelect(guild_id, user_id, unlocks, selected_emoji)
        )


class NicknamePanelButton(discord.ui.Button):
    """Button accessory displayed beside one Nickname Panel section."""

    def __init__(
        self,
        *,
        action: str,
        label: str,
        emoji: str,
        style: discord.ButtonStyle,
        custom_id: str,
    ) -> None:
        super().__init__(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=custom_id,
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.view
        if not isinstance(panel, EmojiControlPanelView):
            await interaction.response.send_message(
                "This control panel is no longer connected correctly. Ask an admin "
                "to post a new one with `/emojiadmin panel`.",
                ephemeral=True,
            )
            return

        if self.action == "vault":
            await panel.open_emoji_menu(interaction)
        elif self.action == "auto":
            await panel.toggle_auto_emoji(interaction)
        elif self.action == "card":
            await panel.view_nickname_card(interaction)
        else:
            await interaction.response.send_message(
                "That panel action is not available.",
                ephemeral=True,
            )


class EmojiControlPanelView(discord.ui.LayoutView):
    """Persistent Components V2 Nickname Panel with inline section buttons."""

    def __init__(
        self,
        guild_name: str = "YOUR SERVER",
        thumbnail_url: str | None = None,
    ) -> None:
        super().__init__(timeout=None)

        vault_button = NicknamePanelButton(
            action="vault",
            label="Open",
            emoji="💠",
            style=discord.ButtonStyle.success,
            custom_id="voicelevels:emoji_panel:open",
        )
        auto_button = NicknamePanelButton(
            action="auto",
            label="Toggle",
            emoji="⚡",
            style=discord.ButtonStyle.primary,
            custom_id="voicelevels:emoji_panel:auto",
        )
        card_button = NicknamePanelButton(
            action="card",
            label="View",
            emoji="📇",
            style=discord.ButtonStyle.secondary,
            custom_id="voicelevels:emoji_panel:status",
        )

        panel = discord.ui.Container(
            accent_color=discord.Color.from_rgb(118, 82, 255),
        )

        header_text = (
            f"### ✦ {guild_name} · MEMBER CUSTOMIZATION\n"
            "# NICKNAME PANEL\n"
            "Build a nickname style that grows with your voice level. "
            "Every action opens privately for you."
        )
        if thumbnail_url:
            panel.add_item(
                discord.ui.Section(
                    header_text,
                    accessory=discord.ui.Thumbnail(
                        thumbnail_url,
                        description="Nickname Panel",
                    ),
                )
            )
        else:
            panel.add_item(discord.ui.TextDisplay(header_text))

        panel.add_item(
            discord.ui.Separator(
                visible=True,
                spacing=discord.SeparatorSpacing.large,
            )
        )
        panel.add_item(
            discord.ui.Section(
                "### 💠 BADGE VAULT",
                "Browse every emoji badge you have unlocked and lock your favorite "
                "beside your nickname. Your badge menu is visible only to you.",
                accessory=vault_button,
            )
        )

        panel.add_item(
            discord.ui.Separator(
                visible=True,
                spacing=discord.SeparatorSpacing.large,
            )
        )
        panel.add_item(
            discord.ui.Section(
                "### ⚡ AUTO UPGRADE",
                "Keep your badge matched to your newest unlock, or pause the system "
                "to hold the badge you like. Press again whenever you want to switch back.",
                accessory=auto_button,
            )
        )

        panel.add_item(
            discord.ui.Separator(
                visible=True,
                spacing=discord.SeparatorSpacing.large,
            )
        )
        panel.add_item(
            discord.ui.Section(
                "### 📇 NICKNAME CARD",
                "View your level, current badge, voice time, nickname mode, unlocked "
                "badge count, and next reward in one private card.",
                accessory=card_button,
            )
        )

        panel.add_item(
            discord.ui.Separator(
                visible=False,
                spacing=discord.SeparatorSpacing.large,
            )
        )
        panel.add_item(
            discord.ui.TextDisplay(
                "-# Public panel · Private actions · Only admins can post a new panel"
            )
        )
        self.add_item(panel)

    async def open_emoji_menu(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(
            interaction.user, discord.Member
        ):
            await interaction.response.send_message(
                "This control only works inside a server.",
                ephemeral=True,
            )
            return

        row = await bot.get_or_create_stats(
            interaction.guild.id,
            interaction.user.id,
        )
        level = level_from_total_seconds(row["total_voice_seconds"])
        unlocks = await bot.get_emoji_unlocks(interaction.guild.id)
        available = unlocked_emojis(level, unlocks)
        if not available:
            await interaction.response.send_message(
                "You have not unlocked any nickname badges yet.",
                ephemeral=True,
            )
            return

        raw_selection = str(row["selected_emoji"] or "").strip()
        current = valid_selected_emoji(raw_selection, level, unlocks)
        auto_enabled = not raw_selection
        embed = discord.Embed(
            title="💠 Your Badge Vault",
            description=(
                "Choose any badge you have earned and lock it beside your name.\n"
                "Selecting a badge pauses **Auto Upgrade** so your choice stays equipped."
            ),
            color=discord.Color.from_rgb(118, 82, 255),
        )
        embed.add_field(name="Voice Level", value=f"**{level}**", inline=True)
        embed.add_field(
            name="Displayed Badge",
            value=f"**{current or 'None'}**",
            inline=True,
        )
        embed.add_field(
            name="Auto Upgrade",
            value="⚡ **ON**" if auto_enabled else "⏸️ **OFF**",
            inline=True,
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(
            text=f"{len(available)} badge(s) unlocked • This menu is private"
        )
        await interaction.response.send_message(
            embed=embed,
            view=EmojiChoiceView(
                interaction.guild.id,
                interaction.user.id,
                available,
                current,
            ),
            ephemeral=True,
        )

    async def toggle_auto_emoji(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(
            interaction.user, discord.Member
        ):
            await interaction.response.send_message(
                "This control only works inside a server.",
                ephemeral=True,
            )
            return

        row = await bot.get_or_create_stats(
            interaction.guild.id,
            interaction.user.id,
        )
        level = level_from_total_seconds(row["total_voice_seconds"])
        unlocks = await bot.get_emoji_unlocks(interaction.guild.id)
        raw_selection = str(row["selected_emoji"] or "").strip()

        if not raw_selection:
            current_badge = valid_selected_emoji("", level, unlocks)
            if not current_badge:
                await interaction.response.send_message(
                    "You need to unlock a badge before Auto Upgrade can be paused.",
                    ephemeral=True,
                )
                return
            new_selection = current_badge
            auto_enabled = False
        else:
            new_selection = ""
            auto_enabled = True

        await interaction.response.defer(ephemeral=True)
        await bot.pool.execute(
            "UPDATE voice_levels SET selected_emoji = $1 "
            "WHERE guild_id = $2 AND user_id = $3;",
            new_selection,
            interaction.guild.id,
            interaction.user.id,
        )
        result = await bot.apply_level_nickname(
            interaction.user,
            level,
            new_selection,
        )
        if result in {"updated", "unchanged"}:
            await bot.mark_nickname_synced(
                interaction.guild.id,
                interaction.user.id,
                level,
            )

        displayed_badge = valid_selected_emoji(new_selection, level, unlocks)
        if auto_enabled:
            embed = discord.Embed(
                title="⚡ Auto Upgrade Activated",
                description=(
                    "Your nickname will now automatically use your newest unlocked badge "
                    "whenever you reach a badge level."
                ),
                color=discord.Color.from_rgb(88, 145, 255),
            )
            embed.add_field(name="Status", value="🟢 **ON**", inline=True)
            embed.add_field(
                name="Current Badge",
                value=f"**{displayed_badge or 'None'}**",
                inline=True,
            )
        else:
            embed = discord.Embed(
                title="⏸️ Auto Upgrade Paused",
                description=(
                    f"Your {new_selection} badge is now locked in and will not change "
                    "when you level up."
                ),
                color=discord.Color.from_rgb(255, 184, 77),
            )
            embed.add_field(name="Status", value="⏸️ **OFF**", inline=True)
            embed.add_field(
                name="Locked Badge",
                value=f"**{new_selection}**",
                inline=True,
            )

        if result not in {"updated", "unchanged"}:
            embed.add_field(
                name="Nickname Update",
                value=(
                    "Your setting was saved, but Discord blocked the nickname edit. "
                    "Check the bot's **Manage Nicknames** permission and role position."
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def view_nickname_card(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(
            interaction.user, discord.Member
        ):
            await interaction.response.send_message(
                "This control only works inside a server.",
                ephemeral=True,
            )
            return

        row = await bot.get_or_create_stats(
            interaction.guild.id,
            interaction.user.id,
        )
        total_seconds = int(row["total_voice_seconds"])
        level = level_from_total_seconds(total_seconds)
        unlocks = await bot.get_emoji_unlocks(interaction.guild.id)
        raw_selection = str(row["selected_emoji"] or "").strip()
        current = valid_selected_emoji(raw_selection, level, unlocks)
        available = unlocked_emojis(level, unlocks)
        next_unlock = next_emoji_unlock(level, unlocks)
        auto_enabled = not raw_selection

        if auto_enabled:
            mode_text = "⚡ Auto Upgrade"
        elif raw_selection == NO_EMOJI_SELECTION:
            mode_text = "🚫 No Badge"
        else:
            mode_text = "🔒 Badge Locked"

        embed = discord.Embed(
            title="✦ Your Nickname Card",
            description=(
                f"`{nickname_with_level(interaction.user, level, raw_selection, unlocks)}`\n"
                "A private snapshot of your current nickname setup."
            ),
            color=discord.Color.from_rgb(118, 82, 255),
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="Voice Level", value=f"**{level}**", inline=True)
        embed.add_field(
            name="Current Badge",
            value=f"**{current or 'None'}**",
            inline=True,
        )
        embed.add_field(name="Mode", value=mode_text, inline=True)
        embed.add_field(
            name="Unlocked Vault",
            value=f"**{len(available)}** badge(s)",
            inline=True,
        )
        embed.add_field(
            name="Voice Time",
            value=f"**{format_duration(total_seconds)}**",
            inline=True,
        )
        embed.add_field(
            name="Next Badge",
            value=(
                f"{next_unlock[1]} at **Level {next_unlock[0]}**"
                if next_unlock is not None
                else "All badges unlocked"
            ),
            inline=True,
        )
        embed.set_footer(
            text="Use Badge Vault to lock a badge • Use Auto Upgrade to follow your level"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class EmojiBulkEditModal(discord.ui.Modal):
    """Private popup used to replace every emoji unlock in one paste."""

    def __init__(self, guild_id: int, current_block: str) -> None:
        super().__init__(title="Edit All Emoji Unlocks", timeout=600)
        self.guild_id = guild_id
        self.emoji_list = discord.ui.TextInput(
            label="One level=emoji entry per line",
            style=discord.TextStyle.paragraph,
            default=current_block,
            placeholder="0=⚪\n1=🟣\n5=⭐\n10=💎",
            required=True,
            max_length=4000,
        )
        self.add_item(self.emoji_list)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await interaction.response.send_message(
                "This emoji editor is no longer connected to the correct server.",
                ephemeral=True,
            )
            return

        member = interaction.user
        if (
            not isinstance(member, discord.Member)
            or not member.guild_permissions.manage_guild
        ):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to edit all emojis.",
                ephemeral=True,
            )
            return

        try:
            parsed = parse_bulk_emoji_unlocks(str(self.emoji_list.value))
            # Reuse the normal Unicode validation so bulk edits follow the same rules.
            unlocks = tuple(
                (required_level, validate_display_emoji(emoji))
                for required_level, emoji in parsed
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await bot.replace_all_emoji_unlocks(interaction.guild.id, unlocks)
        updated, unchanged, skipped = await bot.sync_guild_nicknames(
            interaction.guild
        )
        await interaction.followup.send(
            f"Saved **{len(unlocks)}** emoji unlock(s) at once.\n"
            f"Nicknames updated: **{updated}**, already correct: **{unchanged}**, "
            f"skipped: **{skipped}**.",
            ephemeral=True,
        )


async def configured_emoji_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None:
        return []
    try:
        unlocks = await bot.get_emoji_unlocks(interaction.guild_id)
    except Exception:
        log.exception("Configured emoji autocomplete failed")
        return []

    current_lower = current.casefold().strip()
    choices: list[app_commands.Choice[str]] = []
    for required_level, emoji, _nickname_badge in unlocks:
        label = f"{emoji} — Level {required_level}"
        if current_lower and current_lower not in label.casefold():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=emoji))
    return choices[:25]


@emoji_admin_group.command(
    name="panel",
    description="Post the public Nickname Panel control panel.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def emoji_admin_panel(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None

    thumbnail_url: str | None = None
    if interaction.client.user is not None:
        thumbnail_url = interaction.client.user.display_avatar.url

    await interaction.response.send_message(
        view=EmojiControlPanelView(
            guild_name=interaction.guild.name,
            thumbnail_url=thumbnail_url,
        )
    )


@emoji_admin_group.command(
    name="editall",
    description="Open one popup to copy, paste, and replace every emoji unlock.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def emoji_admin_edit_all(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    unlocks = await bot.get_emoji_unlocks(interaction.guild.id)
    current_block = format_emoji_unlock_block(unlocks)
    if len(current_block) > 4000:
        await interaction.response.send_message(
            "There are too many configured entries to fit in Discord's editor. "
            "Remove a few entries first with `/emojiadmin remove`.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(
        EmojiBulkEditModal(interaction.guild.id, current_block)
    )


@emoji_admin_group.command(
    name="set",
    description="Add a Unicode emoji unlock or change its required level.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    emoji="A normal Unicode emoji such as ⚪, 💎, or 👑",
    level="The level members must reach to unlock it",
)
async def emoji_admin_set(
    interaction: discord.Interaction,
    emoji: str,
    level: app_commands.Range[int, 0, 100000],
) -> None:
    assert interaction.guild is not None
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        emoji = validate_display_emoji(emoji)
        await bot.set_emoji_unlock(interaction.guild.id, emoji, int(level))
    except ValueError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return

    updated, unchanged, skipped = await bot.sync_guild_nicknames(interaction.guild)
    await interaction.followup.send(
        f"Saved {emoji} at **Level {level}**.\n"
        f"Nicknames updated: **{updated}**, already correct: **{unchanged}**, "
        f"skipped: **{skipped}**.",
        ephemeral=True,
    )


@emoji_admin_group.command(
    name="remove",
    description="Remove an emoji unlock from this server.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(emoji="The configured Unicode emoji to remove")
@app_commands.autocomplete(emoji=configured_emoji_autocomplete)
async def emoji_admin_remove(
    interaction: discord.Interaction,
    emoji: str,
) -> None:
    assert interaction.guild is not None
    await interaction.response.defer(ephemeral=True, thinking=True)
    unlocks = await bot.get_emoji_unlocks(interaction.guild.id)
    if len(unlocks) <= 1:
        await interaction.followup.send(
            "You must keep at least one configured emoji. Use `/emojiadmin editall` "
            "to change the final emoji instead.",
            ephemeral=True,
        )
        return

    removed = await bot.remove_emoji_unlock(interaction.guild.id, emoji)
    if not removed:
        await interaction.followup.send(
            "That emoji is not configured. Use `/emojiadmin list`.",
            ephemeral=True,
        )
        return

    updated, unchanged, skipped = await bot.sync_guild_nicknames(interaction.guild)
    await interaction.followup.send(
        f"Removed {emoji}. Members who had it equipped were reset to no badge. "
        f"Nicknames updated: **{updated}**, already correct: **{unchanged}**, "
        f"skipped: **{skipped}**.",
        ephemeral=True,
    )


@emoji_admin_group.command(
    name="list",
    description="List this server's configured emoji unlocks.",
)
@app_commands.guild_only()
async def emoji_admin_list(interaction: discord.Interaction) -> None:
    """This is the only /emojiadmin subcommand available to every member."""
    assert interaction.guild is not None
    unlocks = await bot.get_emoji_unlocks(interaction.guild.id)
    lines: list[str] = []
    for required_level, emoji, _nickname_badge in unlocks:
        lines.append(f"{emoji} — Level **{required_level}**")
    embed = discord.Embed(
        title="Server Emoji Unlock Settings",
        description="\n".join(lines) or "No emoji unlocks configured.",
        color=discord.Color.blurple(),
    )
    embed.set_footer(
        text=(
            "Everyone can use this list. Managers can use /emojiadmin editall "
            "to replace everything with one paste."
        )
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@emoji_admin_group.command(
    name="reset",
    description="Reset this server's emoji unlocks to the default list.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(confirm="Set this to True to confirm the reset")
async def emoji_admin_reset(
    interaction: discord.Interaction,
    confirm: bool,
) -> None:
    assert interaction.guild is not None
    if not confirm:
        await interaction.response.send_message(
            "Reset cancelled. Run the command again with **confirm: True**.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    await bot.reset_emoji_unlocks(interaction.guild.id)
    updated, unchanged, skipped = await bot.sync_guild_nicknames(interaction.guild)
    await interaction.followup.send(
        f"Emoji unlocks were reset to the defaults. Nicknames updated: "
        f"**{updated}**, already correct: **{unchanged}**, skipped: **{skipped}**.",
        ephemeral=True,
    )


bot.tree.add_command(emoji_admin_group)



@bot.tree.command(
    name="levelstatus",
    description="Check voice tracking, database, and nickname permissions.",
)
@app_commands.guild_only()
async def level_status(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    await interaction.response.defer(ephemeral=True, thinking=True)

    me = interaction.guild.me
    manage_nicknames = bool(me and me.guild_permissions.manage_nicknames)
    database_ok = False
    try:
        database_ok = bool(await bot.pool.fetchval("SELECT 1;"))
    except Exception:
        log.exception("Database status check failed")

    embed = discord.Embed(
        title="Voice Level Bot Status",
        color=discord.Color.green() if database_ok else discord.Color.red(),
    )
    embed.add_field(
        name="Database",
        value="✅ Connected" if database_ok else "❌ Not connected",
        inline=True,
    )
    embed.add_field(
        name="Manage Nicknames",
        value="✅ Enabled" if manage_nicknames else "❌ Missing",
        inline=True,
    )
    embed.add_field(
        name="Tracking rule",
        value=(
            f"At least **{MIN_HUMANS_IN_VC}** eligible human(s) in voice\n"
            f"Deafened users: **not counted**\n"
            f"Muted users: **{'not counted' if REQUIRE_UNMUTED else 'counted'}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="Level curve",
        value=(
            f"Level 0 → 1: **{BASE_HOURS:g} hour(s)**\n"
            f"Each next level adds **{HOURS_PER_LEVEL:g} hour(s)**"
        ),
        inline=False,
    )
    unlocks = await bot.get_emoji_unlocks(interaction.guild.id)
    unlock_text = " ".join(
        f"{emoji}=L{required_level}"
        for required_level, emoji, _nickname_badge in unlocks
    ) or "None configured"
    example_level, example_emoji, example_badge = (
        unlocks[0] if unlocks else (0, "⭐", "⭐")
    )
    embed.add_field(
        name="Emoji unlocks",
        value=unlock_text,
        inline=False,
    )
    embed.add_field(
        name="Bot nickname",
        value=f"`{BOT_NICKNAME_PREFIX}{clean_base_name(interaction.guild.me.display_name) if interaction.guild.me else 'BotName'}`",
        inline=False,
    )
    embed.add_field(
        name="Expected nickname",
        value=(
            f"No badge: `{NICKNAME_PREFIX}ExampleName{superscript_number(0)}`\n"
            f"With badge: `{example_badge}{NICKNAME_PREFIX}"
            f"ExampleName{superscript_number(example_level)}`\n"
            f"Menu/message emoji: {example_emoji}"
        ),
        inline=False,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@sync_all_nicknames.error
async def sync_all_nicknames_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the **Manage Server** permission to use this command."
    else:
        log.error(
            "syncallnicknames command failed: %r",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "That command failed. Check the Railway logs for the exact error."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    original = getattr(error, "original", error)
    log.error(
        "Slash command failed: %r",
        original,
        exc_info=(type(original), original, original.__traceback__),
    )
    message = "The command failed. Check `/levelstatus` and the Railway logs."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
