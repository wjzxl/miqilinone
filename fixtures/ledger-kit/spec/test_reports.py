import unittest

from ledgerkit.reports import render


class TestReports(unittest.TestCase):
    def test_render(self):
        text = render({"b": 5, "a": 3})
        self.assertEqual(text, "a: 3\nb: 5\nTOTAL=8")


if __name__ == "__main__":
    unittest.main()
