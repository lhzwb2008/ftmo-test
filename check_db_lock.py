import os
import platform
import subprocess
from datetime import datetime

def get_db_path(db_name):
    """获取数据库路径"""
    if platform.system() == "Windows":
        appdata_path = os.environ.get('APPDATA', os.path.expanduser('~\\AppData\\Roaming'))
        mt5_common_path = os.path.join(appdata_path, "MetaQuotes", "Terminal", "Common", "Files")
        return os.path.join(mt5_common_path, db_name)
    else:
        return db_name

def find_mt5_processes():
    """快速查找MT5相关进程"""
    processes = []
    
    try:
        print("🔍 查找MT5相关进程...")
        result = subprocess.run(['tasklist', '/fi', 'imagename eq terminal64.exe'], 
                              capture_output=True, text=True, shell=True, timeout=10)
        
        if result.returncode == 0 and 'terminal64.exe' in result.stdout:
            lines = result.stdout.split('\n')
            for line in lines:
                if 'terminal64.exe' in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        processes.append({
                            'name': parts[0],
                            'pid': parts[1],
                            'type': 'MT5主程序'
                        })
        
        # 查找其他可能的MT5进程
        other_names = ['metatrader.exe', 'mt5.exe', 'terminal.exe']
        for name in other_names:
            try:
                result = subprocess.run(['tasklist', '/fi', f'imagename eq {name}'], 
                                      capture_output=True, text=True, shell=True, timeout=5)
                if result.returncode == 0 and name in result.stdout:
                    lines = result.stdout.split('\n')
                    for line in lines:
                        if name in line:
                            parts = line.split()
                            if len(parts) >= 2:
                                processes.append({
                                    'name': parts[0],
                                    'pid': parts[1],
                                    'type': 'MT5相关'
                                })
            except:
                continue
                
    except Exception as e:
        print(f"查找MT5进程出错: {e}")
    
    return processes

def try_delete_file(file_path):
    """尝试删除文件"""
    try:
        os.remove(file_path)
        return True, "删除成功"
    except PermissionError:
        return False, "权限被拒绝 - 文件被锁定"
    except Exception as e:
        return False, str(e)

def kill_process(pid, name):
    """终止指定进程"""
    try:
        result = subprocess.run(['taskkill', '/F', '/PID', str(pid)], 
                              capture_output=True, text=True, shell=True, timeout=10)
        return result.returncode == 0, result.stdout + result.stderr
    except Exception as e:
        return False, str(e)

def main():
    """主函数"""
    print("⚡ 快速文件锁定检查工具")
    print(f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    file_path = get_db_path("trading_signals.db")
    print(f"目标文件: {file_path}")
    
    if not os.path.exists(file_path):
        print("❌ 文件不存在")
        return
    
    print("✅ 文件存在")
    
    # 首先尝试直接删除
    print(f"\n🔧 尝试直接删除文件...")
    success, message = try_delete_file(file_path)
    
    if success:
        print("✅ 文件删除成功！")
        return
    else:
        print(f"❌ 删除失败: {message}")
    
    # 查找可能的锁定进程
    print(f"\n🔍 查找可能锁定文件的进程...")
    mt5_processes = find_mt5_processes()
    
    if mt5_processes:
        print(f"\n📊 发现MT5相关进程:")
        print(f"{'进程名':<20} {'PID':<8} {'类型'}")
        print("-" * 40)
        
        for proc in mt5_processes:
            print(f"{proc['name']:<20} {proc['pid']:<8} {proc['type']}")
        
        # 询问是否终止进程
        print(f"\n⚠️  这些进程可能正在锁定数据库文件")
        
        try:
            choice = input("是否终止这些进程? (输入 'YES' 确认): ").strip()
            
            if choice == "YES":
                for proc in mt5_processes:
                    print(f"\n🔧 终止进程 {proc['name']} (PID: {proc['pid']})")
                    success, message = kill_process(proc['pid'], proc['name'])
                    
                    if success:
                        print("✅ 进程已终止")
                    else:
                        print(f"❌ 终止失败: {message}")
                
                # 再次尝试删除文件
                print(f"\n🔧 重新尝试删除文件...")
                success, message = try_delete_file(file_path)
                
                if success:
                    print("✅ 文件删除成功！")
                else:
                    print(f"❌ 仍然无法删除: {message}")
                    print("💡 建议重启电脑后再试")
            else:
                print("❌ 操作已取消")
                
        except KeyboardInterrupt:
            print(f"\n❌ 操作被用户取消")
    
    else:
        print("❌ 没有找到明显的MT5进程")
        print("💡 可能的解决方案:")
        print("1. 手动关闭MT5程序")
        print("2. 重启电脑")
        print("3. 以管理员身份运行此脚本")
        print("4. 检查是否有其他程序在使用SQLite数据库")
        
        # 显示通用进程终止命令
        print(f"\n🔧 手动终止命令:")
        print("taskkill /f /im terminal64.exe")
        print("taskkill /f /im metatrader.exe")

if __name__ == "__main__":
    main() 