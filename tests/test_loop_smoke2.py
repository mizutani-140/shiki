"""Second trivial smoke test confirming the loop test suite imports and runs."""

from __future__ import annotations

import unittest


class LoopSmoke2Tests(unittest.TestCase):
    def test_smoke(self) -> None:
        self.assertEqual(1 + 1, 2)


if __name__ == "__main__":
    unittest.main()
