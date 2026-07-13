# -*- coding: utf-8 -*-
"""
NinjaTrader 8 ATI 链路实时验证脚本（在 Windows 服务器上运行）

流程:
    1. 发送 1 手 MNQ 市价买单 → 你去 NT8 的 Orders/Positions 面板确认成交
    2. 按回车后发送 CLOSEPOSITION 平仓 → 再确认持仓清零
全程只动 1 手, 风险约每点 $2。期货近 23 小时交易, 盘中随时可验证。

运行: python test_nt8_ati.py
"""
import sys

from ninjatrader_client import create_client_or_none

# 与 simulate_futures_core.py 顶部保持一致
NT8_ACCOUNT = "FNFTCHWENBOZHANG87184"
NT8_INSTRUMENT = "MNQ 09-26"
NT8_INCOMING_DIR = None  # None = 默认 ~/Documents/NinjaTrader 8/incoming


def main():
    print("=" * 60)
    print("NinjaTrader 8 ATI 链路验证")
    print("=" * 60)
    client = create_client_or_none(NT8_ACCOUNT, NT8_INSTRUMENT, NT8_INCOMING_DIR)
    if client is None:
        print("客户端初始化失败, 请检查上方错误信息")
        sys.exit(1)

    print("\n本测试将: 1) 市价买入 1 手 MNQ  2) 你确认成交后再平仓")
    answer = input("确认开始? (yes/no): ").strip().lower()
    if answer not in ("y", "yes"):
        print("已取消")
        sys.exit(0)

    oif = client.place_market_order("Buy", 1)
    print(f"\n✅ 开仓指令已写入: {oif}")
    print("请立即到 NT8 检查:")
    print("  - Orders 面板: 应出现 1 手 MNQ 市价买单(状态 Filled)")
    print("  - Positions 面板: 应出现 1 手多头持仓")
    print("  - Log 面板: 不应出现 ATI 相关红色/橙色错误")

    input("\n确认成交后, 按回车发送平仓指令...")
    oif, _ = client.close_all()
    print(f"\n✅ 平仓指令已写入: {oif}")
    print("请再次检查 NT8:")
    print("  - Positions 面板: MNQ 持仓应清零")
    print("  - 如 30 秒后仍有持仓, 请手动平掉并把 NT8 Log 面板截图发回排查")

    input("\n确认持仓清零后, 按回车结束...")
    print("\n🎉 ATI 链路验证完成! 可以启动 simulate_futures_fundednext.py 或 simulate_futures_tradeify.py")


if __name__ == "__main__":
    main()
