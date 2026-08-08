# moealturej Discord Bot — Production Build 3.3

A private, MongoDB-backed Discord operations bot with a web dashboard, secure OAuth verification, professional support tickets, moderation tools, live server statistics, message composers, and a simple link to the separate moealturej web casino.

## Major systems

### Secure OAuth verification

- The OAuth account must exactly match the member who clicked **Verify**.
- Single-use states expire after 10 minutes.
- Configurable minimum Discord account age.
- Optional current-member requirement or OAuth server join.
- Role-hierarchy and permission checks before assignment.
- Idempotent re-verification and unverified-role cleanup.
- MongoDB verification audit history.

### Professional ticket system

- Category selector followed by a subject/details modal.
- One active ticket per member with a creation lock.
- Configurable labels, descriptions, support roles, panel text, and channel-name templates.
- Staff claim button and clear ownership metadata.
- HTML transcript delivery to the user and transcript channel.
- A ticket is not deleted if transcript generation fails.
- Ticket actions are written to the activity history.

### External casino link

- `/casino` sends `https://www.moealturej.com/casino` in a branded embed with an **Open Casino** link button.
- The Discord bot contains no casino games, credit wallet, daily rewards, betting logic, payouts, leaderboards, game sessions, or casino dashboard controls.
- Existing historical casino collections in MongoDB are left untouched for safe rollback or manual archival, but this build never reads or writes them.

### Dashboard and production operations

- Clean collapsible settings for brand, verification, welcome, tickets, and logs.
- Live setup audit for missing permissions, role hierarchy, and required channels.
- Announcement, custom embed, and direct-message composers.
- Security headers, cross-origin write protection, signed sessions, and request IDs.
- Friendly user errors with persistent internal incident IDs.
- Health output reports Discord and MongoDB readiness separately.
- Reusable HTTP session and conservative Discord API circuit breaker.

## Render deployment

1. Upload this project to the connected repository.
2. Create a **Web Service** using the included `Procfile`.
3. Copy every required value from `.env.example` into Render Environment Variables.
4. Set `PUBLIC_BASE_URL` to the exact HTTPS Render URL, with no trailing slash.
5. In the Discord Developer Portal, add these redirect URLs:
   - `https://YOUR-DOMAIN/oauth/callback`
   - `https://YOUR-DOMAIN/verify/callback`
6. Enable the **Server Members Intent** and **Message Content Intent** for ticket transcripts.
7. Give the bot role Manage Roles, Manage Channels, Send Messages, Embed Links, Attach Files, and Read Message History.
8. Move the bot role above the Verified, Unverified, Auto, and support roles.
9. Set `SYNC_COMMANDS=true` for one deployment so Discord removes the old game commands and registers `/casino`. After the log reports a successful sync, set it back to `false` and redeploy.
10. Run `/setup_audit`, then send fresh verification and ticket panels when needed.

## Important upgrade notes

- Old casino commands disappear only after a successful slash-command sync.
- Existing casino wallet/session/event collections are not deleted automatically.
- Existing MongoDB guild settings may still contain old casino keys; this build ignores them.
- The dashboard remains owner-only by default. Set `DASHBOARD_OWNER_ONLY=false` to allow Discord server owners or members with **Manage Server** to access their own connected server configuration.

## Local validation

```bash
python -m pip install -r requirements.txt
python -m compileall -q .
python -m unittest discover -s tests -v
python bot.py
```

Never commit `.env`, bot tokens, OAuth secrets, MongoDB credentials, or the dashboard secret.
