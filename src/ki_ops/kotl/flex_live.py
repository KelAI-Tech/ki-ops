"""Live FlexTrade Brooklyn adapter: CreateOrders, GetOrderInfo2, ReplayPositions.

Everything here is gRPC against the Brooklyn OMS (see
``docs/flextrade-connectivity-guide.md``). Design constraints:

- **Brooklyn SDK is local-only** (gitignored, synced from
  ``s3://kelaitrading/flextrade/1.1.brooklyn-12.57.0/brooklyn-12.57.0-python3/``).
  It is imported lazily, only when a live call is actually made, from
  ``KOTL_FLEX_SDK_PATH`` — both ``<path>`` and ``<path>/API`` go on ``sys.path``
  because the generated stubs import siblings by bare name.
- **grpcio and boto3 stay optional** (``pip install ki-ops[flex]``); importing
  this module must never require them.
- Auth is a static bearer token as gRPC metadata. Resolution order: env
  ``KOTL_FLEX_TOKEN`` / ``KOTL_FLEX_ENDPOINT``, else Secrets Manager
  ``kelai/flextrade/api-token`` (fields ``token`` / ``uat_endpoint`` /
  ``prod_endpoint`` / ``metadata_key`` / ``scheme``).

Sample-code gotchas fixed here (guide §8):

- ``batchRequest.user`` is configurable (``KOTL_FLEX_BATCH_USER``), not a
  hardcoded ``"MGO"``.
- ``owner`` **exists** on the ``Order`` proto (field 3) and is copied.
  ``fund`` does **not** exist as an ``Order`` field — verified against
  ``Orders.proto``: fund allocation derives from ``positionGroup`` (its fund
  splits), overridable via ``overrideFundAllocations`` /
  ``fundAllocationOverride``. The payload ``fund`` key is kept for our ledger
  but intentionally not mapped onto the proto.
- ``CreateOrders`` returns a **stream** of ``CreateOrdersResponse``; per-order
  ``CreateOrderResult`` rows are collected while iterating (the sample's
  post-hoc ``hasattr`` checks ran on a consumed stream and were dead code).
  ``CreateOrderResult`` carries no symbol echo, so results are joined to the
  input orders positionally, and a count mismatch is a hard error.
- **``originId`` is the durable order key** (verified live in UAT 2026-09-08):
  Flex echoes it back as ``orderId`` in both the create results and
  ``GetOrderInfo2``, so it must be globally unique — it is stamped as
  ``<submit_id>-<i>``.
- ``sendToEms`` is configurable (``KOTL_FLEX_SEND_TO_EMS``, default true) and
  the ``PRE_TRADE`` compliance rule set is always appended.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from ki_ops.kotl.models import WorkingOrder

FLEX_SECRET_ID = "kelai/flextrade/api-token"
# The API token is issued for user JCO; UAT rejects other users with
# "User JCO is not entitled to trade as <user>" (verified live 2026-09-08).
DEFAULT_BATCH_USER = "JCO"
DEFAULT_METADATA_KEY = "authorization"
DEFAULT_SCHEME = "Bearer"
DEFAULT_REGION = "us-east-1"

# SOD position scoping (verified against live UAT ReplayPositions 2026-09-08):
# rows carry account=KELAI and the *booking* fund derived from the position
# group's fund splits (KEL-LOMB for USATop2000_strategy_v1) — not the payload
# "fund" key.
DEFAULT_SOD_ACCOUNT = "KELAI"
DEFAULT_SOD_FUND = "KEL-LOMB"

GRPC_OPTIONS = [
    ("grpc.max_message_length", 512 * 1024 * 1024),
    ("grpc.max_receive_message_length", 512 * 1024 * 1024),
    ("grpc.keepalive_time_ms", 330000),
]

CREATE_TIMEOUT_S = 40.0
QUERY_TIMEOUT_S = 100.0

# Plain US equity in Flex symbology: TICKER.US, ticker alnum (hyphen for share
# classes). Excludes non-US listings (18880.KS) and option symbols (spaces).
_PLAIN_US_EQUITY = re.compile(r"^[A-Z0-9\-]{1,12}\.US$")


class FlexSdkMissingError(RuntimeError):
    """Brooklyn SDK not importable — set KOTL_FLEX_SDK_PATH."""


def _truthy(text: str | None, default: bool) -> bool:
    if text is None or text.strip() == "":
        return default
    return text.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FlexConfig:
    """Resolved connection + behavior settings for live Flex calls."""

    endpoint: str
    token: str
    metadata_key: str = DEFAULT_METADATA_KEY
    scheme: str = DEFAULT_SCHEME
    batch_user: str = DEFAULT_BATCH_USER
    send_to_ems: bool = True
    sdk_path: str | None = None
    create_timeout: float = CREATE_TIMEOUT_S
    query_timeout: float = QUERY_TIMEOUT_S

    @property
    def metadata(self) -> list[tuple[str, str]]:
        value = f"{self.scheme} {self.token}".strip() if self.scheme else self.token
        return [(self.metadata_key, value)]


def _load_flex_secret(secret_id: str, region: str) -> dict[str, Any]:
    import boto3

    client = boto3.client("secretsmanager", region_name=region)
    raw = client.get_secret_value(SecretId=secret_id)["SecretString"]
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"secret {secret_id} is not a JSON object")
    return data


def load_flex_config(
    *,
    flex_env: str = "UAT",
    secret_id: str = FLEX_SECRET_ID,
    region: str = DEFAULT_REGION,
) -> FlexConfig:
    """Build a :class:`FlexConfig` from env vars, falling back to Secrets Manager.

    - endpoint: ``KOTL_FLEX_ENDPOINT`` (host:port) else the secret's
      ``uat_endpoint`` / ``prod_endpoint`` picked by *flex_env*.
    - token: ``KOTL_FLEX_TOKEN`` else the secret's ``token``.
    - ``metadata_key`` / ``scheme`` honor the secret when it is consulted.
    """
    endpoint = os.environ.get("KOTL_FLEX_ENDPOINT") or None
    token = os.environ.get("KOTL_FLEX_TOKEN") or None
    metadata_key = DEFAULT_METADATA_KEY
    scheme = DEFAULT_SCHEME

    if endpoint is None or token is None:
        secret = _load_flex_secret(secret_id, region)
        token = token or str(secret.get("token") or "")
        if endpoint is None:
            key = "prod_endpoint" if flex_env.upper() == "PROD" else "uat_endpoint"
            endpoint = str(secret.get(key) or "")
        metadata_key = str(secret.get("metadata_key") or metadata_key)
        scheme = str(secret.get("scheme") or scheme)

    if not endpoint:
        raise ValueError(
            "no Flex endpoint: set KOTL_FLEX_ENDPOINT (host:port) or provide "
            f"uat_endpoint/prod_endpoint in secret {secret_id}"
        )
    if not token:
        raise ValueError(
            f"no Flex token: set KOTL_FLEX_TOKEN or provide token in secret {secret_id}"
        )

    return FlexConfig(
        endpoint=endpoint,
        token=token,
        metadata_key=metadata_key,
        scheme=scheme,
        batch_user=os.environ.get("KOTL_FLEX_BATCH_USER") or DEFAULT_BATCH_USER,
        send_to_ems=_truthy(os.environ.get("KOTL_FLEX_SEND_TO_EMS"), True),
        sdk_path=os.environ.get("KOTL_FLEX_SDK_PATH") or None,
    )


# ---------------------------------------------------------------------------
# Brooklyn SDK loading (lazy; local-only, never committed)
# ---------------------------------------------------------------------------


def _load_sdk(sdk_path: str | Path | None = None):
    """Import ``API.Orders_pb2`` / ``API.Orders_pb2_grpc`` / ``API.DomainCommons_pb2``.

    Inserts both *sdk_path* and ``<sdk_path>/API`` into ``sys.path`` (the
    generated stubs import siblings by bare module name). Already-importable
    modules (e.g. a test-injected fake ``API`` package) are used as-is.
    """
    path = sdk_path or os.environ.get("KOTL_FLEX_SDK_PATH")
    if path:
        base = str(Path(path))
        for entry in (base, str(Path(base) / "API")):
            if entry not in sys.path:
                sys.path.insert(0, entry)
    try:
        import API.DomainCommons_pb2 as DomainCommons_pb2
        import API.Orders_pb2 as Orders_pb2
        import API.Orders_pb2_grpc as OrderServiceModule
    except ImportError as exc:
        raise FlexSdkMissingError(
            "Brooklyn SDK not importable. Set KOTL_FLEX_SDK_PATH to a local "
            "sync of the SDK, e.g.\n"
            "  aws s3 sync s3://kelaitrading/flextrade/1.1.brooklyn-12.57.0/"
            "brooklyn-12.57.0-python3/ vendor/flextrade/brooklyn-12.57.0-python3/\n"
            "  export KOTL_FLEX_SDK_PATH=vendor/flextrade/brooklyn-12.57.0-python3\n"
            f"(import error: {exc})"
        ) from exc
    return Orders_pb2, OrderServiceModule, DomainCommons_pb2


def _import_grpc():
    try:
        import grpc
    except ImportError as exc:
        raise RuntimeError(
            "grpcio is required for live Flex calls: pip install 'ki-ops[flex]'"
        ) from exc
    return grpc


def _open_channel(config: FlexConfig):
    grpc = _import_grpc()
    return grpc.insecure_channel(config.endpoint, options=GRPC_OPTIONS)


def _enum_value(enum_wrapper, value, *, default: int | None = None) -> int:
    """Resolve a proto enum from an int or its label (``"BUY"`` → 0)."""
    if value is None or value == "":
        if default is None:
            raise ValueError("missing enum value")
        return default
    if isinstance(value, int):
        return value
    text = str(value).strip().upper()
    if text.isdigit():
        return int(text)
    return enum_wrapper.Value(text)


def _mmddyyyy(trade_date: str | date) -> str:
    d = trade_date if isinstance(trade_date, date) else date.fromisoformat(str(trade_date))
    return d.strftime("%m/%d/%Y")


_SUBMIT_ID_RE = re.compile(r"submit_id=([0-9a-fA-F-]{8,})")


def _origin_prefix(order: dict) -> str:
    """Unique originId prefix: the submit_id stamped in notes, else a UUID."""
    match = _SUBMIT_ID_RE.search(str(order.get("notes") or ""))
    if match:
        return match.group(1)
    import uuid

    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# CreateOrders
# ---------------------------------------------------------------------------


class LiveFlexAdapter:
    """``FlexSubmitAdapter`` over ``OrderService.CreateOrders`` (gRPC stream)."""

    def __init__(self, config: FlexConfig) -> None:
        self.config = config

    def create_orders(self, order_list: list[dict]) -> list[dict]:
        if not order_list:
            raise ValueError("order_list is empty")
        Orders_pb2, OrderServiceModule, DomainCommons_pb2 = _load_sdk(self.config.sdk_path)

        request = Orders_pb2.CreateOrdersRequest()
        request.user = self.config.batch_user
        request.sendToEms = self.config.send_to_ems
        request.complianceInputs.ruleSets.append(DomainCommons_pb2.PRE_TRADE)

        origin_ids: list[str] = []
        for i, order in enumerate(order_list, start=1):
            proto = request.orders.add()
            # Flex exposes originId as the queryable orderId in both the create
            # results and GetOrderInfo2 (verified live 2026-09-08), and KOTL
            # keys working orders on it — so it must be globally unique. Use
            # <submit_id>-<i> (submit_id is stamped into notes by the submit
            # path); fall back to a fresh UUID prefix.
            proto.originId = str(order.get("originId") or f"{_origin_prefix(order)}-{i}")
            origin_ids.append(proto.originId)
            proto.symbol = str(order["symbol"])
            proto.quantity = float(order["quantity"])
            proto.price = float(order.get("price") or 0)
            proto.side = _enum_value(Orders_pb2.MarketSide, order["side"])
            proto.orderType = _enum_value(Orders_pb2.OrderType, order["orderType"])
            proto.timeInForce = _enum_value(Orders_pb2.TimeInForce, order["timeInForce"])
            proto.positionGroup = str(order.get("positionGroup") or "")
            proto.user = str(order.get("user") or self.config.batch_user)
            # `owner` exists on the Order proto (dropped by the sample sender).
            proto.owner = str(order.get("owner") or "")
            proto.trader = str(order.get("trader") or "")
            proto.notes = str(order.get("notes") or "")
            proto.broker = str(order.get("broker") or "")
            proto.tradingCurrency = str(order.get("tradingCurrency") or "USD")
            proto.settlementCurrency = str(order.get("settlementCurrency") or "USD")
            proto.fixTags = str(order.get("fixTags") or "")
            proto.manualFill = bool(order.get("manualFill", False))
            proto.algo = str(order.get("algo") or "")
            proto.startTime = str(order.get("startTime") or "")
            proto.tradeDate = str(order.get("tradeDate") or "")
            proto.brokerAutomation.predefinedType = _enum_value(
                Orders_pb2.BrokerAutomationType,
                order.get("brokerAutomationType"),
                default=0,
            )
            # No `fund` field on the Order proto: fund allocation derives from
            # positionGroup (see module docstring); payload "fund" stays
            # ledger-only.

        def _dump_create_results(results, submitted_origin_ids) -> str:
            """Raw CreateOrders results → JSON evidence file (join anomalies)."""
            import tempfile
            from datetime import datetime, timezone

            payload = {
                "dumped_at": datetime.now(timezone.utc).isoformat(),
                "submitted_origin_ids": list(submitted_origin_ids),
                "results": [
                    {
                        "orderId": str(r.orderId),
                        "success": bool(r.success),
                        "description": str(getattr(r, "description", "") or ""),
                        "validationIssues": [
                            str(getattr(v, "description", v))
                            for v in getattr(r, "validationIssues", [])
                        ],
                        "complianceIssues": [
                            str(getattr(v, "description", v))
                            for v in getattr(r, "complianceIssues", [])
                        ],
                    }
                    for r in results
                ],
            }
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                prefix="kotl_create_results_",
                suffix=".json",
                delete=False,
                encoding="utf-8",
            )
            with handle as f:
                json.dump(payload, f, indent=1)
            return handle.name

        channel = _open_channel(self.config)
        try:
            stub = OrderServiceModule.OrderServiceStub(channel)
            stream = stub.CreateOrders(
                request,
                timeout=self.config.create_timeout,
                metadata=self.config.metadata,
            )
            raw_results = []
            for response in stream:  # collect while iterating — it is a stream
                raw_results.extend(response.results)
        finally:
            close = getattr(channel, "close", None)
            if close is not None:
                close()

        # The stream yields results in completion order, not submission order
        # (observed live in UAT 2026-09-08: 1568/1569 results out of place), so
        # positional zip misattributes success/rejection. Join on originId,
        # which Flex echoes back as the result orderId, and return results in
        # submission order — callers zip them against the payload list.
        #
        # An order can produce MORE THAN ONE result message (observed live
        # 2026-09-08: 2,082 results for 1,911 orders). Two live-verified cases:
        #
        # - interim states followed by a terminal one — keep the LAST result
        #   received per originId and log the extras;
        # - **fund-split child orders**: Flex splits an order across the
        #   position group's fund allocations and returns one result per child
        #   keyed ``<originId>-B``, ``<originId>-C``, … with NO result under
        #   the parent id (live: 171 of 1,911 orders split into exactly B+C).
        #   Children join back to their parent; the parent result is the
        #   AND of the children's successes.
        #
        # Unknown result ids or submitted orders with no result at all remain
        # hard errors — those would misstate the book.
        known = set(origin_ids)
        result_groups: dict[str, list] = {}
        child_groups: dict[str, list] = {}
        unknown_results = []
        for result in raw_results:
            key = str(result.orderId)
            if key in known:
                result_groups.setdefault(key, []).append(result)
                continue
            parent, dash, suffix = key.rpartition("-")
            if dash and parent in known and len(suffix) == 1 and suffix.isalpha():
                child_groups.setdefault(parent, []).append(result)
            else:
                unknown_results.append(result)
        problems = []
        if unknown_results:
            shown = ", ".join(
                f"{r.orderId!r} success={bool(r.success)}" for r in unknown_results[:5]
            )
            problems.append(
                f"{len(unknown_results)} result(s) with orderIds not among the "
                f"submitted originIds (e.g. {shown})"
            )
        missing = [
            o for o in origin_ids if o not in result_groups and o not in child_groups
        ]
        if missing:
            problems.append(
                f"no result for {len(missing)} submitted originId(s) "
                f"(e.g. {', '.join(missing[:5])})"
            )
        if problems:
            dump = _dump_create_results(raw_results, origin_ids)
            raise RuntimeError(
                "CreateOrders results cannot be joined to orders: "
                + "; ".join(problems)
                + f" — raw results dumped to {dump}"
            )
        if child_groups:
            sample_key = next(iter(child_groups))
            sample_children = ", ".join(
                str(r.orderId) for r in child_groups[sample_key]
            )
            print(
                f"CreateOrders: {len(child_groups)} order(s) fund-split by Flex "
                f"into child orders (e.g. {sample_key} → {sample_children}) — "
                "parent success is the AND of its children"
            )
        multi = {k: v for k, v in result_groups.items() if len(v) > 1}
        if multi:
            extras = sum(len(v) - 1 for v in multi.values())
            sample_key = next(iter(multi))
            sample = " | ".join(
                f"success={bool(r.success)} desc={str(getattr(r, 'description', ''))[:60]!r}"
                for r in multi[sample_key]
            )
            print(
                f"CreateOrders: {extras} extra interim result(s) across "
                f"{len(multi)} order(s) — keeping the last result per originId "
                f"(e.g. {sample_key}: {sample}); raw dump: "
                f"{_dump_create_results(raw_results, origin_ids)}"
            )
        result_by_origin = {key: group[-1] for key, group in result_groups.items()}

        def _issues(results) -> list[str]:
            return [
                str(getattr(v, "description", v))
                for r in results
                for v in list(getattr(r, "validationIssues", []))
                + list(getattr(r, "complianceIssues", []))
            ]

        out: list[dict] = []
        for origin_id, payload in zip(origin_ids, order_list):
            children = child_groups.get(origin_id)
            if children:
                child_ids = sorted(str(r.orderId) for r in children)
                row = {
                    # The ledger keys on the parent originId — the durable
                    # handle KOTL owns; child ids ride along for refresh/audit.
                    "orderId": origin_id,
                    "success": all(bool(r.success) for r in children),
                    "description": "; ".join(
                        text
                        for text in (
                            str(getattr(r, "description", "") or "") for r in children
                        )
                        if text
                    ),
                    "issues": _issues(children),
                    "childOrderIds": child_ids,
                }
            else:
                result = result_by_origin[origin_id]
                row = {
                    "orderId": str(result.orderId),
                    "success": bool(result.success),
                    "description": str(getattr(result, "description", "") or ""),
                    "issues": _issues([result]),
                }
            row.update(
                symbol=payload.get("symbol"),
                side=payload.get("side"),
                quantity=payload.get("quantity"),
            )
            out.append(row)
        return out


# ---------------------------------------------------------------------------
# GetOrderInfo2 → refresh source
# ---------------------------------------------------------------------------


def _kv_string_attrs(attributes) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kv in attributes:
        value = getattr(kv, "value", None)
        if value is None:
            continue
        has_field = getattr(value, "HasField", None)
        if has_field is not None:
            if has_field("stringValue"):
                out[kv.key] = value.stringValue
            elif has_field("doubleValue"):
                out[kv.key] = value.doubleValue
        elif getattr(value, "stringValue", ""):
            out[kv.key] = value.stringValue
    return out


def flatten_order_info(order) -> dict[str, Any]:
    """One ``GrpcClientOrder`` → flat dict in the kelai ``get_orders`` shape.

    Parent-level fields plus ``*_acc_tgt`` from account targets (last target
    wins, matching the sample flatten that ``kelai_refresh`` already parses).
    """
    row: dict[str, Any] = {
        "orderId": str(order.orderId),
        "batchId": str(getattr(order, "batchId", "") or ""),
        "symbol": str(order.symbol),
        "side": order.side,
        "orderType": getattr(order, "orderType", None),
        "quantity": float(order.quantity),
        "filledQuantity": float(order.filledQuantity),
        "weightedAvgPrice": float(getattr(order, "weightedAvgPrice", 0) or 0),
        "status": order.status,
        "tradeDate": str(getattr(order, "tradeDate", "") or ""),
        "notes": str(getattr(order, "notes", "") or ""),
        "clientBatchIdentifier": str(getattr(order, "clientBatchIdentifier", "") or ""),
    }
    row.update(_kv_string_attrs(getattr(order, "attributes", [])))
    for target in getattr(order, "accountTargets", []):
        row["positionGroup_acc_tgt"] = str(getattr(target, "positionGroup", "") or "")
        row["fund_acc_tgt"] = str(getattr(target, "fund", "") or "")
        row["quantity_acc_tgt"] = float(getattr(target, "quantity", 0) or 0)
        row["filledQuantity_acc_tgt"] = float(getattr(target, "filledQuantity", 0) or 0)
    return row


def fetch_order_rows(config: FlexConfig, trade_date: str | date) -> list[dict[str, Any]]:
    """``GetOrderInfo2`` for one trade date → flattened row dicts."""
    _, OrderServiceModule, DomainCommons_pb2 = _load_sdk(config.sdk_path)

    request = DomainCommons_pb2.OrderQueryRequest()
    request.fromDate = _mmddyyyy(trade_date)
    request.toDate = _mmddyyyy(trade_date)
    request.queryType = DomainCommons_pb2.ALL_ORDERS

    channel = _open_channel(config)
    try:
        stub = OrderServiceModule.OrderServiceStub(channel)
        stream = stub.GetOrderInfo2(
            request, timeout=config.query_timeout, metadata=config.metadata
        )
        rows: list[dict[str, Any]] = []
        for response in stream:
            for order in response.orders:
                rows.append(flatten_order_info(order))
    finally:
        close = getattr(channel, "close", None)
        if close is not None:
            close()
    return rows


def aggregate_split_order_rows(
    rows: list[dict[str, Any]], stored_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """Fold Flex fund-split child rows (``<parent>-B``/``-C`` …) into one parent row.

    Flex splits an order across the position group's fund allocations; the
    ledger keys on the parent originId, so child rows must re-aggregate before
    the refresh join: quantities and fills sum, the average price is
    fill-weighted, and when children disagree on status the least-filled
    child's status wins (conservative: the order stays working until every
    child is done). ``fund`` is kept only when unanimous — a split parent has
    no single fund.
    """
    stored = {str(s).upper() for s in stored_ids}
    parents: dict[str, list[dict[str, Any]]] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        order_id = str(row.get("orderId") or "")
        parent, dash, suffix = order_id.rpartition("-")
        if (
            dash
            and len(suffix) == 1
            and suffix.isalpha()
            and parent.upper() in stored
            and order_id.upper() not in stored
        ):
            parents.setdefault(parent, []).append(row)
        else:
            out.append(row)
    for parent, children in parents.items():
        quantity = sum(float(c.get("quantity") or 0) for c in children)
        filled = sum(float(c.get("filledQuantity") or 0) for c in children)
        avg_price = (
            sum(
                float(c.get("filledQuantity") or 0) * float(c.get("weightedAvgPrice") or 0)
                for c in children
            )
            / filled
            if filled
            else 0.0
        )
        least_done = max(
            children,
            key=lambda c: float(c.get("quantity") or 0) - float(c.get("filledQuantity") or 0),
        )
        funds = {str(c.get("fund_acc_tgt") or c.get("fund") or "") for c in children}
        fund_value = next(iter(funds)) if len(funds) == 1 else ""
        merged = dict(children[0])
        merged.update(
            orderId=parent,
            quantity=quantity,
            filledQuantity=filled,
            weightedAvgPrice=avg_price,
            status=least_done.get("status"),
            childOrderIds=sorted(str(c.get("orderId")) for c in children),
        )
        for key in ("fund", "fund_acc_tgt"):
            if key in merged:
                merged[key] = fund_value
        out.append(merged)
    return out


class LiveRefreshSource:
    """Refresh source over live ``GetOrderInfo2`` (drop-in for the fixtures)."""

    def __init__(self, config: FlexConfig) -> None:
        self.config = config

    def fetch_orders(self, trade_date: str, *, stored: Sequence[WorkingOrder]) -> list[dict]:
        from ki_ops.kotl.kelai_refresh import KelaiRefreshSource

        rows = fetch_order_rows(self.config, trade_date)
        rows = aggregate_split_order_rows(rows, [w.flex_order_id for w in stored])
        source = KelaiRefreshSource(rows, trade_date=date.fromisoformat(trade_date))
        return source.fetch_orders(trade_date, stored=stored)


# ---------------------------------------------------------------------------
# ReplayPositions → SOD book
# ---------------------------------------------------------------------------


def _position_row(position) -> dict[str, Any]:
    row = {
        "account": str(getattr(position, "account", "") or ""),
        "symbol": str(position.symbol),
        "fund": str(getattr(position, "fund", "") or ""),
        "primeBroker": str(getattr(position, "primeBroker", "") or ""),
        "primeBrokerAccount": str(getattr(position, "primeBrokerAccount", "") or ""),
        "quantity": float(position.quantity),
        "weightedAveragePrice": float(getattr(position, "weightedAveragePrice", 0) or 0),
        "sequenceId": getattr(position, "sequenceId", 0),
        "settledQuantity": float(getattr(position, "settledQuantity", 0) or 0),
        "currency": str(getattr(position, "currency", "") or ""),
    }
    strategies = [
        f"{getattr(kv, 'key', '')}={getattr(getattr(kv, 'value', None), 'stringValue', '') or getattr(kv, 'value', '')}"
        for kv in getattr(position, "strategies", [])
    ]
    if strategies:
        row["strategies"] = "|".join(strategies)
    row.update(_kv_string_attrs(getattr(position, "attributes", [])))
    return row


_GROUP_MARKER_KEYS = {"positiongroup", "position_group", "group", "strategy", "strategies"}


def _matches_group(row: dict[str, Any], group: str) -> bool:
    """Lenient position-group match on strategies / group-like attributes.

    Rows exposing no group marker at all pass — the fund filter still governs
    them (position replays key on account/fund; group markers only appear via
    strategies KVs, which use Brooklyn's ``group|strategy`` composite format).
    """
    markers = [str(v) for k, v in row.items() if k.lower() in _GROUP_MARKER_KEYS and v]
    if not markers:
        return True
    for cand in markers:
        if group == cand or group in re.split(r"[|,=]", cand):
            return True
    return False


def is_plain_us_equity(symbol: str, *, suffix: str = ".US") -> bool:
    sym = str(symbol).strip().upper()
    if suffix != ".US":
        return sym.endswith(suffix) and " " not in sym and sym.count(".") == 1
    return bool(_PLAIN_US_EQUITY.match(sym))


def fetch_flex_positions(
    config: FlexConfig,
    *,
    account: str | None = None,
    fund: str | None = None,
    position_group: str = "USATop2000_strategy_v1",
    symbol_suffix: str = ".US",
) -> tuple[dict[str, Decimal], list[dict[str, Any]]]:
    """``ReplayPositions(sequenceId=0)`` → current signed position book.

    Returns ``(positions, raw_rows)``:

    - *positions*: ``{flex_symbol: signed Decimal qty}`` filtered to the given
      *account* / *fund* / *position_group* and to plain-equity
      ``symbol_suffix`` symbols (non-US listings and option symbols are
      dropped), zero rows excluded. *account* defaults to
      ``KOTL_FLEX_SOD_ACCOUNT`` env or ``KELAI``; *fund* defaults to
      ``KOTL_FLEX_SOD_FUND`` env or ``KEL-LOMB`` (the **booking** fund the
      position group allocates to — not the payload ``fund`` key).
    - *raw_rows*: every replayed row, unfiltered, for diagnostics.
    """
    if account is None:
        account = os.environ.get("KOTL_FLEX_SOD_ACCOUNT") or DEFAULT_SOD_ACCOUNT
    if fund is None:
        fund = os.environ.get("KOTL_FLEX_SOD_FUND") or DEFAULT_SOD_FUND
    Orders_pb2, OrderServiceModule, _ = _load_sdk(config.sdk_path)

    request = Orders_pb2.ReplayPositionsRequest(sequenceId=0)
    channel = _open_channel(config)
    try:
        stub = OrderServiceModule.OrderServiceStub(channel)
        stream = stub.ReplayPositions(
            request, timeout=config.query_timeout, metadata=config.metadata
        )
        raw_rows = [_position_row(p) for p in stream]
    finally:
        close = getattr(channel, "close", None)
        if close is not None:
            close()

    positions: dict[str, Decimal] = {}
    for row in raw_rows:
        symbol = str(row["symbol"]).strip().upper()
        if account and row.get("account") and str(row["account"]) != account:
            continue
        if fund and row.get("fund") and str(row["fund"]) != fund:
            continue
        if position_group and not _matches_group(row, position_group):
            continue
        if not is_plain_us_equity(symbol, suffix=symbol_suffix):
            continue
        qty = Decimal(str(row["quantity"]))
        if qty == 0:
            continue
        positions[symbol] = positions.get(symbol, Decimal("0")) + qty
    return {s: q for s, q in positions.items() if q != 0}, raw_rows
