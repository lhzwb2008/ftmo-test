"""
使用SQLite本地数据库的交易信号生成器
Python查询QQQ价格，MT5在US100上执行
"""

import sqlite3
import os
from datetime import datetime
from longport_simulate import *  # 导入原有的所有函数和配置

# SQLite数据库路径
DB_PATH = "trading_signals.db"

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
            quantity INTEGER NOT NULL,
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

def write_signal_to_sqlite(symbol, signal_type, quantity, price, stop_price=None):
    """将交易信号写入SQLite数据库"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # 简化信号类型：BUY（买入）, SELL（卖空）, CLOSE（平仓）
        action = signal_type.upper()
        if action == "SELL" and quantity > 0:  # 平仓信号
            action = "CLOSE"
        
        cursor.execute("""
        INSERT INTO signals (action, quantity)
        VALUES (?, ?)
        """, (action, abs(quantity)))
        
        conn.commit()
        signal_id = cursor.lastrowid
        conn.close()
        
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 信号已写入: {action} {quantity} (ID: {signal_id})")
        return signal_id
        
    except Exception as e:
        print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 写入信号失败: {str(e)}")
        return None

# 替换原有的数据库写入函数
write_trading_signal = write_signal_to_sqlite

# 初始化数据库
init_database = init_sqlite_database

# 修改主程序入口
if __name__ == "__main__":
    print("\n交易策略启动（SQLite版）")
    print("时间:", get_us_eastern_time().strftime("%Y-%m-%d %H:%M:%S"), "(美东时间)")
    print(f"监控品种: {SYMBOL} (QQQ)")
    print(f"执行品种: US100.cash (MT5)")
    print(f"数据库: {os.path.abspath(DB_PATH)}")
    
    # 初始化SQLite数据库
    init_database()
    
    if DEBUG_MODE:
        print("调试模式已开启")
    print(f"杠杆倍数: {LEVERAGE}倍")
    
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