#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑 futures_prop_plans.PLANS 的张数网格并汇总（每个产品取打折口径下 出金/成本 最高的配置）。

用法：python research/futures_prop_run.py            # 全部跑并写 reports/futures_prop_study.pkl / .txt
      python research/futures_prop_run.py report     # 只读 pkl 出报告
"""

from __future__ import annotations

import os as _os
import sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, ROOT)
_os.chdir(ROOT)  # 数据与输出路径均相对仓库根目录

import pickle
import sys
from dataclasses import replace

import numpy as np

import futures_prop_study as fps
from futures_prop_plans import PLANS

OUT_PKL = 'reports/futures_prop_study.pkl'
OUT_TXT = 'reports/futures_prop_study.txt'


def variants(plan):
    dd_ev = plan.ev.dd if plan.ev else None
    n_fd = range(1, max(1, int(plan.fd.dd / 650)) + 1)
    n_ev = range(1, max(1, int(dd_ev / 650)) + 1) if dd_ev else (None,)
    caps = (0.0,)
    if plan.ev is not None and plan.ev_consistency:
        caps = (0.0, round(plan.ev_consistency * plan.target * 0.9))
    out = []
    for a in n_ev:
        for b in n_fd:
            for c in caps:
                p = replace(plan, fd=replace(plan.fd, contracts=b), ev_day_cap=c,
                            ev=replace(plan.ev, contracts=a) if a else None)
                out.append(replace(p, key=f'{plan.key}|{a or 0}|{b}|{int(c)}'))
    return out


def run_all():
    plans = [v for p in PLANS for v in variants(p)]
    print(f'{len(PLANS)} 个产品，{len(plans)} 个配置', flush=True)
    res = fps.run(plans)
    summ = {k: fps.summarize(rows) for k, rows in res.items()}
    pickle.dump({'summary': summ, 'rows': res}, open(OUT_PKL, 'wb'))
    return summ


def report(summ):
    lines = []
    hair, base = fps.SLIPS[1], fps.SLIPS[0]
    best = []
    for plan in PLANS:
        keys = [k for (k, s) in summ if s == hair and k.split('|')[0] == plan.key]
        if not keys:
            continue
        k = max(keys, key=lambda x: summ[(x, hair)]['ratio'])
        best.append((plan, k))
    best.sort(key=lambda x: -summ[(x[1], hair)]['ratio'])
    lines.append('按打折口径（实盘约 7 折）出金/成本最高的配置；括号内为回测口径同配置结果。')
    lines.append('一年到手 = 购买后 252 个交易日内到手出金均值（含失败样本），首次出金 = 拿到出金样本的购买→首次出金自然日中位。')
    for plan, k in best:
        s, b = summ[(k, hair)], summ[(k, base)]
        _, ne, nf, cap = k.split('|')
        cfg = (f'考核 {ne} 张' if ne != '0' else '直接 funded') + f' / funded {nf} 张' + (f' / 考核日盈帽 ${cap}' if cap != '0' else '')
        lines.append(
            f'{plan.name:<34} {cfg:<34} 成本 ${s["cost"]:>6.0f} | 到手 ${s["cash"]:>6.0f}（{b["cash"]:>6.0f}） '
            f'出金/成本 {s["ratio"]:>5.1f}（{b["ratio"]:>5.1f}） | 通过 {s["passed"]:>4.0%} 有出金 {s["p_pay"]:>4.0%} '
            f'| 首次出金中位 {s["first_med"]:>4.0f} 天 | 平均出金 {s["pays"]:.1f} 次 | {plan.auto}')
    txt = '\n'.join(lines)
    open(OUT_TXT, 'w').write(txt + '\n')
    print(txt)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'report':
        report(pickle.load(open(OUT_PKL, 'rb'))['summary'])
    else:
        report(run_all())
