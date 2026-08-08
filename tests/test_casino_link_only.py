import ast
from pathlib import Path
import unittest


class CasinoRemovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).parents[1] / "bot.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_only_external_casino_command_remains(self):
        command_names = []
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                if not isinstance(decorator.func, ast.Attribute) or decorator.func.attr != "command":
                    continue
                for keyword in decorator.keywords:
                    if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                        command_names.append(keyword.value.value)
        removed = {"blackjack", "blackjack_rules", "plinko", "mines", "higher_lower", "slots", "roulette", "casino_rules", "balance", "daily", "casino_leaderboard"}
        self.assertFalse(removed.intersection(command_names))
        self.assertIn("casino", command_names)

    def test_exact_casino_url_is_present(self):
        self.assertIn('CASINO_URL = "https://www.moealturej.com/casino"', self.source)

    def test_no_internal_casino_modules_are_imported(self):
        self.assertNotIn("core.blackjack", self.source)
        self.assertNotIn("core.casino_games", self.source)


if __name__ == "__main__":
    unittest.main()
