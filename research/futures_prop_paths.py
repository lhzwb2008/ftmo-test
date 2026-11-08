#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期货 prop 测算用的逐笔逐分钟权益路径（策略 = strategy_params.py 当前版本，与 prop_payout_study 同一套信号）。

每个交易日一组交易，每笔交易保存入场后逐分钟的「浮亏端 / 浮盈端」和平仓盈亏，单位都是 1x 名义本金的比例
（QQQ 股数 × 当日开盘价）。期货账户按 张数 × MNQ 单张名义 缩放即可，手续费按张另算。
和 prop_payout_study 的区别：保留完整分钟序列而不是 running min/max，日内 trailing 回撤需要知道峰值与谷值的先后。

用法：python research/futures_prop_paths.py [extra_slip ...]   （默认 0 0.035）
"""

from __future__ import annotations

import os as _os
import sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, ROOT)
_os.chdir(ROOT)  # 数据与输出路径均相对仓库根目录

import multiprocessing as mp
import pickle
import sys
import time
from math import floor

import numpy as np
import pandas as pd

import ftmo_ibkr_combo_backtest as combo
import prop_payout_study as pps
from backtest import simulate_day

DATA = 'reports/qqq_minutes_futures_study.csv'
OUT = 'reports/futures_prop_paths.pkl'
PATH_CAPITAL = 200_000.0


def _day(args):
    i, slip = args
    d, g, pc = pps.DAYS[i]
    day_open = float(g['day_open'].iloc[0])
    pos = int(floor(PATH_CAPITAL / day_open))
    cfg = dict(pps.BASE_CFG)
    cfg.update(initial_capital=PATH_CAPITAL, slippage_per_share=0.01 + slip, enable_transaction_fees=False,
               enable_intraday_stop_loss=False, max_daily_loss_amount=0, max_daily_profit_amount=0,
               enable_per_trade_stop_loss=False)
    trades = combo.unpack_simulate_day(simulate_day(g, pc, pps.ALLOWED, pos, cfg, PATH_CAPITAL))[0]
    notional = pos * day_open
    times = g['DateTime'].values
    lo_px, hi_px = g['Low'].values, g['High'].values
    out = []
    for t in trades:
        a = int(np.searchsorted(times, np.datetime64(pd.Timestamp(t['entry_time']))))
        b = int(np.searchsorted(times, np.datetime64(pd.Timestamp(t['exit_time']))))
        sh, ep = t['position_size'], t['entry_price']
        if t['side'] == 'Long':
            adv, fav = (lo_px[a + 1:b] - ep) * sh, (hi_px[a + 1:b] - ep) * sh
        else:
            adv, fav = (ep - hi_px[a + 1:b]) * sh, (ep - lo_px[a + 1:b]) * sh
        f = t['pnl'] / notional
        out.append((np.append(adv / notional, f).astype(np.float32),
                    np.append(fav / notional, f).astype(np.float32), float(f)))
    return out


def main():
    slips = tuple(float(s) for s in sys.argv[1:]) or (0.0, 0.035)
    t0 = time.time()
    pps.MERGED = DATA
    pps.load()
    print(f'加载 {time.time() - t0:.0f}s，样本 {pps.DAYS[0][0]} ~ {pps.DAYS[-1][0]}，{len(pps.DAYS)} 天', flush=True)
    paths = {}
    with mp.get_context('fork').Pool(6) as pool:
        for s in slips:
            paths[s] = pool.map(_day, [(i, s) for i in range(len(pps.DAYS))], chunksize=20)
            print(f'slip={s} 完成 {time.time() - t0:.0f}s', flush=True)
    meta = [(d, float(g['day_open'].iloc[0]), pps.QUIET.get(d, 1.0)) for d, g, _ in pps.DAYS]
    pickle.dump({'paths': paths, 'days': meta}, open(OUT, 'wb'))
    for s in slips:
        r = np.array([sum(t[2] for t in p) for p in paths[s]])
        yrs = (meta[-1][0] - meta[0][0]).days / 365.25
        print(f'slip={s}: 1x 日收益均值 {r.mean():.5f} 标准差 {r.std():.4f}，单利年化 {r.sum() / yrs:+.1%}，'
              f'交易日占比 {np.mean([len(p) > 0 for p in paths[s]]):.0%}')


if __name__ == '__main__':
    main()
