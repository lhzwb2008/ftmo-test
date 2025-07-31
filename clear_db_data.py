#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库数据清空工具
清空所有交易信号数据库中的数据，但保留数据库文件和表结构
"""

import os
import platform
import sqlite3
from datetime import datetime


def get_db_path(db_name):
    """获取数据库路径"""
    if platform.system() == "Windows":
        appdata_path = os.environ.get('APPDATA', os.path.expanduser('~\\AppData\\Roaming'))
        mt5_common_path = os.path.join(appdata_path, "MetaQuotes", "Terminal", "Common", "Files")
        return os.path.join(mt5_common_path, db_name)
    else:
        return db_name


def clear_database_data(db_name, label):
    """清空指定数据库中的所有数据"""
    db_path = get_db_path(db_name)
    
    print(f"📋 处理 {label} ({db_name})")
    print(f"   路径: {db_path}")
    
    if not os.path.exists(db_path):
        print(f"   ℹ️  数据库文件不存在，跳过")
        return True
    
    try:
        # 获取文件信息
        file_stat = os.stat(db_path)
        file_size = file_stat.st_size
        last_modified = datetime.fromtimestamp(file_stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
        
        print(f"   文件大小: {file_size} bytes")
        print(f"   最后修改: {last_modified}")
        
        # 连接数据库
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 检查是否存在signals表
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='signals'")
        table_exists = cursor.fetchone() is not None
        
        if not table_exists:
            print(f"   ℹ️  signals表不存在，跳过")
            conn.close()
            return True
        
        # 获取清空前的记录数
        cursor.execute("SELECT COUNT(*) FROM signals")
        record_count = cursor.fetchone()[0]
        print(f"   清空前记录数: {record_count}")
        
        if record_count == 0:
            print(f"   ℹ️  数据库已经是空的")
            conn.close()
            return True
        
        # 清空signals表的所有数据
        cursor.execute("DELETE FROM signals")
        
        # 重置自增ID计数器
        cursor.execute("DELETE FROM sqlite_sequence WHERE name='signals'")
        
        conn.commit()
        
        # 验证清空结果
        cursor.execute("SELECT COUNT(*) FROM signals")
        remaining_count = cursor.fetchone()[0]
        
        conn.close()
        
        if remaining_count == 0:
            print(f"   ✅ 成功清空 {record_count} 条记录")
            return True
        else:
            print(f"   ❌ 清空失败，仍有 {remaining_count} 条记录")
            return False
            
    except Exception as e:
        print(f"   ❌ 清空失败: {e}")
        return False


def show_database_status(db_name, label):
    """显示数据库状态"""
    db_path = get_db_path(db_name)
    
    if not os.path.exists(db_path):
        return f"   {label}: 文件不存在"
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 检查表是否存在
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='signals'")
        table_exists = cursor.fetchone() is not None
        
        if not table_exists:
            conn.close()
            return f"   {label}: 表不存在"
        
        # 获取记录数
        cursor.execute("SELECT COUNT(*) FROM signals")
        count = cursor.fetchone()[0]
        conn.close()
        
        return f"   {label}: {count} 条记录"
        
    except Exception as e:
        return f"   {label}: 检查失败 ({e})"


def main():
    """主函数"""
    print("🧹 数据库数据清空工具")
    print(f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    # 定义所有数据库
    databases = [
        ("trading_signals.db", "Base"),
        ("trading_signals_ftmo.db", "FTMO"),
        ("trading_signals_fundednext.db", "FundedNext"),
        ("trading_signals_the5ers.db", "The5ers")
    ]
    
    print("ℹ️  此操作将清空所有交易信号数据库中的数据")
    print("数据库文件和表结构将被保留，只清空数据记录")
    
    # 显示当前状态
    print(f"\n📊 当前数据库状态:")
    for db_name, label in databases:
        status = show_database_status(db_name, label)
        print(status)
    
    # 确认操作
    try:
        confirmation = input(f"\n确认清空所有数据库中的数据吗? (输入 'YES' 确认): ").strip()
        
        if confirmation != "YES":
            print("❌ 操作已取消")
            return
        
        print(f"\n开始清空数据库数据...")
        
        # 清空每个数据库
        success_count = 0
        total_count = len(databases)
        
        for db_name, label in databases:
            if clear_database_data(db_name, label):
                success_count += 1
            print()  # 空行分隔
        
        print(f"{'='*60}")
        print("📊 清空结果:")
        print(f"   成功处理: {success_count}/{total_count}")
        
        if success_count == total_count:
            print("✅ 所有数据库数据已成功清空")
        else:
            print(f"⚠️  有 {total_count - success_count} 个数据库清空失败")
        
        # 显示最终状态
        print(f"\n📊 清空后数据库状态:")
        for db_name, label in databases:
            status = show_database_status(db_name, label)
            print(status)
        
        print(f"\n💡 说明:")
        print("- 数据库文件和表结构已保留")
        print("- 所有信号记录已被清空")
        print("- 自增ID计数器已重置")
        print("- Python程序和MQ5 EA可以正常写入新信号")
        
    except KeyboardInterrupt:
        print(f"\n❌ 操作被用户取消")
    except Exception as e:
        print(f"\n❌ 操作出错: {e}")


if __name__ == "__main__":
    main()