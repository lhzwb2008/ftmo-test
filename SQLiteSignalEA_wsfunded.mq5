//+------------------------------------------------------------------+
//|                                            SQLiteSignalEA.mq5     |
//|                                     简化版SQLite信号执行EA         |
//+------------------------------------------------------------------+
#property copyright "Copyright 2024"
#property link      ""
#property version   "1.04"
#define   EA_BUILD   "2026-11-10f Ultra单笔止损+免配置+考核止盈挂官方线、无逐tick兜底"  // 每次改 EA 逻辑时同步更新日期和说明
// 杠杆、起始资金、绑定账户、考核通过线全部由 simulate 写入 sim_config；EA inputs 保持默认即可，无需修改
// Ultra：单个 trading idea 的止损风险 ≤ 当日回撤额度的 50%。下单时必须带上，禁止只挂日亏宽止损。
#define   IDEA_RISK_FRACTION  0.50
#property strict

// 考核止盈挂单在官方线基础上多留的滑点（最小价位个数）
#define   PASS_TP_SLIP_TICKS  5

// 添加必要的权限声明
#property script_show_inputs

#include <Trade\Trade.mqh>
#include <Trade\AccountInfo.mqh>

//--- 输入参数
input string   DBPath = "trading_signals_wsfunded.db"; // SQLite数据库文件名（只接一张账户）
input bool     UseCommonPath = true;                   // 使用通用目录（推荐）
input int      MagicNumber = 20261110;                 // 魔术数字（与其他券商 EA 区分）
input double   RiskPercent = 100.0;                    // 使用余额百分比(%)
input int      CheckIntervalSeconds = 1;               // 检查间隔（秒）

//--- 日内亏损风控参数（EA端基于真实权益逐tick监控，Python端不再负责日内止损；限额基于账户初始资金，见 EffInitialBalance）
input double   DailyLossPercent = 5.0;                 // 官方日内最大亏损比例(%)（Ultra 官方 5；必须大于 0）
input double   DailyLossBufferPercent = 5.0;           // 触发缓冲(%，占日亏限额比例)（覆盖滑点/跳空，重大数据日建议调大）
input int      DailyResetServerHour = 0;               // 日亏锚点重置时间（服务器小时，0=服务器午夜，需与考试商日重置一致）
input bool     ForceResetDailyRisk = false;            // 强制重置日内锚点（一般不用：换账户、出入金已自动重算）

//--- 全局变量
CTrade trade;
datetime last_check_time = 0;
int db_handle = INVALID_HANDLE;
double g_sim_initial_balance = 0;    // simulate 写入的账户起始资金（0=未读到，不开仓）
double g_sim_leverage = 0;           // simulate 写入的杠杆（0=未读到，不开仓）
long   g_sim_login = 0;              // simulate 绑定的 MT5 登录号（与本终端不一致则不开仓）
double g_initial_deposit = 0;        // MT5 历史里第一笔入金，供 simulate 作为起始资金默认值
double g_challenge_pass_equity = 0;  // simulate 写入的官方考核通过线（0=Funded/关闭）
bool   pass_halted = false;          // 考核已通过则停止开仓，直到通过线写成 0 或 simulate 换新会话
bool   pass_status_pending = false;  // ea_pass_status 写入失败，待补写
double signal_size_mult = 1.0;  // 当前信号的开仓手数系数（signals.size_mult，开盘冷清日 0.5）

//--- 日内亏损风控状态（通过 GlobalVariables 持久化，EA 重启不丢失）
double   daily_anchor = 0.0;          // 日初锚点权益 = max(balance, equity)，比各家官方锚点定义都保守
bool     daily_halted = false;        // 当日是否已触发日内止损（触发后当日禁止新开仓）
datetime daily_period_start = 0;      // 当前风控日起点（服务器时间）
datetime last_status_write = 0;       // 上次写 ea_daily_status 状态表的时间

//--- 持仓同步（硬止损/服务器SL打掉后写回 DB，供 simulate 镜像平仓）
ulong    last_tracked_ticket = 0;     // 上一tick跟踪的持仓票号
int      ea_close_seq = 0;            // 外部平仓序号（递增），simulate 据此发现硬止损平仓
bool     internal_close_pending = false; // EA 自己执行了平仓（CLOSE信号/反手/日亏全平），下次持仓消失不算外部平仓

//--- 前向声明（MQL5 要求先声明后使用）
string DailyGVName(string suffix);
double DailyLossLimitUSD();
double DailyTriggerFloor();
double IdeaRiskUSD();
void   WriteDailyStatus(string reason);
void   WritePositionStatus(int side, double volume, double open_price, double sl, string reason);
void   UpdateProtectiveSL();
void   SyncPositionState();
double CalcProtectiveSL(double lots, double open_price, ENUM_POSITION_TYPE type);
bool   SimConfigReady(string &why);
void   LoadSimConfig(bool force);
void   WriteAccountHeartbeat();
void   CheckBalanceOperations();
void   UpdatePassTP();
double CalcPassTPPrice(ENUM_POSITION_TYPE type, double lots, double open_price, double pos_float, double close_cost);
double EstimateCloseCost(long position_id);

//+------------------------------------------------------------------+
//| Expert initialization function                                    |
//+------------------------------------------------------------------+
int OnInit()
{
    Print("🏷️ EA 版本 ", EA_BUILD, " | 文件 ", __FILE__,
          " | 编译于 ", TimeToString(__DATETIME__, TIME_DATE | TIME_MINUTES));
    // 设置魔术数字
    trade.SetExpertMagicNumber(MagicNumber);
    
    // 打开SQLite数据库
    if(!OpenDatabase())
    {
        Print("❌ 无法打开SQLite数据库");
        return(INIT_FAILED);
    }
    Print("👤 MT5 账户: ", AccountInfoInteger(ACCOUNT_LOGIN), " @ ", AccountInfoString(ACCOUNT_SERVER),
          " 余额=$", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2));
    g_initial_deposit = DetectInitialDeposit();
    if(g_initial_deposit > 0)
        Print("👤 MT5 历史第一笔入金: $", DoubleToString(g_initial_deposit, 2));

    LoadSimConfig(true);
    LoadChallengePassTarget(true);
    string why = "";
    if(SimConfigReady(why))
        Print("🔗 已读取 simulate 配置: 起始资金 $", DoubleToString(g_sim_initial_balance, 2),
              " | 杠杆 ", g_sim_leverage, "x");
    else
        Print("⏳ 等待 simulate 下发配置（", why, "）。在此之前只写心跳、不开仓，启动 simulate 后自动生效");
    if(g_challenge_pass_equity > 0)
        Print("🎯 考核通过线: $", DoubleToString(g_challenge_pass_equity, 2),
              "（开仓即挂券商止盈，空仓且余额过线即停止开仓）。当前已停止=", pass_halted ? "是" : "否");
    else
        Print("🎯 考核通过检测: 已关闭（Funded 或未写入通过线）");
    
    Print("✅ EA初始化成功");
    Print("🧩 编译版本: ", EA_BUILD, "（开仓必须带单笔止损；Invalid stops 只拒绝一次）");
    Print("📊 使用余额: ", RiskPercent, "%");

    if(DailyLossPercent <= 0)
    {
        Print("❌ Ultra 日亏比例必须大于 0，单笔上限按日亏的 50% 计算");
        return(INIT_FAILED);
    }

    DailyRiskLoadOrReset();
    Print("🛡️ 日内止损(EA端): 限额=$", DoubleToString(DailyLossLimitUSD(), 2),
          " (初始资金 $", DoubleToString(EffInitialBalance(), 2), " × ", DailyLossPercent, "%)",
          " 缓冲=", DailyLossBufferPercent, "% 触发线权益=$", DoubleToString(DailyTriggerFloor(), 2));
    Print("🛡️ 单笔止损: $", DoubleToString(IdeaRiskUSD(), 2),
          " = min(起始资金, 当日锚点) × 日亏 ", DailyLossPercent, "% × ",
          DoubleToString(IDEA_RISK_FRACTION * 100.0, 0), "% × (1-",
          DoubleToString(DailyLossBufferPercent, 0), "% 缓冲)。与日亏线取更紧者，开仓时挂上");
    
    // 恢复外部平仓序号，避免 EA 重启后 simulate 漏同步（旧版 GV 不带登录号，升级时沿用）
    if(GlobalVariableCheck(DailyGVName("close_seq")))
        ea_close_seq = (int)GlobalVariableGet(DailyGVName("close_seq"));
    else if(GlobalVariableCheck(LegacyGVName("close_seq")))
        ea_close_seq = (int)GlobalVariableGet(LegacyGVName("close_seq"));
    
    // 启动时立即检查已有持仓保护性SL（硬止损∩日亏线取更紧）
    EnsureStopLosses();
    UpdatePassTP();
    SyncPositionState();
    WriteAccountHeartbeat();
    // 周末/休市没有 tick，心跳、配置重读、出入金检测靠定时器
    EventSetTimer(5);
    
    return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| 定时器：心跳 + 重读配置 + 出入金检测（不依赖 tick）               |
//+------------------------------------------------------------------+
void OnTimer()
{
    static datetime last_deposit_scan = 0;
    LoadSimConfig(false);
    if(TimeLocal() - last_deposit_scan >= 3600)
    {
        last_deposit_scan = TimeLocal();
        g_initial_deposit = DetectInitialDeposit();
    }
    CheckBalanceOperations();
    WriteAccountHeartbeat();
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
    
    Print("EA已停止");
}

//+------------------------------------------------------------------+
//| Expert tick function                                              |
//+------------------------------------------------------------------+
void OnTick()
{
    // 日内亏损检查逐tick执行（不受节流影响）：真实权益对比触发线，触线立即全平
    DailyRiskCheck();
    ChallengePassCheck();
    // 检测硬止损/服务器SL等外部平仓，并写回 ea_position 供 simulate 镜像
    SyncPositionState();
    
    datetime current_time = TimeCurrent();
    if(current_time - last_check_time < CheckIntervalSeconds)
        return;
        
    last_check_time = current_time;
    // 统一维护保护性SL：min(单笔硬止损, 日亏触发线SL)，取更紧者
    UpdateProtectiveSL();
    UpdatePassTP();
    CheckDatabaseSignals();
}

//+------------------------------------------------------------------+
//| 单笔允许亏损（美元）。Ultra：不超过当日回撤额度的 50%，再留缓冲。   |
//| 基数取起始资金和当日锚点的较低者，账户缩水时上限跟着变小。           |
//+------------------------------------------------------------------+
double IdeaRiskUSD()
{
    double base = EffInitialBalance();
    if(daily_anchor > 0 && daily_anchor < base)
        base = daily_anchor;
    double buf = 1.0 - DailyLossBufferPercent / 100.0;
    if(buf <= 0.0 || buf > 1.0)
        buf = 1.0;
    return base * (DailyLossPercent / 100.0) * IDEA_RISK_FRACTION * buf;
}

//+------------------------------------------------------------------+
//| 计算单笔止损距离（价格单位）。不放宽到经纪商最小距离，放宽会超限。   |
//+------------------------------------------------------------------+
double CalcHardSLDistance(double lots)
{
    if(lots <= 0) return 0;

    double tick_value = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
    double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
    if(tick_value <= 0 || tick_size <= 0) return 0;

    double loss_per_unit = tick_value / tick_size;
    double risk_amount = IdeaRiskUSD();
    if(risk_amount <= 0 || loss_per_unit <= 0) return 0;
    return risk_amount / (loss_per_unit * lots);
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
    if(DailyLossPercent <= 0 || daily_halted || lots <= 0 || daily_anchor <= 0 || EffInitialBalance() <= 0)
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
    sl = NormalizeDouble(sl, digits);
    // 余额已不高于触发线时 offset >= 0，止损会落到盈利一侧。返回 0，避免被当成更紧的止损随市价单发出去。
    if(type == POSITION_TYPE_BUY && sl >= open_price) return 0;
    if(type == POSITION_TYPE_SELL && sl <= open_price) return 0;
    return sl;
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
//| 止损是否在现价正确一侧，且不近于经纪商最小距离                       |
//| 多单：SL <= Bid - 最小距离；空单：SL >= Ask + 最小距离              |
//+------------------------------------------------------------------+
bool SlIsPlaceable(ENUM_POSITION_TYPE type, double sl)
{
    if(sl <= 0) return false;
    double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
    double min_stop = (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * point;
    if(point <= 0) point = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
    // stops level 为 0 时仍要求至少离开现价 1 个点，避免止损贴在买/卖价上被拒
    if(min_stop < point) min_stop = point;
    double market = (type == POSITION_TYPE_BUY) ? SymbolInfoDouble(_Symbol, SYMBOL_BID)
                                                : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    if(market <= 0) return false;
    if(type == POSITION_TYPE_BUY) return (sl <= market - min_stop);
    return (sl >= market + min_stop);
}

//+------------------------------------------------------------------+
//| 余额已经不高于日亏触发线：停止当日新开仓。不平掉已有持仓，           |
//| 净值可能仍靠浮盈停在触发线上方，此时强平会把浮盈兑现成亏损。         |
//+------------------------------------------------------------------+
void HaltDailyNewEntries()
{
    if(daily_halted) return;
    daily_halted = true;
    GlobalVariableSet(DailyGVName("halted"), 1.0);
    Print("🛑 余额已不高于日内触发线，停止当日新开仓（保留现有持仓，由净值触线逻辑兜底）");
    Print("🛑 余额=$", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2),
          " 净值=$", DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2),
          " 触发线=$", DoubleToString(DailyTriggerFloor(), 2),
          " 锚点=$", DoubleToString(daily_anchor, 2));
    WriteDailyStatus("balance_below_floor");
}

//+------------------------------------------------------------------+
//| 开仓前确认保护止损能被服务器接受。不能挂出就消费信号，避免每秒重试。 |
//| 开仓前两道止损都必须能挂出。单笔止损挂不出就不开仓，避免退回日亏宽止损。 |
//+------------------------------------------------------------------+
bool PrepareOpenSl(long signal_id, ENUM_POSITION_TYPE type, double daily_sl, double hard_sl, double &sl_out)
{
    sl_out = 0;
    bool daily_ok = SlIsPlaceable(type, daily_sl);
    bool hard_ok = SlIsPlaceable(type, hard_sl);
    int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);

    if(!hard_ok)
    {
        Print("🚫 单笔止损无法挂出，放弃开仓。计算价=", DoubleToString(hard_sl, digits),
              " 允许亏损=$", DoubleToString(IdeaRiskUSD(), 2));
        MarkSignalConsumed(signal_id);
        return false;
    }

    if(!daily_ok)
    {
        Print("🚫 日亏保护止损无法挂出，放弃开仓。计算价=", DoubleToString(daily_sl, digits),
              " 余额=$", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2),
              " 净值=$", DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2),
              " 触发线=$", DoubleToString(DailyTriggerFloor(), 2));
        if(AccountInfoDouble(ACCOUNT_BALANCE) <= DailyTriggerFloor())
            HaltDailyNewEntries();
        MarkSignalConsumed(signal_id);
        return false;
    }

    sl_out = TighterSL(type, hard_sl, daily_sl);
    if(sl_out <= 0 || !SlIsPlaceable(type, sl_out))
    {
        Print("🚫 保护止损距离现价不足，放弃开仓。日亏线=", DoubleToString(daily_sl, digits),
              " 单笔止损=", DoubleToString(hard_sl, digits));
        MarkSignalConsumed(signal_id);
        return false;
    }
    return true;
}

//+------------------------------------------------------------------+
//| 下单被拒时打印当时的价格和日亏锚点。Invalid stops 消费信号，不再重试。 |
//+------------------------------------------------------------------+
void NoteOrderRejected(long signal_id, double price, double sl)
{
    uint rc = trade.ResultRetcode();
    int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
    double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
    double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    Print("❌ 执行失败: ", rc, " ", trade.ResultRetcodeDescription());
    Print("❌ 请求价=", DoubleToString(price, digits),
          " 止损=", DoubleToString(sl, digits),
          " Bid=", DoubleToString(bid, digits),
          " Ask=", DoubleToString(ask, digits),
          " 余额=$", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2),
          " 净值=$", DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2),
          " 触发线=$", DoubleToString(DailyTriggerFloor(), 2),
          " 锚点=$", DoubleToString(daily_anchor, 2));
    if(rc != TRADE_RETCODE_INVALID_STOPS)
        return;
    Print("🚫 服务器拒绝止损(Invalid stops)，本信号不再重试");
    if(DailyLossPercent > 0 && AccountInfoDouble(ACCOUNT_BALANCE) <= DailyTriggerFloor())
        HaltDailyNewEntries();
    MarkSignalConsumed(signal_id);
}

//+------------------------------------------------------------------+
//| 统一维护持仓保护性SL：显式取 min(硬止损, 日亏触发线)，只收紧不放松  |
//+------------------------------------------------------------------+
void UpdateProtectiveSL()
{
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
//| 平仓手续费估算：按本仓位开仓成交的手续费/费用算（多数券商两边相同）。 |
//+------------------------------------------------------------------+
double EstimateCloseCost(long position_id)
{
    static long   cached_id = -1;
    static double cached_cost = 0;
    if(position_id == cached_id)
        return cached_cost;
    if(!HistorySelectByPosition(position_id))
        return 0;
    double cost = 0;
    bool found = false;
    for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
    {
        ulong deal = HistoryDealGetTicket(i);
        if(deal == 0 || HistoryDealGetInteger(deal, DEAL_ENTRY) != DEAL_ENTRY_IN)
            continue;
        found = true;
        cost += MathAbs(HistoryDealGetDouble(deal, DEAL_COMMISSION)) + MathAbs(HistoryDealGetDouble(deal, DEAL_FEE));
    }
    // 开仓成交还没进历史时不缓存，下一秒再查
    if(found)
    {
        cached_id = position_id;
        cached_cost = cost;
    }
    return cost;
}

//+------------------------------------------------------------------+
//| 考核止盈价：成交后余额 = 官方线 + 平仓手续费，再多留几个价位的滑点。 |
//| pos_float 为本仓位当前浮盈+隔夜费，从净值里扣掉后得到不含本仓的基数。 |
//+------------------------------------------------------------------+
double CalcPassTPPrice(ENUM_POSITION_TYPE type, double lots, double open_price, double pos_float, double close_cost)
{
    if(g_challenge_pass_equity <= 0 || lots <= 0 || open_price <= 0)
        return 0;
    // 已经过线时 needed 为负，算出的止盈价在现价另一侧，由 UpdatePassTP 直接平仓
    double needed = g_challenge_pass_equity + close_cost - (AccountInfoDouble(ACCOUNT_EQUITY) - pos_float);

    bool is_buy = (type == POSITION_TYPE_BUY);
    double per_unit = 0;
    if(!OrderCalcProfit(is_buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, _Symbol, lots, open_price,
                        is_buy ? open_price + 1.0 : open_price - 1.0, per_unit) || per_unit <= 0)
    {
        double tick_value = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
        double ts = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
        per_unit = (ts > 0) ? lots * tick_value / ts : 0;
    }
    if(per_unit <= 0)
        return 0;

    double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
    if(tick_size <= 0) tick_size = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
    double dist = needed / per_unit + PASS_TP_SLIP_TICKS * tick_size;
    double raw = is_buy ? open_price + dist : open_price - dist;
    if(raw <= 0)
        return 0;
    // 多单向上、空单向下取整到最小价位，保证成交时余额不低于官方线
    double tp = is_buy ? MathCeil(raw / tick_size) * tick_size : MathFloor(raw / tick_size) * tick_size;
    return NormalizeDouble(tp, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS));
}

//+------------------------------------------------------------------+
//| 考核阶段给持仓挂服务器端止盈（净值到平仓线的价格），EA 掉线也会成交。 |
//| 止盈是仓位自带属性，仓位被策略/止损/收盘平掉时随之消失，无需撤单。   |
//| 止盈价已被现价越过（券商不收）时直接平仓，效果等同止盈立即成交。     |
//+------------------------------------------------------------------+
void UpdatePassTP()
{
    static datetime last_fail = 0;
    static ulong    noted_ticket = 0;
    if(pass_halted || TimeLocal() - last_fail < 30)
        return;
    int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
    double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
    if(tick_size <= 0) tick_size = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
    double min_stop = (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * SymbolInfoDouble(_Symbol, SYMBOL_POINT);

    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(!PositionSelectByTicket(ticket))
            continue;
        if(PositionGetInteger(POSITION_MAGIC) != MagicNumber)
            continue;
        ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
        double cur_sl = PositionGetDouble(POSITION_SL);
        double cur_tp = PositionGetDouble(POSITION_TP);

        if(g_challenge_pass_equity <= 0)
        {
            // Funded / 通过线清零：撤掉之前挂的考核止盈
            if(cur_tp > 0)
            {
                if(trade.PositionModify(ticket, cur_sl, 0))
                    Print("🎯 无考核通过线，已撤掉持仓 ", ticket, " 的考核止盈");
                else
                    last_fail = TimeLocal();
            }
            continue;
        }

        double lots = PositionGetDouble(POSITION_VOLUME);
        double open_price = PositionGetDouble(POSITION_PRICE_OPEN);
        double pos_float = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
        double close_cost = EstimateCloseCost(PositionGetInteger(POSITION_IDENTIFIER));
        double target_tp = CalcPassTPPrice(type, lots, open_price, pos_float, close_cost);
        if(target_tp <= 0)
            continue;
        // 净值小数误差会让止盈价在相邻价位间跳动，差 1.5 个价位以内不改单（滑点余量已覆盖）
        if(cur_tp > 0 && MathAbs(cur_tp - target_tp) < tick_size * 1.5)
            continue;

        double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
        double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
        bool crossed = (type == POSITION_TYPE_BUY) ? (target_tp <= bid) : (target_tp >= ask);
        if(crossed)
        {
            Print("🎯 现价已越过考核止盈价 ", DoubleToString(target_tp, digits), "，直接平仓");
            CloseAllPositions();
            return;
        }
        bool placeable = (type == POSITION_TYPE_BUY) ? (target_tp > bid + min_stop) : (target_tp < ask - min_stop);
        if(!placeable)
        {
            // 离现价不足最小止损距离：下一秒要么越过（直接平），要么拉开（挂上）
            if(noted_ticket != ticket)
            {
                noted_ticket = ticket;
                Print("🎯 考核止盈价 ", DoubleToString(target_tp, digits), " 离现价不足最小距离，暂不挂单");
            }
            continue;
        }

        if(trade.PositionModify(ticket, cur_sl, target_tp))
            Print("🎯 已挂考核止盈: ", DoubleToString(target_tp, digits),
                  "（官方线 $", DoubleToString(g_challenge_pass_equity, 2),
                  " + 平仓费 $", DoubleToString(close_cost, 2),
                  " + ", DoubleToString(PASS_TP_SLIP_TICKS, 0), " 个价位滑点，持仓 ", ticket, "）");
        else
        {
            last_fail = TimeLocal();
            Print("❌ 挂考核止盈失败，持仓 ", ticket, " 错误: ", trade.ResultRetcode(), "，30 秒后重试");
        }
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
    
    // 方式1: 尝试读写模式（文件不存在则创建，EA 可以先于 simulate 启动）
    if(UseCommonPath)
    {
        db_handle = DatabaseOpen(DBPath, DATABASE_OPEN_READWRITE | DATABASE_OPEN_CREATE | DATABASE_OPEN_COMMON);
    }
    else
    {
        db_handle = DatabaseOpen(DBPath, DATABASE_OPEN_READWRITE | DATABASE_OPEN_CREATE);
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
//| 账户初始资金：只用 simulate 写入 sim_config 的起始资金（0=未就绪） |
//+------------------------------------------------------------------+
double EffInitialBalance()
{
    return g_sim_initial_balance;
}

bool SimTableExists(string name)
{
    long n = 0;
    int req = DatabasePrepare(db_handle, "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = '" + name + "'");
    if(req == INVALID_HANDLE)
        return false;
    if(DatabaseRead(req))
        DatabaseColumnLong(req, 0, n);
    DatabaseFinalize(req);
    return n > 0;
}

//+------------------------------------------------------------------+
//| 读 simulate 下发的账户配置。读失败时保持上一次的值，不能清零       |
//+------------------------------------------------------------------+
void LoadSimConfig(bool force)
{
    static datetime last_load = 0;
    if(db_handle == INVALID_HANDLE)
        return;
    if(!force && TimeLocal() - last_load < 3)
        return;
    last_load = TimeLocal();
    if(!SimTableExists("sim_config"))
        return;

    int req = DatabasePrepare(db_handle,
        "SELECT key, value FROM sim_config WHERE key IN ('account_start_balance', 'leverage', 'account_login')");
    if(req == INVALID_HANDLE)
        return;
    double start = g_sim_initial_balance, lev = g_sim_leverage;
    long login = g_sim_login;
    while(DatabaseRead(req))
    {
        string key = "";
        double v = 0;
        if(!DatabaseColumnText(req, 0, key) || !DatabaseColumnDouble(req, 1, v))
            continue;
        if(key == "account_start_balance" && v > 0)
            start = v;
        else if(key == "leverage" && v > 0)
            lev = v;
        else if(key == "account_login" && v > 0)
            login = (long)MathRound(v);
    }
    DatabaseFinalize(req);

    if(MathAbs(start - g_sim_initial_balance) >= 0.005)
        Print("🔗 起始资金(simulate): $", DoubleToString(start, 2));
    if(MathAbs(lev - g_sim_leverage) >= 0.0001)
        Print("🔗 杠杆(simulate): ", lev, "x");
    if(login != g_sim_login)
        Print("🔗 simulate 绑定账户: ", login, (login == AccountInfoInteger(ACCOUNT_LOGIN)) ? "（与本终端一致）" : "（⚠️ 与本终端登录号不一致，不会开仓）");
    g_sim_initial_balance = start;
    g_sim_leverage = lev;
    g_sim_login = login;
}

bool SimConfigReady(string &why)
{
    if(g_sim_initial_balance <= 0) { why = "缺起始资金"; return false; }
    if(g_sim_leverage <= 0) { why = "缺杠杆"; return false; }
    if(g_sim_login <= 0) { why = "缺绑定账户"; return false; }
    if(g_sim_login != AccountInfoInteger(ACCOUNT_LOGIN))
    {
        why = "simulate 绑定账户 " + IntegerToString(g_sim_login) + " 与本终端 " + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + " 不一致";
        return false;
    }
    why = "";
    return true;
}

//+------------------------------------------------------------------+
//| MT5 历史里最早一笔正数入金，作为 simulate 起始资金的默认值         |
//+------------------------------------------------------------------+
double DetectInitialDeposit()
{
    if(!HistorySelect(0, TimeCurrent() + 86400))
        return g_initial_deposit;
    datetime first_time = 0;
    double first_amount = 0;
    int n = HistoryDealsTotal();
    for(int i = 0; i < n; i++)
    {
        ulong t = HistoryDealGetTicket(i);
        if(t == 0 || HistoryDealGetInteger(t, DEAL_TYPE) != DEAL_TYPE_BALANCE)
            continue;
        double amount = HistoryDealGetDouble(t, DEAL_PROFIT);
        datetime when = (datetime)HistoryDealGetInteger(t, DEAL_TIME);
        if(amount > 0 && (first_time == 0 || when < first_time))
        {
            first_time = when;
            first_amount = amount;
        }
    }
    return first_amount;
}

string SqlText(string s)
{
    StringReplace(s, "'", "''");
    return s;
}

//+------------------------------------------------------------------+
//| 账户心跳：simulate 启动时据此读取余额/登录号，不用手输              |
//+------------------------------------------------------------------+
void WriteAccountHeartbeat()
{
    if(db_handle == INVALID_HANDLE)
        return;
    if(!DatabaseExecute(db_handle,
        "CREATE TABLE IF NOT EXISTS ea_account (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "login INTEGER, server TEXT, company TEXT, name TEXT, currency TEXT, "
        "balance REAL, equity REAL, initial_deposit REAL, symbol TEXT, ea_build TEXT, "
        "config_ready INTEGER, config_note TEXT, updated_at TEXT)"))
        return;
    string why = "";
    bool ready = SimConfigReady(why);
    string sql = StringFormat(
        "INSERT OR REPLACE INTO ea_account (id, login, server, company, name, currency, balance, equity, "
        "initial_deposit, symbol, ea_build, config_ready, config_note, updated_at) "
        "VALUES (1, %I64d, '%s', '%s', '%s', '%s', %.2f, %.2f, %.2f, '%s', '%s', %d, '%s', '%s')",
        AccountInfoInteger(ACCOUNT_LOGIN),
        SqlText(AccountInfoString(ACCOUNT_SERVER)),
        SqlText(AccountInfoString(ACCOUNT_COMPANY)),
        SqlText(AccountInfoString(ACCOUNT_NAME)),
        SqlText(AccountInfoString(ACCOUNT_CURRENCY)),
        AccountInfoDouble(ACCOUNT_BALANCE),
        AccountInfoDouble(ACCOUNT_EQUITY),
        g_initial_deposit,
        SqlText(_Symbol),
        SqlText(EA_BUILD),
        ready ? 1 : 0,
        SqlText(why),
        TimeToString(TimeGMT(), TIME_DATE | TIME_SECONDS));
    if(!DatabaseExecute(db_handle, sql))
        Print("⚠️ 写入 ea_account 失败: ", GetLastError());
}

//+------------------------------------------------------------------+
//| 检查数据库信号                                                    |
//+------------------------------------------------------------------+
void CheckDatabaseSignals()
{
    if(db_handle == INVALID_HANDLE)
        return;
    LoadSimConfig(false);
    // simulate 尚未建表（EA 先启动）时静默等待，避免每秒打印查询失败
    static bool signals_ready = false;
    static datetime last_table_probe = 0;
    if(!signals_ready)
    {
        if(TimeLocal() - last_table_probe < 5)
            return;
        last_table_probe = TimeLocal();
        if(!SimTableExists("signals"))
            return;
        signals_ready = true;
    }
    
    // 查询所有未消费的信号，按时间倒序排列，获取最新的一条
    string query = "SELECT id, action, size_mult FROM signals WHERE consumed = 0 ORDER BY created_at DESC LIMIT 1";
    
    int request = DatabasePrepare(db_handle, query);
    bool has_size_mult = true;
    if(request == INVALID_HANDLE)
    {
        // 旧版 simulate 建的表没有 size_mult 列，按满仓处理
        has_size_mult = false;
        request = DatabasePrepare(db_handle, "SELECT id, action FROM signals WHERE consumed = 0 ORDER BY created_at DESC LIMIT 1");
    }
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
        signal_size_mult = 1.0;
        if(has_size_mult)
            DatabaseColumnDouble(request, 2, signal_size_mult);
        if(signal_size_mult <= 0 || signal_size_mult > 1.0)
            signal_size_mult = 1.0;
        
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
    double sent_price = 0;
    double sent_sl = 0;
    
    // 配置未就绪或账户不对：不开仓，只消费开仓信号。平仓信号照常执行
    string cfg_why = "";
    if((action == "BUY" || action == "SELL") && !SimConfigReady(cfg_why))
    {
        Print("🚫 simulate 配置未就绪（", cfg_why, "），不开仓，忽略信号: ", action);
        MarkSignalConsumed(signal_id);
        return;
    }

    // 考核已通过：停止新开仓，只消费掉开仓信号（通过线为 0 时 pass_halted 保持 false）
    if(pass_halted && (action == "BUY" || action == "SELL"))
    {
        Print("🚫 考核已通过，不再开仓，忽略信号: ", action);
        MarkSignalConsumed(signal_id);
        return;
    }

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
            Print("🔄 检测到买入信号，先平掉现有空仓");
            CloseAllPositions();
            MarkSignalConsumed(signal_id);
            return;
        }
        else
        {
            // 无持仓，开多仓
            lots = ApplySizeMult(CalculateLotSize());
            if(lots <= 0)
            {
                Print("❌ 计算手数失败，余额不足");
                MarkSignalConsumed(signal_id);
                return;
            }
            
            double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
            double daily_sl = CalcDailyFloorSLPrice(lots, ask, POSITION_TYPE_BUY);
            double hard_sl = CalcHardSLPrice(lots, ask, POSITION_TYPE_BUY);
            double sl = 0;
            if(!PrepareOpenSl(signal_id, POSITION_TYPE_BUY, daily_sl, hard_sl, sl))
                return;
            sent_price = ask;
            sent_sl = sl;
            Print("📤 准备开多 lots=", DoubleToString(lots, 2),
                  " ask=", DoubleToString(ask, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS)),
                  " sl=", DoubleToString(sl, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS)),
                  " 单笔上限=$", DoubleToString(IdeaRiskUSD(), 2));
            result = trade.Buy(lots, _Symbol, ask, sl, 0, "QQQ Signal Buy");
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
            lots = ApplySizeMult(CalculateLotSize());
            if(lots <= 0)
            {
                Print("❌ 计算手数失败，余额不足");
                MarkSignalConsumed(signal_id);
                return;
            }
            
            double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
            double daily_sl = CalcDailyFloorSLPrice(lots, bid, POSITION_TYPE_SELL);
            double hard_sl = CalcHardSLPrice(lots, bid, POSITION_TYPE_SELL);
            double sl = 0;
            if(!PrepareOpenSl(signal_id, POSITION_TYPE_SELL, daily_sl, hard_sl, sl))
                return;
            sent_price = bid;
            sent_sl = sl;
            Print("📤 准备开空 lots=", DoubleToString(lots, 2),
                  " bid=", DoubleToString(bid, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS)),
                  " sl=", DoubleToString(sl, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS)),
                  " 单笔上限=$", DoubleToString(IdeaRiskUSD(), 2));
            result = trade.Sell(lots, _Symbol, bid, sl, 0, "QQQ Signal Sell");
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
        if(action == "BUY" || action == "SELL")
            UpdatePassTP();
    }
    else
    {
        NoteOrderRejected(signal_id, sent_price, sent_sl);
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
                // 标记为 EA 自身平仓：SyncPositionState 据此跳过 close_seq 递增，
                // 避免 simulate 把 EA 响应 CLOSE 信号的正常平仓误判成硬止损外部平仓
                if(trade.PositionClose(ticket))
                    internal_close_pending = true;
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
//| 按信号的 size_mult 缩小手数（开盘冷清半仓）                        |
//+------------------------------------------------------------------+
double ApplySizeMult(double lots)
{
    if(lots <= 0 || signal_size_mult >= 1.0)
        return lots;
    double min_lot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
    double lot_step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
    double scaled = lots * signal_size_mult;
    if(lot_step > 0)
        scaled = MathFloor(scaled / lot_step + 1e-9) * lot_step;
    scaled = NormalizeDouble(MathMax(scaled, min_lot), 8);
    Print("📉 开盘冷清半仓: 手数 ", DoubleToString(lots, 2), " × ", DoubleToString(signal_size_mult, 2),
          " → ", DoubleToString(scaled, 2));
    return scaled;
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
    double total_trading_value = risk_amount * g_sim_leverage;
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
    Print("📊 设定杠杆倍数: ", g_sim_leverage, "倍（simulate 下发）");
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
//| 注意：本文件限额基于账户初始资金                                |
//+------------------------------------------------------------------+

// GV 是整个终端共用的，按登录号区分，换账户自动得到一套新的风控/通过状态
string DailyGVName(string suffix)
{
    return "DLS_" + IntegerToString(MagicNumber) + "_" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "_" + suffix;
}

string LegacyGVName(string suffix)
{
    return "DLS_" + IntegerToString(MagicNumber) + "_" + suffix;
}

//+------------------------------------------------------------------+
//| 考核通过：simulate 写入官方通过线 challenge_pass_equity（0=关闭/Funded）|
//| 开仓挂券商止盈到线即平；空仓且余额过线则停止开仓。                   |
//+------------------------------------------------------------------+
void ApplyChallengePassTarget(double value, double session)
{
    // simulate 每次启动写一个新会话号；会话号变了说明换了阶段或账户，上次的通过停止状态作废
    if(session > 0)
    {
        string session_gv = DailyGVName("pass_session");
        if(!GlobalVariableCheck(session_gv) || MathAbs(GlobalVariableGet(session_gv) - session) > 0.5)
        {
            if(pass_halted || GlobalVariableCheck(DailyGVName("pass_halted")))
                Print("simulate 已重新启动（新会话），清除上次的考核通过停止状态");
            pass_halted = false;
            pass_status_pending = false;
            if(GlobalVariableCheck(DailyGVName("pass_halted")))
                GlobalVariableDel(DailyGVName("pass_halted"));
            GlobalVariableSet(session_gv, session);
        }
    }
    if(value <= 0)
    {
        bool had = (g_challenge_pass_equity > 0 || pass_halted || GlobalVariableCheck(DailyGVName("pass_halted")));
        g_challenge_pass_equity = 0;
        pass_halted = false;
        if(GlobalVariableCheck(DailyGVName("pass_halted")))
            GlobalVariableDel(DailyGVName("pass_halted"));
        if(had)
            Print("考核通过检测已关闭（通过线为 0）");
        return;
    }
    if(MathAbs(value - g_challenge_pass_equity) >= 0.5)
        Print("考核通过线: $", DoubleToString(value, 2), "（开仓即挂券商止盈）");
    g_challenge_pass_equity = value;
    if(!pass_halted && GlobalVariableCheck(DailyGVName("pass_halted")) && GlobalVariableGet(DailyGVName("pass_halted")) > 0.5)
        pass_halted = true;
}

void LoadChallengePassTarget(bool force)
{
    static datetime last_load = 0;
    static bool table_ready = false;
    if(db_handle == INVALID_HANDLE)
        return;
    // 未武装时每秒读一次即可。已武装时每个 tick 都重读，避免切到 Funded（写成 0）后仍按旧线平仓
    if(!force && TimeLocal() - last_load < 1)
        return;
    last_load = TimeLocal();

    if(!table_ready)
    {
        long has_table = 0;
        int probe = DatabasePrepare(db_handle, "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'sim_config'");
        if(probe == INVALID_HANDLE)
            return;
        if(DatabaseRead(probe))
            DatabaseColumnLong(probe, 0, has_table);
        DatabaseFinalize(probe);
        if(has_table == 0)
            return;
        table_ready = true;
    }

    int req = DatabasePrepare(db_handle,
        "SELECT key, value FROM sim_config WHERE key IN ('challenge_pass_equity', 'challenge_pass_session')");
    if(req == INVALID_HANDLE)
        return;
    double value = 0;
    double session = 0;
    bool found = false;
    while(DatabaseRead(req))
    {
        string key = "";
        double v = 0;
        if(!DatabaseColumnText(req, 0, key) || !DatabaseColumnDouble(req, 1, v))
            continue;
        if(key == "challenge_pass_equity")
        {
            value = v;
            found = true;
        }
        else if(key == "challenge_pass_session")
            session = v;
    }
    DatabaseFinalize(req);
    // 库忙或旧版 simulate 没写通过线时保持现状，不能因为读失败就关掉检测
    if(!found)
        return;
    ApplyChallengePassTarget(value, session);
}

bool WritePassStatus()
{
    if(db_handle == INVALID_HANDLE)
        return false;
    if(!DatabaseExecute(db_handle,
        "CREATE TABLE IF NOT EXISTS ea_pass_status (id INTEGER PRIMARY KEY, halted INTEGER, equity REAL, target REAL, updated_at TEXT)"))
    {
        Print("创建 ea_pass_status 失败: ", GetLastError());
        return false;
    }
    string when = TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS);
    StringReplace(when, "'", "");
    string sql = StringFormat(
        "INSERT OR REPLACE INTO ea_pass_status (id, halted, equity, target, updated_at) VALUES (1, 1, %.2f, %.2f, '%s')",
        AccountInfoDouble(ACCOUNT_EQUITY),
        g_challenge_pass_equity,
        when);
    if(!DatabaseExecute(db_handle, sql))
    {
        Print("写入 ea_pass_status 失败: ", GetLastError());
        return false;
    }
    return true;
}

bool HasPassPositions()
{
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(PositionSelectByTicket(ticket) && PositionGetInteger(POSITION_MAGIC) == MagicNumber)
            return true;
    }
    return false;
}

void ChallengePassCheck()
{
    static datetime last_retry = 0;
    if(g_challenge_pass_equity > 0)
        LoadChallengePassTarget(true);
    else
        LoadChallengePassTarget(false);
    if(g_challenge_pass_equity <= 0)
        return;
    if(pass_halted)
    {
        // 停止期间若仍有持仓（如人工开仓）则平掉；状态行可能因库忙没写进去，每 5 秒补一次
        if(TimeLocal() - last_retry >= 5)
        {
            last_retry = TimeLocal();
            if(HasPassPositions())
            {
                Print("考核已通过但仍有持仓，全平");
                CloseAllPositions();
            }
            if(pass_status_pending && WritePassStatus())
                pass_status_pending = false;
        }
        return;
    }
    // 通过只认空仓后的余额：考核止盈单成交，或其它平仓后余额已过官方线
    if(HasPassPositions() || AccountInfoDouble(ACCOUNT_BALANCE) < g_challenge_pass_equity)
        return;
    Print("!!!!! 已空仓且余额达到官方考核通过线，停止开仓 !!!!!");
    Print("当前余额: $", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2),
          " 通过线: $", DoubleToString(g_challenge_pass_equity, 2));
    pass_halted = true;
    GlobalVariableSet(DailyGVName("pass_halted"), 1.0);
    pass_status_pending = !WritePassStatus();
}


double DailyLossLimitUSD()
{
    return EffInitialBalance() * DailyLossPercent / 100.0;
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
    if(ForceResetDailyRisk)
    {
        Print("⚠️ ForceResetDailyRisk=true，强制按当前权益重算日内锚点（完成后请取消勾选该参数再重新加载EA）");
        DailyRiskNewDay(period);
        return;
    }
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
    GlobalVariableSet(DailyGVName("anchor_at"), (double)TimeCurrent());
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
    // 没有起始资金时日亏限额为 0，触发线等于锚点，会被误判触线全平
    if(EffInitialBalance() <= 0)
        return;
    
    double equity = AccountInfoDouble(ACCOUNT_EQUITY);
    double floor_eq = DailyTriggerFloor();
    
    if(!daily_halted && equity <= floor_eq)
    {
        Print("🛑 !!!!! 权益触及日内亏损触发线，立即全平并停止当日交易 !!!!!");
        Print("🛑 当前权益: $", DoubleToString(equity, 2),
              " 触发线: $", DoubleToString(floor_eq, 2),
              " 锚点: $", DoubleToString(daily_anchor, 2),
              " 官方限额: $", DoubleToString(DailyLossLimitUSD(), 2));
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
//| 锚点设定之后出现出入金（出金、补款、返还）：按当前余额重算锚点      |
//| 不重算的话出金会让触发线高于真实余额，下一笔就被误判触线            |
//+------------------------------------------------------------------+
void CheckBalanceOperations()
{
    static datetime last_scan = 0;
    if(DailyLossPercent <= 0 || daily_period_start == 0)
        return;
    if(TimeLocal() - last_scan < 30)
        return;
    last_scan = TimeLocal();

    datetime anchor_at = daily_period_start;
    if(GlobalVariableCheck(DailyGVName("anchor_at")))
        anchor_at = (datetime)GlobalVariableGet(DailyGVName("anchor_at"));
    if(!HistorySelect(anchor_at, TimeCurrent() + 60))
        return;
    datetime latest_op = 0;
    double net = 0;
    int n = HistoryDealsTotal();
    for(int i = 0; i < n; i++)
    {
        ulong t = HistoryDealGetTicket(i);
        if(t == 0)
            continue;
        long type = HistoryDealGetInteger(t, DEAL_TYPE);
        if(type != DEAL_TYPE_BALANCE && type != DEAL_TYPE_CREDIT && type != DEAL_TYPE_CHARGE &&
           type != DEAL_TYPE_CORRECTION && type != DEAL_TYPE_BONUS)
            continue;
        datetime when = (datetime)HistoryDealGetInteger(t, DEAL_TIME);
        if(when <= anchor_at)
            continue;
        net += HistoryDealGetDouble(t, DEAL_PROFIT);
        if(when > latest_op)
            latest_op = when;
    }
    if(latest_op == 0)
        return;

    double old_anchor = daily_anchor;
    daily_anchor = MathMax(AccountInfoDouble(ACCOUNT_BALANCE), AccountInfoDouble(ACCOUNT_EQUITY));
    GlobalVariableSet(DailyGVName("anchor"), daily_anchor);
    GlobalVariableSet(DailyGVName("anchor_at"), (double)latest_op);
    Print("💵 检测到出入金 $", DoubleToString(net, 2), "，日内锚点 $", DoubleToString(old_anchor, 2),
          " → $", DoubleToString(daily_anchor, 2), "，触发线 $", DoubleToString(DailyTriggerFloor(), 2));
    WriteDailyStatus("balance_operation");
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
    
    // 上一tick还有仓、这一tick没了 → 区分 EA 自身平仓与外部平仓
    if(last_tracked_ticket != 0 && ticket == 0)
    {
        if(internal_close_pending)
        {
            // EA 自己平的（CLOSE信号/反手/日亏全平）：simulate 已通过信号/halted 状态同步，
            // 不递增 close_seq，否则会被误判为硬止损并触发镜像平仓反馈回路
            internal_close_pending = false;
            Print("ℹ️ 持仓消失为 EA 自身平仓，不计入外部平仓序号（close_seq=", ea_close_seq, "）");
            WritePositionStatus(0, 0, 0, 0, "internal_close");
        }
        else
        {
            ea_close_seq++;
            GlobalVariableSet(DailyGVName("close_seq"), (double)ea_close_seq);
            Print("📣 检测到持仓被外部平掉(硬止损/日亏SL/手动等)，close_seq=", ea_close_seq,
                  " 原票号=", last_tracked_ticket, " —— 已写回 ea_position 供 simulate 镜像");
            WritePositionStatus(0, 0, 0, 0, "external_close");
        }
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