"""Smoke tests for the integration manifest and HACS metadata.

Guards the invariants STACK.md cares about: the Helen dependency is pinned
exactly, the domain matches the constant, and the HACS minimum is declared.
"""

from __future__ import annotations

import json
from pathlib import Path

_COMPONENT = (
    Path(__file__).parent.parent
    / "custom_components"
    / "helen_energy_consumption"
)


def _load(name: str) -> dict:
    return json.loads((_COMPONENT / name).read_text())


def test_manifest_domain_matches_constant() -> None:
    from custom_components.helen_energy_consumption import const

    assert _load("manifest.json")["domain"] == const.DOMAIN


def test_helen_dependency_is_pinned_exactly() -> None:
    requirements = _load("manifest.json")["requirements"]
    assert requirements == ["oma-helen-cli==1.8.0"]


def test_hacs_declares_homeassistant_minimum() -> None:
    hacs = json.loads((_COMPONENT.parent.parent / "hacs.json").read_text())
    assert hacs["homeassistant"] == "2025.1.0"
