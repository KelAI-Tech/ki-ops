"""Pre-submit security resolution via the Flex ``SecurityService`` (symbol lookup).

FlexTrade's recommended workflow (relayed 2026-09-08): **check every order
symbol through the SecurityService lookup before creating orders**, so payloads
always carry an instrument that exists in the Flex security master. Preferred
identifiers: SEDOL for equities, or the Flex symbol itself (``AAPL.US``) —
both are unique in Flex. Securities missing from the master must be sent to
FlexTrade as a list so they can seed them (see the unresolved CSV the submit
path writes).

Live-verified behavior (UAT, 2026-09-08):

- ``LookupSecurityRequest`` has only ``symbol`` (string) and ``flexSecurityId``
  (int32) — there is **no identifier-typed request field**. The ``symbol``
  string is matched against *every* identifier in the master, so a **SEDOL
  passed as the symbol resolves** (``2046251`` → ``AAPL.US``), as does the
  dotted class-share ticker alias (``BF.B`` → canonical ``BF/B.US`` via the
  TICKER identifier). Undotted ds2 spellings (``BFB``, ``BFB.US``) do NOT
  resolve — hence the candidate fallback in :func:`resolve_flex_symbols`.
- ``BatchLookup`` returns one ``SecurityLookupResponse`` per request entry,
  **in request order** (misses come back with ``status.success=False``).
- The canonical payload symbol is ``response.security.commonData.symbol``
  (NOT ``response.security.symbol``).

SEDOL availability (checked 2026-09-08): neither the ds2 H5
(``s3://kelaidata/data/LSEG/Datastream2/ds2_data.h5`` — price/volume panels +
TICKER vocabulary only) nor the kelaidb MySQL instance (single ``kelai``
schema: alphas/backtests tables, no security master, no SEDOL column in any
schema) carries SEDOLs today. Resolution therefore works **symbol-only** by
default; pass ``sedols={ticker: sedol}`` once a SEDOL source exists and those
are tried first, per FlexTrade's preference.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from ki_ops.kotl.flex_live import (
    GRPC_OPTIONS,
    FlexConfig,
    FlexSdkMissingError,
    _import_grpc,
)

BATCH_CHUNK = 500

# SecurityIdentifierType enum values (Securities.proto) we care to name.
IDENTIFIER_TYPE_NAMES = {
    1: "SYMBOL",
    2: "TICKER",
    3: "CUSIP",
    4: "ISIN",
    5: "SEDOL",
    6: "RIC",
    7: "BLOOMBERG",
    15: "BLOOMBERG_UNIQUE_ID",
    16: "FIGI",
}

# Undotted class-share spelling in the ds2 vocabulary: base ticker + one class
# letter (BRKB, BFB, LGFA). Class letters restricted to the ones actually used
# for US share classes so plain tickers ending in other letters don't generate
# junk candidates. Only ever tried AFTER the plain TICKER.US lookup missed.
_UNDOTTED_CLASS = re.compile(r"^([A-Z]{2,7})([ABCK])$")

UNRESOLVED_CSV_FIELDS = ("ticker", "payload_symbol", "side", "quantity", "candidates_tried")


def _load_securities_sdk(sdk_path: str | Path | None = None):
    """Import ``API.Securities_pb2`` / ``API.Securities_pb2_grpc`` (lazy, local SDK)."""
    path = sdk_path or os.environ.get("KOTL_FLEX_SDK_PATH")
    if path:
        base = str(Path(path))
        for entry in (base, str(Path(base) / "API")):
            if entry not in sys.path:
                sys.path.insert(0, entry)
    try:
        import API.Securities_pb2 as Securities_pb2
        import API.Securities_pb2_grpc as SecurityServiceModule
    except ImportError as exc:
        raise FlexSdkMissingError(
            "Brooklyn SDK not importable (need API.Securities_pb2). Set "
            f"KOTL_FLEX_SDK_PATH to a local SDK sync. (import error: {exc})"
        ) from exc
    return Securities_pb2, SecurityServiceModule


def _open_channel(config: FlexConfig):
    grpc = _import_grpc()
    return grpc.insecure_channel(config.endpoint, options=GRPC_OPTIONS)


def _resolved_dict(query: str, response) -> dict[str, Any]:
    """One successful ``SecurityLookupResponse`` → plain resolved dict."""
    common = response.security.commonData
    identifiers: dict[str, str] = {}
    for ident in common.identifierList.identifier:
        type_no = int(ident.identifierType)
        name = IDENTIFIER_TYPE_NAMES.get(type_no, str(type_no))
        identifiers.setdefault(name, str(ident.identifier))
    return {
        "query": query,
        "flex_symbol": str(common.symbol),
        "flex_security_id": int(common.flexSecurityId),
        "description": str(getattr(common, "description", "") or ""),
        "security_type": str(getattr(common, "securityType", "") or ""),
        "exchange_mic": str(getattr(common, "exchangeMIC", "") or ""),
        "identifiers": identifiers,
    }


def lookup_security(config: FlexConfig, query: str) -> dict[str, Any] | None:
    """Single ``SecurityService.Lookup`` for *query* (symbol / SEDOL / any identifier).

    Returns the resolved dict (canonical ``flex_symbol`` =
    ``commonData.symbol``, ``flex_security_id``, identifier map) or ``None``
    when the master has no match.
    """
    Securities_pb2, SecurityServiceModule = _load_securities_sdk(config.sdk_path)
    channel = _open_channel(config)
    try:
        stub = SecurityServiceModule.SecurityServiceStub(channel)
        request = Securities_pb2.LookupSecurityRequest()
        request.symbol = str(query)
        for response in stub.Lookup(
            request, timeout=config.query_timeout, metadata=config.metadata
        ):
            if response.status.success:
                return _resolved_dict(str(query), response)
        return None
    finally:
        close = getattr(channel, "close", None)
        if close is not None:
            close()


def batch_lookup(
    config: FlexConfig,
    queries: Sequence[str],
    *,
    chunk_size: int = BATCH_CHUNK,
) -> list[dict[str, Any] | None]:
    """``SecurityService.BatchLookup`` for *queries* → per-query resolved dict or None.

    Responses come back in request order (verified live 2026-09-08); a count
    mismatch is a hard error rather than a silent misjoin.
    """
    if not queries:
        return []
    Securities_pb2, SecurityServiceModule = _load_securities_sdk(config.sdk_path)
    channel = _open_channel(config)
    out: list[dict[str, Any] | None] = []
    try:
        stub = SecurityServiceModule.SecurityServiceStub(channel)
        for start in range(0, len(queries), max(1, chunk_size)):
            chunk = list(queries[start : start + chunk_size])
            request = Securities_pb2.BatchLookupSecurityRequest()
            for query in chunk:
                entry = request.security.add()
                entry.symbol = str(query)
            responses = []
            for message in stub.BatchLookup(
                request, timeout=config.query_timeout, metadata=config.metadata
            ):
                responses.extend(message.response)
            if len(responses) != len(chunk):
                raise RuntimeError(
                    f"BatchLookup returned {len(responses)} responses for "
                    f"{len(chunk)} queries — cannot join responses to queries"
                )
            for query, response in zip(chunk, responses):
                out.append(
                    _resolved_dict(query, response) if response.status.success else None
                )
    finally:
        close = getattr(channel, "close", None)
        if close is not None:
            close()
    return out


def candidate_queries(
    ticker: str,
    *,
    sedol: str | None = None,
    suffix: str = ".US",
) -> list[tuple[str, str]]:
    """Ordered ``(resolved_via, query)`` lookup candidates for one book ticker.

    Order (per FlexTrade guidance + live UAT findings): SEDOL when available
    (unique, preferred), then the plain Flex symbol ``TICKER.US``, then the
    class-share spellings when the ticker looks like one — the dotted ticker
    alias (``BF.B``, resolves via the TICKER identifier) and the slash Flex
    symbol (``BF/B.US``).
    """
    ticker = str(ticker).strip().upper()
    candidates: list[tuple[str, str]] = []
    if sedol:
        candidates.append(("sedol", str(sedol).strip()))
    if "." in ticker:
        # Dotted class-share spelling (BF.B): the dotted string itself is the
        # TICKER identifier; TICKER.US would be junk ("BF.B.US").
        base, cls = ticker.rsplit(".", 1)
        candidates.append(("ticker_alias", ticker))
        if base and cls:
            candidates.append(("slash_symbol", f"{base}/{cls}{suffix}"))
        return candidates
    candidates.append(("symbol", f"{ticker}{suffix}"))
    match = _UNDOTTED_CLASS.match(ticker)
    if match:
        base, cls = match.groups()
        candidates.append(("ticker_alias", f"{base}.{cls}"))
        candidates.append(("slash_symbol", f"{base}/{cls}{suffix}"))
    return candidates


# ---------------------------------------------------------------------------
# Resolution cache (JSON under the data dir)
# ---------------------------------------------------------------------------

CACHE_FILENAME = "flex_symbols_cache.json"


def _load_cache(cache_path: str | Path | None) -> dict[str, dict[str, Any]]:
    if cache_path is None:
        return {}
    path = Path(cache_path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    return dict(entries) if isinstance(entries, dict) else {}


def _save_cache(cache_path: str | Path | None, entries: dict[str, dict[str, Any]]) -> None:
    if cache_path is None:
        return
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "entries": entries}, indent=1, sort_keys=True),
        encoding="utf-8",
    )


def resolve_flex_symbols(
    config: FlexConfig,
    tickers: Iterable[str],
    *,
    sedols: dict[str, str] | None = None,
    suffix: str = ".US",
    cache_path: str | Path | None = None,
    chunk_size: int = BATCH_CHUNK,
) -> tuple[dict[str, str], list[str], dict[str, dict[str, Any]]]:
    """Resolve book tickers to canonical Flex symbols via ``BatchLookup``.

    Candidates per ticker come from :func:`candidate_queries` (SEDOL first
    when *sedols* provides one, then ``TICKER.US``, then class-share
    spellings). Lookups run in candidate **rounds** — round 1 batches every
    ticker's first candidate, round 2 batches the misses' second candidate,
    and so on — so a full book costs a handful of round trips.

    Returns ``(resolved, unresolved, details)``:

    - *resolved*: ``{book_ticker: canonical flex symbol}`` (always the
      ``commonData.symbol`` echoed by the master, e.g. ``BFB → BF/B.US``);
    - *unresolved*: tickers absent from the Flex master under every candidate
      — the list to send FlexTrade so they seed the master;
    - *details*: per-ticker ``{flex_symbol, flex_security_id, resolved_via,
      query, tried}`` (unresolved tickers carry only ``tried``).

    When *cache_path* is given, prior resolutions are reused
    (``resolved_via="cache"``) and new ones appended; unresolved tickers are
    **never cached** so they are re-checked on every run.
    """
    sedols = sedols or {}
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in tickers:
        ticker = str(raw).strip().upper()
        if ticker and ticker not in seen:
            seen.add(ticker)
            ordered.append(ticker)

    cache = _load_cache(cache_path)
    resolved: dict[str, str] = {}
    details: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    for ticker in ordered:
        entry = cache.get(ticker)
        if entry and entry.get("flex_symbol"):
            resolved[ticker] = str(entry["flex_symbol"])
            details[ticker] = {**entry, "resolved_via": "cache"}
        else:
            pending.append(ticker)

    candidates = {
        ticker: candidate_queries(ticker, sedol=sedols.get(ticker), suffix=suffix)
        for ticker in pending
    }
    tried: dict[str, list[str]] = {ticker: [] for ticker in pending}

    round_no = 0
    while pending:
        batch = [
            (ticker, candidates[ticker][round_no])
            for ticker in pending
            if round_no < len(candidates[ticker])
        ]
        if not batch:
            break
        results = batch_lookup(
            config, [query for _, (_, query) in batch], chunk_size=chunk_size
        )
        still_pending = set(pending)
        now = datetime.now(timezone.utc).isoformat()
        for (ticker, (via, query)), result in zip(batch, results):
            tried[ticker].append(query)
            if result is None:
                continue
            still_pending.discard(ticker)
            entry = {
                "flex_symbol": result["flex_symbol"],
                "flex_security_id": result["flex_security_id"],
                "resolved_via": via,
                "query": query,
                "resolved_at": now,
            }
            resolved[ticker] = result["flex_symbol"]
            details[ticker] = {**entry, "tried": list(tried[ticker])}
            cache[ticker] = entry
        pending = [t for t in pending if t in still_pending]
        round_no += 1

    unresolved = sorted(pending)
    for ticker in unresolved:
        details[ticker] = {"tried": list(tried.get(ticker, []))}

    _save_cache(cache_path, cache)
    return resolved, unresolved, details


# ---------------------------------------------------------------------------
# Unresolved-securities report (the list to hand FlexTrade)
# ---------------------------------------------------------------------------


def build_unresolved_rows(
    unresolved: Sequence[str],
    details: dict[str, dict[str, Any]],
    payloads: Sequence[dict] | None = None,
    *,
    suffix: str = ".US",
) -> list[dict[str, Any]]:
    """Unresolved tickers (+ their intended payloads, when given) → CSV rows."""
    payload_by_ticker: dict[str, dict] = {}
    for payload in payloads or []:
        symbol = str(payload.get("symbol") or "")
        bare = symbol[: -len(suffix)] if suffix and symbol.endswith(suffix) else symbol
        payload_by_ticker.setdefault(bare.upper(), payload)
    rows = []
    for ticker in unresolved:
        payload = payload_by_ticker.get(ticker, {})
        rows.append(
            {
                "ticker": ticker,
                "payload_symbol": str(payload.get("symbol") or f"{ticker}{suffix}"),
                "side": str(payload.get("side") or ""),
                "quantity": payload.get("quantity", ""),
                "candidates_tried": "|".join(details.get(ticker, {}).get("tried", [])),
            }
        )
    return rows


def render_unresolved_csv(rows: Sequence[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=UNRESOLVED_CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in UNRESOLVED_CSV_FIELDS})
    return buf.getvalue()


def write_unresolved_csv(rows: Sequence[dict[str, Any]], dest: str | Path) -> str:
    """Write the unresolved-securities CSV to a local path or ``s3://`` URL."""
    from ki_ops.kotl.kelaidata_source import parse_s3_url

    text = render_unresolved_csv(rows)
    dest_text = str(dest)
    if dest_text.startswith("s3://"):
        import boto3

        bucket, key = parse_s3_url(dest_text)
        boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"))
    else:
        local = Path(dest_text)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(text, encoding="utf-8")
    return dest_text


def format_unresolved_table(rows: Sequence[dict[str, Any]]) -> str:
    cols = UNRESOLVED_CSV_FIELDS
    table = [[str(r.get(c, "")) for c in cols] for r in rows]
    widths = [len(c) for c in cols]
    for line in table:
        for i, cell in enumerate(line):
            widths[i] = max(widths[i], len(cell))
    fmt = lambda cells: "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))
    out = [
        f"UNRESOLVED SECURITIES ({len(rows)}) — absent from the Flex master; "
        "send this list to FlexTrade to seed it",
        fmt(cols),
        fmt(["-" * w for w in widths]),
    ]
    out.extend(fmt(line) for line in table)
    return "\n".join(out)


def format_resolution_summary(
    resolved: dict[str, str],
    unresolved: Sequence[str],
    details: dict[str, dict[str, Any]],
) -> str:
    """One-paragraph summary: counts per resolution route + rewrites."""
    by_via: dict[str, int] = {}
    rewrites = []
    for ticker, symbol in resolved.items():
        via = str(details.get(ticker, {}).get("resolved_via", "?"))
        by_via[via] = by_via.get(via, 0) + 1
        if not symbol.startswith(ticker):
            rewrites.append(f"{ticker}→{symbol}")
    via_text = ", ".join(f"{k}={v}" for k, v in sorted(by_via.items()))
    lines = [
        f"flex symbol resolution: {len(resolved)} resolved ({via_text}), "
        f"{len(unresolved)} unresolved"
    ]
    if rewrites:
        shown = ", ".join(sorted(rewrites)[:20])
        lines.append(
            f"  canonical rewrites: {shown}{' …' if len(rewrites) > 20 else ''}"
        )
    return "\n".join(lines)
