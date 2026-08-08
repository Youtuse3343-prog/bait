from __future__ import annotations

import asyncio
import math
import secrets
from dataclasses import dataclass, field
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
    deck_count = max(1, min(int(decks), 8))
    shoe = [(rank, suit) for _ in range(deck_count) for suit in SUITS for rank in RANKS]
    secrets.SystemRandom().shuffle(shoe)
    return shoe


def card_text(card: Card) -> str:
    rank, suit = card
    return f"`{rank}{suit}`"


def cards_text(cards: list[Card], *, hide_index: Optional[int] = None) -> str:
    if not cards:
        return "—"
    values = [card_text(card) for card in cards]
    if hide_index is not None and 0 <= hide_index < len(values):
        values[hide_index] = HIDDEN_CARD
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


def card_game_value(card: Card) -> int:
    rank, _ = card
    if rank == "A":
        return 11
    if rank in {"10", "J", "Q", "K"}:
        return 10
    return int(rank)


def cards_can_split(cards: list[Card]) -> bool:
    return len(cards) == 2 and card_game_value(cards[0]) == card_game_value(cards[1])


@dataclass(frozen=True, slots=True)
class BlackjackRules:
    difficulty: str = "casino"
    decks: int = 6
    dealer_hits_soft_17: bool = True
    dealer_peeks: bool = True
    blackjack_payout_numerator: int = 3
    blackjack_payout_denominator: int = 2
    allow_insurance: bool = True
    allow_surrender: bool = True
    allow_split: bool = True
    max_split_hands: int = 3
    double_rule: str = "any"  # any, 9-11, 10-11

    @property
    def payout_label(self) -> str:
        return f"{self.blackjack_payout_numerator}:{self.blackjack_payout_denominator}"

    @property
    def double_label(self) -> str:
        return {"any": "any first two cards", "9-11": "hard/soft 9–11", "10-11": "hard/soft 10–11"}.get(
            self.double_rule, "any first two cards"
        )


def rules_for_difficulty(name: str, custom: Optional[dict[str, Any]] = None) -> BlackjackRules:
    difficulty = str(name or "hard").lower().strip()
    presets: dict[str, BlackjackRules] = {
        "casual": BlackjackRules(
            difficulty="casual",
            decks=4,
            dealer_hits_soft_17=False,
            dealer_peeks=True,
            blackjack_payout_numerator=3,
            blackjack_payout_denominator=2,
            allow_insurance=True,
            allow_surrender=True,
            allow_split=True,
            max_split_hands=4,
            double_rule="any",
        ),
        "casino": BlackjackRules(
            difficulty="casino",
            decks=6,
            dealer_hits_soft_17=True,
            dealer_peeks=True,
            blackjack_payout_numerator=3,
            blackjack_payout_denominator=2,
            allow_insurance=True,
            allow_surrender=True,
            allow_split=True,
            max_split_hands=3,
            double_rule="any",
        ),
        "hard": BlackjackRules(
            difficulty="hard",
            decks=8,
            dealer_hits_soft_17=True,
            dealer_peeks=True,
            blackjack_payout_numerator=6,
            blackjack_payout_denominator=5,
            allow_insurance=True,
            allow_surrender=False,
            allow_split=True,
            max_split_hands=2,
            double_rule="9-11",
        ),
    }
    if difficulty != "custom":
        return presets.get(difficulty, presets["hard"])

    data = custom or {}
    payout = str(data.get("payout", "3:2"))
    numerator, denominator = (6, 5) if payout == "6:5" else (3, 2)
    double_rule = str(data.get("double_rule", "any"))
    if double_rule not in {"any", "9-11", "10-11"}:
        double_rule = "any"
    return BlackjackRules(
        difficulty="custom",
        decks=max(1, min(int(data.get("decks", 6)), 8)),
        dealer_hits_soft_17=bool(data.get("dealer_hits_soft_17", True)),
        dealer_peeks=bool(data.get("dealer_peeks", True)),
        blackjack_payout_numerator=numerator,
        blackjack_payout_denominator=denominator,
        allow_insurance=bool(data.get("allow_insurance", True)),
        allow_surrender=bool(data.get("allow_surrender", True)),
        allow_split=bool(data.get("allow_split", True)),
        max_split_hands=max(2, min(int(data.get("max_split_hands", 3)), 4)),
        double_rule=double_rule,
    )


def dealer_should_hit(cards: list[Card], rules: BlackjackRules) -> bool:
    value, soft = hand_value(cards)
    return value < 17 or (value == 17 and soft and rules.dealer_hits_soft_17)


def can_double(cards: list[Card], rule: str) -> bool:
    if len(cards) != 2:
        return False
    total, _ = hand_value(cards)
    if rule == "9-11":
        return 9 <= total <= 11
    if rule == "10-11":
        return 10 <= total <= 11
    return True


def natural_blackjack_payout(bet: int, rules: BlackjackRules) -> int:
    profit = math.floor(int(bet) * rules.blackjack_payout_numerator / rules.blackjack_payout_denominator)
    return int(bet) + profit


@dataclass(slots=True)
class GameResult:
    key: str
    title: str
    detail: str
    payout: int
    color: int


@dataclass(slots=True)
class PlayerHand:
    cards: list[Card] = field(default_factory=list)
    bet: int = 0
    state: str = "active"  # active, stood, bust
    from_split: bool = False
    doubled: bool = False
    payout: int = 0
    outcome: str = ""


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

    async def reserve_game(
        self, guild_id: int, user_id: int, bet: int, starting_balance: int
    ) -> tuple[bool, str, Optional[dict[str, Any]]]:
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
                    "play_deadline": utcnow() + timedelta(minutes=10),
                    "expires_at": utcnow() + timedelta(days=7),
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

    async def reserve_additional_wager(self, session_id: str, guild_id: int, user_id: int, amount: int) -> bool:
        amount = int(amount)
        if amount <= 0:
            return False
        wallet = await self.db.casino_wallets.find_one_and_update(
            {"guild_id": int(guild_id), "user_id": int(user_id), "balance": {"$gte": amount}},
            {"$inc": {"balance": -amount, "wagered": amount}, "$set": {"updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
        if not wallet:
            return False
        updated = await self.db.casino_sessions.update_one(
            {"session_id": session_id, "status": "active"},
            {"$inc": {"bet": amount}, "$set": {"updated_at": utcnow(), "play_deadline": utcnow() + timedelta(minutes=10), "expires_at": utcnow() + timedelta(days=7)}},
        )
        if updated.modified_count != 1:
            await self.db.casino_wallets.update_one(
                {"guild_id": int(guild_id), "user_id": int(user_id)},
                {"$inc": {"balance": amount, "wagered": -amount}, "$set": {"updated_at": utcnow()}},
            )
            return False
        return True

    async def reserve_double(self, session_id: str, guild_id: int, user_id: int, amount: int) -> bool:
        return await self.reserve_additional_wager(session_id, guild_id, user_id, amount)

    async def settle(
        self, session_id: str, guild_id: int, user_id: int, result: GameResult, total_wager: int
    ) -> Optional[dict[str, Any]]:
        claimed = await self.db.casino_sessions.find_one_and_update(
            {"session_id": session_id, "status": "active"},
            {
                "$set": {
                    "status": "settled",
                    "result": result.key,
                    "payout": int(result.payout),
                    "total_wager": int(total_wager),
                    "settled_at": utcnow(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if not claimed:
            return await self.db.casino_wallets.find_one(
                {"guild_id": int(guild_id), "user_id": int(user_id)}, {"_id": 0}
            )

        inc: dict[str, int] = {"balance": int(result.payout), "profit": int(result.payout - total_wager)}
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
                "wager": int(total_wager),
                "payout": int(result.payout),
                "created_at": utcnow(),
            }
        )
        return wallet

    async def abandon(self, session_id: str, guild_id: int, user_id: int, total_wager: int, *, refund: bool) -> None:
        claimed = await self.db.casino_sessions.find_one_and_delete({"session_id": session_id, "status": "active"})
        if claimed and refund:
            actual_wager = int(claimed.get("bet", total_wager))
            await self.db.casino_wallets.update_one(
                {"guild_id": int(guild_id), "user_id": int(user_id)},
                {"$inc": {"balance": actual_wager, "wagered": -actual_wager}, "$set": {"updated_at": utcnow()}},
            )

    async def refund_stale_sessions(self) -> int:
        """Atomically refund abandoned tables after a restart or lost interaction."""
        now = utcnow()
        legacy_cutoff = now - timedelta(minutes=8)
        cursor = self.db.casino_sessions.find(
            {
                "status": "active",
                "$or": [
                    {"play_deadline": {"$lte": now}},
                    {"play_deadline": {"$exists": False}, "created_at": {"$lte": legacy_cutoff}},
                ],
            },
            {"_id": 0},
        ).limit(200)
        refunded = 0
        async for session in cursor:
            claimed = await self.db.casino_sessions.find_one_and_delete(
                {"session_id": session.get("session_id"), "status": "active"}
            )
            if not claimed:
                continue
            wager = max(0, int(claimed.get("bet", 0)))
            if wager:
                await self.db.casino_wallets.update_one(
                    {"guild_id": int(claimed["guild_id"]), "user_id": int(claimed["user_id"])},
                    {"$inc": {"balance": wager, "wagered": -wager}, "$set": {"updated_at": now}},
                )
            await self.db.casino_events.insert_one(
                {
                    "guild_id": int(claimed["guild_id"]),
                    "user_id": int(claimed["user_id"]),
                    "session_id": claimed.get("session_id"),
                    "event": "blackjack_stale_refund",
                    "wager": wager,
                    "created_at": now,
                }
            )
            refunded += 1
        return refunded


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
        rules: Optional[BlackjackRules] = None,
        dealer_hits_soft_17: Optional[bool] = None,
    ) -> None:
        super().__init__(timeout=210)
        self.store = store
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        self.display_name = display_name
        self.session_id = session_id
        self.original_bet = int(bet)
        self.total_wager = int(bet)
        self.currency = currency
        self.balance = int(balance_after_bet)
        self.embed_factory = embed_factory
        self.report_error = report_error
        self.rules = rules or rules_for_difficulty("casino")
        if dealer_hits_soft_17 is not None:
            self.rules = BlackjackRules(
                difficulty=self.rules.difficulty,
                decks=self.rules.decks,
                dealer_hits_soft_17=bool(dealer_hits_soft_17),
                dealer_peeks=self.rules.dealer_peeks,
                blackjack_payout_numerator=self.rules.blackjack_payout_numerator,
                blackjack_payout_denominator=self.rules.blackjack_payout_denominator,
                allow_insurance=self.rules.allow_insurance,
                allow_surrender=self.rules.allow_surrender,
                allow_split=self.rules.allow_split,
                max_split_hands=self.rules.max_split_hands,
                double_rule=self.rules.double_rule,
            )

        self.shoe = new_shoe(self.rules.decks)
        player_first = self._draw()
        dealer_up = self._draw()
        player_second = self._draw()
        dealer_hole = self._draw()
        self.hands: list[PlayerHand] = [PlayerHand(cards=[player_first, player_second], bet=self.original_bet)]
        self.dealer: list[Card] = [dealer_up, dealer_hole]
        self.current_hand = 0
        self.insurance_pending = False
        self.insurance_wager = 0
        self.insurance_outcome = ""
        self.dealer_actions: list[str] = []
        self.finished = False
        self.result: Optional[GameResult] = None
        self.message: Optional[discord.Message] = None
        self._lock = asyncio.Lock()
        self._last_notice = ""
        self._refresh_controls()

    @property
    def player(self) -> list[Card]:
        """Compatibility alias for older tests/extensions."""
        return self.hands[0].cards

    @property
    def bet(self) -> int:
        """Compatibility alias representing all currently reserved wagers."""
        return self.total_wager

    def _draw(self) -> Card:
        if len(self.shoe) < 52:
            self.shoe = new_shoe(self.rules.decks)
        return self.shoe.pop()

    def _button(self, custom_id: str) -> Optional[discord.ui.Button]:
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id == custom_id:
                return item
        return None

    def _active_hand(self) -> Optional[PlayerHand]:
        if self.finished or not (0 <= self.current_hand < len(self.hands)):
            return None
        hand = self.hands[self.current_hand]
        return hand if hand.state == "active" else None

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

    def _refresh_controls(self) -> None:
        if self.finished:
            self.disable_controls()
            return
        hand = self._active_hand()
        for custom_id in (
            "casino_blackjack_hit",
            "casino_blackjack_stand",
            "casino_blackjack_double",
            "casino_blackjack_split",
            "casino_blackjack_surrender",
            "casino_blackjack_insurance",
        ):
            button = self._button(custom_id)
            if button:
                button.disabled = True

        if not hand:
            return
        hit = self._button("casino_blackjack_hit")
        stand = self._button("casino_blackjack_stand")
        double = self._button("casino_blackjack_double")
        split = self._button("casino_blackjack_split")
        surrender = self._button("casino_blackjack_surrender")
        insurance = self._button("casino_blackjack_insurance")
        if hit:
            hit.disabled = False
        if stand:
            stand.disabled = False
        if double:
            double.disabled = not (can_double(hand.cards, self.rules.double_rule) and self.balance >= hand.bet)
        if split:
            split.disabled = not (
                self.rules.allow_split
                and len(self.hands) < self.rules.max_split_hands
                and cards_can_split(hand.cards)
                and self.balance >= hand.bet
            )
        if surrender:
            surrender.disabled = not (
                self.rules.allow_surrender
                and len(self.hands) == 1
                and len(hand.cards) == 2
                and not hand.from_split
            )
        if insurance:
            insurance.disabled = not (
                self.insurance_pending and self.rules.allow_insurance and self.original_bet >= 2 and self.balance >= self.original_bet // 2
            )
            insurance.label = "Insurance" if not is_blackjack(self.player) else "Even Money"

    async def open_table(self, interaction: discord.Interaction) -> None:
        """Open the table, retrying without decorative emojis if Discord rejects one."""
        self._refresh_controls()
        try:
            await interaction.edit_original_response(content=None, embed=self.build_embed(), view=self)
        except discord.HTTPException as exc:
            if not self._is_invalid_component_emoji(exc):
                raise
            self.strip_button_emojis()
            await interaction.edit_original_response(content=None, embed=self.build_embed(), view=self)

    def _rules_summary(self) -> str:
        soft_rule = "H17" if self.rules.dealer_hits_soft_17 else "S17"
        surrender = "late surrender" if self.rules.allow_surrender else "no surrender"
        split = f"split to {self.rules.max_split_hands}" if self.rules.allow_split else "no splits"
        return (
            f"**{self.rules.difficulty.title()}** • {self.rules.decks}-deck shoe • {soft_rule} • "
            f"BJ {self.rules.payout_label} • double {self.rules.double_label} • {split} • {surrender}"
        )

    def build_embed(self, *, reveal_dealer: Optional[bool] = None, notice: str = "") -> discord.Embed:
        reveal = self.finished if reveal_dealer is None else reveal_dealer
        dealer_total, _ = hand_value(self.dealer)
        visible_dealer_total = dealer_total if reveal else hand_value([self.dealer[0]])[0]

        if self.result:
            description = self.result.detail
            color = self.result.color
            title = self.result.title
        else:
            current = f"Hand {self.current_hand + 1} of {len(self.hands)}" if len(self.hands) > 1 else "Your move"
            description = f"{current}. The dealer follows the displayed house rules exactly—no hidden adaptive cheating."
            if self.insurance_pending:
                description = "Dealer shows an **Ace**. Take insurance, or choose a normal action to decline it."
            color = ACCENT
            title = "Blackjack Table"
        effective_notice = notice or self._last_notice
        if effective_notice:
            description = f"{effective_notice}\n\n{description}"

        embed = self.embed_factory(title, description, color)
        embed.add_field(
            name=f"Dealer · {dealer_total if reveal else f'{visible_dealer_total} + ?'}",
            value=cards_text(self.dealer, hide_index=None if reveal else 1),
            inline=False,
        )
        for index, hand in enumerate(self.hands):
            total, soft = hand_value(hand.cards)
            marker = "▶ " if not self.finished and index == self.current_hand and hand.state == "active" else ""
            state = {
                "active": "Playing",
                "stood": "Stood",
                "bust": "Bust",
            }.get(hand.state, hand.state.title())
            softness = " soft" if soft and total <= 21 else ""
            outcome = f" • {hand.outcome}" if hand.outcome else ""
            embed.add_field(
                name=f"{marker}{self.display_name} · Hand {index + 1} · {total}{softness} · {state}{outcome}",
                value=f"{cards_text(hand.cards)}\nWager: **{format_amount(hand.bet, self.currency)}**",
                inline=False,
            )
        embed.add_field(name="Total wager", value=format_amount(self.total_wager, self.currency), inline=True)
        embed.add_field(name="Available balance", value=format_amount(self.balance, self.currency), inline=True)
        if self.insurance_wager:
            embed.add_field(
                name="Insurance",
                value=f"{format_amount(self.insurance_wager, self.currency)}{f' • {self.insurance_outcome}' if self.insurance_outcome else ''}",
                inline=True,
            )
        embed.add_field(name="House rules", value=self._rules_summary(), inline=False)
        embed.set_footer(text="Cryptographically shuffled virtual cards • Credits have no cash value • Play responsibly")
        return embed

    async def resolve_initial(self) -> None:
        player_bj = is_blackjack(self.player)
        dealer_bj = is_blackjack(self.dealer)
        up_value = card_game_value(self.dealer[0])

        if player_bj and dealer_bj:
            await self._finish(
                GameResult("push", "Push", "Both you and the dealer have natural blackjack. Your wager was returned.", self.original_bet, WARNING)
            )
            return
        if player_bj:
            payout = natural_blackjack_payout(self.original_bet, self.rules)
            await self._finish(
                GameResult(
                    "blackjack",
                    "Natural Blackjack!",
                    f"Your natural pays **{self.rules.payout_label}** for **{format_amount(payout, self.currency)}** total.",
                    payout,
                    SUCCESS,
                )
            )
            return

        if up_value == 11 and self.rules.allow_insurance:
            self.insurance_pending = True
            self._last_notice = "**Insurance offered:** up to half your original wager, paying 2:1 if the dealer has blackjack."
            self._refresh_controls()
            return

        if self.rules.dealer_peeks and up_value in {10, 11} and dealer_bj:
            await self._finish(GameResult("loss", "Dealer Blackjack", "The dealer checked the hole card and has a natural blackjack.", 0, DANGER))

    async def _dealer_play(self) -> None:
        self.dealer_actions = []
        reveal_total, reveal_soft = hand_value(self.dealer)
        reveal_label = f"soft {reveal_total}" if reveal_soft else str(reveal_total)
        self.dealer_actions.append(f"Dealer reveals the hole card: **{reveal_label}**.")
        while dealer_should_hit(self.dealer, self.rules):
            before, soft = hand_value(self.dealer)
            reason = "soft 17 rule" if before == 17 and soft else f"total {before}"
            card = self._draw()
            self.dealer.append(card)
            after, after_soft = hand_value(self.dealer)
            after_label = f"soft {after}" if after_soft and after <= 21 else str(after)
            self.dealer_actions.append(f"Dealer hits on {reason}, draws {card_text(card)}, and reaches **{after_label}**.")
        final, final_soft = hand_value(self.dealer)
        if final > 21:
            self.dealer_actions.append(f"Dealer busts at **{final}**.")
        else:
            final_label = f"soft {final}" if final_soft else str(final)
            self.dealer_actions.append(f"Dealer stands on **{final_label}**.")

    async def _decline_insurance_and_peek(self) -> bool:
        if not self.insurance_pending:
            return True
        self.insurance_pending = False
        self._last_notice = "**Insurance declined.**"
        if self.rules.dealer_peeks and is_blackjack(self.dealer):
            await self._finish(GameResult("loss", "Dealer Blackjack", "The dealer checked under the Ace and has blackjack.", 0, DANGER))
            return False
        self._refresh_controls()
        return True

    async def _advance_or_resolve(self) -> None:
        for index in range(self.current_hand + 1, len(self.hands)):
            if self.hands[index].state == "active":
                self.current_hand = index
                self._last_notice = f"**Hand {index + 1}:** choose your action."
                self._refresh_controls()
                return
        await self._resolve_all_hands()

    async def _resolve_all_hands(self) -> None:
        live_hands = [hand for hand in self.hands if hand.state != "bust"]
        if live_hands:
            await self._dealer_play()
        dealer_total, _ = hand_value(self.dealer)
        dealer_bj = is_blackjack(self.dealer)
        payout = 0
        lines: list[str] = []

        for index, hand in enumerate(self.hands, start=1):
            player_total, _ = hand_value(hand.cards)
            if hand.state == "bust" or player_total > 21:
                hand.payout = 0
                hand.outcome = "Lost"
                reason = f"busted at {player_total}"
            elif dealer_bj:
                hand.payout = 0
                hand.outcome = "Lost"
                reason = "dealer blackjack"
            elif dealer_total > 21:
                hand.payout = hand.bet * 2
                hand.outcome = "Won"
                reason = f"dealer busted at {dealer_total}"
            elif player_total > dealer_total:
                hand.payout = hand.bet * 2
                hand.outcome = "Won"
                reason = f"{player_total} beat {dealer_total}"
            elif player_total < dealer_total:
                hand.payout = 0
                hand.outcome = "Lost"
                reason = f"dealer {dealer_total} beat {player_total}"
            else:
                hand.payout = hand.bet
                hand.outcome = "Push"
                reason = f"both finished at {player_total}"
            payout += hand.payout
            lines.append(f"**Hand {index}:** {hand.outcome} — {reason} • returned {format_amount(hand.payout, self.currency)}")

        if self.dealer_actions:
            lines.insert(0, "\n".join(self.dealer_actions))

        if self.insurance_wager:
            if dealer_bj:
                insurance_return = self.insurance_wager * 3
                payout += insurance_return
                self.insurance_outcome = f"Won {format_amount(insurance_return, self.currency)}"
                lines.append(f"**Insurance:** won • returned {format_amount(insurance_return, self.currency)}")
            else:
                if not self.insurance_outcome:
                    self.insurance_outcome = "Lost"
                lines.append("**Insurance:** lost")

        if payout > self.total_wager:
            key, title, color = "win", "Table Win", SUCCESS
        elif payout < self.total_wager:
            key, title, color = "loss", "Dealer Wins", DANGER
        else:
            key, title, color = "push", "Table Push", WARNING
        net = payout - self.total_wager
        net_text = f"+{format_amount(net, self.currency)}" if net > 0 else format_amount(net, self.currency)
        lines.append(f"\n**Net result:** {net_text}")
        await self._finish(GameResult(key, title, "\n".join(lines), payout, color))

    async def _finish(self, result: GameResult) -> None:
        if self.finished:
            return
        self.finished = True
        self.result = result
        self.insurance_pending = False
        self.disable_controls()
        wallet = await self.store.settle(self.session_id, self.guild_id, self.user_id, result, self.total_wager)
        if wallet:
            self.balance = int(wallet.get("balance", self.balance + result.payout))
        self.stop()

    async def _safe_edit(self, interaction: discord.Interaction, *, notice: str = "") -> None:
        if notice:
            self._last_notice = notice
        self._refresh_controls()
        embed = self.build_embed()

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

    async def _prepare_player_action(self) -> bool:
        if self.finished:
            return False
        return await self._decline_insurance_and_peek()

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, emoji="🃏", custom_id="casino_blackjack_hit", row=0)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self._lock:
            if self.finished:
                return await interaction.response.send_message("This table is already settled.", ephemeral=True)
            if not await self._prepare_player_action():
                return await self._safe_edit(interaction)
            hand = self._active_hand()
            if not hand:
                return await interaction.response.send_message("There is no active hand.", ephemeral=True)
            hand.cards.append(self._draw())
            total, _ = hand_value(hand.cards)
            if total > 21:
                hand.state = "bust"
                await self._advance_or_resolve()
            elif total == 21:
                hand.state = "stood"
                await self._advance_or_resolve()
            else:
                self._last_notice = f"**Hit:** Hand {self.current_hand + 1} is now {total}."
            await self._safe_edit(interaction)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary, emoji="✋", custom_id="casino_blackjack_stand", row=0)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self._lock:
            if self.finished:
                return await interaction.response.send_message("This table is already settled.", ephemeral=True)
            if not await self._prepare_player_action():
                return await self._safe_edit(interaction)
            hand = self._active_hand()
            if not hand:
                return await interaction.response.send_message("There is no active hand.", ephemeral=True)
            hand.state = "stood"
            await self._advance_or_resolve()
            await self._safe_edit(interaction)

    @discord.ui.button(label="Double", style=discord.ButtonStyle.success, emoji="⚡", custom_id="casino_blackjack_double", row=0)
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self._lock:
            if self.finished:
                return await interaction.response.send_message("This table is already settled.", ephemeral=True)
            if not await self._prepare_player_action():
                return await self._safe_edit(interaction)
            hand = self._active_hand()
            if not hand or not can_double(hand.cards, self.rules.double_rule):
                return await interaction.response.send_message(
                    f"This table only allows doubling on {self.rules.double_label}.", ephemeral=True
                )
            extra = hand.bet
            ok = await self.store.reserve_additional_wager(self.session_id, self.guild_id, self.user_id, extra)
            if not ok:
                return await interaction.response.send_message("You need enough balance to match this hand's wager.", ephemeral=True)
            self.total_wager += extra
            self.balance -= extra
            hand.bet += extra
            hand.doubled = True
            hand.cards.append(self._draw())
            total, _ = hand_value(hand.cards)
            hand.state = "bust" if total > 21 else "stood"
            await self._advance_or_resolve()
            await self._safe_edit(interaction, notice="**Double down:** the wager was doubled and one final card was drawn.")

    @discord.ui.button(label="Split", style=discord.ButtonStyle.primary, emoji="✂️", custom_id="casino_blackjack_split", row=0)
    async def split(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self._lock:
            if self.finished:
                return await interaction.response.send_message("This table is already settled.", ephemeral=True)
            if not await self._prepare_player_action():
                return await self._safe_edit(interaction)
            hand = self._active_hand()
            if not hand or not self.rules.allow_split or not cards_can_split(hand.cards):
                return await interaction.response.send_message("This hand cannot be split.", ephemeral=True)
            if len(self.hands) >= self.rules.max_split_hands:
                return await interaction.response.send_message(
                    f"This table allows at most {self.rules.max_split_hands} split hands.", ephemeral=True
                )
            extra = hand.bet
            ok = await self.store.reserve_additional_wager(self.session_id, self.guild_id, self.user_id, extra)
            if not ok:
                return await interaction.response.send_message("You need another full hand wager to split.", ephemeral=True)
            self.total_wager += extra
            self.balance -= extra
            first_card, second_card = hand.cards
            first = PlayerHand(cards=[first_card, self._draw()], bet=hand.bet, from_split=True)
            second = PlayerHand(cards=[second_card, self._draw()], bet=hand.bet, from_split=True)
            split_aces = first_card[0] == "A" and second_card[0] == "A"
            if split_aces:
                first.state = "stood"
                second.state = "stood"
            else:
                if hand_value(first.cards)[0] == 21:
                    first.state = "stood"
                if hand_value(second.cards)[0] == 21:
                    second.state = "stood"
            self.hands[self.current_hand : self.current_hand + 1] = [first, second]
            self._last_notice = "**Split:** each hand received one new card. Split aces receive one card each." if split_aces else "**Split:** play each hand separately."
            if first.state != "active":
                await self._advance_or_resolve()
            self._refresh_controls()
            await self._safe_edit(interaction)

    @discord.ui.button(label="Surrender", style=discord.ButtonStyle.danger, emoji="🏳️", custom_id="casino_blackjack_surrender", row=0)
    async def surrender(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self._lock:
            if self.finished:
                return await interaction.response.send_message("This table is already settled.", ephemeral=True)
            if not await self._prepare_player_action():
                return await self._safe_edit(interaction)
            hand = self._active_hand()
            if not hand or not self.rules.allow_surrender or len(self.hands) != 1 or len(hand.cards) != 2 or hand.from_split:
                return await interaction.response.send_message("Late surrender is not available for this hand.", ephemeral=True)
            payout = hand.bet // 2
            await self._finish(
                GameResult(
                    "loss",
                    "Surrendered",
                    f"Half your wager was returned: **{format_amount(payout, self.currency)}**.",
                    payout,
                    WARNING,
                )
            )
            await self._safe_edit(interaction)

    @discord.ui.button(label="Insurance", style=discord.ButtonStyle.secondary, emoji="🛡️", custom_id="casino_blackjack_insurance", row=1)
    async def insurance(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self._lock:
            if self.finished:
                return await interaction.response.send_message("This table is already settled.", ephemeral=True)
            if not self.insurance_pending or not self.rules.allow_insurance:
                return await interaction.response.send_message("Insurance is not currently available.", ephemeral=True)
            wager = self.original_bet // 2
            if wager <= 0:
                return await interaction.response.send_message("The original wager is too small for insurance.", ephemeral=True)
            ok = await self.store.reserve_additional_wager(self.session_id, self.guild_id, self.user_id, wager)
            if not ok:
                return await interaction.response.send_message("You need enough balance for the insurance wager.", ephemeral=True)
            self.insurance_wager = wager
            self.total_wager += wager
            self.balance -= wager
            self.insurance_pending = False
            if is_blackjack(self.dealer):
                insurance_return = wager * 3
                self.insurance_outcome = f"Won {format_amount(insurance_return, self.currency)}"
                await self._finish(
                    GameResult(
                        "loss" if insurance_return < self.total_wager else "push",
                        "Dealer Blackjack",
                        f"The dealer has blackjack. Insurance paid **2:1**, returning **{format_amount(insurance_return, self.currency)}**.",
                        insurance_return,
                        WARNING,
                    )
                )
            else:
                self.insurance_outcome = "Lost"
                self._last_notice = "**No dealer blackjack.** The insurance wager was lost; the main hand continues."
                self._refresh_controls()
            await self._safe_edit(interaction)

    async def on_timeout(self) -> None:
        async with self._lock:
            if self.finished:
                return
            try:
                self.insurance_pending = False
                for hand in self.hands:
                    if hand.state == "active":
                        hand.state = "stood"
                await self._resolve_all_hands()
                if self.message:
                    await self.message.edit(
                        embed=self.build_embed(notice="**Timed out:** all active hands automatically stood."), view=self
                    )
            except Exception as exc:  # pragma: no cover - Discord/network dependent
                try:
                    await self.store.abandon(self.session_id, self.guild_id, self.user_id, self.total_wager, refund=True)
                finally:
                    await self.report_error("blackjack_timeout", exc, guild_id=self.guild_id, user_id=self.user_id)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]
    ) -> None:  # pragma: no cover - Discord/network dependent
        incident = await self.report_error(
            "blackjack_component",
            error,
            guild_id=self.guild_id,
            user_id=self.user_id,
            details={"component": getattr(item, "custom_id", None)},
        )
        if not self.finished:
            await self.store.abandon(self.session_id, self.guild_id, self.user_id, self.total_wager, refund=True)
            self.finished = True
            self.disable_controls()
            self.stop()
        message = f"The table encountered an error and all reserved wagers were refunded. Reference: `{incident}`"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass
