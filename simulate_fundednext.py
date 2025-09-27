import pandas as pd
from datetime import datetime, time, timedelta, date as date_type
import time as time_module
import os
import sys
import pytz
from math import floor
from decimal import Decimal
from dotenv import load_dotenv
import numpy as np
import sqlite3

from longport.openapi import Config, TradeContext, QuoteContext, Period, OrderSide, OrderType, TimeInForceType, AdjustType, OutsideRTH

load_dotenv(override=True)

# 固定配置参数
CHECK_INTERVAL_MINUTES = 15
TRADING_START_TIME = (9, 39)  # 交易开始时间：9点40分
TRADING_END_TIME = (15, 39)   # 交易结束时间：15点40分
MAX_POSITIONS_PER_DAY = 10
LOOKBACK_DAYS = 1
LEVERAGE = 4 # 杠杆倍数，默认为1倍
K1 = 1 # 上边界sigma乘数
K2 = 1 # 下边界sigma乘数
FIXED_POSITION_SIZE = 100  # 模拟模式固定下单量

# 默认交易品种
SYMBOL = os.environ.get('SYMBOL', 'QQQ.US')

# 日志和调试模式配置（分离两个功能）
LOG_VERBOSE = True   # 设置为True开启详细日志打印
DEBUG_MODE = False   # 设置为True开启调试模式（使用固定时间）
DEBUG_TIME = "2025-07-10 10:25:00"  # 调试使用的时间，格式: "YYYY-MM-DD HH:MM:SS"
DEBUG_ONCE = True  # 是否只运行一次就退出（仅在DEBUG_MODE=True时有效）

# 收益统计全局变量
TOTAL_PNL = 0.0  # 总收益
DAILY_PNL = 0.0  # 当日收益
LAST_STATS_DATE = None  # 上次统计日期
DAILY_TRADES = []  # 当日交易记录
DAILY_STOP_TRIGGERED = False  # 当日是否触发了日内止损
INITIAL_CAPITAL = 10000.0  # 初始资金（用于计算亏损百分比）
MAX_DAILY_LOSS_PCT = 0.04  # 日内最大亏损百分比（4%）

# SQLite数据库路径 - 使用MT5通用目录
import platform
if platform.system() == "Windows":
    # Windows系统：使用MT5通用目录
    appdata_path = os.environ.get('APPDATA', os.path.expanduser('~\\AppData\\Roaming'))
    mt5_common_path = os.path.join(appdata_path, "MetaQuotes", "Terminal", "Common", "Files")
    os.makedirs(mt5_common_path, exist_ok=True)
    DB_PATH = os.path.join(mt5_common_path, "trading_signals_fundednext.db")
else:
    # 非Windows系统：使用当前目录
    DB_PATH = "trading_signals_fundednext.db"

def init_sqlite_database():
    """初始化SQLite数据库，每次启动时清空所有数据"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # 先删除现有表（如果存在）
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
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] SQLite数据库初始化成功（已清空历史数据）")
        print(f"数据库路径: {os.path.abspath(DB_PATH)}")
        
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

def create_contexts():
    max_retries = 5
    retry_delay = 5  # 秒
    
    for attempt in range(max_retries):
        try:
            config = Config.from_env()
            quote_ctx = QuoteContext(config)
            trade_ctx = TradeContext(config)
            if LOG_VERBOSE:
                print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] API连接成功")
            return quote_ctx, trade_ctx
        except Exception as e:
            if attempt < max_retries - 1:
                if LOG_VERBOSE:
                    print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] API连接失败 ({attempt + 1}/{max_retries}): {str(e)}")
                    print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] {retry_delay}秒后重试...")
                time_module.sleep(retry_delay)
            else:
                print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] API连接失败，已达最大重试次数")
                raise

QUOTE_CTX, TRADE_CTX = create_contexts()


def get_account_balance():
    """模拟模式：不需要获取实际账户余额"""
    # 返回一个模拟的余额值
    return 10000.0

def get_current_positions():
    """模拟模式：返回空持仓"""
    # 模拟模式总是返回空持仓，让策略可以正常运行
    return {}

def get_historical_data(symbol, days_back=None):
    # 简化天数计算逻辑
    if days_back is None:
        days_back = LOOKBACK_DAYS + 5  # 简化为固定天数
        
    # 直接使用1分钟K线
    sdk_period = Period.Min_1
    adjust_type = AdjustType.ForwardAdjust
    eastern = pytz.timezone('US/Eastern')
    now_et = get_us_eastern_time()
    current_date = now_et.date()
    
    if LOG_VERBOSE:
        print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 开始获取历史数据: {symbol}")
    
    # 计算起始日期
    start_date = current_date - timedelta(days=days_back)
    
    # 对于1分钟数据使用按日获取的方式
    all_candles = []
    
    # 尝试从今天开始向前获取足够的数据
    date_to_check = current_date
    api_call_count = 0
    while date_to_check >= start_date:
        # 跳过周末（周六=5, 周日=6）
        if date_to_check.weekday() >= 5:
            if LOG_VERBOSE:
                print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 跳过周末: {date_to_check}")
            date_to_check -= timedelta(days=1)
            continue
        day_start_time = datetime.combine(date_to_check, time(9, 30))
        day_start_time_et = eastern.localize(day_start_time)
        
        # 添加API调用间隔控制
        if api_call_count > 0:
            time_module.sleep(0.2)  # 200毫秒延迟，避免触发限流
        
        # 重试机制
        max_retries = 3
        retry_delay = 1
        day_candles = None
        
        for attempt in range(max_retries):
            try:
                # 使用history_candlesticks_by_date方法（与backtest数据源一致）
                # 这个方法返回的是完整交易日的数据，避免了by_offset方法可能的日期错误
                day_candles = QUOTE_CTX.history_candlesticks_by_date(
                    symbol, sdk_period, adjust_type,
                    date_to_check,  # 开始日期
                    date_to_check   # 结束日期（同一天）
                )
                api_call_count += 1
                break  # 成功则跳出重试循环
            except Exception as e:
                if "rate limit" in str(e).lower():
                    if attempt < max_retries - 1:
                        if LOG_VERBOSE:
                            print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] API限流，等待 {retry_delay} 秒后重试 ({attempt + 1}/{max_retries})")
                        time_module.sleep(retry_delay)
                        retry_delay *= 2  # 指数退避
                    else:
                        if LOG_VERBOSE:
                            print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] API限流，已达最大重试次数")
                        raise
                else:
                    raise  # 其他错误直接抛出
        
        if day_candles:
            all_candles.extend(day_candles)
            if LOG_VERBOSE:
                print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 获取 {date_to_check} 数据: {len(day_candles)} 条")
            
        date_to_check -= timedelta(days=1)
    
    # 处理数据并去重
    data = []
    processed_timestamps = set()
    
    for candle in all_candles:
        timestamp = candle.timestamp
        if isinstance(timestamp, datetime):
            ts_str = timestamp.strftime('%Y-%m-%d %H:%M:%S')
        else:
            ts_str = str(timestamp)
            
        # 去重处理
        if ts_str in processed_timestamps:
            continue
        processed_timestamps.add(ts_str)
        
        # 标准化时区
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                hour = timestamp.hour
                if symbol.endswith(".US") and 9 <= hour < 17:
                    dt = eastern.localize(timestamp)
                elif symbol.endswith(".US") and (hour >= 21 or hour < 5):
                    beijing = pytz.timezone('Asia/Shanghai')
                    dt = beijing.localize(timestamp).astimezone(eastern)
                else:
                    utc = pytz.utc
                    dt = utc.localize(timestamp).astimezone(eastern)
            else:
                dt = timestamp.astimezone(eastern)
        else:
            dt = datetime.fromtimestamp(timestamp, eastern)
        
        # 过滤未来日期
        if dt.date() > current_date:
            continue
            
        # 添加到数据列表
        data.append({
            "Close": float(candle.close),
            "Open": float(candle.open),
            "High": float(candle.high),
            "Low": float(candle.low),
            "Volume": float(candle.volume),
            "Turnover": float(candle.turnover),
            "DateTime": dt
        })
    
    # 转换为DataFrame并进行后处理
    df = pd.DataFrame(data)
    if df.empty:
        return df
        
    df["Date"] = df["DateTime"].dt.date
    df["Time"] = df["DateTime"].dt.strftime('%H:%M')
    
    # 过滤交易时间
    if symbol.endswith(".US"):
        df = df[df["Time"].between("09:30", "16:00")]
        
    # 去除重复数据
    df = df.drop_duplicates(subset=['Date', 'Time'])
    
    # 过滤掉未来日期的数据（双重保险）
    df = df[df["Date"] <= current_date]
    
    # 过滤周末数据（双重保险）
    weekday_mask = df["Date"].apply(lambda x: x.weekday() < 5 if isinstance(x, date_type) else True)
    df = df[weekday_mask]
    
    if LOG_VERBOSE and not df.empty:
        unique_dates = sorted(df["Date"].unique())
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 最终数据包含的日期: {unique_dates}")
    
    return df

def get_quote(symbol):
    quotes = QUOTE_CTX.quote([symbol])
    quote_data = {
        "symbol": quotes[0].symbol,
        "last_done": str(quotes[0].last_done),
        "open": str(quotes[0].open),
        "high": str(quotes[0].high),
        "low": str(quotes[0].low),
        "volume": str(quotes[0].volume),
        "turnover": str(quotes[0].turnover),
        "timestamp": quotes[0].timestamp.isoformat()
    }
    return quote_data

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
            # 使用时间点特定的sigma计算上下边界，应用K1和K2乘数
            upper_bound = upper_ref * (1 + K1 * sigma)
            lower_bound = lower_ref * (1 - K2 * sigma)
            
            # 更新df中的边界值
            df.loc[(df["Date"] == target_date) & (df["Time"] == tm), "UpperBound"] = upper_bound
            df.loc[(df["Date"] == target_date) & (df["Time"] == tm), "LowerBound"] = lower_bound
    
    return df

def submit_order(symbol, side, quantity, order_type="MO", price=None, outside_rth=None):
    # 将下单改为写入数据库
    action = "BUY" if side == "Buy" else "SELL"
    signal_id = write_signal_to_sqlite(action)
    
    # 返回一个模拟的订单ID
    return f"SIM_{signal_id}" if signal_id else "SIM_ERROR"

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
        # 检查上边界或VWAP是否为None
        if upper is None or vwap is None:
            # 如果已有止损，继续使用
            if current_stop is not None:
                new_stop = current_stop
                exit_signal = price < new_stop
                return exit_signal, new_stop
            else:
                return False, current_stop
        else:
            # 直接使用当前时刻的止损水平，不考虑历史止损
            new_stop = max(upper, vwap)
            
        exit_signal = price < new_stop
        return exit_signal, new_stop
    elif position_quantity < 0:
        # 检查下边界或VWAP是否为None
        if lower is None or vwap is None:
            # 如果已有止损，继续使用
            if current_stop is not None:
                new_stop = current_stop
                exit_signal = price > new_stop
                return exit_signal, new_stop
            else:
                return False, current_stop
        else:
            # 直接使用当前时刻的止损水平，不考虑历史止损
            new_stop = min(lower, vwap)
            
        exit_signal = price > new_stop
        return exit_signal, new_stop
    return False, None

def is_trading_day(symbol=None):
    market = None
    if symbol:
        if symbol.endswith(".US"):
            market = "US"
        elif symbol.endswith(".HK"):
            market = "HK"
        elif symbol.endswith(".SH") or symbol.endswith(".SZ"):
            market = "CN"
        elif symbol.endswith(".SG"):
            market = "SG"
    if not market:
        market = "US"
    now_et = get_us_eastern_time()
    current_date = now_et.date()
    from longport.openapi import Market
    market_mapping = {
        "US": Market.US, "HK": Market.HK, "CN": Market.CN, "SG": Market.SG
    }
    sdk_market = market_mapping.get(market, Market.US)
    calendar_resp = QUOTE_CTX.trading_days(
        sdk_market, current_date, current_date
    )
    trading_dates = calendar_resp.trading_days
    half_trading_dates = calendar_resp.half_trading_days
    is_trade_day = current_date in trading_dates
    is_half_trade_day = current_date in half_trading_dates
    return is_trade_day or is_half_trade_day

def run_trading_strategy(symbol=SYMBOL, check_interval_minutes=CHECK_INTERVAL_MINUTES,
                        trading_start_time=TRADING_START_TIME, trading_end_time=TRADING_END_TIME,
                        max_positions_per_day=MAX_POSITIONS_PER_DAY, lookback_days=LOOKBACK_DAYS):
    global TOTAL_PNL, DAILY_PNL, LAST_STATS_DATE, DAILY_TRADES, DAILY_STOP_TRIGGERED
    
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
    outside_rth_setting = OutsideRTH.AnyTime
    
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
        
        # 如果持仓量变为0，重置入场价格
        if position_quantity == 0:
            entry_price = None
        
        # 检查是否是交易日（调试模式下保持原有逻辑）
        is_today_trading_day = is_trading_day(symbol)
        if LOG_VERBOSE:
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 是否交易日: {is_today_trading_day}")
            
        if not is_today_trading_day:
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 今天不是交易日，跳过交易")
            if position_quantity != 0:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 非交易日，执行平仓")
                
                # 获取当前价格用于计算盈亏
                quote = get_quote(symbol)
                current_price = float(quote.get("last_done", 0))
                
                side = "Sell" if position_quantity > 0 else "Buy"
                close_order_id = submit_order(symbol, side, abs(position_quantity), outside_rth=outside_rth_setting)
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓订单已提交，ID: {close_order_id}")
                
                # 计算盈亏
                if entry_price and current_price > 0:
                    pnl = (current_price - entry_price) * (1 if position_quantity > 0 else -1) * abs(position_quantity)
                    DAILY_PNL += pnl
                    TOTAL_PNL += pnl
                    # 记录平仓交易
                    DAILY_TRADES.append({
                        "time": now.strftime('%Y-%m-%d %H:%M:%S'),
                        "action": "平仓",
                        "side": side,
                        "quantity": abs(position_quantity),
                        "price": current_price,
                        "pnl": pnl
                    })
                    
                position_quantity = 0
                entry_price = None
                
                # 在交易日结束时打印当日所有交易记录
                if DAILY_TRADES:
                    print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] ===== 当日交易记录 =====")
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
                    print(f"  当日盈亏: ${DAILY_PNL:+.2f}")
                    print(f"  累计盈亏: ${TOTAL_PNL:+.2f}")
                    print("=" * 50)
            next_check_time = now + timedelta(hours=12)
            wait_seconds = (next_check_time - now).total_seconds()
            time_module.sleep(wait_seconds)
            continue
            
        # 检查是否是新交易日，如果是则重置今日开仓计数
        if last_date is not None and current_date != last_date:
            positions_opened_today = 0
            DAILY_STOP_TRIGGERED = False  # 重置日内止损标志
            
            # 打印前一日交易记录
            if DAILY_TRADES:
                print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] ===== 前一日交易记录 ({last_date}) =====")
                for i, trade in enumerate(DAILY_TRADES, 1):
                    print(f"交易 #{i}:")
                    print(f"  时间: {trade['time']}")
                    print(f"  操作: {trade['action']} {trade['side']} {trade['quantity']} 股")
                    print(f"  价格: ${trade['price']:.2f}")
                    if trade['pnl'] is not None:
                        print(f"  盈亏: ${trade['pnl']:+.2f}")
                
                # 计算前一日统计
                total_trades = len([t for t in DAILY_TRADES if t['action'] == '平仓'])
                winning_trades = len([t for t in DAILY_TRADES if t['action'] == '平仓' and t['pnl'] > 0])
                losing_trades = len([t for t in DAILY_TRADES if t['action'] == '平仓' and t['pnl'] < 0])
                
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
                wait_seconds = min(wait_seconds, 300)  # 最多等待5分钟
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
            close_order_id = submit_order(symbol, side, abs(position_quantity), outside_rth=outside_rth_setting)
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓订单已提交，ID: {close_order_id}")
            
            # 计算盈亏
            if entry_price:
                pnl = (current_price - entry_price) * (1 if position_quantity > 0 else -1) * abs(position_quantity)
                pnl_pct = (current_price / entry_price - 1) * 100 * (1 if position_quantity > 0 else -1)
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓成功: {side} {abs(position_quantity)} {symbol} 价格: {current_price}")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 交易结果: {'盈利' if pnl > 0 else '亏损'} ${abs(pnl):.2f} ({pnl_pct:.2f}%)")
                # 更新收益统计
                DAILY_PNL += pnl
                TOTAL_PNL += pnl
                # 记录平仓交易
                DAILY_TRADES.append({
                    "time": now.strftime('%Y-%m-%d %H:%M:%S'),
                    "action": "平仓",
                    "side": side,
                    "quantity": abs(position_quantity),
                    "price": current_price,
                    "pnl": pnl
                })
            else:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓成功: {side} {abs(position_quantity)} {symbol} 价格: {current_price}")
                
            position_quantity = 0
            entry_price = None
            
            # 在交易日结束时打印当日所有交易记录
            if DAILY_TRADES:
                print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] ===== 当日交易记录 =====")
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
        
        # 如果有持仓且未触发日内止损，先进行日内止损检查
        if position_quantity != 0 and not DAILY_STOP_TRIGGERED:
            # 获取当前价格
            quote = get_quote(symbol)
            current_price = float(quote.get("last_done", 0))
            
            # 计算当前未实现盈亏
            if entry_price and current_price > 0:
                unrealized_pnl = (current_price - entry_price) * (1 if position_quantity > 0 else -1) * abs(position_quantity)
                total_current_pnl = DAILY_PNL + unrealized_pnl
                total_loss_pct = abs(total_current_pnl / INITIAL_CAPITAL) if total_current_pnl < 0 else 0
            else:
                unrealized_pnl = 0
                total_current_pnl = DAILY_PNL
                total_loss_pct = abs(DAILY_PNL / INITIAL_CAPITAL) if DAILY_PNL < 0 else 0
            
            # 每分钟打印一次检查信息（如果详细日志开启）
            if LOG_VERBOSE and now.second < 5:  # 每分钟的前5秒打印
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] === 日内止损检查 ===")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 持仓: {'多头' if position_quantity > 0 else '空头'} {abs(position_quantity)} 股")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 入场价格: ${entry_price:.2f}")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 当前价格: ${current_price:.2f}")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 未实现盈亏: ${unrealized_pnl:+.2f}")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 当日已实现: ${DAILY_PNL:+.2f}")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 当日总盈亏: ${total_current_pnl:+.2f} ({total_loss_pct*100:.2f}%)")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 止损线: {MAX_DAILY_LOSS_PCT*100:.0f}% (距离: {(MAX_DAILY_LOSS_PCT - total_loss_pct)*100:.2f}%)")
            
            # 检查是否触发日内止损（基于总盈亏）
            if total_loss_pct >= MAX_DAILY_LOSS_PCT:
                print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] !!!!! 触发日内止损 !!!!!")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 当日总亏损: ${total_current_pnl:.2f} ({total_loss_pct*100:.2f}%)")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 超过最大日内亏损限制 {MAX_DAILY_LOSS_PCT*100:.0f}%")
                
                side = "Sell" if position_quantity > 0 else "Buy"
                close_order_id = submit_order(symbol, side, abs(position_quantity), outside_rth=outside_rth_setting)
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 日内止损平仓订单已提交，ID: {close_order_id}")
                
                # 计算最终盈亏
                if entry_price and current_price > 0:
                    pnl = (current_price - entry_price) * (1 if position_quantity > 0 else -1) * abs(position_quantity)
                    DAILY_PNL += pnl
                    TOTAL_PNL += pnl
                    # 记录平仓交易
                    DAILY_TRADES.append({
                        "time": now.strftime('%Y-%m-%d %H:%M:%S'),
                        "action": "平仓(日内止损)",
                        "side": side,
                        "quantity": abs(position_quantity),
                        "price": current_price,
                        "pnl": pnl
                    })
                
                # 重置持仓状态
                position_quantity = 0
                entry_price = None
                current_stop = None
                
                # 设置止损标志，今日不再交易
                DAILY_STOP_TRIGGERED = True
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 今日不再进行新的交易")
                print("=" * 60)
        
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
                close_order_id = submit_order(symbol, side, abs(position_quantity), outside_rth=outside_rth_setting)
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓订单已提交，ID: {close_order_id}")
                
                # 计算盈亏
                if entry_price and current_price > 0:
                    pnl = (current_price - entry_price) * (1 if position_quantity > 0 else -1) * abs(position_quantity)
                    DAILY_PNL += pnl
                    TOTAL_PNL += pnl
                    # 记录平仓交易
                    DAILY_TRADES.append({
                        "time": now.strftime('%Y-%m-%d %H:%M:%S'),
                        "action": "平仓",
                        "side": side,
                        "quantity": abs(position_quantity),
                        "price": current_price,
                        "pnl": pnl
                    })
                    
                position_quantity = 0
                entry_price = None
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
                check_upper = check_row["UpperBound"]
                check_lower = check_row["LowerBound"]
                check_vwap = check_row["VWAP"]
                
                # 根据持仓方向检查退出条件
                exit_signal = False
                if position_quantity > 0:  # 多头持仓
                    # 使用检查时间点的上边界和VWAP作为止损
                    new_stop = max(check_upper, check_vwap)
                    exit_signal = check_price < new_stop
                    current_stop = new_stop
                elif position_quantity < 0:  # 空头持仓
                    # 使用检查时间点的下边界和VWAP作为止损
                    new_stop = min(check_lower, check_vwap)
                    exit_signal = check_price > new_stop
                    current_stop = new_stop
                
                if LOG_VERBOSE:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 持仓检查 {check_time_str}: 数量={position_quantity}, 价格={check_price:.2f}, 止损={current_stop:.2f}, 退出信号={exit_signal}")
            else:
                # 如果没有检查时间点的数据，使用原有逻辑
                exit_signal, new_stop = check_exit_conditions(df, position_quantity, current_stop)
                current_stop = new_stop
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
                    close_order_id = submit_order(symbol, side, abs(position_quantity), outside_rth=outside_rth_setting)
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓订单已提交，ID: {close_order_id}")
                    
                    # 计算盈亏
                    if entry_price:
                        pnl = (exit_price - entry_price) * (1 if position_quantity > 0 else -1) * abs(position_quantity)
                        pnl_pct = (exit_price / entry_price - 1) * 100 * (1 if position_quantity > 0 else -1)
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 平仓成功: {side} {abs(position_quantity)} {symbol} 价格: {exit_price}")
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 交易结果: {'盈利' if pnl > 0 else '亏损'} ${abs(pnl):.2f} ({pnl_pct:.2f}%)")
                        # 更新收益统计
                        DAILY_PNL += pnl
                        TOTAL_PNL += pnl
                        # 记录平仓交易
                        DAILY_TRADES.append({
                            "time": now.strftime('%Y-%m-%d %H:%M:%S'),
                            "action": "平仓",
                            "side": side,
                            "quantity": abs(position_quantity),
                            "price": exit_price,
                            "pnl": pnl
                        })
                    
                    # 平仓后增加交易次数计数器
                    positions_opened_today += 1
                    
                    position_quantity = 0
                    entry_price = None
                else:
                    # 检查是否已有持仓，如果有则不再开仓
                    if position_quantity != 0:
                        if LOG_VERBOSE:
                            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 已有持仓，跳过开仓检查")
                        continue
                    
                    # 检查今日是否达到最大持仓数
                    if positions_opened_today >= max_positions_per_day:
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
            long_price_above_vwap = latest_price > latest_row["VWAP"]
            
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
                stop = max(latest_row["UpperBound"], latest_row["VWAP"])
            else:
                short_price_below_lower = latest_price < latest_row["LowerBound"]
                short_price_below_vwap = latest_price < latest_row["VWAP"]
                if short_price_below_lower and short_price_below_vwap:
                    if LOG_VERBOSE:
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 满足空头入场条件!")
                    signal = -1
                    stop = min(latest_row["LowerBound"], latest_row["VWAP"])
                else:
                    if LOG_VERBOSE:
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 不满足入场条件: 多头({long_price_above_upper} & {long_price_above_vwap}), 空头({short_price_below_lower} & {short_price_below_vwap})")
            if signal != 0:
                # 保留交易信号日志，并添加VWAP和上下界信息
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 触发{'多' if signal == 1 else '空'}头入场信号!")
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 当前价格: {price}, VWAP: {latest_row['VWAP']:.4f}, 上界: {latest_row['UpperBound']:.4f}, 下界: {latest_row['LowerBound']:.4f}, 止损: {stop}")
                
                # 模拟模式：使用固定下单量
                position_size = FIXED_POSITION_SIZE
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 开仓数量: {position_size} 股")
                side = "Buy" if signal > 0 else "Sell"
                order_id = submit_order(symbol, side, position_size, outside_rth=outside_rth_setting)
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 订单已提交，ID: {order_id}")
                
                # 删除订单状态检查代码，直接更新持仓状态
                position_quantity = position_size if signal > 0 else -position_size
                entry_price = latest_price
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 开仓成功: {side} {position_size} {symbol} 价格: {entry_price}")
                
                # 记录开仓交易
                DAILY_TRADES.append({
                            "time": now.strftime('%Y-%m-%d %H:%M:%S'),
                            "action": "开仓",
                            "side": side,
                            "quantity": position_size,
                            "price": entry_price,
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

if __name__ == "__main__":
    print("\n长桥API交易策略启动 - 模拟模式")
    print("版本: 1.0.0")
    print("时间:", get_us_eastern_time().strftime("%Y-%m-%d %H:%M:%S"), "(美东时间)")
    
    # 显示配置状态
    if DEBUG_MODE:
        print("调试模式: 已开启（使用固定时间）")
        if 'DEBUG_TIME' in globals() and DEBUG_TIME:
            print(f"  固定时间: {DEBUG_TIME}")
        if DEBUG_ONCE:
            print("  单次运行: 是")
    else:
        print("调试模式: 已关闭（使用当前时间）")
    
    if LOG_VERBOSE:
        print("详细日志: 已开启")
    else:
        print("详细日志: 已关闭")
    
    print(f"固定下单量: {FIXED_POSITION_SIZE} 股")
    
    # 初始化SQLite数据库
    init_sqlite_database()
    
    if QUOTE_CTX is None or TRADE_CTX is None:
        print("错误: 无法创建API上下文")
        sys.exit(1)
        
    run_trading_strategy(
        symbol=SYMBOL,
        check_interval_minutes=CHECK_INTERVAL_MINUTES,
        trading_start_time=TRADING_START_TIME,
        trading_end_time=TRADING_END_TIME,
        max_positions_per_day=MAX_POSITIONS_PER_DAY,
        lookback_days=LOOKBACK_DAYS
    )
