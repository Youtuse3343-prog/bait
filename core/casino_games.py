from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import random
import secrets
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Sequence

import discord

from core.blackjack import CasinoStore, format_amount


EmbedFactory = Callable[[str, str, int], discord.Embed]
ErrorReporter = Callable[..., Awaitable[str]]

ACCENT = 0x8B5CF6
SUCCESS = 0x22C55E
DANGER = 0xEF4444
WARNING = 0xF59E0B
INFO = 0x38BDF8
CASINO_RTP = 0.96


# ---------------------------------------------------------------------------
# Provably-fair deterministic randomness
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class FairRound:
    """HMAC-SHA256 based deterministic random stream.

    The server publishes ``commitment`` before an interactive result and reveals
    ``server_seed`` when the round settles. A player can verify that:
      sha256(server_seed) == commitment
    and reproduce the stream from client_seed, nonce, label, and counter.
    """

    server_seed: str
    client_seed: str
    nonce: str
    commitment: str
    _counters: dict[str, int] = field(default_factory=dict)

    @classmethod
    def create(cls, *, client_seed: str, nonce: str) -> "FairRound":
        server_seed = secrets.token_hex(32)
        commitment = hashlib.sha256(server_seed.encode("utf-8")).hexdigest()
        return cls(server_seed=server_seed, client_seed=str(client_seed), nonce=str(nonce), commitment=commitment)

    def _digest(self, label: str, counter: int) -> bytes:
        message = f"{self.client_seed}:{self.nonce}:{label}:{counter}".encode("utf-8")
        return hmac.new(self.server_seed.encode("utf-8"), message, hashlib.sha256).digest()

    def randbelow(self, upper: int, *, label: str = "main") -> int:
        """Uniform integer using rejection sampling (no modulo bias)."""
        upper = int(upper)
        if upper <= 0:
            raise ValueError("upper must be positive")
        counter = self._counters.get(label, 0)
        bit_length = max(8, upper.bit_length())
        byte_count = (bit_length + 7) // 8
        sample_space = 1 << (byte_count * 8)
        limit = sample_space - (sample_space % upper)
        while True:
            digest = self._digest(label, counter)
            counter += 1
            value = int.from_bytes(digest[:byte_count], "big")
            if value < limit:
                self._counters[label] = counter
                return value % upper

    def shuffle(self, values: Sequence[Any], *, label: str = "shuffle") -> list[Any]:
        out = list(values)
        for index in range(len(out) - 1, 0, -1):
            swap = self.randbelow(index + 1, label=label)
            out[index], out[swap] = out[swap], out[index]
        return out

    def public_metadata(self, *, reveal: bool) -> dict[str, Any]:
        data: dict[str, Any] = {
            "commitment": self.commitment,
            "client_seed": self.client_seed,
            "nonce": self.nonce,
            "algorithm": "HMAC-SHA256/rejection-sampling/v1",
        }
        if reveal:
            data["server_seed"] = self.server_seed
        return data

    def deterministic_unit(self, label: str) -> float:
        """Stable value in [0, 1) that does not advance the random stream."""
        value = int.from_bytes(self._digest(f"rounding:{label}", 0)[:8], "big")
        return value / float(1 << 64)

    def short_commitment(self) -> str:
        return self.commitment[:16]

    def short_seed(self) -> str:
        return self.server_seed[:16]


def fair_integer_payout(fair: FairRound, exact_value: float, *, label: str) -> int:
    """Unbiased deterministic stochastic rounding for whole-credit economies."""
    exact = max(0.0, float(exact_value))
    base = math.floor(exact)
    fraction = exact - base
    return base + (1 if fraction > 0 and fair.deterministic_unit(label) < fraction else 0)


def fairness_text(fair: FairRound, *, reveal: bool) -> str:
    lines = [
        f"Round `{fair.nonce[:12]}`",
        f"Commit `{fair.short_commitment()}…`",
        f"Client seed `{fair.client_seed}`",
    ]
    if reveal:
        lines.append(f"Server seed `{fair.server_seed}`")
    else:
        lines.append("Server seed reveals when the round settles.")
    return "\n".join(lines)


def _round_result_key(payout: int, wager: int) -> str:
    if payout > wager:
        return "win"
    if payout == wager:
        return "push"
    return "loss"


def _result_color(result_key: str) -> int:
    return SUCCESS if result_key == "win" else (WARNING if result_key in {"push", "refund"} else DANGER)


@dataclass(slots=True)
class ResolvedGame:
    game: str
    title: str
    description: str
    wager: int
    payout: int
    fields: list[tuple[str, str, bool]]
    metadata: dict[str, Any]

    @property
    def result_key(self) -> str:
        return _round_result_key(self.payout, self.wager)

    @property
    def color(self) -> int:
        return _result_color(self.result_key)


# ---------------------------------------------------------------------------
# Plinko
# ---------------------------------------------------------------------------
PLINKO_RISKS = {"low", "medium", "high"}
PLINKO_ROWS = {8, 10, 12}


def plinko_multipliers(risk: str, rows: int, *, target_rtp: float = CASINO_RTP) -> list[float]:
    risk = str(risk).lower()
    if risk not in PLINKO_RISKS:
        risk = "medium"
    rows = int(rows)
    if rows not in PLINKO_ROWS:
        rows = 10
    center = rows / 2
    raw: list[float] = []
    for slot in range(rows + 1):
        distance = abs(slot - center) / max(1.0, center)
        if risk == "low":
            value = 0.72 + 4.8 * (distance**2.15)
        elif risk == "high":
            value = 0.08 + 74.0 * (distance**5.1)
        else:
            value = 0.30 + 17.0 * (distance**3.25)
        raw.append(value)

    probabilities = [math.comb(rows, slot) / (2**rows) for slot in range(rows + 1)]
    expected = sum(prob * value for prob, value in zip(probabilities, raw))
    scale = float(target_rtp) / expected
    values = [max(0.01, round(value * scale, 2)) for value in raw]
    # Correct tiny rounding drift at the most common center bucket.
    actual = sum(prob * value for prob, value in zip(probabilities, values))
    center_index = rows // 2
    if probabilities[center_index] > 0:
        values[center_index] = max(
            0.01,
            round(values[center_index] + (float(target_rtp) - actual) / probabilities[center_index], 2),
        )
    return values


def plinko_theoretical_rtp(risk: str, rows: int) -> float:
    multipliers = plinko_multipliers(risk, rows)
    return sum(math.comb(rows, slot) / (2**rows) * mult for slot, mult in enumerate(multipliers))


def plinko_max_multiplier(risk: str, rows: int) -> float:
    return max(plinko_multipliers(risk, rows))


def resolve_plinko(
    fair: FairRound,
    *,
    bet_per_ball: int,
    balls: int,
    risk: str,
    rows: int,
) -> ResolvedGame:
    bet_per_ball = int(bet_per_ball)
    balls = max(1, min(int(balls), 5))
    risk = str(risk).lower()
    rows = int(rows)
    multipliers = plinko_multipliers(risk, rows)
    outcomes: list[dict[str, Any]] = []
    payout = 0
    for ball in range(balls):
        rights = 0
        path: list[str] = []
        for _ in range(rows):
            direction = fair.randbelow(2, label=f"plinko:{ball}")
            rights += direction
            path.append("R" if direction else "L")
        multiplier = multipliers[rights]
        ball_payout = fair_integer_payout(fair, bet_per_ball * multiplier, label=f"plinko:{ball}:payout")
        payout += ball_payout
        outcomes.append(
            {
                "ball": ball + 1,
                "slot": rights,
                "path": "".join(path),
                "multiplier": multiplier,
                "payout": ball_payout,
            }
        )

    wager = bet_per_ball * balls
    lines = [
        f"**#{entry['ball']}** → slot `{entry['slot']}` • **{entry['multiplier']:.2f}×** • {entry['payout']:,}"
        for entry in outcomes
    ]
    rtp = plinko_theoretical_rtp(risk, rows)
    return ResolvedGame(
        game="plinko",
        title="Plinko Board",
        description="The ball paths were generated from the committed fair seed before this result was rendered.",
        wager=wager,
        payout=payout,
        fields=[
            ("Drops", "\n".join(lines), False),
            ("Board", f"{rows} rows • {risk.title()} risk • theoretical RTP **{rtp * 100:.2f}%**", False),
        ],
        metadata={
            "risk": risk,
            "rows": rows,
            "balls": balls,
            "bet_per_ball": bet_per_ball,
            "outcomes": outcomes,
            "rtp": rtp,
            "fairness": fair.public_metadata(reveal=True),
        },
    )


# ---------------------------------------------------------------------------
# European roulette
# ---------------------------------------------------------------------------
ROULETTE_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


@dataclass(frozen=True, slots=True)
class RouletteBet:
    key: str
    label: str
    payout_multiplier: int  # Total return, including original stake.
    numbers: frozenset[int]


def parse_roulette_bet(value: str) -> Optional[RouletteBet]:
    raw = " ".join(str(value or "").strip().lower().replace("_", " ").replace("-", " ").split())
    compact = raw.replace(" ", "")
    if compact.isdigit():
        number = int(compact)
        if compact == str(number) and 0 <= number <= 36:
            return RouletteBet(f"number_{number}", f"Straight up {number}", 36, frozenset({number}))

    aliases = {
        "red": ("Red", 2, ROULETTE_RED),
        "black": ("Black", 2, set(range(1, 37)) - ROULETTE_RED),
        "odd": ("Odd", 2, {n for n in range(1, 37) if n % 2 == 1}),
        "even": ("Even", 2, {n for n in range(1, 37) if n % 2 == 0}),
        "low": ("Low 1–18", 2, set(range(1, 19))),
        "1to18": ("Low 1–18", 2, set(range(1, 19))),
        "high": ("High 19–36", 2, set(range(19, 37))),
        "19to36": ("High 19–36", 2, set(range(19, 37))),
        "dozen1": ("1st dozen", 3, set(range(1, 13))),
        "1stdozen": ("1st dozen", 3, set(range(1, 13))),
        "dozen2": ("2nd dozen", 3, set(range(13, 25))),
        "2nddozen": ("2nd dozen", 3, set(range(13, 25))),
        "dozen3": ("3rd dozen", 3, set(range(25, 37))),
        "3rddozen": ("3rd dozen", 3, set(range(25, 37))),
        "column1": ("1st column", 3, {n for n in range(1, 37) if (n - 1) % 3 == 0}),
        "1stcolumn": ("1st column", 3, {n for n in range(1, 37) if (n - 1) % 3 == 0}),
        "column2": ("2nd column", 3, {n for n in range(1, 37) if (n - 2) % 3 == 0}),
        "2ndcolumn": ("2nd column", 3, {n for n in range(1, 37) if (n - 2) % 3 == 0}),
        "column3": ("3rd column", 3, {n for n in range(1, 37) if n % 3 == 0}),
        "3rdcolumn": ("3rd column", 3, {n for n in range(1, 37) if n % 3 == 0}),
    }
    found = aliases.get(compact)
    if not found:
        return None
    label, multiplier, numbers = found
    return RouletteBet(compact, label, multiplier, frozenset(numbers))


def roulette_color(number: int) -> str:
    if number == 0:
        return "green"
    return "red" if number in ROULETTE_RED else "black"


def resolve_roulette(fair: FairRound, *, bet: int, wager: RouletteBet) -> ResolvedGame:
    number = fair.randbelow(37, label="roulette-wheel")
    color = roulette_color(number)
    won = number in wager.numbers
    payout = int(bet) * wager.payout_multiplier if won else 0
    result_label = "WIN" if won else "LOSS"
    return ResolvedGame(
        game="roulette",
        title="European Roulette",
        description=f"The wheel landed on **{number} {color.upper()}**.",
        wager=int(bet),
        payout=payout,
        fields=[
            ("Your bet", f"{wager.label} • total return **{wager.payout_multiplier}×**", True),
            ("Result", result_label, True),
            ("Wheel", "European single-zero • house edge **2.70%**", False),
        ],
        metadata={
            "number": number,
            "color": color,
            "bet_key": wager.key,
            "bet_label": wager.label,
            "return_multiplier": wager.payout_multiplier,
            "fairness": fair.public_metadata(reveal=True),
        },
    )


# ---------------------------------------------------------------------------
# Classic 3x3 slots with five fixed paylines
# ---------------------------------------------------------------------------
SLOT_SYMBOLS = ("cherry", "lemon", "bell", "bar", "diamond", "seven", "crown")
SLOT_DISPLAY = {
    "cherry": "🍒",
    "lemon": "🍋",
    "bell": "🔔",
    "bar": "BAR",
    "diamond": "💎",
    "seven": "7️⃣",
    "crown": "👑",
}
SLOT_LINES = (
    (0, 0, 0),
    (1, 1, 1),
    (2, 2, 2),
    (0, 1, 2),
    (2, 1, 0),
)


@dataclass(frozen=True, slots=True)
class SlotMachine:
    key: str
    name: str
    volatility: str
    weights: dict[str, int]
    payouts: dict[str, float]
    reels: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]
    rtp: float
    max_spin_multiplier: float

    @property
    def max_multiplier(self) -> float:
        """Maximum total-bet multiplier reachable across all five paylines."""
        return self.max_spin_multiplier


def _deterministic_strip(weights: dict[str, int], salt: str) -> tuple[str, ...]:
    strip: list[str] = []
    for symbol in SLOT_SYMBOLS:
        strip.extend([symbol] * max(1, int(weights[symbol])))
    seed = int.from_bytes(hashlib.sha256(salt.encode("utf-8")).digest()[:8], "big")
    random.Random(seed).shuffle(strip)
    return tuple(strip)


def _slot_max_spin_multiplier(
    reels: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]],
    payouts: dict[str, float],
) -> float:
    maximum = 0.0
    for first in range(len(reels[0])):
        for second in range(len(reels[1])):
            for third in range(len(reels[2])):
                stops = (first, second, third)
                grid: list[list[str]] = [[], [], []]
                for reel_index, strip in enumerate(reels):
                    stop = stops[reel_index]
                    for row, offset in enumerate((-1, 0, 1)):
                        grid[row].append(strip[(stop + offset) % len(strip)])
                total = 0.0
                for line in SLOT_LINES:
                    symbols = [grid[line[reel]][reel] for reel in range(3)]
                    if symbols[0] == symbols[1] == symbols[2]:
                        total += payouts[symbols[0]]
                maximum = max(maximum, total / len(SLOT_LINES))
    return maximum


def _build_machine(
    key: str,
    name: str,
    volatility: str,
    weights: dict[str, int],
    raw_payouts: dict[str, float],
    target_rtp: float = CASINO_RTP,
) -> SlotMachine:
    total = sum(weights.values())
    raw_expected = sum((weights[symbol] / total) ** 3 * raw_payouts[symbol] for symbol in SLOT_SYMBOLS)
    scale = target_rtp / raw_expected
    payouts = {symbol: max(0.05, round(raw_payouts[symbol] * scale, 2)) for symbol in SLOT_SYMBOLS}
    actual_rtp = sum((weights[symbol] / total) ** 3 * payouts[symbol] for symbol in SLOT_SYMBOLS)
    reels = tuple(_deterministic_strip(weights, f"{key}:reel:{index}") for index in range(3))
    typed_reels = reels  # type: ignore[assignment]
    maximum = _slot_max_spin_multiplier(typed_reels, payouts)
    return SlotMachine(
        key=key,
        name=name,
        volatility=volatility,
        weights=dict(weights),
        payouts=payouts,
        reels=typed_reels,
        rtp=actual_rtp,
        max_spin_multiplier=maximum,
    )


SLOT_MACHINES: dict[str, SlotMachine] = {
    "fortune": _build_machine(
        "fortune",
        "Purple Fortune",
        "Low",
        {"cherry": 14, "lemon": 12, "bell": 9, "bar": 6, "diamond": 3, "seven": 2, "crown": 1},
        {"cherry": 2, "lemon": 3, "bell": 5, "bar": 9, "diamond": 18, "seven": 35, "crown": 80},
    ),
    "vault": _build_machine(
        "vault",
        "Diamond Vault",
        "Medium",
        {"cherry": 16, "lemon": 11, "bell": 7, "bar": 4, "diamond": 3, "seven": 2, "crown": 1},
        {"cherry": 1.5, "lemon": 3, "bell": 7, "bar": 16, "diamond": 35, "seven": 75, "crown": 180},
    ),
    "void": _build_machine(
        "void",
        "Void Jackpot",
        "High",
        {"cherry": 18, "lemon": 10, "bell": 6, "bar": 3, "diamond": 2, "seven": 1, "crown": 1},
        {"cherry": 1.2, "lemon": 2.5, "bell": 6, "bar": 18, "diamond": 55, "seven": 150, "crown": 500},
    ),
}


def slot_paytable_text(machine: SlotMachine) -> str:
    return " • ".join(f"{SLOT_DISPLAY[symbol]} {machine.payouts[symbol]:g}×" for symbol in SLOT_SYMBOLS)


def resolve_slots(fair: FairRound, *, bet: int, machine: SlotMachine) -> ResolvedGame:
    stops = [fair.randbelow(len(machine.reels[index]), label=f"slots:{machine.key}:reel:{index}") for index in range(3)]
    grid: list[list[str]] = [[], [], []]
    for reel_index, strip in enumerate(machine.reels):
        stop = stops[reel_index]
        for row, offset in enumerate((-1, 0, 1)):
            grid[row].append(strip[(stop + offset) % len(strip)])

    wins: list[dict[str, Any]] = []
    multiplier_sum = 0.0
    for line_number, line in enumerate(SLOT_LINES, 1):
        symbols = [grid[line[reel]][reel] for reel in range(3)]
        if symbols[0] == symbols[1] == symbols[2]:
            multiplier = machine.payouts[symbols[0]]
            multiplier_sum += multiplier
            wins.append({"line": line_number, "symbol": symbols[0], "multiplier": multiplier})

    payout = fair_integer_payout(
        fair,
        int(bet) * multiplier_sum / len(SLOT_LINES),
        label=f"slots:{machine.key}:payout",
    )
    rendered_rows = [" │ ".join(f"{SLOT_DISPLAY[symbol]:^3}" for symbol in row) for row in grid]
    win_text = (
        "\n".join(
            f"Line {win['line']}: {SLOT_DISPLAY[win['symbol']]} ×3 → **{win['multiplier']:g}× line bet**" for win in wins
        )
        if wins
        else "No completed payline this spin."
    )
    return ResolvedGame(
        game="slots",
        title=machine.name,
        description="```\n" + "\n".join(rendered_rows) + "\n```",
        wager=int(bet),
        payout=payout,
        fields=[
            ("Paylines", win_text, False),
            ("Machine", f"{machine.volatility} volatility • theoretical RTP **{machine.rtp * 100:.2f}%** • 5 equal paylines", False),
            ("Paytable", slot_paytable_text(machine), False),
        ],
        metadata={
            "machine": machine.key,
            "stops": stops,
            "grid": grid,
            "wins": wins,
            "multiplier_sum": multiplier_sum,
            "rtp": machine.rtp,
            "fairness": fair.public_metadata(reveal=True),
        },
    )


# ---------------------------------------------------------------------------
# Mines interactive table
# ---------------------------------------------------------------------------
MINES_CELLS = 20


def mines_multiplier(cells: int, mines: int, safe_picks: int, *, rtp: float = CASINO_RTP) -> float:
    cells = int(cells)
    mines = int(mines)
    safe_picks = int(safe_picks)
    if safe_picks <= 0:
        return 1.0
    safe_cells = cells - mines
    if safe_picks > safe_cells:
        return 0.0
    fair = math.comb(cells, safe_picks) / math.comb(safe_cells, safe_picks)
    return fair * float(rtp)


class MinesTileButton(discord.ui.Button):
    def __init__(self, index: int) -> None:
        super().__init__(
            label=str(index + 1),
            style=discord.ButtonStyle.secondary,
            custom_id=f"casino_mines_tile_{index}",
            row=index // 5,
        )
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, MinesView):
            await view.pick(interaction, self.index)


class MinesCashoutButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Cash Out",
            style=discord.ButtonStyle.success,
            custom_id="casino_mines_cashout",
            row=4,
            disabled=True,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, MinesView):
            await view.cashout(interaction)


class MinesView(discord.ui.View):
    def __init__(
        self,
        *,
        store: CasinoStore,
        guild_id: int,
        user_id: int,
        display_name: str,
        session_id: str,
        bet: int,
        mines: int,
        currency: str,
        balance_after_bet: int,
        max_payout: int,
        fair: FairRound,
        embed_factory: EmbedFactory,
        report_error: ErrorReporter,
        rtp: float = CASINO_RTP,
    ) -> None:
        super().__init__(timeout=240)
        self.store = store
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        self.display_name = display_name
        self.session_id = session_id
        self.bet = int(bet)
        self.mines = max(1, min(int(mines), 15))
        self.currency = str(currency)
        self.balance = int(balance_after_bet)
        self.max_payout = max(self.bet, int(max_payout))
        self.fair = fair
        self.embed_factory = embed_factory
        self.report_error = report_error
        self.rtp = max(0.90, min(float(rtp), 0.99))
        self.mine_positions = set(self.fair.shuffle(range(MINES_CELLS), label="mines-board")[: self.mines])
        self.revealed: set[int] = set()
        self.finished = False
        self.result_text = ""
        self.message: Optional[discord.Message] = None
        self._lock = asyncio.Lock()
        for index in range(MINES_CELLS):
            self.add_item(MinesTileButton(index))
        self.add_item(MinesCashoutButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This Mines board belongs to another player.", ephemeral=True)
            return False
        return True

    def _cashout_button(self) -> Optional[discord.ui.Button]:
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id == "casino_mines_cashout":
                return item
        return None

    def _tile(self, index: int) -> Optional[MinesTileButton]:
        for item in self.children:
            if isinstance(item, MinesTileButton) and item.index == index:
                return item
        return None

    @property
    def multiplier(self) -> float:
        return mines_multiplier(MINES_CELLS, self.mines, len(self.revealed), rtp=self.rtp)

    @property
    def current_payout(self) -> int:
        if not self.revealed:
            return self.bet
        return min(
            self.max_payout,
            fair_integer_payout(self.fair, self.bet * self.multiplier, label=f"mines:payout:{len(self.revealed)}"),
        )

    def _refresh_buttons(self, *, reveal_all: bool = False) -> None:
        for index in range(MINES_CELLS):
            button = self._tile(index)
            if not button:
                continue
            if reveal_all:
                button.disabled = True
                if index in self.mine_positions:
                    button.label = "MINE"
                    button.style = discord.ButtonStyle.danger
                elif index in self.revealed:
                    button.label = "SAFE"
                    button.style = discord.ButtonStyle.success
                else:
                    button.label = "·"
                    button.style = discord.ButtonStyle.secondary
            elif index in self.revealed:
                button.disabled = True
                button.label = "SAFE"
                button.style = discord.ButtonStyle.success
        cashout = self._cashout_button()
        if cashout:
            cashout.disabled = self.finished or not self.revealed
            cashout.label = f"Cash Out · {self.current_payout:,}" if self.revealed and not self.finished else "Cash Out"
        if self.finished:
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True

    def build_embed(self) -> discord.Embed:
        safe_total = MINES_CELLS - self.mines
        description = self.result_text or "Choose safe tiles, then cash out whenever you are satisfied with the payout. Mine positions cannot change after the round begins."
        embed = self.embed_factory("Mines", description, SUCCESS if self.finished and self.result_text.startswith("You cashed") else (DANGER if "mine" in self.result_text.lower() else ACCENT))
        embed.add_field(name="Board", value=f"{MINES_CELLS} tiles • {self.mines} mines • {safe_total} safe", inline=True)
        embed.add_field(name="Safe picks", value=f"{len(self.revealed)} / {safe_total}", inline=True)
        embed.add_field(name="Multiplier", value=f"{self.multiplier:.4f}×" if self.revealed else "1.0000×", inline=True)
        embed.add_field(name="Wager", value=format_amount(self.bet, self.currency), inline=True)
        embed.add_field(name="Cash-out value", value=format_amount(self.current_payout, self.currency), inline=True)
        embed.add_field(name="Wallet balance", value=format_amount(self.balance, self.currency), inline=True)
        embed.add_field(name="Payout ceiling", value=format_amount(self.max_payout, self.currency), inline=True)
        embed.add_field(name="Fairness commitment", value=fairness_text(self.fair, reveal=self.finished), inline=False)
        embed.set_footer(text=f"Fixed {self.rtp * 100:.0f}% mathematical return before the disclosed payout cap")
        return embed

    async def open_table(self, interaction: discord.Interaction) -> None:
        self._refresh_buttons()
        await interaction.edit_original_response(content=None, embed=self.build_embed(), view=self)

    async def pick(self, interaction: discord.Interaction, index: int) -> None:
        async with self._lock:
            if self.finished or index in self.revealed:
                return await interaction.response.defer()
            try:
                await self.store.touch_session(self.session_id, play_minutes=10)
                if index in self.mine_positions:
                    self.finished = True
                    self.result_text = f"Tile **{index + 1}** contained a mine. The wager was lost."
                    self._refresh_buttons(reveal_all=True)
                    wallet = await self.store.settle_game(
                        self.session_id,
                        self.guild_id,
                        self.user_id,
                        game="mines",
                        result_key="loss",
                        payout=0,
                        total_wager=self.bet,
                        metadata={
                            "mines": self.mines,
                            "safe_picks": len(self.revealed),
                            "mine_positions": sorted(self.mine_positions),
                            "fairness": self.fair.public_metadata(reveal=True),
                        },
                    )
                    if wallet:
                        self.balance = int(wallet.get("balance", self.balance))
                    self.stop()
                    return await interaction.response.edit_message(embed=self.build_embed(), view=self)

                self.revealed.add(index)
                button = self._tile(index)
                if button:
                    button.disabled = True
                    button.label = "SAFE"
                    button.style = discord.ButtonStyle.success
                reached_cap = self.current_payout >= self.max_payout
                cleared = len(self.revealed) >= MINES_CELLS - self.mines
                if reached_cap or cleared:
                    reason = "Maximum payout reached" if reached_cap else "Every safe tile cleared"
                    await self._settle_cashout(interaction, reason=reason)
                    return
                self._refresh_buttons()
                await interaction.response.edit_message(embed=self.build_embed(), view=self)
            except Exception as exc:
                await self._technical_failure(interaction, exc)

    async def cashout(self, interaction: discord.Interaction) -> None:
        async with self._lock:
            if self.finished or not self.revealed:
                return await interaction.response.defer()
            try:
                await self._settle_cashout(interaction, reason="Manual cash out")
            except Exception as exc:
                await self._technical_failure(interaction, exc)

    async def _settle_cashout(self, interaction: discord.Interaction, *, reason: str) -> None:
        payout = self.current_payout
        self.finished = True
        self.result_text = f"You cashed out for **{format_amount(payout, self.currency)}**. {reason}."
        self._refresh_buttons(reveal_all=True)
        result_key = _round_result_key(payout, self.bet)
        wallet = await self.store.settle_game(
            self.session_id,
            self.guild_id,
            self.user_id,
            game="mines",
            result_key=result_key,
            payout=payout,
            total_wager=self.bet,
            metadata={
                "mines": self.mines,
                "safe_picks": len(self.revealed),
                "multiplier": self.multiplier,
                "mine_positions": sorted(self.mine_positions),
                "reason": reason,
                "fairness": self.fair.public_metadata(reveal=True),
            },
        )
        if wallet:
            self.balance = int(wallet.get("balance", self.balance))
        self.stop()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _technical_failure(self, interaction: discord.Interaction, exc: Exception) -> None:
        self.finished = True
        await self.store.abandon(self.session_id, self.guild_id, self.user_id, self.bet, refund=True)
        incident = await self.report_error("mines_component", exc, guild_id=self.guild_id, user_id=self.user_id)
        self.result_text = f"The board encountered an error and your wager was refunded. Reference: `{incident}`"
        self._refresh_buttons(reveal_all=True)
        self.stop()
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=self.build_embed(), view=self)
            else:
                await interaction.response.edit_message(embed=self.build_embed(), view=self)
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        if self.finished:
            return
        async with self._lock:
            try:
                if self.revealed:
                    payout = self.current_payout
                    self.finished = True
                    self.result_text = f"The board timed out and automatically cashed out for **{format_amount(payout, self.currency)}**."
                    wallet = await self.store.settle_game(
                        self.session_id,
                        self.guild_id,
                        self.user_id,
                        game="mines",
                        result_key=_round_result_key(payout, self.bet),
                        payout=payout,
                        total_wager=self.bet,
                        metadata={
                            "mines": self.mines,
                            "safe_picks": len(self.revealed),
                            "timed_out": True,
                            "mine_positions": sorted(self.mine_positions),
                            "fairness": self.fair.public_metadata(reveal=True),
                        },
                    )
                    if wallet:
                        self.balance = int(wallet.get("balance", self.balance))
                else:
                    self.finished = True
                    self.result_text = "The untouched board timed out, so the original wager was refunded."
                    wallet = await self.store.settle_game(
                        self.session_id,
                        self.guild_id,
                        self.user_id,
                        game="mines",
                        result_key="refund",
                        payout=self.bet,
                        total_wager=self.bet,
                        metadata={"timed_out": True, "fairness": self.fair.public_metadata(reveal=True)},
                    )
                    if wallet:
                        self.balance = int(wallet.get("balance", self.balance))
                self._refresh_buttons(reveal_all=True)
                if self.message:
                    await self.message.edit(embed=self.build_embed(), view=self)
            except Exception as exc:
                await self.report_error("mines_timeout", exc, guild_id=self.guild_id, user_id=self.user_id)
            finally:
                self.stop()


# ---------------------------------------------------------------------------
# Higher or Lower interactive deck
# ---------------------------------------------------------------------------
CARD_SUITS = ("♠", "♥", "♦", "♣")
CARD_RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
CARD_VALUES = {rank: index + 1 for index, rank in enumerate(CARD_RANKS)}


def _card_text(card: tuple[str, str]) -> str:
    return f"`{card[0]}{card[1]}`"


class HigherLowerView(discord.ui.View):
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
        max_payout: int,
        fair: FairRound,
        embed_factory: EmbedFactory,
        report_error: ErrorReporter,
        rtp: float = CASINO_RTP,
    ) -> None:
        super().__init__(timeout=240)
        self.store = store
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        self.display_name = display_name
        self.session_id = session_id
        self.bet = int(bet)
        self.currency = str(currency)
        self.balance = int(balance_after_bet)
        self.max_payout = max(self.bet, int(max_payout))
        self.fair = fair
        self.embed_factory = embed_factory
        self.report_error = report_error
        self.rtp = max(0.90, min(float(rtp), 0.99))
        cards = [(rank, suit) for suit in CARD_SUITS for rank in CARD_RANKS]
        self.deck = self.fair.shuffle(cards, label="higher-lower-deck")
        self.current_card = self.deck.pop()
        self.multiplier = 1.0
        self.correct_guesses = 0
        self.ties = 0
        self.history: list[tuple[str, tuple[str, str]]] = []
        self.finished = False
        self.result_text = ""
        self.outcome = ""
        self.final_payout: Optional[int] = None
        self.message: Optional[discord.Message] = None
        self._lock = asyncio.Lock()
        self._refresh_buttons()

    @discord.ui.button(label="Higher", style=discord.ButtonStyle.primary, custom_id="casino_hilo_higher", row=0)
    async def higher(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.guess(interaction, "higher")

    @discord.ui.button(label="Lower", style=discord.ButtonStyle.primary, custom_id="casino_hilo_lower", row=0)
    async def lower(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.guess(interaction, "lower")

    @discord.ui.button(label="Cash Out", style=discord.ButtonStyle.success, custom_id="casino_hilo_cashout", row=0)
    async def cashout_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self._lock:
            if self.finished or self.correct_guesses <= 0:
                return await interaction.response.defer()
            try:
                await self._settle_cashout(interaction, reason="Manual cash out")
            except Exception as exc:
                await self._technical_failure(interaction, exc)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This Higher or Lower table belongs to another player.", ephemeral=True)
            return False
        return True

    def _counts(self, direction: str) -> tuple[int, int, int]:
        current_value = CARD_VALUES[self.current_card[0]]
        favorable = 0
        unfavorable = 0
        ties = 0
        for card in self.deck:
            value = CARD_VALUES[card[0]]
            if value == current_value:
                ties += 1
            elif (direction == "higher" and value > current_value) or (direction == "lower" and value < current_value):
                favorable += 1
            else:
                unfavorable += 1
        return favorable, unfavorable, ties

    def _probability(self, direction: str) -> float:
        favorable, unfavorable, _ = self._counts(direction)
        decisive = favorable + unfavorable
        return favorable / decisive if decisive else 0.0

    def _next_multiplier(self, direction: str) -> float:
        probability = self._probability(direction)
        if probability <= 0:
            return 0.0
        step = max(1.0, self.rtp / probability)
        return self.multiplier * step

    @property
    def current_payout(self) -> int:
        return min(
            self.max_payout,
            fair_integer_payout(
                self.fair,
                self.bet * self.multiplier,
                label=f"higher-lower:payout:{self.correct_guesses}",
            ),
        )

    def _potential_payout(self, direction: str) -> int:
        multiplier = self._next_multiplier(direction)
        if multiplier <= 0:
            return 0
        return min(
            self.max_payout,
            fair_integer_payout(
                self.fair,
                self.bet * multiplier,
                label=f"higher-lower:potential:{self.correct_guesses}:{direction}",
            ),
        )

    def _refresh_buttons(self) -> None:
        higher_probability = self._probability("higher") if self.deck else 0
        lower_probability = self._probability("lower") if self.deck else 0
        self.higher.disabled = self.finished or higher_probability <= 0
        self.lower.disabled = self.finished or lower_probability <= 0
        self.cashout_button.disabled = self.finished or self.correct_guesses <= 0
        if not self.finished:
            self.higher.label = f"Higher · {higher_probability * 100:.1f}%" if higher_probability else "Higher · unavailable"
            self.lower.label = f"Lower · {lower_probability * 100:.1f}%" if lower_probability else "Lower · unavailable"
            self.cashout_button.label = f"Cash Out · {self.current_payout:,}" if self.correct_guesses else "Cash Out"
        else:
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True

    def build_embed(self) -> discord.Embed:
        description = self.result_text or "Predict whether the next non-equal card is higher or lower. Equal ranks are a push and do not change your multiplier."
        color = _result_color(self.outcome) if self.finished and self.outcome else ACCENT
        embed = self.embed_factory("Higher or Lower", description, color)
        embed.add_field(name="Current card", value=_card_text(self.current_card), inline=True)
        embed.add_field(name="Cards remaining", value=str(len(self.deck)), inline=True)
        embed.add_field(name="Correct guesses", value=str(self.correct_guesses), inline=True)
        embed.add_field(name="Current multiplier", value=f"{self.multiplier:.4f}×", inline=True)
        shown_payout = self.final_payout if self.finished and self.final_payout is not None else self.current_payout
        embed.add_field(name="Final payout" if self.finished else "Cash-out value", value=format_amount(shown_payout, self.currency), inline=True)
        embed.add_field(name="Ties", value=str(self.ties), inline=True)
        embed.add_field(name="Payout ceiling", value=format_amount(self.max_payout, self.currency), inline=True)
        if not self.finished and self.deck:
            higher_probability = self._probability("higher")
            lower_probability = self._probability("lower")
            embed.add_field(
                name="Next decisions",
                value=(
                    f"Higher: **{higher_probability * 100:.2f}%** decisive chance → {format_amount(self._potential_payout('higher'), self.currency)}\n"
                    f"Lower: **{lower_probability * 100:.2f}%** decisive chance → {format_amount(self._potential_payout('lower'), self.currency)}"
                ),
                inline=False,
            )
        if self.history:
            recent = self.history[-6:]
            embed.add_field(name="Recent cards", value=" → ".join(f"{choice.title()} {_card_text(card)}" for choice, card in recent), inline=False)
        embed.add_field(name="Fairness commitment", value=fairness_text(self.fair, reveal=self.finished), inline=False)
        embed.set_footer(text=f"Ace low • King high • ties push • {self.rtp * 100:.0f}% return applied to each decisive prediction")
        return embed

    async def open_table(self, interaction: discord.Interaction) -> None:
        self._refresh_buttons()
        await interaction.edit_original_response(content=None, embed=self.build_embed(), view=self)

    async def guess(self, interaction: discord.Interaction, direction: str) -> None:
        async with self._lock:
            if self.finished or not self.deck:
                return await interaction.response.defer()
            probability = self._probability(direction)
            if probability <= 0:
                return await interaction.response.send_message("That direction cannot win from the current card.", ephemeral=True)
            try:
                await self.store.touch_session(self.session_id, play_minutes=10)
                next_multiplier = self._next_multiplier(direction)
                next_card = self.deck.pop()
                current_value = CARD_VALUES[self.current_card[0]]
                next_value = CARD_VALUES[next_card[0]]
                self.history.append((direction, next_card))
                self.current_card = next_card
                if next_value == current_value:
                    self.ties += 1
                    self.result_text = "Equal rank — push. Your multiplier is unchanged and the round continues."
                else:
                    won = (direction == "higher" and next_value > current_value) or (direction == "lower" and next_value < current_value)
                    if not won:
                        self.finished = True
                        self.outcome = "loss"
                        self.final_payout = 0
                        self.result_text = f"The next card was {_card_text(next_card)}. Your **{direction}** prediction lost."
                        self._refresh_buttons()
                        wallet = await self.store.settle_game(
                            self.session_id,
                            self.guild_id,
                            self.user_id,
                            game="higher_lower",
                            result_key="loss",
                            payout=0,
                            total_wager=self.bet,
                            metadata={
                                "correct_guesses": self.correct_guesses,
                                "ties": self.ties,
                                "history": self.history,
                                "fairness": self.fair.public_metadata(reveal=True),
                            },
                        )
                        if wallet:
                            self.balance = int(wallet.get("balance", self.balance))
                        self.stop()
                        return await interaction.response.edit_message(embed=self.build_embed(), view=self)
                    self.multiplier = next_multiplier
                    self.correct_guesses += 1
                    self.result_text = f"Correct — {_card_text(next_card)} was {direction}. You may continue or cash out."

                if not self.deck or self.current_payout >= self.max_payout:
                    reason = "Maximum payout reached" if self.current_payout >= self.max_payout else "Deck completed"
                    await self._settle_cashout(interaction, reason=reason)
                    return
                self._refresh_buttons()
                await interaction.response.edit_message(embed=self.build_embed(), view=self)
            except Exception as exc:
                await self._technical_failure(interaction, exc)

    async def _settle_cashout(self, interaction: discord.Interaction, *, reason: str) -> None:
        payout = self.current_payout
        self.finished = True
        self.outcome = _round_result_key(payout, self.bet)
        self.final_payout = payout
        self.result_text = f"You cashed out for **{format_amount(payout, self.currency)}**. {reason}."
        self._refresh_buttons()
        wallet = await self.store.settle_game(
            self.session_id,
            self.guild_id,
            self.user_id,
            game="higher_lower",
            result_key=_round_result_key(payout, self.bet),
            payout=payout,
            total_wager=self.bet,
            metadata={
                "correct_guesses": self.correct_guesses,
                "ties": self.ties,
                "multiplier": self.multiplier,
                "history": self.history,
                "reason": reason,
                "fairness": self.fair.public_metadata(reveal=True),
            },
        )
        if wallet:
            self.balance = int(wallet.get("balance", self.balance))
        self.stop()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _technical_failure(self, interaction: discord.Interaction, exc: Exception) -> None:
        self.finished = True
        self.outcome = "push"
        self.final_payout = self.bet
        await self.store.abandon(self.session_id, self.guild_id, self.user_id, self.bet, refund=True)
        incident = await self.report_error("higher_lower_component", exc, guild_id=self.guild_id, user_id=self.user_id)
        self.result_text = f"The table encountered an error and your wager was refunded. Reference: `{incident}`"
        self._refresh_buttons()
        self.stop()
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=self.build_embed(), view=self)
            else:
                await interaction.response.edit_message(embed=self.build_embed(), view=self)
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        if self.finished:
            return
        async with self._lock:
            try:
                self.finished = True
                if self.correct_guesses > 0:
                    payout = self.current_payout
                    self.result_text = f"The table timed out and automatically cashed out for **{format_amount(payout, self.currency)}**."
                    result_key = _round_result_key(payout, self.bet)
                else:
                    payout = self.bet
                    result_key = "refund"
                    self.result_text = "The untouched table timed out, so the original wager was refunded."
                self.outcome = result_key
                self.final_payout = payout
                wallet = await self.store.settle_game(
                    self.session_id,
                    self.guild_id,
                    self.user_id,
                    game="higher_lower",
                    result_key=result_key,
                    payout=payout,
                    total_wager=self.bet,
                    metadata={
                        "correct_guesses": self.correct_guesses,
                        "ties": self.ties,
                        "timed_out": True,
                        "history": self.history,
                        "fairness": self.fair.public_metadata(reveal=True),
                    },
                )
                if wallet:
                    self.balance = int(wallet.get("balance", self.balance))
                self._refresh_buttons()
                if self.message:
                    await self.message.edit(embed=self.build_embed(), view=self)
            except Exception as exc:
                await self.report_error("higher_lower_timeout", exc, guild_id=self.guild_id, user_id=self.user_id)
            finally:
                self.stop()
