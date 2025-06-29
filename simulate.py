"""
简化版交易信号生成器
只负责生成交易信号并写入SQLite数据库
使用长桥API获取股票数据
"""

import pandas as pd
from datetime import datetime, time, timedelta, date as date_type
import time as time_module
import os
import sys
import pytz
from math import floor
import numpy as np
import sqlite3
from dotenv import load_dotenv

from longport.openapi import Config, QuoteContext, Period, AdjustType

load_dotenv(override=True)

# SQLite数据库路径 - 使用MT5通用目录
import platform
if platform.system() == "Windows":
    # Windows系统：使用MT5通用目录
    # 获取AppData路径
    appdata_path = os.environ.get('APPDATA', os.path.expanduser('~\\AppData\\Roaming'))
    mt5_common_path = os.path.join(appdata_path, "MetaQuotes", "Terminal", "Common", "Files")
    
    # 确保目录存在
    os.makedirs(mt5_common_path, exist_ok=True)
    
    # 数据库文件路径
    DB_PATH = os.path.join(mt5_common_path, "trading_signals.db")
    
    # 打印路径信息
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Windows系统检测")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] MT5通用目录: {mt5_common_path}")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 数据库路径: {DB_PATH}")
else:
    # 非Windows系统：使用当前目录
    DB_PATH = "trading_signals.db"
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 非Windows系统，使用当前目录")

# 固定配置参数
CHECK_INTERVAL_MINUTES = 15
TRADING_START_TIME = (9, 40)  # 交易开始时间：9点40分
TRADING_END_TIME = (15, 45)   # 交易结束时间：15点45分
MAX_POSITIONS_PER_DAY = 10
LOOKBACK_DAYS = 1
K1 = 1 # 上边界sigma乘数
K2 = 1 # 下边界sigma乘数

# 默认交易品种
SYMBOL = 'QQQ.US'  # 长桥格式，需要.US后缀

# 调试模式配置
DEBUG_MODE = False   # 设置为True开启调试模式
DEBUG_TIME = "2025-05-15 12:36:00"  # 调试使用的时间，格式: "YYYY-MM-DD HH:MM:SS"
DEBUG_ONCE = True  # 是否只运行一次就退出

def get_us_eastern_time():
    """获取美东时间"""
    if DEBUG_MODE and DEBUG_TIME:
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

def create_quote_context():
    """创建长桥行情连接"""
    max_retries = 5
    retry_delay = 5  # 秒
    
    for attempt in range(max_retries):
        try:
            config = Config.from_env()
            quote_ctx = QuoteContext(config)
            print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 长桥API连接成功")
            return quote_ctx
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 长桥API连接失败 ({attempt + 1}/{max_retries}): {str(e)}")
                print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] {retry_delay}秒后重试...")
                time_module.sleep(retry_delay)
            else:
                print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 长桥API连接失败，已达最大重试次数")
                raise

# 创建全局行情连接
QUOTE_CTX = None  # 延迟初始化，在main中创建

def init_sqlite_database():
    """初始化SQLite数据库"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # 创建简化的交易信号表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,  -- BUY, SELL, CLOSE
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            consumed INTEGER DEFAULT 0
        )
        """)
        
        conn.commit()
        conn.close()
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] SQLite数据库初始化成功")
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



def get_historical_data(symbol, days_back=5):
    """使用长桥API获取历史数据"""
    try:
        # 使用1分钟K线
        sdk_period = Period.Min_1
        adjust_type = AdjustType.ForwardAdjust
        eastern = pytz.timezone('US/Eastern')
        now_et = get_us_eastern_time()
        current_date = now_et.date()
        
        if DEBUG_MODE:
            print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] 开始获取历史数据: {symbol}")
        
        # 计算起始日期
        start_date = current_date - timedelta(days=days_back)
        
        # 对于1分钟数据使用按日获取的方式
        all_candles = []
        
        # 尝试从今天开始向前获取足够的数据
        date_to_check = current_date
        api_call_count = 0
        
        while date_to_check >= start_date:
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
                    # 每天最多获取390分钟数据（6.5小时交易时间）
                    day_candles = QUOTE_CTX.history_candlesticks_by_offset(
                        symbol, sdk_period, adjust_type, True, 390,
                        day_start_time_et
                    )
                    api_call_count += 1
                    break  # 成功则跳出重试循环
                except Exception as e:
                    if "rate limit" in str(e).lower():
                        if attempt < max_retries - 1:
                            if DEBUG_MODE:
                                print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] API限流，等待 {retry_delay} 秒后重试 ({attempt + 1}/{max_retries})")
                            time_module.sleep(retry_delay)
                            retry_delay *= 2  # 指数退避
                        else:
                            if DEBUG_MODE:
                                print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] API限流，已达最大重试次数")
                            raise
                    else:
                        raise  # 其他错误直接抛出
            
            if day_candles:
                all_candles.extend(day_candles)
                if DEBUG_MODE:
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
            print(f"警告: 无法获取{symbol}的历史数据")
            return pd.DataFrame()
            
        df["Date"] = df["DateTime"].dt.date
        df["Time"] = df["DateTime"].dt.strftime('%H:%M')
        
        # 过滤交易时间
        if symbol.endswith(".US"):
            df = df[df["Time"].between("09:30", "16:00")]
            
        # 去除重复数据
        df = df.drop_duplicates(subset=['Date', 'Time'])
        
        # 过滤掉未来日期的数据（双重保险）
        df = df[df["Date"] <= current_date]
        
        return df
        
    except Exception as e:
        print(f"获取历史数据失败: {str(e)}")
        return pd.DataFrame()

def calculate_vwap(df):
    """计算VWAP（成交量加权平均价）"""
    # 创建一个结果DataFrame的副本
    result_df = df.copy()
    
    # 按日期分组计算每日VWAP
    for date in result_df['Date'].unique():
        # 获取当日数据的索引
        daily_mask = result_df['Date'] == date
        daily_indices = result_df[daily_mask].index
        
        # 初始化当日的累计值
        cumulative_turnover = 0.0
        cumulative_volume = 0.0
        
        # 逐行计算VWAP
        for idx in daily_indices:
            # 累加成交额和成交量
            cumulative_turnover += result_df.loc[idx, 'Turnover']
            cumulative_volume += result_df.loc[idx, 'Volume']
            
            # 计算VWAP
            if cumulative_volume > 0:
                vwap = cumulative_turnover / cumulative_volume
            else:
                vwap = result_df.loc[idx, 'Close']
            
            # 直接在result_df中设置VWAP值
            result_df.loc[idx, 'VWAP'] = vwap
    
    return result_df['VWAP']  # 只返回VWAP列

def check_exit_conditions(df, position_type, current_date, current_time_str):
    """检查退出条件（参考simulate_old逻辑）"""
    # 获取当前时间点数据
    current_data = df[(df["Date"] == current_date) & (df["Time"] == current_time_str)]
    
    # 如果当前时间点没有数据，使用最新数据
    if current_data.empty:
        df_sorted = df.sort_values(by=["Date", "Time"], ascending=True)
        latest = df_sorted.iloc[-1]
    else:
        latest = current_data.iloc[0]
        
    price = latest["Close"]
    vwap = latest["VWAP"]
    upper = latest["UpperBound"]
    lower = latest["LowerBound"]
    
    if position_type == 'LONG':
        # 多头持仓：价格跌破上轨和VWAP的较大值时退出
        stop_level = max(upper, vwap)
        exit_signal = price < stop_level
        return exit_signal
    elif position_type == 'SHORT':
        # 空头持仓：价格突破下轨和VWAP的较小值时退出
        stop_level = min(lower, vwap)
        exit_signal = price > stop_level
        return exit_signal
    
    return False

def calculate_noise_area(df, lookback_days=LOOKBACK_DAYS, K1=1, K2=1):
    """计算噪声区域"""
    # 创建数据副本
    data = df.copy()
    
    # 确保数据按时间排序
    data = data.sort_values(by=['Date', 'Time'])
    
    # 获取唯一日期列表
    unique_dates = sorted(data['Date'].unique())
    
    # 初始化边界列
    data['UpperBound'] = np.nan
    data['LowerBound'] = np.nan
    
    # 对每个日期计算噪声区域
    for i, current_date in enumerate(unique_dates):
        # 获取当前日期的数据索引
        current_date_mask = data['Date'] == current_date
        current_date_indices = data[current_date_mask].index
        
        if len(current_date_indices) == 0:
            continue
        
        # 确定回看期的开始日期
        lookback_start_idx = max(0, i - lookback_days)
        lookback_dates = unique_dates[lookback_start_idx:i+1]
        
        # 获取回看期内的所有数据
        lookback_mask = data['Date'].isin(lookback_dates)
        lookback_data = data[lookback_mask]
        
        if len(lookback_data) == 0:
            continue
        
        # 计算噪声（标准差）
        noise = lookback_data['Close'].std()
        
        # 如果标准差为0或NaN，使用一个小的默认值
        if pd.isna(noise) or noise == 0:
            noise = 0.001
        
        # 获取当前日期第一个时间点的VWAP作为基准
        first_idx = current_date_indices[0]
        vwap_base = data.loc[first_idx, 'VWAP']
        
        # 为当前日期的所有时间点设置边界
        # 使用基准VWAP计算固定的边界
        upper_bound = vwap_base + K1 * noise
        lower_bound = vwap_base - K2 * noise
        
        # 设置边界值
        data.loc[current_date_indices, 'UpperBound'] = upper_bound
        data.loc[current_date_indices, 'LowerBound'] = lower_bound
    
    return data

def is_trading_day():
    """简单判断是否为交易日（周一到周五）"""
    now = get_us_eastern_time()
    # 周末不交易
    if now.weekday() >= 5:  # 5是周六，6是周日
        return False
    # 这里可以添加美国节假日判断
    return True

def run_trading_strategy(symbol=SYMBOL, check_interval_minutes=CHECK_INTERVAL_MINUTES,
                        trading_start_time=TRADING_START_TIME, trading_end_time=TRADING_END_TIME,
                        max_positions_per_day=MAX_POSITIONS_PER_DAY, lookback_days=LOOKBACK_DAYS):
    """运行交易策略"""
    
    now_et = get_us_eastern_time()
    print(f"启动交易策略 - 交易品种: {symbol}")
    print(f"当前美东时间: {now_et.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"交易时间: {trading_start_time[0]:02d}:{trading_start_time[1]:02d} - {trading_end_time[0]:02d}:{trading_end_time[1]:02d}")
    print(f"每日最大信号次数: {max_positions_per_day}")
    if DEBUG_MODE:
        print(f"调试模式已开启! 使用时间: {now_et.strftime('%Y-%m-%d %H:%M:%S')}")
        if DEBUG_ONCE:
            print("单次运行模式已开启，策略将只运行一次")
    
    # 跟踪每日信号数量
    signals_sent_today = 0
    last_date = None
    last_signal_time = None  # 防止同一分钟内重复发送信号
    current_position = None  # 跟踪当前持仓状态：None, 'LONG', 'SHORT'
    
    while True:
        now = get_us_eastern_time()
        current_date = now.date()
        current_hour, current_minute = now.hour, now.minute
        current_time_str = now.strftime('%H:%M')
        
        if DEBUG_MODE:
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 主循环开始")
        
        # 检查是否是交易日
        if not is_trading_day():
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 今天不是交易日")
            
            # 等待到下一个检查时间
            next_check_time = now + timedelta(hours=12)
            wait_seconds = (next_check_time - now).total_seconds()
            time_module.sleep(wait_seconds)
            continue
        
        # 检查是否是新交易日
        if last_date is not None and current_date != last_date:
            signals_sent_today = 0
            current_position = None  # 新交易日重置持仓状态
        last_date = current_date
        
        # 检查是否在交易时间内
        start_hour, start_minute = trading_start_time
        end_hour, end_minute = trading_end_time
        is_trading_hours = (
            (current_hour > start_hour or (current_hour == start_hour and current_minute >= start_minute)) and
            (current_hour < end_hour or (current_hour == end_hour and current_minute <= end_minute))
        )
        
        # 如果是交易结束时间，发送收盘信号
        is_trading_end = current_hour == end_hour and current_minute == end_minute
        if is_trading_end:
            if last_signal_time != current_time_str:  # 避免重复发送
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 交易时间结束，发送收盘信号")
                write_signal_to_sqlite("CLOSE")
                last_signal_time = current_time_str
                current_position = None  # 收盘后重置持仓状态
            
            if DEBUG_MODE and DEBUG_ONCE:
                print("\n调试模式单次运行完成，程序退出")
                break
            continue
        
        if not is_trading_hours:
            if DEBUG_MODE:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 当前不在交易时间内")
            
            # 等待到下一个交易开始时间
            today_start = datetime.combine(current_date, time(start_hour, start_minute), tzinfo=now.tzinfo)
            if now < today_start:
                next_check_time = today_start
            else:
                tomorrow = current_date + timedelta(days=1)
                next_check_time = datetime.combine(tomorrow, time(start_hour, start_minute), tzinfo=now.tzinfo)
            
            wait_seconds = min(1800, (next_check_time - now).total_seconds())
            time_module.sleep(wait_seconds)
            continue
        
        # 获取历史数据
        df = get_historical_data(symbol, lookback_days + 5)
        if df.empty:
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 无法获取历史数据，等待下次重试")
            time_module.sleep(60)  # 等待1分钟后重试
            continue
        
        if DEBUG_MODE:
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 历史数据获取完成: {len(df)} 条")
            # 调试模式下，截断到调试时间之前的数据
            df = df[df["DateTime"] <= now]
        
        # 计算VWAP和噪声区域
        df["VWAP"] = calculate_vwap(df)
        df = calculate_noise_area(df, lookback_days, K1, K2)
        
        # 检查是否已有持仓，如果有则检查退出条件
        if current_position is not None:
            if DEBUG_MODE:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 已有持仓状态: {current_position}，检查退出条件")
            
            # 检查退出条件
            exit_signal = check_exit_conditions(df, current_position, current_date, current_time_str)
            if exit_signal:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 触发退出信号，发送平仓信号")
                write_signal_to_sqlite("CLOSE")
                current_position = None  # 平仓后重置持仓状态
                last_signal_time = current_time_str
        # 检查是否达到每日信号上限
        elif signals_sent_today >= max_positions_per_day:
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 今日已发送 {signals_sent_today} 个信号，达到上限")
        else:
            # 避免同一分钟内重复发送信号
            if last_signal_time == current_time_str:
                if DEBUG_MODE:
                    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 本分钟已发送过信号，跳过")
            else:
                # 获取最新数据
                latest_data = df[df["Date"] == current_date]
                if not latest_data.empty:
                    latest_row = latest_data.iloc[-1]
                    price = latest_row["Close"]
                    vwap = latest_row["VWAP"]
                    upper = latest_row["UpperBound"]
                    lower = latest_row["LowerBound"]
                    
                    if DEBUG_MODE:
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 价格={price:.2f}, VWAP={vwap:.2f}, 上界={upper:.2f}, 下界={lower:.2f}")
                    
                    # 检查多头入场条件
                    if price > upper and price > vwap:
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 满足多头入场条件，发送买入信号")
                        write_signal_to_sqlite("BUY")
                        signals_sent_today += 1
                        last_signal_time = current_time_str
                        current_position = 'LONG'
                    # 检查空头入场条件
                    elif price < lower and price < vwap:
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 满足空头入场条件，发送卖出信号")
                        write_signal_to_sqlite("SELL")
                        signals_sent_today += 1
                        last_signal_time = current_time_str
                        current_position = 'SHORT'
                    else:
                        if DEBUG_MODE:
                            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 不满足入场条件")
        
        # 调试模式且单次运行
        if DEBUG_MODE and DEBUG_ONCE:
            print("\n调试模式单次运行完成，程序退出")
            break
        
        # 等待下一次检查
        next_check_time = now + timedelta(minutes=check_interval_minutes)
        sleep_seconds = (next_check_time - now).total_seconds()
        if sleep_seconds > 0:
            if DEBUG_MODE:
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 等待 {sleep_seconds:.0f} 秒")
            time_module.sleep(sleep_seconds)

if __name__ == "__main__":
    print("\n简化版交易信号生成器")
    print("版本: 1.0.0")
    print("时间:", get_us_eastern_time().strftime("%Y-%m-%d %H:%M:%S"), "(美东时间)")
    print(f"监控品种: {SYMBOL}")
    print(f"数据库: {os.path.abspath(DB_PATH)}")
    
    # 初始化SQLite数据库
    init_sqlite_database()
    
    # 初始化长桥API连接
    print("\n初始化长桥API连接...")
    QUOTE_CTX = create_quote_context()
    
    if DEBUG_MODE:
        print("调试模式已开启")
        if DEBUG_TIME:
            print(f"调试时间: {DEBUG_TIME}")
        if DEBUG_ONCE:
            print("单次运行模式已开启")
    
    # 运行交易策略
    run_trading_strategy(
        symbol=SYMBOL,
        check_interval_minutes=CHECK_INTERVAL_MINUTES,
        trading_start_time=TRADING_START_TIME,
        trading_end_time=TRADING_END_TIME,
        max_positions_per_day=MAX_POSITIONS_PER_DAY,
        lookback_days=LOOKBACK_DAYS
    ) 