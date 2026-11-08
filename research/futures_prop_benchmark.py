#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CFD 基准：FTMO 100K 2-Step 按与 futures_prop_study 相同的口径（购买后 252 个交易日内到手 / 购买成本、首次出金用时）。

考核与 funded 规则同 prop_payout_study / prop_refund_study：日亏 5%（EA 4.75%）、总亏 10% 静态、每阶段最少 4 天；
funded 第一笔交易满 14 天后出金，之后每 14 天，80% 分成，提走全部利润，首次出金退考核费。
"""

from __future__ import annotations

import os as _os
import sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, ROOT)
_os.chdir(ROOT)  # 数据与输出路径均相对仓库根目录

import multiprocessing as mp
import sys

import numpy as np

import prop_payout_study as pps

FEE = 600.0
HORIZON = 252
STEP = 5


def funded(eng, i, end, size):
    cap, cash, n, last, first = size, 0.0, 0, pps.DAYS[i][0], None
    for j in range(i, end):
        pnl, low, _, _ = eng.day(j, cap)
        if cap - low >= size * 0.05 or low <= size * 0.90:
            return cash, n, first
        cap += pnl
        if (pps.DAYS[j][0] - last).days >= 14 and cap - size >= 50:
            cash += (cap - size) * 0.8 + (FEE if n == 0 else 0.0)
            n += 1
            first = first or j
            cap, last = size, pps.DAYS[j][0]
    return cash, n, first


def job(spec):
    ev_lev, fd_lev, slip = spec
    size = 100_000.0
    ev = pps.Engine(ev_lev, size, slip, daily_stop=size * 0.05 * 0.95)
    fd = pps.Engine(fd_lev, size, slip, daily_stop=size * 0.05 * 0.95)
    rows = []
    for i in range(0, len(pps.DAYS) - HORIZON, STEP):
        end = i + HORIZON
        st, j = pps.phase(ev, i, 0.10, 0.05, 0.10, min_days=4)
        if st == 'pass' and j + 1 < end:
            st, j = pps.phase(ev, j + 1, 0.05, 0.05, 0.10, min_days=4)
        if st != 'pass' or j + 1 >= end:
            rows.append((0.0, None))
            continue
        cash, n, first = funded(fd, j + 1, end, size)
        rows.append((cash, None if first is None else (pps.DAYS[first][0] - pps.DAYS[i][0]).days))
    return spec, rows


def main():
    pps.MERGED = 'reports/qqq_minutes_futures_study.csv'
    pps.load()
    slips = (0.0, float(sys.argv[1]) if len(sys.argv) > 1 else 0.035)
    pps.build_paths(slips)
    specs = [(e, f, s) for s in slips for e, f in ((2.0, 1.5), (3.0, 1.5), (3.0, 2.0), (4.0, 2.0))]
    with mp.get_context('fork').Pool(6) as pool:
        for (e, f, s), rows in sorted(pool.map(job, specs), key=lambda x: (x[0][2], x[0][0], x[0][1])):
            cash = np.mean([r[0] for r in rows])
            firsts = [r[1] for r in rows if r[1] is not None]
            print(f'FTMO 100K 考核 {e:g}x / funded {f:g}x {"打折" if s else "回测"}: 一年到手 ${cash:,.0f}，'
                  f'出金/成本 {cash / FEE:.1f}，有出金 {len(firsts) / len(rows):.0%}，'
                  f'首次出金中位 {np.median(firsts) if firsts else float("nan"):.0f} 天 / 均值 {np.mean(firsts) if firsts else float("nan"):.0f} 天')


if __name__ == '__main__':
    main()
