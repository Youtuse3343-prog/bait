# moealturej Discord Bot — Production Build 3.2

A private, MongoDB-backed Discord operations bot with a web dashboard, secure OAuth verification, professional support tickets, moderation tools, live server statistics, message composers, and a complete provably-fair virtual-credit casino suite.

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

### Realistic blackjack and virtual economy

- `/blackjack bet:<amount>` with **Hit**, **Stand**, **Double**, **Split**, **Surrender**, and **Insurance**.
- Alternating initial deal, concealed dealer hole card, dealer blackjack checks, soft-hand logic, and split-ace restrictions.
- Multiple split hands are resolved independently with correct combined payouts and atomic MongoDB settlement.
- Three server-wide difficulty presets:
  - **Casual:** four decks, dealer stands on soft 17, 3:2 naturals, flexible doubles, surrender, and up to four hands.
  - **Casino:** six decks, dealer hits soft 17, 3:2 naturals, insurance, surrender, and up to three hands.
  - **Hard:** eight decks, dealer hits soft 17, 6:5 naturals, doubles limited to totals 9–11, no surrender, and two-hand split limit.
- A **Custom** preset exposes decks, blackjack payout, soft-17 behavior, hole-card peeking, insurance, surrender, splitting, split limits, and double-down restrictions.
- `/blackjack_rules` clearly shows the active table rules and fairness model.
- Cryptographically shuffled cards; the dealer follows fixed house rules and never adapts to a player's balance or future cards.
- Atomic additional wagers for splits, doubles, and insurance, plus automatic stale-session refunds after restarts or lost interactions.
- `/balance`, `/daily`, and `/casino_leaderboard` remain available.
- Virtual credits have no real-world cash value.

### Provably-fair casino suite

Every game uses the same server-specific virtual wallet, one-active-game lock, MongoDB settlement history, technical-error refunds, and configurable payout ceiling. Credits have no cash value.

- `/plinko` — 8, 10, or 12 rows; low, medium, or high risk; one to five balls; probability-balanced multiplier tables at approximately 96% theoretical RTP.
- `/mines` — interactive 20-tile board with 1–15 mines; cash-out values are calculated directly from combinations rather than arbitrary tables; untouched timeouts refund and active timeouts cash out safely.
- `/higher_lower` — a real shuffled 52-card deck with Ace low, King high, ties pushing, remaining-deck probabilities shown before every choice, and automatic timeout cash-out.
- `/slots` — three fixed-reel machines with low, medium, and high volatility; five equal paylines; published paytables; no result-generated near misses; approximately 96% theoretical RTP.
- `/roulette` — European single-zero wheel, standard straight/even-money/dozen/column payouts, and an exact 2.70% house edge.
- `/casino_rules` — publishes the active fairness model, RTP, payout ceiling, and economy protections.
- `/balance` now includes per-game activity and net results.

Fairness uses a SHA-256 commitment and an HMAC-SHA256 deterministic stream. Interactive games show the commitment before decisions and reveal the server seed after settlement. Whole-credit payouts use deterministic unbiased rounding from that same seed, preventing low wagers from suffering hidden truncation.

For MongoDB Atlas or another replica set, wallet reservation and settlement use multi-document transactions. Standalone MongoDB remains supported with compensating refunds and stale-session recovery, but a replica set is recommended for the strongest crash consistency.

### Dashboard and production operations

- Clean collapsible settings for brand, verification, welcome, tickets, logs, economy safeguards, every casino game, and blackjack rules.
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
7. Give the bot role these permissions:
   - Manage Roles
   - Manage Channels
   - Send Messages
   - Embed Links
   - Attach Files
   - Read Message History
8. Move the bot role above the Verified, Unverified, Auto, and support roles.
9. Set `SYNC_COMMANDS=true` for one deployment so Discord receives the new commands. After the log reports a successful sync, set it back to `false` and deploy once more.
10. Run `/setup_audit`, then send fresh verification and ticket panels.

## Important upgrade notes

- Existing MongoDB guild settings are migrated automatically by filling in missing defaults.
- Existing ticket and verification panel messages still respond because their persistent component IDs were preserved. Send new panels to display the customizable labels and improved copy.
- Existing wallet documents are not overwritten. New game statistics and defaults migrate lazily, and new users receive the configured starting balance when they first use a casino command.
- The dashboard remains owner-only by default. Set `DASHBOARD_OWNER_ONLY=false` to allow Discord server owners or members with **Manage Server** to access their own connected server configuration.

## Channel-name template variables

The ticket channel template supports:

- `{username}`
- `{type}`
- `{short_id}`

Example: `ticket-{username}-{short_id}`

## Welcome-message variables

- `{mention}`
- `{username}`
- `{user_id}`
- `{server}`
- `{server_id}`
- `{member_count}`

## Health endpoint

`GET /health`

The response includes build version, bot readiness, database readiness, latency, uptime, guild count, Discord circuit-breaker status, and startup retry details.

## Local validation

```bash
python -m pip install -r requirements.txt
python -m compileall -q .
python -m unittest discover -s tests -v
python bot.py
```

Never commit `.env`, bot tokens, OAuth secrets, MongoDB credentials, or the dashboard secret.
