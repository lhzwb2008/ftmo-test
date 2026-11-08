"""Lucid Trading LucidFlex 50K 独立启动入口。

要买的产品（2026-11-07 价格；官网结账输入优惠码 VAULT 打 7 折）
  Lucid Trading → LucidFlex → 50K，一次性付费、无激活费，分成 90%
  推荐 A：日亏关闭（Daily Loss Limit OFF）+ 40% 一致性 ≈ $115.50（重置 $115）
  备选 B：日亏开启（Daily Loss Limit ON） + 40% 一致性 ≈ $80.50（重置 $80，最便宜）
  日亏设置购买后不能更改，启动本程序时选择对应版本。

账户规则（50K）
  挑战：利润目标 $3,000 | EOD 回撤 $2,000（权益到 $52,100 后底线锁在 $50,100）| 一致性 40% | 最少 3 天
        B 版有软性日亏 $1,200（触及只停手到次日，不爆仓）
  Funded：无一致性 | 5 个 ≥$150 盈利日且周期净利为正可申请出金，每次利润的 50%、上限 $2,000、最少 $500
          申请出金后回撤立即锁到 $50,100 | 最多 5 次出金后转实盘 | 收款 WorkMarket 或加密货币
  账户数：每户最多 5 个 funded，考核 + funded 合计最多 10 个；30 天不交易会删号
  必须在 16:45 ET 前平仓（本程序 15:40 收盘平仓）；新闻时段可交易（LucidDaily 禁止，别买）
  禁止对冲（含跨账户、跨公司）：某账户平仓失败（日志出现 ❌❌❌）时立即到 NT8 手动平仓，避免与其他账户反向持仓
  自动化：帮助中心（2026-08）与 FAQ 写明允许自有 bot 和跟单器，无审批流程；只有旧版 Pro/Direct 合同 PDF
          （2025 年）写着要书面批准，Flex 合同未公开 → 开户后给客服发邮件留个书面确认即可，不是前置门槛
  VPS：官方未写限制（无需另购资格）；VPN 禁止 → 服务器直连，不要挂 VPN 登录网站或平台
  平台：选 CQG 线（NinjaTrader 8 / Tradovate），本程序走 NinjaTrader ATI
  只做 50K：100K / 150K 测算近期只有 4.4 / 5.5 倍

本程序设置（research/futures_prop_robust.py：取「全样本」与「近期」两者较差值最大的配置）
  A：挑战 3 张 / funded 3 张 MNQ；挑战期当日盈亏（含浮盈）≥ $1,080 平仓停手到次日
  B：挑战 1 张 / funded 1 张 MNQ；挑战期日盈帽 $1,080；当日亏损达 $1,200 时同步平仓停手

测算结果（2020-01 起每 5 个交易日开一个新账户，每个账户跟踪 252 个交易日；实盘 7 折口径；天数为购买后的自然日）
  A：全样本 8.0 倍（一年平均到手 $927 / $115.5）| 21% 的账户拿到出金，首次出金中位 53 天 | 单名额一年净 $5,370
     近期（2024-09 后开户）8.1 倍 | 28% 拿到出金，首次出金中位 60 天 | 单名额一年净 $2,002
  B：全样本 23.3 倍（$1,873 / $80.5）| 60% 拿到出金，首次出金中位 122 天 | 单名额一年净 $2,346
     近期 8.1 倍 | 56% 拿到出金，首次出金中位 232 天 | 单名额一年净 $501
  选 A：近期两者倍数相同，A 出金快约 4 倍、每个 funded 名额收益高约 4 倍；
        B 更稳（过半账户能出金）、单次成本低、全样本倍数高，适合想少亏购买费时用。
  Lucid 通过即成为 funded（不能排队），所以一个名额只挂一个考核；同一天多开几个考核结果完全相同，没有分散效果。
  对照 FTMO 100K（3x / 2x）：全样本 23.5 倍 / 近期 9.7 倍

运行：Windows + NinjaTrader 8（开启 ATI）+ longport_data_service.py
  启动顺序：longport_data_service.py → NinjaTrader 连好账户并开 ATI → 本程序
  启动后选日亏版本，再输入 NT8 账户名、规模（50K）、轮次（1 / funded）、当前余额。ATI 查不到持仓，只能空仓时重启。
  通过挑战后以 funded 轮次重启；出金在官网手动申请，出金后重启时输入新的当前余额。
"""

from simulate_futures_core import LUCID_FLEX, LUCID_FLEX_DLL, run_application


def choose_program():
    while True:
        choice = input("该 LucidFlex 账户购买时的日亏设置（1=关闭, 推荐 / 2=开启）: ").strip()
        if choice in ("1", "2"):
            return LUCID_FLEX if choice == "1" else LUCID_FLEX_DLL
        print("错误: 请输入 1 或 2")


if __name__ == "__main__":
    run_application(choose_program())
