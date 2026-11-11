#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FundedNext Flex 50K 多名额时间线：每个名额从指定日期买考核，考核失败 / funded 爆仓 / 满 5 次出金后次日重买。

交易按回测口径（不加额外滑点）跑，只在每笔出金到手时乘 PAYOUT_HAIRCUT。
张数配置 = 考核张数|funded 张数|考核日盈帽（$900 对应 40% 一致性，0 = 不设）；simulate_futures_fundednext.py 现用 2|2|900。
比较几种 5 名额买法的成本、到手和「扎堆爆仓」程度（同一天爆几张），并打印推荐买法的逐月现金流。

用法：python research/futures_fn50_timeline.py [年数]（默认 2，从数据末日往前推）
"""

from __future__ import annotations

import os as _os
import sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, ROOT)
_os.chdir(ROOT)  # 数据与输出路径均相对仓库根目录

import sys
from collections import Counter, defaultdict

import numpy as np

import futures_prop_study as fps
from futures_prop_analyze import build
from futures_prop_plans import PLANS

PAYOUT_HAIRCUT = 0.7
SLIP = 0.0
MONTH = 21
ROLL_STEP = 10
RECENT = '2023-01-01'
CONFIGS = ('2|2|900', '2|2|0', '2|1|900', '2|1|0', '1|2|0', '1|1|0')
OUT_TXT = 'reports/futures_fn50_timeline.txt'


def events(plan, i0, end):
    """返回 [(交易日下标, 类型, 金额)]；类型 buy / pass / pay / ev_bust / fd_bust / done / open。"""
    ev = []
    j = i0
    while j < end:
        ev.append((j, 'buy', plan.fee))
        st, k = fps.run_eval(plan, j, SLIP, end)
        if st == 'fail':
            ev.append((k, 'ev_bust', 0.0))
            j = k + 1
            continue
        if st == 'open':
            ev.append((k, 'open', 0.0))
            break
        ev.append((k, 'pass', 0.0))
        if k + 1 >= end:
            break
        st, _, _, pays, k2 = fps.run_funded(plan, k + 1, SLIP, end)
        prev = 0.0
        for jp, cum in pays:
            ev.append((jp, 'pay', (cum - prev) * PAYOUT_HAIRCUT))
            prev = cum
        ev.append((k2, {'bust': 'fd_bust', 'live': 'done', 'alive': 'open'}[st], 0.0))
        if st == 'alive':
            break
        j = k2 + 1
    return ev


def summary(slot_ev):
    cost = sum(a for ev in slot_ev for _, t, a in ev if t == 'buy')
    cash = sum(a for ev in slot_ev for _, t, a in ev if t == 'pay')
    c = Counter(t for ev in slot_ev for _, t, _ in ev)
    bust_days = Counter(j for ev in slot_ev for j, t, _ in ev if t in ('ev_bust', 'fd_bust'))
    return dict(cost=cost, cash=cash, net=cash - cost, buy=c['buy'], ev_bust=c['ev_bust'], fd_bust=c['fd_bust'],
                done=c['done'], pays=c['pay'], bust_days=len(bust_days),
                all_bust=sum(1 for v in bust_days.values() if v >= len(slot_ev)),
                max_bust=max(bust_days.values()) if bust_days else 0)


def render_slot(ev):
    days = fps.DAYS
    out, n_buy, d_buy, n_pay = [], 0, None, 0
    for j, t, a in ev:
        d = days[j][0]
        if t == 'buy':
            n_buy, d_buy, n_pay = n_buy + 1, d, 0
            out.append(f'{d}  第 {n_buy} 张考核买入 ${a:.0f}')
        elif t == 'ev_bust':
            out[-1] += f' → {d} 考核爆仓（{(d - d_buy).days} 天）'
        elif t == 'pass':
            out[-1] += f' → {d} 通过（{(d - d_buy).days} 天），次日起 funded'
        elif t == 'pay':
            n_pay += 1
            out.append(f'            {d}  第 {n_pay} 次出金，到手 ${a:,.0f}')
        elif t == 'fd_bust':
            out.append(f'            {d} funded 爆仓')
        elif t == 'done':
            out.append(f'            {d} 满 5 次出金，账户结束')
        else:
            out.append(f'            到 {d} 仍在进行')
    return out


def render_months(slot_ev):
    days = fps.DAYS
    n = defaultdict(Counter)
    amt = defaultdict(lambda: defaultdict(float))
    for ev in slot_ev:
        for j, t, a in ev:
            key = days[j][0].strftime('%Y-%m')
            n[key][t] += 1
            amt[key][t] += a
    out = ['月份      买考核  考核费  考核爆仓  通过  funded爆仓  满5次  出金次数  到手       当月净     累计净']
    cum = 0.0
    for key in sorted(n):
        c, s = n[key], amt[key]
        net = s['pay'] - s['buy']
        cum += net
        out.append(f'{key}  {c["buy"]:>5}  ${s["buy"]:>5.0f}  {c["ev_bust"]:>7}  {c["pass"]:>4}  {c["fd_bust"]:>9}  {c["done"]:>5}  '
                   f'{c["pay"]:>7}  ${s["pay"]:>7,.0f}  ${net:>+7,.0f}  ${cum:>+8,.0f}')
    return out


def fmt(s):
    return (f'买 {s["buy"]:>3} 张 ${s["cost"]:>6,.0f} | 到手 ${s["cash"]:>7,.0f} | 净 ${s["net"]:>+7,.0f} | '
            f'考核爆 {s["ev_bust"]:>2} funded 爆 {s["fd_bust"]:>2} 满5次 {s["done"]} 出金 {s["pays"]:>2} 次 | '
            f'有爆仓的天数 {s["bust_days"]:>2}，单日最多爆 {s["max_bust"]} 张，全军覆没 {s["all_bust"]:>2} 天')


def main():
    years = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    fps.load_paths()
    base = {p.key: p for p in PLANS}['fnf50']
    plans = {c: build(base, f'fnf50|{c}') for c in CONFIGS}
    end = len(fps.DAYS)
    i0 = end - fps.HORIZON * years
    days = fps.DAYS
    lines = [f'FundedNext Flex 50K，{days[i0][0]} ~ {days[-1][0]}（{end - i0} 个交易日），交易按回测口径，出金到手 × {PAYOUT_HAIRCUT}',
             '配置 = 考核张数|funded 张数|考核日盈帽']

    lines.append('\n== 单名额（爆了次日重买）')
    for c, p in plans.items():
        lines.append(f'{c:<9} {fmt(summary([events(p, i0, end)]))}')

    cur = plans['2|2|900']
    modes = {
        'A 同一天买满 5 张 2|2|900': [(cur, 0)] * 5,
        'B 每月补一张 2|2|900': [(cur, MONTH * s) for s in range(5)],
        'C 同一天买 5 张，张数混搭': [(plans[c], 0) for c in ('2|2|900', '2|1|900', '2|1|0', '1|2|0', '1|1|0')],
        'D 每月补一张，张数混搭': [(plans[c], MONTH * s) for s, c in enumerate(('2|2|900', '2|1|900', '2|1|0', '1|2|0', '1|1|0'))],
        'E 同一天买 5 张 2|1|900': [(plans['2|1|900'], 0)] * 5,
        'F 每月补一张 2|1|900': [(plans['2|1|900'], MONTH * s) for s in range(5)],
        'G 同一天买 5 张，只混强配置': [(plans[c], 0) for c in ('2|2|900', '2|2|0', '2|1|900', '2|1|0', '2|2|0')],
        'H 每月补一张，只混强配置': [(plans[c], MONTH * s) for s, c in enumerate(('2|2|900', '2|2|0', '2|1|900', '2|1|0', '2|2|0'))],
    }
    lines.append('\n== 5 个名额的买法对比')
    res = {}
    for name, slots in modes.items():
        slot_ev = [events(p, i0 + o, end) for p, o in slots]
        res[name] = slot_ev
        lines.append(f'{name:<26} {fmt(summary(slot_ev))}')

    span = end - i0
    starts = list(range(0, end - span, ROLL_STEP))
    lines.append(f'\n== 滚动起点：{days[starts[0]][0]} ~ {days[starts[-1]][0]} 每 {ROLL_STEP} 个交易日起跑一次，各跑 {span} 个交易日'
                 f'（{len(starts)} 个起点，近期 = {RECENT} 起）')
    for name, slots in modes.items():
        nets, alls = [], []
        for i in starts:
            s = summary([events(p, i + o, i + span) for p, o in slots])
            nets.append(s['net'])
            alls.append(s['all_bust'])
        a = np.array(nets)
        rec = np.array([x for x, i in zip(nets, starts) if str(days[i][0]) >= RECENT])
        lines.append(f'{name:<26} 净 均值 ${a.mean():>+7,.0f} 中位 ${np.median(a):>+7,.0f} 最差 10% ${np.percentile(a, 10):>+7,.0f} '
                     f'最差 ${a.min():>+7,.0f} 亏钱 {np.mean(a < 0):>4.0%} | 近期均值 ${rec.mean():>+7,.0f} 亏钱 {np.mean(rec < 0):>4.0%} '
                     f'| 全军覆没 平均 {np.mean(alls):.1f} 天')

    for name in modes:
        lines.append(f'\n== 逐月现金流：{name}')
        lines += render_months(res[name])
    lines.append('\n== 各名额时间线：D 每月补一张，张数混搭')
    for (p, _), ev in zip(modes['D 每月补一张，张数混搭'], res['D 每月补一张，张数混搭']):
        lines.append(f'-- {p.key.split("|", 1)[1]}')
        lines += render_slot(ev)
    txt = '\n'.join(lines)
    open(OUT_TXT, 'w').write(txt + '\n')
    print(txt)


if __name__ == '__main__':
    main()
