from __future__ import annotations

import json
from pathlib import Path

from beancount_importer.rules.models import CategorizationRule


def load_rules(path: Path) -> list[CategorizationRule]:
    """Load rules from a JSON file. Returns empty list if file doesn't exist."""
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [CategorizationRule.model_validate(item) for item in data]


def save_rules(rules: list[CategorizationRule], path: Path) -> None:
    """Persist rules to a JSON file, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([r.model_dump() for r in rules], f, indent=2, ensure_ascii=False)
        f.write("\n")
