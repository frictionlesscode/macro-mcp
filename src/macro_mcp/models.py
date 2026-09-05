"""Return shapes and controlled vocabularies.

Every enum here is also enforced as a CHECK constraint in the schema. The duplication is
deliberate: the DB is the last line of defence against an LLM inventing a value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from datetime import date as Date, datetime
from typing import Any
from zoneinfo import ZoneInfo

# --- controlled vocabularies -------------------------------------------------

MEALS = ("breakfast", "lunch", "dinner", "snack", "other")

#: Where a number came from. Ordered best-to-worst; the calibration harness cares about
#: this distinction when scoring estimate accuracy.
SOURCES = ("label", "barcode", "library", "estimate")

#: Coarse buckets rather than a float. An LLM sets these far more consistently than it
#: sets "0.73", and a fake-precise number would imply calibration we do not have.
CONFIDENCES = ("high", "medium", "low")

#: ``unlogged`` is the absence of data, not a zero-intake day. Only ``complete`` days are
#: eligible for trend statistics — see SPEC.md, "A day with no logged food is unknown".
DAY_STATUSES = ("complete", "partial", "unlogged")

GOAL_MODES = ("cut", "bulk", "maintain")
STOP_METRICS = ("weight", "bodyfat", "date", "none")
GOAL_STATUSES = ("active", "met", "superseded", "abandoned")

PROPOSAL_KINDS = ("target", "transition", "reconciliation")
PROPOSAL_STATUSES = ("pending", "accepted", "declined")

BODY_COMP_METHODS = ("scale", "calipers", "dexa", "estimate")

PHOTO_ANGLES = ("front", "side", "back")

MACRO_FIELDS = ("kcal", "protein_g", "carb_g", "fat_g", "fiber_g")


def tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TZ") or "America/New_York")


def now() -> datetime:
    return datetime.now(tz())


def today() -> Date:
    """Current local date. The day boundary is midnight — locked in SPEC.md."""
    return now().date()


def iso(value: datetime | Date) -> str:
    return value.isoformat()


class ValidationError(ValueError):
    """Raised when input violates a controlled vocabulary or a numeric invariant."""


def require(value: Any, allowed: tuple[str, ...], label: str) -> str:
    if value not in allowed:
        raise ValidationError(
            f"{label} must be one of {', '.join(allowed)}; got {value!r}"
        )
    return value


# --- macro arithmetic --------------------------------------------------------


@dataclass(frozen=True)
class Macros:
    kcal: float = 0.0
    protein_g: float = 0.0
    carb_g: float = 0.0
    fat_g: float = 0.0
    fiber_g: float = 0.0

    def __add__(self, other: "Macros") -> "Macros":
        return Macros(*(getattr(self, f) + getattr(other, f) for f in MACRO_FIELDS))

    def scaled(self, factor: float) -> "Macros":
        return Macros(*(getattr(self, f) * factor for f in MACRO_FIELDS))

    def rounded(self, places: int = 1) -> "Macros":
        return Macros(*(round(getattr(self, f), places) for f in MACRO_FIELDS))

    def as_dict(self) -> dict[str, float]:
        return {f: round(getattr(self, f), 1) for f in MACRO_FIELDS}

    @classmethod
    def from_row(cls, row: Any) -> "Macros":
        return cls(*(float(row[f] or 0.0) for f in MACRO_FIELDS))

    def validate(self) -> "Macros":
        for f in MACRO_FIELDS:
            v = getattr(self, f)
            if v < 0:
                raise ValidationError(f"{f} cannot be negative; got {v}")
        return self

    def implied_kcal(self) -> float:
        """Atwater estimate from the macros alone.

        Used only to *report* a mismatch against the stated calories, never to correct it —
        silently rewriting a user's number would violate the no-fabrication rule.
        """
        return self.protein_g * 4 + self.carb_g * 4 + self.fat_g * 9


@dataclass
class FoodItem:
    """One component of a logged meal."""

    name: str
    kcal: float
    protein_g: float
    carb_g: float
    fat_g: float
    fiber_g: float = 0.0
    qty: float | None = None
    unit: str | None = None
    source: str = "estimate"
    confidence: str = "medium"
    library_food_id: int | None = None

    def validate(self) -> "FoodItem":
        if not self.name or not self.name.strip():
            raise ValidationError("item name is required")
        require(self.source, SOURCES, "source")
        require(self.confidence, CONFIDENCES, "confidence")
        self.macros.validate()
        if self.qty is not None and self.qty <= 0:
            raise ValidationError(f"qty must be positive; got {self.qty}")
        return self

    @property
    def macros(self) -> Macros:
        return Macros(
            self.kcal, self.protein_g, self.carb_g, self.fat_g, self.fiber_g
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FoodItem":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        unknown = set(d) - set(known)
        if unknown:
            raise ValidationError(
                f"unknown item field(s): {', '.join(sorted(unknown))}"
            )
        return cls(**known).validate()


@dataclass
class DayTotals:
    day: str
    status: str
    actual: Macros = field(default_factory=Macros)
    planned: Macros = field(default_factory=Macros)
    entry_count: int = 0
    planned_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "day": self.day,
            "status": self.status,
            "totals": self.actual.as_dict(),
            "entry_count": self.entry_count,
        }
        if self.planned_count:
            out["planned_totals"] = self.planned.as_dict()
            out["planned_count"] = self.planned_count
        return out


def to_dict(obj: Any) -> dict[str, Any]:
    return asdict(obj)
