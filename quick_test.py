"""
简单的SQLite交易信号测试
写入买入信号 -> 等待1分钟 -> 写入平仓信号
"""

import sqlite3
import time
from datetime import datetime

DB_PATH = "trading_signals.db"

def write_signal(action, quantity):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 创建表（如果不存在）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        consumed INTEGER DEFAULT 0
    )
    """)
    
    # 写入信号
    cursor.execute("INSERT INTO signals (action, quantity) VALUES (?, ?)", 
                   (action.upper(), quantity))
    
    conn.commit()
    signal_id = cursor.lastrowid
    conn.close()
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 信号已写入: {action} {quantity} (ID: {signal_id})")

if __name__ == "__main__":
    print("🚀 SQLite交易信号测试")
    print("=" * 40)
    
    # 写入买入信号
    print("📝 写入买入信号...")
    write_signal("BUY", 100)
    
    # 等待1分钟
    print("⏰ 等待60秒...")
    for i in range(60, 0, -1):
        print(f"倒计时: {i}秒", end='\r')
        time.sleep(1)
    print()
    
    # 写入平仓信号
    print("📝 写入平仓信号...")
    write_signal("CLOSE", 100)
    
    print("✅ 测试完成！检查MT5是否执行了交易。") 