"""T-20260617T020914950053Z-592052d8 — unit tests for the pure ``slugify`` helper.

These characterize the observable behavior of ``scripts/shiki_process.slugify``:
lowercasing, collapsing runs of non-alphanumeric characters to a single ``-``,
stripping leading/trailing separators, falling back to ``"task"`` for empty or
all-symbol input, and capping the result at 48 characters. They import the real
module and never modify it.
"""

from __future__ import annotations

import unittest

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_process import slugify


class SlugifyTests(unittest.TestCase):
    def test_basic_punctuation_and_case(self) -> None:
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_lowercases_input(self) -> None:
        self.assertEqual(slugify("FooBar"), "foobar")

    def test_collapses_runs_of_non_alphanumeric(self) -> None:
        for value in ("foo   bar", "foo___bar", "foo!!!@@@bar", "foo - bar"):
            result = slugify(value)
            self.assertEqual(result, "foo-bar", f"unexpected slug for {value!r}")
            self.assertNotIn("--", result)

    def test_strips_leading_and_trailing_separators(self) -> None:
        for value in ("  hello  ", "---hello---", "!hello!", "...hello..."):
            self.assertEqual(slugify(value), "hello", f"unexpected slug for {value!r}")

    def test_empty_input_returns_task(self) -> None:
        self.assertEqual(slugify(""), "task")

    def test_all_symbol_input_returns_task(self) -> None:
        for value in ("!!!", "@#$%", "   ", "---", "...,,,"):
            self.assertEqual(slugify(value), "task", f"unexpected slug for {value!r}")

    def test_truncates_to_48_characters(self) -> None:
        result = slugify("a" * 60)
        self.assertEqual(len(result), 48)
        self.assertEqual(result, "a" * 48)

    def test_long_mixed_input_capped_at_48_characters(self) -> None:
        value = "The quick brown fox jumps over the lazy dog again and again"
        result = slugify(value)
        self.assertLessEqual(len(result), 48)

    def test_short_input_not_truncated(self) -> None:
        self.assertEqual(slugify("short-slug"), "short-slug")


if __name__ == "__main__":
    unittest.main()
