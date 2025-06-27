//+------------------------------------------------------------------+
//|                                                 SimpleTestEA.mq5 |
//|                                  Copyright 2024, MetaQuotes Ltd. |
//|                                             https://www.mql5.com |
//+------------------------------------------------------------------+
#property copyright "Copyright 2024, MetaQuotes Ltd."
#property link      "https://www.mql5.com"
#property version   "1.00"

//--- 输入参数
input double   LotSize = 0.01;        // 交易手数
input int      MagicNumber = 12345;   // 魔术数字
input int      StopLoss = 100;        // 止损点数
input int      TakeProfit = 200;      // 止盈点数
input string   TradeSymbol = "US100.cash"; // 交易品种

//--- 全局变量
CTrade trade;

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
    // 设置魔术数字
    trade.SetExpertMagicNumber(MagicNumber);
    
    // 检查交易品种是否可用
    if(!SymbolSelect(TradeSymbol, true))
    {
        Print("无法选择交易品种: ", TradeSymbol);
        return(INIT_FAILED);
    }
    
    Print("SimpleTestEA 初始化成功 - 交易品种: ", TradeSymbol);
    return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    Print("SimpleTestEA 已停止");
}

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
{
    // 获取当前价格信息
    MqlTick latest_price;
    if(!SymbolInfoTick(TradeSymbol, latest_price))
    {
        Print("获取价格信息失败");
        return;
    }
    
    // 检查是否有持仓
    if(PositionsTotal() == 0)
    {
        // 简单的买入条件：每100个tick执行一次买入（仅用于测试）
        static int tick_count = 0;
        tick_count++;
        
        if(tick_count >= 100)
        {
            OpenBuyOrder(latest_price.ask);
            tick_count = 0;
        }
    }
    else
    {
        // 检查是否需要平仓
        CheckForClose();
    }
}

//+------------------------------------------------------------------+
//| 开仓买入函数                                                      |
//+------------------------------------------------------------------+
void OpenBuyOrder(double price)
{
    double sl = 0, tp = 0;
    
    // 计算止损止盈
    if(StopLoss > 0)
        sl = price - StopLoss * SymbolInfoDouble(TradeSymbol, SYMBOL_POINT);
    if(TakeProfit > 0)
        tp = price + TakeProfit * SymbolInfoDouble(TradeSymbol, SYMBOL_POINT);
    
    // 执行买入
    if(trade.Buy(LotSize, TradeSymbol, price, sl, tp, "SimpleTestEA Buy"))
    {
        Print("买入订单成功 - 价格: ", price, " 止损: ", sl, " 止盈: ", tp);
    }
    else
    {
        Print("买入订单失败 - 错误代码: ", trade.ResultRetcode());
    }
}

//+------------------------------------------------------------------+
//| 检查平仓条件                                                      |
//+------------------------------------------------------------------+
void CheckForClose()
{
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(PositionSelectByTicket(ticket))
        {
            if(PositionGetString(POSITION_SYMBOL) == TradeSymbol && 
               PositionGetInteger(POSITION_MAGIC) == MagicNumber)
            {
                // 简单的平仓条件：持仓超过50个tick就平仓（仅用于测试）
                static int hold_count = 0;
                hold_count++;
                
                if(hold_count >= 50)
                {
                    if(trade.PositionClose(ticket))
                    {
                        Print("平仓成功 - 订单号: ", ticket);
                        hold_count = 0;
                    }
                }
            }
        }
    }
}

//+------------------------------------------------------------------+
//| 交易事件处理函数                                                  |
//+------------------------------------------------------------------+
void OnTrade()
{
    Print("交易事件发生");
} 