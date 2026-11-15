from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> Path:
    return FIXTURES / f"{name}.logicx"


def all_fixtures() -> list[Path]:
    return sorted(FIXTURES.glob("*.logicx"))
