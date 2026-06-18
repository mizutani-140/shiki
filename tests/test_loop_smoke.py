"""Trivial smoke test: confirms the test suite imports and runs."""

from __future__ import annotations

import unittest


class LoopSmokeTests(unittest.TestCase):
    def test_smoke(self) -> None:
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
