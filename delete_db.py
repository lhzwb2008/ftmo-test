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

def delete_database(db_name, label):
    """删除指定的数据库文件"""
    db_path = get_db_path(db_name)
    
    print(f"📋 检查 {label} ({db_name})")
    print(f"   路径: {db_path}")
    
    if os.path.exists(db_path):
        try:
            # 获取文件信息
            file_stat = os.stat(db_path)
            file_size = file_stat.st_size
            last_modified = datetime.fromtimestamp(file_stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            
            print(f"   文件大小: {file_size} bytes")
            print(f"   最后修改: {last_modified}")
            
            # 删除文件
            os.remove(db_path)
            print(f"   ✅ 已删除")
            return True
            
        except Exception as e:
            print(f"   ❌ 删除失败: {e}")
            return False
    else:
        print(f"   ℹ️  文件不存在，无需删除")
        return True

def main():
    """主函数"""
    print("🗑️  数据库删除工具")
    print(f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    # 定义所有数据库
    databases = [
        ("trading_signals.db", "Base"),
        ("trading_signals_ftmo.db", "FTMO"),
        ("trading_signals_fundednext.db", "FundedNext"),
        ("trading_signals_the5ers.db", "The5ers")
    ]
    
    print("⚠️  警告: 此操作将删除所有交易信号数据库文件！")
    print("这将清除所有历史信号记录，无法恢复。")
    
    # 确认操作
    try:
        confirmation = input("\n确认删除所有数据库文件吗? (输入 'YES' 确认): ").strip()
        
        if confirmation != "YES":
            print("❌ 操作已取消")
            return
        
        print(f"\n开始删除数据库文件...")
        
        # 删除每个数据库
        success_count = 0
        total_count = len(databases)
        
        for db_name, label in databases:
            if delete_database(db_name, label):
                success_count += 1
        
        print(f"\n{'='*60}")
        print("📊 删除结果:")
        print(f"   成功删除: {success_count}/{total_count}")
        
        if success_count == total_count:
            print("✅ 所有数据库文件已成功删除")
        else:
            print(f"⚠️  有 {total_count - success_count} 个文件删除失败")
        
        print(f"\n💡 说明:")
        print("- 删除后，Python程序重新运行时会自动创建新的数据库")
        print("- MQ5 EA会从空数据库开始工作")
        print("- 所有历史信号记录已被清除")
        
    except KeyboardInterrupt:
        print(f"\n❌ 操作被用户取消")
    except Exception as e:
        print(f"\n❌ 操作出错: {e}")

if __name__ == "__main__":
    main() 