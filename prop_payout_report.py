#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汇总 prop_payout_study.py 的结果。"""

import pickle
import sys

import numpy as np

d = pickle.load(open(sys.argv[1] if len(sys.argv) > 1 else 'reports/prop_payout_study.pkl', 'rb'))
R, dates, slip_h = d['results'], d['dates'], d['slip']


def tag(slip):
    return '回测' if slip == 0 else '实盘7折'


def rate(rows):
    done = [r for r in rows if r[1] in ('pass', 'fail')]
    p = [r for r in done if r[1] == 'pass']
    days = [r[2] - r[0] + 1 for r in p]
    return len(p) / max(len(done), 1), len(done), (np.median(days) if days else float('nan'))


def funded(res_list, fee=0.0):
    rs = [r for r in res_list if r[0] != 'open']
    cash = np.array([r[1] for r in rs])
    bust = np.mean([r[0] == 'bust' for r in rs])
    return dict(n=len(rs), bust=bust, mean=cash.mean(), med=np.median(cash),
                p_pay=np.mean(cash > 0), p_fee=np.mean(cash > fee), p10=np.percentile(cash, 10),
                p90=np.percentile(cash, 90))


print(f'样本 {dates[0]} ~ {dates[-1]}，实盘7折 = 每股额外滑点 ${slip_h}\n')

print('== FTMO 2-Step 100K（P1+P2 全过 = 拿到 funded）')
for (k, lev, slip, _), rows in sorted(R.items(), key=lambda x: (x[0][0], x[0][2], x[0][1])):
    if k == 'ftmo':
        p, n, md = rate(rows)
        print(f'  {tag(slip)} {lev:g}x: 两阶段通过率 {p:.1%}（n={n}，通过中位 {md:.0f} 个交易日）')

print('\n== Aqua Standard 2-Step 100K')
for (k, lev, slip, _), rows in sorted(R.items(), key=lambda x: (x[0][0], x[0][2], x[0][1])):
    if k != 'aqua_std':
        continue
    chain = [r for r in rows if r[0] != 'funded_only']
    p, n, md = rate(chain)
    f = funded([r[2] for r in rows if r[0] == 'funded_only'])
    ev = p * f['mean']
    print(f'  {tag(slip)} {lev:g}x: 两阶段通过 {p:.1%}（中位 {md:.0f} 天）| funded 12 个月：爆仓 {f["bust"]:.0%}，'
          f'出金均值 ${f["mean"]:,.0f} 中位 ${f["med"]:,.0f}（P10 ${f["p10"]:,.0f} / P90 ${f["p90"]:,.0f}），'
          f'有出金 {f["p_pay"]:.0%} | 买一次考试期望到手 ${ev:,.0f} → 保本考试费 ${ev:,.0f}')

print('\n== Aqua Pay Later 200K Challenge（+3%，5% trailing）')
for (k, lev, slip, _), rows in sorted(R.items(), key=lambda x: (x[0][0], x[0][2], x[0][1])):
    if k == 'pl_challenge':
        p, n, md = rate(rows)
        print(f'  {tag(slip)} {lev:g}x: 通过 {p:.1%}（中位 {md:.0f} 天）')

FEE = 597.93
print(f'\n== Aqua Pay Later 200K funded，12 个月（激活费 ${FEE}）')
for (k, lev, slip, cap), rows in sorted(R.items(), key=lambda x: (x[0][0], x[0][2], x[0][1], x[0][3])):
    if k != 'pl_funded':
        continue
    capname = f'日盈帽${cap:,.0f}' if cap else '无日盈帽'
    for which, label in (('new', '新开户 20万'), ('now', '你现在 19.7万/高点20.3万')):
        f = funded([r[2] for r in rows if r[0] == which], FEE)
        print(f'  {tag(slip)} {lev:g}x {capname} {label}: 爆仓 {f["bust"]:.0%}，出金均值 ${f["mean"]:,.0f} '
              f'中位 ${f["med"]:,.0f}（P10 ${f["p10"]:,.0f} / P90 ${f["p90"]:,.0f}），有出金 {f["p_pay"]:.0%}，'
              f'超过激活费 {f["p_fee"]:.0%}，净期望 ${f["mean"] - FEE:,.0f}')
