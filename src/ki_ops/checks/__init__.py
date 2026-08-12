"""Pre-trade check types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    BLOCK = "BLOCK"
    WARN = "WARN"


@dataclass(frozen=True)
class CheckViolation:
    code: str
    message: str
    severity: Severity = Severity.BLOCK
    symbol: str | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity.value,
            "symbol": self.symbol,
        }


def block(code: str, message: str, symbol: str | None = None) -> CheckViolation:
    return CheckViolation(code, message, Severity.BLOCK, symbol)


def warn(code: str, message: str, symbol: str | None = None) -> CheckViolation:
    return CheckViolation(code, message, Severity.WARN, symbol)
