//+------------------------------------------------------------------+
//|                                   SQLiteSignalEA_darwinex.mq5     |
//|                          简化版SQLite信号执行EA（Darwinex 版）     |
//|  挂在 Darwinex MT5 的纳指品种图表上（指数CFD: NDX；或 ETF: QQQ）   |
//|  Darwinex 无 prop firm 回撤规则，但请保持杠杆/手数风格稳定，       |
//|  以免被平台 Risk Engine（月 VaR 6.5%）降杠杆影响 DARWIN 表现。     |
//+------------------------------------------------------------------+
#property copyright "Copyright 2024"
#property link      ""
#property version   "1.02"
#property strict

// 添加必要的权限声明
#property script_show_inputs

#include <Trade\Trade.mqh>
#include <Trade\AccountInfo.mqh>

//--- 输入参数
input string   DBPath = "trading_signals_darwinex.db";      // SQLite数据库文件名
input bool     UseCommonPath = true;                   // 使用通用目录（推荐）
input int      MagicNumber = 20260612;                 // 魔术数字
input double   Leverage = 1.5;                      // 杠杆倍数（VaR 测算最优值，与 simulate_darwinex.py 保持一致）
input double   RiskPercent = 100.0;                    // 使用余额百分比(%)
input int      CheckIntervalSeconds = 1;               // 检查间隔（秒）
input int      PreMarketCleanupHour = 9;               // 开盘前清仓：美东时间小时（与 Python 策略一致）
input int      PreMarketCleanupMinute = 25;            // 开盘前清仓：美东时间分钟（默认9:25，美股9:30开盘）
input int      MaxSignalAgeMinutes = 20;               // 信号最大有效分钟数（SQLite UTC 时间戳，超时丢弃）

//--- 全局变量
CTrade trade;
datetime last_check_time = 0;
int db_handle = INVALID_HANDLE;
datetime last_cleanup_us_date = 0;
bool last_terminal_connected = true;

//+------------------------------------------------------------------+
//| 将 UTC 年月日时分秒转为 MQL5 datetime（与 TimeGMT 可比）           |
//+------------------------------------------------------------------+
datetime MakeUtcDatetime(int year, int mon, int day, int hour, int min, int sec)
{
    MqlDateTime dt;
    dt.year = year;
    dt.mon = mon;
    dt.day = day;
    dt.hour = hour;
    dt.min = min;
    dt.sec = sec;
    datetime local_parsed = StructToTime(dt);
    return local_parsed + (TimeCurrent() - TimeGMT());
}

//+------------------------------------------------------------------+
//| 某月第 N 个星期 X 的日期（dow: 0=周日）                            |
//+------------------------------------------------------------------+
int NthWeekdayOfMonth(int year, int month, int nth, int dow)
{
    MqlDateTime dt;
    dt.year = year;
    dt.mon = month;
    dt.day = 1;
    dt.hour = 12;
    dt.min = 0;
    dt.sec = 0;
    datetime first = StructToTime(dt);
    TimeToStruct(first, dt);
    int first_dow = dt.day_of_week;
    return 1 + ((dow - first_dow + 7) % 7) + (nth - 1) * 7;
}

//+------------------------------------------------------------------+
//| 美国东部是否处于夏令时（基于 UTC，自动处理 DST 切换）               |
//+------------------------------------------------------------------+
bool IsUsDaylightSavingTime(datetime utc_time)
{
    MqlDateTime dt;
    TimeToStruct(utc_time, dt);
    int y = dt.year;

    int march_day = NthWeekdayOfMonth(y, 3, 2, 0);
    datetime dst_start = MakeUtcDatetime(y, 3, march_day, 7, 0, 0);

    int nov_day = NthWeekdayOfMonth(y, 11, 1, 0);
    datetime dst_end = MakeUtcDatetime(y, 11, nov_day, 6, 0, 0);

    return utc_time >= dst_start && utc_time < dst_end;
}

//+------------------------------------------------------------------+
//| 当前美东时间（与 simulate_darwinex.py 对齐）                        |
//+------------------------------------------------------------------+
datetime TimeUsEastern()
{
    datetime utc = TimeGMT();
    int offset = IsUsDaylightSavingTime(utc) ? -4 * 3600 : -5 * 3600;
    return utc + offset;
}

//+------------------------------------------------------------------+
//| 美东时间是否已到指定时刻                                           |
//+------------------------------------------------------------------+
bool IsUsEasternAtOrAfter(int target_hour, int target_minute)
{
    MqlDateTime dt;
    TimeToStruct(TimeUsEastern(), dt);
    if(dt.hour > target_hour)
        return true;
    if(dt.hour == target_hour && dt.min >= target_minute)
        return true;
    return false;
}

//+------------------------------------------------------------------+
//| 解析 SQLite UTC 时间戳 "YYYY-MM-DD HH:MM:SS"                       |
//+------------------------------------------------------------------+
datetime ParseUtcTimestamp(string ts)
{
    string parts[];
    if(StringSplit(ts, ' ', parts) != 2)
        return 0;

    string date_parts[], time_parts[];
    if(StringSplit(parts[0], '-', date_parts) != 3)
        return 0;
    if(StringSplit(parts[1], ':', time_parts) != 3)
        return 0;

    return MakeUtcDatetime(
        (int)StringToInteger(date_parts[0]),
        (int)StringToInteger(date_parts[1]),
        (int)StringToInteger(date_parts[2]),
        (int)StringToInteger(time_parts[0]),
        (int)StringToInteger(time_parts[1]),
        (int)StringToInteger(time_parts[2])
    );
}

//+------------------------------------------------------------------+
//| 信号是否已过期（created_at 为 SQLite UTC，与 TimeGMT 比较）         |
//+------------------------------------------------------------------+
bool IsSignalExpired(string created_at)
{
    if(MaxSignalAgeMinutes <= 0)
        return false;

    datetime signal_utc = ParseUtcTimestamp(created_at);
    if(signal_utc <= 0)
        return false;

    return (TimeGMT() - signal_utc) > MaxSignalAgeMinutes * 60;
}

//+------------------------------------------------------------------+
//| 启动时打印时间诊断信息                                             |
//+------------------------------------------------------------------+
void LogTimeDiagnostics()
{
    MqlDateTime srv, et;
    TimeToStruct(TimeCurrent(), srv);
    TimeToStruct(TimeUsEastern(), et);

    int server_gmt_offset_hours = (int)((TimeCurrent() - TimeGMT()) / 3600);
    string dst_label = IsUsDaylightSavingTime(TimeGMT()) ? "EDT(UTC-4)" : "EST(UTC-5)";

    Print("=== Darwinex 时间诊断 ===");
    Print("服务器时间: ", TimeToString(TimeCurrent(), TIME_DATE|TIME_MINUTES|TIME_SECONDS));
    Print("UTC/GMT: ", TimeToString(TimeGMT(), TIME_DATE|TIME_MINUTES|TIME_SECONDS));
    Print("美东时间: ", TimeToString(TimeUsEastern(), TIME_DATE|TIME_MINUTES|TIME_SECONDS), " (", dst_label, ")");
    Print("服务器 GMT 偏移: GMT", (server_gmt_offset_hours >= 0 ? "+" : ""), server_gmt_offset_hours);
    Print("开盘前清仓触发: 美东 ", PreMarketCleanupHour, ":", StringFormat("%02d", PreMarketCleanupMinute));
    Print("信号有效期: ", MaxSignalAgeMinutes, " 分钟");
}

//+------------------------------------------------------------------+
//| Expert initialization function                                    |
//+------------------------------------------------------------------+
int OnInit()
{
    // 设置魔术数字
    trade.SetExpertMagicNumber(MagicNumber);
    
    // 打开SQLite数据库
    if(!OpenDatabase())
    {
        Print("❌ 无法打开SQLite数据库");
        return(INIT_FAILED);
    }
    
    // 启动定时器作为OnTick的补充，断线时OnTick不触发，定时器可兜底
    EventSetTimer(CheckIntervalSeconds);
    
    LogTimeDiagnostics();
    
    Print("✅ EA初始化成功");
    Print("💰 杠杆: ", Leverage, "倍");
    Print("📊 使用余额: ", RiskPercent, "%");
    
    return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    EventKillTimer();
    
    if(db_handle != INVALID_HANDLE)
    {
        DatabaseClose(db_handle);
        db_handle = INVALID_HANDLE;
    }
    
    Print("EA已停止，原因代码: ", reason);
}

//+------------------------------------------------------------------+
//| Expert tick function                                              |
//+------------------------------------------------------------------+
void OnTick()
{
    PreMarketCleanup();
    
    datetime current_time = TimeCurrent();
    if(current_time - last_check_time < CheckIntervalSeconds)
        return;
        
    last_check_time = current_time;
    CheckDatabaseSignals();
}

//+------------------------------------------------------------------+
//| Timer function - 断线时OnTick不触发，定时器兜底                     |
//+------------------------------------------------------------------+
void OnTimer()
{
    bool connected = (bool)TerminalInfoInteger(TERMINAL_CONNECTED);
    if(!connected && last_terminal_connected)
        Print("⚠️ 终端与 Darwinex 断连，OnTick 已停止；定时器仍在轮询 DB，但下单需等重连后才会成功");
    else if(connected && !last_terminal_connected)
        Print("✅ 终端已重新连接 Darwinex");
    last_terminal_connected = connected;

    PreMarketCleanup();
    CheckDatabaseSignals();
}

//+------------------------------------------------------------------+
//| 开盘前清仓：按美东时间触发，避免服务器时区与本地时间混淆            |
//+------------------------------------------------------------------+
void PreMarketCleanup()
{
    if(!IsUsEasternAtOrAfter(PreMarketCleanupHour, PreMarketCleanupMinute))
        return;

    MqlDateTime et;
    TimeToStruct(TimeUsEastern(), et);
    datetime today_us = StringToTime(StringFormat("%04d.%02d.%02d", et.year, et.mon, et.day));
    if(today_us == last_cleanup_us_date)
        return;

    last_cleanup_us_date = today_us;

    int position_type = GetPositionType();
    if(position_type != 0)
    {
        Print("⚠️ 美东 ", et.hour, ":", StringFormat("%02d", et.min),
              " 开盘前检测到残留持仓（", (position_type == 1 ? "多仓" : "空仓"), "），执行清仓");
        CloseAllPositions();

        if(db_handle != INVALID_HANDLE)
            MarkAllSignalsConsumed();
    }
}

//+------------------------------------------------------------------+
//| 打开SQLite数据库                                                  |
//+------------------------------------------------------------------+
bool OpenDatabase()
{
    string full_path;
    
    // 根据设置选择路径
    if(UseCommonPath)
    {
        // 使用通用目录 - 所有MT5终端共享
        full_path = TerminalInfoString(TERMINAL_COMMONDATA_PATH) + "\\Files\\" + DBPath;
    }
    else
    {
        // 使用当前终端的Files目录
        full_path = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Files\\" + DBPath;
    }
    
    // 打印调试信息
    Print("🔍 尝试打开数据库: ", full_path);
    Print("🔍 使用通用目录: ", UseCommonPath ? "是" : "否");
    Print("🔍 当前终端目录: ", TerminalInfoString(TERMINAL_DATA_PATH));
    Print("🔍 通用数据目录: ", TerminalInfoString(TERMINAL_COMMONDATA_PATH));
    
    // 方式1: 尝试读写模式
    if(UseCommonPath)
    {
        db_handle = DatabaseOpen(DBPath, DATABASE_OPEN_READWRITE | DATABASE_OPEN_COMMON);
    }
    else
    {
        db_handle = DatabaseOpen(DBPath, DATABASE_OPEN_READWRITE);
    }
    
    if(db_handle == INVALID_HANDLE)
    {
        Print("❌ 读写模式失败，尝试只读模式...");
        if(UseCommonPath)
        {
            db_handle = DatabaseOpen(DBPath, DATABASE_OPEN_READONLY | DATABASE_OPEN_COMMON);
        }
        else
        {
            db_handle = DatabaseOpen(DBPath, DATABASE_OPEN_READONLY);
        }
    }
    
    if(db_handle == INVALID_HANDLE)
    {
        Print("❌ 所有方式都失败了");
        Print("❌ 最后错误代码: ", GetLastError());
        Print("❌ 请检查以下事项:");
        Print("   1. 数据库文件是否存在于正确目录");
        Print("   2. 文件权限是否正确");
        Print("   3. 数据库文件是否损坏");
        Print("   4. 建议的解决方案:");
        if(UseCommonPath)
        {
            Print("      - 将数据库文件复制到: ", TerminalInfoString(TERMINAL_COMMONDATA_PATH), "\\Files\\");
        }
        else
        {
            Print("      - 将数据库文件复制到: ", TerminalInfoString(TERMINAL_DATA_PATH), "\\MQL5\\Files\\");
        }
        return false;
    }
    
    Print("✅ 数据库已成功打开: ", full_path);
    return true;
}

//+------------------------------------------------------------------+
//| 检查数据库信号                                                    |
//+------------------------------------------------------------------+
void CheckDatabaseSignals()
{
    if(db_handle == INVALID_HANDLE)
        return;
    
    // 先统计未消费信号数量
    int pending_count = CountPendingSignals();
    if(pending_count <= 0)
        return;
    
    if(pending_count > 1)
    {
        // 多条信号堆积 = 断线期间积压，信号已过期，全部丢弃并平仓
        Print("⚠️ 检测到 ", pending_count, " 条堆积信号，判断为断线积压，全部丢弃");
        MarkAllSignalsConsumed();
        
        int position_type = GetPositionType();
        if(position_type != 0)
        {
            Print("🔄 断线恢复，平掉所有持仓以避免风险敞口");
            CloseAllPositions();
        }
        return;
    }
    
    // 只有1条未消费信号，正常实时处理
    string query = "SELECT id, action, created_at FROM signals WHERE consumed = 0 ORDER BY created_at DESC LIMIT 1";
    
    int request = DatabasePrepare(db_handle, query);
    if(request == INVALID_HANDLE)
    {
        Print("❌ 查询失败");
        return;
    }
    
    if(DatabaseRead(request))
    {
        long signal_id;
        string action;
        string created_at;
        
        DatabaseColumnLong(request, 0, signal_id);
        DatabaseColumnText(request, 1, action);
        DatabaseColumnText(request, 2, created_at);
        
        if(IsSignalExpired(created_at))
        {
            long age_sec = TimeGMT() - ParseUtcTimestamp(created_at);
            Print("⚠️ 信号已过期，丢弃: ", action, " (ID: ", signal_id,
                  ", 年龄 ", age_sec / 60, " 分钟, 上限 ", MaxSignalAgeMinutes, " 分钟)");
            MarkSignalConsumed(signal_id);
            DatabaseFinalize(request);
            return;
        }
        
        Print("📊 检测到未消费信号: ", action, " (ID: ", signal_id, ", UTC: ", created_at, ")");
        ProcessSignal(signal_id, action);
    }
    
    DatabaseFinalize(request);
}

//+------------------------------------------------------------------+
//| 统计未消费信号数量                                                 |
//+------------------------------------------------------------------+
int CountPendingSignals()
{
    string query = "SELECT COUNT(*) FROM signals WHERE consumed = 0";
    int request = DatabasePrepare(db_handle, query);
    if(request == INVALID_HANDLE)
        return 0;
    
    int count = 0;
    if(DatabaseRead(request))
    {
        long cnt;
        DatabaseColumnLong(request, 0, cnt);
        count = (int)cnt;
    }
    DatabaseFinalize(request);
    return count;
}

//+------------------------------------------------------------------+
//| 标记所有未消费信号为已消费                                          |
//+------------------------------------------------------------------+
void MarkAllSignalsConsumed()
{
    string update_query = "UPDATE signals SET consumed = 1 WHERE consumed = 0";
    if(DatabaseExecute(db_handle, update_query))
    {
        Print("✅ 所有堆积信号已标记为已消费（丢弃）");
    }
}

//+------------------------------------------------------------------+
//| 处理交易信号                                                      |
//+------------------------------------------------------------------+
void ProcessSignal(long signal_id, string action)
{
    bool result = false;
    double lots = 0;
    
    // 检查当前持仓状态
    int position_type = GetPositionType(); // 0=无持仓, 1=多仓, -1=空仓
    
    if(action == "BUY")
    {
        if(position_type == 1)
        {
            // 已有多仓，忽略买入信号
            Print("⚠️ 已有多仓，忽略买入信号");
            MarkSignalConsumed(signal_id);
            return;
        }
        else if(position_type == -1)
        {
            // 有空仓，先平仓
            Print("🔄 检测到买入信号，先平掉现有空仓");
            CloseAllPositions();
            MarkSignalConsumed(signal_id);
            return;
        }
        else
        {
            // 无持仓，开多仓
            lots = CalculateLotSize();
            if(lots <= 0)
            {
                Print("❌ 计算手数失败，余额不足");
                MarkSignalConsumed(signal_id);
                return;
            }
            
            double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
            
            // 开单前最终保证金校验
            double pre_margin = 0;
            if(OrderCalcMargin(ORDER_TYPE_BUY, _Symbol, lots, ask, pre_margin))
            {
                double avail = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
                if(pre_margin > avail * 0.95)
                {
                    Print("❌ 开单前保证金校验失败：需要 $", DoubleToString(pre_margin, 2),
                          " 可用 $", DoubleToString(avail, 2));
                    MarkSignalConsumed(signal_id);
                    return;
                }
            }
            
            result = trade.Buy(lots, _Symbol, ask, 0, 0, "QQQ Signal Buy");
        }
    }
    else if(action == "SELL")
    {
        if(position_type == -1)
        {
            // 已有空仓，忽略卖出信号
            Print("⚠️ 已有空仓，忽略卖出信号");
            MarkSignalConsumed(signal_id);
            return;
        }
        else if(position_type == 1)
        {
            // 有多仓，先平仓
            Print("🔄 检测到卖出信号，先平掉现有多仓");
            CloseAllPositions();
            MarkSignalConsumed(signal_id);
            return;
        }
        else
        {
            // 无持仓，开空仓
            lots = CalculateLotSize();
            if(lots <= 0)
            {
                Print("❌ 计算手数失败，余额不足");
                MarkSignalConsumed(signal_id);
                return;
            }
            
            double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
            
            // 开单前最终保证金校验
            double pre_margin_sell = 0;
            if(OrderCalcMargin(ORDER_TYPE_SELL, _Symbol, lots, bid, pre_margin_sell))
            {
                double avail_sell = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
                if(pre_margin_sell > avail_sell * 0.95)
                {
                    Print("❌ 开单前保证金校验失败：需要 $", DoubleToString(pre_margin_sell, 2),
                          " 可用 $", DoubleToString(avail_sell, 2));
                    MarkSignalConsumed(signal_id);
                    return;
                }
            }
            
            result = trade.Sell(lots, _Symbol, bid, 0, 0, "QQQ Signal Sell");
        }
    }
    else if(action == "CLOSE")
    {
        // 平仓所有持仓
        if(position_type != 0)
        {
            CloseAllPositions();
            result = true;
        }
        else
        {
            Print("⚠️ 无持仓，忽略平仓信号");
            result = true; // 标记为成功，避免重复处理
        }
    }
    
    if(result)
    {
        Print("✅ 执行成功");
        MarkSignalConsumed(signal_id);
    }
    else
    {
        uint retcode = trade.ResultRetcode();
        Print("❌ 执行失败，错误码: ", retcode, " 描述: ", trade.ResultRetcodeDescription());
        // 无论何种失败，都标记信号为已消费，避免无限重试
        // 若需重试，应由Python端重新写入新信号
        MarkSignalConsumed(signal_id);
    }
}

//+------------------------------------------------------------------+
//| 检查是否有持仓                                                    |
//+------------------------------------------------------------------+
bool HasPosition()
{
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(PositionSelectByTicket(ticket))
        {
            if(PositionGetInteger(POSITION_MAGIC) == MagicNumber)
                return true;
        }
    }
    return false;
}

//+------------------------------------------------------------------+
//| 获取当前持仓类型                                                  |
//| 返回: 0=无持仓, 1=多仓, -1=空仓                                  |
//+------------------------------------------------------------------+
int GetPositionType()
{
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(PositionSelectByTicket(ticket))
        {
            if(PositionGetInteger(POSITION_MAGIC) == MagicNumber)
            {
                ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
                if(type == POSITION_TYPE_BUY)
                    return 1;  // 多仓
                else if(type == POSITION_TYPE_SELL)
                    return -1; // 空仓
            }
        }
    }
    return 0; // 无持仓
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
            if(PositionGetInteger(POSITION_MAGIC) == MagicNumber)
            {
                trade.PositionClose(ticket);
            }
        }
    }
}

//+------------------------------------------------------------------+
//| 标记信号为已消费                                                  |
//+------------------------------------------------------------------+
void MarkSignalConsumed(long signal_id)
{
    string update_query = StringFormat("UPDATE signals SET consumed = 1 WHERE id = %d", signal_id);
    
    if(DatabaseExecute(db_handle, update_query))
    {
        Print("✅ 信号已标记为已消费");
    }
}

//+------------------------------------------------------------------+
//| 根据账户余额和杠杆计算手数                                         |
//+------------------------------------------------------------------+
double CalculateLotSize()
{
    // 获取账户信息
    double balance = AccountInfoDouble(ACCOUNT_BALANCE);
    double equity = AccountInfoDouble(ACCOUNT_EQUITY);
    double free_margin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
    double account_leverage = (double)AccountInfoInteger(ACCOUNT_LEVERAGE);
    
    // 使用余额作为基础
    double base_amount = balance;
    
    // 应用风险百分比
    double risk_amount = base_amount * (RiskPercent / 100.0);
    
    // 获取当前价格
    double price = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    if(price <= 0) return 0;
    
    // 获取合约规格
    double contract_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
    if(contract_size <= 0) contract_size = 1;
    
    // 获取点值
    double tick_value = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
    double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
    
    // 获取保证金计算相关信息
    double margin_initial = SymbolInfoDouble(_Symbol, SYMBOL_MARGIN_INITIAL);
    double margin_maintenance = SymbolInfoDouble(_Symbol, SYMBOL_MARGIN_MAINTENANCE);
    ENUM_SYMBOL_CALC_MODE calc_mode = (ENUM_SYMBOL_CALC_MODE)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_CALC_MODE);
    
    // 打印合约规格信息（用于调试）
    Print("=== 合约规格信息 ===");
    Print("📊 交易品种: ", _Symbol);
    Print("📊 合约大小: ", contract_size);
    Print("📊 最小变动价位: ", tick_size);
    Print("📊 最小变动价值: ", tick_value);
    Print("📊 当前价格: ", price);
    Print("📊 账户杠杆: ", account_leverage);
    Print("📊 初始保证金: ", margin_initial);
    Print("📊 维持保证金: ", margin_maintenance);
    Print("📊 保证金计算模式: ", EnumToString(calc_mode));
    Print("💰 账户余额: $", DoubleToString(balance, 2));
    Print("💰 账户净值: $", DoubleToString(equity, 2));
    Print("💰 可用保证金: $", DoubleToString(free_margin, 2));
    Print("💰 使用资金比例: ", RiskPercent, "%");
    Print("💰 计算使用资金: $", DoubleToString(risk_amount, 2));
    
    // 计算1手所需的保证金
    double margin_for_one_lot = 0;
    bool margin_calc_result = OrderCalcMargin(
        ORDER_TYPE_BUY,
        _Symbol,
        1.0,  // 1手
        price,
        margin_for_one_lot
    );
    
    if(margin_calc_result)
    {
        Print("📊 1手所需保证金: $", DoubleToString(margin_for_one_lot, 2));
    }
    else
    {
        Print("❌ 无法计算1手保证金");
    }
    
    // 方法1：根据可用保证金和杠杆计算最大可能手数
    double max_lots_by_margin = 0;
    if(margin_for_one_lot > 0)
    {
        max_lots_by_margin = (free_margin * 0.95) / margin_for_one_lot;  // 使用95%的可用保证金
        Print("📊 基于可用保证金的最大手数: ", DoubleToString(max_lots_by_margin, 2));
    }
    
    // 方法2：使用设定的杠杆倍数计算
    double total_trading_value = risk_amount * Leverage;
    double lots_by_leverage = total_trading_value / (price * contract_size);
    
    // 选择两种方法中较小的值（更保守）
    double lots = MathMin(lots_by_leverage, max_lots_by_margin);
    
    Print("📊 基于杠杆的手数: ", DoubleToString(lots_by_leverage, 2));
    Print("📊 选择手数（取较小值）: ", DoubleToString(lots, 2));
    
    // 调整到合法范围
    double min_lot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
    double max_lot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
    double lot_step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
    
    Print("📊 最小手数: ", min_lot);
    Print("📊 最大手数: ", max_lot);
    Print("📊 手数步长: ", lot_step);
    
    // 向下取整到步长
    lots = MathFloor(lots / lot_step) * lot_step;
    
    // 确保在允许范围内
    lots = MathMax(lots, min_lot);
    lots = MathMin(lots, max_lot);
    
    // 计算实际的保证金需求
    double actual_margin_required = 0;
    OrderCalcMargin(ORDER_TYPE_BUY, _Symbol, lots, price, actual_margin_required);
    
    // 计算实际持仓价值
    double position_value = lots * price * contract_size;
    
    // 计算实际杠杆
    double actual_leverage = position_value / actual_margin_required;
    
    Print("💰 账户余额: $", DoubleToString(balance, 2));
    Print("💰 可用保证金: $", DoubleToString(free_margin, 2));
    Print("💰 使用资金: $", DoubleToString(risk_amount, 2), " (", RiskPercent, "%)");
    Print("📊 1手价值: $", DoubleToString(price * contract_size, 2));
    Print("📊 设定杠杆倍数: ", Leverage, "倍");
    Print("💰 可交易总价值: $", DoubleToString(total_trading_value, 2));
    Print("📊 最终手数: ", DoubleToString(lots, 2));
    Print("💰 实际持仓价值: $", DoubleToString(position_value, 2));
    Print("💰 实际所需保证金: $", DoubleToString(actual_margin_required, 2));
    Print("📊 实际杠杆: ", DoubleToString(actual_leverage, 2), "倍");
    
    // 再次检查保证金是否充足（使用90%更保守）
    if(actual_margin_required > free_margin * 0.90)
    {
        Print("⚠️ 警告：所需保证金超过可用保证金90%！");
        Print("⚠️ 所需保证金: $", DoubleToString(actual_margin_required, 2));
        Print("⚠️ 可用保证金: $", DoubleToString(free_margin, 2));
        
        if(margin_for_one_lot <= 0)
        {
            Print("❌ 无法计算1手保证金，放弃开仓");
            return 0;
        }
        
        // 按可用保证金的90%重新计算手数
        lots = (free_margin * 0.90) / margin_for_one_lot;
        lots = MathFloor(lots / lot_step) * lot_step;
        
        Print("📊 调整后手数: ", DoubleToString(lots, 2));
        
        // 注意：调整后不再强制套用min_lot，避免超保证金
        if(lots < min_lot)
        {
            Print("❌ 调整后手数(", DoubleToString(lots, 2), ")小于最小手数(", DoubleToString(min_lot, 2), ")，余额不足无法开仓");
            return 0;
        }
    }
    
    // 最终安全校验：再做一次保证金确认
    double final_margin = 0;
    if(OrderCalcMargin(ORDER_TYPE_BUY, _Symbol, lots, price, final_margin))
    {
        if(final_margin > free_margin * 0.95)
        {
            Print("❌ 最终校验失败：需要保证金 $", DoubleToString(final_margin, 2),
                  " 超过可用保证金95% $", DoubleToString(free_margin * 0.95, 2));
            return 0;
        }
    }
    
    return lots;
}
