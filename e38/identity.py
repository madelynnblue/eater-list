"""Restaurant identity: name normalisation and alias resolution.

Eater's URL anchors are unreliable — it used placeholder ``article-N`` anchors
through 2024-25 and re-slugged entries constantly — so identity is keyed on the
display name. Normalisation strips case, accents and punctuation; a small,
curated alias map (``aliases.json``) joins entries that Eater genuinely renamed
between editions.
"""
from __future__ import annotations

import json
import re
import unicodedata


def normalize(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_aliases(path: str) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def resolver(aliases: dict[str, str]):
    """Return a function mapping a raw name to its canonical normalised key."""

    def canonical(name: str) -> str:
        key = normalize(name)
        seen = set()
        while key in aliases and key not in seen:
            seen.add(key)
            key = aliases[key]
        return key

    return canonical


def slugify(name: str) -> str:
    return normalize(name).replace(" ", "-") or "unknown"


def entry_keys(payload: dict, canonical) -> list[str]:
    """Canonical keys for one capture, de-duplicated, order preserved."""
    keys: list[str] = []
    for item in payload.get("items", []):
        key = canonical(item.get("name", ""))
        if key and key not in keys:
            keys.append(key)
    return keys
