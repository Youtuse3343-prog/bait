import unittest

from core.blackjack import (
    can_double,
    cards_can_split,
    dealer_should_hit,
    hand_value,
    is_blackjack,
    natural_blackjack_payout,
    new_shoe,
    rules_for_difficulty,
)


class BlackjackRulesTests(unittest.TestCase):
    def test_natural_blackjack(self):
        cards = [("A", "♠"), ("K", "♥")]
        self.assertEqual(hand_value(cards), (21, True))
        self.assertTrue(is_blackjack(cards))

    def test_multiple_aces(self):
        self.assertEqual(hand_value([("A", "♠"), ("A", "♥"), ("9", "♦")]), (21, True))
        self.assertEqual(hand_value([("A", "♠"), ("A", "♥"), ("9", "♦"), ("K", "♣")]), (21, False))

    def test_three_card_twenty_one_is_not_natural(self):
        self.assertFalse(is_blackjack([("7", "♠"), ("7", "♥"), ("7", "♦")]))

    def test_eight_deck_shoe(self):
        shoe = new_shoe(8)
        self.assertEqual(len(shoe), 416)
        self.assertEqual(len(set(shoe)), 52)

    def test_ten_value_cards_can_split(self):
        self.assertTrue(cards_can_split([("K", "♠"), ("10", "♥")]))
        self.assertFalse(cards_can_split([("K", "♠"), ("9", "♥")]))

    def test_hard_preset(self):
        rules = rules_for_difficulty("hard")
        self.assertEqual(rules.decks, 8)
        self.assertTrue(rules.dealer_hits_soft_17)
        self.assertEqual(rules.payout_label, "6:5")
        self.assertFalse(rules.allow_surrender)
        self.assertEqual(rules.double_rule, "9-11")

    def test_soft_seventeen_changes_by_table(self):
        soft_17 = [("A", "♠"), ("6", "♥")]
        self.assertTrue(dealer_should_hit(soft_17, rules_for_difficulty("hard")))
        self.assertFalse(dealer_should_hit(soft_17, rules_for_difficulty("casual")))

    def test_double_restrictions(self):
        self.assertTrue(can_double([("4", "♠"), ("5", "♥")], "9-11"))
        self.assertFalse(can_double([("8", "♠"), ("5", "♥")], "9-11"))

    def test_payout_presets(self):
        self.assertEqual(natural_blackjack_payout(100, rules_for_difficulty("hard")), 220)
        self.assertEqual(natural_blackjack_payout(100, rules_for_difficulty("casual")), 250)


if __name__ == "__main__":
    unittest.main()
