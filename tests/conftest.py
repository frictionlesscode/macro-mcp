import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for sub in ("src", "scripts"):
    path = str(ROOT / sub)
    if path not in sys.path:
        sys.path.insert(0, path)

from macro_mcp.store import open_db  # noqa: E402


@pytest.fixture
def db():
    conn = open_db(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def oats(db):
    """A label-sourced library food with a known serving mass."""
    from macro_mcp import foods

    return foods.save_food(
        db, name="Oats", serving_desc="40 g dry", serving_g=40.0,
        kcal=150, protein_g=5, carb_g=27, fat_g=2.5, fiber_g=4, source="label",
    )["id"]
