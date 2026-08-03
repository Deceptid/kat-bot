from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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
log = logging.getLogger("kat")


def env_flag(name: str, default: bool = False) -> bool:
    fallback = "true" if default else "false"
    return os.getenv(name, fallback).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw or default)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a whole number.") from exc


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number.") from exc


def parse_id_list(raw: str) -> set[int]:
    ids: set[int] = set()
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            ids.add(int(piece))
        except ValueError as exc:
            raise RuntimeError(
                "LFG_CHANNEL_IDS must contain Discord channel IDs separated by commas."
            ) from exc
    return ids


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

# Kat game-alert settings. /ping works in any text channel or thread whose
# name matches a game configured through /gameadmin.
PINGER_ROLE_ID = env_int("PINGER_ROLE_ID", 0)
# Kept as an optional legacy variable so existing Railway setups do not break.
# It no longer limits where /ping can be used.
LFG_CHANNEL_IDS = parse_id_list(os.getenv("LFG_CHANNEL_IDS", ""))
COOLDOWN_MINUTES = max(0, env_int("COOLDOWN_MINUTES", 15))
MAX_RECIPIENTS = max(1, env_int("MAX_RECIPIENTS", 500))
DM_DELAY_SECONDS = max(0.0, env_float("DM_DELAY_SECONDS", 0.20))
BOT_NAME = os.getenv("BOT_NAME", "Kat").strip() or "Kat"

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


def parse_bulk_emoji_unlocks(raw: str) -> tuple[tuple[int, str], ...]:
    """Parse a copy/paste block containing one level=emoji entry per line.

    Commas and semicolons are also accepted.
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


DEFAULT_PARSED_EMOJI_UNLOCKS = parse_emoji_unlocks(DEFAULT_EMOJI_UNLOCKS)

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
    """Return every saved entry as a paste-ready level=emoji block."""
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

ALTER TABLE voice_levels
ADD COLUMN IF NOT EXISTS level_locked BOOLEAN NOT NULL DEFAULT FALSE;

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

CREATE TABLE IF NOT EXISTS katping_opt_outs (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    opted_out_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS katping_ping_log (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    inviter_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    post_name TEXT NOT NULL,
    game_name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    delivered_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS katping_ping_log_guild_created_idx
ON katping_ping_log (guild_id, created_at DESC);

CREATE TABLE IF NOT EXISTS game_notification_roles (
    guild_id BIGINT NOT NULL,
    game_key TEXT NOT NULL,
    game_name TEXT NOT NULL,
    role_id BIGINT NOT NULL,
    emoji TEXT NOT NULL DEFAULT '🎮',
    created_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, game_key),
    UNIQUE (guild_id, role_id)
);

CREATE INDEX IF NOT EXISTS game_notification_roles_guild_name_idx
ON game_notification_roles (guild_id, game_name);
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
    total_voice_seconds = CASE
        WHEN voice_levels.level_locked THEN voice_levels.total_voice_seconds
        ELSE voice_levels.total_voice_seconds + EXCLUDED.total_voice_seconds
    END,
    last_active_at = NOW()
RETURNING guild_id, user_id, total_voice_seconds, level, nickname_level,
          selected_emoji, level_locked;
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
    *,
    ignore_level_requirement: bool = False,
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
    if required_level is None:
        return ""
    if level < required_level and not ignore_level_requirement:
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
    *,
    ignore_level_requirement: bool = False,
) -> str:
    """Return Badge│ExampleName⁰, or │ExampleName⁰ with no equipped badge."""
    current_name = member.nick or member.global_name or member.name
    base_name = clean_base_name(current_name)
    suffix = superscript_number(level)
    display_emoji = valid_selected_emoji(
        selected_emoji,
        level,
        unlocks,
        ignore_level_requirement=ignore_level_requirement,
    )
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


@dataclass(frozen=True)
class LfgContext:
    post_name: str
    channel_name: str
    channel_id: int
    allowed_channel_id: int
    jump_url: str


def get_lfg_context(
    guild: discord.Guild,
    channel: discord.abc.GuildChannel | discord.Thread,
) -> LfgContext:
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        allowed_channel_id = parent.id if parent is not None else channel.id
        channel_name = parent.name if parent is not None else "LFG"
        post_name = channel.name
    else:
        allowed_channel_id = channel.id
        channel_name = channel.name
        post_name = channel.name.replace("-", " ").title()

    jump_url = f"https://discord.com/channels/{guild.id}/{channel.id}"
    return LfgContext(
        post_name=post_name,
        channel_name=channel_name,
        channel_id=channel.id,
        allowed_channel_id=allowed_channel_id,
        jump_url=jump_url,
    )


def footer_guild_id(embed: discord.Embed) -> int | None:
    text = embed.footer.text or ""
    match = re.search(r"Guild ID: (\d{15,22})", text)
    return int(match.group(1)) if match else None


class FollowPostButton(discord.ui.Button):
    """Add the clicking member to the current forum post/thread."""

    def __init__(self) -> None:
        super().__init__(
            label="Notify Me / Follow Post",
            style=discord.ButtonStyle.success,
            emoji="🔔",
            custom_id="katping:follow_post",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        member = interaction.user

        if not isinstance(channel, discord.Thread):
            await interaction.response.send_message(
                (
                    "Automatic following works inside Discord forum posts and threads. "
                    "For a normal text channel, right-click the channel and choose "
                    "**Notification Settings** to enable notifications."
                ),
                ephemeral=True,
            )
            return

        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This button only works inside a Discord server.",
                ephemeral=True,
            )
            return

        if channel.archived:
            await interaction.response.send_message(
                "This post is archived, so Discord will not let Kat add new followers.",
                ephemeral=True,
            )
            return

        try:
            # Adding a member to a forum post's underlying thread makes Discord
            # treat that member as following/joined to the post.
            await channel.add_user(member)
        except discord.Forbidden:
            await interaction.response.send_message(
                (
                    "Kat could not make you follow this post. Give Kat **View Channel**, "
                    "**Send Messages**, and **Send Messages in Threads** in the LFG forum."
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            log.exception(
                "Discord rejected follow request for user %s in thread %s",
                member.id,
                channel.id,
            )
            await interaction.response.send_message(
                "Discord could not follow this post right now. Try again in a moment.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            (
                f"🔔 You are now following **{channel.name}**. Discord will show the "
                "post as **Following**, and notifications will use your personal "
                "thread notification settings."
            ),
            ephemeral=True,
        )


class ChannelNotificationButton(discord.ui.Button):
    """Unfollow a thread when possible, otherwise show channel-mute instructions."""

    def __init__(self) -> None:
        super().__init__(
            label="Turn Off Notifications",
            style=discord.ButtonStyle.secondary,
            emoji="🔕",
            custom_id="katping:channel_notification_help",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        member = interaction.user
        channel_name = getattr(channel, "name", "this channel")

        if isinstance(channel, discord.Thread) and isinstance(member, discord.Member):
            me = channel.guild.me
            can_remove = (
                me is not None
                and channel.permissions_for(me).manage_threads
                and not channel.archived
            )
            if can_remove:
                try:
                    await channel.remove_user(member)
                except discord.NotFound:
                    # The member was already not following the post.
                    pass
                except (discord.Forbidden, discord.HTTPException):
                    log.exception(
                        "Could not remove user %s from thread %s",
                        member.id,
                        channel.id,
                    )
                else:
                    await interaction.response.send_message(
                        f"🔕 You are no longer following **{channel.name}**.",
                        ephemeral=True,
                    )
                    return

        await interaction.response.send_message(
            (
                f"To stop `@here` alerts from **#{channel_name}**, use Discord's "
                "channel notification settings:\n"
                "**Desktop:** Right-click the channel or forum post → "
                "**Notification Settings** → **Nothing**, or choose **Mute Channel**.\n"
                "**Mobile:** Press and hold the channel or post → "
                "**Notifications** → **Nothing**, or mute it.\n\n"
                "For one-click unfollowing, give Kat the **Manage Threads** permission. "
                "Discord still does not allow bots to change your personal notification "
                "level directly."
            ),
            ephemeral=True,
        )




PRIVILEGED_ROLE_PERMISSION_NAMES = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "kick_members",
    "ban_members",
    "moderate_members",
    "manage_messages",
    "mention_everyone",
    "manage_webhooks",
    "manage_threads",
)


def normalize_game_key(name: str) -> str:
    """Create a stable key so post names match configured games."""
    normalized = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
    return re.sub(r"\s+", " ", normalized)


def role_has_privileged_permissions(role: discord.Role) -> bool:
    permissions = role.permissions
    return any(getattr(permissions, name, False) for name in PRIVILEGED_ROLE_PERMISSION_NAMES)


async def fetch_game_role_rows(
    pool: asyncpg.Pool,
    guild_id: int,
) -> list[asyncpg.Record]:
    rows = await pool.fetch(
        """
        SELECT game_key, game_name, role_id, emoji
        FROM game_notification_roles
        WHERE guild_id = $1
        ORDER BY LOWER(game_name), role_id;
        """,
        guild_id,
    )
    return list(rows)


async def find_game_notification_role(
    pool: asyncpg.Pool,
    guild: discord.Guild,
    post_name: str,
) -> tuple[discord.Role | None, str | None]:
    game_key = normalize_game_key(post_name)
    if not game_key:
        return None, None

    row = await pool.fetchrow(
        """
        SELECT game_name, role_id
        FROM game_notification_roles
        WHERE guild_id = $1 AND game_key = $2;
        """,
        guild.id,
        game_key,
    )
    if row is None:
        return None, None

    role = guild.get_role(int(row["role_id"]))
    if role is None:
        return None, str(row["game_name"])
    return role, str(row["game_name"])


def build_game_roles_panel_embed(
    guild: discord.Guild,
    configured_count: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="ROLES & GAME ALERTS",
        description=(
            "Choose the games you want notifications for. Your selections are "
            "private, and you can change them whenever you want."
        ),
        color=discord.Color.from_rgb(87, 242, 135),
    )
    embed.add_field(
        name="🎮 PICK YOUR GAMES",
        value=(
            "Press **Choose Game Roles**, then tap a game button to turn its "
            "notification role on or off. Green buttons are currently selected."
        ),
        inline=False,
    )
    embed.add_field(
        name="🔔 GAME-SPECIFIC ALERTS",
        value=(
            "Use `/ping` in any text channel, forum post, or thread whose name "
            "matches a configured game. Kat uses `@here` for active members and "
            "DMs subscribed members who were not already covered by `@here`."
        ),
        inline=False,
    )
    embed.add_field(
        name="🛡️ ADMIN CONTROL",
        value=(
            "Only administrators can add games, connect roles, remove games, or "
            "post a new panel."
        ),
        inline=False,
    )
    embed.set_footer(
        text=f"{configured_count} game role(s) configured • Private role selection"
    )
    if guild.icon is not None:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


GAME_ROLES_PER_PAGE = 20


def safe_component_emoji(value: str) -> str:
    """Return a safe standard emoji for Discord component buttons."""
    emoji = (value or "").strip()
    if not emoji or len(emoji) > 8 or is_custom_emoji_code(emoji):
        return "🎮"
    return emoji


class GameRoleToggleButton(discord.ui.Button):
    def __init__(
        self,
        picker: "GameRolePickerView",
        row_data: asyncpg.Record,
        *,
        button_row: int,
    ) -> None:
        self.picker = picker
        self.role_id = int(row_data["role_id"])
        self.game_name = str(row_data["game_name"])
        selected = self.role_id in picker.selected_role_ids
        super().__init__(
            label=self.game_name[:80],
            style=(
                discord.ButtonStyle.success
                if selected
                else discord.ButtonStyle.secondary
            ),
            emoji=safe_component_emoji(str(row_data["emoji"] or "🎮")),
            custom_id=f"kat:toggle_game_role:{self.role_id}",
            row=button_row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.picker.toggle_role(interaction, self.role_id, self.game_name)


class GameRolePageButton(discord.ui.Button):
    def __init__(
        self,
        picker: "GameRolePickerView",
        *,
        direction: int,
    ) -> None:
        self.picker = picker
        self.direction = direction
        previous = direction < 0
        super().__init__(
            label="Previous Page" if previous else "Next Page",
            style=discord.ButtonStyle.primary,
            emoji="◀️" if previous else "▶️",
            custom_id=(
                "kat:game_roles_previous_page"
                if previous
                else "kat:game_roles_next_page"
            ),
            disabled=(
                picker.page <= 0
                if previous
                else picker.page >= picker.page_count - 1
            ),
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.picker.change_page(interaction, self.direction)


class GameRoleDoneButton(discord.ui.Button):
    def __init__(self, picker: "GameRolePickerView") -> None:
        self.picker = picker
        super().__init__(
            label="Done",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id="kat:game_roles_done",
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.picker.finish(interaction)


class GameRolePickerView(discord.ui.View):
    """Private paginated role buttons, similar to a Discord game picker."""

    def __init__(
        self,
        bot: "KatBot",
        guild: discord.Guild,
        member: discord.Member,
        rows: list[asyncpg.Record],
        *,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild.id
        self.member_id = member.id
        self.rows = rows
        self.page_count = max(1, math.ceil(len(rows) / GAME_ROLES_PER_PAGE))
        self.page = max(0, min(page, self.page_count - 1))
        self.selected_role_ids = {role.id for role in member.roles}
        self.rebuild_items()

    def content(self, status: str | None = None) -> str:
        selected_count = sum(
            1 for row in self.rows if int(row["role_id"]) in self.selected_role_ids
        )
        lines = [
            "**Choose the games you want notifications for.**",
            "Green = selected • Gray = not selected",
            f"Page **{self.page + 1}/{self.page_count}** • Selected **{selected_count}**",
        ]
        if status:
            lines.append(f"\n{status}")
        return "\n".join(lines)

    def rebuild_items(self) -> None:
        self.clear_items()
        start = self.page * GAME_ROLES_PER_PAGE
        page_rows = self.rows[start : start + GAME_ROLES_PER_PAGE]
        for index, row_data in enumerate(page_rows):
            self.add_item(
                GameRoleToggleButton(
                    self,
                    row_data,
                    button_row=index // 5,
                )
            )

        self.add_item(GameRolePageButton(self, direction=-1))
        self.add_item(
            discord.ui.Button(
                label=f"{self.page + 1}/{self.page_count}",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                custom_id="kat:game_roles_page_number",
                row=4,
            )
        )
        self.add_item(GameRolePageButton(self, direction=1))
        self.add_item(GameRoleDoneButton(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.member_id:
            return True
        await interaction.response.send_message(
            "Open the game-role panel yourself to change your roles.",
            ephemeral=True,
        )
        return False

    async def toggle_role(
        self,
        interaction: discord.Interaction,
        role_id: int,
        game_name: str,
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This menu only works inside a Discord server.",
                ephemeral=True,
            )
            return

        me = guild.me
        role = guild.get_role(role_id)
        if role is None:
            self.rows = [row for row in self.rows if int(row["role_id"]) != role_id]
            self.page_count = max(
                1,
                math.ceil(len(self.rows) / GAME_ROLES_PER_PAGE),
            )
            self.page = min(self.page, self.page_count - 1)
            self.rebuild_items()
            await interaction.response.edit_message(
                content=self.content("That game's Discord role no longer exists."),
                view=self,
            )
            return

        if me is None or not me.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "Kat needs **Manage Roles** before it can update game roles.",
                ephemeral=True,
            )
            return
        if role.managed or role >= me.top_role or role_has_privileged_permissions(role):
            await interaction.response.send_message(
                "Kat cannot safely assign that role. Move Kat above it and make sure "
                "the role has no staff or moderation permissions.",
                ephemeral=True,
            )
            return

        selected = role_id in self.selected_role_ids
        try:
            if selected:
                await member.remove_roles(
                    role,
                    reason="Member disabled a Kat game notification role",
                )
                self.selected_role_ids.discard(role_id)
                status = f"Removed {role.mention} for **{game_name}**."
            else:
                await member.add_roles(
                    role,
                    reason="Member enabled a Kat game notification role",
                )
                self.selected_role_ids.add(role_id)
                status = f"Added {role.mention} for **{game_name}**."
        except discord.Forbidden:
            await interaction.response.send_message(
                "Kat could not update that role. Keep **Manage Roles** enabled and "
                "move Kat's role above every game notification role.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            log.exception(
                "Discord rejected a game-role update for member %s and role %s",
                member.id,
                role_id,
            )
            await interaction.response.send_message(
                "Discord could not update that game role right now.",
                ephemeral=True,
            )
            return

        self.rebuild_items()
        await interaction.response.edit_message(
            content=self.content(status),
            view=self,
        )

    async def change_page(
        self,
        interaction: discord.Interaction,
        direction: int,
    ) -> None:
        self.page = max(0, min(self.page + direction, self.page_count - 1))
        self.rebuild_items()
        await interaction.response.edit_message(
            content=self.content(),
            view=self,
        )

    async def finish(self, interaction: discord.Interaction) -> None:
        selected_count = sum(
            1 for row in self.rows if int(row["role_id"]) in self.selected_role_ids
        )
        await interaction.response.edit_message(
            content=(
                f"✅ Saved. You currently have **{selected_count}** game notification "
                "role(s). Open the panel again whenever you want to change them."
            ),
            view=None,
        )
        self.stop()


class GameRolesPanelView(discord.ui.View):
    """Persistent public panel; each member opens a private paginated picker."""

    def __init__(self, bot: "KatBot") -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Choose Game Roles",
        style=discord.ButtonStyle.success,
        emoji="🎮",
        custom_id="kat:open_game_roles",
    )
    async def open_game_roles(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This panel only works inside a Discord server.",
                ephemeral=True,
            )
            return

        rows = await fetch_game_role_rows(self.bot.pool, guild.id)
        valid_rows = [
            row for row in rows if guild.get_role(int(row["role_id"])) is not None
        ]
        if not valid_rows:
            await interaction.response.send_message(
                "No game notification roles are configured yet. An administrator can "
                "add one with `/gameadmin add` or `/gameadmin create`.",
                ephemeral=True,
            )
            return

        picker = GameRolePickerView(self.bot, guild, member, valid_rows)
        await interaction.response.send_message(
            picker.content(),
            view=picker,
            ephemeral=True,
        )


def build_role_settings_panel_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="ROLES & SETTINGS",
        description=(
            "Manage your game alerts, nickname badge, and voice-level settings. "
            "Each option opens privately for the member using it."
        ),
        color=discord.Color.from_rgb(88, 101, 242),
    )
    embed.add_field(
        name="🎮 Game Alerts",
        value="Pick the game roles you want notifications for.",
        inline=False,
    )
    embed.add_field(
        name="💎 Badge Vault",
        value="Choose the emoji shown before your nickname.",
        inline=False,
    )
    embed.add_field(
        name="⚡ Auto Upgrade",
        value="Keep your badge matched to your newest unlock.",
        inline=True,
    )
    embed.add_field(
        name="🪪 Nickname Card",
        value="View your level, voice time, badge, and nickname.",
        inline=True,
    )
    embed.set_footer(text="Public panel • Private actions")
    if guild.icon is not None:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


def badge_mode_label(saved_selection: str) -> str:
    selection = (saved_selection or "").strip()
    if selection == "":
        return "Auto"
    if selection == NO_EMOJI_SELECTION:
        return "Hidden"
    return "Manual"


class BadgeVaultSelect(discord.ui.Select):
    def __init__(self, vault: "BadgeVaultView") -> None:
        self.vault = vault
        options: list[discord.SelectOption] = []
        current = vault.display_badge
        for required_level, emoji, _nickname_badge in vault.unlocked_badges[:25]:
            label = f"Level {required_level}"
            description = "Currently equipped" if emoji == current else "Unlocked badge"
            options.append(
                discord.SelectOption(
                    label=label,
                    value=emoji,
                    emoji=emoji,
                    description=description,
                    default=emoji == current,
                )
            )
        super().__init__(
            placeholder="Choose a nickname badge",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.values:
            await interaction.response.defer()
            return
        badge = self.values[0]
        await self.vault.set_selected_badge(interaction, badge)


class BadgeVaultAutoButton(discord.ui.Button):
    def __init__(self, vault: "BadgeVaultView") -> None:
        self.vault = vault
        enabled = (vault.saved_selection or "").strip() == ""
        super().__init__(
            label="Auto On" if enabled else "Use Auto",
            style=discord.ButtonStyle.primary if enabled else discord.ButtonStyle.secondary,
            emoji="⚡",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.vault.enable_auto(interaction)


class BadgeVaultHideButton(discord.ui.Button):
    def __init__(self, vault: "BadgeVaultView") -> None:
        self.vault = vault
        hidden = (vault.saved_selection or "").strip() == NO_EMOJI_SELECTION
        super().__init__(
            label="Hidden" if hidden else "Hide Badge",
            style=discord.ButtonStyle.danger if hidden else discord.ButtonStyle.secondary,
            emoji="🚫",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.vault.hide_badge(interaction)


class BadgeVaultView(discord.ui.View):
    def __init__(self, bot: "KatBot", guild: discord.Guild, member: discord.Member) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild = guild
        self.member = member
        self.total_seconds = 0
        self.level = 0
        self.unlocks: tuple[EmojiUnlock, ...] = ()
        self.unlocked_badges: list[EmojiUnlock] = []
        self.saved_selection = ""
        self.display_badge = ""
        self.level_locked = False

    async def refresh(self) -> None:
        row = await self.bot.get_or_create_stats(self.guild.id, self.member.id)
        self.total_seconds = int(row["total_voice_seconds"])
        self.level = level_from_total_seconds(self.total_seconds)
        self.unlocks = await self.bot.get_emoji_unlocks(self.guild.id)
        self.unlocked_badges = list(unlocked_emojis(self.level, self.unlocks))
        self.saved_selection = str(row["selected_emoji"] or "")
        self.level_locked = bool(row["level_locked"])
        self.display_badge = valid_selected_emoji(
            self.saved_selection,
            self.level,
            self.unlocks,
            ignore_level_requirement=self.level_locked,
        )
        self.clear_items()
        if self.unlocked_badges:
            self.add_item(BadgeVaultSelect(self))
        self.add_item(BadgeVaultAutoButton(self))
        self.add_item(BadgeVaultHideButton(self))

    def build_embed(self, *, note: str | None = None) -> discord.Embed:
        current = self.display_badge or "None"
        unlocked = " ".join(emoji for _lvl, emoji, _nickname_badge in self.unlocked_badges) or "None yet"
        next_unlock = next_emoji_unlock(self.level, self.unlocks)
        embed = discord.Embed(
            title="BADGE VAULT",
            description="Pick the emoji shown before your nickname.",
            color=discord.Color.from_rgb(87, 242, 135),
        )
        embed.add_field(name="Current", value=current, inline=True)
        embed.add_field(name="Mode", value=badge_mode_label(self.saved_selection), inline=True)
        embed.add_field(name="Level", value=f"{self.level}", inline=True)
        embed.add_field(name="Unlocked", value=unlocked, inline=False)
        if next_unlock is not None:
            embed.add_field(
                name="Next unlock",
                value=f"{next_unlock[1]} at **Level {next_unlock[0]}**",
                inline=False,
            )
        embed.add_field(
            name="Nickname Preview",
            value=f"`{nickname_with_level(self.member, self.level, self.saved_selection, self.unlocks, ignore_level_requirement=self.level_locked)}`",
            inline=False,
        )
        if note:
            embed.set_footer(text=note)
        else:
            embed.set_footer(text="Private menu • Choose a badge, use auto, or hide it")
        return embed

    async def set_selected_badge(self, interaction: discord.Interaction, badge: str) -> None:
        await self.bot.pool.execute(
            "UPDATE voice_levels SET selected_emoji = $1 WHERE guild_id = $2 AND user_id = $3;",
            badge,
            self.guild.id,
            self.member.id,
        )
        await self.bot.apply_level_nickname(self.member, self.level, badge)
        await self.bot.mark_nickname_synced(self.guild.id, self.member.id, self.level)
        await self.refresh()
        await interaction.response.edit_message(
            embed=self.build_embed(note=f"Equipped {badge}."),
            view=self,
        )

    async def enable_auto(self, interaction: discord.Interaction) -> None:
        await self.bot.pool.execute(
            "UPDATE voice_levels SET selected_emoji = $1 WHERE guild_id = $2 AND user_id = $3;",
            "",
            self.guild.id,
            self.member.id,
        )
        await self.bot.apply_level_nickname(self.member, self.level, "")
        await self.bot.mark_nickname_synced(self.guild.id, self.member.id, self.level)
        await self.refresh()
        await interaction.response.edit_message(
            embed=self.build_embed(note="Auto upgrade is now enabled."),
            view=self,
        )

    async def hide_badge(self, interaction: discord.Interaction) -> None:
        await self.bot.pool.execute(
            "UPDATE voice_levels SET selected_emoji = $1 WHERE guild_id = $2 AND user_id = $3;",
            NO_EMOJI_SELECTION,
            self.guild.id,
            self.member.id,
        )
        await self.bot.apply_level_nickname(self.member, self.level, NO_EMOJI_SELECTION)
        await self.bot.mark_nickname_synced(self.guild.id, self.member.id, self.level)
        await self.refresh()
        await interaction.response.edit_message(
            embed=self.build_embed(note="Your nickname badge is now hidden."),
            view=self,
        )


async def build_nickname_card_embed(bot: "KatBot", member: discord.Member) -> discord.Embed:
    row = await bot.get_or_create_stats(member.guild.id, member.id)
    total = int(row["total_voice_seconds"])
    level, progress, required = progress_for_total(total)
    remaining = max(0, required - progress)
    unlocks = await bot.get_emoji_unlocks(member.guild.id)
    saved_selection = str(row["selected_emoji"] or "")
    level_locked = bool(row["level_locked"])
    display_emoji = valid_selected_emoji(
        saved_selection,
        level,
        unlocks,
        ignore_level_requirement=level_locked,
    )
    next_unlock = next_emoji_unlock(level, unlocks)

    embed = discord.Embed(
        title="NICKNAME CARD",
        description="A quick look at your voice level nickname setup.",
        color=discord.Color.from_rgb(88, 101, 242),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Level", value=f"**{level}** `{superscript_number(level)}`", inline=True)
    embed.add_field(name="Voice Time", value=f"**{format_duration(total)}**", inline=True)
    embed.add_field(name="Badge", value=display_emoji or "None", inline=True)
    embed.add_field(
        name="Mode",
        value=badge_mode_label(saved_selection),
        inline=True,
    )
    embed.add_field(
        name="Current Nickname",
        value=f"`{nickname_with_level(member, level, saved_selection, unlocks, ignore_level_requirement=level_locked)}`",
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
    if next_unlock is not None:
        embed.add_field(
            name="Next unlock",
            value=f"{next_unlock[1]} at **Level {next_unlock[0]}**",
            inline=False,
        )
    embed.set_footer(text="Private card")
    return embed


class RoleSettingsPanelView(discord.ui.View):
    def __init__(self, bot: "KatBot") -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Game Roles",
        style=discord.ButtonStyle.success,
        emoji="🎮",
        custom_id="kat:role_settings_game_roles",
        row=0,
    )
    async def open_game_roles(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This panel only works inside a Discord server.",
                ephemeral=True,
            )
            return

        rows = await fetch_game_role_rows(self.bot.pool, guild.id)
        valid_rows = [row for row in rows if guild.get_role(int(row["role_id"])) is not None]
        if not valid_rows:
            await interaction.response.send_message(
                "No game notification roles are configured yet.",
                ephemeral=True,
            )
            return

        picker = GameRolePickerView(self.bot, guild, member, valid_rows)
        await interaction.response.send_message(
            picker.content(),
            view=picker,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Badge Vault",
        style=discord.ButtonStyle.success,
        emoji="💎",
        custom_id="kat:role_settings_badges",
        row=0,
    )
    async def open_badge_vault(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This panel only works inside a Discord server.",
                ephemeral=True,
            )
            return

        view = BadgeVaultView(self.bot, guild, member)
        await view.refresh()
        await interaction.response.send_message(
            embed=view.build_embed(),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Auto Upgrade",
        style=discord.ButtonStyle.primary,
        emoji="⚡",
        custom_id="kat:role_settings_auto",
        row=1,
    )
    async def toggle_auto_upgrade(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This panel only works inside a Discord server.",
                ephemeral=True,
            )
            return

        row = await self.bot.get_or_create_stats(guild.id, member.id)
        total = int(row["total_voice_seconds"])
        level = level_from_total_seconds(total)
        unlocks = await self.bot.get_emoji_unlocks(guild.id)
        saved_selection = str(row["selected_emoji"] or "")
        current_badge = valid_selected_emoji(
            saved_selection,
            level,
            unlocks,
            ignore_level_requirement=bool(row["level_locked"]),
        )

        if saved_selection.strip() == "":
            new_selection = current_badge or NO_EMOJI_SELECTION
            message = (
                f"Auto upgrade is now **off**. "
                f"{current_badge + ' has' if current_badge else 'No badge has'} been locked in."
            )
        else:
            new_selection = ""
            message = "Auto upgrade is now **on**. Your highest unlocked badge will be used."

        await self.bot.pool.execute(
            "UPDATE voice_levels SET selected_emoji = $1 WHERE guild_id = $2 AND user_id = $3;",
            new_selection,
            guild.id,
            member.id,
        )
        await self.bot.apply_level_nickname(member, level, new_selection)
        await self.bot.mark_nickname_synced(guild.id, member.id, level)
        await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(
        label="Nickname Card",
        style=discord.ButtonStyle.secondary,
        emoji="🪪",
        custom_id="kat:role_settings_card",
        row=1,
    )
    async def view_nickname_card(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This panel only works inside a Discord server.",
                ephemeral=True,
            )
            return

        embed = await build_nickname_card_embed(self.bot, member)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ChannelAlertView(discord.ui.View):
    """Buttons shown under each public @here game alert."""

    def __init__(
        self,
        *,
        voice_url: str | None = None,
        lfg_url: str | None = None,
    ) -> None:
        super().__init__(timeout=None)

        if voice_url:
            self.add_item(
                discord.ui.Button(
                    label="Join VC",
                    style=discord.ButtonStyle.link,
                    emoji="🔊",
                    url=voice_url,
                )
            )
        elif lfg_url:
            self.add_item(
                discord.ui.Button(
                    label="Open LFG",
                    style=discord.ButtonStyle.link,
                    emoji="🎮",
                    url=lfg_url,
                )
            )

        self.add_item(FollowPostButton())
        self.add_item(ChannelNotificationButton())


class KatBot(commands.Bot):
    pool: asyncpg.Pool

    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.presences = True
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

        # Keep public buttons active after Railway restarts.
        self.add_view(ChannelAlertView())
        self.add_view(GameRolesPanelView(self))
        self.add_view(RoleSettingsPanelView(self))

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

    async def cooldown_remaining(self, guild_id: int) -> int:
        if COOLDOWN_MINUTES <= 0:
            return 0

        last_ping = await self.pool.fetchval(
            "SELECT MAX(created_at) FROM katping_ping_log WHERE guild_id = $1;",
            guild_id,
        )
        if last_ping is None:
            return 0

        now = datetime.now(timezone.utc)
        elapsed = int((now - last_ping).total_seconds())
        cooldown_seconds = COOLDOWN_MINUTES * 60
        return max(0, cooldown_seconds - elapsed)

    async def log_ping(
        self,
        *,
        guild_id: int,
        inviter_id: int,
        context: LfgContext,
        game_name: str,
        delivered: int,
        failed: int,
        skipped: int,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO katping_ping_log (
                guild_id,
                inviter_id,
                channel_id,
                post_name,
                game_name,
                note,
                delivered_count,
                failed_count,
                skipped_count
            )
            VALUES ($1, $2, $3, $4, $5, '', $6, $7, $8);
            """,
            guild_id,
            inviter_id,
            context.channel_id,
            context.post_name,
            game_name,
            delivered,
            failed,
            skipped,
        )

    async def get_emoji_unlocks(
        self,
        guild_id: int,
        *,
        force_refresh: bool = False,
    ) -> tuple[EmojiUnlock, ...]:
        """Load Unicode unlocks and optionally bypass the in-memory cache."""
        if force_refresh:
            self.invalidate_emoji_unlocks(guild_id)

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

    async def replace_emoji_unlock(
        self,
        guild_id: int,
        old_emoji: str,
        new_emoji: str,
        required_level: int,
    ) -> tuple[bool, str]:
        old_emoji = old_emoji.strip()
        new_emoji = validate_display_emoji(new_emoji)
        if required_level < 0:
            raise ValueError("The required level cannot be negative.")

        exists = await self.pool.fetchval(
            "SELECT 1 FROM emoji_unlocks WHERE guild_id = $1 AND emoji = $2;",
            guild_id,
            old_emoji,
        )
        if not exists:
            return False, ""

        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM emoji_unlocks WHERE guild_id = $1 AND emoji = $2;",
                    guild_id,
                    old_emoji,
                )
                await connection.execute(
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
                    new_emoji,
                    required_level,
                )
                await connection.execute(
                    """
                    UPDATE voice_levels
                    SET selected_emoji = $1
                    WHERE guild_id = $2 AND selected_emoji = $3;
                    """,
                    new_emoji,
                    guild_id,
                    old_emoji,
                )

        self.invalidate_emoji_unlocks(guild_id)
        return True, new_emoji

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
    ) -> int:
        """Replace the full schedule and recalculate every saved member badge."""
        if not unlocks:
            raise ValueError("The emoji list must contain at least one entry.")

        reset_count = 0
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

                status = await connection.execute(
                    """
                    UPDATE voice_levels
                    SET selected_emoji = '',
                        nickname_level = -1
                    WHERE guild_id = $1;
                    """,
                    guild_id,
                )
                try:
                    reset_count = int(status.rsplit(" ", 1)[-1])
                except (TypeError, ValueError):
                    reset_count = 0

        self._emoji_unlock_cache[guild_id] = tuple(
            (required_level, emoji, emoji)
            for required_level, emoji in unlocks
        )
        return reset_count

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
        *,
        level_locked: bool | None = None,
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

        state_row: asyncpg.Record | None = None
        if selected_emoji is None or level_locked is None:
            state_row = await self.pool.fetchrow(
                "SELECT selected_emoji, level_locked FROM voice_levels "
                "WHERE guild_id = $1 AND user_id = $2;",
                guild.id,
                member.id,
            )
        if selected_emoji is None:
            selected_emoji = state_row["selected_emoji"] if state_row else ""
        if level_locked is None:
            level_locked = bool(state_row["level_locked"]) if state_row else False

        unlocks = await self.get_emoji_unlocks(guild.id)
        equipped_emoji = valid_selected_emoji(
            selected_emoji,
            level,
            unlocks,
            ignore_level_requirement=level_locked,
        )
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
            ignore_level_requirement=level_locked,
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
            SELECT user_id, total_voice_seconds, selected_emoji, level_locked
            FROM voice_levels
            WHERE guild_id = $1;
            """,
            guild.id,
        )
        stats = {
            row["user_id"]: (
                row["total_voice_seconds"],
                row["selected_emoji"],
                bool(row["level_locked"]),
            )
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

            total_seconds, selected_emoji, level_locked = stats.get(
                member.id,
                (0, "", False),
            )
            level = level_from_total_seconds(total_seconds)
            result = await self.apply_level_nickname(
                member,
                level,
                selected_emoji,
                level_locked=level_locked,
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


bot = KatBot()


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


def member_can_ping(member: discord.Member) -> bool:
    if member.guild_permissions.manage_guild or member.guild_permissions.manage_messages:
        return True
    if PINGER_ROLE_ID <= 0:
        return True
    return any(role.id == PINGER_ROLE_ID for role in member.roles)


def format_wait(seconds: int) -> str:
    minutes, secs = divmod(max(0, seconds), 60)
    if minutes and secs:
        return f"{minutes}m {secs}s"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def member_is_covered_by_here(
    member: discord.Member,
    channel: discord.TextChannel | discord.Thread,
) -> bool:
    """Best-effort check for whether Discord's @here should already alert a member."""
    if member.status == discord.Status.offline:
        return False
    return channel.permissions_for(member).view_channel


def build_game_dm_content(
    *,
    inviter: discord.Member,
    game_name: str,
    context: LfgContext,
    voice_channel: discord.VoiceChannel | None,
) -> str:
    safe_game = discord.utils.escape_markdown(game_name)
    safe_post = discord.utils.escape_markdown(context.post_name)

    if voice_channel is not None:
        voice_url = (
            f"https://discord.com/channels/{inviter.guild.id}/{voice_channel.id}"
        )
        safe_voice = discord.utils.escape_markdown(voice_channel.name)
        call_line = f"**Join them here:** [🔊 {safe_voice}]({voice_url})"
    else:
        call_line = "**Call location:** They are not in a voice channel yet."

    return (
        f"{inviter.mention} is inviting you to play **{safe_game}**!\n\n"
        f"{call_line}\n"
        f"**Game post:** [# {safe_post}]({context.jump_url})\n\n"
        "**NOTE:** Most people wait until they see activity in VC before joining. "
        "Be one of the first to hop in and help the lobby grow!\n"
        "To stop these DMs, remove this game's role from the Kat game-role panel."
    )


def build_game_dm_view(
    *,
    context: LfgContext,
    guild_id: int,
    voice_channel: discord.VoiceChannel | None,
) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if voice_channel is not None:
        view.add_item(
            discord.ui.Button(
                label="Join VC",
                style=discord.ButtonStyle.link,
                emoji="🔊",
                url=f"https://discord.com/channels/{guild_id}/{voice_channel.id}",
            )
        )
    view.add_item(
        discord.ui.Button(
            label="Open Game Post",
            style=discord.ButtonStyle.link,
            emoji="🎮",
            url=context.jump_url,
        )
    )
    return view


async def send_game_dm(
    member: discord.Member,
    *,
    inviter: discord.Member,
    game_name: str,
    context: LfgContext,
    voice_channel: discord.VoiceChannel | None,
) -> bool:
    try:
        await member.send(
            content=build_game_dm_content(
                inviter=inviter,
                game_name=game_name,
                context=context,
                voice_channel=voice_channel,
            ),
            view=build_game_dm_view(
                context=context,
                guild_id=inviter.guild.id,
                voice_channel=voice_channel,
            ),
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=[inviter],
                replied_user=False,
            ),
        )
        if DM_DELAY_SECONDS:
            await asyncio.sleep(DM_DELAY_SECONDS)
        return True
    except (discord.Forbidden, discord.NotFound):
        return False
    except discord.HTTPException:
        log.exception("Discord rejected a Kat game DM to user %s", member.id)
        return False


def build_alert_embed(
    *,
    guild: discord.Guild,
    inviter: discord.Member,
    context: LfgContext,
    voice_channel: discord.VoiceChannel | None,
) -> discord.Embed:
    safe_post = discord.utils.escape_markdown(context.post_name)
    safe_inviter = discord.utils.escape_markdown(inviter.display_name)
    destination = " in VC" if voice_channel is not None else ""

    embed = discord.Embed(
        title="🎮 Game alert!",
        url=context.jump_url,
        description=(
            f"**{safe_inviter}** started playing **{safe_post}** and is inviting "
            f"everyone here to join them{destination}!\n\n"
            f"**LFG post:** [Open {safe_post}]({context.jump_url})"
        ),
        color=discord.Color.from_rgb(87, 242, 135),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(
        name=f"{BOT_NAME} • {guild.name}",
        icon_url=inviter.display_avatar.url,
    )

    if voice_channel is not None:
        embed.add_field(
            name="Voice channel",
            value=(
                f"[🔊 {discord.utils.escape_markdown(voice_channel.name)}]"
                f"(https://discord.com/channels/{guild.id}/{voice_channel.id})"
            ),
            inline=True,
        )

    embed.add_field(
        name="Started by",
        value=inviter.mention,
        inline=True,
    )
    embed.set_footer(text="Use the buttons below to follow or mute this game channel")
    return embed


@bot.tree.command(
    name="ping",
    description="Ping @here and alert subscribers for the matching game.",
)
@app_commands.guild_only()
async def ping_command(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    channel = interaction.channel
    user = interaction.user

    if guild is None or channel is None or not isinstance(user, discord.Member):
        await interaction.response.send_message(
            "This command only works inside a Discord server.",
            ephemeral=True,
        )
        return

    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message(
            "Use `/ping` inside a text channel, forum post, or thread.",
            ephemeral=True,
        )
        return

    context = get_lfg_context(guild, channel)

    if not member_can_ping(user):
        await interaction.response.send_message(
            "You do not have permission to send Kat game alerts.",
            ephemeral=True,
        )
        return

    me = guild.me
    if me is None:
        await interaction.response.send_message(
            "Kat could not read its server permissions.",
            ephemeral=True,
        )
        return

    game_role, configured_game_name = await find_game_notification_role(
        bot.pool,
        guild,
        context.post_name,
    )

    if configured_game_name is not None and game_role is None:
        await interaction.response.send_message(
            (
                f"The game **{configured_game_name}** is configured, but its Discord "
                "role no longer exists. An admin needs to repair it with `/gameadmin`."
            ),
            ephemeral=True,
        )
        return

    if game_role is None:
        await interaction.response.send_message(
            (
                f"No game is configured for **{context.post_name}**. An admin must add "
                "that game with `/gameadmin create` or `/gameadmin add` before `/ping` "
                "can be used here."
            ),
            ephemeral=True,
        )
        return

    mention_permissions = channel.permissions_for(me)
    if not mention_permissions.mention_everyone:
        await interaction.response.send_message(
            "Kat needs **Mention @everyone, @here, and All Roles** in this channel "
            "before it can send the `@here` game alert.",
            ephemeral=True,
        )
        return

    remaining = await bot.cooldown_remaining(guild.id)
    if remaining > 0 and not user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            f"Kat game alerts are on cooldown for **{format_wait(remaining)}**.",
            ephemeral=True,
        )
        return

    game_name = configured_game_name or context.post_name.strip()

    voice_channel: discord.VoiceChannel | None = None
    if user.voice is not None and isinstance(user.voice.channel, discord.VoiceChannel):
        voice_channel = user.voice.channel

    voice_url = (
        f"https://discord.com/channels/{guild.id}/{voice_channel.id}"
        if voice_channel is not None
        else None
    )
    embed = build_alert_embed(
        guild=guild,
        inviter=user,
        context=context,
        voice_channel=voice_channel,
    )

    # Use only @here in the public post. The game role is used silently to choose
    # DM subscribers, preventing a member from receiving both an @here mention and
    # a second role mention in the same alert.
    await interaction.response.send_message(
        content="@here",
        embed=embed,
        view=ChannelAlertView(
            voice_url=voice_url,
            lfg_url=context.jump_url,
        ),
        allowed_mentions=discord.AllowedMentions(
            everyone=True,
            users=True,
            roles=False,
            replied_user=False,
        ),
    )

    opted_out_rows = await bot.pool.fetch(
        "SELECT user_id FROM katping_opt_outs WHERE guild_id = $1;",
        guild.id,
    )
    opted_out_ids = {int(row["user_id"]) for row in opted_out_rows}

    # De-duplicate by user ID. Members who are online and can see this channel are
    # already covered by @here, so only subscribers not covered by @here receive a DM.
    unique_role_members = {member.id: member for member in game_role.members}
    dm_recipients: list[discord.Member] = []
    skipped = 0
    covered_by_here = 0

    for member in unique_role_members.values():
        if member.bot or member.id == user.id or member.id in opted_out_ids:
            skipped += 1
            continue
        if member_is_covered_by_here(member, channel):
            covered_by_here += 1
            continue
        dm_recipients.append(member)

    if len(dm_recipients) > MAX_RECIPIENTS:
        skipped += len(dm_recipients) - MAX_RECIPIENTS
        dm_recipients = dm_recipients[:MAX_RECIPIENTS]

    delivered = 0
    failed = 0
    for member in dm_recipients:
        sent = await send_game_dm(
            member,
            inviter=user,
            game_name=game_name,
            context=context,
            voice_channel=voice_channel,
        )
        if sent:
            delivered += 1
        else:
            failed += 1

    await bot.log_ping(
        guild_id=guild.id,
        inviter_id=user.id,
        context=context,
        game_name=game_name,
        delivered=delivered,
        failed=failed,
        skipped=skipped + covered_by_here,
    )

    try:
        await interaction.followup.send(
            (
                f"Alert posted. **{covered_by_here}** subscribed member(s) were "
                f"already covered by `@here`; DM sent: **{delivered}**, failed: "
                f"**{failed}**, skipped: **{skipped}**."
            ),
            ephemeral=True,
        )
    except discord.HTTPException:
        log.exception("Could not send the private /ping delivery summary")


@ping_command.error
async def ping_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    original = getattr(error, "original", error)
    log.error(
        "/ping failed: %r",
        original,
        exc_info=(type(original), original, original.__traceback__),
    )
    message = "The Kat LFG alert failed. Check the Railway logs."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)




@bot.tree.command(
    name="rolesettingspanel",
    description="Post the public member role and nickname settings panel.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def role_settings_panel(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    embed = build_role_settings_panel_embed(interaction.guild)
    await interaction.response.send_message(
        embed=embed,
        view=RoleSettingsPanelView(bot),
    )


@role_settings_panel.error
async def role_settings_panel_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "Only administrators with **Manage Server** can post this panel."
    else:
        log.exception("/rolesettingspanel failed", exc_info=error)
        message = "The role settings panel could not be posted. Check the Railway logs."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)



game_admin_group = app_commands.Group(
    name="gameadmin",
    description="Administratively manage self-assignable game notification roles.",
)


async def validate_game_role_for_self_assignment(
    interaction: discord.Interaction,
    role: discord.Role,
) -> str | None:
    guild = interaction.guild
    if guild is None:
        return "This command only works inside a Discord server."
    me = guild.me
    if role.is_default():
        return "The `@everyone` role cannot be self-assigned."
    if role.managed:
        return "Integration-managed roles cannot be self-assigned."
    if role_has_privileged_permissions(role):
        return (
            "That role has moderation or management permissions. Game notification "
            "roles must be safe, non-staff roles."
        )
    if me is None or not me.guild_permissions.manage_roles:
        return "Kat needs **Manage Roles** before this role can be used."
    if role >= me.top_role:
        return "Move Kat's role above that game role first."
    return None


@game_admin_group.command(
    name="add",
    description="Connect an existing safe role to a game name.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    game="Game/post name that /ping should match",
    role="Existing notification role members can select",
    emoji="Unicode emoji shown in the selector",
)
async def game_admin_add(
    interaction: discord.Interaction,
    game: str,
    role: discord.Role,
    emoji: str = "🎮",
) -> None:
    assert interaction.guild is not None
    game_name = game.strip()
    game_key = normalize_game_key(game_name)
    emoji = emoji.strip() or "🎮"

    if not game_key or len(game_name) < 2 or len(game_name) > 80:
        await interaction.response.send_message(
            "The game name must be between 2 and 80 characters.",
            ephemeral=True,
        )
        return
    if len(emoji) > 8 or is_custom_emoji_code(emoji):
        await interaction.response.send_message(
            "Use one short standard Unicode emoji, such as `🎮`, `🔥`, or `🏆`.",
            ephemeral=True,
        )
        return

    role_error = await validate_game_role_for_self_assignment(interaction, role)
    if role_error:
        await interaction.response.send_message(role_error, ephemeral=True)
        return


    conflicting = await bot.pool.fetchrow(
        """
        SELECT game_name FROM game_notification_roles
        WHERE guild_id = $1 AND role_id = $2 AND game_key <> $3;
        """,
        interaction.guild.id,
        role.id,
        game_key,
    )
    if conflicting is not None:
        await interaction.response.send_message(
            f"{role.mention} is already connected to **{conflicting['game_name']}**. "
            "Use a different role or remove the old game first.",
            ephemeral=True,
        )
        return

    await bot.pool.execute(
        """
        INSERT INTO game_notification_roles (
            guild_id, game_key, game_name, role_id, emoji, created_by
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (guild_id, game_key)
        DO UPDATE SET
            game_name = EXCLUDED.game_name,
            role_id = EXCLUDED.role_id,
            emoji = EXCLUDED.emoji,
            updated_at = NOW();
        """,
        interaction.guild.id,
        game_key,
        game_name,
        role.id,
        emoji,
        interaction.user.id,
    )
    await interaction.response.send_message(
        f"Added **{emoji} {game_name}** using {role.mention}. `/ping` will mention "
        "that role when the current post name matches this game.",
        ephemeral=True,
    )


@game_admin_group.command(
    name="create",
    description="Create a new safe notification role and connect it to a game.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    game="Game/post name that /ping should match",
    emoji="Unicode emoji shown in the role and selector",
)
async def game_admin_create(
    interaction: discord.Interaction,
    game: str,
    emoji: str = "🎮",
) -> None:
    assert interaction.guild is not None
    game_name = game.strip()
    game_key = normalize_game_key(game_name)
    emoji = emoji.strip() or "🎮"

    if not game_key or len(game_name) < 2 or len(game_name) > 70:
        await interaction.response.send_message(
            "The game name must be between 2 and 70 characters.",
            ephemeral=True,
        )
        return
    if len(emoji) > 8 or is_custom_emoji_code(emoji):
        await interaction.response.send_message(
            "Use one short standard Unicode emoji, such as `🎮`, `🔥`, or `🏆`.",
            ephemeral=True,
        )
        return

    me = interaction.guild.me
    if me is None or not me.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "Kat needs **Manage Roles** before it can create game roles.",
            ephemeral=True,
        )
        return


    existing = await bot.pool.fetchval(
        "SELECT 1 FROM game_notification_roles WHERE guild_id = $1 AND game_key = $2;",
        interaction.guild.id,
        game_key,
    )
    if existing:
        await interaction.response.send_message(
            "That game is already configured. Use `/gameadmin add` to replace its role.",
            ephemeral=True,
        )
        return

    try:
        role = await interaction.guild.create_role(
            name=f"{emoji} {game_name}"[:100],
            permissions=discord.Permissions.none(),
            mentionable=False,
            reason=f"Kat game notification role created by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "Kat could not create the role. Give it **Manage Roles**.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        log.exception("Discord rejected creation of a game notification role")
        await interaction.response.send_message(
            "Discord could not create that role right now.",
            ephemeral=True,
        )
        return

    await bot.pool.execute(
        """
        INSERT INTO game_notification_roles (
            guild_id, game_key, game_name, role_id, emoji, created_by
        )
        VALUES ($1, $2, $3, $4, $5, $6);
        """,
        interaction.guild.id,
        game_key,
        game_name,
        role.id,
        emoji,
        interaction.user.id,
    )
    await interaction.response.send_message(
        f"Created {role.mention} and connected it to **{game_name}**.",
        ephemeral=True,
    )


@game_admin_group.command(
    name="remove",
    description="Remove a game from the selector and optionally delete its role.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    game="Configured game name",
    delete_role="Also delete the linked Discord role",
)
async def game_admin_remove(
    interaction: discord.Interaction,
    game: str,
    delete_role: bool = False,
) -> None:
    assert interaction.guild is not None
    game_key = normalize_game_key(game)
    row = await bot.pool.fetchrow(
        """
        DELETE FROM game_notification_roles
        WHERE guild_id = $1 AND game_key = $2
        RETURNING game_name, role_id;
        """,
        interaction.guild.id,
        game_key,
    )
    if row is None:
        await interaction.response.send_message(
            "That game is not configured.",
            ephemeral=True,
        )
        return

    role = interaction.guild.get_role(int(row["role_id"]))
    role_note = "The Discord role was kept."
    if delete_role and role is not None:
        try:
            await role.delete(reason=f"Kat game role removed by {interaction.user}")
            role_note = "The Discord role was also deleted."
        except (discord.Forbidden, discord.HTTPException):
            role_note = "Kat could not delete the Discord role, so it was kept."

    await interaction.response.send_message(
        f"Removed **{row['game_name']}** from the game-role panel. {role_note}",
        ephemeral=True,
    )


@game_admin_group.command(
    name="list",
    description="List every configured game notification role.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def game_admin_list(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    rows = await fetch_game_role_rows(bot.pool, interaction.guild.id)
    if not rows:
        await interaction.response.send_message(
            "No game notification roles are configured.",
            ephemeral=True,
        )
        return

    lines: list[str] = []
    for row in rows:
        role = interaction.guild.get_role(int(row["role_id"]))
        role_text = role.mention if role is not None else "`missing role`"
        lines.append(f"{row['emoji']} **{row['game_name']}** — {role_text}")

    embed = discord.Embed(
        title="Configured Game Notification Roles",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"{len(rows)} game role(s) configured")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@game_admin_group.error
async def game_admin_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "Only administrators with **Manage Server** can manage game roles."
    else:
        original = getattr(error, "original", error)
        log.error(
            "Game admin command failed: %r",
            original,
            exc_info=(type(original), original, original.__traceback__),
        )
        message = "The game-role command failed. Check the Railway logs."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


bot.tree.add_command(game_admin_group)


level_admin_group = app_commands.Group(
    name="leveladmin",
    description="Set and lock a member's voice level.",
)


async def level_admin_emoji_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest configured server badges for /leveladmin set."""
    if interaction.guild_id is None:
        return []
    try:
        unlocks = await bot.get_emoji_unlocks(interaction.guild_id)
    except Exception:
        log.exception("Level-admin emoji autocomplete failed")
        return []

    search = current.casefold().strip()
    choices: list[app_commands.Choice[str]] = []
    for required_level, configured, _nickname_badge in unlocks:
        label = f"{configured} — Level {required_level}"
        if search and search not in label.casefold():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=configured))
    return choices[:25]


@level_admin_group.command(
    name="set",
    description="Set and lock a member's level, with an optional nickname emoji.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    member="The member whose level should be fixed",
    level="The exact level to assign and lock",
    emoji=(
        "Optional configured emoji. Leave blank to use the highest badge unlocked "
        "at the assigned level"
    ),
)
@app_commands.autocomplete(emoji=level_admin_emoji_autocomplete)
async def level_admin_set(
    interaction: discord.Interaction,
    member: discord.Member,
    level: app_commands.Range[int, 0, 100000],
    emoji: str | None = None,
) -> None:
    assert interaction.guild is not None
    if member.bot:
        await interaction.response.send_message(
            "Bot accounts do not use the human voice-level system.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    fixed_level = int(level)
    fixed_seconds = cumulative_seconds_for_level(fixed_level)
    unlocks = await bot.get_emoji_unlocks(interaction.guild.id)

    requested_emoji = (emoji or "").strip()
    if requested_emoji:
        required_level = emoji_required_level(unlocks, requested_emoji)
        if required_level is None:
            await interaction.followup.send(
                "That emoji is not configured for this server. Choose one from the "
                "suggestions or check `/emojiadmin list`.",
                ephemeral=True,
            )
            return
        selected_emoji = requested_emoji
        badge_reason = "the emoji you selected"
    else:
        available = unlocked_emojis(fixed_level, unlocks)
        if available:
            selected_emoji = available[-1][1]
            badge_reason = "the highest emoji unlocked at that level"
        else:
            selected_emoji = NO_EMOJI_SELECTION
            badge_reason = "no emoji because none is unlocked at that level"

    await bot.pool.execute(
        """
        INSERT INTO voice_levels (
            guild_id,
            user_id,
            total_voice_seconds,
            level,
            nickname_level,
            selected_emoji,
            level_locked,
            last_active_at
        )
        VALUES ($1, $2, $3, $4, -1, $5, TRUE, NOW())
        ON CONFLICT (guild_id, user_id)
        DO UPDATE SET
            total_voice_seconds = EXCLUDED.total_voice_seconds,
            level = EXCLUDED.level,
            nickname_level = -1,
            selected_emoji = EXCLUDED.selected_emoji,
            level_locked = TRUE,
            last_active_at = NOW();
        """,
        interaction.guild.id,
        member.id,
        fixed_seconds,
        fixed_level,
        selected_emoji,
    )

    nickname_result = await bot.apply_level_nickname(
        member,
        fixed_level,
        selected_emoji,
        level_locked=True,
    )
    if nickname_result in {"updated", "unchanged"}:
        await bot.mark_nickname_synced(
            interaction.guild.id,
            member.id,
            fixed_level,
        )

    badge_text = selected_emoji if selected_emoji != NO_EMOJI_SELECTION else "None"
    message = (
        f"🔒 {member.mention} is now fixed at **Level {fixed_level}** "
        f"`{superscript_number(fixed_level)}`. Voice activity will not change it.\n"
        f"Emoji: **{badge_text}** — {badge_reason}."
    )
    if nickname_result not in {"updated", "unchanged"}:
        message += (
            "\nThe level and emoji were saved, but Kat could not update the nickname "
            "because of Discord nickname permissions or role hierarchy."
        )
    await interaction.followup.send(message, ephemeral=True)


@level_admin_group.command(
    name="unlock",
    description="Unlock a fixed member level so normal voice XP continues again.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(member="The member whose level should resume normal XP")
async def level_admin_unlock(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    assert interaction.guild is not None
    await bot.get_or_create_stats(interaction.guild.id, member.id)
    result = await bot.pool.execute(
        """
        UPDATE voice_levels
        SET level_locked = FALSE,
            nickname_level = -1
        WHERE guild_id = $1 AND user_id = $2 AND level_locked = TRUE;
        """,
        interaction.guild.id,
        member.id,
    )
    if result.endswith("0"):
        await interaction.response.send_message(
            f"{member.mention}'s level is already using normal voice XP.",
            ephemeral=True,
        )
        return

    row = await bot.get_or_create_stats(interaction.guild.id, member.id)
    level = level_from_total_seconds(int(row["total_voice_seconds"]))
    nickname_result = await bot.apply_level_nickname(
        member,
        level,
        row["selected_emoji"],
    )
    if nickname_result in {"updated", "unchanged"}:
        await bot.mark_nickname_synced(interaction.guild.id, member.id, level)

    await interaction.response.send_message(
        f"🔓 {member.mention}'s level is unlocked at **Level {level}**. "
        "Normal voice XP will continue from here.",
        ephemeral=True,
    )


@level_admin_group.command(
    name="status",
    description="Check whether a member's voice level is fixed or using normal XP.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(member="The member to check")
async def level_admin_status(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    assert interaction.guild is not None
    row = await bot.get_or_create_stats(interaction.guild.id, member.id)
    level = level_from_total_seconds(int(row["total_voice_seconds"]))
    locked = bool(row["level_locked"])
    await interaction.response.send_message(
        (
            f"{member.mention} is **Level {level}** `{superscript_number(level)}`.\n"
            + (
                "🔒 The level is fixed and will not change."
                if locked
                else "🔓 The level is using normal voice XP."
            )
        ),
        ephemeral=True,
    )


@level_admin_group.error
async def level_admin_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the **Manage Server** permission to manage fixed levels."
    else:
        original = getattr(error, "original", error)
        log.error(
            "Level admin command failed: %r",
            original,
            exc_info=(type(original), original, original.__traceback__),
        )
        message = "The level-admin command failed. Check the Railway logs."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


bot.tree.add_command(level_admin_group)


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
    selected_emoji = valid_selected_emoji(
        row["selected_emoji"],
        level,
        unlocks,
        ignore_level_requirement=bool(row["level_locked"]),
    )

    # Reapply the nickname whenever rank is checked so the displayed level stays current.
    nickname_result = await bot.apply_level_nickname(
        target,
        level,
        row["selected_emoji"],
        level_locked=bool(row["level_locked"]),
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
        value=f"`{nickname_with_level(target, level, row['selected_emoji'], unlocks, ignore_level_requirement=bool(row['level_locked']))}`",
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

    if bool(row["level_locked"]):
        embed.add_field(
            name="Level Status",
            value=(
                "🔒 **Fixed by an administrator**\n"
                "Voice activity will not increase or decrease this level."
            ),
            inline=False,
        )
        embed.set_footer(text="This member's voice level is locked.")
    else:
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
        SELECT user_id, total_voice_seconds, level_locked
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
        lock_marker = " 🔒" if bool(row["level_locked"]) else ""
        lines.append(
            f"{prefix} {name} — **Level {level}** `{superscript_number(level)}`"
            f"{lock_marker} · {format_duration(total)}"
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
    selected_emoji = valid_selected_emoji(
        row["selected_emoji"],
        level,
        unlocks,
        ignore_level_requirement=bool(row["level_locked"]),
    )
    result = await bot.apply_level_nickname(
        interaction.user,
        level,
        row["selected_emoji"],
        level_locked=bool(row["level_locked"]),
    )

    if result in {"updated", "unchanged"}:
        await bot.mark_nickname_synced(
            interaction.guild.id,
            interaction.user.id,
            level,
        )
        await interaction.response.send_message(
            f"Your nickname is now "
            f"`{nickname_with_level(interaction.user, level, row['selected_emoji'], unlocks, ignore_level_requirement=bool(row['level_locked']))}`.",
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
    selected_emoji = valid_selected_emoji(
        row["selected_emoji"],
        level,
        unlocks,
        ignore_level_requirement=bool(row["level_locked"]),
    )

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


class EmojiBulkEditModal(discord.ui.Modal):
    """Private popup used to edit every saved emoji unlock in one place."""

    def __init__(
        self,
        bot: "KatBot",
        guild: discord.Guild,
        current_block: str,
    ) -> None:
        super().__init__(title="Edit All Emoji Unlocks", timeout=600)
        self.bot = bot
        self.guild = guild
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
        if interaction.guild is None or interaction.guild.id != self.guild.id:
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
            unlocks = tuple(
                (required_level, validate_display_emoji(emoji))
                for required_level, emoji in parsed
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        if not interaction.guild.chunked:
            try:
                await interaction.guild.chunk(cache=True)
            except Exception:
                log.exception(
                    "Could not refresh the member cache before emoji bulk sync "
                    "for guild %s",
                    interaction.guild.id,
                )

        reset_count = await self.bot.replace_all_emoji_unlocks(
            interaction.guild.id,
            unlocks,
        )

        saved_unlocks = await self.bot.get_emoji_unlocks(
            interaction.guild.id,
            force_refresh=True,
        )
        expected_unlocks = tuple(
            (required_level, emoji, emoji)
            for required_level, emoji in unlocks
        )
        if saved_unlocks != expected_unlocks:
            log.error(
                "Emoji bulk-save verification failed for guild %s: expected=%r saved=%r",
                interaction.guild.id,
                expected_unlocks,
                saved_unlocks,
            )
            await interaction.followup.send(
                "The emoji list could not be verified after saving. Check the "
                "Railway logs and try again.",
                ephemeral=True,
            )
            return

        updated, unchanged, skipped = await self.bot.sync_guild_nicknames(
            interaction.guild
        )
        await interaction.followup.send(
            f"Saved and verified **{len(saved_unlocks)}** emoji unlock(s).\n"
            f"Recalculated **{reset_count}** saved member(s) using Auto Upgrade.\n"
            f"Nicknames updated: **{updated}**, already correct: **{unchanged}**, "
            f"skipped: **{skipped}**.",
            ephemeral=True,
        )


emoji_admin_group = app_commands.Group(
    name="emojiadmin",
    description="View or manage this server's level-unlocked nickname emojis.",
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
    name="replace",
    description="Replace an old Unicode emoji while preserving selections.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    old_emoji="The currently configured Unicode emoji",
    new_emoji="The new Unicode emoji",
    level="The new required level",
)
@app_commands.autocomplete(old_emoji=configured_emoji_autocomplete)
async def emoji_admin_replace(
    interaction: discord.Interaction,
    old_emoji: str,
    new_emoji: str,
    level: app_commands.Range[int, 0, 100000],
) -> None:
    assert interaction.guild is not None
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        changed, _saved_badge = await bot.replace_emoji_unlock(
            interaction.guild.id,
            old_emoji,
            new_emoji,
            int(level),
        )
    except ValueError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return

    if not changed:
        await interaction.followup.send(
            "That old emoji is not configured. Use `/emojiadmin list`.",
            ephemeral=True,
        )
        return

    updated, unchanged, skipped = await bot.sync_guild_nicknames(interaction.guild)
    await interaction.followup.send(
        f"Replaced {old_emoji} with {new_emoji} at **Level {level}**.\n"
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
            "You must keep at least one configured emoji. Use `/emojiadmin replace` "
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
    name="editall",
    description="Open one popup to copy, paste, and replace every emoji unlock.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def emoji_admin_editall(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    unlocks = await bot.get_emoji_unlocks(
        interaction.guild.id,
        force_refresh=True,
    )
    current_block = format_emoji_unlock_block(unlocks)
    if len(current_block) > 4000:
        await interaction.response.send_message(
            "There are too many configured entries to fit in Discord's editor. "
            "Remove a few entries first with `/emojiadmin remove`.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(
        EmojiBulkEditModal(bot, interaction.guild, current_block)
    )


@emoji_admin_group.command(
    name="list",
    description="List this server's configured emoji unlocks.",
)
@app_commands.guild_only()
async def emoji_admin_list(interaction: discord.Interaction) -> None:
    """This is the only /emojiadmin subcommand available to every member."""
    assert interaction.guild is not None
    unlocks = await bot.get_emoji_unlocks(
        interaction.guild.id,
        force_refresh=True,
    )
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
            "to copy, paste, and replace the full schedule."
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
