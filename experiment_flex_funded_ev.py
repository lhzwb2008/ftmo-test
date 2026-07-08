# -*- coding: utf-8 -*-
"""
FundedNext Futures Flex $100K Funded 阶段一年期收益/爆仓期望测算
- 用真实两年回测(1.5x, 无日内止损)的日收益序列做自助推演
- Funded 规则: 最大亏损 $2,500 (EOD 追踪高水位, 追至初始余额后锁定) | 无 consistency | 每 5 个交易日可提款
- 提款策略: 每 5 个交易日提走高于初始余额的全部利润(利润落袋防止回吐, 提款后回撤线已锁定在初始余额, 不受影响)
"""
import io
import contextlib
import statistics
from datetime import date

import numpy as np

from backtest import run_backtest
from experiment_flex_leverage import BASE_CONFIG

START = 100000.0
MAX_LOSS = 2500.0
PAYOUT_EVERY = 5          # 每 5 个交易日提款一次
SHARES = {"基础分润80%": 0.80, "加购分润95%": 0.95}
YEAR_DAYS = 252
CHALLENGE_PASS = 0.4873   # 1.5x 挑战期自助通过率(上一实验)
CHALLENGE_E_COST = 282.54  # 期望考试费(含重考)
CHALLENGE_E_DAYS = 69.7    # 期望首过耗时(交易日)


def sim_funded(returns, rng, n=30000):
    results = []
    for _ in range(n):
        seq = rng.choice(returns, size=YEAR_DAYS, replace=True)
        bal = START
        hw = START
        withdrawn = 0.0
        bust_day = None
        for i, r in enumerate(seq):
            # 追踪线: 高水位-2500, 追到初始余额即锁定(不再上移)
            floor = min(hw - MAX_LOSS, START - 0.0) if hw - MAX_LOSS < START else START
            floor = min(hw - MAX_LOSS, START)
            # 日内近似最低净值(策略自带追踪止损, 用 1.2 倍日亏近似盘中极值)
            intraday_low = bal + bal / START * START * min(0.0, r) * 1.2
            day_pnl = bal * r
            bal += day_pnl
            if min(intraday_low, bal) <= floor:
                bust_day = i + 1
                break
            hw = max(hw, bal)
            # 每 5 个交易日提款: 提走高于初始余额的利润
            if (i + 1) % PAYOUT_EVERY == 0 and bal > START:
                withdrawn += bal - START
                bal = START
                # 提款后高水位重置为当前余额(=初始), 回撤线保持锁定在 START-2500 以下不劣化
                hw = max(START, hw - (hw - START))  # hw 回到 START
        # 存活到年末: 把剩余浮盈也算作可提
        if bust_day is None and bal > START:
            withdrawn += bal - START
        results.append((withdrawn, bust_day))
    return results


def main():
    cfg = dict(BASE_CONFIG)
    cfg['leverage'] = 1.5
    cfg['enable_intraday_stop_loss'] = False
    print("运行 1.5x 真实回测获取日收益序列...", flush=True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        daily_df, _, _, _ = run_backtest(cfg)
    returns = daily_df['daily_return'].to_numpy()
    print(f"日收益样本: {len(returns)} 天, 日均 {returns.mean()*100:.3f}%, 日波动 {returns.std()*100:.3f}%")

    rng = np.random.default_rng(11)
    results = sim_funded(returns, rng)

    withdrawn = np.array([w for w, _ in results])
    bust_days = [d for _, d in results if d is not None]
    bust_rate = len(bust_days) / len(results)
    survive_rate = 1 - bust_rate
    avg_life = statistics.mean(bust_days) if bust_days else YEAR_DAYS

    print(f"\n===== Funded $100K, 1.5x, 一年期({YEAR_DAYS}交易日), 每{PAYOUT_EVERY}日提款 =====")
    print(f"一年内爆仓率: {bust_rate:.1%} (存活率 {survive_rate:.1%})")
    print(f"爆仓账户平均存活: {avg_life:.0f} 个交易日")
    print(f"账户毛利润(提款总额, 分润前): 均值 ${withdrawn.mean():,.0f} | 中位数 ${np.median(withdrawn):,.0f} | P10 ${np.percentile(withdrawn,10):,.0f} | P90 ${np.percentile(withdrawn,90):,.0f}")

    for label, share in SHARES.items():
        net = withdrawn * share
        print(f"\n--- {label} ---")
        print(f"一年期望到手: ${net.mean():,.0f} | 中位数 ${np.median(net):,.0f}")
        # 完整管线期望: 含考试费与考试耗时
        pipeline_ev = net.mean() - CHALLENGE_E_COST
        print(f"完整管线期望(扣考试费 ${CHALLENGE_E_COST:.0f}): ${pipeline_ev:,.0f}")
        print(f"含考试期总耗时 ≈ {CHALLENGE_E_DAYS:.0f} + 存活期 交易日")

    # 单账户全生命周期期望(不限一年): 爆仓前平均提款
    per_account = withdrawn.mean()
    avg_account_life = statistics.mean([d if d is not None else YEAR_DAYS for _, d in results])
    print(f"\n===== 单账户口径 =====")
    print(f"平均账户寿命(一年内): {avg_account_life:.0f} 个交易日")
    print(f"单账户年内毛提款均值: ${per_account:,.0f}")
    print(f"爆仓账户中零提款(白干)占比: {np.mean([1 if w == 0 and d is not None else 0 for w, d in results]):.1%}")


if __name__ == '__main__':
    main()
