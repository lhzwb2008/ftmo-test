//+------------------------------------------------------------------+
//|                                        MomentumStrategy_US100.mq5 |
//|                                  Copyright 2024, MetaQuotes Ltd. |
//|                                             https://www.mql5.com |
//+------------------------------------------------------------------+
#property copyright "Copyright 2024, MetaQuotes Ltd."
#property link      "https://www.mql5.com"
#property version   "1.00"

//--- 包含必要的头文件
#include <Trade\Trade.mqh>

//--- 输入参数
input string   TradeSymbol = "US100.cash";     // 交易品种
input double   LotSize = 0.1;                  // 交易手数
input int      MagicNumber = 20241227;         // 魔术数字
input int      LookbackDays = 1;               // 历史回看天数
input double   K1_Multiplier = 1.0;            // 上边界sigma乘数
input double   K2_Multiplier = 1.0;            // 下边界sigma乘数
input double   Leverage = 1.8;                 // 杠杆倍数
input int      MaxPositionsPerDay = 10;        // 每日最大开仓次数
input string   TradingStartTime = "16:40";     // 交易开始时间
input string   TradingEndTime = "22:45";       // 交易结束时间
input int      CheckIntervalMinutes = 15;      // 检查间隔（分钟）
input bool     DebugMode = true;               // 调试模式

//--- 全局变量
CTrade trade;
datetime last_check_time = 0;
int positions_opened_today = 0;
datetime last_trading_day = 0;
double entry_price = 0;
double current_stop = 0;
int current_position_type = 0; // 0=无持仓, 1=多头, -1=空头

// 数据存储结构
struct PriceData {
    datetime time;
    double open;
    double high;
    double low;
    double close;
    double volume;
    double vwap;
    double upper_bound;
    double lower_bound;
};

PriceData historical_data[];
int data_size = 0;

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
    // 设置魔术数字
    trade.SetExpertMagicNumber(MagicNumber);
    
    // 检查交易品种
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
    
    Print("=== MomentumStrategy_US100 初始化 ===");
    Print("📊 交易品种: ", TradeSymbol);
    Print("📏 点值: ", point);
    Print("📏 小数位: ", digits);
    Print("📈 最小手数: ", min_lot);
    Print("📈 最大手数: ", max_lot);
    Print("🔧 回看天数: ", LookbackDays);
    Print("🎯 K1乘数: ", K1_Multiplier);
    Print("🎯 K2乘数: ", K2_Multiplier);
    Print("💰 杠杆倍数: ", Leverage);
    Print("⏰ 交易时间: ", TradingStartTime, " - ", TradingEndTime);
    Print("=====================================");
    
    // 初始化历史数据
    LoadHistoricalData();
    
    return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    Print("🛑 MomentumStrategy_US100 已停止");
}

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
{
    // 获取当前时间
    datetime current_time = TimeCurrent();
    
    // 检查是否需要执行策略（根据检查间隔）
    if(current_time - last_check_time < CheckIntervalMinutes * 60)
        return;
        
    last_check_time = current_time;
    
    // 检查是否是新的交易日
    MqlDateTime dt;
    TimeToStruct(current_time, dt);
    datetime current_day = StringToTime(StringFormat("%04d.%02d.%02d", dt.year, dt.mon, dt.day));
    
    if(current_day != last_trading_day)
    {
        positions_opened_today = 0;
        last_trading_day = current_day;
        if(DebugMode)
            Print("📅 新交易日: ", TimeToString(current_day, TIME_DATE));
    }
    
    // 检查是否在交易时间内
    if(!IsInTradingHours())
    {
        // 如果不在交易时间且有持仓，平仓
        if(HasPosition())
        {
            Print("⏰ 交易时间结束，执行平仓");
            CloseAllPositions();
        }
        return;
    }
    
    // 更新历史数据
    UpdateHistoricalData();
    
    // 计算VWAP和噪声区域
    CalculateIndicators();
    
    // 获取当前持仓状态
    UpdatePositionStatus();
    
    // 执行交易逻辑
    if(current_position_type != 0)
    {
        // 有持仓，检查出场条件
        CheckExitConditions();
    }
    else
    {
        // 无持仓，检查入场条件
        if(positions_opened_today < MaxPositionsPerDay)
        {
            CheckEntryConditions();
        }
        else
        {
            if(DebugMode)
                Print("📊 今日已开仓 ", positions_opened_today, " 次，达到上限");
        }
    }
}

//+------------------------------------------------------------------+
//| 加载历史数据                                                      |
//+------------------------------------------------------------------+
void LoadHistoricalData()
{
    // 计算需要的历史数据天数
    int days_needed = LookbackDays + 5; // 额外加载一些数据
    
    // 获取1分钟K线数据
    MqlRates rates[];
    int copied = CopyRates(TradeSymbol, PERIOD_M1, 0, days_needed * 24 * 60, rates);
    
    if(copied <= 0)
    {
        Print("❌ 无法获取历史数据");
        return;
    }
    
    // 调整数组大小
    ArrayResize(historical_data, copied);
    data_size = copied;
    
    // 复制数据到结构体数组
    for(int i = 0; i < copied; i++)
    {
        historical_data[i].time = rates[i].time;
        historical_data[i].open = rates[i].open;
        historical_data[i].high = rates[i].high;
        historical_data[i].low = rates[i].low;
        historical_data[i].close = rates[i].close;
        historical_data[i].volume = (double)rates[i].tick_volume;
        historical_data[i].vwap = 0;
        historical_data[i].upper_bound = 0;
        historical_data[i].lower_bound = 0;
    }
    
    if(DebugMode)
        Print("📊 加载历史数据: ", data_size, " 条");
}

//+------------------------------------------------------------------+
//| 更新历史数据                                                      |
//+------------------------------------------------------------------+
void UpdateHistoricalData()
{
    // 获取最新的K线数据
    MqlRates rates[];
    int copied = CopyRates(TradeSymbol, PERIOD_M1, 0, 100, rates);
    
    if(copied <= 0)
        return;
        
    // 检查是否有新数据
    datetime latest_time = rates[copied-1].time;
    if(data_size > 0 && latest_time <= historical_data[data_size-1].time)
        return;
        
    // 添加新数据
    for(int i = 0; i < copied; i++)
    {
        if(rates[i].time > historical_data[data_size-1].time)
        {
            // 扩展数组
            ArrayResize(historical_data, data_size + 1);
            
            // 添加新数据
            historical_data[data_size].time = rates[i].time;
            historical_data[data_size].open = rates[i].open;
            historical_data[data_size].high = rates[i].high;
            historical_data[data_size].low = rates[i].low;
            historical_data[data_size].close = rates[i].close;
            historical_data[data_size].volume = (double)rates[i].tick_volume;
            historical_data[data_size].vwap = 0;
            historical_data[data_size].upper_bound = 0;
            historical_data[data_size].lower_bound = 0;
            
            data_size++;
        }
    }
}

//+------------------------------------------------------------------+
//| 计算VWAP                                                          |
//+------------------------------------------------------------------+
void CalculateVWAP()
{
    // 按日计算VWAP
    datetime current_day = 0;
    double cumulative_volume = 0;
    double cumulative_turnover = 0;
    
    for(int i = 0; i < data_size; i++)
    {
        MqlDateTime dt;
        TimeToStruct(historical_data[i].time, dt);
        datetime day = StringToTime(StringFormat("%04d.%02d.%02d", dt.year, dt.mon, dt.day));
        
        // 新的一天，重置累计值
        if(day != current_day)
        {
            current_day = day;
            cumulative_volume = 0;
            cumulative_turnover = 0;
        }
        
        // 计算成交额（使用典型价格）
        double typical_price = (historical_data[i].high + historical_data[i].low + historical_data[i].close) / 3;
        double turnover = typical_price * historical_data[i].volume;
        
        // 累计
        cumulative_volume += historical_data[i].volume;
        cumulative_turnover += turnover;
        
        // 计算VWAP
        if(cumulative_volume > 0)
            historical_data[i].vwap = cumulative_turnover / cumulative_volume;
        else
            historical_data[i].vwap = historical_data[i].close;
    }
}

//+------------------------------------------------------------------+
//| 计算噪声区域                                                      |
//+------------------------------------------------------------------+
void CalculateNoiseArea()
{
    if(data_size < LookbackDays * 390) // 确保有足够的历史数据
        return;
        
    // 获取今天的日期
    datetime today = StringToTime(TimeToString(TimeCurrent(), TIME_DATE));
    
    // 获取今天的开盘价和昨天的收盘价
    double today_open = 0;
    double prev_close = 0;
    bool found_today_open = false;
    bool found_prev_close = false;
    
    for(int i = data_size - 1; i >= 0; i--)
    {
        MqlDateTime dt;
        TimeToStruct(historical_data[i].time, dt);
        datetime day = StringToTime(StringFormat("%04d.%02d.%02d", dt.year, dt.mon, dt.day));
        
        // 找到今天的开盘价
        if(day == today && !found_today_open)
        {
            // 检查是否是开盘时间（9:30左右）
            if(dt.hour == 9 && dt.min >= 30)
            {
                today_open = historical_data[i].open;
                found_today_open = true;
            }
        }
        
        // 找到昨天的收盘价
        if(day < today && !found_prev_close)
        {
            // 找到前一天的最后一个数据点
            prev_close = historical_data[i].close;
            found_prev_close = true;
        }
        
        if(found_today_open && found_prev_close)
            break;
    }
    
    if(!found_today_open || !found_prev_close)
        return;
        
    // 计算参考价格
    double upper_ref = MathMax(today_open, prev_close);
    double lower_ref = MathMin(today_open, prev_close);
    
    // 对今天的每个时间点计算边界
    for(int i = data_size - 1; i >= 0; i--)
    {
        MqlDateTime dt;
        TimeToStruct(historical_data[i].time, dt);
        datetime day = StringToTime(StringFormat("%04d.%02d.%02d", dt.year, dt.mon, dt.day));
        
        if(day != today)
            continue;
            
        // 计算该时间点的历史sigma
        double sigma = CalculateTimeSigma(dt.hour, dt.min);
        
        // 计算边界
        historical_data[i].upper_bound = upper_ref * (1 + K1_Multiplier * sigma);
        historical_data[i].lower_bound = lower_ref * (1 - K2_Multiplier * sigma);
    }
}

//+------------------------------------------------------------------+
//| 计算特定时间点的sigma                                             |
//+------------------------------------------------------------------+
double CalculateTimeSigma(int hour, int minute)
{
    double sum_moves = 0;
    int count = 0;
    
    // 遍历历史数据，找到相同时间点的数据
    for(int i = 0; i < data_size; i++)
    {
        MqlDateTime dt;
        TimeToStruct(historical_data[i].time, dt);
        
        // 匹配时间点
        if(dt.hour == hour && dt.min == minute)
        {
            // 找到当天的开盘价
            double day_open = 0;
            datetime day_start = StringToTime(StringFormat("%04d.%02d.%02d", dt.year, dt.mon, dt.day));
            
            for(int j = i; j >= 0; j--)
            {
                MqlDateTime dt2;
                TimeToStruct(historical_data[j].time, dt2);
                datetime day2 = StringToTime(StringFormat("%04d.%02d.%02d", dt2.year, dt2.mon, dt2.day));
                
                if(day2 == day_start && dt2.hour == 9 && dt2.min >= 30)
                {
                    day_open = historical_data[j].open;
                    break;
                }
                
                if(day2 < day_start)
                    break;
            }
            
            if(day_open > 0)
            {
                // 计算相对变动率
                double move = MathAbs(historical_data[i].close / day_open - 1);
                sum_moves += move;
                count++;
            }
        }
    }
    
    if(count > 0)
        return sum_moves / count;
    else
        return 0.01; // 默认值
}

//+------------------------------------------------------------------+
//| 计算所有指标                                                      |
//+------------------------------------------------------------------+
void CalculateIndicators()
{
    CalculateVWAP();
    CalculateNoiseArea();
}

//+------------------------------------------------------------------+
//| 检查是否在交易时间内                                              |
//+------------------------------------------------------------------+
bool IsInTradingHours()
{
    MqlDateTime dt;
    TimeToStruct(TimeCurrent(), dt);
    
    // 解析交易时间
    string start_parts[];
    string end_parts[];
    StringSplit(TradingStartTime, ':', start_parts);
    StringSplit(TradingEndTime, ':', end_parts);
    
    int start_hour = (int)StringToInteger(start_parts[0]);
    int start_min = (int)StringToInteger(start_parts[1]);
    int end_hour = (int)StringToInteger(end_parts[0]);
    int end_min = (int)StringToInteger(end_parts[1]);
    
    // 转换为分钟
    int current_minutes = dt.hour * 60 + dt.min;
    int start_minutes = start_hour * 60 + start_min;
    int end_minutes = end_hour * 60 + end_min;
    
    return (current_minutes >= start_minutes && current_minutes <= end_minutes);
}

//+------------------------------------------------------------------+
//| 检查是否有持仓                                                    |
//+------------------------------------------------------------------+
bool HasPosition()
{
    return (current_position_type != 0);
}

//+------------------------------------------------------------------+
//| 更新持仓状态                                                      |
//+------------------------------------------------------------------+
void UpdatePositionStatus()
{
    current_position_type = 0;
    
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(PositionSelectByTicket(ticket))
        {
            if(PositionGetString(POSITION_SYMBOL) == TradeSymbol && 
               PositionGetInteger(POSITION_MAGIC) == MagicNumber)
            {
                long position_type = PositionGetInteger(POSITION_TYPE);
                if(position_type == POSITION_TYPE_BUY)
                    current_position_type = 1;
                else if(position_type == POSITION_TYPE_SELL)
                    current_position_type = -1;
                    
                entry_price = PositionGetDouble(POSITION_PRICE_OPEN);
                break;
            }
        }
    }
}

//+------------------------------------------------------------------+
//| 检查入场条件                                                      |
//+------------------------------------------------------------------+
void CheckEntryConditions()
{
    if(data_size < 2)
        return;
        
    // 获取最新数据
    PriceData &latest = historical_data[data_size - 1];
    double current_price = SymbolInfoDouble(TradeSymbol, SYMBOL_LAST);
    
    if(latest.vwap == 0 || latest.upper_bound == 0 || latest.lower_bound == 0)
        return;
        
    // 检查多头入场条件
    bool long_condition = (current_price > latest.upper_bound) && (current_price > latest.vwap);
    
    // 检查空头入场条件
    bool short_condition = (current_price < latest.lower_bound) && (current_price < latest.vwap);
    
    if(DebugMode && (long_condition || short_condition))
    {
        Print("📊 当前价格: ", current_price);
        Print("📊 上边界: ", latest.upper_bound);
        Print("📊 VWAP: ", latest.vwap);
        Print("📊 下边界: ", latest.lower_bound);
    }
    
    if(long_condition)
    {
        Print("🔵 触发多头入场信号!");
        current_stop = MathMax(latest.upper_bound, latest.vwap);
        OpenPosition(1); // 开多
    }
    else if(short_condition)
    {
        Print("🔴 触发空头入场信号!");
        current_stop = MathMin(latest.lower_bound, latest.vwap);
        OpenPosition(-1); // 开空
    }
}

//+------------------------------------------------------------------+
//| 检查出场条件                                                      |
//+------------------------------------------------------------------+
void CheckExitConditions()
{
    if(data_size < 2)
        return;
        
    // 获取最新数据
    PriceData &latest = historical_data[data_size - 1];
    double current_price = SymbolInfoDouble(TradeSymbol, SYMBOL_LAST);
    
    if(latest.vwap == 0 || latest.upper_bound == 0 || latest.lower_bound == 0)
        return;
        
    bool exit_signal = false;
    
    if(current_position_type > 0) // 多头持仓
    {
        // 更新止损
        double new_stop = MathMax(latest.upper_bound, latest.vwap);
        current_stop = MathMax(current_stop, new_stop); // 移动止损只能向上
        
        // 检查出场条件
        exit_signal = (current_price < current_stop);
        
        if(DebugMode && MQLInfoInteger(MQL_TESTER) == 0) // 非回测模式
        {
            static datetime last_log_time = 0;
            if(TimeCurrent() - last_log_time > 300) // 每5分钟记录一次
            {
                Print("📊 多头持仓 - 价格: ", current_price, " | 止损: ", current_stop);
                last_log_time = TimeCurrent();
            }
        }
    }
    else if(current_position_type < 0) // 空头持仓
    {
        // 更新止损
        double new_stop = MathMin(latest.lower_bound, latest.vwap);
        current_stop = MathMin(current_stop, new_stop); // 移动止损只能向下
        
        // 检查出场条件
        exit_signal = (current_price > current_stop);
        
        if(DebugMode && MQLInfoInteger(MQL_TESTER) == 0) // 非回测模式
        {
            static datetime last_log_time = 0;
            if(TimeCurrent() - last_log_time > 300) // 每5分钟记录一次
            {
                Print("📊 空头持仓 - 价格: ", current_price, " | 止损: ", current_stop);
                last_log_time = TimeCurrent();
            }
        }
    }
    
    if(exit_signal)
    {
        Print("🛑 触发出场信号!");
        CloseAllPositions();
    }
}

//+------------------------------------------------------------------+
//| 开仓函数                                                          |
//+------------------------------------------------------------------+
void OpenPosition(int signal)
{
    double current_price = SymbolInfoDouble(TradeSymbol, SYMBOL_ASK);
    if(signal < 0) // 如果是卖出，使用Bid价格
        current_price = SymbolInfoDouble(TradeSymbol, SYMBOL_BID);
        
    // 计算仓位大小
    double account_balance = AccountInfoDouble(ACCOUNT_BALANCE);
    double available_capital = account_balance * Leverage;
    double position_size = LotSize; // 使用固定手数
    
    // 检查最小/最大手数
    double min_lot = SymbolInfoDouble(TradeSymbol, SYMBOL_VOLUME_MIN);
    double max_lot = SymbolInfoDouble(TradeSymbol, SYMBOL_VOLUME_MAX);
    double lot_step = SymbolInfoDouble(TradeSymbol, SYMBOL_VOLUME_STEP);
    
    // 调整手数到合法值
    position_size = MathMax(position_size, min_lot);
    position_size = MathMin(position_size, max_lot);
    position_size = MathRound(position_size / lot_step) * lot_step;
    
    Print("💰 账户余额: ", account_balance);
    Print("💰 杠杆调整后资金: ", available_capital);
    Print("📈 开仓手数: ", position_size);
    
    bool result = false;
    
    if(signal > 0) // 买入
    {
        result = trade.Buy(position_size, TradeSymbol, current_price, 0, 0, "Momentum买入");
    }
    else // 卖出
    {
        result = trade.Sell(position_size, TradeSymbol, current_price, 0, 0, "Momentum卖出");
    }
    
    if(result)
    {
        Print("✅ 开仓成功！");
        Print("   订单号: ", trade.ResultOrder());
        Print("   价格: ", current_price);
        positions_opened_today++;
        current_position_type = signal;
        entry_price = current_price;
    }
    else
    {
        Print("❌ 开仓失败！");
        Print("   错误代码: ", trade.ResultRetcode());
        Print("   错误描述: ", trade.ResultRetcodeDescription());
    }
}

//+------------------------------------------------------------------+
//| 平仓所有持仓                                                      |
//+------------------------------------------------------------------+
void CloseAllPositions()
{
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(PositionSelectByTicket(ticket))
        {
            if(PositionGetString(POSITION_SYMBOL) == TradeSymbol && 
               PositionGetInteger(POSITION_MAGIC) == MagicNumber)
            {
                double position_profit = PositionGetDouble(POSITION_PROFIT);
                double position_volume = PositionGetDouble(POSITION_VOLUME);
                
                if(trade.PositionClose(ticket))
                {
                    Print("✅ 平仓成功！");
                    Print("   订单号: ", ticket);
                    Print("   手数: ", position_volume);
                    Print("   盈亏: ", position_profit);
                    
                    // 计算百分比收益
                    if(entry_price > 0)
                    {
                        double current_price = PositionGetDouble(POSITION_PRICE_CURRENT);
                        double pnl_pct = 0;
                        
                        if(current_position_type > 0) // 多头
                            pnl_pct = (current_price / entry_price - 1) * 100;
                        else // 空头
                            pnl_pct = (entry_price / current_price - 1) * 100;
                            
                        Print("   收益率: ", DoubleToString(pnl_pct, 2), "%");
                    }
                    
                    current_position_type = 0;
                    entry_price = 0;
                    current_stop = 0;
                }
                else
                {
                    Print("❌ 平仓失败！");
                    Print("   错误代码: ", trade.ResultRetcode());
                }
            }
        }
    }
}

//+------------------------------------------------------------------+
//| 交易事件处理                                                      |
//+------------------------------------------------------------------+
void OnTrade()
{
    if(DebugMode)
        Print("📈 交易事件触发");
} 