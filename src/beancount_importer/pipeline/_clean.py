"""Pre-match cleaners for noisy bank exports.

These run after parsing but before rule/dedup resolution. They reshape
the SourceTransaction so the merge-prompt display and any downstream
text-similarity scoring see a tidied payee + description without the
bank-specific transport noise. Originals are preserved in
`raw_data['_original_*']` so the UI can fall back to them when
needed.

Today this module only knows about SPK-shaped PayPal rows (the most
common case in the user's workflow). New bank-specific cleaners can
be added incrementally as their export shapes appear.
"""

from __future__ import annotations

import re

from beancount_importer.models import SourceTransaction


# Trailing transaction-type suffixes the SPK CSV appends to the
# Verwendungszweck column. Stripped at the end of the description.
_TRAILING_SUFFIXES = re.compile(
    r"\s*\|\s*(?:FOLGE)?LASTSCHRIFT\s*$"
    r"|\s*\|\s*GUTSCHRIFT\s*$"
    r"|\s*\|\s*ONLINE[-\s]UEBERWEISUNG\s*$"
    r"|\s*\|\s*DAUERAUFTRAG\s*$",
    re.IGNORECASE,
)

# SPK-style PayPal description prefixes:
#   "1234567890/PP.1234.PP/. Merchant Foo, ..."
#   "1234567890/. Merchant Foo, ..."
_PAYPAL_PREFIX = re.compile(
    r"^\d+[/\s]PP\.\d+\.PP[/\s]\.\s*,?\s*"
    r"|^\d+[/\s]\.\s*",
)

# Merchant-extraction patterns. SPK's PayPal rows often spell out
# "Ihr Einkauf bei <Merchant>" — pull the merchant out so the
# merge-prompt sees a clean payee.
_MERCHANT_RE = re.compile(r"Ihr Einkauf bei\s+(.*)", re.IGNORECASE)
_MERCHANT_TAIL = re.compile(r",\s*Ihr Einkauf bei.*$", re.IGNORECASE)


# Card-acquirer / payment-facilitator descriptor prefixes. These PSPs
# prepend "<NAME> *" (whitespace and digit-runs vary) to the real merchant
# in the payee field — e.g. "SumUp  *Donaladn Frank", "SQ *ARCH CAFE",
# "UZR*TRITON Tauchsports". The prefix carries no information worth keeping,
# so we strip it and surface the merchant. PayPal is handled separately by
# `clean_paypal_noise`.
#
# Deliberately EXCLUDED: brand-owned prefixes where the leading token *is*
# the merchant identity and stripping makes the payee worse —
# "UBER *EATS" → "EATS", "FRAMEWORK*..." (Framework laptops), "SNACK* TACOS".
_ACQUIRER_PREFIXES = (
    "sumup", "sq", "uzr", "nya", "spc", "com", "lsp", "fawry",
    "iz", "zettle", "tst", "stripe", "wpy",
)

_ACQUIRER_PREFIX_RE = re.compile(
    r"^(?:" + "|".join(_ACQUIRER_PREFIXES) + r")\s*\*+\s*",
    re.IGNORECASE,
)


def clean_acquirer_prefix(txn: SourceTransaction) -> SourceTransaction:
    """Strip a leading payment-facilitator descriptor from the payee.

    Fires only when the payee starts with a known facilitator prefix
    (`SumUp *`, `SQ *`, `UZR*`, ...) AND a non-empty merchant remains after
    stripping — a payee that is *only* the prefix is left untouched. The
    original payee is preserved in `raw_data['_original_payee']`.

    The required `*` anchors the match, so merchants that merely begin with
    one of these letter-runs ("Commerzbank", "Square Enix") are unaffected.
    """
    payee = txn.payee
    if not payee:
        return txn
    stripped = _ACQUIRER_PREFIX_RE.sub("", payee).strip()
    if stripped == payee or not stripped:
        return txn
    return txn.model_copy(
        update={
            "payee": stripped,
            "raw_data": {**txn.raw_data, "_original_payee": payee},
        }
    )


# Leading "DATUM <date>, <time> UHR <TYPE>" transport segment SPK prepends to
# the Verwendungszweck of online transfers / standing orders — e.g.
#   "DATUM 03.05.2025, 02.09 UHR ONLINE-UEBERWEISUNG Miete Mai"
# The date/time duplicate the booking date and the transaction-type token
# duplicates the Buchungstext column, so the whole segment is pure narration
# noise. The type token is optional: if it isn't one we recognise we still drop
# the "DATUM ... UHR" part but leave the following text as narration.
_SPK_TRANSFER_PREFIX = re.compile(
    r"^\s*DATUM\s+\d{2}\.\d{2}\.\d{4},\s*\d{2}\.\d{2}\s+UHR"
    r"(?:\s+(?:ONLINE[-\s]UEBERWEISUNG|UEBERWEISUNG"
    r"|DAUERAUFTRAG|FOLGELASTSCHRIFT|LASTSCHRIFT|GUTSCHRIFT))?"
    r"\s*",
    re.IGNORECASE,
)


def clean_spk_transfer_prefix(txn: SourceTransaction) -> SourceTransaction:
    """Strip SPK's "DATUM <date>, <time> UHR <TYPE>" narration prefix.

    Fires only when the description opens with that transport segment. The
    original description is preserved in `raw_data['_original_description']`.
    Self-anchoring on the distinctive shape, so it needs no bank gate.
    """
    desc = txn.description
    if not desc:
        return txn
    stripped = _SPK_TRANSFER_PREFIX.sub("", desc).strip()
    if stripped == desc:
        return txn
    return txn.model_copy(
        update={
            "description": stripped,
            "raw_data": {**txn.raw_data, "_original_description": desc},
        }
    )


def _looks_like_paypal(payee: str | None) -> bool:
    return bool(payee) and "paypal" in payee.lower()


def clean_paypal_noise(txn: SourceTransaction) -> SourceTransaction:
    """If `txn` looks like an SPK-style PayPal row, strip transport noise
    and surface the merchant. Otherwise return the txn unchanged.

    Heuristic: payee contains "paypal" (case-insensitive). The cleaner
    only fires on those rows. Originals are preserved in
    `raw_data['_original_payee']` and `raw_data['_original_description']`.

    Result shape:
      - merchant >= 3 chars: `payee=<merchant>`, `description='PayPal'`
      - otherwise:           `payee='PayPal'`, `description='PayPal'`

    Rationale: PayPal-funded rows in the SPK ledger almost always live
    alongside a matching PayPal CSV row; the cleaned text makes the
    cross-source match easier to recognise on both sides without
    requiring user intervention.
    """
    if not _looks_like_paypal(txn.payee):
        return txn

    raw_payee = txn.payee or ""
    raw_description = txn.description or ""

    cleaned_desc = _TRAILING_SUFFIXES.sub("", raw_description).strip()
    cleaned_desc = _PAYPAL_PREFIX.sub("", cleaned_desc).strip()

    merchant: str | None = None
    m = _MERCHANT_RE.search(cleaned_desc)
    if m:
        merchant = m.group(1).strip().rstrip(",")
    else:
        cleaned_desc = _MERCHANT_TAIL.sub("", cleaned_desc).strip()

    if merchant and len(merchant) >= 3:
        new_payee = merchant
        new_description = "PayPal"
    else:
        new_payee = "PayPal"
        new_description = "PayPal"

    return txn.model_copy(
        update={
            "payee": new_payee,
            "description": new_description,
            "raw_data": {
                **txn.raw_data,
                "_original_payee": raw_payee,
                "_original_description": raw_description,
            },
        }
    )
