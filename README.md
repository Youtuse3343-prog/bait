# Professional Discord Bot + Owner Dashboard

A complete Discord server-management bot with a private owner-only web dashboard.

## Included

- Tickets: configurable panel, private channels, support role access, claim, close, transcript logging.
- Announcements: slash command + dashboard send.
- Welcome system: channel messages, variables, optional DM.
- Auto-role on join.
- Discord OAuth verification: identity-only OAuth flow, remove one role + add another role after verification.
- Bot DMs: slash command + owner dashboard.
- Status rotation: configurable presence type/text and interval.
- Automatic recurring messages: per-server schedule and on/off feature switch.
- Moderation: warnings, warning history, clear warnings, purge, timeout/untimeout, kick, ban/unban, slowmode, lock/unlock.
- Logs / mod logs: joins, leaves, deleted/edited messages, role changes, nickname changes, moderation actions, tickets, verification.
- General commands: `/ping`, `/userinfo`, `/avatar`, `/serverinfo`, `/botinfo`, `/help`.
- Command cleanup: startup sync replaces stale global commands with the current tree; owner `/synccommands` supports guild/global sync.
- Per-server feature switches for tickets, announcements, welcome, autorole, verification, moderation, logs, auto-messages and bot DMs.
- MongoDB production persistence with SQLite fallback for local development.
- Owner-only dashboard secured by Discord OAuth + exact `OWNER_ID` match, OAuth state validation, CSRF protection, secure cookie options and security headers.

## Required Discord Developer Portal settings

1. Create a Discord application and bot.
2. Enable **Server Members Intent** and **Message Content Intent** in the Bot page.
3. Invite the bot with `bot` + `applications.commands` scopes.
4. Give it permissions needed for the enabled features: Manage Roles, Manage Channels, Manage Messages, Moderate Members, Kick Members, Ban Members, View Channels, Send Messages, Read Message History, Embed Links, Attach Files.
5. Add your Discord OAuth redirect URL exactly, preferably `https://your-domain.com/oauth/callback`. The app also accepts `/verify/callback` for compatibility.
6. Keep the bot role above any roles it must add/remove.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env        # Windows
# edit .env
python main.py
```

On macOS/Linux use `source .venv/bin/activate` and `cp .env.example .env`.

## Important `.env` values

- `DISCORD_TOKEN`: bot token.
- `DISCORD_CLIENT_ID`: application client ID.
- `DISCORD_CLIENT_SECRET`: application OAuth client secret.
- `OWNER_ID`: only this Discord account may use the dashboard.
- `OAUTH_REDIRECT_URI`: exact callback registered in Discord. Prefer `/oauth/callback`; `/verify/callback` is also supported.
- `DASHBOARD_BASE_URL`: public dashboard base URL; verification panel links use this.
- `SECRET_KEY`: long random secret. Example generator: `python -c "import secrets; print(secrets.token_hex(48))"`.
- `MONGODB_URI`: recommended on Render/production. Leave blank for local SQLite.
- `SESSION_COOKIE_SECURE=true`: set this in production behind HTTPS.
- `DEV_GUILD_ID`: optional testing server ID. If set, slash sync targets that guild instantly instead of global registration.

## First-use flow

1. Start the bot.
2. Open the dashboard and sign in with the same Discord account as `OWNER_ID`.
3. Select a server and configure channels + roles.
4. Save feature switches and messages.
5. Run `/ticketpanel` in a server to post the ticket panel.
6. Run `/verificationpanel` to post the Discord OAuth verification link.
7. If you previously had old slash commands, run `/synccommands global`; the current tree becomes the registered global command set.

## Production notes

- Use MongoDB on hosts with ephemeral disks.
- Use HTTPS and `SESSION_COOKIE_SECURE=true`.
- Never commit `.env` or expose `DISCORD_TOKEN`, `DISCORD_CLIENT_SECRET`, `SECRET_KEY`, or `MONGODB_URI`.
- Restrict dashboard ingress further at your reverse proxy/firewall if you want IP-level protection in addition to Discord owner authentication.
- The dashboard only allows a Discord user whose ID exactly matches `OWNER_ID`; other successful OAuth identities are rejected.

## Customization placeholders

Welcome/ticket messages support `{mention}`, `{user}`, `{user_id}`, `{server}`, `{server_id}`, and `{member_count}` where relevant.

## Dashboard self keep-alive

The owner dashboard now includes **Global → Dashboard keep-alive**. It can periodically request the bot's own `DASHBOARD_BASE_URL/health` endpoint and includes a **Ping now** test button.

1. Set `DASHBOARD_BASE_URL` to the public URL of the running service (for example `https://your-service.onrender.com`). Do not include `/health`.
2. Open **Dashboard → Global**.
3. Enable **Self-ping**, choose an interval (300 seconds / 5 minutes is a sensible default), and save.
4. Use **Ping now** to verify the public health URL returns HTTP 2xx.

The ping target is intentionally not editable in the browser; it is locked to this app's own `/health` route to avoid turning the dashboard into an arbitrary URL requester.

**Hosting note:** a self-ping thread only runs while your process is already running. If a hosting provider fully suspends/stops the process, the thread cannot wake itself. On hosts that suspend free services regardless of self-traffic, use the host's always-on plan or an external uptime monitor instead. The existing `render.yaml` also exposes `/health` as the Render health check path.
