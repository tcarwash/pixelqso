"""Allocate card IDs without reusing IDs in the local transmit library."""
import json
import secrets
from pathlib import Path


def allocate_card_id(data_dir, reserved=()):
    used = {int(value) & 0xffff for value in reserved}
    for folder in ("cards", "sent-cards"):
        for path in (Path(data_dir) / folder).glob("*.json"):
            try:
                card = json.loads(path.read_text())
                used.add(int(card.get("card_id", 0xC0DE)) & 0xffff)
            except (ValueError, TypeError, AttributeError):
                continue
    start = secrets.randbelow(65536)
    for offset in range(65536):
        candidate = (start + offset) & 0xffff
        if candidate not in used:
            return candidate
    raise ValueError("All card IDs are in use; archive unused cards before creating another.")
