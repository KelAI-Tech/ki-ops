"""Hashes for reproducible pre-trade artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from pathlib import Path
from typing import Any, Mapping

from ki_ops.config import RiskManagementSettings


def sha256_file(path: str | Path | None) -> str | None:
    if path is None:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def settings_hash(settings: RiskManagementSettings) -> str:
    payload = {f.name: str(getattr(settings, f.name)) for f in fields(settings)}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def input_hashes(paths: Mapping[str, str | Path | None]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, path in paths.items():
        digest = sha256_file(path)
        if digest is not None:
            out[name] = digest
    return out


def write_json_out(path: str | Path, payload: Mapping[str, Any]) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, default=str)
    needle = '\n  "output":'
    if needle in body:
        body = body.replace(needle, "\n" + needle, 1)
    dest.write_text(body + "\n", encoding="utf-8")
    return dest
