# moealturej Discord Bot — Production Build 4.0

Private Discord operations bot and web control panel built with `discord.py`, `aiohttp`, and MongoDB. Build 4.0 overhauls the dashboard and bot presentation around the black/purple moealturej website theme, adds owner-level global control, per-server feature and command switches, safer message tools, and stronger production safeguards.

## What changed in 4.0

- Full dashboard visual overhaul matching the moealturej site: black surfaces, purple accent, compact navigation, responsive cards, cleaner forms, sticky save controls, and consistent preview components.
- New **Owner** control center for global branding, website links, presence rotation, maintenance mode, statistics interval, purge limits, and dashboard tools.
- New **Command Center** for enabling/disabling entire feature modules and individual slash commands per server.
- Per-server customization for branding, verification, welcome flow, support tickets, moderation behavior, log channels, live statistics, announcements, public command copy, and store buttons.
- Announcement/embed/DM composers now use server branding, validate image URLs, suppress mentions by default, and include live previews.
- Ticket transcripts redesigned to match the dashboard, with portable escaped HTML, message/attachment counts, responsive layout, and server brand color.
- Dashboard activity center combines recent bot errors, moderation actions, ticket activity, verification activity, and dashboard operations.
- Production hardening: signed sessions, signed double-submit CSRF protection, cross-origin write checks, security headers, no-store HTML caching, rate-limit guards, cooldowns, incident IDs, input limits, safe URL handling, and owner bypass protection against accidental lockout.
- Fixed a production-critical dashboard parser issue that could clamp real Discord snowflake IDs because Discord IDs exceed normal 32-bit/8-digit ranges.

## Dashboard areas

| Route | Purpose |
| --- | --- |
| `/` | Login / server overview / health metrics |
| `/owner` | Global owner-only configuration |
| `/guild/{guild_id}` | Complete server configuration |
| `/guild/{guild_id}/commands` | Feature modules, individual command switches, public command copy |
| `/guild/{guild_id}/announcements` | Announcement composer |
| `/guild/{guild_id}/embeds` | General embed composer |
| `/guild/{guild_id}/dms` | Controlled private DM composer |
| `/guild/{guild_id}/activity` | Operations, moderation, ticket, verification, and error history |
| `/status` | Branded HTML runtime status page |
| `/health` | JSON health/readiness endpoint |
| `/verify/start` + `/verify/callback` | OAuth verification flow |

The dashboard is owner-only by default. Set `DASHBOARD_OWNER_ONLY=false` if you want Discord server owners or members with **Manage Server** to manage only servers they are authorized for.

## Command groups

Build 4.0 exposes 32 slash commands across essentials, administration, verification, welcome, support tickets, announcements, live statistics, utilities, and moderation. Every command is represented in the Command Center and can be individually disabled without removing it from Discord.

The owner account can still use disabled commands so you cannot accidentally lock yourself out of recovery/configuration operations.

## Required environment variables

Copy `.env.example` to `.env` for local development. At minimum set:

- `BOT_TOKEN`
- `DISCORD_CLIENT_ID`
- `DISCORD_CLIENT_SECRET`
- `OWNER_USER_ID`
- `MONGO_URI`
- `DASHBOARD_SECRET` — use a long random value (32+ bytes recommended)
- `PUBLIC_BASE_URL` — exact public HTTPS origin in production, no trailing slash

Never commit `.env`, bot tokens, OAuth secrets, MongoDB credentials, or dashboard secrets.

## Discord Developer Portal

Add these OAuth redirect URLs using the same host as `PUBLIC_BASE_URL`:

- `https://YOUR-DOMAIN/oauth/callback`
- `https://YOUR-DOMAIN/verify/callback`

Enable **Server Members Intent**. Enable **Message Content Intent** if you want full ticket transcript message content.

Recommended bot permissions depend on enabled modules, but a full setup commonly needs Manage Roles, Manage Channels, Manage Messages, Moderate Members, Send Messages, Embed Links, Attach Files, Read Message History, and View Channels. Keep the bot role above every role it needs to assign/remove.

## Render deployment

1. Push the project to the repository connected to Render.
2. Create a **Web Service**. The included `Procfile` runs `python bot.py`.
3. Add values from `.env.example` as Render Environment Variables.
4. Set `PUBLIC_BASE_URL` to the exact HTTPS service/custom-domain URL.
5. Add the matching Discord OAuth redirect URLs.
6. Deploy with `SYNC_COMMANDS=true` once when command definitions change. This is required after this update so Discord removes any stale slash-command registrations that no longer exist in the code.
7. Confirm the logs report a successful slash-command sync, then set `SYNC_COMMANDS=false` and redeploy.
8. Open `/health`, then run `/setup_audit` in each configured Discord server.
9. Configure the remaining settings from `/owner` and each server's dashboard page.

The app starts its web health server before Discord login, so temporary Discord/Cloudflare login rate limits do not force a Render restart loop.

## Local development

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
# source .venv/bin/activate

python -m pip install -r requirements.txt
copy .env.example .env   # Windows
# cp .env.example .env   # macOS/Linux
python preflight.py
python -m compileall -q .
python bot.py
```

## Production notes

- Owner/global defaults are stored in MongoDB and apply immediately after saving.
- Existing guild and owner documents are migrated to the current schema: missing defaults are added and obsolete configuration keys are pruned automatically.
- Message tools intentionally suppress Discord mentions unless an announcement operator explicitly enables them for that send.
- Dashboard image URLs accept only complete `http://` or `https://` URLs.
- Ticket transcripts escape message/embed text before rendering to HTML.
- `/purge` is capped by the owner-configurable maximum, with a hard ceiling of 100.

See `CHANGELOG_4.0.md` for the upgrade breakdown and `preflight.py` for a safe environment/configuration check.
