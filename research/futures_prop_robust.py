#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""稳健配置：每个产品取 min(全样本, 近期) 出金/成本 最大的张数配置（打折口径），再算单名额连续一年净收益。

近期 = 2024-09 之后开户（观察到 2026-11）。同时输出 FTMO 100K 同口径近期对照。
用法：python research/futures_prop_robust.py
"""

from __future__ import annotations

import os as _os
import sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, ROOT)
_os.chdir(ROOT)  # 数据与输出路径均相对仓库根目录

import multiprocessing as mp
import pickle

import numpy as np

import futures_prop_benchmark as fb
import futures_prop_study as fps
import prop_payout_study as pps
from futures_prop_analyze import build
from futures_prop_plans import PLANS
from futures_prop_run import OUT_PKL

RECENT = '2024-09-01'
OUT_TXT = 'reports/futures_prop_robust.txt'


def slot_job(spec):
    plan, slip, idx = spec
    return plan.key, slip, [fps.run_slot(plan, i, slip) for i in idx]


def main():
    obj = pickle.load(open(OUT_PKL, 'rb'))
    summ, rows = obj['summary'], obj['rows']
    fps.load_paths()
    hair = fps.SLIPS[1]
    starts = list(range(0, len(fps.DAYS) - fps.HORIZON, fps.STEP))
    rec = [n for n, i in enumerate(starts) if str(fps.DAYS[i][0]) >= RECENT]

    def ratio(k, sel=None):
        r = rows[(k, hair)] if sel is None else [rows[(k, hair)][n] for n in sel]
        return np.mean([x['cash'] for x in r]) / np.mean([x['cost'] for x in r]), r

    picks = []
    for plan in PLANS:
        keys = [k for (k, s) in summ if s == hair and k.split('|')[0] == plan.key]
        k = max(keys, key=lambda x: min(ratio(x)[0], ratio(x, rec)[0]))
        ra, _ = ratio(k)
        rr, sub = ratio(k, rec)
        paid = [x['first'] for x in sub if x['first'] is not None]
        picks.append((min(ra, rr), plan, k, ra, rr, len(paid) / len(sub), np.median(paid) if paid else np.nan,
                      np.mean([x['cash'] for x in sub])))
    picks.sort(key=lambda x: -x[0])
    lines = ['== 稳健配置（打折口径）：min(全样本, 近期) 最大；近期 = 2024-09 起开户']
    for _, plan, k, ra, rr, pp, fm, cash in picks:
        _, ne, nf, cap = k.split('|')
        cfg = (f'考核 {ne} 张' if ne != '0' else '直接 funded') + f' / funded {nf} 张' + (f' / 考核日盈帽 ${cap}' if cap != '0' else '')
        lines.append(f'{plan.name:<34} {cfg:<32} 全样本 {ra:5.1f} | 近期 {rr:5.1f}，近期一年到手 ${cash:>5.0f}，'
                     f'有出金 {pp:4.0%}，首次出金中位 {fm:4.0f} 天 | 成本 ${summ[(k, hair)]["cost"]:.0f}')
    top = picks[:10]
    idx_all = list(range(0, len(fps.DAYS) - fps.HORIZON, fps.STEP * 2))
    idx_rec = [i for i in range(0, len(fps.DAYS) - fps.HORIZON, fps.STEP) if str(fps.DAYS[i][0]) >= RECENT]
    specs = [(build(p, k), hair, idx) for _, p, k, *_ in top for idx in (idx_all, idx_rec)]
    with mp.get_context('fork').Pool(6) as pool:
        out = pool.map(slot_job, specs)
    lines.append('\n== 单个账户名额连续一年（失败即重买），打折口径：年均净收益（全样本 / 近期）')
    for n, (_, p, k, *_) in enumerate(top):
        a, r = np.array(out[2 * n][2]), np.array(out[2 * n + 1][2])
        lines.append(f'{p.name:<34} 全样本 净 ${(a[:, 1] - a[:, 0]).mean():>6.0f}（买 {a[:, 2].mean():.1f} 次）| '
                     f'近期 净 ${(r[:, 1] - r[:, 0]).mean():>6.0f}（成本 ${r[:, 0].mean():.0f}，到手 ${r[:, 1].mean():.0f}，'
                     f'买 {r[:, 2].mean():.1f} 次，亏钱名额占 {np.mean(r[:, 1] < r[:, 0]):.0%}）')
    pps.MERGED = 'reports/qqq_minutes_futures_study.csv'
    pps.load()
    pps.build_paths((hair,))
    with mp.get_context('fork').Pool(2) as pool:
        bench = pool.map(fb.job, [(3.0, 2.0, hair), (2.0, 1.5, hair)])
    bstarts = list(range(0, len(pps.DAYS) - fb.HORIZON, fb.STEP))
    lines.append('\n== FTMO 100K 同口径对照（打折口径，成本 $600）')
    for (e, f, _), rws in bench:
        ra = np.mean([x[0] for x in rws]) / fb.FEE
        sub = [x for i, x in zip(bstarts, rws) if str(pps.DAYS[i][0]) >= RECENT]
        paid = [x[1] for x in sub if x[1] is not None]
        lines.append(f'FTMO 100K 考核 {e:g}x / funded {f:g}x  全样本 {ra:5.1f} | 近期 {np.mean([x[0] for x in sub]) / fb.FEE:5.1f}，'
                     f'近期一年到手 ${np.mean([x[0] for x in sub]):.0f}，有出金 {len(paid) / len(sub):.0%}，'
                     f'首次出金中位 {np.median(paid) if paid else np.nan:.0f} 天')
    txt = '\n'.join(lines)
    open(OUT_TXT, 'w').write(txt + '\n')
    print(txt)


if __name__ == '__main__':
    main()
