#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""考核单日盈利帽扫描：FundedNext Flex 50K（2/2 张）与 100K（3/3 张），打折口径。

看三件事：通过率、通过所需天数（自然日）、出金/成本；按开户年份拆开，检查盈利帽是否只在某段历史有效（过拟合）。
用法：python research/futures_cap_sweep.py
"""

from __future__ import annotations

import os as _os
import sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, ROOT)
_os.chdir(ROOT)  # 数据与输出路径均相对仓库根目录

import multiprocessing as mp

import numpy as np

import futures_prop_study as fps
from futures_prop_analyze import build
from futures_prop_plans import PLANS

OUT_TXT = 'reports/futures_cap_sweep.txt'
SLIP = fps.SLIPS[1]
CASES = (('fnf50', 2, 2, (0, 500, 700, 900, 1000, 1100, 1300, 1600, 2000)),
         ('fnf100', 3, 3, (0, 1000, 1400, 1800, 2000, 2200, 2600, 3200, 4000)))


def job(spec):
    plan, = spec
    return plan.key, [fps.run_account(plan, i, SLIP) for i in range(0, len(fps.DAYS) - fps.HORIZON, fps.STEP)]


def stats(rows):
    passed = [r['pass_days'] for r in rows if r['pass_days'] is not None]
    ratio = np.mean([r['cash'] for r in rows]) / np.mean([r['cost'] for r in rows])
    return len(passed) / len(rows), (np.median(passed) if passed else np.nan), ratio


def main():
    fps.load_paths()
    by_key = {p.key: p for p in PLANS}
    specs = [(build(by_key[k], f'{k}|{ne}|{nf}|{cap}'),) for k, ne, nf, caps in CASES for cap in caps]
    with mp.get_context('fork').Pool(6) as pool:
        res = dict(pool.map(job, specs))
    starts = list(range(0, len(fps.DAYS) - fps.HORIZON, fps.STEP))
    years = sorted({fps.DAYS[i][0].year for i in starts})
    lines = []
    for k, ne, nf, caps in CASES:
        target = by_key[k].target
        lines.append(f'\n== {by_key[k].name} 考核 {ne} 张 / funded {nf} 张（打折口径；通过天数为自然日中位）')
        lines.append(f'{"日盈帽":>8} {"占目标":>6} | {"全样本 通过率 / 通过天数 / 出金成本比":<30} | '
                     f'{"近期(2024-09起)":<24} | 各开户年份 出金/成本')
        for cap in caps:
            rows = res[f'{k}|{ne}|{nf}|{cap}']
            rec = [r for i, r in zip(starts, rows) if str(fps.DAYS[i][0]) >= '2024-09-01']
            p, d, ra = stats(rows)
            rp, rd, rr = stats(rec)
            yr = ' '.join(f'{y % 100:02d}:{stats([r for i, r in zip(starts, rows) if fps.DAYS[i][0].year == y])[2]:4.1f}'
                          for y in years)
            label = '不设' if cap == 0 else f'${cap}'
            pct = '-' if cap == 0 else f'{cap / target:.0%}'
            lines.append(f'{label:>8} {pct:>6} | {p:4.0%} / {d:4.0f} 天 / {ra:5.1f}             | '
                         f'{rp:4.0%} / {rd:4.0f} 天 / {rr:5.1f} | {yr}')
    txt = '\n'.join(lines)
    open(OUT_TXT, 'w').write(txt + '\n')
    print(txt)


if __name__ == '__main__':
    main()
