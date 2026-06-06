"""P2.3.1 — ID allocation uniqueness and format for shiki_state.new_control_id."""

from __future__ import annotations

import re
import unittest

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_state import new_control_id


ID_PATTERN = re.compile(
    r"^(?P<prefix>[A-Z]+(?:-[A-Z0-9]+)*)-"
    r"\d{8}T\d{6}\d{6}Z-"
    r"[0-9a-f]{8}$"
)


class IdAllocationTests(unittest.TestCase):
    def test_format_matches_contract(self) -> None:
        control_id = new_control_id("L")
        match = ID_PATTERN.match(control_id)
        self.assertIsNotNone(match, f"unexpected id format: {control_id}")
        self.assertEqual(match.group("prefix"), "L")

    def test_prefix_is_preserved(self) -> None:
        for prefix in ("L", "T", "G", "RP"):
            self.assertTrue(new_control_id(prefix).startswith(f"{prefix}-"))

    def test_ids_are_unique(self) -> None:
        generated = {new_control_id("L") for _ in range(2000)}
        self.assertEqual(len(generated), 2000)

    def test_ids_are_filename_safe(self) -> None:
        control_id = new_control_id("T")
        self.assertNotIn("/", control_id)
        self.assertNotIn(" ", control_id)
        self.assertNotIn(":", control_id)

    def test_invalid_prefixes_rejected(self) -> None:
        for bad in ("", "l", "L_X", "L X", "lower", "L-x", "RP-2"):
            with self.assertRaises(ValueError):
                new_control_id(bad)

    def test_compound_uppercase_prefix_allowed(self) -> None:
        control_id = new_control_id("RP-A2")
        self.assertTrue(control_id.startswith("RP-A2-"))


if __name__ == "__main__":
    unittest.main()
