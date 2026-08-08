import hashlib
import unittest

from core.casino_games import (
    FairRound,
    PLINKO_RISKS,
    PLINKO_ROWS,
    SLOT_MACHINES,
    mines_multiplier,
    parse_roulette_bet,
    plinko_theoretical_rtp,
    resolve_plinko,
    resolve_slots,
)


class CasinoMathTests(unittest.TestCase):
    @staticmethod
    def fair(seed: str = "test-seed", nonce: str = "round-1") -> FairRound:
        return FairRound(
            server_seed=seed,
            client_seed="123456789",
            nonce=nonce,
            commitment=hashlib.sha256(seed.encode("utf-8")).hexdigest(),
        )

    def test_commitment_matches_revealed_seed(self):
        fair = self.fair()
        self.assertEqual(hashlib.sha256(fair.server_seed.encode("utf-8")).hexdigest(), fair.commitment)

    def test_fair_stream_is_reproducible(self):
        first = self.fair()
        second = self.fair()
        self.assertEqual(
            [first.randbelow(37, label="roulette") for _ in range(50)],
            [second.randbelow(37, label="roulette") for _ in range(50)],
        )

    def test_plinko_tables_are_balanced(self):
        for risk in PLINKO_RISKS:
            for rows in PLINKO_ROWS:
                rtp = plinko_theoretical_rtp(risk, rows)
                self.assertGreaterEqual(rtp, 0.95)
                self.assertLessEqual(rtp, 0.97)

    def test_plinko_round_reproduces(self):
        first = resolve_plinko(self.fair(nonce="plinko"), bet_per_ball=25, balls=3, risk="medium", rows=10)
        second = resolve_plinko(self.fair(nonce="plinko"), bet_per_ball=25, balls=3, risk="medium", rows=10)
        self.assertEqual(first.payout, second.payout)
        self.assertEqual(first.metadata["outcomes"], second.metadata["outcomes"])

    def test_mines_multiplier_is_probability_derived(self):
        self.assertAlmostEqual(mines_multiplier(20, 1, 1), (20 / 19) * 0.96)
        self.assertGreater(mines_multiplier(20, 5, 3), mines_multiplier(20, 5, 2))

    def test_roulette_parser_uses_european_wheel(self):
        self.assertEqual(parse_roulette_bet("red").payout_multiplier, 2)
        self.assertEqual(parse_roulette_bet("17").payout_multiplier, 36)
        self.assertEqual(parse_roulette_bet("0").payout_multiplier, 36)
        self.assertIsNone(parse_roulette_bet("00"))
        self.assertIsNone(parse_roulette_bet("37"))

    def test_slot_profiles_publish_balanced_rtp(self):
        for machine in SLOT_MACHINES.values():
            self.assertGreaterEqual(machine.rtp, 0.95)
            self.assertLessEqual(machine.rtp, 0.97)

    def test_slot_round_reproduces(self):
        machine = SLOT_MACHINES["vault"]
        first = resolve_slots(self.fair(nonce="slots"), bet=100, machine=machine)
        second = resolve_slots(self.fair(nonce="slots"), bet=100, machine=machine)
        self.assertEqual(first.payout, second.payout)
        self.assertEqual(first.metadata["grid"], second.metadata["grid"])


if __name__ == "__main__":
    unittest.main()
