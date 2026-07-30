//+------------------------------------------------------------------+
//|                                            SQLiteSignalEA.mq5     |
//|                                     简化版SQLite信号执行EA         |
//+------------------------------------------------------------------+
#property copyright "Copyright 2024"
#property link      ""
#property version   "1.01"
#property strict

// 添加必要的权限声明
#property script_show_inputs

#include <Trade\Trade.mqh>
#include <Trade\AccountInfo.mqh>

//--- 输入参数
input string   DBPath = "trading_signals_fundednext.db";          // SQLite数据库文件名
input bool     UseCommonPath = true;                   // 使用通用目录（推荐）
input int      MagicNumber = 20241228;                 // 魔术数字
input double   Leverage = 2;                        // 杠杆倍数
input double   RiskPercent = 100.0;                    // 使用余额百分比(%)
input int      CheckIntervalSeconds = 1;               // 检查间隔（秒）
input double   InitialBalance = 6000.0;                // 账户初始资金（FundedNext风控按初始资金计算）
input double   HardSLRiskPercent = 2.5;                // 单笔硬止损(%)（Funded 账户必填，官方上限3%留0.5%；Challenge 请设 0 禁用）
input bool     DryRun = true;                         // 演练模式：只打日志不下单（上线前务必先测）

//--- 日内亏损风控参数（EA端基于真实权益逐tick监控，Python端不再负责日内止损；限额基于 InitialBalance）
input double   DailyLossPercent = 5.0;                 // 官方日内最大亏损比例(%)（FundedNext 官方 5；0=禁用）
input double   DailyLossBufferPercent = 5.0;           // 触发缓冲(%，占日亏限额比例)（覆盖滑点/跳空，重大数据日建议调大）
input int      DailyResetServerHour = 0;               // 日亏锚点重置时间（服务器小时，0=服务器午夜，需与考试商日重置一致）

//--- 全局变量
CTrade trade;
datetime last_check_time = 0;
int db_handle = INVALID_HANDLE;

//--- 日内亏损风控状态（通过 GlobalVariables 持久化，EA 重启不丢失）
double   daily_anchor = 0.0;          // 日初锚点权益 = max(balance, equity)，比各家官方锚点定义都保守
bool     daily_halted = false;        // 当日是否已触发日内止损（触发后当日禁止新开仓）
datetime daily_period_start = 0;      // 当前风控日起点（服务器时间）
datetime last_status_write = 0;       // 上次写 ea_daily_status 状态表的时间

//--- 持仓同步（硬止损/服务器SL打掉后写回 DB，供 simulate 镜像平仓）
ulong    last_tracked_ticket = 0;     // 上一tick跟踪的持仓票号
int      ea_close_seq = 0;            // 外部平仓序号（递增），simulate 据此发现硬止损平仓

//--- 前向声明（MQL5 要求先声明后使用）
string DailyGVName(string suffix);
double DailyLossLimitUSD();
double DailyTriggerFloor();
void   WriteDailyStatus(string reason);
void   WritePositionStatus(int side, double volume, double open_price, double sl, string reason);
void   UpdateProtectiveSL();
void   SyncPositionState();
double CalcProtectiveSL(double lots, double open_price, ENUM_POSITION_TYPE type);

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
    
    Print("✅ EA初始化成功");
    Print("💰 杠杆: ", Leverage, "倍");
    Print("📊 使用余额: ", RiskPercent, "%");
    if(DryRun)
        Print("🧪 演练模式已开启：将计算手数/止损并写日志，但不会向服务器发送任何订单");
    
    // 初始化日内亏损风控（恢复或重置当日锚点/halted 状态）
    if(DailyLossPercent > 0)
    {
        DailyRiskLoadOrReset();
        Print("🛡️ 日内止损(EA端): 限额=$", DoubleToString(DailyLossLimitUSD(), 2),
              " (初始资金 $", DoubleToString(InitialBalance, 2), " × ", DailyLossPercent, "%)",
              " 缓冲=", DailyLossBufferPercent, "% 触发线权益=$", DoubleToString(DailyTriggerFloor(), 2));
    }
    else
        Print("🛡️ 日内止损(EA端): 已禁用");
    
    if(HardSLRiskPercent > 0)
        Print("🛡️ 单笔硬止损: 初始资金 $", DoubleToString(InitialBalance, 2), " × ", HardSLRiskPercent,
              "% = $", DoubleToString(InitialBalance * HardSLRiskPercent / 100.0, 2),
              " （与日亏线SL取更紧者；Challenge 请将 HardSLRiskPercent 设为 0）");
    else
        Print("🛡️ 单笔硬止损: 已禁用（Challenge 模式）");
    
    // 恢复外部平仓序号，避免 EA 重启后 simulate 漏同步
    if(GlobalVariableCheck(DailyGVName("close_seq")))
        ea_close_seq = (int)GlobalVariableGet(DailyGVName("close_seq"));
    
    // 启动时立即检查已有持仓保护性SL（硬止损∩日亏线取更紧）
    EnsureStopLosses();
    SyncPositionState();
    
    return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    if(db_handle != INVALID_HANDLE)
    {
        DatabaseClose(db_handle);
        db_handle = INVALID_HANDLE;
    }
    
    Print("EA已停止");
}

//+------------------------------------------------------------------+
//| Expert tick function                                              |
//+------------------------------------------------------------------+
void OnTick()
{
    // 日内亏损检查逐tick执行（不受节流影响）：真实权益对比触发线，触线立即全平
    DailyRiskCheck();
    // 检测硬止损/服务器SL等外部平仓，并写回 ea_position 供 simulate 镜像
    SyncPositionState();
    
    datetime current_time = TimeCurrent();
    if(current_time - last_check_time < CheckIntervalSeconds)
        return;
        
    last_check_time = current_time;
    // 统一维护保护性SL：min(单笔硬止损, 日亏触发线SL)，取更紧者
    UpdateProtectiveSL();
    CheckDatabaseSignals();
}

//+------------------------------------------------------------------+
//| 计算硬止损距离（价格单位）                                         |
//| Funded 账户：每笔单必须挂SL，且累计风险≤初始资金3%                   |
//| Challenge：将 HardSLRiskPercent 设为 0 即可禁用                     |
//| 风险金额 = 初始资金 × HardSLRiskPercent%，换算成价格距离            |
//+------------------------------------------------------------------+
double CalcHardSLDistance(double lots)
{
    if(lots <= 0 || HardSLRiskPercent <= 0) return 0;
    
    double tick_value = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
    double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
    if(tick_value <= 0 || tick_size <= 0) return 0;
    
    // 每1.0手每1个价格单位的亏损金额
    double loss_per_unit = tick_value / tick_size;
    double risk_amount = InitialBalance * (HardSLRiskPercent / 100.0);
    
    double distance = risk_amount / (loss_per_unit * lots);
    
    // 不能小于经纪商允许的最小止损距离
    double min_stop = (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * SymbolInfoDouble(_Symbol, SYMBOL_POINT);
    if(distance < min_stop) distance = min_stop;
    
    return distance;
}

//+------------------------------------------------------------------+
//| 单笔硬止损价格（0=未启用）                                         |
//+------------------------------------------------------------------+
double CalcHardSLPrice(double lots, double open_price, ENUM_POSITION_TYPE type)
{
    double distance = CalcHardSLDistance(lots);
    if(distance <= 0) return 0;
    int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
    double sl = (type == POSITION_TYPE_BUY) ? open_price - distance : open_price + distance;
    return NormalizeDouble(sl, digits);
}

//+------------------------------------------------------------------+
//| 日亏触发线对应的保护性SL价格（0=未启用/无法计算）                   |
//+------------------------------------------------------------------+
double CalcDailyFloorSLPrice(double lots, double open_price, ENUM_POSITION_TYPE type)
{
    if(DailyLossPercent <= 0 || daily_halted || lots <= 0 || daily_anchor <= 0)
        return 0;
    
    double tick_value = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
    double tick_size  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
    if(tick_value <= 0 || tick_size <= 0) return 0;
    
    double loss_per_unit = tick_value / tick_size;
    double balance = AccountInfoDouble(ACCOUNT_BALANCE);
    double floor_eq = DailyTriggerFloor();
    double allowed_floating = floor_eq - balance;
    double offset = allowed_floating / (loss_per_unit * lots);
    
    int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
    double sl = (type == POSITION_TYPE_BUY) ? open_price + offset : open_price - offset;
    return NormalizeDouble(sl, digits);
}

//+------------------------------------------------------------------+
//| 取更紧的SL：多单取较高价，空单取较低价；0 表示该侧未启用            |
//+------------------------------------------------------------------+
double TighterSL(ENUM_POSITION_TYPE type, double sl_a, double sl_b)
{
    if(sl_a <= 0) return sl_b;
    if(sl_b <= 0) return sl_a;
    if(type == POSITION_TYPE_BUY)
        return MathMax(sl_a, sl_b);
    return MathMin(sl_a, sl_b);
}

//+------------------------------------------------------------------+
//| 计算最终保护性SL = min(硬止损, 日亏线SL) 的更紧者                   |
//+------------------------------------------------------------------+
double CalcProtectiveSL(double lots, double open_price, ENUM_POSITION_TYPE type)
{
    double hard_sl  = CalcHardSLPrice(lots, open_price, type);
    double daily_sl = CalcDailyFloorSLPrice(lots, open_price, type);
    return TighterSL(type, hard_sl, daily_sl);
}

//+------------------------------------------------------------------+
//| 统一维护持仓保护性SL：显式取 min(硬止损, 日亏触发线)，只收紧不放松  |
//+------------------------------------------------------------------+
void UpdateProtectiveSL()
{
    if(DryRun)
        return;
    
    int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
    double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
    double min_stop = (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * SymbolInfoDouble(_Symbol, SYMBOL_POINT);
    if(tick_size <= 0) tick_size = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
    
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(!PositionSelectByTicket(ticket))
            continue;
        if(PositionGetInteger(POSITION_MAGIC) != MagicNumber)
            continue;
        
        double lots = PositionGetDouble(POSITION_VOLUME);
        if(lots <= 0) continue;
        double open_price = PositionGetDouble(POSITION_PRICE_OPEN);
        double cur_sl = PositionGetDouble(POSITION_SL);
        double tp = PositionGetDouble(POSITION_TP);
        ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
        
        double hard_sl  = CalcHardSLPrice(lots, open_price, type);
        double daily_sl = CalcDailyFloorSLPrice(lots, open_price, type);
        double target_sl = TighterSL(type, hard_sl, daily_sl);
        if(target_sl <= 0)
            continue;
        
        // SL 距现价太近时跳过（权益触线检查仍会兜底），避免服务器拒绝
        double market = (type == POSITION_TYPE_BUY) ? SymbolInfoDouble(_Symbol, SYMBOL_BID)
                                                    : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
        if(type == POSITION_TYPE_BUY && target_sl > market - min_stop)
            continue;
        if(type == POSITION_TYPE_SELL && target_sl < market + min_stop)
            continue;
        
        // 只收紧不放松
        bool need_update = false;
        if(cur_sl <= 0)
            need_update = true;
        else if(type == POSITION_TYPE_BUY && target_sl > cur_sl + tick_size / 2)
            need_update = true;
        else if(type == POSITION_TYPE_SELL && target_sl < cur_sl - tick_size / 2)
            need_update = true;
        if(!need_update)
            continue;
        
        string src = "";
        if(hard_sl > 0 && daily_sl > 0)
            src = (MathAbs(target_sl - hard_sl) <= tick_size) ? "硬止损" : "日亏线";
        else if(hard_sl > 0)
            src = "硬止损";
        else
            src = "日亏线";
        
        if(trade.PositionModify(ticket, target_sl, tp))
            Print("🛡️ 已更新保护性SL(", src, "取更紧): ", DoubleToString(target_sl, digits),
                  " 硬止损=", DoubleToString(hard_sl, digits),
                  " 日亏线SL=", DoubleToString(daily_sl, digits));
        else
            Print("❌ 更新保护性SL失败，持仓 ", ticket, " 错误: ", trade.ResultRetcode());
    }
}

//+------------------------------------------------------------------+
//| 兼容旧调用名：统一走 UpdateProtectiveSL                            |
//+------------------------------------------------------------------+
void EnsureStopLosses()
{
    UpdateProtectiveSL();
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
    
    // 查询所有未消费的信号，按时间倒序排列，获取最新的一条
    string query = "SELECT id, action FROM signals WHERE consumed = 0 ORDER BY created_at DESC LIMIT 1";
    
    int request = DatabasePrepare(db_handle, query);
    if(request == INVALID_HANDLE)
    {
        Print("❌ 查询失败");
        return;
    }
    
    // 读取查询结果
    if(DatabaseRead(request))
    {
        long latest_signal_id;
        string latest_action;
        
        DatabaseColumnLong(request, 0, latest_signal_id);
        DatabaseColumnText(request, 1, latest_action);
        
        Print("📊 检测到未消费信号，只执行最新的: ", latest_action, " (ID: ", latest_signal_id, ")");
        
        // 先标记所有未消费的信号为已消费（除了最新的这一条）
        MarkOldSignalsConsumed(latest_signal_id);
        
        // 处理最新的信号
        ProcessSignal(latest_signal_id, latest_action);
    }
    
    DatabaseFinalize(request);
}

//+------------------------------------------------------------------+
//| 处理交易信号                                                      |
//+------------------------------------------------------------------+
void ProcessSignal(long signal_id, string action)
{
    bool result = false;
    double lots = 0;
    
    // 日内止损已触发：当日禁止一切新开仓，只消费掉开仓信号
    if(daily_halted && (action == "BUY" || action == "SELL"))
    {
        Print("🚫 日内止损已触发，今日不再开仓，忽略信号: ", action);
        MarkSignalConsumed(signal_id);
        return;
    }
    
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
            Print(DryRun ? "🧪 [演练] 检测到买入信号，将平掉现有空仓" : "🔄 检测到买入信号，先平掉现有空仓");
            if(!DryRun) CloseAllPositions();
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
            // 开仓即挂保护性SL = min(硬止损, 日亏线)，取更紧者
            double sl = CalcProtectiveSL(lots, ask, POSITION_TYPE_BUY);
            if(DryRun)
            {
                Print("🧪 [演练] BUY 手数=", DoubleToString(lots, 2), " 价格=", DoubleToString(ask, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS)),
                      " SL=", DoubleToString(sl, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS)),
                      " (硬止损风险 ", HardSLRiskPercent, "% / 日亏保护取更紧)");
                result = true;
            }
            else
            {
                result = trade.Buy(lots, _Symbol, ask, sl, 0, "QQQ Signal Buy");
            }
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
            Print(DryRun ? "🧪 [演练] 检测到卖出信号，将平掉现有多仓" : "🔄 检测到卖出信号，先平掉现有多仓");
            if(!DryRun) CloseAllPositions();
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
            // 开仓即挂保护性SL = min(硬止损, 日亏线)，取更紧者
            double sl = CalcProtectiveSL(lots, bid, POSITION_TYPE_SELL);
            if(DryRun)
            {
                Print("🧪 [演练] SELL 手数=", DoubleToString(lots, 2), " 价格=", DoubleToString(bid, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS)),
                      " SL=", DoubleToString(sl, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS)),
                      " (硬止损风险 ", HardSLRiskPercent, "% / 日亏保护取更紧)");
                result = true;
            }
            else
            {
                result = trade.Sell(lots, _Symbol, bid, sl, 0, "QQQ Signal Sell");
            }
        }
    }
    else if(action == "CLOSE")
    {
        // 平仓所有持仓
        if(position_type != 0)
        {
            if(DryRun)
            {
                Print("🧪 [演练] CLOSE 将平掉所有持仓");
                result = true;
            }
            else
            {
                CloseAllPositions();
                result = true;
            }
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
        Print("❌ 执行失败: ", trade.ResultRetcode());
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
//| 标记旧信号为已消费（除了指定的最新信号）                            |
//+------------------------------------------------------------------+
void MarkOldSignalsConsumed(long latest_signal_id)
{
    string update_query = StringFormat("UPDATE signals SET consumed = 1 WHERE consumed = 0 AND id != %d", latest_signal_id);
    
    if(DatabaseExecute(db_handle, update_query))
    {
        Print("✅ 旧信号已全部标记为已消费");
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
    
    // 再次检查保证金是否充足
    if(actual_margin_required > free_margin)
    {
        Print("⚠️ 警告：所需保证金超过可用保证金！");
        Print("⚠️ 所需保证金: $", DoubleToString(actual_margin_required, 2));
        Print("⚠️ 可用保证金: $", DoubleToString(free_margin, 2));
        
        // 调整手数以适应可用保证金
        lots = (free_margin * 0.95) / margin_for_one_lot;
        lots = MathFloor(lots / lot_step) * lot_step;
        lots = MathMax(lots, min_lot);
        
        Print("📊 调整后手数: ", DoubleToString(lots, 2));
    }
    
    return lots;
}

//+------------------------------------------------------------------+
//| ============ 日内亏损风控模块（EA端权威执行） ============         |
//| 1) 逐tick用真实权益对比触发线，触线立即市价全平并停止当日交易       |
//| 2) 给持仓维护一张 broker 服务器端保护性 SL 单（价格 = 权益恰好      |
//|    触及触发线的换算价）。SL 挂在服务器上，即使 EA/终端/VPS 全部     |
//|    失联，broker 服务器也会自动执行                                 |
//| 3) 状态写回 ea_daily_status 表，Python 模拟端镜像记账              |
//| 注意：本文件限额基于 InitialBalance；DryRun 模式只打日志不下单      |
//+------------------------------------------------------------------+

string DailyGVName(string suffix)
{
    return "DLS_" + IntegerToString(MagicNumber) + "_" + suffix;
}

double DailyLossLimitUSD()
{
    return InitialBalance * DailyLossPercent / 100.0;
}

double DailyTriggerFloor()
{
    return daily_anchor - DailyLossLimitUSD() * (1.0 - DailyLossBufferPercent / 100.0);
}

//+------------------------------------------------------------------+
//| 当前风控日起点（服务器时间，按 DailyResetServerHour 切日）          |
//+------------------------------------------------------------------+
datetime CurrentDailyPeriodStart()
{
    datetime now = TimeCurrent();
    MqlDateTime dt;
    TimeToStruct(now, dt);
    dt.hour = DailyResetServerHour;
    dt.min = 0;
    dt.sec = 0;
    datetime start = StructToTime(dt);
    if(now < start)
        start -= 86400;
    return start;
}

//+------------------------------------------------------------------+
//| EA 启动时恢复当日风控状态（GlobalVariables 持久化，重启不丢失）     |
//+------------------------------------------------------------------+
void DailyRiskLoadOrReset()
{
    datetime period = CurrentDailyPeriodStart();
    if(GlobalVariableCheck(DailyGVName("day")) && (datetime)GlobalVariableGet(DailyGVName("day")) == period)
    {
        daily_anchor = GlobalVariableGet(DailyGVName("anchor"));
        daily_halted = GlobalVariableGet(DailyGVName("halted")) > 0.5;
        daily_period_start = period;
        Print("♻️ 已恢复当日风控状态: 锚点=$", DoubleToString(daily_anchor, 2),
              " 当日已停止=", daily_halted ? "是" : "否");
        WriteDailyStatus(daily_halted ? "halted" : "restored");
        return;
    }
    DailyRiskNewDay(period);
}

//+------------------------------------------------------------------+
//| 新风控日：重置锚点与 halted 标志                                   |
//+------------------------------------------------------------------+
void DailyRiskNewDay(datetime period)
{
    daily_period_start = period;
    double balance = AccountInfoDouble(ACCOUNT_BALANCE);
    double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
    daily_anchor = MathMax(balance, equity);   // 取较大者，比各家官方锚点定义都保守
    daily_halted = false;
    GlobalVariableSet(DailyGVName("day"), (double)period);
    GlobalVariableSet(DailyGVName("anchor"), daily_anchor);
    GlobalVariableSet(DailyGVName("halted"), 0.0);
    Print("🌅 新风控日: 锚点=$", DoubleToString(daily_anchor, 2),
          " 日亏限额=$", DoubleToString(DailyLossLimitUSD(), 2),
          " 触发线权益=$", DoubleToString(DailyTriggerFloor(), 2));
    WriteDailyStatus("new_day");
}

//+------------------------------------------------------------------+
//| 每tick调用：切日检测 + 权益触线检查 + 维护保护性SL + 定期写状态     |
//+------------------------------------------------------------------+
void DailyRiskCheck()
{
    if(DailyLossPercent <= 0)
        return;
    
    datetime period = CurrentDailyPeriodStart();
    if(period != daily_period_start)
        DailyRiskNewDay(period);
    
    double equity = AccountInfoDouble(ACCOUNT_EQUITY);
    double floor_eq = DailyTriggerFloor();
    
    if(!daily_halted && equity <= floor_eq)
    {
        Print("🛑 !!!!! 权益触及日内亏损触发线，立即全平并停止当日交易 !!!!!");
        Print("🛑 当前权益: $", DoubleToString(equity, 2),
              " 触发线: $", DoubleToString(floor_eq, 2),
              " 锚点: $", DoubleToString(daily_anchor, 2),
              " 官方限额: $", DoubleToString(DailyLossLimitUSD(), 2));
        if(DryRun)
            Print("🧪 [演练] 将全平所有持仓并停止当日交易（演练模式不实际下单）");
        else
            CloseAllPositions();
        daily_halted = true;
        GlobalVariableSet(DailyGVName("halted"), 1.0);
        WriteDailyStatus("daily_loss_halt");
        return;
    }
    
    if(!daily_halted)
        UpdateProtectiveSL();
    
    // 定期写心跳状态，Python 端据此镜像 halted 标志同步记账
    if(TimeCurrent() - last_status_write >= 30)
        WriteDailyStatus(daily_halted ? "halted" : "ok");
}

//+------------------------------------------------------------------+
//| 检测持仓消失（硬止损/日亏SL/手动平仓），递增 close_seq 写回 DB      |
//+------------------------------------------------------------------+
void SyncPositionState()
{
    ulong ticket = 0;
    int side = 0;
    double volume = 0, open_price = 0, sl = 0;
    
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong t = PositionGetTicket(i);
        if(!PositionSelectByTicket(t))
            continue;
        if(PositionGetInteger(POSITION_MAGIC) != MagicNumber)
            continue;
        ticket = t;
        ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
        side = (type == POSITION_TYPE_BUY) ? 1 : -1;
        volume = PositionGetDouble(POSITION_VOLUME);
        open_price = PositionGetDouble(POSITION_PRICE_OPEN);
        sl = PositionGetDouble(POSITION_SL);
        break;
    }
    
    // 上一tick还有仓、这一tick没了 → 服务器端SL/手动/日亏市价平仓等外部平仓
    if(last_tracked_ticket != 0 && ticket == 0)
    {
        ea_close_seq++;
        GlobalVariableSet(DailyGVName("close_seq"), (double)ea_close_seq);
        Print("📣 检测到持仓被外部平掉(硬止损/日亏SL/手动等)，close_seq=", ea_close_seq,
              " 原票号=", last_tracked_ticket, " —— 已写回 ea_position 供 simulate 镜像");
        WritePositionStatus(0, 0, 0, 0, "external_close");
    }
    else if(ticket != 0)
    {
        // 持仓中定期刷新状态（节流：与 last_status_write 共用 30s，或票号变化立即写）
        static ulong last_written_ticket = 0;
        if(ticket != last_written_ticket || TimeCurrent() - last_status_write >= 30)
        {
            WritePositionStatus(side, volume, open_price, sl, "open");
            last_written_ticket = ticket;
        }
    }
    else if(last_tracked_ticket == 0 && ticket == 0)
    {
        // 空仓心跳（低频）
        static datetime last_flat_write = 0;
        if(TimeCurrent() - last_flat_write >= 60)
        {
            WritePositionStatus(0, 0, 0, 0, "flat");
            last_flat_write = TimeCurrent();
        }
    }
    
    last_tracked_ticket = ticket;
}

//+------------------------------------------------------------------+
//| 写持仓同步表（simulate 用 close_seq 发现硬止损等外部平仓）           |
//+------------------------------------------------------------------+
void WritePositionStatus(int side, double volume, double open_price, double sl, string reason)
{
    if(db_handle == INVALID_HANDLE)
        return;
    
    string create_sql = "CREATE TABLE IF NOT EXISTS ea_position ("
                        "id INTEGER PRIMARY KEY CHECK (id = 1), "
                        "side INTEGER, volume REAL, open_price REAL, sl REAL, "
                        "close_seq INTEGER, reason TEXT, updated_at TEXT)";
    DatabaseExecute(db_handle, create_sql);
    
    string sql = StringFormat(
        "INSERT INTO ea_position (id, side, volume, open_price, sl, close_seq, reason, updated_at) "
        "VALUES (1, %d, %.4f, %.5f, %.5f, %d, '%s', '%s') "
        "ON CONFLICT(id) DO UPDATE SET side=excluded.side, volume=excluded.volume, "
        "open_price=excluded.open_price, sl=excluded.sl, close_seq=excluded.close_seq, "
        "reason=excluded.reason, updated_at=excluded.updated_at",
        side, volume, open_price, sl, ea_close_seq, reason,
        TimeToString(TimeGMT(), TIME_DATE | TIME_SECONDS));
    
    if(!DatabaseExecute(db_handle, sql))
        Print("⚠️ 写入 ea_position 失败: ", GetLastError());
}

//+------------------------------------------------------------------+
//| （已合并进 UpdateProtectiveSL，保留空壳避免旧引用编译失败）         |
//+------------------------------------------------------------------+
void UpdateDailyFloorSL(double floor_eq)
{
    UpdateProtectiveSL();
}

//+------------------------------------------------------------------+
//| 把当日风控状态写回信号数据库，Python 端读取后镜像记账               |
//+------------------------------------------------------------------+
void WriteDailyStatus(string reason)
{
    if(db_handle == INVALID_HANDLE)
        return;
    last_status_write = TimeCurrent();
    
    string create_sql = "CREATE TABLE IF NOT EXISTS ea_daily_status ("
                        "id INTEGER PRIMARY KEY CHECK (id = 1), "
                        "server_day TEXT, halted INTEGER, reason TEXT, "
                        "anchor REAL, equity REAL, loss_floor REAL, updated_at TEXT)";
    DatabaseExecute(db_handle, create_sql);
    
    string sql = StringFormat(
        "INSERT INTO ea_daily_status (id, server_day, halted, reason, anchor, equity, loss_floor, updated_at) "
        "VALUES (1, '%s', %d, '%s', %.2f, %.2f, %.2f, '%s') "
        "ON CONFLICT(id) DO UPDATE SET server_day=excluded.server_day, halted=excluded.halted, "
        "reason=excluded.reason, anchor=excluded.anchor, equity=excluded.equity, "
        "loss_floor=excluded.loss_floor, updated_at=excluded.updated_at",
        TimeToString(daily_period_start, TIME_DATE), daily_halted ? 1 : 0, reason,
        daily_anchor, AccountInfoDouble(ACCOUNT_EQUITY), DailyTriggerFloor(),
        TimeToString(TimeGMT(), TIME_DATE | TIME_SECONDS));
    
    if(!DatabaseExecute(db_handle, sql))
        Print("⚠️ 写入 ea_daily_status 失败: ", GetLastError());
}