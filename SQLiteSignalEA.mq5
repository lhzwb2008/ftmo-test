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
input string   DBPath = "trading_signals.db";          // SQLite数据库文件名
input bool     UseCommonPath = true;                   // 使用通用目录（推荐）
input int      MagicNumber = 20241228;                 // 魔术数字
input double   Leverage = 5.0;                        // 杠杆倍数
input double   RiskPercent = 100.0;                    // 使用余额百分比(%)
input int      CheckIntervalSeconds = 1;               // 检查间隔（秒）

//--- 全局变量
CTrade trade;
datetime last_check_time = 0;
int db_handle = INVALID_HANDLE;

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
    datetime current_time = TimeCurrent();
    if(current_time - last_check_time < CheckIntervalSeconds)
        return;
        
    last_check_time = current_time;
    CheckDatabaseSignals();
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
    
    // 根据账户余额计算手数
    if(action == "BUY" || action == "SELL")
    {
        lots = CalculateLotSize();
        if(lots <= 0)
        {
            Print("❌ 计算手数失败，余额不足");
            MarkSignalConsumed(signal_id);
            return;
        }
    }
    
    if(action == "BUY")
    {
        // 检查是否已有持仓
        if(HasPosition())
        {
            Print("⚠️ 已有持仓，忽略买入信号");
            MarkSignalConsumed(signal_id);
            return;
        }
        
        double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
        result = trade.Buy(lots, _Symbol, ask, 0, 0, "QQQ Signal Buy");
    }
    else if(action == "SELL")
    {
        // 检查是否已有持仓
        if(HasPosition())
        {
            Print("⚠️ 已有持仓，忽略卖空信号");
            MarkSignalConsumed(signal_id);
            return;
        }
        
        double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
        result = trade.Sell(lots, _Symbol, bid, 0, 0, "QQQ Signal Sell");
    }
    else if(action == "CLOSE")
    {
        // 平仓所有持仓
        CloseAllPositions();
        result = true;
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
    
    // 使用可用保证金和余额中的较小值
    double available_funds = MathMin(free_margin, equity);
    
    // 应用风险百分比
    double risk_amount = available_funds * (RiskPercent / 100.0);
    
    // 应用杠杆
    double leveraged_amount = risk_amount * Leverage;
    
    // 获取当前价格
    double price = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    if(price <= 0) return 0;
    
    // 获取合约规格
    double contract_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
    if(contract_size <= 0) contract_size = 1;
    
    // 计算手数
    double lots = leveraged_amount / (price * contract_size);
    
    // 调整到合法范围
    double min_lot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
    double max_lot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
    double lot_step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
    
    // 向下取整到步长
    lots = MathFloor(lots / lot_step) * lot_step;
    
    // 确保在允许范围内
    lots = MathMax(lots, min_lot);
    lots = MathMin(lots, max_lot);
    
    Print("💰 账户余额: $", DoubleToString(balance, 2));
    Print("💰 可用保证金: $", DoubleToString(free_margin, 2));
    Print("💰 使用资金: $", DoubleToString(risk_amount, 2), " (", RiskPercent, "%)");
    Print("💰 杠杆后资金: $", DoubleToString(leveraged_amount, 2), " (", Leverage, "倍)");
    Print("📊 计算手数: ", DoubleToString(lots, 2));
    
    return lots;
} 