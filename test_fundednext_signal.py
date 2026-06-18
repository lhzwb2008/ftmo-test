#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FundedNext EA 上线前测试脚本

向 trading_signals_fundednext.db 写入测试信号，配合 MT5 上 SQLiteSignalEA_fundednext（DryRun=true）验证硬止损逻辑。

用法:
  python test_fundednext_signal.py              # 交互菜单
  python test_fundednext_signal.py flow         # 完整演练流程（推荐上线前跑一遍）
  python test_fundednext_signal.py buy          # 仅写入 BUY
  python test_fundednext_signal.py close        # 仅写入 CLOSE
  python test_fundednext_signal.py status       # 查看数据库状态
"""

import argparse
import os
import platform
import sqlite3
import sys
import time
from datetime import datetime

DB_NAME = "trading_signals_fundednext.db"
POLL_INTERVAL_SEC = 2
POLL_TIMEOUT_SEC = 30


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_db_path():
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA", os.path.expanduser("~\\AppData\\Roaming"))
        common = os.path.join(appdata, "MetaQuotes", "Terminal", "Common", "Files")
        os.makedirs(common, exist_ok=True)
        return os.path.join(common, DB_NAME)
    return os.path.join(os.getcwd(), DB_NAME)


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            consumed INTEGER DEFAULT 0
        )
    """)
    conn.commit()


def print_prerequisites():
    print("\n=== 上线前检查清单（请在 Windows MT5 上确认）===")
    print("  [ ] 图表已挂载 SQLiteSignalEA_fundednext（不是 Momentum）")
    print("  [ ] EA 已重新编译")
    print("  [ ] DryRun = true（演练模式，不下真单）")
    print("  [ ] Leverage = 3（Funded 账户；需与 simulate_fundednext.py 选 funded 时一致）")
    print("  [ ] InitialBalance = 6000")
    print("  [ ] HardSLRiskPercent = 2.5")
    print("  [ ] 顶部「算法交易」为绿色开启")
    print("  [ ] 底部「专家」标签页已打开，准备看日志")
    print("=" * 50)


def print_expected_logs(action):
    print("\n=== 请在 MT5「专家」标签页查找以下日志 ===")
    if action == "BUY":
        print("  🧪 演练模式已开启...")
        print("  📊 检测到未消费信号，只执行最新的: BUY")
        print("  🧪 [演练] BUY 手数=... 价格=... SL=... 止损距离=... 风险=$150.00")
        print("  ✅ 执行成功")
        print("\n关键验证点: SL= 后面的值必须 > 0，风险=$150.00（6000×2.5%）")
        print("同时确认「交易」面板没有出现新持仓。")
    elif action == "CLOSE":
        print("  📊 检测到未消费信号... CLOSE")
        print("  ⚠️ 无持仓，忽略平仓信号  或  🧪 [演练] CLOSE 将平掉所有持仓")


def get_signals(conn):
    cur = conn.execute(
        "SELECT id, action, created_at, consumed FROM signals ORDER BY id DESC"
    )
    return cur.fetchall()


def show_status():
    db_path = get_db_path()
    print(f"\n[{now_str()}] 数据库: {db_path}")
    print(f"文件存在: {os.path.exists(db_path)}")

    if not os.path.exists(db_path):
        print("❌ 数据库不存在。首次运行 flow/buy 会自动创建。")
        return

    conn = sqlite3.connect(db_path)
    ensure_table(conn)
    rows = get_signals(conn)
    conn.close()

    if not rows:
        print("暂无信号记录。")
        return

    print(f"\n{'ID':<6} {'Action':<8} {'Created At':<22} {'Consumed':<10}")
    print("-" * 50)
    for row in rows[:20]:
        consumed = "是" if row[3] else "否"
        print(f"{row[0]:<6} {row[1]:<8} {row[2]:<22} {consumed:<10}")
    if len(rows) > 20:
        print(f"... 共 {len(rows)} 条，仅显示最近 20 条")


def write_signal(action):
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    ensure_table(conn)

    unconsumed = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE consumed = 0"
    ).fetchone()[0]
    if unconsumed > 0:
        print(f"⚠️  警告: 仍有 {unconsumed} 条未消费信号，EA 只会执行最新一条，旧信号会被跳过。")

    conn.execute("INSERT INTO signals (action) VALUES (?)", (action.upper(),))
    conn.commit()
    signal_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    print(f"[{now_str()}] ✅ 已写入 {action.upper()} 信号 (ID: {signal_id})")
    print(f"数据库路径: {db_path}")
    return signal_id


def wait_for_consumed(signal_id):
    db_path = get_db_path()
    print(f"\n[{now_str()}] 等待 EA 消费信号 ID={signal_id}（最多 {POLL_TIMEOUT_SEC}s）...")

    deadline = time.time() + POLL_TIMEOUT_SEC
    while time.time() < deadline:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT consumed FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()
        conn.close()

        if row and row[0] == 1:
            print(f"[{now_str()}] ✅ 信号 ID={signal_id} 已被 EA 消费")
            return True

        time.sleep(POLL_INTERVAL_SEC)

    print(f"[{now_str()}] ❌ 超时: 信号 ID={signal_id} 在 {POLL_TIMEOUT_SEC}s 内未被消费")
    print("可能原因:")
    print("  1. EA 未挂载或未开启算法交易")
    print("  2. 数据库路径与 EA 的 DBPath/UseCommonPath 不一致")
    print("  3. EA 初始化失败（查看专家日志中的 ❌）")
    return False


def run_flow():
    print_prerequisites()
    try:
        input("\n确认 MT5 已按上述清单配置好后，按回车开始演练测试...")
    except EOFError:
        print("非交互环境，直接开始测试。")

    print(f"\n[{now_str()}] === 步骤 1/2: 写入 BUY 信号 ===")
    signal_id = write_signal("BUY")
    print_expected_logs("BUY")

    if not wait_for_consumed(signal_id):
        print("\n❌ 演练失败: EA 未响应 BUY 信号，请检查 MT5 后再重试。")
        return False

    print(f"\n[{now_str()}] === 步骤 2/2: 写入 CLOSE 信号（清理）===")
    close_id = write_signal("CLOSE")
    print_expected_logs("CLOSE")
    wait_for_consumed(close_id)

    print("\n" + "=" * 50)
    print("✅ 数据库侧测试通过（信号已被 EA 消费）")
    print("\n请人工确认 MT5「专家」日志:")
    print("  1. 出现 🧪 [演练] BUY ... SL=xxx（SL 必须 > 0）")
    print("  2. 交易面板无新持仓")
    print("\n若以上均 OK，可将 EA 的 DryRun 改为 false，再跑:")
    print("  python test_fundednext_signal.py buy")
    print("然后检查第一笔真单的 S/L 列是否有止损价格，确认后正式上线。")
    print("=" * 50)
    return True


def interactive_menu():
    print_prerequisites()
    while True:
        print("\n--- FundedNext 测试菜单 ---")
        print("  1. flow   - 完整演练流程（推荐）")
        print("  2. buy    - 写入 BUY")
        print("  3. sell   - 写入 SELL")
        print("  4. close  - 写入 CLOSE")
        print("  5. status - 查看数据库")
        print("  0. 退出")
        try:
            choice = input("请选择: ").strip().lower()
        except EOFError:
            break

        if choice in ("0", "q", "quit", "exit"):
            break
        if choice in ("1", "flow"):
            run_flow()
        elif choice in ("2", "buy"):
            sid = write_signal("BUY")
            print_expected_logs("BUY")
            wait_for_consumed(sid)
        elif choice in ("3", "sell"):
            sid = write_signal("SELL")
            print_expected_logs("SELL")
            wait_for_consumed(sid)
        elif choice in ("4", "close"):
            sid = write_signal("CLOSE")
            print_expected_logs("CLOSE")
            wait_for_consumed(sid)
        elif choice in ("5", "status"):
            show_status()
        else:
            print("无效选项")


def main():
    parser = argparse.ArgumentParser(description="FundedNext EA 上线前测试")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["flow", "buy", "sell", "close", "status"],
        help="测试命令；省略则进入交互菜单",
    )
    args = parser.parse_args()

    if args.command is None:
        interactive_menu()
        return

    if args.command == "status":
        show_status()
        return

    if args.command == "flow":
        ok = run_flow()
        sys.exit(0 if ok else 1)

    signal_id = write_signal(args.command)
    print_expected_logs(args.command.upper())
    ok = wait_for_consumed(signal_id)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
