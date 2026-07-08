# -*- coding: utf-8 -*-
"""
FundedNext Futures Flex $100K 参数实验
- 用真实两年 1 分钟回测(backtest.py)产出不同 [杠杆 x 日内止损] 组合的日度收益序列
- 再按 Flex 规则做挑战推演: 5%目标 / 2.5% EOD追踪最大亏损 / 40% consistency / 无日损限制 / 无时间限制
- 核心指标: 期望通过总耗时(含失败重考) 与 期望考试费用, 而非单纯通过率
"""
import io
import sys
import contextlib
import statistics
from datetime import date

import numpy as np
import pandas as pd

from backtest import run_backtest

DATA_PATH = "../quantra/qqq_longport_2year.csv"

BASE_CONFIG = {
    'data_path': DATA_PATH,
    'ticker': 'QQQ',
    'initial_capital': 100000,
    'lookback_days': 1,
    'start_date': date(2024, 7, 1),
    'end_date': date(2026, 6, 30),
    'check_interval_minutes': 15,
    'enable_transaction_fees': True,
    'transaction_fee_per_share': 0.008166,
    'slippage_per_share': 0.01,
    'trading_start_time': (9, 40),
    'trading_end_time': (15, 40),
    'max_positions_per_day': 10,
    'print_daily_trades': False,
    'print_trade_details': False,
    'K1': 1, 'K2': 1,
    'enable_k_side_adjustment': True,
    'k_side_adjustment': {'long': {'metric': 'minutes_from_open', 'min': 120, 'k_if_true': 0.9, 'k_if_false': 1.0}},
    'use_vwap': False,
    'enable_per_trade_stop_loss': False,
    'enable_trailing_take_profit': True,
    'trailing_tp_activation_pct': 0.001,
    'trailing_tp_callback_pct': 0.7,
    'entry_trend_filter': {'metric': 'er5', 'min': 0.1},
}

# Flex $100K 规则
TARGET_PCT = 0.05          # $5,000
MAX_LOSS_PCT = 0.025       # $2,500 EOD 追踪(高水位按每日收盘更新, 追到初始余额封顶)
CONSISTENCY = 0.40         # 最佳单日利润 <= 总利润40%(仅挑战期)
CHALLENGE_FEE = 129.99
RESET_FEE = 144.99


def get_daily_returns(leverage, stop_pct):
    cfg = dict(BASE_CONFIG)
    cfg['leverage'] = leverage
    if stop_pct is not None:
        cfg['enable_intraday_stop_loss'] = True
        cfg['intraday_stop_loss_pct'] = stop_pct
    else:
        cfg['enable_intraday_stop_loss'] = False
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        daily_df, monthly, trades, metrics = run_backtest(cfg)
    r = daily_df['daily_return'].to_numpy()
    return r, metrics


def run_challenge(returns, stop_pct):
    """单次挑战推演. returns: 从某起点开始的日收益序列. 返回 (result, days)
    result: 'pass' / 'fail' / 'end'(数据用尽未出结果)"""
    bal = 1.0
    hw = 1.0            # EOD 高水位(封顶不设: Flex 持续追踪; 保守处理为一直追踪)
    best_day = 0.0
    total_profit = 0.0
    for i, r in enumerate(returns):
        floor_level = hw - MAX_LOSS_PCT
        # 日内近似最低净值: 触发日内止损时亏损被截断在 stop_pct 附近(留 20% 滑点缓冲)
        worst_intraday = bal * (1 + min(0.0, r) * 1.2)
        if stop_pct is not None:
            worst_intraday = max(worst_intraday, bal * (1 - stop_pct * 1.2))
        if worst_intraday <= floor_level:
            return 'fail', i + 1
        day_pnl = bal * r
        bal += day_pnl
        if bal <= floor_level:
            return 'fail', i + 1
        hw = max(hw, bal)
        total_profit = bal - 1.0
        best_day = max(best_day, day_pnl)
        if total_profit >= TARGET_PCT and best_day <= CONSISTENCY * total_profit:
            return 'pass', i + 1
    return 'end', len(returns)


def evaluate(returns, stop_pct, n_boot=20000, seed=7):
    # 1) 走查式: 每个交易日作为起点
    wf = [run_challenge(returns[s:], stop_pct) for s in range(len(returns) - 20)]
    wf_valid = [x for x in wf if x[0] != 'end']
    wf_pass = sum(1 for x in wf_valid if x[0] == 'pass') / max(len(wf_valid), 1)

    # 2) 自助法: 从经验分布重采样(保留肥尾), 每次最多400天
    rng = np.random.default_rng(seed)
    res = []
    for _ in range(n_boot):
        seq = rng.choice(returns, size=400, replace=True)
        res.append(run_challenge(seq, stop_pct))
    passes = [d for r_, d in res if r_ == 'pass']
    fails = [d for r_, d in res if r_ == 'fail']
    p = len(passes) / len(res)
    avg_pass_d = statistics.mean(passes) if passes else float('nan')
    avg_fail_d = statistics.mean(fails) if fails else 0.0
    # 期望首次通过总耗时: 失败次数期望 (1-p)/p 次, 每次平均 avg_fail_d 天, 最后一次通过 avg_pass_d 天
    if p > 0:
        e_days = (1 - p) / p * avg_fail_d + avg_pass_d
        e_cost = CHALLENGE_FEE + (1 - p) / p * RESET_FEE
    else:
        e_days, e_cost = float('inf'), float('inf')
    return dict(wf_pass=wf_pass, boot_pass=p, avg_pass_d=avg_pass_d,
                avg_fail_d=avg_fail_d, e_days=e_days, e_cost=e_cost)


def main():
    combos = []
    for lev in [1.5, 2, 3, 4]:
        for sp in [None, 0.02, 0.015, 0.01]:
            combos.append((lev, sp))

    rows = []
    for lev, sp in combos:
        sp_txt = f"{sp:.1%}" if sp else "无"
        print(f"回测: 杠杆={lev}x 日内止损={sp_txt} ...", flush=True)
        r, m = get_daily_returns(lev, sp)
        ev = evaluate(r, sp)
        ann = (1 + pd.Series(r)).prod() ** (252 / len(r)) - 1
        rows.append(dict(lev=lev, stop=sp_txt, ann_ret=ann, mdd=m.get('mdd'),
                         sharpe=m.get('sharpe_ratio'), **ev))
        print(f"  两年年化={ann:.1%} 精确MDD={m.get('mdd'):.2%} | "
              f"走查通过率={ev['wf_pass']:.0%} 自助通过率={ev['boot_pass']:.0%} | "
              f"通过均需{ev['avg_pass_d']:.0f}天 失败均{ev['avg_fail_d']:.0f}天 | "
              f"期望首过总耗时={ev['e_days']:.0f}天 期望费用=${ev['e_cost']:.0f}", flush=True)

    df = pd.DataFrame(rows)
    df.to_csv("flex_experiment_results.csv", index=False)
    print("\n===== 按期望首过总耗时排序(时间效率优先) =====")
    print(df.sort_values('e_days')[['lev', 'stop', 'ann_ret', 'mdd', 'boot_pass', 'avg_pass_d', 'e_days', 'e_cost']].to_string(index=False))


if __name__ == '__main__':
    main()
