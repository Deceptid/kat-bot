# Discord Voice Level Bot

A `discord.py` bot that:

- Awards time for qualifying voice-channel activity.
- Makes each next level take longer.
- Adds a superscript level to the end of a member's server nickname, such as `Aidan ¹²`.
- Stores progress in Postgres.
- Provides `/rank`, `/leaderboard`, `/syncnickname`, and `/syncallnicknames`.

## How voice time qualifies

By default, a member earns time when:

1. They are in a voice or stage channel.
2. They are not a bot.
3. They are not self-deafened or server-deafened.
4. The channel contains at least two qualifying human members.
5. The channel is not the server AFK channel.

This tracks qualifying time in voice channels. It does **not** record audio or inspect what anyone says.

## Level formula

The time from level `L` to `L + 1` is:

```text
BASE_HOURS + (HOURS_PER_LEVEL × L)
```

Defaults:

- Level 0 → 1: 1 hour
- Level 1 → 2: 1 hour 15 minutes
- Level 5 → 6: 2 hours 15 minutes
- Level 10 → 11: 3 hours 30 minutes

Change `BASE_HOURS` and `HOURS_PER_LEVEL` in your hosting environment variables.

## Discord bot setup

1. Open the Discord Developer Portal and create a new application.
2. Open **Bot**, create the bot, and reset/copy its token.
3. Under **Privileged Gateway Intents**, enable **Server Members Intent**.
4. Open **OAuth2 → URL Generator**.
5. Select scopes:
   - `bot`
   - `applications.commands`
6. Select bot permissions:
   - View Channels
   - Send Messages
   - Embed Links
   - Manage Nicknames
7. Use the generated URL to invite the bot.
8. In **Server Settings → Roles**, drag the bot's role above every role whose nickname it should edit.

Do not give the bot Administrator unless you deliberately want to.

## Neon database setup

1. Create a free Neon project.
2. Open the project and click **Connect**.
3. Turn on **Connection pooling**.
4. Copy the connection string.
5. Save it as the `DATABASE_URL` environment variable.

The bot creates its table and index automatically on first startup.

## Railway hosting setup

1. Put these files in a GitHub repository.
2. In Railway, create a project and choose **Deploy from GitHub repo**.
3. Select your repository.
4. Open the service's **Variables** tab and add:
   - `DISCORD_TOKEN`
   - `DATABASE_URL`
   - `TEST_GUILD_ID` (optional but useful while testing)
   - the other settings from `.env.example`
5. Railway should detect Python. The included `railway.toml` starts the bot with `python bot.py`.
6. Open **Deployments / Logs** and look for `Logged in as ...`.

Never upload a real `.env` file or bot token to GitHub.

## Local test, optional

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python bot.py
```

## Important nickname limitation

Discord nicknames are limited to 32 characters. The bot shortens the base nickname when needed so the superscript level still fits. It cannot edit the server owner's nickname or anyone whose highest role is equal to or above the bot's highest role.
