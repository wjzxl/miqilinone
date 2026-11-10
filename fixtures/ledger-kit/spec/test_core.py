import unittest

from ledgerkit.core import balance, total


class TestCore(unittest.TestCase):
    def test_total(self):
        self.assertEqual(total([3, 7]), 10)

    def test_total_empty(self):
        self.assertEqual(total([]), 0)

    def test_balance(self):
        self.assertEqual(balance([("a", 2), ("b", 5), ("a", 3)]), {"a": 5, "b": 5})


if __name__ == "__main__":
    unittest.main()
