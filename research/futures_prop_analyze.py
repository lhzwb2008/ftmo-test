#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对 futures_prop_run 的每个产品最佳配置做稳健性检查：按开户年份拆分、相邻张数配置、单名额连续重买一年净收益。

用法：python research/futures_prop_analyze.py [top_n]
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
from dataclasses import replace

import numpy as np

import futures_prop_study as fps
from futures_prop_plans import PLANS
from futures_prop_run import OUT_PKL

OUT_TXT = 'reports/futures_prop_analyze.txt'


def build(plan, key):
    _, ne, nf, cap = key.split('|')
    ne, nf, cap = int(ne), int(nf), float(cap)
    return replace(plan, key=key, fd=replace(plan.fd, contracts=nf), ev_day_cap=cap,
                   ev=replace(plan.ev, contracts=ne) if plan.ev else None)


def slot_job(spec):
    plan, slip = spec
    out = [fps.run_slot(plan, i, slip) for i in range(0, len(fps.DAYS) - fps.HORIZON, fps.STEP * 2)]
    return plan.key, slip, out


def main():
    top = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    obj = pickle.load(open(OUT_PKL, 'rb'))
    summ, rows = obj['summary'], obj['rows']
    fps.load_paths()
    hair, base = fps.SLIPS[1], fps.SLIPS[0]
    starts = list(range(0, len(fps.DAYS) - fps.HORIZON, fps.STEP))
    years = [fps.DAYS[i][0].year for i in starts]
    best = []
    for plan in PLANS:
        keys = [k for (k, s) in summ if s == hair and k.split('|')[0] == plan.key]
        k = max(keys, key=lambda x: summ[(x, hair)]['ratio'])
        best.append((summ[(k, hair)]['ratio'], plan, k, keys))
    best.sort(key=lambda x: -x[0])
    best = best[:top]
    lines = []
    uy = sorted(set(years))
    lines.append('== 按开户年份：出金/成本（打折口径）；2025 年起开户的样本观察期被数据截止日截断，只到 2025-11 左右开户')
    lines.append(f'{"产品":<36}' + ''.join(f'{y:>8}' for y in uy))
    for _, plan, k, _ in best:
        r = rows[(k, hair)]
        vals = []
        for y in uy:
            sub = [x for x, yy in zip(r, years) if yy == y]
            vals.append(np.mean([x['cash'] for x in sub]) / np.mean([x['cost'] for x in sub]))
        lines.append(f'{plan.name:<36}' + ''.join(f'{v:>8.1f}' for v in vals))
    lines.append('\n== 相邻配置（打折口径出金/成本）：同一产品所有张数组合的中位数 / 最佳；默认配置 = 考核 1 张、funded 1 张、无日盈帽')
    for ratio, plan, k, keys in best:
        allr = [summ[(x, hair)]['ratio'] for x in keys]
        dflt = [x for x in keys if x.split('|')[1] in ('0', '1') and x.split('|')[2] == '1' and x.split('|')[3] == '0']
        d = summ[(dflt[0], hair)]['ratio'] if dflt else float('nan')
        lines.append(f'{plan.name:<36} 最佳 {ratio:5.1f}  中位 {np.median(allr):5.1f}  默认 {d:5.1f}  （{len(keys)} 个配置）')
    specs = [(build(plan, k), s) for _, plan, k, _ in best for s in (base, hair)]
    with mp.get_context('fork').Pool(6) as pool:
        res = {(k, s): out for k, s, out in pool.map(slot_job, specs)}
    lines.append('\n== 单个账户名额连续一年（失败 / 爆仓 / 转 Live 后立即重买）：年均成本、年均到手、年均净收益、年均购买次数')
    for _, plan, k, _ in best:
        parts = []
        for s, tag in ((hair, '打折'), (base, '回测')):
            out = np.array(res[(k, s)])
            parts.append(f'{tag} 成本 ${out[:, 0].mean():>5.0f} 到手 ${out[:, 1].mean():>6.0f} 净 ${(out[:, 1] - out[:, 0]).mean():>6.0f} '
                         f'（10% 分位 ${np.percentile(out[:, 1] - out[:, 0], 10):>6.0f}）买 {out[:, 2].mean():.1f} 次')
        lines.append(f'{plan.name:<36} ' + ' | '.join(parts))
    txt = '\n'.join(lines)
    open(OUT_TXT, 'w').write(txt + '\n')
    print(txt)


if __name__ == '__main__':
    main()
