#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试文件：向三个不同数据库写入买入和卖出信号
每个数据库写入一个买入信号，持仓5分钟后写入一个卖出信号
在一分钟内离散写入三个买入信号
"""

import sqlite3
import time
import os
import platform
from datetime import datetime

# 配置参数
BUY_SIGNAL_INTERVAL = 20    # 买入信号间隔（秒）- 20秒间隔，总共60秒内完成3个信号
HOLD_TIME_MINUTES = 5       # 持仓时间（分钟）
SELL_SIGNAL_INTERVAL = 10   # 卖出信号间隔（秒）
STATUS_UPDATE_INTERVAL = 30 # 持仓期间状态更新间隔（秒）


def get_current_time():
    """获取当前时间（简化版本，不处理时区）"""
    return datetime.now()


def get_db_path(db_name):
    """获取数据库路径"""
    if platform.system() == "Windows":
        # Windows系统：使用MT5通用目录
        appdata_path = os.environ.get('APPDATA', os.path.expanduser('~\\AppData\\Roaming'))
        mt5_common_path = os.path.join(appdata_path, "MetaQuotes", "Terminal", "Common", "Files")
        os.makedirs(mt5_common_path, exist_ok=True)
        return os.path.join(mt5_common_path, db_name)
    else:
        # 非Windows系统：使用当前目录
        return db_name


def init_database(db_path):
    """初始化数据库"""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 先删除现有表（如果存在）
        cursor.execute("DROP TABLE IF EXISTS signals")
        
        # 创建交易信号表
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
        print(f"[{get_current_time().strftime('%Y-%m-%d %H:%M:%S')}] 数据库初始化成功: {os.path.abspath(db_path)}")
        return True
        
    except Exception as e:
        print(f"[{get_current_time().strftime('%Y-%m-%d %H:%M:%S')}] 数据库初始化失败 {db_path}: {str(e)}")
        return False


def write_signal_to_db(db_path, action, db_label):
    """向数据库写入交易信号"""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
        INSERT INTO signals (action)
        VALUES (?)
        """, (action.upper(),))
        
        conn.commit()
        signal_id = cursor.lastrowid
        conn.close()
        
        print(f"[{get_current_time().strftime('%Y-%m-%d %H:%M:%S')}] {db_label} - 信号已写入: {action} (ID: {signal_id})")
        return signal_id
        
    except Exception as e:
        print(f"[{get_current_time().strftime('%Y-%m-%d %H:%M:%S')}] {db_label} - 写入信号失败: {str(e)}")
        return None


def check_signals_in_db(db_path, db_label):
    """检查数据库中的信号"""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT id, action, created_at, consumed FROM signals ORDER BY created_at")
        signals = cursor.fetchall()
        conn.close()
        
        print(f"\n{db_label} 数据库中的信号:")
        if signals:
            for signal in signals:
                print(f"  ID: {signal[0]}, 动作: {signal[1]}, 时间: {signal[2]}, 已消费: {signal[3]}")
        else:
            print("  无信号")
        print()
        
    except Exception as e:
        print(f"[{get_current_time().strftime('%Y-%m-%d %H:%M:%S')}] {db_label} - 检查信号失败: {str(e)}")


def main():
    """主函数"""
    print(f"[{get_current_time().strftime('%Y-%m-%d %H:%M:%S')}] 开始测试三个数据库信号写入")
    
    # 三个数据库配置
    databases = [
        {
            'name': 'trading_signals_ftmo.db',
            'label': 'FTMO',
            'path': None
        },
        {
            'name': 'trading_signals_fundednext.db', 
            'label': 'FundedNext',
            'path': None
        },
        {
            'name': 'trading_signals_the5ers.db',
            'label': 'The5ers', 
            'path': None
        }
    ]
    
    # 获取数据库路径并初始化
    for db in databases:
        db['path'] = get_db_path(db['name'])
        if not init_database(db['path']):
            print(f"初始化 {db['label']} 数据库失败，退出")
            return
    
    print(f"\n[{get_current_time().strftime('%Y-%m-%d %H:%M:%S')}] 所有数据库初始化完成")
    
    # 第一阶段：在一分钟内离散写入三个买入信号
    print(f"\n[{get_current_time().strftime('%Y-%m-%d %H:%M:%S')}] 开始写入买入信号...")
    
    # 使用配置的买入信号间隔
    
    for i, db in enumerate(databases):
        # 写入买入信号
        signal_id = write_signal_to_db(db['path'], 'BUY', db['label'])
        if signal_id:
            print(f"✅ {db['label']} 买入信号写入成功")
        else:
            print(f"❌ {db['label']} 买入信号写入失败")
        
        # 如果不是最后一个数据库，等待指定间隔
        if i < len(databases) - 1:
            print(f"等待 {BUY_SIGNAL_INTERVAL} 秒后写入下一个信号...")
            time.sleep(BUY_SIGNAL_INTERVAL)
    
    print(f"\n[{get_current_time().strftime('%Y-%m-%d %H:%M:%S')}] 所有买入信号写入完成")
    
    # 检查所有数据库的信号
    print("\n=== 买入信号写入后的数据库状态 ===")
    for db in databases:
        check_signals_in_db(db['path'], db['label'])
    
    # 第二阶段：持仓指定时间
    hold_time = HOLD_TIME_MINUTES * 60  # 转换为秒
    print(f"[{get_current_time().strftime('%Y-%m-%d %H:%M:%S')}] 持仓 {HOLD_TIME_MINUTES} 分钟...")
    
    # 倒计时显示
    for remaining in range(hold_time, 0, -STATUS_UPDATE_INTERVAL):
        minutes = remaining // 60
        seconds = remaining % 60
        print(f"[{get_current_time().strftime('%Y-%m-%d %H:%M:%S')}] 剩余持仓时间: {minutes}分{seconds}秒")
        time.sleep(STATUS_UPDATE_INTERVAL)
    
    # 第三阶段：写入卖出信号
    print(f"\n[{get_current_time().strftime('%Y-%m-%d %H:%M:%S')}] 开始写入卖出信号...")
    
    # 使用配置的卖出信号间隔
    
    for i, db in enumerate(databases):
        # 写入卖出信号
        signal_id = write_signal_to_db(db['path'], 'SELL', db['label'])
        if signal_id:
            print(f"✅ {db['label']} 卖出信号写入成功")
        else:
            print(f"❌ {db['label']} 卖出信号写入失败")
        
        # 如果不是最后一个数据库，等待指定间隔
        if i < len(databases) - 1:
            print(f"等待 {SELL_SIGNAL_INTERVAL} 秒后写入下一个信号...")
            time.sleep(SELL_SIGNAL_INTERVAL)
    
    print(f"\n[{get_current_time().strftime('%Y-%m-%d %H:%M:%S')}] 所有卖出信号写入完成")
    
    # 最终检查所有数据库的信号
    print("\n=== 最终数据库状态 ===")
    for db in databases:
        check_signals_in_db(db['path'], db['label'])
    
    print(f"[{get_current_time().strftime('%Y-%m-%d %H:%M:%S')}] 测试完成！")
    
    # 显示总结
    print("\n=== 测试总结 ===")
    print("✅ 已向三个数据库分别写入:")
    print("   - 1个买入信号 (BUY)")
    print("   - 1个卖出信号 (SELL)")
    print("✅ 买入信号在1分钟内离散写入")
    print("✅ 持仓时间约5分钟")
    print("✅ 数据库路径:")
    for db in databases:
        print(f"   - {db['label']}: {db['path']}")


if __name__ == "__main__":
    main()