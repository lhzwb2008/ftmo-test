import sqlite3
import os
import platform
from datetime import datetime

def get_db_path(db_name):
    """获取数据库路径"""
    if platform.system() == "Windows":
        appdata_path = os.environ.get('APPDATA', os.path.expanduser('~\\AppData\\Roaming'))
        mt5_common_path = os.path.join(appdata_path, "MetaQuotes", "Terminal", "Common", "Files")
        return os.path.join(mt5_common_path, db_name)
    else:
        return db_name

def check_database_readonly(db_name, label):
    """只读方式检查数据库内容，不做任何修改"""
    db_path = get_db_path(db_name)
    
    print(f"\n📊 检查 {label} ({db_name})")
    print(f"路径: {db_path}")
    print(f"文件存在: {os.path.exists(db_path)}")
    
    if not os.path.exists(db_path):
        print("❌ 数据库文件不存在")
        return
    
    try:
        # 只读模式连接数据库
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()
        
        # 检查表是否存在
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='signals'")
        table_exists = cursor.fetchone()
        
        if not table_exists:
            print("❌ signals表不存在")
            # 显示所有表
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = cursor.fetchall()
            if tables:
                print(f"数据库中的表: {[t[0] for t in tables]}")
            else:
                print("数据库中没有任何表")
            conn.close()
            return
        
        print("✅ signals表存在")
        
        # 获取记录统计
        cursor.execute("SELECT COUNT(*) FROM signals")
        total_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM signals WHERE consumed = 0")
        unconsumed_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM signals WHERE consumed = 1")
        consumed_count = cursor.fetchone()[0]
        
        print(f"总记录数: {total_count}")
        print(f"未消费信号: {unconsumed_count}")
        print(f"已消费信号: {consumed_count}")
        
        if total_count > 0:
            # 显示所有记录
            cursor.execute("SELECT id, action, created_at, consumed FROM signals ORDER BY created_at DESC")
            all_signals = cursor.fetchall()
            
            print(f"\n所有信号记录:")
            print(f"{'ID':<4} {'Action':<6} {'Created At':<20} {'Status':<8}")
            print("-" * 45)
            
            for signal in all_signals:
                status = "已消费" if signal[3] == 1 else "未消费"
                print(f"{signal[0]:<4} {signal[1]:<6} {signal[2]:<20} {status:<8}")
            
            # 显示时间统计
            cursor.execute("SELECT MIN(created_at), MAX(created_at) FROM signals")
            time_range = cursor.fetchone()
            if time_range[0]:
                print(f"\n时间范围: {time_range[0]} ~ {time_range[1]}")
        else:
            print("数据库中没有信号记录")
        
        # 文件信息
        file_stat = os.stat(db_path)
        print(f"\n文件信息:")
        print(f"文件大小: {file_stat.st_size} bytes")
        print(f"最后修改: {datetime.fromtimestamp(file_stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}")
        
        conn.close()
        
    except Exception as e:
        print(f"❌ 检查数据库出错: {e}")

def main():
    """主函数"""
    print("🔍 数据库内容查看工具（只读模式）")
    print(f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    # 定义所有数据库
    databases = [
        ("trading_signals.db", "Base"),
        ("trading_signals_ftmo.db", "FTMO"),
        ("trading_signals_fundednext.db", "FundedNext"),
        ("trading_signals_the5ers.db", "The5ers")
    ]
    
    # 检查每个数据库
    for db_name, label in databases:
        check_database_readonly(db_name, label)
    
    print(f"\n{'='*60}")
    print("💡 说明:")
    print("- 此工具只读取数据库内容，不做任何修改")
    print("- 如果某个数据库不存在，说明对应的Python程序还没运行过")
    print("- 未消费信号表示MQ5 EA还没有处理这些信号")

if __name__ == "__main__":
    main() 