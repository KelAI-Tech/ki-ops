"""In-memory Brooklyn SDK + grpc stand-ins for LiveFlexAdapter tests.

No grpcio and no real SDK needed: ``install_fake_sdk`` injects module objects
into ``sys.modules`` so ``ki_ops.kotl.flex_live._load_sdk`` finds them, and a
scriptable :class:`FakeFlexBackend` plays the OrderService.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace


class _EnumWrapper:
    """Mimics protobuf's EnumTypeWrapper (``.Value(name)``)."""

    def __init__(self, values: dict) -> None:
        self._values = dict(values)

    def Value(self, name: str) -> int:  # noqa: N802 (protobuf API)
        return self._values[name]


MARKET_SIDE = {"BUY": 0, "SELL": 1, "COVER": 2, "SHORT": 3}
ORDER_TYPE = {"MARKET": 0, "LIMIT": 1, "STOP_MARKET": 2, "STOP_LIMIT": 3}
TIME_IN_FORCE = {"GFD": 0, "GTC": 1, "OPEN": 2, "CLOSE": 3, "GTX": 4, "IOC": 5}
BROKER_AUTOMATION = {"UNSPECIFIED_AUTOMATION": 0, "NO_AUTOMATION": 2, "AUTOROUTE": 3}


class FakeProtoOrder:
    """Accepts arbitrary field assignment like a generated proto message."""

    def __init__(self) -> None:
        self.brokerAutomation = SimpleNamespace(predefinedType=0)


class _OrderList(list):
    def add(self) -> FakeProtoOrder:
        order = FakeProtoOrder()
        self.append(order)
        return order


class FakeCreateOrdersRequest:
    def __init__(self) -> None:
        self.user = ""
        self.sendToEms = False
        self.batchId = ""
        self.complianceInputs = SimpleNamespace(ruleSets=[])
        self.orders = _OrderList()


class FakeReplayPositionsRequest:
    def __init__(self, sequenceId: int = 0) -> None:  # noqa: N803 (proto field)
        self.sequenceId = sequenceId


class FakeOrderQueryRequest:
    def __init__(self) -> None:
        self.fromDate = ""
        self.toDate = ""
        self.queryType = None
        self.values = []


class FakeLookupSecurityRequest:
    def __init__(self, symbol: str = "", flexSecurityId: int = 0) -> None:  # noqa: N803
        self.symbol = symbol
        self.flexSecurityId = flexSecurityId


class _LookupList(list):
    def add(self) -> FakeLookupSecurityRequest:
        entry = FakeLookupSecurityRequest()
        self.append(entry)
        return entry


class FakeBatchLookupSecurityRequest:
    def __init__(self) -> None:
        self.security = _LookupList()


# SecurityIdentifierType enum numbers (Securities.proto).
ID_SYMBOL, ID_TICKER, ID_CUSIP, ID_ISIN, ID_SEDOL, ID_BLOOMBERG, ID_FIGI = 1, 2, 3, 4, 5, 7, 16


def make_security(
    symbol: str,
    flex_id: int,
    *,
    identifiers: list | None = None,
    description: str = "",
    security_type: str = "EQUITY",
    exchange_mic: str = "XNAS",
):
    """One Flex master entry: canonical *symbol* + ``(type, value)`` identifiers.

    The canonical SYMBOL identifier is always present; pass extra identifiers
    for tickers/SEDOLs, e.g. ``[(ID_TICKER, "BF.B"), (ID_SEDOL, "2146838")]``.
    """
    id_rows = [SimpleNamespace(identifier=symbol, identifierType=ID_SYMBOL)]
    for id_type, value in identifiers or []:
        id_rows.append(SimpleNamespace(identifier=value, identifierType=id_type))
    return SimpleNamespace(
        commonData=SimpleNamespace(
            flexSecurityId=flex_id,
            symbol=symbol,
            description=description,
            securityType=security_type,
            exchangeMIC=exchange_mic,
            identifierList=SimpleNamespace(identifier=id_rows),
        )
    )


_EMPTY_SECURITY = SimpleNamespace(
    commonData=SimpleNamespace(
        flexSecurityId=0,
        symbol="",
        description="",
        securityType="",
        exchangeMIC="",
        identifierList=SimpleNamespace(identifier=[]),
    )
)


def make_create_result(order_id: str, *, success: bool = True, description: str = ""):
    return SimpleNamespace(
        success=success,
        orderId=order_id,
        description=description,
        validationIssues=[],
        complianceIssues=[],
    )


def make_position(
    symbol: str,
    quantity: float,
    *,
    # Live UAT reality: account is KELAI, booking fund is KEL-LOMB.
    fund: str = "KEL-LOMB",
    account: str = "KELAI",
    strategies: list | None = None,
    **extra,
):
    return SimpleNamespace(
        account=account,
        symbol=symbol,
        fund=fund,
        primeBroker=extra.pop("primeBroker", "GS"),
        primeBrokerAccount=extra.pop("primeBrokerAccount", ""),
        quantity=quantity,
        weightedAveragePrice=extra.pop("weightedAveragePrice", 0.0),
        sequenceId=extra.pop("sequenceId", 1),
        transmissionTime="",
        settledQuantity=quantity,
        currency=extra.pop("currency", "USD"),
        strategies=strategies or [],
        attributes=[],
        **extra,
    )


def make_order_info(
    order_id: str,
    symbol: str,
    *,
    side: int = 0,
    quantity: float = 0.0,
    filled_quantity: float = 0.0,
    status: int = 2,
    weighted_avg_price: float = 0.0,
    trade_date: str = "",
    fund: str = "KELAI",
    position_group: str = "USATop2000_strategy_v1",
):
    return SimpleNamespace(
        id=1,
        orderId=order_id,
        batchId="B1",
        symbol=symbol,
        side=side,
        orderType=0,
        quantity=quantity,
        filledQuantity=filled_quantity,
        weightedAvgPrice=weighted_avg_price,
        status=status,
        tradeDate=trade_date,
        notes="",
        clientBatchIdentifier="",
        attributes=[],
        accountTargets=[
            SimpleNamespace(
                positionGroup=position_group,
                fund=fund,
                quantity=quantity,
                filledQuantity=filled_quantity,
            )
        ],
        streetOrders=[],
    )


class FakeFlexBackend:
    """Scriptable OrderService: set the responses, inspect the requests."""

    def __init__(self) -> None:
        self.create_results = []  # list[CreateOrderResult-likes], streamed in chunks
        self.create_chunk_size = 2
        self.order_infos = []  # GrpcClientOrder-likes for GetOrderInfo2
        self.positions = []  # PositionUpdateResponse-likes for ReplayPositions
        self.security_master = []  # make_security(...) entries for Lookup/BatchLookup
        self.security_chunk_size = 0  # 0 = one stream message per BatchLookup
        self.security_error = None  # raise this from Lookup/BatchLookup (network down)
        self.lookup_calls = 0
        self.batch_lookup_calls = 0
        self.last_create_request = None
        self.last_query_request = None
        self.last_replay_request = None
        self.last_lookup_request = None
        self.last_batch_lookup_request = None
        self.last_metadata = None
        self.channels_opened = []

    # --- stub RPCs -------------------------------------------------------
    def CreateOrders(self, request, timeout=None, metadata=None):  # noqa: N802
        self.last_create_request = request
        self.last_metadata = metadata
        results = list(self.create_results)
        chunk = max(1, self.create_chunk_size)
        for i in range(0, len(results), chunk):
            yield SimpleNamespace(
                status=SimpleNamespace(code=0), batchId="B1", results=results[i : i + chunk]
            )
        if not results:
            yield SimpleNamespace(status=SimpleNamespace(code=0), batchId="B1", results=[])

    def GetOrderInfo2(self, request, timeout=None, metadata=None):  # noqa: N802
        self.last_query_request = request
        self.last_metadata = metadata
        yield SimpleNamespace(status=SimpleNamespace(code=0), orders=list(self.order_infos))

    def ReplayPositions(self, request, timeout=None, metadata=None):  # noqa: N802
        self.last_replay_request = request
        self.last_metadata = metadata
        for position in self.positions:
            yield position

    # --- SecurityService ---------------------------------------------------
    def _match_security(self, query: str):
        """Live-verified semantics: query matches the canonical symbol or ANY identifier."""
        text = str(query).strip().upper()
        for security in self.security_master:
            common = security.commonData
            if str(common.symbol).upper() == text:
                return security
            for ident in common.identifierList.identifier:
                if str(ident.identifier).upper() == text:
                    return security
        return None

    def _lookup_response(self, query: str):
        security = self._match_security(query)
        if security is None:
            return SimpleNamespace(
                status=SimpleNamespace(
                    success=False,
                    description=f"Security not found for query (symbol = '{query}', flexSecurityId = 0)",
                ),
                security=_EMPTY_SECURITY,
            )
        return SimpleNamespace(
            status=SimpleNamespace(
                success=True, description=f"Security found for symbol = '{query}'."
            ),
            security=security,
        )

    def Lookup(self, request, timeout=None, metadata=None):  # noqa: N802
        if self.security_error is not None:
            raise self.security_error
        self.lookup_calls += 1
        self.last_lookup_request = request
        self.last_metadata = metadata
        yield self._lookup_response(request.symbol)

    def BatchLookup(self, request, timeout=None, metadata=None):  # noqa: N802
        if self.security_error is not None:
            raise self.security_error
        self.batch_lookup_calls += 1
        self.last_batch_lookup_request = request
        self.last_metadata = metadata
        responses = [self._lookup_response(entry.symbol) for entry in request.security]
        chunk = self.security_chunk_size or len(responses) or 1
        for i in range(0, max(len(responses), 1), chunk):
            yield SimpleNamespace(
                status=SimpleNamespace(success=True, description=""),
                response=responses[i : i + chunk],
            )


class _FakeChannel:
    def __init__(self, endpoint: str, options=None) -> None:
        self.endpoint = endpoint
        self.options = options
        self.closed = False

    def close(self) -> None:
        self.closed = True


def install_fake_sdk(monkeypatch, backend: FakeFlexBackend) -> None:
    """Inject fake ``grpc`` + ``API`` modules into ``sys.modules``."""

    grpc_mod = types.ModuleType("grpc")

    def insecure_channel(endpoint, options=None):
        channel = _FakeChannel(endpoint, options)
        backend.channels_opened.append(channel)
        return channel

    grpc_mod.insecure_channel = insecure_channel
    grpc_mod.RpcError = type("RpcError", (Exception,), {})

    orders_pb2 = types.ModuleType("API.Orders_pb2")
    orders_pb2.CreateOrdersRequest = FakeCreateOrdersRequest
    orders_pb2.ReplayPositionsRequest = FakeReplayPositionsRequest
    orders_pb2.MarketSide = _EnumWrapper(MARKET_SIDE)
    orders_pb2.OrderType = _EnumWrapper(ORDER_TYPE)
    orders_pb2.TimeInForce = _EnumWrapper(TIME_IN_FORCE)
    orders_pb2.BrokerAutomationType = _EnumWrapper(BROKER_AUTOMATION)

    orders_grpc = types.ModuleType("API.Orders_pb2_grpc")

    class OrderServiceStub:
        def __init__(self, channel) -> None:
            self.channel = channel
            self.CreateOrders = backend.CreateOrders
            self.GetOrderInfo2 = backend.GetOrderInfo2
            self.ReplayPositions = backend.ReplayPositions

    orders_grpc.OrderServiceStub = OrderServiceStub

    domain_pb2 = types.ModuleType("API.DomainCommons_pb2")
    domain_pb2.PRE_TRADE = 0
    domain_pb2.ALL_ORDERS = 4
    domain_pb2.OrderQueryRequest = FakeOrderQueryRequest

    securities_pb2 = types.ModuleType("API.Securities_pb2")
    securities_pb2.LookupSecurityRequest = FakeLookupSecurityRequest
    securities_pb2.BatchLookupSecurityRequest = FakeBatchLookupSecurityRequest

    securities_grpc = types.ModuleType("API.Securities_pb2_grpc")

    class SecurityServiceStub:
        def __init__(self, channel) -> None:
            self.channel = channel
            self.Lookup = backend.Lookup
            self.BatchLookup = backend.BatchLookup

    securities_grpc.SecurityServiceStub = SecurityServiceStub

    api_pkg = types.ModuleType("API")
    api_pkg.__path__ = []  # mark as package
    api_pkg.Orders_pb2 = orders_pb2
    api_pkg.Orders_pb2_grpc = orders_grpc
    api_pkg.DomainCommons_pb2 = domain_pb2
    api_pkg.Securities_pb2 = securities_pb2
    api_pkg.Securities_pb2_grpc = securities_grpc

    monkeypatch.setitem(sys.modules, "grpc", grpc_mod)
    monkeypatch.setitem(sys.modules, "API", api_pkg)
    monkeypatch.setitem(sys.modules, "API.Orders_pb2", orders_pb2)
    monkeypatch.setitem(sys.modules, "API.Orders_pb2_grpc", orders_grpc)
    monkeypatch.setitem(sys.modules, "API.DomainCommons_pb2", domain_pb2)
    monkeypatch.setitem(sys.modules, "API.Securities_pb2", securities_pb2)
    monkeypatch.setitem(sys.modules, "API.Securities_pb2_grpc", securities_grpc)
