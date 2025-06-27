//+------------------------------------------------------------------+
//|                                             SimpleTestEA_Fixed.mq5 |
//|                                  Copyright 2024, MetaQuotes Ltd. |
//|                                             https://www.mql5.com |
//+------------------------------------------------------------------+
#property copyright "Copyright 2024, MetaQuotes Ltd."
#property link      "https://www.mql5.com"
#property version   "1.00"

//--- 包含必要的头文件
#include <Trade\Trade.mqh>

//--- 输入参数
input double   LotSize = 0.01;        // 交易手数
input int      MagicNumber = 12345;   // 魔术数字
input int      StopLossPoints = 200;  // 止损点数（增大）
input int      TakeProfitPoints = 400; // 止盈点数（增大）
input string   TradeSymbol = "US100.cash"; // 交易品种
input int      TicksToTrade = 20;     // 多少个tick后交易
input int      TicksToClose = 30;     // 多少个tick后平仓
input bool     UseStops = false;      // 是否使用止损止盈（默认关闭）

//--- 全局变量
CTrade trade;
int hold_count = 0;
int tick_count = 0;
datetime last_log_time = 0;

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
    trade.SetExpertMagicNumber(MagicNumber);
    
    if(!SymbolSelect(TradeSymbol, true))
    {
        Print("❌ 无法选择交易品种: ", TradeSymbol);
        return(INIT_FAILED);
    }
    
    // 获取品种信息
    double point = SymbolInfoDouble(TradeSymbol, SYMBOL_POINT);
    int digits = (int)SymbolInfoInteger(TradeSymbol, SYMBOL_DIGITS);
    double min_lot = SymbolInfoDouble(TradeSymbol, SYMBOL_VOLUME_MIN);
    double max_lot = SymbolInfoDouble(TradeSymbol, SYMBOL_VOLUME_MAX);
    double lot_step = SymbolInfoDouble(TradeSymbol, SYMBOL_VOLUME_STEP);
    int stops_level = (int)SymbolInfoInteger(TradeSymbol, SYMBOL_TRADE_STOPS_LEVEL);
    
    Print("✅ SimpleTestEA_Fixed 初始化成功");
    Print("📊 交易品种: ", TradeSymbol);
    Print("📏 点值: ", point);
    Print("📏 小数位: ", digits);
    Print("📈 最小手数: ", min_lot);
    Print("📈 最大手数: ", max_lot);
    Print("📈 手数步长: ", lot_step);
    Print("🛡️ 最小止损距离: ", stops_level, " 点");
    Print("💰 账户余额: ", AccountInfoDouble(ACCOUNT_BALANCE));
    Print("⚙️ 使用止损止盈: ", UseStops ? "是" : "否");
    
    // 检查手数是否有效
    if(LotSize < min_lot)
    {
        Print("⚠️ 交易手数太小，调整为最小手数: ", min_lot);
        LotSize = min_lot;
    }
    
    return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    Print("🛑 SimpleTestEA_Fixed 已停止 - 原因: ", reason);
}

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
{
    MqlTick latest_price;
    if(!SymbolInfoTick(TradeSymbol, latest_price))
    {
        Print("❌ 获取价格信息失败");
        return;
    }
    
    tick_count++;
    
    // 每15秒输出一次状态
    if(TimeCurrent() - last_log_time >= 15)
    {
        Print("📊 状态 - Tick: ", tick_count, 
              " | 买价: ", latest_price.ask, 
              " | 卖价: ", latest_price.bid,
              " | 持仓: ", PositionsTotal());
        last_log_time = TimeCurrent();
    }
    
    // 检查交易条件
    if(PositionsTotal() == 0)
    {
        if(tick_count >= TicksToTrade)
        {
            Print("🎯 达到交易条件！准备开仓...");
            OpenBuyOrder(latest_price.ask);
            tick_count = 0;
        }
    }
    else
    {
        CheckForClose();
    }
}

//+------------------------------------------------------------------+
//| 开仓买入函数                                                      |
//+------------------------------------------------------------------+
void OpenBuyOrder(double price)
{
    // 检查账户状态
    double margin_free = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
    Print("💰 可用保证金: ", margin_free);
    
    double sl = 0, tp = 0;
    
    // 只有在启用止损止盈时才设置
    if(UseStops)
    {
        double point = SymbolInfoDouble(TradeSymbol, SYMBOL_POINT);
        int stops_level = (int)SymbolInfoInteger(TradeSymbol, SYMBOL_TRADE_STOPS_LEVEL);
        
        // 确保止损止盈距离足够大
        int actual_sl_points = MathMax(StopLossPoints, stops_level + 10);
        int actual_tp_points = MathMax(TakeProfitPoints, stops_level + 10);
        
        sl = price - actual_sl_points * point;
        tp = price + actual_tp_points * point;
        
        Print("🛡️ 止损距离: ", actual_sl_points, " 点 (", sl, ")");
        Print("🎯 止盈距离: ", actual_tp_points, " 点 (", tp, ")");
    }
    else
    {
        Print("⚠️ 不使用止损止盈，将通过EA逻辑控制风险");
    }
    
    // 执行买入
    bool result;
    if(UseStops)
    {
        result = trade.Buy(LotSize, TradeSymbol, price, sl, tp, "TestEA_Fixed Buy");
    }
    else
    {
        result = trade.Buy(LotSize, TradeSymbol, price, 0, 0, "TestEA_Fixed Buy");
    }
    
    if(result)
    {
        Print("✅ 买入订单成功！");
        Print("   价格: ", price);
        Print("   手数: ", LotSize);
        Print("   订单号: ", trade.ResultOrder());
        hold_count = 0;
    }
    else
    {
        Print("❌ 买入订单失败！");
        Print("   错误代码: ", trade.ResultRetcode());
        Print("   错误描述: ", trade.ResultRetcodeDescription());
        
        // 输出详细的错误信息
        switch(trade.ResultRetcode())
        {
            case TRADE_RETCODE_INVALID_STOPS:
                Print("   💡 建议：止损止盈设置无效，尝试关闭UseStops参数");
                break;
            case TRADE_RETCODE_NOT_ENOUGH_MONEY:
                Print("   💡 建议：资金不足，减少交易手数");
                break;
            case TRADE_RETCODE_MARKET_CLOSED:
                Print("   💡 建议：市场已关闭");
                break;
            case TRADE_RETCODE_INVALID_VOLUME:
                Print("   💡 建议：交易手数无效");
                break;
        }
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
                hold_count++;
                
                // 显示持仓信息
                double position_profit = PositionGetDouble(POSITION_PROFIT);
                double position_volume = PositionGetDouble(POSITION_VOLUME);
                
                if(hold_count % 10 == 0) // 每10个tick显示一次
                {
                    Print("📊 持仓状态 - Tick: ", hold_count, 
                          " | 盈亏: ", position_profit, 
                          " | 手数: ", position_volume);
                }
                
                if(hold_count >= TicksToClose)
                {
                    Print("🎯 达到平仓条件！准备平仓...");
                    
                    if(trade.PositionClose(ticket))
                    {
                        Print("✅ 平仓成功 - 订单号: ", ticket, " | 盈亏: ", position_profit);
                        hold_count = 0;
                    }
                    else
                    {
                        Print("❌ 平仓失败 - 错误代码: ", trade.ResultRetcode());
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
    Print("📈 交易事件发生！");
} 