"""Third trivial smoke test confirming the loop test suite imports and runs."""

from __future__ import annotations

import unittest


class LoopSmoke3Tests(unittest.TestCase):
    def test_smoke(self) -> None:
        self.assertEqual(2 * 3, 6)


if __name__ == "__main__":
    unittest.main()
