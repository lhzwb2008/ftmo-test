"""
IBKR Gateway 微纳指期货（MNQ）交易程序（Windows）

架构：
  - 信号：Longport 行情缓存中的 QQQ.US 分钟线（与 FTMO/IC 策略一致）
  - 执行：ib_insync 直连本机 IB Gateway，交易 MNQ（Micro E-mini Nasdaq-100）
  - 无 SQLite / MT5；无 FTMO 日亏/考试规则

Windows 运行前准备：
  1. 安装 IB Gateway Offline，登录 Paper（或 Live）
  2. Configure → Settings → API → Settings：
     - 勾选 Enable ActiveX and Socket Clients
     - 取消 Read-Only API
     - Socket port：Paper=4002，Live=4001
     - Trusted IPs 加入 127.0.0.1
  3. Configure → Lock and Exit：选「自动重启」(Auto Restart)，勿用「自动退出」
     （IB 强制每日重置；自动退出会导致 API 端口关闭，策略进程旧版会直接退出）
  4. 确认账户有 CME 期货 / MNQ 交易权限（非 Index CFD）
  5. 启动 longport_data_service.py（写入 market_data_cache.db）
  6. pip install -r requirements.txt（含 ib_insync）
  7. python simulate_ibkr.py

.env 仅需 Longport 凭证；IB 连接参数写死在下方常量。
仓位：按 IB 净值 100% / 日内单张初始保证金估算张数；Error 201 则按 90%→85%→70% 减张重试（默认不订 IB 行情、不用 whatIf）。
断线：运行中自动心跳探活 + 无限重连（Gateway Auto Restart 窗口内会短暂拒连，脚本会等待恢复）。
风险：QQQ 信号 vs MNQ 存在基差；每季度需换月（更新 MNQ_EXPIRY 或依赖自动选月）；满仓保证金风险极高。
"""

import pandas as pd
from datetime import datetime, time, timedelta, date as date_type
import time as time_module
import os
import sys
import pytz
from math import floor
from dotenv import load_dotenv
import numpy as np
import sqlite3
import threading
import platform

from longport.openapi import OutsideRTH

from trend_er5_gate import history_days_back, apply_entry_gates_to_signal
from k_side_adjust import effective_k1_for_time, format_k_strategy_params

try:
    from ib_insync import IB, Future, ContFuture, MarketOrder
except ImportError:
    print("请先安装 ib_insync: pip install ib_insync")
    sys.exit(1)

load_dotenv(override=True)

# ============================================================================
# 用户配置参数 - 请根据需要修改以下参数
# ============================================================================

# 信号品种（Longport 缓存）与执行合约（IBKR MNQ 期货）
SYMBOL = 'QQQ.US'
TRADE_SYMBOL = 'MNQ'
# 合约月 YYYYMM；空字符串=自动选近月（ContFuture / reqContractDetails）
# ⚠️ 每季度换月手动更新更稳妥（3/6/9/12 月），例如 202609 → 202612
MNQ_EXPIRY = '202609'
MNQ_POINT_VALUE = 2.0  # Micro NQ 每点 $2
NQ_QQQ_RATIO = float(os.environ.get('NQ_QQQ_RATIO', '41.45'))  # NQ≈QQQ×该比值（盈亏估算用）
# IB Gateway（与 Configure → API → Socket port 保持一致；Paper 常见 4002，Live 常见 4001）
IB_HOST = '127.0.0.1'
IB_PORT = 4002
IB_CLIENT_ID = 1
IB_ACCOUNT = ''  # 空=自动取 managedAccounts[0]
# Gateway 每日强制 Auto Logoff/Auto Restart；断线后自动重试，避免进程退出
IB_RECONNECT_INTERVAL_SEC = 45       # 重连间隔（秒）；Gateway 自动重启常需数分钟
IB_RECONNECT_MAX_ATTEMPTS = 0        # 运行中/启动重连次数；0=无限重试直到连上
IB_HEARTBEAT_INTERVAL_SEC = 120      # 心跳探活间隔（秒）；缩短以便更快发现重启断线
IB_KEEPALIVE_SLEEP_CHUNK_SEC = 60    # 长等待分段 sleep，以便心跳/重连
_IB_LAST_HEARTBEAT_TS = 0.0
# 先按净值 100% 估张数（日内约 13x）；Error 201 再减张，不在估算阶段预留 15%
MARGIN_USAGE_PCT = 1.0
# 开仓被 201 拒单后，按首次张数的这些比例重试（90% 吃小误差，70% 约等于隔夜保证金）
IB_OPEN_RETRY_QTY_FRACS = (1.00, 0.90, 0.85, 0.70)
# 信号来自 Longport，IB 只做市价执行，默认不订阅 IB 行情、不用 whatIf（避免 Error 354）
# 张数 = floor(净值 × MARGIN_USAGE_PCT / 单张日内初始保证金)
IB_USE_MARKET_DATA = False
IB_USE_WHATIF_MARGIN = False
# 市价单显式 DAY。Gateway「订单预设」把空 TIF 改成 DAY 时会报 10349，ib_insync 常把原单标成 Cancelled，实际已成交。
IB_TIF = 'DAY'
IB_INFORMATIONAL_ERROR_CODES = frozenset({
    10349,  # Order TIF was set to DAY based on order preset
    2104, 2106, 2107, 2108, 2119, 2158, 399,
})
# 与 ftmo_ibkr_combo_backtest 主口径一致：IBKR 日内初始（收盘前平仓），不是隔夜 12%
# 公开表约 $4,468–$4,597/口，合名义 ~7.5–7.7%；100% 净值有效杠杆约 13x
MNQ_INTRADAY_IM_PCT = 0.0771
# 点位极低时的美元下限，避免低估；当前 MNQ 名义下不会碰到
MNQ_INIT_MARGIN_FLOOR_USD = 3000.0
# 可选硬顶；<=0 表示不限制（能买多少买多少）
MAX_MNQ_CONTRACTS = 0
# accountSummary 订阅状态（保留标志；账户刷新走 accountUpdates）
_ACCOUNT_SUMMARY_SUBSCRIBED = False

# 资金和风控设置（净值从 IB 读取；止盈/日亏默认关闭）
ACCOUNT_START_BALANCE = None
INITIAL_CAPITAL = None
LEVERAGE = None  # 兼容旧日志字段；期货满仓模式下不再使用

# 可选账户级止盈/日内止损（自营默认关闭；设正数启用）
PROFIT_TARGET_PCT = -1
DAILY_LOSS_PCT = -1
TP_BUFFER_PCT = 0.01

MAX_PROFIT_AMOUNT = -1
MAX_DAILY_LOSS_AMOUNT = -1

# 交易时间设置
TRADING_START_TIME = (9, 40)
TRADING_END_TIME = (15, 40)
CHECK_INTERVAL_MINUTES = 15
MAX_POSITIONS_PER_DAY = 10

# 策略参数（与 backtest / FTMO / IC 对齐）
LOOKBACK_DAYS = 1
K1 = 1
K2 = 1.04
ENABLE_K_SIDE_ADJUSTMENT = True

USE_VWAP = False

ENABLE_TRAILING_TAKE_PROFIT = True
TRAILING_TP_ACTIVATION_PCT = 0.006
TRAILING_TP_CALLBACK_PCT = 0.65

DEBUG_MODE = False
DEBUG_TIME = "2025-07-10 10:25:00"
DEBUG_ONCE = True
LOG_VERBOSE = False

# ============================================================================
# 程序内部变量 - 请勿手动修改
# ============================================================================

LOG_FILE = "trading_ibkr.log"

# IB 连接（全局）
IB_CONN = None  # type: IB | None
IB_CONTRACT = None
IB_ACCOUNT_ID = None

# 收益统计变量
TOTAL_PNL = 0.0  # 总收益（累计）
DAILY_PNL = 0.0  # 当日收益
LAST_STATS_DATE = None  # 上次统计日期
DAILY_TRADES = []  # 当日交易记录

# 止盈止损状态标志
DAILY_STOP_TRIGGERED = False  # 当日是否触发了日内止损
PROFIT_TARGET_TRIGGERED = False  # 是否触发了止盈
DAILY_LOSS_MONITOR_ACTIVE = False  # 日内止损监控是否激活
FORCE_CLOSE_POSITION = False  # 强制平仓标志（监控线程设置）

# 线程锁，用于保护共享变量
pnl_lock = threading.Lock()

# 日志文件类 - 将输出同时写入控制台和文件
class Logger:
    def __init__(self, log_file):
        self.terminal = sys.stdout
        self.log_file = log_file
        # 创建日志文件（追加模式）
        self.log = open(log_file, 'a', encoding='utf-8', buffering=1)
        # 写入分隔线标记新的启动
        separator = "\n" + "="*80 + "\n"
        separator += f"程序启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        separator += "="*80 + "\n"
        self.log.write(separator)
        self.log.flush()
    
    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)
        self.log.flush()
    
    def flush(self):
        self.terminal.flush()
        self.log.flush()
    
    def close(self):
        self.log.close()

def apply_optional_risk_settings():
    """账户止盈/日内止损仅在配置为正数时启用；不再询问杠杆。"""
    global MAX_PROFIT_AMOUNT, MAX_DAILY_LOSS_AMOUNT

    if PROFIT_TARGET_PCT > 0:
        MAX_PROFIT_AMOUNT = None  # 待拿到账户净值后再算
    else:
        MAX_PROFIT_AMOUNT = -1
        print("账户止盈: 已禁用（PROFIT_TARGET_PCT 为负）")

    if DAILY_LOSS_PCT > 0:
        MAX_DAILY_LOSS_AMOUNT = None
    else:
        MAX_DAILY_LOSS_AMOUNT = -1
        print("日内止损: 已禁用（DAILY_LOSS_PCT 为负）")


def apply_ib_account_capital():
    """连接 Gateway 后读取 NetLiquidation；仓位按可用保证金满仓，不按杠杆。净值未就绪则阻塞重试。"""
    global ACCOUNT_START_BALANCE, INITIAL_CAPITAL, MAX_PROFIT_AMOUNT, MAX_DAILY_LOSS_AMOUNT

    attempt = 0
    while True:
        attempt += 1
        if not ib_ensure_connected():
            time_module.sleep(IB_RECONNECT_INTERVAL_SEC)
            continue
        refresh_ib_account_data(wait_sec=1.5)
        balance = get_account_balance()
        if balance > 0:
            break
        ts = get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts}] 尚未读到 NetLiquidation（#{attempt}），"
              f"{IB_RECONNECT_INTERVAL_SEC}s 后重试（Gateway 重启后账户数据可能延迟就绪）...")
        time_module.sleep(IB_RECONNECT_INTERVAL_SEC)

    ACCOUNT_START_BALANCE = balance
    INITIAL_CAPITAL = balance
    avail = get_ib_buying_power()
    print(f"IB 账户净值(NetLiquidation): ${balance:,.2f}")
    if avail > 0:
        print(f"可用保证金/购买力: ${avail:,.2f} → 开仓按满仓估算张数（使用 {MARGIN_USAGE_PCT*100:.0f}%）")
    else:
        print("⚠️ 暂未读到 AvailableFunds/ExcessLiquidity；开仓将按净值估算张数")

    if PROFIT_TARGET_PCT > 0:
        tp_buffer = balance * TP_BUFFER_PCT
        MAX_PROFIT_AMOUNT = balance * PROFIT_TARGET_PCT + tp_buffer
        print(f"账户止盈金额: ${MAX_PROFIT_AMOUNT:.2f}")
    if DAILY_LOSS_PCT > 0:
        MAX_DAILY_LOSS_AMOUNT = balance * DAILY_LOSS_PCT
        print(f"日内止损金额: ${MAX_DAILY_LOSS_AMOUNT:.2f}")

def get_common_files_dir():
    """行情缓存目录：Windows 优先 MT5 Common/Files（与 longport_data_service 一致），否则当前目录。"""
    if platform.system() == "Windows":
        appdata_path = os.environ.get('APPDATA', os.path.expanduser('~\\AppData\\Roaming'))
        mt5_common_path = os.path.join(appdata_path, "MetaQuotes", "Terminal", "Common", "Files")
        os.makedirs(mt5_common_path, exist_ok=True)
        return mt5_common_path
    return "."


def get_market_data_db_path():
    return os.environ.get('MARKET_DATA_DB_PATH', os.path.join(get_common_files_dir(), "market_data_cache.db"))


MARKET_DATA_DB_PATH = get_market_data_db_path()
MARKET_DATA_MAX_AGE_SECONDS = int(os.environ.get('MARKET_DATA_MAX_AGE_SECONDS', '120'))

# 开仓幂等：同一检查窗口只下一次单
_LAST_OPEN_SIGNAL_KEY = None


def parse_cache_timestamp(value):
    if not value:
        return None
    eastern = pytz.timezone('US/Eastern')
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return eastern.localize(dt)
    except ValueError:
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                return eastern.localize(dt)
            return dt.astimezone(eastern)
        except ValueError:
            return None


def ensure_market_data_service_available():
    if not os.path.exists(MARKET_DATA_DB_PATH):
        print(f"错误: 行情缓存数据库不存在: {os.path.abspath(MARKET_DATA_DB_PATH)}")
        print("请先启动 longport_data_service.py，等待其写入 market_data_cache.db 后再启动本脚本")
        return False

    try:
        conn = sqlite3.connect(MARKET_DATA_DB_PATH)
        row = conn.execute("""
        SELECT value FROM service_state
        WHERE key = 'last_success_at'
        """).fetchone()
        conn.close()
    except Exception as e:
        print(f"错误: 无法读取行情服务心跳: {str(e)}")
        return False

    if row is None:
        print("错误: 行情服务尚未写入成功心跳，请等待 longport_data_service.py 完成一次更新")
        return False

    last_success_at = parse_cache_timestamp(row[0])
    if last_success_at is None:
        print(f"错误: 行情服务心跳时间格式异常: {row[0]}")
        return False

    age_seconds = (get_us_eastern_time() - last_success_at).total_seconds()
    if age_seconds > MARKET_DATA_MAX_AGE_SECONDS:
        print(f"错误: 行情缓存过旧，最近成功更新时间: {row[0]}，距今 {age_seconds:.0f} 秒")
        return False

    if LOG_VERBOSE:
        print(f"行情缓存服务可用: {os.path.abspath(MARKET_DATA_DB_PATH)}，最近更新 {age_seconds:.0f} 秒前")
    return True

# ============================================================================
# IBKR 执行层（MNQ 期货）
# ============================================================================

def make_mnq_contract():
    """构造 MNQ 合约；指定 MNQ_EXPIRY 用 Future，否则用 ContFuture 自动近月。"""
    if MNQ_EXPIRY:
        return Future(
            symbol=TRADE_SYMBOL,
            lastTradeDateOrContractMonth=MNQ_EXPIRY,
            exchange='CME',
            currency='USD',
        )
    return ContFuture(symbol=TRADE_SYMBOL, exchange='CME', currency='USD')


def resolve_mnq_contract(ib):
    """qualify MNQ；失败时尝试 GLOBEX / 从 contractDetails 选近月。连接断开时抛出异常由上层重试。"""
    if ib is None or not ib.isConnected():
        raise ConnectionError('Not connected')
    contract = make_mnq_contract()
    qualified = ib.qualifyContracts(contract)
    if qualified:
        return qualified[0]

    # ContFuture / 指定月失败时，拉全部 MNQ 细节选最近未到期合约
    for exchange in ('CME', 'GLOBEX'):
        if not ib.isConnected():
            raise ConnectionError('Not connected')
        template = Future(symbol=TRADE_SYMBOL, exchange=exchange, currency='USD')
        try:
            details = ib.reqContractDetails(template)
        except Exception as e:
            print(f"⚠️ reqContractDetails({exchange}) 失败: {e}")
            if 'Not connected' in str(e) or 'disconnect' in str(e).lower():
                raise
            continue
        if not details:
            continue
        today = datetime.utcnow().strftime('%Y%m%d')
        candidates = []
        for d in details:
            c = d.contract
            expiry = (c.lastTradeDateOrContractMonth or '')[:8]
            if expiry and expiry >= today[:len(expiry)]:
                candidates.append(c)
        if not candidates:
            candidates = [d.contract for d in details]
        candidates.sort(key=lambda c: c.lastTradeDateOrContractMonth or '')
        pick = candidates[0]
        q = ib.qualifyContracts(pick)
        if q:
            return q[0]
    return None


def _safe_ib_disconnect(ib):
    """断开临时 IB 实例，忽略异常。"""
    if ib is None:
        return
    try:
        if ib.isConnected():
            ib.disconnect()
    except Exception:
        pass


def _ib_subscribe_account_updates(ib, account='', wait_sec=1.5):
    """
    订阅账户资金推送，但不阻塞等待 accountDownloadEnd。
    ib.reqAccountUpdates() 会等下载结束；纸账户/只读 API 常永不回调，导致进程卡死。
    """
    if ib is None or not ib.isConnected():
        return False
    try:
        ib.client.reqAccountUpdates(True, account or '')
        if wait_sec and wait_sec > 0:
            ib.sleep(float(wait_sec))
        return True
    except Exception as e:
        ts = get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts}] 订阅账户更新失败: {e}")
        return False


def _ib_cached_account_summary(ib, account=''):
    """只读已缓存的 accountSummary，不触发阻塞的 reqAccountSummary。"""
    if ib is None:
        return []
    try:
        items = list(ib.wrapper.acctSummary.values())
    except Exception:
        return []
    if account:
        return [v for v in items if getattr(v, 'account', '') == account]
    return items


def _ib_try_connect(verbose=True):
    """
    尝试连接 IB Gateway 一次并 qualify MNQ。
    成功则设置全局 IB_CONN/IB_CONTRACT/IB_ACCOUNT_ID 并返回 True；失败返回 False（不 sys.exit）。
    Gateway 自动重启窗口内常出现 Peer closed / Socket disconnect，调用方应重试。
    """
    global IB_CONN, IB_CONTRACT, IB_ACCOUNT_ID, _IB_LAST_HEARTBEAT_TS

    mode = "Paper" if IB_PORT in (4002, 7497) else ("Live" if IB_PORT in (4001, 7496) else f"port={IB_PORT}")
    if verbose:
        print("\n" + "=" * 60)
        print("IBKR Gateway 连接检查")
        print("=" * 60)
        print(f"目标: {IB_HOST}:{IB_PORT} ({mode}), clientId={IB_CLIENT_ID}")
        print("请确认: Gateway 已登录 | Enable Socket API | 关闭 Read-Only | Trusted IP 含 127.0.0.1 | 有 CME/MNQ 期货权限")
        print("建议: Lock and Exit 选「自动重启」而非「自动退出」，避免每日登出后 API 端口关闭")
        print("-" * 60)

    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, readonly=False, timeout=20)
    except Exception as e:
        print(f"❌ 连接 IB Gateway 失败: {e}")
        if verbose:
            print("提示: Gateway 若正在自动重启，请等待几分钟；脚本会自动重试。")
        _safe_ib_disconnect(ib)
        return False

    if not ib.isConnected():
        print("❌ 未连接到 IB Gateway")
        _safe_ib_disconnect(ib)
        return False

    print("✅ 已连接 IB Gateway")
    try:
        print(f"  服务器版本: {ib.client.serverVersion()}")
    except Exception:
        pass

    try:
        # Paper / 无实时行情：仅在显式启用 IB 行情时切换延迟数据类型
        if IB_USE_MARKET_DATA:
            try:
                ib.reqMarketDataType(3)  # 3=Delayed
            except Exception:
                pass

        try:
            accounts = ib.managedAccounts()
        except Exception as e:
            print(f"❌ 读取 managedAccounts 失败: {e}")
            _safe_ib_disconnect(ib)
            return False

        if not accounts:
            print("❌ managedAccounts 为空，请检查登录状态（Gateway 重启后需重新登录完成）")
            _safe_ib_disconnect(ib)
            return False

        IB_ACCOUNT_ID = IB_ACCOUNT if IB_ACCOUNT in accounts else accounts[0]
        if IB_ACCOUNT and IB_ACCOUNT not in accounts:
            print(f"⚠️ IB_ACCOUNT={IB_ACCOUNT} 不在账户列表 {accounts}，改用 {IB_ACCOUNT_ID}")
        print(f"交易账户: {IB_ACCOUNT_ID} (可用: {accounts})")

        # 先 qualify 合约；账户订阅放后面（重启窗口里 updates 易把连接打掉）
        try:
            contract = resolve_mnq_contract(ib)
        except Exception as e:
            print(f"❌ qualifyContracts 异常（连接可能已被 Gateway 重启断开）: {e}")
            _safe_ib_disconnect(ib)
            return False

        if contract is None:
            print(f"❌ qualifyContracts 失败: {TRADE_SYMBOL} (FUT/CME/USD)")
            print("可能原因: 无 CME 期货权限、合约月过期、合约代码变更。请在 TWS/Gateway 确认可交易 MNQ。")
            if MNQ_EXPIRY:
                print(f"当前 MNQ_EXPIRY={MNQ_EXPIRY}，可改为空字符串自动选月，或更新为下一季 YYYYMM。")
            _safe_ib_disconnect(ib)
            return False

        if not ib.isConnected():
            print("❌ qualify 后连接已断开（Gateway 可能正在重启）")
            _safe_ib_disconnect(ib)
            return False

        IB_CONTRACT = contract
        local = IB_CONTRACT.localSymbol or IB_CONTRACT.symbol
        expiry = IB_CONTRACT.lastTradeDateOrContractMonth or MNQ_EXPIRY or '?'
        print(f"✅ 合约已确认: {local} expiry={expiry} conId={IB_CONTRACT.conId} "
              f"exchange={IB_CONTRACT.exchange} multiplier={IB_CONTRACT.multiplier}")

        # 底层订阅；勿用阻塞的 ib.reqAccountUpdates()（等 accountDownloadEnd）
        try:
            _ib_subscribe_account_updates(ib, IB_ACCOUNT_ID or '', wait_sec=1.5)
        except Exception as e:
            print(f"⚠️ reqAccountUpdates 失败: {e}（可继续；稍后刷新账户）")
            if not ib.isConnected():
                print("❌ 账户刷新后连接已断开，本次连接作废，稍后重试")
                _safe_ib_disconnect(ib)
                return False

        if verbose and IB_USE_MARKET_DATA:
            try:
                ticker = ib.reqMktData(IB_CONTRACT, '', False, False)
                ib.sleep(2)
                last = ticker.marketPrice()
                if last != last or last <= 0:
                    last = ticker.last or ticker.close or ticker.bid or ticker.ask
                if last and last == last and last > 0:
                    mnq_px = float(last)
                    notional_1 = mnq_px * MNQ_POINT_VALUE
                    print(f"MNQ 参考价: {mnq_px:.2f}")
                    print(f"  1 张名义约 ${notional_1:,.0f}（点值 ${MNQ_POINT_VALUE:g}/点）；保证金以 Gateway 为准")
                else:
                    print("⚠️ 暂未拿到 MNQ 有效报价（可继续；开仓用日内保证金估算）")
                ib.cancelMktData(IB_CONTRACT)
            except Exception as e:
                print(f"⚠️ 获取报价异常: {e}")
        elif verbose:
            print("IB 行情订阅: 已关闭（信号用 Longport；仓位按日内保证金估算后市价下单）")

        if verbose:
            try:
                if ib.isConnected():
                    summary = list(ib.accountValues(IB_ACCOUNT_ID or ''))
                    if not summary:
                        summary = _ib_cached_account_summary(ib, IB_ACCOUNT_ID or '')
                    interesting = {
                        'NetLiquidation', 'TotalCashValue', 'BuyingPower',
                        'AvailableFunds', 'GrossPositionValue', 'InitMarginReq'
                    }
                    print(f"账户摘要 ({IB_ACCOUNT_ID}):")
                    shown = 0
                    for item in summary:
                        if item.tag in interesting and (not item.currency or item.currency == 'USD'):
                            print(f"  {item.tag}: {item.value} {item.currency}")
                            shown += 1
                    if shown == 0:
                        for item in summary[:8]:
                            print(f"  {item.tag}: {item.value} {item.currency}")
                    if shown == 0 and not summary:
                        print("  （暂未收到账户字段，稍后由 apply_ib_account_capital 重试）")
            except Exception as e:
                print(f"⚠️ 读取账户摘要失败: {e}")

            print("佣金口径仅供参考: IB≈$0.25/张 + CME 等，单边常约 $0.6–1（以账户报表为准）")
            print("=" * 60 + "\n")

        if not ib.isConnected():
            print("❌ 初始化结束前连接已断开，本次连接作废")
            _safe_ib_disconnect(ib)
            return False

        IB_CONN = ib
        _IB_LAST_HEARTBEAT_TS = time_module.time()
        return True

    except Exception as e:
        print(f"❌ 连接后初始化失败: {e}（Gateway 重启/断线时常见，将自动重试）")
        _safe_ib_disconnect(ib)
        return False


def ib_connect(exit_on_fail=False):
    """
    启动时连接 IB Gateway。
    默认无限重试（Gateway 每日自动重启窗口可能持续数分钟），不因短暂拒连而退出进程。
    仅当 exit_on_fail=True 时，第一次失败就退出（一般不要用）。
    """
    attempt = 0
    while True:
        attempt += 1
        verbose = (attempt == 1 or attempt % 5 == 0)
        if attempt > 1:
            ts = get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{ts}] 启动连接重试 #{attempt}："
                  f"若 Gateway 正在「自动重启」，通常需等待 2–10 分钟，脚本会一直等，不会退出")
        if _ib_try_connect(verbose=verbose):
            if attempt > 1:
                ts = get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')
                print(f"[{ts}] 启动连接成功（第 {attempt} 次尝试）")
            return IB_CONN
        if exit_on_fail:
            print("启动连接失败，进程退出。请先确认 IB Gateway 已登录且 API 端口可用。")
            sys.exit(1)
        time_module.sleep(IB_RECONNECT_INTERVAL_SEC)


def ib_disconnect():
    global IB_CONN, _ACCOUNT_SUMMARY_SUBSCRIBED
    if IB_CONN is not None:
        try:
            if IB_ACCOUNT_ID:
                # 底层 client 才有 subscribe 开关；高层 API 只有 account 参数
                IB_CONN.client.reqAccountUpdates(False, IB_ACCOUNT_ID)
        except Exception:
            pass
        try:
            if IB_CONN.isConnected():
                IB_CONN.disconnect()
        except Exception:
            pass
        IB_CONN = None
    _ACCOUNT_SUMMARY_SUBSCRIBED = False


def refresh_ib_account_data(wait_sec=0.8):
    """
    刷新账户资金字段。只用 accountUpdates / accountValues，
    不再反复 reqAccountSummary（会触发 Error 322）。
    """
    if IB_CONN is None or not IB_CONN.isConnected():
        return False
    try:
        return _ib_subscribe_account_updates(IB_CONN, IB_ACCOUNT_ID or '', wait_sec=wait_sec)
    except Exception as e:
        ts = get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts}] 刷新账户数据失败: {e}")
        return False


def ib_heartbeat(force=False):
    """
    轻量探活（reqCurrentTime）。失败则清理本地连接态并返回 False。
    用于发现「isConnected 仍为 True 但 socket 已死」的僵连接。
    """
    global _IB_LAST_HEARTBEAT_TS
    now_ts = time_module.time()
    if not force and (now_ts - _IB_LAST_HEARTBEAT_TS) < IB_HEARTBEAT_INTERVAL_SEC:
        return IB_CONN is not None and IB_CONN.isConnected()

    if IB_CONN is None or not IB_CONN.isConnected():
        return False

    try:
        IB_CONN.reqCurrentTime()
        IB_CONN.sleep(0.3)
        _IB_LAST_HEARTBEAT_TS = now_ts
        return True
    except Exception as e:
        ts = get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts}] IB 心跳失败: {e}，标记断线")
        ib_disconnect()
        return False


def ib_ensure_connected(max_attempts=None, interval_sec=None):
    """
    确保已连接；断线则自动重连。
    默认无限重试（IB_RECONNECT_MAX_ATTEMPTS=0），不因 Gateway 短暂不可用而退出进程。
    Gateway Auto Restart 期间会短暂拒连，重试即可恢复。
    """
    if max_attempts is None:
        max_attempts = IB_RECONNECT_MAX_ATTEMPTS
    if interval_sec is None:
        interval_sec = IB_RECONNECT_INTERVAL_SEC

    if ib_heartbeat(force=False):
        return True
    if IB_CONN is not None and IB_CONN.isConnected():
        if ib_heartbeat(force=True):
            return True

    attempt = 0
    while True:
        attempt += 1
        ts = get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts}] IB 断线，尝试重连 (#{attempt}"
              f"{'' if max_attempts <= 0 else f'/{max_attempts}'})...")
        ib_disconnect()
        verbose = (attempt == 1 or attempt % 10 == 0)
        try:
            if _ib_try_connect(verbose=verbose):
                ts_ok = get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')
                print(f"[{ts_ok}] IB 重连成功")
                return True
        except Exception as e:
            print(f"[{ts}] 重连异常: {e}")

        if max_attempts > 0 and attempt >= max_attempts:
            print(f"[{ts}] IB 重连失败，已达上限 {max_attempts} 次")
            return False

        print(f"[{ts}] 重连失败，{interval_sec}s 后重试"
              f"（请确认 Gateway 已登录；Lock and Exit 建议用「自动重启」）...")
        time_module.sleep(interval_sec)


def sleep_with_ib_keepalive(total_seconds, chunk_sec=None):
    """长等待分段 sleep，期间做心跳；断线则阻塞重连，避免周末/夜间 Gateway 重启后僵死。"""
    if chunk_sec is None:
        chunk_sec = IB_KEEPALIVE_SLEEP_CHUNK_SEC
    if total_seconds <= 0:
        return
    end_ts = time_module.time() + total_seconds
    while True:
        remaining = end_ts - time_module.time()
        if remaining <= 0:
            break
        time_module.sleep(min(chunk_sec, remaining))
        if not ib_heartbeat(force=True):
            ib_ensure_connected()


def get_mnq_price():
    """可选：从 IB 取 MNQ 价。默认关闭行情订阅时直接返回 None（用 QQQ 粗估即可）。"""
    if not IB_USE_MARKET_DATA:
        return None
    if IB_CONN is None or IB_CONTRACT is None or not IB_CONN.isConnected():
        return None
    try:
        try:
            IB_CONN.reqMarketDataType(3)
        except Exception:
            pass
        ticker = IB_CONN.reqMktData(IB_CONTRACT, '', False, False)
        IB_CONN.sleep(1.5)
        last = ticker.marketPrice()
        if last != last or last <= 0:
            last = ticker.last or ticker.close or ticker.bid or ticker.ask
        try:
            IB_CONN.cancelMktData(IB_CONTRACT)
        except Exception:
            pass
        if last and last == last and last > 0:
            return float(last)
    except Exception as e:
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 获取 MNQ 价格失败: {e}")
    return None


def get_ib_account_tag(tag, prefer_usd=True):
    """读取账户字段（优先 accountValues，其次 accountSummary 缓存）。"""
    if IB_CONN is None or not IB_CONN.isConnected():
        return 0.0

    def _parse(items):
        best = 0.0
        for item in items:
            item_tag = getattr(item, 'tag', None)
            if item_tag != tag:
                continue
            cur = getattr(item, 'currency', '') or ''
            if prefer_usd and cur and cur != 'USD':
                continue
            try:
                val = float(item.value)
            except (TypeError, ValueError):
                continue
            if abs(val) > abs(best):
                best = val
        return best

    try:
        v = _parse(IB_CONN.accountValues(IB_ACCOUNT_ID or ''))
        if v != 0:
            return v
    except Exception:
        pass
    try:
        v = _parse(_ib_cached_account_summary(IB_CONN, IB_ACCOUNT_ID or ''))
        if v != 0:
            return v
    except Exception:
        pass
    return 0.0


def get_ib_buying_power():
    """优先 ExcessLiquidity / AvailableFunds；再试 BuyingPower；最后回退净值。"""
    for tag in ('ExcessLiquidity', 'AvailableFunds', 'FullAvailableFunds', 'BuyingPower'):
        v = get_ib_account_tag(tag)
        if v > 0:
            return v
    nlv = get_ib_account_tag('NetLiquidation')
    return nlv if nlv > 0 else 0.0


def _parse_whatif_margin(state):
    """从 whatIf OrderState 提取初始保证金（优先 Change，避免 After 含其它仓位）。"""
    if state is None:
        return None
    for attr in ('initMarginChange', 'fullInitMarginChange'):
        raw = getattr(state, attr, None)
        if raw is None or raw == '':
            continue
        try:
            val = abs(float(raw))
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    for attr in ('initMarginAfter', 'fullInitMarginAfter'):
        raw = getattr(state, attr, None)
        if raw is None or raw == '':
            continue
        try:
            val = abs(float(raw))
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    return None


def get_mnq_init_margin_per_contract(action='BUY'):
    """可选 whatIf 估 1 张保证金；默认关闭（不依赖 IB 行情）。"""
    if not IB_USE_WHATIF_MARGIN:
        return None
    if IB_CONN is None or IB_CONTRACT is None or not IB_CONN.isConnected():
        return None
    try:
        order = _ib_market_order(action.upper(), 1)
        state = IB_CONN.whatIfOrder(IB_CONTRACT, order)
        margin = _parse_whatif_margin(state)
        if margin and margin > 0:
            return margin
    except Exception as e:
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] whatIf 保证金估算失败: {e}")
    return None


def whatif_order_init_margin(action, quantity):
    """可选：对指定张数 whatIf。默认关闭。"""
    if not IB_USE_WHATIF_MARGIN:
        return None
    if IB_CONN is None or IB_CONTRACT is None or not IB_CONN.isConnected():
        return None
    quantity = int(abs(quantity))
    if quantity < 1:
        return None
    try:
        order = _ib_market_order(action.upper(), quantity)
        state = IB_CONN.whatIfOrder(IB_CONTRACT, order)
        for attr in ('initMarginAfter', 'fullInitMarginAfter', 'initMarginChange', 'fullInitMarginChange'):
            raw = getattr(state, attr, None)
            if raw is None or raw == '':
                continue
            try:
                val = abs(float(raw))
            except (TypeError, ValueError):
                continue
            if val > 0:
                return val
    except Exception as e:
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] whatIf({quantity}) 失败: {e}")
    return None


def estimate_mnq_price_from_qqq(qqq_px):
    """无 MNQ 报价时用 QQQ×比值粗估点位。"""
    if qqq_px is None or qqq_px <= 0:
        return None
    return float(qqq_px) * NQ_QQQ_RATIO


def _fallback_margin_per_contract(mnq_px):
    """不依赖 IB 行情/whatIf：按 IBKR 日内初始保证金比例估单张保证金。"""
    by_notional = 0.0
    if mnq_px and mnq_px > 0:
        by_notional = float(mnq_px) * MNQ_POINT_VALUE * MNQ_INTRADAY_IM_PCT
    return max(MNQ_INIT_MARGIN_FLOOR_USD, by_notional)


def calc_ib_order_quantity(mnq_px=None, qqq_px=None, side='Buy'):
    """
    按净值预算满仓：floor(NLV × 缓冲 / 单张日内初始保证金)。
    默认不订 IB 行情、不做 whatIf；市价单直接下。
    """
    ts = get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')
    if not ib_ensure_connected() or IB_CONTRACT is None:
        print(f"[{ts}] 无法计算手数：IB 未连接")
        return 0

    refresh_ib_account_data(wait_sec=0.8)

    available = get_ib_buying_power()
    nlv = get_ib_account_tag('NetLiquidation')
    # IB 大宗商品规则：NLV 必须大于总初始保证金；预算按净值封顶
    if nlv > 0:
        if available <= 0 or available > nlv:
            if available > nlv:
                print(f"[{ts}] 可用资金 ${available:,.0f} > 净值 ${nlv:,.0f}，按净值作为预算基数")
            available = nlv
    usable = available * MARGIN_USAGE_PCT
    action = 'BUY' if side == 'Buy' else 'SELL'

    if mnq_px is None or mnq_px <= 0:
        mnq_px = get_mnq_price()
    if (mnq_px is None or mnq_px <= 0) and qqq_px:
        mnq_px = estimate_mnq_price_from_qqq(qqq_px)

    margin_1 = get_mnq_init_margin_per_contract(action=action)
    if margin_1 and margin_1 > 0:
        margin_source = 'whatIf×1'
    else:
        margin_1 = _fallback_margin_per_contract(mnq_px)
        margin_source = 'intraday'
        print(f"[{ts}] 仓位估算用日内保证金 ≈ ${margin_1:,.0f}/张"
              f"（名义×{MNQ_INTRADAY_IM_PCT:.2%}，下限 ${MNQ_INIT_MARGIN_FLOOR_USD:,.0f}；不订 IB 行情）")

    if usable <= 0:
        print(f"[{ts}] 无法计算手数：可用保证金/购买力为 0")
        return 0

    qty = int(floor(usable / margin_1))
    if MAX_MNQ_CONTRACTS and MAX_MNQ_CONTRACTS > 0:
        qty = min(qty, int(MAX_MNQ_CONTRACTS))

    # 仅当显式开启 whatIf 时做整单二次校验
    if qty >= 1 and IB_USE_WHATIF_MARGIN:
        full_margin = whatif_order_init_margin(action, qty)
        budget_cap = nlv * MARGIN_USAGE_PCT if nlv > 0 else usable
        if full_margin and full_margin > 0:
            if full_margin > budget_cap:
                new_qty = int(floor(qty * budget_cap / full_margin))
                print(f"[{ts}] 整单 whatIf 总初始保证金 ${full_margin:,.0f} > 预算 ${budget_cap:,.0f}，"
                      f"张数 {qty} → {new_qty}")
                qty = max(0, new_qty)
            else:
                print(f"[{ts}] 整单 whatIf 通过: {qty} 张总初始保证金≈${full_margin:,.0f}")

    notional = (qty * mnq_px * MNQ_POINT_VALUE) if (mnq_px and mnq_px > 0 and qty > 0) else 0.0
    eff_lev = (notional / INITIAL_CAPITAL) if (INITIAL_CAPITAL and INITIAL_CAPITAL > 0 and notional > 0) else 0.0

    print(f"[{ts}] 满仓计算: 预算=${usable:,.0f} (基数${available:,.0f}×{MARGIN_USAGE_PCT:g}) / "
          f"单张≈${margin_1:,.0f} ({margin_source}) = {qty} 张 MNQ"
          + (f" (名义约 ${notional:,.0f} ≈ {eff_lev:.1f}x 净值)" if notional > 0 else ""))
    if qty < 1:
        print(f"[{ts}] ⚠️ 可用保证金不足 1 张（约需 ${margin_1:,.0f}+），跳过开仓")
        return 0
    return qty


def _ib_market_order(action, quantity):
    order = MarketOrder(action, quantity)
    order.tif = IB_TIF
    if IB_ACCOUNT_ID:
        order.account = IB_ACCOUNT_ID
    return order


def _position_moved_as_expected(pos_before, pos_after, action):
    """仓位是否朝 BUY/SELL 方向变动至少 1 张（含部分成交）。"""
    try:
        delta = float(pos_after) - float(pos_before)
    except (TypeError, ValueError):
        return False
    if action == 'BUY':
        return delta >= 1
    if action == 'SELL':
        return delta <= -1
    return False


def get_ib_position_qty():
    """返回 MNQ 净仓位（多头>0，空头<0）。"""
    if IB_CONN is None or IB_CONTRACT is None or not IB_CONN.isConnected():
        return 0
    try:
        IB_CONN.reqPositions()
        IB_CONN.sleep(0.5)
        total = 0.0
        for p in IB_CONN.positions():
            if IB_ACCOUNT_ID and p.account and p.account != IB_ACCOUNT_ID:
                continue
            c = p.contract
            same_con = c.conId and IB_CONTRACT.conId and c.conId == IB_CONTRACT.conId
            same_mnq = (
                c.secType == 'FUT'
                and c.symbol == TRADE_SYMBOL
                and (
                    not IB_CONTRACT.localSymbol
                    or c.localSymbol == IB_CONTRACT.localSymbol
                    or (c.lastTradeDateOrContractMonth or '')[:6]
                    == (IB_CONTRACT.lastTradeDateOrContractMonth or '')[:6]
                )
            )
            if same_con or same_mnq:
                total += float(p.position)
        return int(total) if total == int(total) else total
    except Exception as e:
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 读取 IB 仓位失败: {e}")
        # socket 已死但 isConnected 可能仍为 True，强制清理以便后续重连
        err = str(e).lower()
        if 'socket' in err or 'disconnect' in err or '10053' in err or '10054' in err:
            ib_disconnect()
        return 0


def _trade_fill_avg(trade):
    """从 Trade 提取成交均价；无成交返回 None。"""
    try:
        if trade.orderStatus and trade.orderStatus.avgFillPrice:
            px = float(trade.orderStatus.avgFillPrice)
            if px > 0:
                return px
        fills = getattr(trade, 'fills', None) or []
        if fills:
            notional = sum(float(f.execution.shares) * float(f.execution.price) for f in fills)
            shares = sum(float(f.execution.shares) for f in fills)
            if shares > 0:
                return notional / shares
    except Exception:
        pass
    return None


def _trade_commission(trade):
    try:
        total = 0.0
        for f in (getattr(trade, 'fills', None) or []):
            if f.commissionReport and f.commissionReport.commission is not None:
                total += float(f.commissionReport.commission)
        return total
    except Exception:
        return None


def _trade_error_codes(trade):
    """Trade.log 里所有 IB 错误码（10349 常和 201 一起出现）。"""
    codes = []
    try:
        for entry in getattr(trade, 'log', None) or []:
            code = int(getattr(entry, 'errorCode', 0) or 0)
            if code > 0:
                codes.append(code)
    except Exception:
        pass
    return codes


def _trade_error_code(trade):
    """优先返回 Error 201（保证金不足）；忽略 10349 等提示码。"""
    codes = _trade_error_codes(trade)
    if 201 in codes:
        return 201
    for code in codes:
        if code not in IB_INFORMATIONAL_ERROR_CODES:
            return code
    return 0


def place_ib_market(action, quantity):
    """
    市价单。action: 'BUY' | 'SELL'；quantity > 0（张）。
    以仓位变化为准：Error 10349 可能把订单标成 Cancelled，但实际已成交。
    """
    ts = get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')
    if not ib_ensure_connected() or IB_CONTRACT is None:
        return {'ok': False, 'error': 'not connected'}
    quantity = int(abs(quantity))
    if quantity < 1:
        return {'ok': False, 'error': 'quantity < 1'}

    action = action.upper()
    if action not in ('BUY', 'SELL'):
        return {'ok': False, 'error': f'invalid action {action}'}

    local = (IB_CONTRACT.localSymbol or TRADE_SYMBOL) if IB_CONTRACT else TRADE_SYMBOL
    try:
        pos_before = get_ib_position_qty()
        order = _ib_market_order(action, quantity)
        trade = IB_CONN.placeOrder(IB_CONTRACT, order)
        print(f"[{ts}] 已提交市价单: {action} {quantity} {local} tif={order.tif} orderId={trade.order.orderId}")

        status = ''
        for _ in range(40):
            IB_CONN.sleep(0.25)
            status = trade.orderStatus.status
            if status in ('Filled', 'Cancelled', 'ApiCancelled', 'Inactive'):
                break

        codes = _trade_error_codes(trade)
        if status != 'Filled':
            extra = 8 if (status in ('Cancelled', 'ApiCancelled', 'Inactive', 'Submitted', 'PreSubmitted', 'PendingSubmit') or 10349 in codes) else 2
            for _ in range(extra):
                if trade.orderStatus.status == 'Filled':
                    break
                pos_mid = get_ib_position_qty()
                if _position_moved_as_expected(pos_before, pos_mid, action):
                    break
                IB_CONN.sleep(1.0)

        avg_fill = _trade_fill_avg(trade)
        commission = _trade_commission(trade)
        status = trade.orderStatus.status
        err_code = _trade_error_code(trade)
        pos = get_ib_position_qty()
        moved = _position_moved_as_expected(pos_before, pos, action)
        print(f"[{ts}] 订单状态={status}, avgFill={avg_fill}, commission={commission}, "
              f"errorCode={err_code or '-'}, 仓位 {pos_before}→{pos}")

        if status == 'Filled' or moved:
            if status != 'Filled':
                print(f"[{ts}] 订单状态非 Filled（常为 10349/预设改 TIF），但仓位已变，按成交处理")
            return {
                'ok': True,
                'orderId': trade.order.orderId,
                'avg_fill': avg_fill,
                'commission': commission,
                'position': pos,
                'error_code': 0,
            }

        info_only = bool(codes) and set(codes).issubset(IB_INFORMATIONAL_ERROR_CODES)
        if info_only and err_code == 10349:
            print(f"[{ts}] Error 10349 且仓位未变，视为未成交")
        return {
            'ok': False,
            'orderId': trade.order.orderId,
            'avg_fill': avg_fill,
            'commission': commission,
            'position': pos,
            'error_code': err_code,
            'error': f'status={status}' + (f' errorCode={err_code}' if err_code else ''),
        }
    except Exception as e:
        print(f"[{ts}] 下单异常: {e}")
        return {'ok': False, 'error': str(e)}


def close_ib_position():
    """按 ib.positions() 核对后反向市价平仓；无仓位则成功返回。以平后仓位=0 为准。"""
    ts = get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')
    if not ib_ensure_connected():
        return {'ok': False, 'error': 'not connected'}

    pos = get_ib_position_qty()
    if pos == 0:
        print(f"[{ts}] 平仓跳过: IB 仓位已为 0")
        return {'ok': True, 'orderId': None, 'avg_fill': None, 'position': 0, 'skipped': True}

    action = 'SELL' if pos > 0 else 'BUY'
    qty = abs(int(pos)) if float(pos) == int(pos) else abs(pos)
    print(f"[{ts}] 平仓: 当前仓位={pos} → {action} {qty}")
    result = place_ib_market(action, qty)
    pos_after = get_ib_position_qty()
    if pos_after == 0:
        if not result:
            result = {}
        result['ok'] = True
        result['position'] = 0
        return result
    print(f"[{ts}] 平仓后仍有仓位 {pos_after}，未完全平掉")
    if not result:
        result = {}
    result['ok'] = False
    result['position'] = pos_after
    result['error'] = result.get('error') or f'residual position {pos_after}'
    return result


def get_us_eastern_time():
    if DEBUG_MODE and 'DEBUG_TIME' in globals() and DEBUG_TIME:
        # 如果处于调试模式且指定了时间，返回指定的时间
        try:
            dt = datetime.strptime(DEBUG_TIME, "%Y-%m-%d %H:%M:%S")
            eastern = pytz.timezone('US/Eastern')
            return eastern.localize(dt)
        except ValueError:
            print(f"错误的调试时间格式: {DEBUG_TIME}，应为 'YYYY-MM-DD HH:MM:SS'")
    
    # 正常模式或调试时间格式错误时返回当前时间
    eastern = pytz.timezone('US/Eastern')
    return datetime.now(eastern)

def get_account_balance():
    """从 IB 取 USD NetLiquidation；失败再试 AvailableFunds / TotalCashValue。"""
    if IB_CONN is None or not IB_CONN.isConnected():
        return 0.0

    def _from_summary():
        try:
            for item in _ib_cached_account_summary(IB_CONN, IB_ACCOUNT_ID or ''):
                if item.tag == 'NetLiquidation' and (not item.currency or item.currency == 'USD'):
                    return float(item.value)
        except Exception:
            pass
        return 0.0

    def _from_values(tag):
        try:
            for v in IB_CONN.accountValues(IB_ACCOUNT_ID or ''):
                if v.tag == tag and (not v.currency or v.currency == 'USD'):
                    return float(v.value)
        except Exception:
            pass
        return 0.0

    for tag in ('NetLiquidation', 'EquityWithLoanValue', 'AvailableFunds', 'TotalCashValue'):
        bal = _from_values(tag)
        if bal > 0:
            return bal
    return _from_summary()


def get_current_positions():
    """返回 {TRADE_SYMBOL: {quantity, cost_price}}。"""
    qty = get_ib_position_qty()
    return {TRADE_SYMBOL: {"quantity": qty, "cost_price": 0}}


def calculate_pnl(entry_price, exit_price, direction, quantity=None):
    """
    按实际 MNQ 敞口估算盈亏（信号价用 QQQ）。
    敞口 ≈ 张数 × QQQ入场价 × NQ_QQQ_RATIO × $2；接受 QQQ↔MNQ 基差。
    quantity 未传时无法可靠估算（满仓张数每次可变），返回 0。
    """
    if entry_price <= 0:
        return 0.0, 0.0

    price_change_pct = (exit_price - entry_price) / entry_price
    if quantity is None or abs(quantity) <= 0:
        return 0.0, 0.0

    qty = abs(quantity)
    exposure = qty * entry_price * NQ_QQQ_RATIO * MNQ_POINT_VALUE
    pnl = exposure * price_change_pct * direction
    pnl_pct = (pnl / INITIAL_CAPITAL * 100) if INITIAL_CAPITAL else 0.0
    return pnl, pnl_pct

def get_historical_data(symbol, days_back=None):
    if days_back is None:
        days_back = history_days_back(LOOKBACK_DAYS)

    now_et = get_us_eastern_time()
    current_date = now_et.date()
    start_date = current_date - timedelta(days=days_back)

    if not ensure_market_data_service_available():
        return pd.DataFrame()

    if LOG_VERBOSE:
        print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 从本地行情缓存读取历史数据: {symbol}")

    try:
        conn = sqlite3.connect(MARKET_DATA_DB_PATH)
        query = """
        SELECT datetime_et, open, high, low, close, volume, turnover
        FROM candles
        WHERE symbol = ? AND date >= ? AND date <= ?
        ORDER BY datetime_et ASC
        """
        rows = conn.execute(query, (symbol, start_date.isoformat(), current_date.isoformat())).fetchall()
        conn.close()
    except Exception as e:
        print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 读取行情缓存失败: {str(e)}")
        return pd.DataFrame()

    eastern = pytz.timezone('US/Eastern')
    data = []
    for row in rows:
        dt = eastern.localize(datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S"))
        data.append({
            "Close": float(row[4]),
            "Open": float(row[1]),
            "High": float(row[2]),
            "Low": float(row[3]),
            "Volume": float(row[5]),
            "Turnover": float(row[6]),
            "DateTime": dt,
        })

    df = pd.DataFrame(data)
    if df.empty:
        return df

    df["Date"] = df["DateTime"].dt.date
    df["Time"] = df["DateTime"].dt.strftime('%H:%M')

    if symbol.endswith(".US"):
        df = df[df["Time"].between("09:30", "16:00")]

    df = df.drop_duplicates(subset=['Date', 'Time'])
    df = df[df["Date"] <= current_date]
    weekday_mask = df["Date"].apply(lambda x: x.weekday() < 5 if isinstance(x, date_type) else True)
    df = df[weekday_mask]

    if LOG_VERBOSE and not df.empty:
        unique_dates = sorted(df["Date"].unique())
        latest_row = df.sort_values(by=["Date", "Time"]).iloc[-1]
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 缓存数据日期: {unique_dates}, 最新K线: {latest_row['Date']} {latest_row['Time']}")

    return df

def get_quote(symbol):
    if not ensure_market_data_service_available():
        return {}

    try:
        conn = sqlite3.connect(MARKET_DATA_DB_PATH)
        row = conn.execute("""
        SELECT symbol, last_done, open, high, low, volume, turnover, quote_timestamp
        FROM quotes
        WHERE symbol = ?
        """, (symbol,)).fetchone()
        conn.close()
    except Exception as e:
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 读取报价缓存失败: {str(e)}")
        return {}

    if row is None:
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 报价缓存为空: {symbol}")
        return {}

    return {
        "symbol": row[0],
        "last_done": row[1],
        "open": row[2],
        "high": row[3],
        "low": row[4],
        "volume": row[5],
        "turnover": row[6],
        "timestamp": row[7],
    }

def calculate_vwap(df):
    # 创建一个结果DataFrame的副本
    result_df = df.copy()
    
    # 按照日期分组
    for date in result_df['Date'].unique():
        # 获取当日数据
        day_data = result_df[result_df['Date'] == date]
        
        # 按时间排序确保正确累计
        day_data = day_data.sort_values('Time')
        
        # 计算累计成交量和成交额
        cumulative_volume = day_data['Volume'].cumsum()
        cumulative_turnover = day_data['Turnover'].cumsum()
        
        # 计算VWAP: 累计成交额 / 累计成交量
        vwap = cumulative_turnover / cumulative_volume
        # 处理成交量为0的情况
        vwap = vwap.fillna(day_data['Close'])
        
        # 更新结果DataFrame中的对应行
        result_df.loc[result_df['Date'] == date, 'VWAP'] = vwap.values
    
    return result_df['VWAP']

def calculate_noise_area(df, lookback_days=LOOKBACK_DAYS, K1=1, K2=1):
    # 创建数据副本
    df_copy = df.copy()
    
    # 获取唯一日期并排序
    unique_dates = sorted(df_copy["Date"].unique())
    now_et = get_us_eastern_time()
    current_date = now_et.date()
    
    # 过滤未来日期
    if unique_dates and isinstance(unique_dates[0], date_type):
        unique_dates = [d for d in unique_dates if d <= current_date]
        df_copy = df_copy[df_copy["Date"].isin(unique_dates)]
    
    # 过滤周末数据：只保留周一到周五的数据
    weekday_dates = []
    for d in unique_dates:
        if isinstance(d, date_type):
            # weekday(): 0=Monday, 1=Tuesday, ..., 6=Sunday
            if d.weekday() < 5:  # 0-4 表示周一到周五
                weekday_dates.append(d)
        else:
            weekday_dates.append(d)  # 如果不是date类型，保留原样
    
    unique_dates = weekday_dates
    df_copy = df_copy[df_copy["Date"].isin(unique_dates)]
    
    if LOG_VERBOSE:
        print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 过滤周末后的日期数量: {len(unique_dates)}")
        if len(unique_dates) > 0:
            print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 最近的交易日: {unique_dates[-5:]}")
    
    # 假设最后一天是当前交易日，直接排除
    if len(unique_dates) > 1:
        target_date = unique_dates[-1]  # 保存目标日期（当前交易日）
        history_dates = unique_dates[:-1]  # 排除最后一天
        
        # 从剩余日期中选择最近的lookback_days天
        history_dates = history_dates[-lookback_days:] if len(history_dates) >= lookback_days else history_dates
    else:
        print(f"错误: 数据中只有一天或没有数据，无法计算噪声空间")
        sys.exit(1)
    
    # 检查数据是否足够
    if len(history_dates) < lookback_days:
        print(f"错误: 历史数据不足，至少需要{lookback_days}个交易日，当前只有{len(history_dates)}个交易日")
        sys.exit(1)
    
    # 为历史日期计算当日开盘价和相对变动率
    history_df = df_copy[df_copy["Date"].isin(history_dates)].copy()
    
    # 为每个历史日期计算当日开盘价
    day_opens = {}
    for date in history_dates:
        day_data = history_df[history_df["Date"] == date]
        if day_data.empty:
            print(f"错误: {date} 日期数据为空")
            sys.exit(1)
        day_opens[date] = day_data["Open"].iloc[0]
    
    # 为每个时间点计算相对于开盘价的绝对变动率
    history_df["move"] = 0.0
    for date in history_dates:
        day_open = day_opens[date]
        history_df.loc[history_df["Date"] == date, "move"] = abs(history_df.loc[history_df["Date"] == date, "Close"] / day_open - 1)
    
    # 计算每个时间点的sigma (使用历史数据)
    time_sigma = {}
    
    # 获取目标日期的所有时间点
    target_day_data = df[df["Date"] == target_date]
    times = target_day_data["Time"].unique()
    
    # 对每个时间点计算sigma
    for tm in times:
        # 获取历史数据中相同时间点的数据
        historical_moves = []
        for date in history_dates:
            hist_data = history_df[(history_df["Date"] == date) & (history_df["Time"] == tm)]
            if not hist_data.empty:
                historical_moves.append(hist_data["move"].iloc[0])
        
        # 确保有足够的历史数据计算sigma
        if len(historical_moves) == 0:
            continue
        
        # 计算平均变动率作为sigma
        sigma = sum(historical_moves) / len(historical_moves)
        time_sigma[(target_date, tm)] = sigma
    
    # 计算上下边界
    # 获取目标日期的开盘价
    target_day_data = df[df["Date"] == target_date]
    if target_day_data.empty:
        print(f"错误: 目标日期 {target_date} 数据为空")
        sys.exit(1)
    
    # 使用指定时间点的K线数据
    # 获取当日09:30的开盘价
    day_0930_data = target_day_data[target_day_data["Time"] == "09:30"]
    if not day_0930_data.empty:
        day_open = day_0930_data["Open"].iloc[0]
        if LOG_VERBOSE:
            print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 使用09:30开盘价: {day_open}")
    else:
        # 如果没有09:30数据，回退到第一根K线
        day_open = target_day_data["Open"].iloc[0]
        if LOG_VERBOSE:
            print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 09:30数据缺失，使用第一根K线开盘价: {day_open}")
    
    # 获取前一日15:59的收盘价
    if target_date in unique_dates and unique_dates.index(target_date) > 0:
        prev_date = unique_dates[unique_dates.index(target_date) - 1]
        prev_day_data = df[df["Date"] == prev_date]
        if not prev_day_data.empty:
            # 尝试获取15:59的收盘价
            prev_1559_data = prev_day_data[prev_day_data["Time"] == "15:59"]
            if not prev_1559_data.empty:
                prev_close = prev_1559_data["Close"].iloc[0]
                if LOG_VERBOSE:
                    print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 使用前日15:59收盘价: {prev_close}")
            else:
                # 如果没有15:59数据，回退到最后一根K线
                prev_close = prev_day_data["Close"].iloc[-1]
                if LOG_VERBOSE:
                    print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 15:59数据缺失，使用最后一根K线收盘价: {prev_close}")
        else:
            prev_close = None
    else:
        prev_close = None
    
    if prev_close is None:
        return df
    
    # 根据算法计算参考价格
    upper_ref = max(day_open, prev_close)
    lower_ref = min(day_open, prev_close)
    
    # 对目标日期的每个时间点计算上下边界
    # 使用目标日期的数据
    for _, row in target_day_data.iterrows():
        tm = row["Time"]
        sigma = time_sigma.get((target_date, tm))
        
        if sigma is not None:
            # 使用时间点特定的sigma计算上下边界（K1 可按午后规则动态调整，见 k_side_adjust）
            k1_eff = effective_k1_for_time(tm, K1, ENABLE_K_SIDE_ADJUSTMENT)
            upper_bound = upper_ref * (1 + k1_eff * sigma)
            lower_bound = lower_ref * (1 - K2 * sigma)
            
            # 更新df中的边界值
            df.loc[(df["Date"] == target_date) & (df["Time"] == tm), "UpperBound"] = upper_bound
            df.loc[(df["Date"] == target_date) & (df["Time"] == tm), "LowerBound"] = lower_bound
            df.loc[(df["Date"] == target_date) & (df["Time"] == tm), "sigma"] = sigma
    
    return df

def submit_order(symbol, side, quantity, order_type="MO", price=None, outside_rth=None, is_close=False):
    """
    直连 IB Gateway 市价开/平仓。
    - 开仓: side Buy/Sell，quantity 为 MNQ 张数（必须 >= 1）
    - 平仓: is_close=True，按 positions() 核对后反向市价
    成功返回 orderId 字符串，失败返回 None。
    """
    ts = get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')
    if is_close:
        result = close_ib_position()
    else:
        action = "BUY" if side == "Buy" else "SELL"
        if quantity is None or int(quantity) < 1:
            print(f"[{ts}] 开仓拒绝: quantity={quantity}（最小 1 张 MNQ）")
            return None
        qty = int(quantity)
        result = None
        tried = set()
        # 先按 100% 张数下；201 再 90%→85%→70%（70% 约等于隔夜保证金能过的张数）
        for frac in IB_OPEN_RETRY_QTY_FRACS:
            qty_try = int(qty * frac) if frac < 1.0 else qty
            if qty_try < 1 or qty_try in tried:
                continue
            tried.add(qty_try)
            if frac < 1.0:
                print(f"[{ts}] Error 201 保证金不足，开仓张数 {qty} → {qty_try} "
                      f"（{frac:.0%}）后重试")
            result = place_ib_market(action, qty_try)
            if result and result.get('ok'):
                break
            err_code = (result or {}).get('error_code') or 0
            err_text = str((result or {}).get('error', ''))
            is_201 = err_code == 201 or '201' in err_text
            if not is_201:
                break

    if not result or not result.get('ok'):
        err = (result or {}).get('error', 'unknown')
        print(f"[{ts}] IB 下单失败: {err}")
        return None

    oid = result.get('orderId')
    avg = result.get('avg_fill')
    comm = result.get('commission')
    pos = result.get('position')
    print(f"[{ts}] IB 下单成功 orderId={oid} avgFill={avg} commission={comm} pos={pos}")
    return str(oid) if oid is not None else "IB_OK"

def check_exit_conditions(df, position_quantity, current_stop):
    # 获取当前时间点
    now = get_us_eastern_time()
    current_time = now.strftime('%H:%M')
    current_date = now.date()
    
    # 使用前一分钟的完整K线数据
    prev_minute_time = (now - timedelta(minutes=1)).strftime('%H:%M')
    prev_data = df[(df["Date"] == current_date) & (df["Time"] == prev_minute_time)]
    
    # 如果前一分钟没有数据，使用最新数据
    if prev_data.empty:
        # 按日期和时间排序，获取最新的数据
        df_sorted = df.sort_values(by=["Date", "Time"], ascending=True)
        latest = df_sorted.iloc[-1]
    else:
        latest = prev_data.iloc[0]
        
    price = latest["Close"]
    vwap = latest["VWAP"]
    upper = latest["UpperBound"]
    lower = latest["LowerBound"]
    
    # 检查数据是否为空值
    if price is None:
        return False, current_stop
    
    if position_quantity > 0:
        # 检查上边界是否为None
        if upper is None or (USE_VWAP and vwap is None):
            # 如果已有止损，继续使用
            if current_stop is not None:
                new_stop = current_stop
                exit_signal = price < new_stop
                return exit_signal, new_stop
            else:
                return False, current_stop
        else:
            # 直接使用当前时刻的止损水平，不考虑历史止损
            new_stop = max(upper, vwap) if USE_VWAP else upper
            
        exit_signal = price < new_stop
        return exit_signal, new_stop
    elif position_quantity < 0:
        # 检查下边界是否为None
        if lower is None or (USE_VWAP and vwap is None):
            # 如果已有止损，继续使用
            if current_stop is not None:
                new_stop = current_stop
                exit_signal = price > new_stop
                return exit_signal, new_stop
            else:
                return False, current_stop
        else:
            # 直接使用当前时刻的止损水平，不考虑历史止损
            new_stop = min(lower, vwap) if USE_VWAP else lower
            
        exit_signal = price > new_stop
        return exit_signal, new_stop
    return False, None

def daily_loss_monitor_thread(symbol, position_data):
    """
    日内止盈止损监控线程
    每分钟检查一次当前总盈亏（已实现+未实现），一旦超过止盈或止损限制立即设置强制平仓标志
    注意：盈亏按 MNQ 张数名义估算（QQQ 涨跌幅 × 敞口）
    """
    global DAILY_STOP_TRIGGERED, FORCE_CLOSE_POSITION, DAILY_LOSS_MONITOR_ACTIVE
    global DAILY_PNL, PROFIT_TARGET_TRIGGERED
    
    print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] === 日内止盈止损监控线程已启动 ===")
    if MAX_PROFIT_AMOUNT > 0:
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 止盈目标: ${MAX_PROFIT_AMOUNT:.2f}")
    else:
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 止盈: 已禁用")
    if MAX_DAILY_LOSS_AMOUNT > 0:
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 最大允许亏损额: ${MAX_DAILY_LOSS_AMOUNT:.2f}")
    else:
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 日内止损: 已禁用")
    print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 仓位模式: 日内保证金满仓（约 13x；201 则减张）")
    
    while DAILY_LOSS_MONITOR_ACTIVE:
        try:
            now = get_us_eastern_time()
            current_hour = now.hour
            
            # 判断是否在交易时间内（9:30-16:00）
            is_trading_hours = (current_hour >= 10 or (current_hour == 9 and now.minute >= 30)) and current_hour < 16
            
            # 使用锁保护共享变量
            with pnl_lock:
                # 如果已经触发止损或止盈，停止监控
                if DAILY_STOP_TRIGGERED or PROFIT_TARGET_TRIGGERED:
                    break
                
                # 获取持仓信息
                position_quantity = position_data.get('quantity', 0)
                entry_price = position_data.get('entry_price', None)
            
            # 只在交易时间内进行检查和打印
            if is_trading_hours:
                # 计算当前盈亏（分别计算累计和当日）
                unrealized_pnl = 0.0
                
                # 如果有持仓，获取当前价格并计算未实现盈亏
                if position_quantity != 0 and entry_price is not None:
                    try:
                        quote = get_quote(symbol)
                        current_price = float(quote.get("last_done", 0))
                        
                        if current_price > 0:
                            direction = 1 if position_quantity > 0 else -1
                            unrealized_pnl, _ = calculate_pnl(entry_price, current_price, direction, position_quantity)
                    except Exception as e:
                        if LOG_VERBOSE:
                            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] 获取价格失败: {str(e)}")
                
                with pnl_lock:
                    current_total_pnl = TOTAL_PNL + unrealized_pnl
                    current_daily_pnl = DAILY_PNL + unrealized_pnl
                
                # 检查是否触发止盈（基于累计盈亏，且MAX_PROFIT_AMOUNT > 0时才启用）
                if MAX_PROFIT_AMOUNT > 0 and current_total_pnl >= MAX_PROFIT_AMOUNT:
                    print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] !!!!! [监控线程] 检测到达成止盈目标 !!!!!")
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] 累计总盈利: ${current_total_pnl:.2f}")
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] 止盈目标: ${MAX_PROFIT_AMOUNT:.2f}")
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] 超出目标: ${current_total_pnl - MAX_PROFIT_AMOUNT:.2f}")
                    
                    with pnl_lock:
                        FORCE_CLOSE_POSITION = True
                        PROFIT_TARGET_TRIGGERED = True
                    
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] 已设置止盈平仓标志")
                    break
                
                # 检查是否触发日内止损（基于当日盈亏，每日重置，且MAX_DAILY_LOSS_AMOUNT > 0时才启用）
                if MAX_DAILY_LOSS_AMOUNT > 0 and current_daily_pnl < 0 and abs(current_daily_pnl) >= MAX_DAILY_LOSS_AMOUNT:
                    print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] !!!!! [监控线程] 检测到日内亏损超限 !!!!!")
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] 当日亏损: ${current_daily_pnl:.2f}")
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] 日内最大允许亏损: ${-MAX_DAILY_LOSS_AMOUNT:.2f}")
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] 超出金额: ${abs(current_daily_pnl) - MAX_DAILY_LOSS_AMOUNT:.2f}")
                    
                    with pnl_lock:
                        FORCE_CLOSE_POSITION = True
                        DAILY_STOP_TRIGGERED = True
                    
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] 已设置强制平仓标志")
                    break
                else:
                    status_parts = [f"当日盈亏: ${current_daily_pnl:+.2f}", f"累计盈亏: ${current_total_pnl:+.2f}"]
                    
                    if MAX_PROFIT_AMOUNT > 0:
                        profit_remain = MAX_PROFIT_AMOUNT - current_total_pnl
                        status_parts.append(f"距止盈: ${profit_remain:.2f}")
                    
                    if MAX_DAILY_LOSS_AMOUNT > 0:
                        loss_remain = MAX_DAILY_LOSS_AMOUNT + current_daily_pnl
                        status_parts.append(f"距日内止损: ${loss_remain:.2f}")
                    
                    status_parts.append(f"持仓: {position_quantity}")
                    
                    if LOG_VERBOSE:
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] " + " | ".join(status_parts))
            
            # 等待60秒后再次检查
            time_module.sleep(60)
            
        except Exception as e:
            print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] 发生错误: {str(e)}")
            time_module.sleep(60)
    
    print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] === 日内止盈止损监控线程已停止 ===")

def is_trading_day(symbol=None):
    """返回 (是否交易日, 日历是否过期)。日历过期时应短间隔重试。"""
    if not ensure_market_data_service_available():
        return False, True

    now_et = get_us_eastern_time()
    current_date = now_et.date()
    try:
        conn = sqlite3.connect(MARKET_DATA_DB_PATH)
        rows = conn.execute("""
        SELECT key, value FROM service_state
        WHERE key IN ('calendar_date', 'is_trading_day', 'is_half_trading_day', 'prev_trading_day_is_half')
        """).fetchall()
        conn.close()
    except Exception as e:
        print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 读取交易日历缓存失败: {str(e)}")
        return False, True

    state = {key: value for key, value in rows}
    if state.get('calendar_date') != current_date.isoformat():
        print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 交易日历缓存不是今天: {state.get('calendar_date')}")
        return False, True

    if state.get('is_half_trading_day') == '1':
        print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 今日为半交易日，不进行交易")
        return False, False

    if state.get('is_trading_day') != '1':
        print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 今日不是交易日，不进行交易")
        return False, False

    # 上一交易日为半交易日时，其午后分钟数据缺失会污染噪声区间计算（lookback=1 时下午 sigma 完全缺失），跳过当日交易
    if state.get('prev_trading_day_is_half') == '1':
        print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 上一交易日为半交易日，历史数据不完整，不进行交易")
        return False, False

    return True, False

def run_trading_strategy(symbol=SYMBOL, check_interval_minutes=CHECK_INTERVAL_MINUTES,
                        trading_start_time=TRADING_START_TIME, trading_end_time=TRADING_END_TIME,
                        max_positions_per_day=MAX_POSITIONS_PER_DAY, lookback_days=LOOKBACK_DAYS):
    global TOTAL_PNL, DAILY_PNL, LAST_STATS_DATE, DAILY_TRADES, DAILY_STOP_TRIGGERED, PROFIT_TARGET_TRIGGERED
    global MAX_DAILY_LOSS_AMOUNT, DAILY_LOSS_MONITOR_ACTIVE, FORCE_CLOSE_POSITION
    
    now_et = get_us_eastern_time()
    print(f"启动交易策略 - 交易品种: {symbol}")
    print(f"当前美东时间: {now_et.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"交易时间: {trading_start_time[0]:02d}:{trading_start_time[1]:02d} - {trading_end_time[0]:02d}:{trading_end_time[1]:02d}")
    print(f"每日最大开仓次数: {max_positions_per_day}")
    if DEBUG_MODE:
        print(f"调试模式已开启! 使用时间: {now_et.strftime('%Y-%m-%d %H:%M:%S')}")
        if DEBUG_ONCE:
            print("单次运行模式已开启，策略将只运行一次")
    
    # 同步 IB 已有仓位（断线重连后先对齐再决策）
    position_quantity = 0
    entry_price = None
    try:
        ib_pos = get_ib_position_qty()
        if ib_pos != 0:
            position_quantity = int(ib_pos) if float(ib_pos) == int(ib_pos) else ib_pos
            print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 检测到 IB 已有 {TRADE_SYMBOL} 仓位: {position_quantity}，将按策略管理平仓")
            print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 注意: 入场价未知，策略止损按噪声带；盈亏统计可能不完整")
    except Exception as e:
        print(f"同步 IB 仓位失败: {e}")

    current_stop = None
    positions_opened_today = 0
    last_date = None
    outside_rth_setting = OutsideRTH.AnyTime
    
    # 🎯 动态追踪止盈状态变量
    max_profit_price = None         # 持仓期间的最优价格（多头：最高价，空头：最低价）
    trailing_tp_activated = False   # 追踪止盈是否已激活
    trailing_tp_day_stop = False    # 当日是否已因追踪止盈平仓（触发后当日不再开仓）
    last_processed_trigger = None    # 已处理的触发点(date, k_h, k_m)，用于触发窗口内去重，避免空转刷屏
    
    # 持仓数据字典（供监控线程使用）
    position_data = {
        'quantity': 0,
        'entry_price': None
    }
    
    # 监控线程对象
    monitor_thread = None
    
    while True:
        now = get_us_eastern_time()
        current_date = now.date()
        if LOG_VERBOSE:
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S.%f')}] 主循环开始 (精确时间)")
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 时间精度: 秒={now.second}, 微秒={now.microsecond}")

        # Gateway 可能因 Auto Restart / 网络抖动断线；先探活，断则阻塞重连（不退出进程）
        if not ib_heartbeat():
            if not ib_ensure_connected():
                time_module.sleep(IB_RECONNECT_INTERVAL_SEC)
                continue
        
        # 模拟模式下不再重新获取持仓状态，保持本地状态
        # current_positions = get_current_positions()
        # symbol_position = current_positions.get(symbol, {"quantity": 0, "cost_price": 0})
        # position_quantity = symbol_position["quantity"]
        
        # 模拟模式：不需要获取账户余额
        # current_balance = get_account_balance()
        
        # 更新持仓数据供监控线程使用
        with pnl_lock:
            position_data['quantity'] = position_quantity
            position_data['entry_price'] = entry_price
        
        # 检查监控线程是否设置了强制平仓标志
        if FORCE_CLOSE_POSITION and position_quantity != 0:
            print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] !!!!! 收到强制平仓信号 !!!!!")
            
            # 获取当前价格
            quote = get_quote(symbol)
            current_price = float(quote.get("last_done", 0))
            
            # 执行平仓
            side = "Sell" if position_quantity > 0 else "Buy"
            close_order_id = submit_order(symbol, side, 0, outside_rth=outside_rth_setting, is_close=True)
            if not close_order_id:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 强制平仓失败，保持仓位等待重试")
                continue
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 强制平仓完成，ID: {close_order_id}")
            
            # 计算盈亏（全仓计算）
            if entry_price and current_price > 0:
                direction = 1 if position_quantity > 0 else -1
                pnl, pnl_pct = calculate_pnl(entry_price, current_price, direction, position_quantity)
                with pnl_lock:
                    DAILY_PNL += pnl
                    TOTAL_PNL += pnl
                # 记录平仓交易（根据是止盈还是止损区分）
                action_type = "平仓(止盈)" if PROFIT_TARGET_TRIGGERED else "平仓(止损)"
                DAILY_TRADES.append({
                    "time": now.strftime('%Y-%m-%d %H:%M:%S'),
                    "action": action_type,
                    "side": side,
                    "entry_price": entry_price,
                    "exit_price": current_price,
                    "pnl": pnl
                })
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] {action_type}完成: 价格=${current_price:.2f}, 盈亏=${pnl:+.2f} ({pnl_pct:+.2f}%)")
            
            # 重置持仓
            position_quantity = 0
            entry_price = None
            current_stop = None
            # 🎯 重置动态追踪止盈状态
            max_profit_price = None
            trailing_tp_activated = False
            
            with pnl_lock:
                position_data['quantity'] = 0
                position_data['entry_price'] = None
            
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 今日不再进行新的交易")
            print("=" * 60)
        
        # 如果持仓量变为0，重置入场价格和追踪止盈状态
        if position_quantity == 0:
            entry_price = None
            # 🎯 重置动态追踪止盈状态
            max_profit_price = None
            trailing_tp_activated = False
        
        # 检查是否是交易日（调试模式下保持原有逻辑）
        is_today_trading_day, calendar_stale = is_trading_day(symbol)
        if LOG_VERBOSE:
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 是否交易日: {is_today_trading_day}")
            
        if not is_today_trading_day:
            if monitor_thread is not None and monitor_thread.is_alive():
                DAILY_LOSS_MONITOR_ACTIVE = False
                monitor_thread.join(timeout=5)
                monitor_thread = None
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 跳过交易，监控线程已停止")

            if calendar_stale:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 交易日历缓存过期，跳过交易")
            else:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 今天不是交易日，跳过交易")
            if position_quantity != 0:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 非交易日，执行平仓")
                
                # 获取当前价格用于计算盈亏
                quote = get_quote(symbol)
                current_price = float(quote.get("last_done", 0))
                
                side = "Sell" if position_quantity > 0 else "Buy"
                close_order_id = submit_order(symbol, side, 0, outside_rth=outside_rth_setting, is_close=True)
                if not close_order_id:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓失败，保持仓位")
                    continue
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓完成，ID: {close_order_id}")
                
                # 计算盈亏（全仓计算）
                if entry_price and current_price > 0:
                    direction = 1 if position_quantity > 0 else -1
                    pnl, pnl_pct = calculate_pnl(entry_price, current_price, direction, position_quantity)
                    DAILY_PNL += pnl
                    TOTAL_PNL += pnl
                    # 记录平仓交易
                    DAILY_TRADES.append({
                        "time": now.strftime('%Y-%m-%d %H:%M:%S'),
                        "action": "平仓",
                        "side": side,
                        "entry_price": entry_price,
                        "exit_price": current_price,
                        "pnl": pnl
                    })
                    
                position_quantity = 0
                entry_price = None
                # 🎯 重置动态追踪止盈状态
                max_profit_price = None
                trailing_tp_activated = False
                
                # 在交易日结束时打印当日所有交易记录
                if DAILY_TRADES:
                    print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] ===== 当日交易记录 =====")
                    for i, trade in enumerate(DAILY_TRADES, 1):
                        print(f"交易 #{i}:")
                        print(f"  时间: {trade['time']}")
                        print(f"  操作: {trade['action']} {trade['side']}")
                        if 'entry_price' in trade:
                            print(f"  入场价: ${trade['entry_price']:.2f}")
                        if 'exit_price' in trade:
                            print(f"  出场价: ${trade['exit_price']:.2f}")
                        if trade['pnl'] is not None:
                            print(f"  盈亏: ${trade['pnl']:+.2f}")
                    
                    # 计算当日统计
                    total_trades = len([t for t in DAILY_TRADES if '平仓' in t['action']])
                    winning_trades = len([t for t in DAILY_TRADES if '平仓' in t['action'] and t['pnl'] and t['pnl'] > 0])
                    losing_trades = len([t for t in DAILY_TRADES if '平仓' in t['action'] and t['pnl'] and t['pnl'] < 0])
                    
                    print(f"\n当日交易统计:")
                    print(f"  总交易次数: {total_trades}")
                    print(f"  盈利次数: {winning_trades}")
                    print(f"  亏损次数: {losing_trades}")
                    if total_trades > 0:
                        print(f"  胜率: {winning_trades/total_trades*100:.1f}%")
                    print(f"  当日盈亏: ${DAILY_PNL:+.2f}")
                    print(f"  累计盈亏: ${TOTAL_PNL:+.2f}")
                    print("=" * 50)
            retry_hours = 1 if calendar_stale else 12
            next_check_time = now + timedelta(hours=retry_hours)
            wait_seconds = (next_check_time - now).total_seconds()
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] {retry_hours} 小时后重新检查"
                  f"（分段等待并保持 IB 心跳，Gateway 重启后会自动重连）")
            sleep_with_ib_keepalive(wait_seconds)
            continue
            
        # 检查是否是新交易日，如果是则重置今日开仓计数
        if last_date is not None and current_date != last_date:
            positions_opened_today = 0
            DAILY_STOP_TRIGGERED = False  # 重置日内止损标志
            FORCE_CLOSE_POSITION = False  # 重置强制平仓标志
            PROFIT_TARGET_TRIGGERED = False  # 重置止盈标志
            trailing_tp_day_stop = False  # 🎯 重置追踪止盈当日停止开仓标志
            
            # 停止旧的监控线程
            if monitor_thread is not None and monitor_thread.is_alive():
                DAILY_LOSS_MONITOR_ACTIVE = False
                monitor_thread.join(timeout=5)
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 前一日监控线程已停止")
            
            # 打印前一日交易记录
            if DAILY_TRADES:
                print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] ===== 前一日交易记录 ({last_date}) =====")
                for i, trade in enumerate(DAILY_TRADES, 1):
                    print(f"交易 #{i}:")
                    print(f"  时间: {trade['time']}")
                    print(f"  操作: {trade['action']} {trade['side']}")
                    if 'entry_price' in trade:
                        print(f"  入场价: ${trade['entry_price']:.2f}")
                    if 'exit_price' in trade:
                        print(f"  出场价: ${trade['exit_price']:.2f}")
                    if trade['pnl'] is not None:
                        print(f"  盈亏: ${trade['pnl']:+.2f}")
                
                # 计算前一日统计
                total_trades = len([t for t in DAILY_TRADES if '平仓' in t['action']])
                winning_trades = len([t for t in DAILY_TRADES if '平仓' in t['action'] and t['pnl'] and t['pnl'] > 0])
                losing_trades = len([t for t in DAILY_TRADES if '平仓' in t['action'] and t['pnl'] and t['pnl'] < 0])
                
                print(f"\n前一日交易统计:")
                print(f"  总交易次数: {total_trades}")
                print(f"  盈利次数: {winning_trades}")
                print(f"  亏损次数: {losing_trades}")
                if total_trades > 0:
                    print(f"  胜率: {winning_trades/total_trades*100:.1f}%")
                    
                # 清空交易记录，为新交易日准备
                DAILY_TRADES.clear()
            
            # 输出前一日收益统计
            if LAST_STATS_DATE is not None and DAILY_PNL != 0:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] === 收益统计 ===")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 昨日盈亏: ${DAILY_PNL:+.2f}")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 累计盈亏: ${TOTAL_PNL:+.2f}")
                print("=" * 50)
                
            DAILY_PNL = 0.0  # 重置当日收益
            DAILY_STOP_TRIGGERED = False  # 重置日内止损标志
        last_date = current_date
        LAST_STATS_DATE = current_date
        
        # 在每天9:30之后启动日内止损监控线程（只启动一次）
        # 判断当前时间是否在9:30之后且监控线程未启动
        current_hour, current_minute = now.hour, now.minute
        is_after_930 = (current_hour > 9) or (current_hour == 9 and current_minute >= 30)
        # 已触发止盈/止损或已收盘时不再重启监控线程，避免每分钟重复打印启动/停止日志
        monitor_needed = is_after_930 and current_hour < 16 and not (PROFIT_TARGET_TRIGGERED or DAILY_STOP_TRIGGERED)
        if monitor_needed and (monitor_thread is None or not monitor_thread.is_alive()):
            print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] === 初始化日内止损监控 ===")
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 初始资金: ${INITIAL_CAPITAL:.2f}")
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 当日盈亏: ${DAILY_PNL:+.2f}")
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 累计盈亏: ${TOTAL_PNL:+.2f}")
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 仓位模式: 日内保证金满仓（约 13x；201 则减张）")
            if MAX_DAILY_LOSS_AMOUNT > 0:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 日内最大亏损限额: ${MAX_DAILY_LOSS_AMOUNT:.2f}")
            else:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 日内止损: 已禁用")
            print("=" * 60 + "\n")
            
            # 启动监控线程
            DAILY_LOSS_MONITOR_ACTIVE = True
            monitor_thread = threading.Thread(
                target=daily_loss_monitor_thread,
                args=(symbol, position_data),
                daemon=True
            )
            monitor_thread.start()
        
        # 检查是否到达检查时间点
        current_hour, current_minute = now.hour, now.minute
        current_second = now.second
        
        # 生成今天所有的检查时间点（这些是K线时间，不是触发时间）
        k_line_check_times = []
        h, m = trading_start_time
        while h < trading_end_time[0] or (h == trading_end_time[0] and m <= trading_end_time[1]):
            k_line_check_times.append((h, m))
            m += check_interval_minutes
            if m >= 60:
                h += 1
                m = m % 60
        
        # 始终添加结束时间
        if (trading_end_time[0], trading_end_time[1]) not in k_line_check_times:
            k_line_check_times.append((trading_end_time[0], trading_end_time[1]))
        
        # 生成实际的触发时间点（K线时间的下一分钟）
        trigger_times = []
        for k_h, k_m in k_line_check_times:
            # 计算下一分钟作为触发时间
            trigger_m = k_m + 1
            trigger_h = k_h
            if trigger_m >= 60:
                trigger_h += 1
                trigger_m = 0
            # 跳过超出交易时间的触发点
            if trigger_h < 16:  # 假设市场在16:00关闭
                trigger_times.append((trigger_h, trigger_m))
        
        # 判断当前是否是触发时间点（允许前后30秒的误差）
        is_trigger_time = False
        for trigger_h, trigger_m in trigger_times:
            trigger_time = now.replace(hour=trigger_h, minute=trigger_m, second=1, microsecond=0)
            time_diff = abs((now - trigger_time).total_seconds())
            if time_diff <= 30:  # 30秒误差范围内都认为是触发时间
                is_trigger_time = True
                break
        
        if is_trigger_time:
            # 找到最接近的触发时间对应的K线时间
            closest_trigger_idx = None
            min_diff = float('inf')
            for i, (trigger_h, trigger_m) in enumerate(trigger_times):
                trigger_time = now.replace(hour=trigger_h, minute=trigger_m, second=1, microsecond=0)
                time_diff = abs((now - trigger_time).total_seconds())
                if time_diff < min_diff:
                    min_diff = time_diff
                    closest_trigger_idx = i
            
            if closest_trigger_idx is not None:
                k_h, k_m = k_line_check_times[closest_trigger_idx]
                # 去重：同一触发点在±30秒窗口内只处理一次，避免触发窗口内 continue 不睡眠导致空转刷屏
                if last_processed_trigger == (now.date(), k_h, k_m):
                    time_module.sleep(5)
                    continue
                last_processed_trigger = (now.date(), k_h, k_m)
                check_time_str = f"{k_h:02d}:{k_m:02d}"
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 触发检查，使用 {check_time_str} 的K线数据")
            else:
                # 如果没有找到合适的触发时间，跳过本次检查
                continue
        else:
            # 如果不是触发时间点，计算下一个触发时间
            next_trigger_time = None
            for trigger_h, trigger_m in trigger_times:
                if trigger_h > current_hour or (trigger_h == current_hour and trigger_m > current_minute):
                    next_trigger_time = datetime.combine(current_date, time(trigger_h, trigger_m), tzinfo=now.tzinfo)
                    break
            
            if next_trigger_time is None:
                # 今天没有更多触发时间，等到明天
                tomorrow = current_date + timedelta(days=1)
                if trigger_times:
                    next_trigger_time = datetime.combine(tomorrow, time(trigger_times[0][0], trigger_times[0][1]), tzinfo=now.tzinfo)
                else:
                    # 如果没有触发时间，使用默认的开始时间
                    next_trigger_time = datetime.combine(tomorrow, time(trading_start_time[0], trading_start_time[1] + 1), tzinfo=now.tzinfo)
            
            wait_seconds = (next_trigger_time - now).total_seconds()
            if wait_seconds > 0:
                wait_seconds = min(wait_seconds, 60)  # 最多等待1分钟，以便及时响应止盈止损信号
                
                # 止盈止损检查已由监控线程处理，此处不再重复检查
                
                if LOG_VERBOSE:
                    # 找到下一个K线检查时间用于显示
                    next_trigger_idx = None
                    for i, (t_h, t_m) in enumerate(trigger_times):
                        if t_h > current_hour or (t_h == current_hour and t_m > current_minute):
                            next_trigger_idx = i
                            break
                    if next_trigger_idx is not None:
                        next_k_h, next_k_m = k_line_check_times[next_trigger_idx]
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 等待 {wait_seconds:.0f} 秒到下一个检查时间 {next_k_h:02d}:{next_k_m:02d} (触发时间: {next_trigger_time.strftime('%H:%M:%S')})")
                    else:
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 等待 {wait_seconds:.0f} 秒到下一个检查时间 (触发时间: {next_trigger_time.strftime('%H:%M:%S')})")
                time_module.sleep(wait_seconds)
                continue
        
        # 更新当前时间信息
        now = get_us_eastern_time()
        current_date = now.date()
        
        # 只在触发时间点进行交易检查
        if not is_trigger_time:
            # 如果不是触发时间，跳过本次循环
            continue

        # 每个检查点以 IB 真实仓位为准（10349 假失败时本地会错当成空仓，从而漏止损）
        try:
            ib_pos = get_ib_position_qty()
            ib_pos = int(ib_pos) if float(ib_pos) == int(ib_pos) else ib_pos
            if ib_pos != position_quantity:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 同步 IB 仓位: 本地 {position_quantity} → {ib_pos}")
                if position_quantity != 0 and ib_pos == 0:
                    entry_price = None
                    max_profit_price = None
                    trailing_tp_activated = False
                position_quantity = ib_pos
        except Exception as e:
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 同步 IB 仓位失败: {e}")
            
        # 检查是否是交易时间结束点，如果是且有持仓，则强制平仓
        is_trading_end = (current_hour, current_minute) == (trading_end_time[0], trading_end_time[1])
        # 兜底：到达交易结束时间点时，空仓也不再开新仓，避免尾盘开仓被持仓过夜
        if is_trading_end and position_quantity == 0:
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 已到交易结束时间 {trading_end_time[0]:02d}:{trading_end_time[1]:02d}，当前空仓，跳过开仓检查")
            continue
        if is_trading_end and position_quantity != 0:
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 当前时间为交易结束时间 {trading_end_time[0]}:{trading_end_time[1]}，执行平仓")
            
            # 获取历史数据
            if LOG_VERBOSE:
                print("获取历史数据")
            df = get_historical_data(symbol)
            if df.empty:
                print("错误: 获取历史数据为空")
                sys.exit(1)
                
            if DEBUG_MODE:
                df = df[df["DateTime"] <= now]
            
            # 获取当前时间点的价格数据
            current_time = now.strftime('%H:%M')
            
            # 尝试获取当前时间点数据，如果没有则等待重试
            retry_count = 0
            max_retries = 10
            retry_interval = 5
            current_price = None
            
            while retry_count < max_retries:
                current_data = df[(df["Date"] == current_date) & (df["Time"] == current_time)]
                
                if not current_data.empty:
                    # 使用当前时间点的价格
                    current_price = float(current_data["Close"].iloc[0])
                    break
                else:
                    retry_count += 1
                    if retry_count < max_retries:
                        if LOG_VERBOSE:
                            print(f"警告: 当前时间点 {current_time} 没有数据，等待{retry_interval}秒后重试 ({retry_count}/{max_retries})")
                        time_module.sleep(retry_interval)
                        # 重新获取数据
                        df = get_historical_data(symbol)
                        if DEBUG_MODE:
                            df = df[df["DateTime"] <= now]
            
            if current_price is None:
                print(f"错误: 尝试{max_retries}次后仍无法获取当前时间点 {current_time} 的数据")
                sys.exit(1)
            
            # 执行平仓
            side = "Sell" if position_quantity > 0 else "Buy"
            close_order_id = submit_order(symbol, side, 0, outside_rth=outside_rth_setting, is_close=True)
            if not close_order_id:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 收盘平仓失败，保持仓位")
                continue
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓完成，ID: {close_order_id}")
            
            # 计算盈亏（全仓计算）
            if entry_price:
                direction = 1 if position_quantity > 0 else -1
                pnl, pnl_pct = calculate_pnl(entry_price, current_price, direction, position_quantity)
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓成功: {side} {TRADE_SYMBOL} 出场价: {current_price}")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 交易结果: {'盈利' if pnl > 0 else '亏损'} ${abs(pnl):.2f} ({pnl_pct:+.2f}%)")
                # 更新收益统计
                DAILY_PNL += pnl
                TOTAL_PNL += pnl
                # 记录平仓交易
                DAILY_TRADES.append({
                    "time": now.strftime('%Y-%m-%d %H:%M:%S'),
                    "action": "平仓",
                    "side": side,
                    "entry_price": entry_price,
                    "exit_price": current_price,
                    "pnl": pnl
                })
            else:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓成功: {side} {TRADE_SYMBOL} 出场价: {current_price}")
                
            position_quantity = 0
            entry_price = None
            # 🎯 重置动态追踪止盈状态
            max_profit_price = None
            trailing_tp_activated = False
            
            # 在交易日结束时打印当日所有交易记录
            if DAILY_TRADES:
                print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] ===== 当日交易记录 =====")
                for i, trade in enumerate(DAILY_TRADES, 1):
                    print(f"交易 #{i}:")
                    print(f"  时间: {trade['time']}")
                    print(f"  操作: {trade['action']} {trade['side']}")
                    if 'entry_price' in trade:
                        print(f"  入场价: ${trade['entry_price']:.2f}")
                    if 'exit_price' in trade:
                        print(f"  出场价: ${trade['exit_price']:.2f}")
                    if trade['pnl'] is not None:
                        print(f"  盈亏: ${trade['pnl']:+.2f}")
                
                # 计算当日统计
                total_trades = len([t for t in DAILY_TRADES if '平仓' in t['action']])
                winning_trades = len([t for t in DAILY_TRADES if '平仓' in t['action'] and t['pnl'] and t['pnl'] > 0])
                losing_trades = len([t for t in DAILY_TRADES if '平仓' in t['action'] and t['pnl'] and t['pnl'] < 0])
                
                print(f"\n当日交易统计:")
                print(f"  总交易次数: {total_trades}")
                print(f"  盈利次数: {winning_trades}")
                print(f"  亏损次数: {losing_trades}")
                if total_trades > 0:
                    print(f"  胜率: {winning_trades/total_trades*100:.1f}%")
                print(f"  当日盈亏: ${DAILY_PNL:+.2f}")
                print(f"  累计盈亏: ${TOTAL_PNL:+.2f}")
                print("=" * 50)
                
                # 清空当日交易记录，为下一个交易日准备
                DAILY_TRADES.clear()
            

            continue
        
        # 保持原有交易时间检查逻辑
        start_hour, start_minute = trading_start_time
        end_hour, end_minute = trading_end_time
        is_trading_hours = (
            (current_hour > start_hour or (current_hour == start_hour and current_minute >= start_minute)) and
            (current_hour < end_hour or (current_hour == end_hour and current_minute <= end_minute))
        )
        
        df = get_historical_data(symbol)
        if df.empty:
            print("Error: Could not get historical data")
            sys.exit(1)
        if LOG_VERBOSE:
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 历史数据获取完成: {len(df)} 条")
            
        # 调试模式下，根据指定时间截断数据
        if DEBUG_MODE:
            # 截断到调试时间之前的数据
            df = df[df["DateTime"] <= now]
            
        if not is_trading_hours:
            if LOG_VERBOSE:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 当前不在交易时间内 ({trading_start_time[0]:02d}:{trading_start_time[1]:02d} - {trading_end_time[0]:02d}:{trading_end_time[1]:02d})")
            if position_quantity != 0:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 交易日结束，执行平仓")
                
                # 获取当前价格用于计算盈亏
                quote = get_quote(symbol)
                current_price = float(quote.get("last_done", 0))
                
                side = "Sell" if position_quantity > 0 else "Buy"
                close_order_id = submit_order(symbol, side, 0, outside_rth=outside_rth_setting, is_close=True)
                if not close_order_id:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 交易时段外平仓失败，保持仓位")
                else:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓完成，ID: {close_order_id}")
                    
                    # 计算盈亏（全仓计算）
                    if entry_price and current_price > 0:
                        direction = 1 if position_quantity > 0 else -1
                        pnl, pnl_pct = calculate_pnl(entry_price, current_price, direction, position_quantity)
                        DAILY_PNL += pnl
                        TOTAL_PNL += pnl
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓盈亏: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
                        # 记录平仓交易
                        DAILY_TRADES.append({
                            "time": now.strftime('%Y-%m-%d %H:%M:%S'),
                            "action": "平仓",
                            "side": side,
                            "entry_price": entry_price,
                            "exit_price": current_price,
                            "pnl": pnl
                        })
                        
                    position_quantity = 0
                    entry_price = None
                    # 🎯 重置动态追踪止盈状态
                    max_profit_price = None
                    trailing_tp_activated = False
            now = get_us_eastern_time()
            today = now.date()
            today_start = datetime.combine(today, time(trading_start_time[0], trading_start_time[1]), tzinfo=now.tzinfo)
            if now < today_start:
                next_check_time = today_start
            else:
                tomorrow = today + timedelta(days=1)
                tomorrow_start = datetime.combine(tomorrow, time(trading_start_time[0], trading_start_time[1]), tzinfo=now.tzinfo)
                next_check_time = tomorrow_start
            wait_seconds = min(1800, (next_check_time - now).total_seconds())
            sleep_with_ib_keepalive(wait_seconds)
            continue
            
        # 使用新的VWAP计算方法
        df["VWAP"] = calculate_vwap(df)
        
        # 直接计算噪声区域，不需要中间复制
        df = calculate_noise_area(df, lookback_days, K1, K2)
        
        if position_quantity != 0:
            # 使用检查时间点的数据进行止损检查
            if 'check_time_str' not in locals():
                # 如果没有设置check_time_str，使用当前时间的前一分钟
                if current_minute > 0:
                    check_time_str = f"{current_hour:02d}:{current_minute-1:02d}"
                else:
                    check_time_str = f"{current_hour-1:02d}:59"
            
            # 获取检查时间点的数据
            latest_date = df["Date"].max()
            check_data = df[(df["Date"] == latest_date) & (df["Time"] == check_time_str)]
            
            if not check_data.empty:
                check_row = check_data.iloc[0]
                check_price = float(check_row["Close"])
                check_high = float(check_row["High"])
                check_low = float(check_row["Low"])
                check_upper = check_row["UpperBound"]
                check_lower = check_row["LowerBound"]
                check_vwap = check_row["VWAP"]
                
                # 根据持仓方向检查退出条件
                exit_signal = False
                stop_loss_exit = False
                trailing_tp_exit = False
                exit_reason = "Stop Loss"
                
                if position_quantity > 0:  # 多头持仓
                    # 使用检查时间点的上边界（和VWAP）作为止损
                    new_stop = max(check_upper, check_vwap) if USE_VWAP else check_upper
                    stop_loss_exit = check_price < new_stop
                    current_stop = new_stop
                    
                    # 🎯 动态追踪止盈逻辑（多头）
                    if ENABLE_TRAILING_TAKE_PROFIT and entry_price is not None:
                        # 使用 High 更新最大盈利价格
                        if max_profit_price is None:
                            max_profit_price = check_high
                        else:
                            max_profit_price = max(max_profit_price, check_high)
                        
                        # 计算当前浮盈百分比（基于最大盈利价格）
                        current_profit_pct = (max_profit_price - entry_price) / entry_price
                        
                        # 检查是否激活追踪止盈
                        if current_profit_pct >= TRAILING_TP_ACTIVATION_PCT:
                            if not trailing_tp_activated:
                                trailing_tp_activated = True
                                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 🎯 动态追踪止盈已激活! 最大浮盈: {current_profit_pct*100:.2f}%")
                            
                            # 计算动态止盈水平：入场价 + 保护的利润比例 * 最大浮盈
                            protected_profit = (max_profit_price - entry_price) * TRAILING_TP_CALLBACK_PCT
                            dynamic_take_profit_level = entry_price + protected_profit
                            
                            # 检查是否触发追踪止盈
                            if check_price <= dynamic_take_profit_level:
                                trailing_tp_exit = True
                                exit_reason = "Trailing Take Profit"
                                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 🎯 触发动态追踪止盈! 价格={check_price:.2f}, 止盈线={dynamic_take_profit_level:.2f}")
                    
                    exit_signal = stop_loss_exit or trailing_tp_exit
                    
                elif position_quantity < 0:  # 空头持仓
                    # 使用检查时间点的下边界（和VWAP）作为止损
                    new_stop = min(check_lower, check_vwap) if USE_VWAP else check_lower
                    stop_loss_exit = check_price > new_stop
                    current_stop = new_stop
                    
                    # 🎯 动态追踪止盈逻辑（空头）
                    if ENABLE_TRAILING_TAKE_PROFIT and entry_price is not None:
                        # 使用 Low 更新最大盈利价格（空头：价格越低盈利越大）
                        if max_profit_price is None:
                            max_profit_price = check_low
                        else:
                            max_profit_price = min(max_profit_price, check_low)
                        
                        # 计算当前浮盈百分比（基于最大盈利价格）
                        current_profit_pct = (entry_price - max_profit_price) / entry_price
                        
                        # 检查是否激活追踪止盈
                        if current_profit_pct >= TRAILING_TP_ACTIVATION_PCT:
                            if not trailing_tp_activated:
                                trailing_tp_activated = True
                                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 🎯 动态追踪止盈已激活! 最大浮盈: {current_profit_pct*100:.2f}%")
                            
                            # 计算动态止盈水平：入场价 - 保护的利润比例 * 最大浮盈
                            protected_profit = (entry_price - max_profit_price) * TRAILING_TP_CALLBACK_PCT
                            dynamic_take_profit_level = entry_price - protected_profit
                            
                            # 检查是否触发追踪止盈
                            if check_price >= dynamic_take_profit_level:
                                trailing_tp_exit = True
                                exit_reason = "Trailing Take Profit"
                                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 🎯 触发动态追踪止盈! 价格={check_price:.2f}, 止盈线={dynamic_take_profit_level:.2f}")
                    
                    exit_signal = stop_loss_exit or trailing_tp_exit
                
                if LOG_VERBOSE:
                    trailing_info = ""
                    if ENABLE_TRAILING_TAKE_PROFIT and trailing_tp_activated:
                        trailing_info = f", 追踪止盈=已激活, 最优价={max_profit_price:.2f}"
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 持仓检查 {check_time_str}: 数量={position_quantity}, 价格={check_price:.2f}, 止损={current_stop:.2f}, 退出信号={exit_signal}{trailing_info}")
            else:
                # 如果没有检查时间点的数据，使用原有逻辑
                exit_signal, new_stop = check_exit_conditions(df, position_quantity, current_stop)
                current_stop = new_stop
                trailing_tp_exit = False  # 此路径无追踪止盈判断，避免沿用上一轮的旧值
                exit_reason = "Stop Loss"  # 默认使用止损退出原因
                if LOG_VERBOSE:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 持仓检查: 数量={position_quantity}, 退出信号={exit_signal}, 当前止损={current_stop}")
            if exit_signal:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 触发退出信号!")
                    
                    # 确保使用当前时间点的价格数据
                    current_time = now.strftime('%H:%M')
                    
                    # 尝试获取当前时间点数据，如果没有则等待重试
                    retry_count = 0
                    max_retries = 10
                    retry_interval = 5
                    exit_price = None
                    
                    while retry_count < max_retries:
                        current_data = df[(df["Date"] == current_date) & (df["Time"] == current_time)]
                        
                        if not current_data.empty:
                            # 使用当前时间点的价格
                            exit_price = float(current_data["Close"].iloc[0])
                            break
                        else:
                            retry_count += 1
                            if retry_count < max_retries:
                                if LOG_VERBOSE:
                                    print(f"警告: 当前时间点 {current_time} 没有数据，等待{retry_interval}秒后重试 ({retry_count}/{max_retries})")
                                time_module.sleep(retry_interval)
                                # 重新获取数据
                                df = get_historical_data(symbol)
                                if DEBUG_MODE:
                                    df = df[df["DateTime"] <= now]
                                # 重新计算VWAP和噪声区域
                                df["VWAP"] = calculate_vwap(df)
                                df = calculate_noise_area(df, lookback_days, K1, K2)
                    
                    if exit_price is None:
                        print(f"错误: 尝试{max_retries}次后仍无法获取当前时间点 {current_time} 的数据")
                        continue  # 继续下一次循环，而不是退出
                    
                    # 执行平仓
                    side = "Sell" if position_quantity > 0 else "Buy"
                    close_order_id = submit_order(symbol, side, 0, outside_rth=outside_rth_setting, is_close=True)
                    if not close_order_id:
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 止损/追踪止盈平仓失败，保持仓位")
                        continue
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓完成，ID: {close_order_id}")
                    
                    # 计算盈亏（全仓计算）
                    if entry_price:
                        direction = 1 if position_quantity > 0 else -1
                        pnl, pnl_pct = calculate_pnl(entry_price, exit_price, direction, position_quantity)
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓成功: {side} {TRADE_SYMBOL} 出场价: {exit_price}")
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 交易结果: {'盈利' if pnl > 0 else '亏损'} ${abs(pnl):.2f} ({pnl_pct:+.2f}%)")
                        # 更新收益统计
                        DAILY_PNL += pnl
                        TOTAL_PNL += pnl
                        # 记录平仓交易
                        DAILY_TRADES.append({
                            "time": now.strftime('%Y-%m-%d %H:%M:%S'),
                            "action": "平仓",
                            "side": side,
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "pnl": pnl
                        })
                    
                    # 平仓后增加交易次数计数器
                    positions_opened_today += 1
                    
                    position_quantity = 0
                    entry_price = None
                    # 🎯 重置动态追踪止盈状态
                    max_profit_price = None
                    trailing_tp_activated = False
                    
                    # 🎯 追踪止盈触发后，当日不再开新仓
                    if trailing_tp_exit:
                        trailing_tp_day_stop = True
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 🎯 追踪止盈已触发，今日不再开新仓")
        else:
            # 检查是否已有持仓，如果有则不再开仓
            if position_quantity != 0:
                if LOG_VERBOSE:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 已有持仓，跳过开仓检查")
                continue
            
            # 检查是否触发了止盈或止损，如果是则不再开仓
            if PROFIT_TARGET_TRIGGERED:
                if LOG_VERBOSE:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 已触发止盈目标，跳过开仓检查")
                continue
            
            if DAILY_STOP_TRIGGERED:
                if LOG_VERBOSE:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 已触发日内止损，跳过开仓检查")
                continue
            
            # 🎯 追踪止盈当日已触发，不再开仓
            if trailing_tp_day_stop:
                if LOG_VERBOSE:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 当日已触发追踪止盈，跳过开仓检查")
                continue
                
            # 检查今日是否达到最大持仓数
            if positions_opened_today >= max_positions_per_day:
                if LOG_VERBOSE:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 今日已开仓 {positions_opened_today} 次，达到上限")
                continue
            
            # 使用检查时间点的完整K线数据
            # check_time_str 在前面已经设置为要检查的时间（如 "09:40"）
            if 'check_time_str' not in locals():
                # 如果没有设置check_time_str，使用当前时间的前一分钟
                if current_minute > 0:
                    check_time_str = f"{current_hour:02d}:{current_minute-1:02d}"
                else:
                    check_time_str = f"{current_hour-1:02d}:59"
            
            # 获取检查时间点的数据
            latest_date = df["Date"].max()
            check_data = df[(df["Date"] == latest_date) & (df["Time"] == check_time_str)]
            
            if check_data.empty:
                if LOG_VERBOSE:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 警告: 没有找到 {check_time_str} 的数据，跳过本次检查")
                continue
            
            # 使用检查时间点的完整K线数据
            latest_row = check_data.iloc[0].copy()
            latest_price = float(latest_row["Close"])
            long_price_above_upper = latest_price > latest_row["UpperBound"]
            long_price_above_vwap = latest_price > latest_row["VWAP"] if USE_VWAP else True
            
            if LOG_VERBOSE:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 检查 {check_time_str} 的数据:")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 价格={latest_price:.2f}, 上界={latest_row['UpperBound']:.2f}, VWAP={latest_row['VWAP']:.2f}, 下界={latest_row['LowerBound']:.2f}")
            
            signal = 0
            price = latest_price
            stop = None
            
            if long_price_above_upper and long_price_above_vwap:
                if LOG_VERBOSE:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 满足多头入场条件!")
                signal = 1
                stop = max(latest_row["UpperBound"], latest_row["VWAP"]) if USE_VWAP else latest_row["UpperBound"]
            else:
                short_price_below_lower = latest_price < latest_row["LowerBound"]
                short_price_below_vwap = latest_price < latest_row["VWAP"] if USE_VWAP else True
                if short_price_below_lower and short_price_below_vwap:
                    if LOG_VERBOSE:
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 满足空头入场条件!")
                    signal = -1
                    stop = min(latest_row["LowerBound"], latest_row["VWAP"]) if USE_VWAP else latest_row["LowerBound"]
                else:
                    if LOG_VERBOSE:
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 不满足入场条件: 多头({long_price_above_upper} & {long_price_above_vwap}), 空头({short_price_below_lower} & {short_price_below_vwap})")
            signal = apply_entry_gates_to_signal(signal, df, LOG_VERBOSE, now.strftime('%Y-%m-%d %H:%M:%S'), current_sigma=latest_row.get('sigma'))
            if signal != 0:
                global _LAST_OPEN_SIGNAL_KEY
                signal_key = (current_date, check_time_str)
                if _LAST_OPEN_SIGNAL_KEY == signal_key:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 同一信号窗口已下过单 ({signal_key})，跳过重复开仓")
                    continue

                # 断线重连后先同步仓位，避免盲开
                if not ib_ensure_connected():
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] IB 未连接，跳过开仓")
                    continue
                ib_pos = get_ib_position_qty()
                if ib_pos != 0:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] IB 已有仓位 {ib_pos}，同步本地状态，不开新仓")
                    position_quantity = int(ib_pos) if float(ib_pos) == int(ib_pos) else ib_pos
                    if entry_price is None:
                        entry_price = latest_price
                    continue

                side = "Buy" if signal > 0 else "Sell"
                qty = calc_ib_order_quantity(qqq_px=latest_price, side=side)
                if qty < 1:
                    continue

                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 触发{'多' if signal == 1 else '空'}头入场信号!")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 信号价(QQQ): {price}, VWAP: {latest_row['VWAP']:.4f}, "
                      f"上界: {latest_row['UpperBound']:.4f}, 下界: {latest_row['LowerBound']:.4f}, 止损: {stop}")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 执行: {TRADE_SYMBOL} × {qty} 张（满仓）")

                order_id = submit_order(symbol, side, qty, outside_rth=outside_rth_setting)
                ib_pos = get_ib_position_qty()
                ib_pos = int(ib_pos) if float(ib_pos) == int(ib_pos) else ib_pos
                if ib_pos == 0:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 开仓失败，保持空仓")
                    continue
                if not order_id:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 下单回报失败但 IB 已成交 {ib_pos} 张，按真实仓位对齐")

                _LAST_OPEN_SIGNAL_KEY = signal_key
                position_quantity = ib_pos
                entry_price = latest_price
                filled_qty = abs(int(ib_pos)) if float(ib_pos) == int(ib_pos) else abs(ib_pos)
                side = "Buy" if ib_pos > 0 else "Sell"
                actual_notional = filled_qty * entry_price * NQ_QQQ_RATIO * MNQ_POINT_VALUE
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 开仓成功: {side} {TRADE_SYMBOL} qty={filled_qty} 张 "
                      f"信号入场价(QQQ): {entry_price} orderId={order_id}")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 仓位: {filled_qty} 张 MNQ ≈ ${actual_notional:,.0f} 名义 "
                      f"(按可用保证金满仓)")

                DAILY_TRADES.append({
                    "time": now.strftime('%Y-%m-%d %H:%M:%S'),
                    "action": "开仓",
                    "side": side,
                    "entry_price": entry_price,
                    "pnl": None
                })
        
        # 调试模式且单次运行模式，完成一次循环后退出
        if DEBUG_MODE and DEBUG_ONCE:
            print("\n调试模式单次运行完成，程序退出")
            
            # 打印当日交易记录（如果有）
            if DAILY_TRADES:
                print(f"\n===== 当日交易记录 =====")
                for i, trade in enumerate(DAILY_TRADES, 1):
                    print(f"交易 #{i}:")
                    print(f"  时间: {trade['time']}")
                    print(f"  操作: {trade['action']} {trade['side']} {trade['quantity']} 股")
                    print(f"  价格: ${trade['price']:.2f}")
                    if trade['pnl'] is not None:
                        print(f"  盈亏: ${trade['pnl']:+.2f}")
                
                # 计算当日统计
                total_trades = len([t for t in DAILY_TRADES if t['action'] == '平仓'])
                winning_trades = len([t for t in DAILY_TRADES if t['action'] == '平仓' and t['pnl'] > 0])
                losing_trades = len([t for t in DAILY_TRADES if t['action'] == '平仓' and t['pnl'] < 0])
                
                print(f"\n当日交易统计:")
                print(f"  总交易次数: {total_trades}")
                print(f"  盈利次数: {winning_trades}")
                print(f"  亏损次数: {losing_trades}")
                if total_trades > 0:
                    print(f"  胜率: {winning_trades/total_trades*100:.1f}%")
                print("=" * 50)
            
            # 输出最终收益统计
            if DAILY_PNL != 0 or TOTAL_PNL != 0:
                print(f"\n=== 最终收益统计 ===")
                print(f"当日盈亏: ${DAILY_PNL:+.2f}")
                print(f"累计盈亏: ${TOTAL_PNL:+.2f}")
            break
            
        # 同步持仓状态给监控线程：平仓后本轮会进入长时间 sleep，若不立即同步，
        # 监控线程会读到已平仓的旧持仓并把未实现盈亏重复计入，导致日内止损被误触发
        with pnl_lock:
            position_data['quantity'] = position_quantity
            position_data['entry_price'] = entry_price

        # 计算下一个精确的检查时间点（避免累积误差）
        current_time = now.time()
        current_hour, current_minute = current_time.hour, current_time.minute
        
        # 计算下一个检查时间点
        next_check_minute = ((current_minute // check_interval_minutes) + 1) * check_interval_minutes
        next_check_hour = current_hour
        
        if next_check_minute >= 60:
            next_check_hour += next_check_minute // 60
            next_check_minute = next_check_minute % 60
        
        # 创建下一个检查时间的datetime对象
        next_check_time = now.replace(hour=next_check_hour, minute=next_check_minute, second=0, microsecond=0)
        
        # 如果计算的时间已经过了，则加一天
        if next_check_time <= now:
            next_check_time += timedelta(days=1)
        
        sleep_seconds = (next_check_time - now).total_seconds()
        if sleep_seconds > 0:
            if LOG_VERBOSE:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 等待 {sleep_seconds:.1f} 秒到下一个精确检查时间 {next_check_time.strftime('%H:%M:%S')}")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 当前时间精度检查: 秒={now.second}, 微秒={now.microsecond}")
            # 超过心跳间隔的长等待用分段保活，短等待直接 sleep（下一轮主循环会探活）
            if sleep_seconds >= IB_HEARTBEAT_INTERVAL_SEC:
                sleep_with_ib_keepalive(sleep_seconds)
            else:
                time_module.sleep(sleep_seconds)

if __name__ == "__main__":
    sys.stdout = Logger(LOG_FILE)
    sys.stderr = sys.stdout

    print("\n" + "=" * 60)
    print("IBKR Gateway 微纳指期货（MNQ）交易程序")
    print("=" * 60)
    print("信号: Longport QQQ 分钟线 | 执行: ib_insync → MNQ")
    print("无 SQLite/MT5 | 无 FTMO 日亏/考试规则")
    print("-" * 60)
    print("风险提示:")
    print("  · QQQ 信号 vs MNQ 存在基差，实盘 PnL ≠ QQQ 回测")
    print("  · 每季度换月：请更新 MNQ_EXPIRY（或设为空自动选近月）")
    print("  · 无 CME/MNQ 期货权限时 qualify 失败即停")
    print("  · 满仓模式：按 IBKR 日内初始保证金 100% 净值估算张数（约 13x）；Error 201 减张重试；收盘前平仓")
    print("  · Gateway Lock and Exit 请用「自动重启」；脚本会心跳+自动重连，断线不退出")
    print("=" * 60)

    print("\n--- 风控设置 ---")
    apply_optional_risk_settings()

    print("版本: 2.1.1")
    print("时间:", get_us_eastern_time().strftime("%Y-%m-%d %H:%M:%S"), "(美东时间)")
    print(f"日志文件: {os.path.abspath(LOG_FILE)}")
    print(f"行情缓存数据库: {os.path.abspath(MARKET_DATA_DB_PATH)}")

    if not ensure_market_data_service_available():
        sys.exit(1)

    ib_connect()
    apply_ib_account_capital()

    print("\n--- 用户配置参数 ---")
    print(f"信号品种: {SYMBOL}")
    expiry_disp = (IB_CONTRACT.lastTradeDateOrContractMonth if IB_CONTRACT else None) or MNQ_EXPIRY or '自动近月'
    local_disp = (IB_CONTRACT.localSymbol if IB_CONTRACT else None) or TRADE_SYMBOL
    print(f"执行合约: {local_disp} (FUT/CME) expiry={expiry_disp}")
    print(f"IB Gateway: {IB_HOST}:{IB_PORT} clientId={IB_CLIENT_ID}")
    print(f"IB 断线重连: 间隔 {IB_RECONNECT_INTERVAL_SEC}s，"
          f"{'无限重试' if IB_RECONNECT_MAX_ATTEMPTS <= 0 else f'最多 {IB_RECONNECT_MAX_ATTEMPTS} 次'}，"
          f"心跳 {IB_HEARTBEAT_INTERVAL_SEC}s")
    print(f"账户净值: ${INITIAL_CAPITAL:.2f}")
    print(f"仓位模式: 满仓（净值 × {MARGIN_USAGE_PCT:g} / 日内初始保证金，名义×{MNQ_INTRADAY_IM_PCT:.2%}；201 则 90%/85%/70% 减张）")
    if MAX_MNQ_CONTRACTS and MAX_MNQ_CONTRACTS > 0:
        print(f"张数硬顶: {MAX_MNQ_CONTRACTS}")
    else:
        print("张数硬顶: 无（能买多少买多少）")
    print(f"可选账户止盈: ${MAX_PROFIT_AMOUNT:.2f} ({'已禁用' if MAX_PROFIT_AMOUNT is None or MAX_PROFIT_AMOUNT <= 0 else '已启用'})")
    print(f"可选日内止损: ${MAX_DAILY_LOSS_AMOUNT:.2f} ({'已禁用' if MAX_DAILY_LOSS_AMOUNT is None or MAX_DAILY_LOSS_AMOUNT <= 0 else '已启用'})")
    print(f"交易时间: {TRADING_START_TIME[0]:02d}:{TRADING_START_TIME[1]:02d} - {TRADING_END_TIME[0]:02d}:{TRADING_END_TIME[1]:02d}")
    print(f"检查间隔: {CHECK_INTERVAL_MINUTES} 分钟")
    print(f"每日最大开仓: {MAX_POSITIONS_PER_DAY} 次")
    print(f"策略参数: {format_k_strategy_params(K1, K2, LOOKBACK_DAYS, ENABLE_K_SIDE_ADJUSTMENT)}")

    print("\n--- 动态追踪止盈配置 ---")
    print(f"动态追踪止盈: {'已启用' if ENABLE_TRAILING_TAKE_PROFIT else '已禁用'}")
    if ENABLE_TRAILING_TAKE_PROFIT:
        print(f"  激活阈值: {TRAILING_TP_ACTIVATION_PCT*100:.1f}% (浮盈达到此比例后激活)")
        print(f"  保护比例: {TRAILING_TP_CALLBACK_PCT*100:.0f}% (保护最大浮盈的此比例)")
        print("  触发后当日停止开仓: 是")

    print("\n--- 调试配置 ---")
    if DEBUG_MODE:
        print("调试模式: 已开启（使用固定时间）")
        if 'DEBUG_TIME' in globals() and DEBUG_TIME:
            print(f"  固定时间: {DEBUG_TIME}")
        if DEBUG_ONCE:
            print("  单次运行: 是")
    else:
        print("调试模式: 已关闭（使用当前时间）")
    print(f"详细日志: {'已开启' if LOG_VERBOSE else '已关闭'}")

    print("\n--- 运行时状态 ---")
    print(f"初始 TOTAL_PNL: ${TOTAL_PNL:.2f}")
    print(f"初始 DAILY_PNL: ${DAILY_PNL:.2f}")
    print("=" * 60 + "\n")

    # Gateway 每日自动重启会导致 socket 断开；外层兜底捕获后重连，绝不因断线退出
    while True:
        try:
            if not ib_ensure_connected():
                time_module.sleep(IB_RECONNECT_INTERVAL_SEC)
                continue
            run_trading_strategy(
                symbol=SYMBOL,
                check_interval_minutes=CHECK_INTERVAL_MINUTES,
                trading_start_time=TRADING_START_TIME,
                trading_end_time=TRADING_END_TIME,
                max_positions_per_day=MAX_POSITIONS_PER_DAY,
                lookback_days=LOOKBACK_DAYS
            )
            # 正常返回（例如 DEBUG_ONCE）则结束
            break
        except KeyboardInterrupt:
            print(f"\n[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 收到中断，准备退出")
            break
        except Exception as e:
            ts = get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{ts}] 策略循环异常（多为 Gateway 重启/断线）: {e}")
            print(f"[{ts}] {IB_RECONNECT_INTERVAL_SEC}s 后自动重连并继续，进程不退出")
            try:
                ib_disconnect()
            except Exception:
                pass
            time_module.sleep(IB_RECONNECT_INTERVAL_SEC)

    ib_disconnect()
    print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 已断开 IB Gateway")