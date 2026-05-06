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


def parse_amount_de(value: str) -> Decimal:
    """Parse German locale amount: 1.234,56 → Decimal('1234.56')."""
    value = value.strip().replace("\xa0", "")
    if not value:
        raise ValueError("Empty amount string")
    negative = value.startswith("-")
    if negative:
        value = value[1:].strip()
    # Remove thousand separators (dots), replace decimal comma with dot
    value = value.replace(".", "").replace(",", ".")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"Cannot parse German amount {value!r}")
    return -result if negative else result


def parse_amount_en(value: str) -> Decimal:
    """Parse English locale amount: 1,234.56 → Decimal('1234.56')."""
    value = value.strip().replace("\xa0", "")
    if not value:
        raise ValueError("Empty amount string")
    negative = value.startswith("-")
    if negative:
        value = value[1:].strip()
    # Remove thousand separators (commas)
    value = value.replace(",", "")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"Cannot parse English amount {value!r}")
    return -result if negative else result


def parse_amount(value: str, locale: str) -> Decimal:
    if locale == "de":
        return parse_amount_de(value)
    return parse_amount_en(value)
