from __future__ import annotations

import asyncio
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

import discord
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError


Card = tuple[str, str]
EmbedFactory = Callable[[str, str, int], discord.Embed]
ErrorReporter = Callable[..., Awaitable[str]]

SUITS = ("♠", "♥", "♦", "♣")
RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
HIDDEN_CARD = "`??`"
SUCCESS = 0x22C55E
DANGER = 0xEF4444
WARNING = 0xF59E0B
ACCENT = 0x8B5CF6


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_amount(value: int, currency: str) -> str:
    return f"{value:,} {currency}"


def new_shoe(decks: int = 6) -> list[Card]:
    shoe = [(rank, suit) for _ in range(max(1, min(decks, 8))) for suit in SUITS for rank in RANKS]
    secrets.SystemRandom().shuffle(shoe)
    return shoe


def card_text(card: Card) -> str:
    rank, suit = card
    return f"`{rank}{suit}`"


def cards_text(cards: list[Card], *, hide_first: bool = False) -> str:
    if not cards:
        return "—"
    values = [card_text(card) for card in cards]
    if hide_first:
        values[0] = HIDDEN_CARD
    return " ".join(values)


def hand_value(cards: list[Card]) -> tuple[int, bool]:
    total = 0
    aces = 0
    for rank, _ in cards:
        if rank == "A":
            total += 11
            aces += 1
        elif rank in {"J", "Q", "K"}:
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total, aces > 0


def is_blackjack(cards: list[Card]) -> bool:
    return len(cards) == 2 and hand_value(cards)[0] == 21


@dataclass(slots=True)
class GameResult:
    key: str
    title: str
    detail: str
    payout: int
    color: int


class CasinoStore:
    """Atomic MongoDB operations used by the casino commands and views."""

    def __init__(self, db: Any):
        self.db = db

    async def ensure_wallet(self, guild_id: int, user_id: int, starting_balance: int) -> dict[str, Any]:
        now = utcnow()
        return await self.db.casino_wallets.find_one_and_update(
            {"guild_id": int(guild_id), "user_id": int(user_id)},
            {
                "$setOnInsert": {
                    "balance": int(starting_balance),
                    "wins": 0,
                    "losses": 0,
                    "pushes": 0,
                    "blackjacks": 0,
                    "wagered": 0,
                    "profit": 0,
                    "created_at": now,
                },
                "$set": {"updated_at": now},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
            projection={"_id": 0},
        )

    async def reserve_game(self, guild_id: int, user_id: int, bet: int, starting_balance: int) -> tuple[bool, str, Optional[dict[str, Any]]]:
        await self.ensure_wallet(guild_id, user_id, starting_balance)
        session_id = secrets.token_urlsafe(16)
        try:
            await self.db.casino_sessions.insert_one(
                {
                    "session_id": session_id,
                    "guild_id": int(guild_id),
                    "user_id": int(user_id),
                    "bet": int(bet),
                    "status": "active",
                    "created_at": utcnow(),
                    "expires_at": utcnow() + timedelta(minutes=20),
                }
            )
        except DuplicateKeyError:
            return False, "You already have an active blackjack table. Finish that game first.", None

        wallet = await self.db.casino_wallets.find_one_and_update(
            {"guild_id": int(guild_id), "user_id": int(user_id), "balance": {"$gte": int(bet)}},
            {"$inc": {"balance": -int(bet), "wagered": int(bet)}, "$set": {"updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
            projection={"_id": 0},
        )
        if not wallet:
            await self.db.casino_sessions.delete_one({"session_id": session_id})
            return False, "Your balance is too low for that bet.", None
        wallet["session_id"] = session_id
        return True, session_id, wallet

    async def reserve_double(self, session_id: str, guild_id: int, user_id: int, amount: int) -> bool:
        wallet = await self.db.casino_wallets.find_one_and_update(
            {"guild_id": int(guild_id), "user_id": int(user_id), "balance": {"$gte": int(amount)}},
            {"$inc": {"balance": -int(amount), "wagered": int(amount)}, "$set": {"updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
        if not wallet:
            return False
        updated = await self.db.casino_sessions.update_one(
            {"session_id": session_id, "status": "active"},
            {"$inc": {"bet": int(amount)}, "$set": {"updated_at": utcnow()}},
        )
        if updated.modified_count != 1:
            await self.db.casino_wallets.update_one(
                {"guild_id": int(guild_id), "user_id": int(user_id)},
                {"$inc": {"balance": int(amount), "wagered": -int(amount)}, "$set": {"updated_at": utcnow()}},
            )
            return False
        return True

    async def settle(self, session_id: str, guild_id: int, user_id: int, result: GameResult, bet: int) -> Optional[dict[str, Any]]:
        claimed = await self.db.casino_sessions.find_one_and_update(
            {"session_id": session_id, "status": "active"},
            {"$set": {"status": "settled", "result": result.key, "payout": int(result.payout), "settled_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
        if not claimed:
            return await self.db.casino_wallets.find_one({"guild_id": int(guild_id), "user_id": int(user_id)}, {"_id": 0})

        inc: dict[str, int] = {"balance": int(result.payout), "profit": int(result.payout - bet)}
        if result.key in {"win", "blackjack"}:
            inc["wins"] = 1
        if result.key == "blackjack":
            inc["blackjacks"] = 1
        elif result.key == "loss":
            inc["losses"] = 1
        elif result.key == "push":
            inc["pushes"] = 1

        wallet = await self.db.casino_wallets.find_one_and_update(
            {"guild_id": int(guild_id), "user_id": int(user_id)},
            {"$inc": inc, "$set": {"updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
            projection={"_id": 0},
        )
        await self.db.casino_sessions.delete_one({"session_id": session_id})
        await self.db.casino_events.insert_one(
            {
                "guild_id": int(guild_id),
                "user_id": int(user_id),
                "session_id": session_id,
                "event": "blackjack_settled",
                "result": result.key,
                "bet": int(bet),
                "payout": int(result.payout),
                "created_at": utcnow(),
            }
        )
        return wallet

    async def abandon(self, session_id: str, guild_id: int, user_id: int, bet: int, *, refund: bool) -> None:
        claimed = await self.db.casino_sessions.find_one_and_delete({"session_id": session_id, "status": "active"})
        if claimed and refund:
            await self.db.casino_wallets.update_one(
                {"guild_id": int(guild_id), "user_id": int(user_id)},
                {"$inc": {"balance": int(bet), "wagered": -int(bet)}, "$set": {"updated_at": utcnow()}},
            )


class BlackjackView(discord.ui.View):
    def __init__(
        self,
        *,
        store: CasinoStore,
        guild_id: int,
        user_id: int,
        display_name: str,
        session_id: str,
        bet: int,
        currency: str,
        balance_after_bet: int,
        embed_factory: EmbedFactory,
        report_error: ErrorReporter,
        dealer_hits_soft_17: bool = False,
    ) -> None:
        super().__init__(timeout=180)
        self.store = store
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        self.display_name = display_name
        self.session_id = session_id
        self.bet = int(bet)
        self.currency = currency
        self.balance = int(balance_after_bet)
        self.embed_factory = embed_factory
        self.report_error = report_error
        self.dealer_hits_soft_17 = dealer_hits_soft_17
        self.shoe = new_shoe()
        self.player: list[Card] = [self.shoe.pop(), self.shoe.pop()]
        self.dealer: list[Card] = [self.shoe.pop(), self.shoe.pop()]
        self.finished = False
        self.result: Optional[GameResult] = None
        self.message: Optional[discord.Message] = None
        self._lock = asyncio.Lock()

    def _draw(self) -> Card:
        if len(self.shoe) < 20:
            self.shoe = new_shoe()
        return self.shoe.pop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This blackjack table belongs to another player.", ephemeral=True)
            return False
        return True

    def disable_controls(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    def strip_button_emojis(self) -> None:
        """Remove decorative emojis so Discord can still render the controls."""
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.emoji = None

    @staticmethod
    def _is_invalid_component_emoji(exc: discord.HTTPException) -> bool:
        return getattr(exc, "code", None) == 50035 and "emoji" in str(exc).lower()

    async def open_table(self, interaction: discord.Interaction) -> None:
        """Open the table, retrying without decorative emojis if Discord rejects one."""
        try:
            await interaction.edit_original_response(content=None, embed=self.build_embed(), view=self)
        except discord.HTTPException as exc:
            if not self._is_invalid_component_emoji(exc):
                raise
            self.strip_button_emojis()
            await interaction.edit_original_response(content=None, embed=self.build_embed(), view=self)

    def build_embed(self, *, reveal_dealer: Optional[bool] = None, notice: str = "") -> discord.Embed:
        reveal = self.finished if reveal_dealer is None else reveal_dealer
        player_total, _ = hand_value(self.player)
        dealer_total, _ = hand_value(self.dealer)
        visible_dealer_total = dealer_total if reveal else hand_value(self.dealer[1:])[0]

        if self.result:
            description = self.result.detail
            color = self.result.color
            title = self.result.title
        else:
            description = "Choose **Hit**, **Stand**, **Double**, or **Surrender**. Dealer stands on 17."
            color = ACCENT
            title = "Blackjack Table"
        if notice:
            description = f"{notice}\n\n{description}"

        embed = self.embed_factory(title, description, color)
        embed.add_field(
            name=f"Dealer · {visible_dealer_total if reveal else f'{visible_dealer_total} + ?'}",
            value=cards_text(self.dealer, hide_first=not reveal),
            inline=False,
        )
        embed.add_field(name=f"{self.display_name} · {player_total}", value=cards_text(self.player), inline=False)
        embed.add_field(name="Bet", value=format_amount(self.bet, self.currency), inline=True)
        embed.add_field(name="Balance", value=format_amount(self.balance, self.currency), inline=True)
        embed.set_footer(text="Cards are shuffled with Python's cryptographic random source • Play responsibly")
        return embed

    async def resolve_initial(self) -> None:
        player_bj = is_blackjack(self.player)
        dealer_bj = is_blackjack(self.dealer)
        if not player_bj and not dealer_bj:
            return
        if player_bj and dealer_bj:
            await self._finish(GameResult("push", "Push", "Both you and the dealer have blackjack. Your bet was returned.", self.bet, WARNING))
        elif player_bj:
            payout = self.bet + math.floor(self.bet * 1.5)
            await self._finish(GameResult("blackjack", "Natural Blackjack!", "Blackjack pays **3:2**. Beautiful hand.", payout, SUCCESS))
        else:
            await self._finish(GameResult("loss", "Dealer Blackjack", "The dealer has a natural blackjack.", 0, DANGER))

    async def _dealer_play(self) -> None:
        while True:
            value, soft = hand_value(self.dealer)
            if value < 17 or (value == 17 and soft and self.dealer_hits_soft_17):
                self.dealer.append(self._draw())
                continue
            return

    async def _stand_and_resolve(self) -> None:
        await self._dealer_play()
        player_total, _ = hand_value(self.player)
        dealer_total, _ = hand_value(self.dealer)
        if player_total > 21:
            result = GameResult("loss", "Bust", f"You went over 21 with **{player_total}**.", 0, DANGER)
        elif dealer_total > 21:
            result = GameResult("win", "Dealer Bust", f"Dealer busted with **{dealer_total}**. You win!", self.bet * 2, SUCCESS)
        elif player_total > dealer_total:
            result = GameResult("win", "You Win", f"Your **{player_total}** beats the dealer's **{dealer_total}**.", self.bet * 2, SUCCESS)
        elif player_total < dealer_total:
            result = GameResult("loss", "Dealer Wins", f"Dealer's **{dealer_total}** beats your **{player_total}**.", 0, DANGER)
        else:
            result = GameResult("push", "Push", f"Both hands finished at **{player_total}**. Your bet was returned.", self.bet, WARNING)
        await self._finish(result)

    async def _finish(self, result: GameResult) -> None:
        if self.finished:
            return
        self.finished = True
        self.result = result
        self.disable_controls()
        wallet = await self.store.settle(self.session_id, self.guild_id, self.user_id, result, self.bet)
        if wallet:
            self.balance = int(wallet.get("balance", self.balance + result.payout))
        self.stop()

    async def _safe_edit(self, interaction: discord.Interaction, *, notice: str = "") -> None:
        embed = self.build_embed(notice=notice)

        async def apply_edit() -> None:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)

        try:
            await apply_edit()
        except discord.HTTPException as exc:
            if not self._is_invalid_component_emoji(exc):
                raise
            self.strip_button_emojis()
            await apply_edit()

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, emoji="🃏", custom_id="casino_blackjack_hit")
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self._lock:
            if self.finished:
                return await interaction.response.send_message("This table is already settled.", ephemeral=True)
            self.player.append(self._draw())
            total, _ = hand_value(self.player)
            if total >= 21:
                await self._stand_and_resolve()
            await self._safe_edit(interaction)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary, emoji="✋", custom_id="casino_blackjack_stand")
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self._lock:
            if self.finished:
                return await interaction.response.send_message("This table is already settled.", ephemeral=True)
            await self._stand_and_resolve()
            await self._safe_edit(interaction)

    @discord.ui.button(label="Double", style=discord.ButtonStyle.success, emoji="⚡", custom_id="casino_blackjack_double")
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self._lock:
            if self.finished:
                return await interaction.response.send_message("This table is already settled.", ephemeral=True)
            if len(self.player) != 2:
                return await interaction.response.send_message("You can only double on your first two cards.", ephemeral=True)
            original_bet = self.bet
            ok = await self.store.reserve_double(self.session_id, self.guild_id, self.user_id, original_bet)
            if not ok:
                return await interaction.response.send_message("You need enough balance to match your original bet.", ephemeral=True)
            self.bet += original_bet
            self.balance -= original_bet
            self.player.append(self._draw())
            await self._stand_and_resolve()
            await self._safe_edit(interaction, notice="**Double down:** one final card was drawn.")

    @discord.ui.button(label="Surrender", style=discord.ButtonStyle.danger, emoji="🏳️", custom_id="casino_blackjack_surrender")
    async def surrender(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self._lock:
            if self.finished:
                return await interaction.response.send_message("This table is already settled.", ephemeral=True)
            if len(self.player) != 2:
                return await interaction.response.send_message("Surrender is only available before you hit.", ephemeral=True)
            payout = self.bet // 2
            await self._finish(GameResult("loss", "Surrendered", f"Half your bet was returned: **{format_amount(payout, self.currency)}**.", payout, WARNING))
            await self._safe_edit(interaction)

    async def on_timeout(self) -> None:
        async with self._lock:
            if self.finished:
                return
            try:
                await self._stand_and_resolve()
                if self.message:
                    await self.message.edit(embed=self.build_embed(notice="**Timed out:** your hand was automatically stood."), view=self)
            except Exception as exc:  # pragma: no cover - Discord/network dependent
                try:
                    await self.store.abandon(self.session_id, self.guild_id, self.user_id, self.bet, refund=True)
                finally:
                    await self.report_error("blackjack_timeout", exc, guild_id=self.guild_id, user_id=self.user_id)
