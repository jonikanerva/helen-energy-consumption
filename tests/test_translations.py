"""Structural key-parity checks for the translation files.

hassfest validates each translation file's schema but not key parity between
locales — a key missing from a locale silently falls back to English at
runtime. This guards the project rule that every locale mirrors en.json 1:1.
"""

from __future__ import annotations

import json
from pathlib import Path

_TRANSLATIONS = (
    Path(__file__).parent.parent
    / "custom_components"
    / "helen_energy_consumption"
    / "translations"
)


def _key_paths(node: object, prefix: str = "") -> set[str]:
    """Recursively collect the dotted key paths of a decoded JSON value."""
    if not isinstance(node, dict):
        return {prefix}
    paths: set[str] = set()
    for key, value in node.items():
        paths |= _key_paths(value, f"{prefix}.{key}" if prefix else key)
    return paths


def _load_paths(locale: Path) -> set[str]:
    """Load a translation file and return its key-path set."""
    return _key_paths(json.loads(locale.read_text(encoding="utf-8")))


def test_locales_mirror_en_json_key_paths() -> None:
    """Every non-English locale has exactly the key paths of en.json."""
    en_paths = _load_paths(_TRANSLATIONS / "en.json")
    assert en_paths, "en.json produced no key paths"

    others = sorted(p for p in _TRANSLATIONS.glob("*.json") if p.name != "en.json")
    assert others, "expected at least one non-English locale file"

    for locale in others:
        locale_paths = _load_paths(locale)
        missing = sorted(en_paths - locale_paths)
        extra = sorted(locale_paths - en_paths)
        assert locale_paths == en_paths, (
            f"{locale.name} diverges from en.json: missing={missing} extra={extra}"
        )
