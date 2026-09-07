import sys
import grpc
import platform
import numpy as np
import pandas as pd
from datetime import datetime

if platform.system() == 'Windows':
    sys.path.append('{your api package path/flex_api/API}')
    
    import API.Orders_pb2 as Orders_pb2
    import API.Views_pb2 as Views_pb2
    import API.DomainCommons_pb2 as DomainCommons_pb2

    import API.Views_pb2_grpc as ViewService
    import API.Orders_pb2_grpc as OrderService
    from API.Token import UAT, PROD, metadata

else:
    import API.Orders_pb2 as Orders_pb2
    import API.Views_pb2 as Views_pb2
    import API.DomainCommons_pb2 as DomainCommons_pb2

    import API.Views_pb2_grpc as ViewService
    import API.Orders_pb2_grpc as OrderService
    from API.Token import UAT, PROD, metadata

grpc_options = [('grpc.max_message_length', 512 * 1024 * 1024),
                ('grpc.max_receive_message_length', 512 * 1024 * 1024),
                ('grpc.keepalive_time_ms', 330000)]

def get_grpc_channel(env='PROD'):
    """
    Returns a gRPC channel for the specified environment.
    """
    if env == 'UAT':
        return grpc.insecure_channel(UAT, options=grpc_options)
    elif env == 'PROD':
        return grpc.insecure_channel(PROD, options=grpc_options)
    else:
        raise ValueError("Invalid environment specified. Use 'UAT' or 'PROD'.")
    

def get_positions(env = 'PROD'):

    channel = get_grpc_channel(env)

    stub = OrderService.OrderServiceStub(channel)

    positionsRequest = Orders_pb2.ReplayPositionsRequest(sequenceId=0)
    positions = stub.ReplayPositions(positionsRequest, timeout=100, metadata=metadata)

    positions_list = []
    for position in positions:
        attributes_dict = {kv.key: kv.value.stringValue for kv in position.attributes}
        
        position_data = {
            'account': position.account,
            'symbol': position.symbol,
            'fund': position.fund,
            'primeBroker': position.primeBroker,
            'primeBrokerAccount': position.primeBrokerAccount,
            'quantity': position.quantity,
            'weightedAveragePrice': position.weightedAveragePrice,
            'sequenceId': position.sequenceId,
            'transmissionTime': position.transmissionTime,
            'settledQuantity': position.settledQuantity,
            'currency': position.currency
        }
        
        # Merge the attributes dictionary with the main position data
        position_data.update(attributes_dict)
        
        positions_list.append(position_data)

    df = pd.DataFrame(positions_list)
    return df

def get_orders(Orderdate, env = 'PROD'):

    channel = get_grpc_channel(env)

    stub = OrderService.OrderServiceStub(channel)
    orderQueryRequest = DomainCommons_pb2.OrderQueryRequest()

    Orderdate_str = datetime.strptime(Orderdate, '%Y-%m-%d')
    formatted_date = Orderdate_str.strftime('%m/%d/%Y')

    orderQueryRequest.fromDate = formatted_date
    orderQueryRequest.toDate = formatted_date
    
    orderQueryRequest.queryType = DomainCommons_pb2.ALL_ORDERS
    
    orderQueryResponse = stub.GetOrderInfo2(orderQueryRequest, timeout=100, metadata=metadata)

    orders_list = []
    for response in orderQueryResponse:
        for order in response.orders:
            attributes_data = {kv.key: kv.value.stringValue for kv in order.attributes}
            
            account_targets_data = {}
            
            if order.accountTargets:
                for target in order.accountTargets:
                    account_targets_data.update({
                        'positionGroup_acc_tgt': target.positionGroup,
                        'fund_acc_tgt': target.fund,
                        'primeBroker_acc_tgt': target.primeBroker,
                        'quantity_acc_tgt': target.quantity,
                        'filledQuantity_acc_tgt': target.filledQuantity
                    })
            
            street_orders_data = {}
            if order.streetOrders:
                for street_order in order.streetOrders:
                    street_orders_data.update({
                        'orderId_st': street_order.orderId,
                        'executingBroker_st': street_order.executingBroker,
                        'quantity_st': street_order.quantity,
                        'filledQuantity_st': street_order.filledQuantity,
                        'weightedAvgPrice_st': street_order.weightedAvgPrice,
                        'status_st': street_order.status,
                        'commissions_st': street_order.commissions,
                        'settlementDate_st': street_order.settlementDate,
                        'currency_st': street_order.currency,
                        'fxRate_st': street_order.fxRate,
                        'tradingCurrency_st': street_order.tradingCurrency,
                        'settlementCurrency_st': street_order.settlementCurrency,
                        'settlementFxRate_st': street_order.settlementFxRate,
                        'tradeDate_st': street_order.tradeDate,
                        'createdTime_st': street_order.createdTime,
                        'id_st': street_order.id
                    })
                    
                    # Add attributes from street orders
                    for kv in street_order.attributes:
                        if kv.value.HasField('stringValue'):
                            street_orders_data[f'{kv.key}_st'] = kv.value.stringValue
                        if kv.value.HasField('doubleValue'):
                            street_orders_data[f'{kv.key}_st'] = kv.value.doubleValue

            orders_data = {
                'id': order.id,
                'orderId': order.orderId,
                'batchId': order.batchId,
                'user': order.user,
                'symbol': order.symbol,
                'orderType': order.orderType,
                'side': order.side,
                'limitPrice': order.limitPrice,
                'quantity': order.quantity,
                'filledQuantity': order.filledQuantity,
                'weightedAvgPrice': order.weightedAvgPrice,
                'fixTags': order.fixTags,
                'tradingCurrency': order.tradingCurrency,
                'settlementCurrency': order.settlementCurrency,
                'tradeDate': order.tradeDate,
                'settleDate': order.settleDate,
                'status': order.status,
                'finalizationStatus': order.finalizationStatus,
                'complianceStatus': order.complianceStatus,
                'sequenceId': order.sequenceId,
                'trader': order.trader,
                'owner': order.owner,
                'inputQuantity': order.inputQuantity,
                'createdTime': order.createdTime,
                'clientBatchIdentifier': order.clientBatchIdentifier,
                'flexSecurityId': order.flexSecurityId
            }

            orders_data.update(attributes_data)
            orders_data.update(street_orders_data)
            orders_data.update(account_targets_data)
            orders_list.append(orders_data)

    df = pd.DataFrame(orders_list)
    return df

def get_orders_detailed(Orderdate, env = 'PROD'):

    channel = get_grpc_channel(env)
    stub = OrderService.OrderServiceStub(channel)
    orderQueryRequest = DomainCommons_pb2.OrderQueryRequest()

    Orderdate_str = datetime.strptime(Orderdate, '%Y-%m-%d')
    formatted_date = Orderdate_str.strftime('%m/%d/%Y')

    orderQueryRequest.fromDate = formatted_date
    orderQueryRequest.toDate = formatted_date
    orderQueryRequest.queryType = DomainCommons_pb2.ALL_ORDERS

    orderQueryResponse = stub.GetOrderInfo2(orderQueryRequest, timeout=100, metadata=metadata)

    rows = []
    for response in orderQueryResponse:
        for order in response.orders:
            # Flatten order-level attributes
            order_attrs = {kv.key: kv.value.stringValue for kv in order.attributes if kv.value.HasField('stringValue')}
            order_attrs.update({kv.key: kv.value.doubleValue for kv in order.attributes if kv.value.HasField('doubleValue')})

            order_data = {
                'id': order.id,
                'orderId': order.orderId,
                'batchId': order.batchId,
                'user': order.user,
                'symbol': order.symbol,
                'orderType': order.orderType,
                'side': order.side,
                'limitPrice': order.limitPrice,
                'quantity': order.quantity,
                'filledQuantity': order.filledQuantity,
                'weightedAvgPrice': order.weightedAvgPrice,
                'fixTags': order.fixTags,
                'tradingCurrency': order.tradingCurrency,
                'settlementCurrency': order.settlementCurrency,
                'tradeDate': order.tradeDate,
                'settleDate': order.settleDate,
                'status': order.status,
                'finalizationStatus': order.finalizationStatus,
                'complianceStatus': order.complianceStatus,
                'sequenceId': order.sequenceId,
                'trader': order.trader,
                'owner': order.owner,
                'inputQuantity': order.inputQuantity,
                'createdTime': order.createdTime,
                'clientBatchIdentifier': order.clientBatchIdentifier,
                'flexSecurityId': order.flexSecurityId,
                **order_attrs
            }

            # If no streetOrders, still want to capture order-level data
            if not order.streetOrders:
                rows.append(order_data)
                continue

            for street_order in order.streetOrders:
                # Flatten street order attributes
                st_attrs = {kv.key: kv.value.stringValue for kv in street_order.attributes if kv.value.HasField('stringValue')}
                st_attrs.update({kv.key: kv.value.doubleValue for kv in street_order.attributes if kv.value.HasField('doubleValue')})

                street_order_data = {
                    'orderId_st': street_order.orderId,
                    'executingBroker_st': street_order.executingBroker,
                    'quantity_st': street_order.quantity,
                    'filledQuantity_st': street_order.filledQuantity,
                    'weightedAvgPrice_st': street_order.weightedAvgPrice,
                    'status_st': street_order.status,
                    'commissions_st': street_order.commissions,
                    'settlementDate_st': street_order.settlementDate,
                    'currency_st': street_order.currency,
                    'fxRate_st': street_order.fxRate,
                    'tradingCurrency_st': street_order.tradingCurrency,
                    'settlementCurrency_st': street_order.settlementCurrency,
                    'settlementFxRate_st': street_order.settlementFxRate,
                    'tradeDate_st': street_order.tradeDate,
                    'createdTime_st': street_order.createdTime,
                    'id_st': street_order.id,
                    **st_attrs
                }

                # If no allocations, still want to capture street order-level data
                if not street_order.allocations:
                    row = {**order_data, **street_order_data}
                    rows.append(row)
                    continue

                for alloc in street_order.allocations:
                    # Flatten allocation attributes
                    alloc_attrs = {kv.key: kv.value.stringValue for kv in alloc.attributes if kv.value.HasField('stringValue')}
                    alloc_attrs.update({kv.key: kv.value.doubleValue for kv in alloc.attributes if kv.value.HasField('doubleValue')})

                    # Flatten allocation strategies (fix)
                    strategies = {}
                    for strat in getattr(alloc, "strategies", []):
                        if hasattr(strat, "key") and hasattr(strat, "value"):
                            if strat.value.HasField('customTypedValue'):
                                strategies[strat.key] = strat.value.customTypedValue.value

                    alloc_data = {
                        'positionGroup_alloc': alloc.positionGroup,
                        'fund_alloc': alloc.fund,
                        'primeBroker_alloc': alloc.primeBroker,
                        'filledQuantity_alloc': alloc.filledQuantity,
                        'commissions_alloc': alloc.commissions,
                        'executingBroker_alloc': alloc.executingBroker,
                        'id_alloc': alloc.id,
                        **alloc_attrs,
                        **strategies
                    }

                    row = {**order_data, **street_order_data, **alloc_data}
                    rows.append(row)

    df = pd.DataFrame(rows)
    return df


def get_pnl(user, portfolio_name, col_names, env = 'PROD'):

    channel = get_grpc_channel(env)
    stub = ViewService.ViewServiceStub(channel)

    viewRequest = Views_pb2.ViewRequest()
    viewRequest.user = user
    viewRequest.viewType = 5

    for col in col_names:
        viewRequest.columnsToInclude.append(col)

    option = viewRequest.optionEntry.add()
    option.key = 'Portfolio'
    option.stringValue = portfolio_name

    response = stub.GenerateView(viewRequest, timeout=None, metadata=metadata)

    rows_list = []
    upd_col_names = []

    for update in response:
        for col in update.header.column:
            upd_col_names.append(' '.join([str(c) for c in [col.name, col.grouping] if len(str(c)) > 0]))
    
        if update.page.pageNumber > 0:
            for row in update.page.row:
                out = []
                for val in row.cell:
                    if val.hasValue:
                        if val.value.HasField('stringValue'):
                            out.append(val.value.stringValue)
                        elif val.value.HasField('doubleValue'):
                            out.append(val.value.doubleValue)
                        elif val.value.HasField('intValue'):
                            out.append(val.value.intValue)
                        elif val.value.HasField('longValue'):
                            out.append(val.value.longValue)
                        elif val.value.HasField('boolValue'):
                            out.append(val.value.boolValue)
                        else:
                            out.append(np.nan)
                    else:
                        out.append(np.nan)
                rows_list.append(out)

    out_df = pd.DataFrame(rows_list, columns=upd_col_names)
    out_df = out_df.drop_duplicates()

    return out_df


if __name__ == '__main__':

    

    #Get current live pnl data for KELAI portfolio

    # portfolio_name can be 'KELAI'
    # test_df = get_pnl(
    #     user="JCO",
    #     portfolio_name='KELAI',
    #     col_names=['Ticker', 'Symbol', 'Security Description', 'SEDOL', 'RIC Code', 'FIGI', 'Bloomberg', 'Fund', 'Group', 'Qty', 'Side', 'DAILY P&L|HCur Total (Sec & Fx)', 'MTD P&L|HCur Total (Sec & Fx)', 
    #                'YTD P&L|HCur Total (Sec & Fx)', 'ITD P&L|HCur Total (Sec & Fx)', 'HCur Exp', 'HCur Gross Exp', 'HCur Long Exp', 'HCur Short Exp', 'Mkt Px', 'TS-Close Price', 
    #                'HCur Commissions', 'Trade Date', 'Prime', 'Sec Type', 'Px Change %', 'Allocation Price', 'SOD Qty', 'Net Traded Qty', 'Net Finalized Qty', 'Long Exp', 'Short Exp', 
    #                'HCur Fees', 'Volume Avg 90D','Mkt Currency', 'Country', 'Currency', 'FX Rate', 'DAILY P&L|HCur Realized', 'DAILY P&L|HCur Unrealized', 'DAILY P&L|Fx Total'],
    #     env='PROD'
    # )

    #Get current live or historical orders data

    # today = pd.Timestamp.now(tz='US/Eastern').normalize().strftime('%Y-%m-%d')
    # test_df= get_orders(today, env='PROD') #get_orders(Orderdate='2026-04-20', env='PROD')

    #Get current positions for KELAI portfolio

    test_df= get_positions(env='PROD')
    
    print(test_df)