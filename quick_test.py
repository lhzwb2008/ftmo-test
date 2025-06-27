"""
简单的SQLite交易信号测试
写入买入信号 -> 等待1分钟 -> 写入平仓信号
"""

import sqlite3
import time
import os
import platform
from datetime import datetime

# SQLite数据库路径 - 使用MT5通用目录
if platform.system() == "Windows":
    # Windows系统：使用MT5通用目录
    mt5_files_dir = os.path.expanduser("~/AppData/Roaming/MetaQuotes/Terminal/Common/Files")
    
    # 确保目录存在
    os.makedirs(mt5_files_dir, exist_ok=True)
    DB_PATH = os.path.join(mt5_files_dir, "trading_signals.db")
    print(f"使用MT5通用目录: {mt5_files_dir}")
else:
    # 非Windows系统：使用当前目录
    DB_PATH = "trading_signals.db"

def write_signal(action):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 创建表（如果不存在）- 移除quantity字段
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        consumed INTEGER DEFAULT 0
    )
    """)
    
    # 写入信号
    cursor.execute("INSERT INTO signals (action) VALUES (?)", 
                   (action.upper(),))
    
    conn.commit()
    signal_id = cursor.lastrowid
    conn.close()
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 信号已写入: {action} (ID: {signal_id})")
    print(f"数据库路径: {DB_PATH}")

if __name__ == "__main__":
    print("🚀 SQLite交易信号测试")
    print("=" * 40)
    print(f"📍 数据库路径: {DB_PATH}")
    
    # 写入买入信号
    print("📝 写入买入信号...")
    write_signal("BUY")
    
    # 等待1分钟
    print("⏰ 等待60秒...")
    for i in range(60, 0, -1):
        print(f"倒计时: {i}秒", end='\r')
        time.sleep(1)
    print()
    
    # 写入平仓信号
    print("📝 写入平仓信号...")
    write_signal("CLOSE")
    
    print("✅ 测试完成！检查MT5是否执行了交易。") 