import pandas as pd
from datetime import datetime, time, timedelta, date as date_type
import time as time_module
import os
import sys
import pytz
from decimal import Decimal
from dotenv import load_dotenv
import numpy as np
import sqlite3
import threading
import platform
from dataclasses import dataclass

from trend_er5_gate import history_days_back, apply_entry_gates_to_signal
from k_side_adjust import effective_k1_for_time, format_k_strategy_params
from ninjatrader_client import create_client_or_none, sanitize_file_tag

load_dotenv(override=True)

@dataclass(frozen=True)
class AccountRule:
    account_size: int
    profit_target: float
    max_loss: float
    max_micro_contracts: int  # 平台允许的 MNQ 上限
    trade_contracts: int       # 策略固定开仓手数（约合 1~1.5x 名义；不随行情/杠杆重算）
    minimum_trading_days: int = 0
    funded_micro_scaling: tuple = ()


@dataclass(frozen=True)
class FuturesProgram:
    key: str
    display_name: str
    model_name: str
    account_prefix: str
    rules: dict
    consistency_pct: float = 0.40
    no_daily_loss_limit: bool = True
    drawdown_lock_buffer: float = 100.0


FUNDEDNEXT_FLEX = FuturesProgram(
    key="fundednext",
    display_name="FundedNext Futures",
    model_name="Flex",
    account_prefix="FNFT",
    rules={
        50_000: AccountRule(50_000, 2_500, 1_500, 30, 1),
        100_000: AccountRule(100_000, 5_000, 2_500, 50, 2),
        150_000: AccountRule(150_000, 8_000, 4_000, 80, 3),
    },
)

TRADEIFY_SELECT_FLEX = FuturesProgram(
    key="tradeify",
    display_name="Tradeify",
    model_name="Select Flex",
    account_prefix="TDFY",
    rules={
        25_000: AccountRule(25_000, 1_500, 1_000, 10, 1, 3, ((0, 10),)),
        50_000: AccountRule(50_000, 3_000, 2_000, 40, 1, 3, ((0, 20), (1_500, 30), (2_000, 40))),
        100_000: AccountRule(100_000, 6_000, 3_000, 80, 2, 3, ((0, 30), (1_500, 40), (2_000, 50), (3_000, 80))),
        150_000: AccountRule(150_000, 9_000, 4_500, 120, 3, 3, ((0, 30), (1_500, 40), (2_000, 50), (3_000, 80), (4_500, 120))),
    },
)

ACTIVE_PROGRAM = FUNDEDNEXT_FLEX
ACTIVE_RULE = FUNDEDNEXT_FLEX.rules[100_000]
CURRENT_PHASE = "1"
COMPLETED_TRADING_DAYS = 0
HISTORICAL_BEST_DAY_PROFIT = 0.0

# ============================================================================
# 用户配置参数 - 请根据需要修改以下参数
# FundedNext Futures Flex（$50K/$100K/$150K）官方规则:
#   挑战阶段(1): 利润目标 5%（$100K 为 $5,000）| 最大亏损 2.5%（$2,500, EOD 追踪）
#                无日内亏损限制 | Consistency 40%（最佳单日利润 <= 总利润 40%）| 无时间限制
#   Funded:      最大亏损 2.5%（EOD 追踪）| 无 Consistency | 每 5 天可提款
#   合约上限: 5 Minis 或 50 Micros（MNQ）
# 回测结论（真实两年数据）: 不加日内止损、保留策略自带追踪止损；
# 仓位按账户规模固定 MNQ 手数（约合 1~1.5x 名义），不再用 CFD 式连续杠杆折算。
#
# 多账户并行: 一进程一账户。启动时交互输入 NT8 账户名与资金;
# tag 由账户名自动生成, 日志与信号库按 tag 隔离; 行情缓存与 NT8 incoming 共用。
# ============================================================================

# 信号计算品种（行情来自 longport_data_service 缓存, QQQ 与 NQ 高度同步）
SYMBOL = os.environ.get('SYMBOL', 'QQQ.US')

# NinjaTrader 8 ATI（账户在启动时交互输入; 合约写在此处, 换月时改）
NT8_ACCOUNT = ""  # 启动时交互输入: NT8 Accounts 标签页 Name 列（非 Display Name）
NT8_INSTRUMENT = "MNQ 09-26"  # ⚠️ 每季度换月手动更新（3/6/9/12 月）
NT8_INCOMING_DIR = None  # None = 默认 ~/Documents/NinjaTrader 8/incoming
INSTANCE_TAG = None  # 由账户名自动生成, 用于日志/DB/OIF 隔离

# 期货合约换算（仅用于盈亏估算）: MNQ 每点 $2, 名义 ≈ QQQ × NQ_QQQ_RATIO × $2
MNQ_POINT_VALUE = 2.0
NQ_QQQ_RATIO = float(os.environ.get('NQ_QQQ_RATIO', '41.45'))  # NQ 指数 / QQQ 价格（2026-07-08 校准: 29200/704.4）
MAX_CONTRACTS = ACTIVE_RULE.max_micro_contracts

# 资金和风控设置（启动时交互输入账户起始资金与当前金额，自动计算止盈/风控金额）
ACCOUNT_START_BALANCE = None  # 账户起始资金（启动时输入）
INITIAL_CAPITAL = None  # 账户当前金额（启动时输入）
PROFIT_TARGET_PCT = -1     # 当前轮次止盈比例（启动时根据输入轮次自动设置）
MAX_LOSS_AMOUNT = ACTIVE_RULE.max_loss
MAX_LOSS_BUFFER = 0.9      # 保险丝缓冲: 到官方线 90%（即 2.25%）即强制平仓, 防滑点击穿
CONSISTENCY_PCT = ACTIVE_PROGRAM.consistency_pct
TP_BUFFER_PCT = 0.005      # 止盈余量比例（按起始资金 0.5% 上调止盈目标, 覆盖手续费/滑点损耗）

# 止盈止损设置（金额）——启动时按上述比例自动计算，请勿手动修改
MAX_PROFIT_AMOUNT = -1  # 止盈目标金额（自动计算；负数表示未初始化/禁用）
MAX_DAILY_LOSS_AMOUNT = -1  # 日内止损: Flex 无日损限制且回测证明日内止损有害, 固定禁用
MAX_DAILY_PROFIT_AMOUNT = -1  # 单日利润上限: Flex 无此规则, 固定禁用
MAX_LOSS_FUSE_AMOUNT = -1  # 追踪回撤保险丝金额（自动计算 = 起始资金 × 2.5% × 90%）

# 交易时间设置（写死; 多开时各进程策略相同, 靠不同 NT8 账户隔离）
TRADING_START_TIME = (9, 40)  # 交易开始时间：9点40分
TRADING_END_TIME = (15, 40)   # 交易结束时间：15点40分
CHECK_INTERVAL_MINUTES = 15   # 检查间隔（分钟）
MAX_POSITIONS_PER_DAY = 10    # 每日最大开仓次数

# 策略参数
LOOKBACK_DAYS = 1  # 回看天数（用于计算噪声区域）
K1 = 1  # 上边界sigma乘数（多头基准）
K2 = 1.04  # 下边界sigma乘数（空头）
ENABLE_K_SIDE_ADJUSTMENT = True  # 午后收紧多头 K（午前1.0/午后0.9）；False=全天固定 K1

# VWAP开关：False=不使用VWAP作为入场/止损条件，True=使用VWAP
USE_VWAP = False
# er5/range1/sigma 开仓门控（与 Quantra/backtest 一致）：开关与阈值见 trend_er5_gate.py

# 🎯 动态追踪止盈配置（单笔浮盈回撤止盈，与 simulate_icmarkets 一致；触发后当日不再开仓）
ENABLE_TRAILING_TAKE_PROFIT = True   # 是否启用动态追踪止盈
TRAILING_TP_ACTIVATION_PCT = 0.006   # 激活追踪止盈的最低浮盈百分比（0.6%），追踪的是qqq
TRAILING_TP_CALLBACK_PCT = 0.65      # 保护的利润比例（65%），即从最大浮盈回撤35%时触发止盈

# 调试模式配置
DEBUG_MODE = False   # 设置为True开启调试模式（使用固定时间）
DEBUG_TIME = "2025-07-10 10:25:00"  # 调试使用的时间，格式: "YYYY-MM-DD HH:MM:SS"
DEBUG_ONCE = True  # 是否只运行一次就退出（仅在DEBUG_MODE=True时有效）
LOG_VERBOSE = False  # 设置为True开启详细日志（主循环/等待/时间精度等周期性输出）

# ============================================================================
# 程序内部变量 - 请勿手动修改
# ============================================================================

# 日志 / 信号库路径（启动确定 INSTANCE_TAG 后由 apply_instance_paths 赋值）
LOG_FILE = "trading_fundednext_futures.log"
DB_PATH = "trading_signals_fundednext_futures.db"

# 收益统计变量
TOTAL_PNL = 0.0  # 总收益（累计）
DAILY_PNL = 0.0  # 当日收益
LAST_STATS_DATE = None  # 上次统计日期
DAILY_TRADES = []  # 当日交易记录
DAILY_PNL_HISTORY = {}  # 每日已实现盈亏历史 {date: pnl}，用于 consistency 40% 检查
EOD_HIGH_WATER = None  # EOD 追踪高水位（每日收盘后用账户净值更新, 启动时=max(起始资金, 当前金额)）

# 止盈止损状态标志
DAILY_STOP_TRIGGERED = False  # 当日是否触发了日内止损（Flex 禁用, 保留字段兼容主循环）
DAILY_PROFIT_CAP_TRIGGERED = False  # 单日利润上限（Flex 禁用, 保留字段兼容主循环）
PROFIT_TARGET_TRIGGERED = False  # 挑战止盈是否触发；触发后永久停止开仓，需重启并切换 funded
TRAILING_FUSE_TRIGGERED = False  # 是否触发追踪回撤保险丝（触发后永久停止交易, 需人工介入）
DAILY_LOSS_MONITOR_ACTIVE = False  # 风控监控线程是否激活
FORCE_CLOSE_POSITION = False  # 强制平仓标志（监控线程设置）

# NinjaTrader ATI 客户端（主程序启动时初始化; None=仅记录信号模式）
NT8_CLIENT = None

# 线程锁，用于保护共享变量
pnl_lock = threading.Lock()

# 日志文件类 - 将输出同时写入控制台和文件
class Logger:
    def __init__(self, log_file):
        self.terminal = sys.stdout
        self.log_file = log_file
        log_dir = os.path.dirname(os.path.abspath(log_file))
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
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


def apply_instance_paths(tag):
    """按 tag 隔离日志与信号库; 行情缓存仍共用。"""
    global INSTANCE_TAG, LOG_FILE, DB_PATH
    INSTANCE_TAG = sanitize_file_tag(tag)
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    LOG_FILE = os.path.join(log_dir, f"{ACTIVE_PROGRAM.key}_futures_{INSTANCE_TAG}.log")
    DB_PATH = os.path.join(
        get_common_files_dir(),
        f"trading_signals_{ACTIVE_PROGRAM.key}_futures_{INSTANCE_TAG}.db",
    )
    return INSTANCE_TAG


def prompt_instance_identity():
    """交互输入 NT8 账户名; tag 由账户名自动生成。"""
    global NT8_ACCOUNT

    account = ""
    while not account:
        try:
            account = input("请输入 NT8 账户名（Accounts 标签页 Name 列, 必填）: ").strip()
        except EOFError:
            print("错误: 无法读取输入，程序退出")
            sys.exit(1)
        if not account:
            print("错误: 账户名不能为空")

    if ACTIVE_PROGRAM.account_prefix and not account.upper().startswith(ACTIVE_PROGRAM.account_prefix):
        confirm = input(
            f"警告: 该账户名不像 {ACTIVE_PROGRAM.display_name} 账户"
            f"（通常以 {ACTIVE_PROGRAM.account_prefix} 开头）。仍要继续? (yes/no): "
        ).strip().lower()
        if confirm not in ("y", "yes"):
            print("已取消，请用正确的平台启动脚本和账户名重新启动")
            sys.exit(1)

    NT8_ACCOUNT = account
    apply_instance_paths(account)
    print(f"实例账户: {NT8_ACCOUNT}")
    print(f"实例 tag(自动): {INSTANCE_TAG}")
    return INSTANCE_TAG


def prompt_capital_settings():
    """启动时交互输入考试轮次、账户起始资金与当前金额。"""
    global ACCOUNT_START_BALANCE, INITIAL_CAPITAL, MAX_PROFIT_AMOUNT, MAX_LOSS_FUSE_AMOUNT
    global PROFIT_TARGET_PCT, EOD_HIGH_WATER, MAX_CONTRACTS
    global MAX_LOSS_AMOUNT, ACTIVE_RULE, CURRENT_PHASE, COMPLETED_TRADING_DAYS
    global HISTORICAL_BEST_DAY_PROFIT

    while True:
        try:
            sizes = "/".join(f"{size // 1000}K" for size in ACTIVE_PROGRAM.rules)
            size_str = input(f"请输入账户规模（{sizes}）: ").strip().upper().replace("$", "")
            if size_str.endswith("K"):
                account_size = int(float(size_str[:-1]) * 1000)
            else:
                account_size = int(float(size_str.replace(",", "")))
            if account_size not in ACTIVE_PROGRAM.rules:
                print(f"错误: {ACTIVE_PROGRAM.model_name} 不支持该规模，请重新输入")
                continue

            phase = input(
                f"请输入当前轮次（1=挑战阶段({ACTIVE_PROGRAM.model_name}) / funded=已通过考试）: "
            ).strip().lower()
            if phase not in ("1", "funded"):
                print("错误: 轮次必须是 1 或 funded，请重新输入")
                continue

            start_balance = float(account_size)
            current_str = input(f"请输入账户当前金额（如 {account_size}）: ").strip()
            hw_str = input(
                "请输入账户历史 EOD 最高净值"
                "（首次运行直接回车=取起始/当前较大者；已锁定回撤可输入当前最高值）: "
            ).strip()
            current_balance = float(current_str)
            high_water = float(hw_str) if hw_str else max(start_balance, current_balance)
            if current_balance <= 0:
                print("错误: 金额必须大于 0，请重新输入")
                continue
            if high_water < max(start_balance, current_balance) * 0.999:
                print("错误: 历史最高净值不应低于起始资金/当前金额，请重新输入")
                continue

            existing_profit = max(0.0, current_balance - start_balance)
            if phase == "1":
                minimum_days = ACTIVE_PROGRAM.rules[account_size].minimum_trading_days
                if minimum_days > 0:
                    days_str = input(
                        f"请输入已完成的挑战交易日数（首次运行填 0；"
                        f"{ACTIVE_PROGRAM.model_name} 最少 {minimum_days} 天）: "
                    ).strip()
                    completed_days = int(days_str or "0")
                else:
                    completed_days = 0
                if existing_profit > 0:
                    best_day_str = input(
                        "请输入历史最佳单日盈利"
                        f"（直接回车按保守值 ${existing_profit:.2f} 计算）: "
                    ).strip()
                    historical_best_day = float(best_day_str) if best_day_str else existing_profit
                else:
                    historical_best_day = 0.0
            else:
                completed_days = 0
                historical_best_day = 0.0
            break
        except ValueError:
            print("错误: 输入格式不正确，请输入数字")
        except EOFError:
            print("错误: 无法读取输入，程序退出")
            sys.exit(1)

    ACTIVE_RULE = ACTIVE_PROGRAM.rules[account_size]
    CURRENT_PHASE = phase
    ACCOUNT_START_BALANCE = start_balance
    INITIAL_CAPITAL = current_balance
    MAX_LOSS_AMOUNT = ACTIVE_RULE.max_loss
    MAX_CONTRACTS = ACTIVE_RULE.max_micro_contracts
    COMPLETED_TRADING_DAYS = completed_days
    HISTORICAL_BEST_DAY_PROFIT = historical_best_day

    lock_high_water = start_balance + MAX_LOSS_AMOUNT + ACTIVE_PROGRAM.drawdown_lock_buffer
    EOD_HIGH_WATER = min(high_water, lock_high_water)
    PROFIT_TARGET_PCT = ACTIVE_RULE.profit_target / start_balance if phase == "1" else -1

    phase_label = {"1": f"挑战阶段({ACTIVE_PROGRAM.model_name})", "funded": "Funded(已通过)"}[phase]
    print(f"当前轮次: {phase_label}")
    print(
        f"固定开仓手数: {ACTIVE_RULE.trade_contracts} 张 MNQ"
        f"（账户规模 ${account_size:,.0f}；平台上限 {current_max_contracts()} 张）"
    )
    print(
        f"ℹ️ {ACTIVE_PROGRAM.model_name} 最大亏损 ${MAX_LOSS_AMOUNT:.0f}"
        f"（EOD 追踪，锁定底线 ${start_balance + ACTIVE_PROGRAM.drawdown_lock_buffer:.0f}）"
        f" | {'无' if ACTIVE_PROGRAM.no_daily_loss_limit else '有'}日内亏损限制"
        f" | 当前合约上限 {current_max_contracts()} 张 MNQ"
    )
    if phase == "1":
        print(
            f"ℹ️ 挑战期 Consistency: 最佳单日利润 ≤ 总利润 {CONSISTENCY_PCT*100:.0f}%"
            f" | 最少交易日 {ACTIVE_RULE.minimum_trading_days}"
        )
    print(f"账户起始资金: ${start_balance:.2f}")
    print(f"账户当前金额: ${current_balance:.2f}")
    print(f"EOD 高水位: ${high_water:.2f}")
    print(f"已有盈亏: ${current_balance - start_balance:+.2f}")

    if PROFIT_TARGET_PCT > 0:
        tp_buffer = start_balance * TP_BUFFER_PCT
        MAX_PROFIT_AMOUNT = ACTIVE_RULE.profit_target - (current_balance - start_balance) + tp_buffer
        print(
            f"账户剩余止盈金额: ${MAX_PROFIT_AMOUNT:.2f}"
            f" (= 官方目标 ${ACTIVE_RULE.profit_target:.0f} − 已有盈亏 + 余量 ${tp_buffer:.2f})"
        )
        if MAX_PROFIT_AMOUNT <= 0:
            print("警告: 当前金额已达到/超过账户止盈目标，无需继续交易，程序退出")
            sys.exit(0)
    else:
        MAX_PROFIT_AMOUNT = -1
        print("账户止盈: 已禁用（Funded 账户无利润目标）")

    MAX_LOSS_FUSE_AMOUNT = MAX_LOSS_AMOUNT * MAX_LOSS_BUFFER
    fuse_floor = EOD_HIGH_WATER - MAX_LOSS_FUSE_AMOUNT
    official_floor = EOD_HIGH_WATER - MAX_LOSS_AMOUNT
    print(
        f"追踪回撤保险丝: 净值 ≤ ${fuse_floor:.2f} 即强制全平停机"
        f" (官方违规线 ${official_floor:.2f}, 预留 ${MAX_LOSS_AMOUNT - MAX_LOSS_FUSE_AMOUNT:.2f})"
    )
    print("ℹ️ 日内止损: 已禁用（当前账户规则无 DLL）")


# SQLite / 公共目录工具（信号库路径由 apply_instance_paths 按 tag 设置）
def get_common_files_dir():
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

def init_sqlite_database():
    """启动时清空信号：DROP 后重建 signals 表，删除全部历史行（含 consumed=0），避免 EA 误执行旧信号。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS signals")
        
        # 创建简化的交易信号表
        cursor.execute("""
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,  -- BUY, SELL, CLOSE
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            consumed INTEGER DEFAULT 0
        )
        """)
        
        conn.commit()
        conn.close()
        ts = get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')
        abs_db = os.path.abspath(DB_PATH)
        print(f"[{ts}] SQLite 已清空旧信号并重建表（未消费记录已删除）")
        print(f"数据库路径: {abs_db}")
        print(f"[{ts}] 说明：期货模式下 SQLite 仅作信号留痕, 实际下单通过 NinjaTrader ATI 指令文件, 无需 MT5 EA。")
        
    except Exception as e:
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] SQLite数据库初始化失败: {str(e)}")

def write_signal_to_sqlite(action):
    """将交易信号写入SQLite数据库"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
        INSERT INTO signals (action)
        VALUES (?)
        """, (action.upper(),))
        
        conn.commit()
        signal_id = cursor.lastrowid
        conn.close()
        
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 信号已写入: {action} (ID: {signal_id})")
        return signal_id
        
    except Exception as e:
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 写入信号失败: {str(e)}")
        return None

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
    """模拟模式：不需要获取实际账户余额"""
    # 返回一个模拟的余额值
    return 10000.0

def get_current_positions():
    """模拟模式：返回空持仓"""
    # 模拟模式总是返回空持仓，让策略可以正常运行
    return {}

def calculate_pnl(entry_price, exit_price, direction, quantity=None):
    """
    按实际 MNQ 敞口计算盈亏。

    盈亏 = 手数 × MNQ名义价值 × 价格变动百分比 × 方向
    quantity 未传时按账户规则的固定手数估算。
    """
    if entry_price <= 0:
        return 0.0, 0.0

    price_change_pct = (exit_price - entry_price) / entry_price
    qty = abs(quantity) if quantity else ACTIVE_RULE.trade_contracts
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

def current_max_contracts():
    """返回当前阶段允许的 MNQ(Micro) 上限；Tradeify funded 按 EOD 权益阶梯扩容。"""
    if CURRENT_PHASE != "funded" or not ACTIVE_RULE.funded_micro_scaling:
        return ACTIVE_RULE.max_micro_contracts

    eod_profit = max(0.0, EOD_HIGH_WATER - ACCOUNT_START_BALANCE)
    limit = ACTIVE_RULE.funded_micro_scaling[0][1]
    for required_profit, micro_limit in ACTIVE_RULE.funded_micro_scaling:
        if eod_profit >= required_profit:
            limit = micro_limit
    return limit


def calculate_contract_qty(qqq_price=None):
    """按账户规模返回固定 MNQ 手数，不超过平台当前上限。

    50K→1 / 100K→2 / 150K→3（约合 1~1.5x 名义）；不再按杠杆连续折算。
    """
    qty = ACTIVE_RULE.trade_contracts
    return min(qty, current_max_contracts())

def submit_order(symbol, side, quantity, order_type="MO", price=None, outside_rth=None, is_close=False):
    """下单: 写 ATI 指令文件由 NinjaTrader 8 执行; 同时把信号写入 SQLite 留痕。
    开仓时 quantity 为 MNQ 手数; 平仓时忽略 quantity, 由 NT8 CLOSEPOSITION 平掉全部持仓。"""
    if is_close:
        action = "CLOSE"
    else:
        action = "BUY" if side == "Buy" else "SELL"
    signal_id = write_signal_to_sqlite(action)
    ts = get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')

    if NT8_CLIENT is None:
        return f"SIM_{signal_id}" if signal_id else "SIM_ERROR"

    try:
        if is_close:
            oif_name, _ = NT8_CLIENT.close_all()
            print(f"[{ts}] NT8 平仓指令已写入: CLOSEPOSITION 账户={NT8_CLIENT.account} ({oif_name})")
            return oif_name
        else:
            if quantity <= 0:
                print(f"[{ts}] 错误: 开仓手数为 0, 不下单")
                return "QTY_ERROR"
            oif_name = NT8_CLIENT.place_market_order(side, quantity)
            print(f"[{ts}] NT8 开仓指令已写入: {side} {quantity} 手 {NT8_CLIENT.instrument} 账户={NT8_CLIENT.account} ({oif_name})")
            return oif_name
    except Exception as e:
        print(f"[{ts}] ❌ NT8 指令写入失败: {str(e)}")
        if is_close:
            # 多账户并行时禁止 FLATTENEVERYTHING（会误伤其他账户）; 仅重试本账户 CLOSEPOSITION
            try:
                oif_name, _ = NT8_CLIENT.close_all()
                print(f"[{ts}] ⚠️ 平仓指令首次失败后已重试 CLOSEPOSITION 账户={NT8_CLIENT.account} ({oif_name})")
                return oif_name
            except Exception as e2:
                print(f"[{ts}] ❌❌❌ CLOSEPOSITION 重试仍失败: {str(e2)}")
                print(f"[{ts}] ❌❌❌ 严重: 平仓指令未能送达 NT8, 真实持仓可能仍然敞口, 请立即人工到 NT8 平仓!!!")
                print(f"[{ts}] ❌❌❌ 未使用 FLATTENEVERYTHING（多账户模式下会误平其他账户）")
        return "ATI_ERROR"

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
    注意：盈亏按实际 MNQ 手数名义计算
    """
    global DAILY_STOP_TRIGGERED, FORCE_CLOSE_POSITION, DAILY_LOSS_MONITOR_ACTIVE
    global DAILY_PNL, PROFIT_TARGET_TRIGGERED, TRAILING_FUSE_TRIGGERED
    
    print(
        f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"=== 风控监控线程已启动 ({ACTIVE_PROGRAM.display_name} {ACTIVE_PROGRAM.model_name}) ==="
    )
    if MAX_PROFIT_AMOUNT > 0:
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 止盈目标: ${MAX_PROFIT_AMOUNT:.2f} (需同时满足 consistency {CONSISTENCY_PCT*100:.0f}%)")
    else:
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 止盈: 已禁用")
    print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 追踪保险丝线: ${EOD_HIGH_WATER - MAX_LOSS_FUSE_AMOUNT:.2f} (EOD 高水位 ${EOD_HIGH_WATER:.2f} − ${MAX_LOSS_FUSE_AMOUNT:.2f})")
    print(
        f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"日内止损: 已禁用 | 固定手数: {ACTIVE_RULE.trade_contracts} 张 MNQ | 平台上限: {current_max_contracts()}"
    )
    
    while DAILY_LOSS_MONITOR_ACTIVE:
        try:
            now = get_us_eastern_time()
            current_hour = now.hour
            
            # 判断是否在交易时间内（9:30-16:00）
            is_trading_hours = (current_hour >= 10 or (current_hour == 9 and now.minute >= 30)) and current_hour < 16
            
            # 使用锁保护共享变量
            with pnl_lock:
                # 如果已经触发保险丝或止盈，停止监控
                if TRAILING_FUSE_TRIGGERED or PROFIT_TARGET_TRIGGERED:
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
                
                # ===== 追踪回撤保险丝（最高优先级）=====
                # 当前净值从启动时真实余额继续累计；违规线按所选平台/规模的固定美元回撤计算。
                current_equity = INITIAL_CAPITAL + current_total_pnl
                fuse_floor = EOD_HIGH_WATER - MAX_LOSS_FUSE_AMOUNT
                if MAX_LOSS_FUSE_AMOUNT > 0 and current_equity <= fuse_floor:
                    print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] !!!!! [监控线程] 触发追踪回撤保险丝 !!!!!")
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] 当前净值: ${current_equity:.2f} <= 保险丝线: ${fuse_floor:.2f}")
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] EOD 高水位: ${EOD_HIGH_WATER:.2f}, 官方违规线: ${EOD_HIGH_WATER - MAX_LOSS_AMOUNT:.2f}")
                    
                    with pnl_lock:
                        FORCE_CLOSE_POSITION = True
                        TRAILING_FUSE_TRIGGERED = True
                    
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] ⛔ 已设置强制平仓标志, 程序将永久停止开仓, 请人工评估账户状态")
                    break
                
                # ===== 止盈检查（需同时满足 consistency 40% 规则）=====
                if MAX_PROFIT_AMOUNT > 0 and current_total_pnl >= MAX_PROFIT_AMOUNT:
                    # consistency: 最佳单日利润 <= 总利润 × 40%（含今日, 用总盈亏含未实现做保守估计）
                    account_total_profit = (INITIAL_CAPITAL - ACCOUNT_START_BALANCE) + current_total_pnl
                    with pnl_lock:
                        best_day = max(
                            [HISTORICAL_BEST_DAY_PROFIT]
                            + list(DAILY_PNL_HISTORY.values())
                            + [current_daily_pnl, 0.0]
                        )
                    consistency_ok = (
                        account_total_profit <= 0
                        or best_day <= CONSISTENCY_PCT * account_total_profit
                    )
                    days_at_check = COMPLETED_TRADING_DAYS + (
                        1 if DAILY_TRADES or position_quantity != 0 else 0
                    )
                    minimum_days_ok = days_at_check >= ACTIVE_RULE.minimum_trading_days
                    if consistency_ok and minimum_days_ok:
                        print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] !!!!! [监控线程] 检测到达成止盈目标(consistency 已达标) !!!!!")
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] 累计总盈利: ${current_total_pnl:.2f} / 目标: ${MAX_PROFIT_AMOUNT:.2f}")
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] 最佳单日: ${best_day:.2f} ({best_day / account_total_profit * 100:.1f}% <= {CONSISTENCY_PCT*100:.0f}%)")
                        
                        with pnl_lock:
                            FORCE_CLOSE_POSITION = True
                            PROFIT_TARGET_TRIGGERED = True
                        
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] 已设置止盈平仓标志")
                        break
                    else:
                        ratio = best_day / account_total_profit * 100 if account_total_profit > 0 else 0
                        print(
                            f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [监控线程] "
                            f"⚠️ 已达利润目标但规则未达标: 最佳单日占比 {ratio:.1f}%"
                            f"（要求 ≤ {CONSISTENCY_PCT*100:.0f}%），交易日 "
                            f"{days_at_check}/{ACTIVE_RULE.minimum_trading_days}"
                        )
                
                status_parts = [f"当日盈亏: ${current_daily_pnl:+.2f}", f"累计盈亏: ${current_total_pnl:+.2f}"]
                
                if MAX_PROFIT_AMOUNT > 0:
                    profit_remain = MAX_PROFIT_AMOUNT - current_total_pnl
                    status_parts.append(f"距止盈: ${profit_remain:.2f}")
                
                status_parts.append(f"距保险丝: ${current_equity - fuse_floor:.2f}")
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


def wait_seconds_until_next_session(now, trading_start_time, calendar_stale=False):
    """非交易日等待时长：日历过期短间隔重试；否则睡到下一美东自然日的开盘时间。

    避免固定睡 12 小时导致周日晚启动后周一开盘仍在睡眠、错过早盘。
    """
    if calendar_stale:
        return 3600  # 1 小时

    next_day = now.date() + timedelta(days=1)
    next_check = datetime.combine(
        next_day,
        time(trading_start_time[0], trading_start_time[1]),
        tzinfo=now.tzinfo,
    )
    return max((next_check - now).total_seconds(), 60)

def run_trading_strategy(symbol=SYMBOL, check_interval_minutes=CHECK_INTERVAL_MINUTES,
                        trading_start_time=TRADING_START_TIME, trading_end_time=TRADING_END_TIME,
                        max_positions_per_day=MAX_POSITIONS_PER_DAY, lookback_days=LOOKBACK_DAYS):
    global TOTAL_PNL, DAILY_PNL, LAST_STATS_DATE, DAILY_TRADES, DAILY_STOP_TRIGGERED, PROFIT_TARGET_TRIGGERED
    global MAX_DAILY_LOSS_AMOUNT, DAILY_LOSS_MONITOR_ACTIVE, FORCE_CLOSE_POSITION, DAILY_PROFIT_CAP_TRIGGERED
    global TRAILING_FUSE_TRIGGERED, DAILY_PNL_HISTORY, EOD_HIGH_WATER, COMPLETED_TRADING_DAYS
    
    now_et = get_us_eastern_time()
    print(f"启动交易策略 - 交易品种: {symbol}")
    print(f"当前美东时间: {now_et.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"交易时间: {trading_start_time[0]:02d}:{trading_start_time[1]:02d} - {trading_end_time[0]:02d}:{trading_end_time[1]:02d}")
    print(f"每日最大开仓次数: {max_positions_per_day}")
    if DEBUG_MODE:
        print(f"调试模式已开启! 使用时间: {now_et.strftime('%Y-%m-%d %H:%M:%S')}")
        if DEBUG_ONCE:
            print("单次运行模式已开启，策略将只运行一次")
    
    # 模拟模式：不需要获取实际账户余额
    # initial_capital = get_account_balance()
    # if initial_capital <= 0:
    #     print("Error: Could not get account balance or balance is zero")
    #     sys.exit(1)
    
    # 初始化持仓状态
    position_quantity = 0
    entry_price = None
    
    current_stop = None
    positions_opened_today = 0
    last_date = None
    outside_rth_setting = None  # 期货无盘前盘后概念, 此参数仅为兼容 submit_order 签名
    
    # 持仓数据字典（供监控线程使用）
    position_data = {
        'quantity': 0,
        'entry_price': None
    }
    
    # 监控线程对象
    monitor_thread = None
    
    # 🎯 动态追踪止盈状态变量
    max_profit_price = None         # 持仓期间的最优价格（多头：最高价，空头：最低价）
    trailing_tp_activated = False   # 追踪止盈是否已激活
    trailing_tp_day_stop = False    # 当日是否已因追踪止盈平仓（触发后当日不再开仓）
    last_processed_trigger = None    # 已处理的触发点(date, k_h, k_m)，用于触发窗口内去重，避免空转刷屏
    
    while True:
        now = get_us_eastern_time()
        current_date = now.date()
        if LOG_VERBOSE:
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S.%f')}] 主循环开始 (精确时间)")
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 时间精度: 秒={now.second}, 微秒={now.microsecond}")
        
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
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 强制平仓信号已发送，ID: {close_order_id}")
            
            # 计算盈亏（全仓计算）
            if entry_price and current_price > 0:
                direction = 1 if position_quantity > 0 else -1
                pnl, pnl_pct = calculate_pnl(entry_price, current_price, direction, position_quantity)
                with pnl_lock:
                    DAILY_PNL += pnl
                    TOTAL_PNL += pnl
                # 记录平仓交易（根据是止盈还是止损区分）
                if PROFIT_TARGET_TRIGGERED:
                    action_type = "平仓(止盈)"
                elif TRAILING_FUSE_TRIGGERED:
                    action_type = "平仓(回撤保险丝)"
                else:
                    action_type = "平仓(止损)"
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
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓信号已发送，ID: {close_order_id}")
                
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
            wait_seconds = wait_seconds_until_next_session(
                now, trading_start_time, calendar_stale=calendar_stale
            )
            next_check_time = now + timedelta(seconds=wait_seconds)
            print(
                f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] "
                f"{wait_seconds / 3600:.1f} 小时后重新检查"
                f"（目标 {next_check_time.strftime('%Y-%m-%d %H:%M:%S')}）"
            )
            time_module.sleep(wait_seconds)
            continue
            
        # 检查是否是新交易日，如果是则重置今日开仓计数
        if last_date is not None and current_date != last_date:
            positions_opened_today = 0
            DAILY_STOP_TRIGGERED = False  # 重置日内止损标志
            DAILY_PROFIT_CAP_TRIGGERED = False  # 保留字段, Flex 不使用
            # ⚠️ 保险丝/挑战止盈均不重置：前者需人工介入，后者需切换到 funded 后重启。
            FORCE_CLOSE_POSITION = False  # 重置强制平仓标志
            trailing_tp_day_stop = False  # 🎯 重置追踪止盈当日停止开仓标志
            
            # 记录前一日已实现盈亏（consistency 40% 统计用）
            with pnl_lock:
                DAILY_PNL_HISTORY[last_date] = DAILY_PNL
                if DAILY_TRADES:
                    COMPLETED_TRADING_DAYS += 1
                # EOD 追踪高水位从启动时真实余额继续累计，并在官方锁定点停止上移。
                eod_equity = INITIAL_CAPITAL + TOTAL_PNL
                lock_high_water = (
                    ACCOUNT_START_BALANCE
                    + MAX_LOSS_AMOUNT
                    + ACTIVE_PROGRAM.drawdown_lock_buffer
                )
                eod_equity = min(eod_equity, lock_high_water)
                if eod_equity > EOD_HIGH_WATER:
                    EOD_HIGH_WATER = eod_equity
                    print(
                        f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"EOD 高水位更新: ${EOD_HIGH_WATER:.2f}, "
                        f"新保险丝线: ${EOD_HIGH_WATER - MAX_LOSS_FUSE_AMOUNT:.2f}, "
                        f"交易日: {COMPLETED_TRADING_DAYS}"
                    )
            
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
        monitor_needed = is_after_930 and current_hour < 16 and not (PROFIT_TARGET_TRIGGERED or TRAILING_FUSE_TRIGGERED)
        if monitor_needed and (monitor_thread is None or not monitor_thread.is_alive()):
            print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] === 初始化日内止损监控 ===")
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 初始资金: ${INITIAL_CAPITAL:.2f}")
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 当日盈亏: ${DAILY_PNL:+.2f}")
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 累计盈亏: ${TOTAL_PNL:+.2f}")
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 固定手数: {ACTIVE_RULE.trade_contracts} 张 MNQ")
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
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓信号已发送，ID: {close_order_id}")
            
            # 计算盈亏（全仓计算）
            if entry_price:
                direction = 1 if position_quantity > 0 else -1
                pnl, pnl_pct = calculate_pnl(entry_price, current_price, direction, position_quantity)
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓成功: {side} {symbol} 出场价: {current_price}")
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
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓成功: {side} {symbol} 出场价: {current_price}")
                
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
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓信号已发送，ID: {close_order_id}")
                
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
            time_module.sleep(wait_seconds)
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
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓信号已发送，ID: {close_order_id}")
                    
                    # 计算盈亏（全仓计算）
                    if entry_price:
                        direction = 1 if position_quantity > 0 else -1
                        pnl, pnl_pct = calculate_pnl(entry_price, exit_price, direction, position_quantity)
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓成功: {side} {symbol} 出场价: {exit_price}")
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
            
            # ⛔ 追踪回撤保险丝已触发: 永久停止开仓（需人工评估后重启程序）
            if TRAILING_FUSE_TRIGGERED:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] ⛔ 追踪回撤保险丝已触发，程序不再开仓，请人工评估账户状态")
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
                # 保留交易信号日志，并添加VWAP和上下界信息
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 触发{'多' if signal == 1 else '空'}头入场信号!")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 当前价格: {price}, VWAP: {latest_row['VWAP']:.4f}, 上界: {latest_row['UpperBound']:.4f}, 下界: {latest_row['LowerBound']:.4f}, 止损: {stop}")
                
                # 期货模式：按账户规模固定 MNQ 手数，经 NT8 ATI 下单；盈亏按实际手数名义计算
                side = "Buy" if signal > 0 else "Sell"
                contract_qty = calculate_contract_qty(latest_price)
                if contract_qty <= 0:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 手数计算为 0，跳过本次开仓")
                    continue
                order_id = submit_order(symbol, side, contract_qty, outside_rth=outside_rth_setting)
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 信号已发送，ID: {order_id}")
                
                # 记录持仓：符号表示方向，绝对值为 MNQ 手数
                position_quantity = contract_qty if signal > 0 else -contract_qty
                entry_price = latest_price
                actual_notional = contract_qty * entry_price * NQ_QQQ_RATIO * MNQ_POINT_VALUE
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 开仓信号: {side} {symbol} 入场价: {entry_price}")
                print(
                    f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 仓位: {contract_qty} 手 MNQ "
                    f"≈ ${actual_notional:.0f} 名义 "
                    f"(规模 ${ACTIVE_RULE.account_size:,.0f} 固定手数, 平台上限 {current_max_contracts()} 手)"
                )
                
                # 记录开仓交易
                DAILY_TRADES.append({
                    "time": now.strftime('%Y-%m-%d %H:%M:%S'),
                    "action": "开仓",
                    "side": side,
                    "entry_price": entry_price,
                    "pnl": None  # 开仓时还没有盈亏
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
            time_module.sleep(sleep_seconds)

def run_application(program):
    global ACTIVE_PROGRAM, ACTIVE_RULE, CONSISTENCY_PCT, NT8_CLIENT

    ACTIVE_PROGRAM = program
    ACTIVE_RULE = next(iter(program.rules.values()))
    CONSISTENCY_PCT = program.consistency_pct

    # 先确定账户/tag 与隔离路径, 再接管 stdout 写日志
    print("\n" + "=" * 60)
    print(
        f"{ACTIVE_PROGRAM.display_name} {ACTIVE_PROGRAM.model_name} "
        "交易策略启动 (NinjaTrader ATI, MNQ, 多账户可并行)"
    )
    print("=" * 60)
    print("\n--- 实例身份 (一进程一账户) ---")
    prompt_instance_identity()

    sys.stdout = Logger(LOG_FILE)
    sys.stderr = sys.stdout

    print("\n" + "=" * 60)
    print(f"实例 tag={INSTANCE_TAG} | NT8账户={NT8_ACCOUNT}")
    print("=" * 60)

    print("\n--- 账户资金设置 ---")
    prompt_capital_settings()

    print("版本: 2.0.0 (multi-platform / multi-account)")
    print(f"平台: {ACTIVE_PROGRAM.display_name} | 模型: {ACTIVE_PROGRAM.model_name}")
    print("时间:", get_us_eastern_time().strftime("%Y-%m-%d %H:%M:%S"), "(美东时间)")
    print(f"实例 tag: {INSTANCE_TAG}")
    print(f"NT8 账户: {NT8_ACCOUNT}")
    print(f"NT8 合约: {NT8_INSTRUMENT}")
    print(f"日志文件: {os.path.abspath(LOG_FILE)}")
    print(f"信号数据库: {os.path.abspath(DB_PATH)}")
    print(f"行情缓存数据库(共用): {os.path.abspath(MARKET_DATA_DB_PATH)}")

    print("\n--- 用户配置参数 ---")
    print(f"信号品种: {SYMBOL} (行情) → 交易合约: MNQ (NinjaTrader ATI)")
    print(f"NQ/QQQ 换算比例: {NQ_QQQ_RATIO} (请定期核对, 会随分红缓慢漂移)")
    print(f"账户起始资金: ${ACCOUNT_START_BALANCE:.2f}")
    print(f"账户当前金额(仓位基准): ${INITIAL_CAPITAL:.2f}")
    print(
        f"固定开仓手数: {ACTIVE_RULE.trade_contracts} 张 MNQ "
        f"(规模 ${ACTIVE_RULE.account_size:,.0f}; 平台上限 {current_max_contracts()} 手)"
    )
    print(f"止盈目标: ${MAX_PROFIT_AMOUNT:.2f} ({'已禁用' if MAX_PROFIT_AMOUNT <= 0 else '已启用, 需 consistency 达标'})")
    print(f"追踪回撤保险丝: ${MAX_LOSS_FUSE_AMOUNT:.2f} (官方最大回撤 ${MAX_LOSS_AMOUNT:.2f})")
    print("日内止损: 已禁用（当前模型无 DLL）")
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

    if not ensure_market_data_service_available():
        sys.exit(1)

    init_sqlite_database()

    print("\n--- NinjaTrader ATI 连接 ---")
    NT8_CLIENT = create_client_or_none(
        NT8_ACCOUNT, NT8_INSTRUMENT, NT8_INCOMING_DIR, file_tag=INSTANCE_TAG
    )
    if NT8_CLIENT is not None:
        print("⚠️ 提醒: ATI 文件接口无法查询持仓, 本程序假设启动时该账户无持仓; 若 NT8 中已有持仓请先手动平掉")
        print("⚠️ 提醒: 每季度合约换月时, 请更新本文件顶部的 NT8_INSTRUMENT (如 MNQ 09-26 → MNQ 12-26)")
        print("⚠️ 提醒: 多账户并行时仅对本账户发 CLOSEPOSITION, 不会使用 FLATTENEVERYTHING")

    run_trading_strategy(
        symbol=SYMBOL,
        check_interval_minutes=CHECK_INTERVAL_MINUTES,
        trading_start_time=TRADING_START_TIME,
        trading_end_time=TRADING_END_TIME,
        max_positions_per_day=MAX_POSITIONS_PER_DAY,
        lookback_days=LOOKBACK_DAYS
    )


if __name__ == "__main__":
    # 兼容旧启动方式：旧文件始终启动 FundedNext Flex。
    run_application(FUNDEDNEXT_FLEX)
