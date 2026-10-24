"""Числа текста в едином виде — для проверки «нет чисел вне источников»."""

from __future__ import annotations

import re

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def numbers_in(text: str) -> set[str]:
    """Числа текста в едином виде: запятая → точка, без ведущих нулей."""
    result = set()
    for number in _NUMBER_RE.findall(text):
        whole, _, fraction = number.replace(",", ".").partition(".")
        whole = whole.lstrip("0") or "0"
        fraction = fraction.rstrip("0")
        result.add(f"{whole}.{fraction}" if fraction else whole)
    return result
