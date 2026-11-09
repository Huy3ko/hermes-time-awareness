"""Test-Isolation: Hermes-Stubs dürfen nicht in die nächste Testdatei lecken.

Befund 09.11.2026: `test_time_awareness.py` legt `sys.modules["hermes_time"]`
mit einem Stub an. Ohne Aufräumen sieht die danach laufende Datei diesen Stub
statt des echten Fallbacks — und ein Zeitzonen-Test schlägt fehl, obwohl der
Code stimmt. Isoliert lief die Datei grün, im Verbund nicht.

Deshalb: nach jedem Test die Hermes-Modulnamen aus `sys.modules` entfernen.
"""

import sys

import pytest

_STUB_NAMES = (
    "hermes_constants",
    "hermes_time",
    "hermes_cli",
    "hermes_cli.config",
)


@pytest.fixture(autouse=True)
def _clean_hermes_stubs():
    yield
    for name in _STUB_NAMES:
        sys.modules.pop(name, None)
