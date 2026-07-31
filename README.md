# Kat — Combined Discord Bot

This project combines both uploaded bots into one `discord.py` application named **Kat**.

## Included features

### Voice leveling

- Tracks qualifying voice activity.
- Calculates levels using the existing level curve.
- Updates member nicknames with an emoji, `│`, and superscript level.
- Formats editable bot accounts as `🤖│BotName`.
- Keeps the existing emoji unlock and administration commands.
- Includes `/rank`, `/leaderboard`, `/syncnickname`, `/syncallnicknames`, `/emoji`, `/emojiadmin`, and `/levelstatus`.

### Kat LFG alerts

- `/ping` has no options.
- It copies the current forum post/thread name automatically.
- It DMs members who have the configured alert role.
- It includes a link back to the exact LFG post.
- It includes the inviter's voice channel when they are already in one.
- Members can mute or unmute alerts from that server using the button in the DM.
- Cooldowns and delivery history are stored in PostgreSQL.

## Discord application setup

1. Open the Discord Developer Portal and create one application named **Kat**.
2. Create its bot user and copy the token.
3. Enable **Server Members Intent** under the bot's privileged intents.
4. Install the bot with both scopes:
   - `bot`
   - `applications.commands`
5. Give Kat these permissions:
   - View Channels
   - Send Messages
   - Embed Links
   - Read Message History
   - Manage Nicknames
   - Change Nickname
6. Move Kat's role above members and bots whose nicknames it should edit.

The application name is changed in the Discord Developer Portal. `BOT_NAME=Kat` controls the name displayed in LFG alert messages.

## LFG setup

Create a role for members who should receive game-alert DMs, such as `LFG Alerts`.

Copy that role ID into `PING_ROLE_ID`.

For a forum channel, put the **forum channel ID** in `LFG_CHANNEL_IDS`. Kat accepts `/ping` inside posts created under that forum. For multiple channels, separate IDs with commas.

Example:

```env
PING_ROLE_ID=123456789012345678
LFG_CHANNEL_IDS=234567890123456789,345678901234567890
```

`PINGER_ROLE_ID=0` allows everyone to use `/ping`. Set it to a role ID to restrict the command to that role, Manage Server users, and Manage Messages users.

## Railway deployment

1. Upload this folder to a GitHub repository.
2. Create a Railway project and deploy the repository.
3. Add one PostgreSQL service.
4. Add the variables from `.env.example` to the Kat service.
5. Reference Railway PostgreSQL's `DATABASE_URL` from the Kat service.
6. Railway can use the included Dockerfile automatically. The manual start command is:

```text
python bot.py
```

### Recommended Railway variables

```env
DISCORD_TOKEN=your_new_kat_bot_token
DATABASE_URL=${{Postgres.DATABASE_URL}}
TEST_GUILD_ID=your_server_id

BOT_NAME=Kat
PING_ROLE_ID=your_lfg_alert_role_id
PINGER_ROLE_ID=0
LFG_CHANNEL_IDS=your_lfg_forum_or_channel_id
COOLDOWN_MINUTES=15
MAX_RECIPIENTS=500
DM_DELAY_SECONDS=0.20

LEVEL_UP_CHANNEL_ID=0
BASE_HOURS=1.0
HOURS_PER_LEVEL=0.25
TICK_SECONDS=60
MIN_HUMANS_IN_VC=1
REQUIRE_UNMUTED=false
SYNC_NICKNAMES_ON_START=true
```

## Keeping existing voice levels

Use the same `DATABASE_URL` that the voice-level bot already used. Kat will reuse the existing voice-level tables and automatically create the new KatPing tables.

When the two old bots used separate databases, one combined bot can connect to only one `DATABASE_URL` at a time. Using the voice-level database preserves levels; old KatPing delivery history from another database is not copied automatically.

## Testing

Inside an approved LFG forum post, run:

```text
/ping
```

The DM title and link will use that post's name automatically.

Then test:

```text
/rank
/levelstatus
/syncallnicknames
```
