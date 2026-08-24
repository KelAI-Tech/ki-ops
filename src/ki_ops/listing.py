"""Security-master listing status (tradability)."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Mapping

ACTIVE_STATUS_CODES = {"A", ""}


def parse_iso_date(raw: str | None) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None
    return date.fromisoformat(text[:10])


def parse_is_active(raw: str | None) -> bool:
    s = (raw or "").strip().lower()
    return s in {"true", "1", "yes", "y", "t"}


@dataclass(frozen=True)
class ListingStatus:
    infocode: str
    ticker: str
    status_code: str
    is_active: bool
    delist_date: date | None

    def is_tradable(self, as_of: date) -> bool:
        if not self.is_active:
            return False
        code = self.status_code.strip().upper()
        if code not in ACTIVE_STATUS_CODES:
            return False
        if self.delist_date is not None and self.delist_date <= as_of:
            return False
        return True

    def block_reason(self, as_of: date) -> str | None:
        if self.is_tradable(as_of):
            return None
        parts = []
        if not self.is_active:
            parts.append("ISACTIVE=false")
        code = self.status_code.strip().upper()
        if code and code not in ACTIVE_STATUS_CODES:
            parts.append(f"STATUSCODE={code}")
        if self.delist_date is not None and self.delist_date <= as_of:
            parts.append(f"DELISTDATE={self.delist_date.isoformat()}")
        return ", ".join(parts) or "not tradable"


def listing_csv_has_status_fields(path: str | Path) -> bool:
    path = Path(path)
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        names = {str(k).strip().lower() for k in (reader.fieldnames or []) if k}
    return bool(names & {"isactive", "statuscode", "delistdate"})


def load_listing_status(path: str | Path) -> dict[str, ListingStatus]:
    """Load ``{INFOCODE: ListingStatus}`` from a SECURITY_MASTER_DT-style CSV."""
    path = Path(path)
    out: dict[str, ListingStatus] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return {}
        fields = {str(k).strip().lower(): k for k in reader.fieldnames if k}
        id_key = fields.get("infocode") or fields.get("security_id")
        if not id_key:
            raise ValueError(f"Need INFOCODE column in {path}")
        tic_key = fields.get("ticker") or fields.get("symbol")
        status_key = fields.get("statuscode")
        active_key = fields.get("isactive")
        delist_key = fields.get("delistdate")
        for raw in reader:
            sid = (raw.get(id_key) or "").strip()
            if not sid:
                continue
            out[sid] = ListingStatus(
                infocode=sid,
                ticker=((raw.get(tic_key) or "").strip().upper() if tic_key else ""),
                status_code=(raw.get(status_key) or "").strip() if status_key else "",
                is_active=parse_is_active(raw.get(active_key) if active_key else "true"),
                delist_date=parse_iso_date(raw.get(delist_key) if delist_key else None),
            )
    return out


def load_infocode_ticker_map_from_listing(master: Mapping[str, ListingStatus]) -> dict[str, str]:
    return {sid: rec.ticker for sid, rec in master.items() if rec.ticker}


_ADV_COLUMNS = ("adv20_adj", "adv20", "adv63_adj", "adv63")


def load_adv_map(path: str | Path) -> dict[str, Decimal]:
    """Load ``{INFOCODE: ADV}`` from a BASE_DATA_US_DT-style snapshot.

    Prefers split-adjusted 20-day ADV, then raw ADV20, then 63-day variants.
    """
    path = Path(path)
    out: dict[str, Decimal] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return {}
        fields = {str(k).strip().lower(): k for k in reader.fieldnames if k}
        id_key = fields.get("infocode") or fields.get("security_id")
        if not id_key:
            raise ValueError(f"Need INFOCODE column in {path}")
        adv_key = next((fields[c] for c in _ADV_COLUMNS if c in fields), None)
        if not adv_key:
            raise ValueError(f"Need an ADV column ({', '.join(_ADV_COLUMNS)}) in {path}")
        for raw in reader:
            sid = (raw.get(id_key) or "").strip()
            val = (raw.get(adv_key) or "").strip()
            if not sid or not val:
                continue
            adv = Decimal(val)
            if adv > 0:
                out[sid] = adv
    return out
