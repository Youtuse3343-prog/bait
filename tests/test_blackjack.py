import unittest

from core.blackjack import hand_value, is_blackjack, new_shoe


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

    def test_six_deck_shoe(self):
        shoe = new_shoe()
        self.assertEqual(len(shoe), 312)
        self.assertEqual(len(set(shoe)), 52)


if __name__ == "__main__":
    unittest.main()
