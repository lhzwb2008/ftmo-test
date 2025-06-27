"""
简化版交易信号生成器
只负责生成交易信号并写入SQLite数据库
不包含任何长桥证券API调用
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

# SQLite数据库路径 - 使用MT5通用目录
import platform
if platform.system() == "Windows":
    # Windows系统：使用MT5通用目录
    mt5_files_dir = os.path.expanduser("~/AppData/Roaming/MetaQuotes/Terminal/Common/Files")
    
    # 确保目录存在
    os.makedirs(mt5_files_dir, exist_ok=True)
    DB_PATH = os.path.join(mt5_files_dir, "trading_signals.db")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 使用MT5通用目录: {mt5_files_dir}")
else:
    # 非Windows系统：使用当前目录
    DB_PATH = "trading_signals.db"

# 固定配置参数
CHECK_INTERVAL_MINUTES = 15
TRADING_START_TIME = (9, 40)  # 交易开始时间：9点40分
TRADING_END_TIME = (15, 45)   # 交易结束时间：15点45分
MAX_POSITIONS_PER_DAY = 10
LOOKBACK_DAYS = 1
K1 = 1 # 上边界sigma乘数
K2 = 1 # 下边界sigma乘数

# 默认交易品种
SYMBOL = 'QQQ'  # yfinance格式，不需要.US后缀

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

def get_historical_data_yfinance(symbol, days_back=5):
    """使用yfinance获取历史数据"""
    try:
        # 计算开始和结束日期
        end_date = get_us_eastern_time().date()
        start_date = end_date - timedelta(days=days_back + 10)  # 多获取一些数据以确保有足够的交易日
        
        # 下载数据
        ticker = yf.Ticker(symbol)
        df = ticker.history(start=start_date, end=end_date, interval='1m')
        
        if df.empty:
            print(f"警告: 无法获取{symbol}的历史数据")
            return pd.DataFrame()
        
        # 转换时区到美东时间
        eastern = pytz.timezone('US/Eastern')
        df.index = df.index.tz_convert(eastern)
        
        # 添加必要的列
        df['DateTime'] = df.index
        df['Date'] = df.index.date
        df['Time'] = df.index.strftime('%H:%M')
        
        # 过滤交易时间
        df = df[df['Time'].between('09:30', '16:00')]
        
        # 重命名列以匹配原始格式
        df = df.rename(columns={
            'Open': 'Open',
            'High': 'High',
            'Low': 'Low',
            'Close': 'Close',
            'Volume': 'Volume'
        })
        
        # 添加Turnover列（成交额 = 价格 * 成交量）
        df['Turnover'] = df['Close'] * df['Volume']
        
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
    
    return result_df

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
        df = get_historical_data_yfinance(symbol, lookback_days + 5)
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
        
        # 检查是否达到每日信号上限
        if signals_sent_today >= max_positions_per_day:
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
                    # 检查空头入场条件
                    elif price < lower and price < vwap:
                        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 满足空头入场条件，发送卖出信号")
                        write_signal_to_sqlite("SELL")
                        signals_sent_today += 1
                        last_signal_time = current_time_str
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