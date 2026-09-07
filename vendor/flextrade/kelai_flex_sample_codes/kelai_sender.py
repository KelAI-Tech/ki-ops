
def send_orders(*,order_list, _env: str):
    import sys
    sys.path.append('{your api package path}/flex_api/API')
    import grpc
    import API.Orders_pb2 as Orders_pb2
    import API.Orders_pb2_grpc as OrderService
    import API.DomainCommons_pb2 as DomainCommons_pb2
    from API.Token import metadata
    from flex_utils import get_grpc_channel

    channel = get_grpc_channel(env=_env)
    stub = OrderService.OrderServiceStub(channel)

    batchRequest = Orders_pb2.CreateOrdersRequest()
    batchRequest.user = "MGO"
    batchRequest.sendToEms = True
    batchRequest.complianceInputs.ruleSets.append(DomainCommons_pb2.PRE_TRADE)  # type: ignore

    for order in order_list:
        order_proto = batchRequest.orders.add()
        order_proto.symbol = order['symbol']
        order_proto.quantity = order['quantity']
        order_proto.price = order.get('price', 0)  # Optional field
        order_proto.side = order['side']
        order_proto.orderType = order['orderType']
        order_proto.positionGroup = order['positionGroup']
        order_proto.user = order['user']
        order_proto.trader = order.get('trader', "") # Optional field
        order_proto.notes = order.get('notes', "") # Optional field
        order_proto.broker = order.get('broker', "") # Optional field
        order_proto.tradingCurrency = order.get('tradingCurrency', "USD")
        order_proto.settlementCurrency = order.get('settlementCurrency', "USD")
        order_proto.fixTags = order.get('fixTags', "") # Optional field
        order_proto.manualFill = order.get('manualFill', False)
        order_proto.timeInForce = order['timeInForce']
        order_proto.algo = order.get('algo', "") # Optional field
        order_proto.startTime = order.get('startTime', "") # Optional field
        order_proto.tradeDate = order.get('tradeDate', "") # Optional field
        order_proto.brokerAutomation.predefinedType = order.get('brokerAutomationType', 0)

    try:
        response = stub.CreateOrders(batchRequest, timeout=40, metadata=metadata)
        print("gRPC call succeeded.")
        for details in response:
            print("Response details:", details)
        # from google.protobuf.json_format import MessageToDict
        # print(MessageToDict(response))
        # If response has fields like status or error, print them:
        if hasattr(response, 'status'):
            print("Status:", response.status)
        if hasattr(response, 'error'):
            print("Error:", response.error)
        # If response has orders or orderIds, print them:
        if hasattr(response, 'orders'):
            print("Orders:", response.orders)
    except grpc.RpcError as e:
        print("gRPC call failed:", e.code(), e.details())
        return None
    except Exception as e:
        print("An unexpected error occurred:", e)
        return None
    return response

if __name__ == "__main__":
    from kelai_example_orders import *
    order_list = [DASH_param_on_close]
    send_orders(order_list=order_list, _env='UAT')