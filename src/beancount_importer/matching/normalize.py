from __future__ import annotations

import re
import unicodedata


def normalize_text(text: str) -> str:
    """NFKD → ASCII → lowercase → collapse whitespace."""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_bytes = nfkd.encode("ascii", errors="ignore")
    lowered = ascii_bytes.decode("ascii").lower()
    return re.sub(r"\s+", " ", lowered).strip()
