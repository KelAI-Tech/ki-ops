"""KOTL enums."""

from __future__ import annotations

from enum import Enum


class OrderStatus(str, Enum):
    OPEN = "open"
    PARTIAL = "partial"
    DONE = "done"
    CANCELLED = "cancelled"
