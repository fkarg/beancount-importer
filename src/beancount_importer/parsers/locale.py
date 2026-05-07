from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation


def parse_date(value: str, formats: list[str]) -> date:
    """Try each format in order; raise ValueError if none match."""
    from datetime import datetime

    value = value.strip()
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date {value!r} with formats {formats}")


_CURRENCY_SYMBOLS = ("€", "$", "£", "¥", "EUR", "USD", "GBP", "CHF")


def _strip_amount_decoration(value: str) -> tuple[str, bool]:
    """Strip whitespace, currency symbols, and a leading sign from `value`.

    Returns (cleaned_digits, negative). Zinia exports look like `+184,90 €`
    and `-43,22 €` — same shape as bank CSVs except for the trailing currency
    symbol — so we centralize the decoration handling here rather than push
    it into each callsite.
    """
    s = value.strip().replace("\xa0", "")
    for sym in _CURRENCY_SYMBOLS:
        if s.endswith(sym):
            s = s[: -len(sym)].rstrip()
        elif s.startswith(sym):
            s = s[len(sym) :].lstrip()
    if not s:
        raise ValueError("Empty amount string")
    negative = s.startswith("-")
    if negative or s.startswith("+"):
        s = s[1:].strip()
    return s, negative


def parse_amount_de(value: str) -> Decimal:
    """Parse German locale amount: 1.234,56 → Decimal('1234.56')."""
    s, negative = _strip_amount_decoration(value)
    # Remove thousand separators (dots), replace decimal comma with dot
    s = s.replace(".", "").replace(",", ".")
    try:
        result = Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"Cannot parse German amount {value!r}") from e
    return -result if negative else result


def parse_amount_en(value: str) -> Decimal:
    """Parse English locale amount: 1,234.56 → Decimal('1234.56')."""
    s, negative = _strip_amount_decoration(value)
    # Remove thousand separators (commas)
    s = s.replace(",", "")
    try:
        result = Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"Cannot parse English amount {value!r}") from e
    return -result if negative else result


def parse_amount(value: str, locale: str) -> Decimal:
    if locale == "de":
        return parse_amount_de(value)
    return parse_amount_en(value)
