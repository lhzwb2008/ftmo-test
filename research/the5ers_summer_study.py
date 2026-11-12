#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The5ers Summer Plan（200K 2-Step New/Classic、100K 1-Step）vs 现跑 High Stakes 100K Classic：通过率、耗时、funded 出金。

复用 prop_payout_study.py 的 1x 日内路径（策略 = strategy_params.py 当前版本），数据 = qqq_longport.csv（2024-01 起回测）。
每个交易日都当作一次买号日，逐日跑 考核 → KYC（FUNDED_DELAY 个交易日）→ funded 出金，直到爆仓或数据结束。
日亏按「当日开始余额 × 官方比例」计（隔夜空仓，= 官方 max(EOD 余额, 权益)）；EA 在 95% 日亏处停手，总亏地板不设保护，
盘中权益跌破官方线才算爆仓。考核止盈 = 目标 + 1% 余量（与 simulate 一致），盘中触及即平仓当天收工。

  Summer 200K New 10/5（$249）/ Classic 8/5（$279）：日亏 3% 永久封号，总亏 10% 静态，考核无最少天数 / 无一致性；
      Pro 80% 分润，14 天一出，单次 ≤ $3,000、≥ $250，出金时最好一天 ≤ 未提利润 50%，不扩容；
      退费全是 Hub Credit（过 P1 10%、过 P2 20%、第 3 次出金 70%），只在「死了就重买」时抵下一单
  Summer 1-Step 100K（$249，1-Step 最大只有 100K）：目标 10%，总亏 6%，日亏 3%，考核和 Pro 都有 50% 一致性；
      Pro 75% 分润，单次 ≤ $2,000，Pro 每 +10%（且满足一致性）扩容 100K → 125K → 150K；退费 10% + 90% Hub Credit
  High Stakes 100K Classic（现跑，旧规则）：P1 8% / P2 5%，每步 ≥ 3 个盈利日（当日 ≥ 初始 0.5%），日亏 5%，总亏 10%；
      Pro 80%（175K 起 85%、250K 起 90%、350K 起 100%），单次 ≤ $4,000、≥ $500，每 +10% 且 3 个盈利日扩容；
      过 P1 / P2 各 10% / 20% Hub Credit，第一次出金退 70% 现金（$455 Classic 现价）
  High Stakes 100K Classic（现在新买）：同上但日亏 4%、总亏 8%，单次出金 ≤ 3%（$3,000）

扩容时按单次上限先提一笔，余下利润留在新级别账户里（官方没写清楚扩容时超出上限的利润怎么处理，取保守口径）。

用法：python research/the5ers_summer_study.py run [extra_slip]   # 不给 extra_slip 则自动标定「1x 年化 7 折」滑点
      python research/the5ers_summer_study.py report
      python research/the5ers_summer_study.py funded   # 只看 funded 阶段：一致性 / 出金上限的影响
数据：先用 data_fetch_from_longport.py 拉 2023-08-01 起的 QQQ 1 分钟线到 qqq_longport.csv（2023 下半年只做指标预热）。
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
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

import prop_payout_study as pps

pps.MERGED = 'qqq_longport.csv'
pps.DATA_START = date(2023, 12, 1)
pps.SIM_START = date(2024, 1, 1)

OUT = 'reports/the5ers_summer_study.pkl'
FUNDED_DELAY = 3  # 过考 → KYC → Pro 账户开出（交易日）
TP_BUFFER = 0.01
STOP_BUFFER = 0.95
HAIRCUT = 0.7


@dataclass(frozen=True)
class Acct:
    key: str
    name: str
    size: float
    fee: float
    targets: tuple
    daily: float
    max_loss: float
    good_days: int = 0          # 每步 / 每次扩容前需要的盈利日数（当日 ≥ 初始 0.5%）
    eval_consistency: float = 0.0
    pay_cap: float = 0.0
    pay_min: float = 250.0
    pay_consistency: float = 0.0
    ladder: tuple = ()          # Pro 扩容级别（含起始规模）；空 = 不扩容
    scale_consistency: float = 0.0
    split: tuple = ((0, 0.8),)  # (级别下限, 分润)
    cash_refund: float = 0.0    # 第一次出金退的现金比例
    hub_steps: tuple = (0.1, 0.2)  # 过每一步给的 Hub Credit 比例
    hub_payout: tuple = (0, 0.0)   # (第几次出金, Hub Credit 比例)
    eval_levs: tuple = ()
    fund_levs: tuple = ()


HS_LADDER = (100e3, 125e3, 150e3, 175e3, 200e3, 250e3, 300e3, 350e3, 400e3, 450e3, 500e3)
HS_SPLIT = ((0, 0.8), (175e3, 0.85), (250e3, 0.9), (350e3, 1.0))

ACCTS = (
    Acct('s10', 'Summer 200K New 10/5', 200e3, 249.0, (0.10, 0.05), 0.03, 0.10, pay_cap=3000.0, pay_consistency=0.5,
         hub_payout=(3, 0.7), eval_levs=(1.0, 1.5, 2.0, 2.5, 3.0), fund_levs=(0.75, 1.0, 1.5, 2.0, 2.5)),
    Acct('s8', 'Summer 200K Classic 8/5', 200e3, 279.0, (0.08, 0.05), 0.03, 0.10, pay_cap=3000.0, pay_consistency=0.5,
         hub_payout=(3, 0.7), eval_levs=(1.0, 1.5, 2.0, 2.5, 3.0), fund_levs=(0.75, 1.0, 1.5, 2.0, 2.5)),
    Acct('s1', 'Summer 1-Step 100K', 100e3, 249.0, (0.10,), 0.03, 0.06, eval_consistency=0.5, pay_cap=2000.0,
         pay_consistency=0.5, ladder=(100e3, 125e3, 150e3), scale_consistency=0.5, split=((0, 0.75),),
         hub_steps=(0.1,), hub_payout=(3, 0.9), eval_levs=(0.75, 1.0, 1.5, 2.0, 2.5), fund_levs=(0.5, 0.75, 1.0, 1.5, 2.0)),
    Acct('hs', 'High Stakes 100K Classic（现跑旧规则）', 100e3, 455.0, (0.08, 0.05), 0.05, 0.10, good_days=3,
         pay_cap=4000.0, pay_min=500.0, ladder=HS_LADDER, split=HS_SPLIT, cash_refund=0.7,
         eval_levs=(1.5, 2.0, 3.0, 4.0), fund_levs=(1.0, 1.5, 2.0, 3.0)),
    Acct('hsn', 'High Stakes 100K Classic（现在新买 4%/8%）', 100e3, 455.0, (0.08, 0.05), 0.04, 0.08, good_days=3,
         pay_cap=3000.0, pay_min=500.0, ladder=HS_LADDER, split=HS_SPLIT, cash_refund=0.7,
         eval_levs=(1.0, 1.5, 2.0, 3.0), fund_levs=(0.75, 1.0, 1.5, 2.0)),
)
ACCT = {a.key: a for a in ACCTS}


def day(eng, j, cap, daily_pct, floor_):
    """返回 (pnl, low, high, 是否爆仓)；EA 停手线 = 95% 日亏，总亏地板不设保护。"""
    limit = cap * daily_pct
    eng.daily_stop = limit * STOP_BUFFER
    pnl, low, high, _ = eng.day(j, cap)
    return pnl, low, high, (cap - low >= limit) or (low <= floor_)


def eval_step(eng, a, i, target, initial):
    size = a.size
    floor_ = initial * (1 - a.max_loss)
    cap, good, best = initial, 0, 0.0
    tp = size * (1 + target + TP_BUFFER)
    goal = size * (1 + target)
    for j in range(i, len(pps.DAYS)):
        pnl, low, high, bust = day(eng, j, cap, a.daily, floor_)
        if bust:
            return 'fail', j, cap
        if high >= tp and cap < tp:
            pnl = tp - cap
        cap += pnl
        good += pnl >= size * 0.005
        best = max(best, pnl)
        if cap < goal or good < a.good_days:
            continue
        if a.eval_consistency and best > a.eval_consistency * (cap - initial):
            continue
        return 'pass', j, cap
    return 'open', len(pps.DAYS) - 1, cap


def run_eval(eng, a, i):
    """返回 (状态, 结束日, [各步通过日])。每一步都从初始资金重新开始。"""
    passed = []
    j = i
    for t in a.targets:
        st, e, _ = eval_step(eng, a, j, t, a.size)
        if st != 'pass':
            return st, e, passed
        passed.append(e)
        j = e + 1
        if j >= len(pps.DAYS):
            return 'open', e, passed
    return 'pass', passed[-1], passed


def split_of(a, size):
    s = a.split[0][1]
    for lo, v in a.split:
        if size >= lo:
            s = v
    return s


def run_funded(eng, a, k):
    """从第 k 个交易日开 Pro 账户，跑到爆仓或数据结束。返回 (状态, 结束日, [(日, 到手现金)])。"""
    size = a.size
    lvl = 0
    cap = size
    last_pay = pps.DAYS[k][0]
    pnls, good, pays = [], 0, []
    for j in range(k, len(pps.DAYS)):
        floor_ = size * (1 - a.max_loss)
        pnl, low, high, bust = day(eng, j, cap, a.daily, floor_)
        if bust:
            return 'bust', j, pays
        cap += pnl
        pnls.append(pnl)
        good += pnl >= a.size * 0.005
        profit = cap - size
        d = pps.DAYS[j][0]
        if a.ladder and lvl + 1 < len(a.ladder) and cap >= size * 1.10 and good >= a.good_days and \
                not (a.scale_consistency and max(pnls) > a.scale_consistency * profit):
            amt = min(profit, a.pay_cap) if a.pay_cap else profit
            pays.append((j, amt * split_of(a, size)))
            lvl += 1
            size = a.ladder[lvl]
            cap = size + (profit - amt)
            last_pay, pnls, good = d, [], 0
            continue
        if (d - last_pay).days < 14 or profit < a.pay_min:
            continue
        if a.pay_consistency and max(pnls) > a.pay_consistency * profit:
            continue
        amt = min(profit, a.pay_cap) if a.pay_cap else profit
        pays.append((j, amt * split_of(a, size)))
        cap -= amt
        last_pay, pnls = d, []
    return 'alive', len(pps.DAYS) - 1, pays


def eval_job(spec):
    key, elev, slip = spec
    a = ACCT[key]
    t0 = time.time()
    eng = pps.Engine(elev, a.size, slip, daily_stop=1.0)
    return spec, [run_eval(eng, a, i) for i in range(len(pps.DAYS))], time.time() - t0


def funded_job(spec):
    key, flev, slip, ks = spec
    a = ACCT[key]
    t0 = time.time()
    eng = pps.Engine(flev, a.size, slip, daily_stop=1.0)
    return (key, flev, slip), {k: run_funded(eng, a, k) for k in ks}, time.time() - t0


def combine(a, evals, funded):
    n = len(pps.DAYS)
    rows = []
    for i, (st, e, passed) in enumerate(evals):
        fund = None
        if st == 'pass':
            k = e + 1 + FUNDED_DELAY
            if k < n:
                fund = (k, *funded[k])
            else:
                st = 'open'
        rows.append((i, st, e, tuple(passed), fund))
    return rows


def calibrate():
    base = pps.cagr_1x(0.0)
    cands = (0.01, 0.02, 0.03, 0.04, 0.05)
    pps.build_paths(cands)
    vals = [pps.cagr_1x(s) for s in cands]
    goal = base * HAIRCUT
    slip = float(np.interp(-goal, [-v for v in vals], cands))
    print('1x 年化：回测 {:+.2%}；'.format(base) + '，'.join(f'滑点+{s:.2f} → {v:+.2%}' for s, v in zip(cands, vals)) +
          f'；7 折目标 {goal:+.2%} → 额外滑点 ${slip:.4f}/股', flush=True)
    for s in cands:
        pps.PATHS.pop(s, None)
    return round(slip, 4)


def run(slip_h=None):
    pps.load()
    print(f'交易日 {len(pps.DAYS)}：{pps.DAYS[0][0]} ~ {pps.DAYS[-1][0]}', flush=True)
    t0 = time.time()
    pps.build_paths((0.0,))
    if slip_h is None:
        slip_h = calibrate()
    pps.build_paths((slip_h,))
    print(f'路径预计算 {time.time() - t0:.0f}s；打折 1x 年化 {pps.cagr_1x(slip_h):+.2%}', flush=True)
    slips = (0.0, slip_h)
    n = len(pps.DAYS)
    evals, funded = {}, {}
    t0 = time.time()
    with mp.get_context('fork').Pool(7) as pool:
        specs = [(a.key, el, s) for s in slips for a in ACCTS for el in a.eval_levs]
        for spec, out, dt in pool.imap_unordered(eval_job, specs):
            evals[spec] = out
            print(f'考核 {spec} {dt:.0f}s（累计 {time.time() - t0:.0f}s，{len(evals)}/{len(specs)}）', flush=True)
        specs = []
        for s in slips:
            for a in ACCTS:
                ks = sorted({e + 1 + FUNDED_DELAY for el in a.eval_levs for st, e, _ in evals[(a.key, el, s)]
                             if st == 'pass' and e + 1 + FUNDED_DELAY < n})
                specs += [(a.key, fl, s, ks) for fl in a.fund_levs]
        for spec, out, dt in pool.imap_unordered(funded_job, specs):
            funded[spec] = out
            print(f'funded {spec} {dt:.0f}s（累计 {time.time() - t0:.0f}s，{len(funded)}/{len(specs)}）', flush=True)
    results = {}
    for s in slips:
        for a in ACCTS:
            for fl in a.fund_levs:
                results[(a.key, fl, s)] = {el: combine(a, evals[(a.key, el, s)], funded[(a.key, fl, s)])
                                           for el in a.eval_levs}
    _os.makedirs(_os.path.dirname(OUT), exist_ok=True)
    pickle.dump({'results': results, 'dates': [d for d, _, _ in pps.DAYS], 'slip': slip_h}, open(OUT, 'wb'))


# ---------------------------------------------------------------- 报告
def single(a, rows, dates, horizon_days):
    """单个账户（不重买）在买号后 horizon_days 自然日内的结果，只统计观察期完整的买号日。"""
    last = dates[-1]
    stats = dict(n=0, p1=0, funded=0, t_fund=[], paid=0, t_pay=[], cash=[], bust=0)
    for i, st, e, passed, fund in rows:
        d0 = dates[i]
        end = d0 + timedelta(days=horizon_days)
        if end > last:
            continue
        stats['n'] += 1
        stats['p1'] += bool(passed) and dates[passed[0]] <= end
        cash = 0.0
        if st == 'pass' and fund and dates[fund[0]] <= end:
            k, fst, fe, pays = fund
            stats['funded'] += 1
            stats['t_fund'].append((dates[k] - d0).days)
            got = [(j, c) for j, c in pays if dates[j] <= end]
            if got:
                stats['paid'] += 1
                stats['t_pay'].append((dates[got[0][0]] - d0).days)
                cash = sum(c for _, c in got) + a.fee * a.cash_refund
            stats['bust'] += fst == 'bust' and dates[fe] <= end
        stats['cash'].append(cash)
    return stats


def chain(a, rows, dates, horizon_days):
    """一个名额「死了就重买」，horizon_days 内的现金净额（到手 − 现金报名费），Hub Credit 抵下一单。"""
    last = dates[-1]
    n = len(dates)
    nets, buys, first_fund = [], [], []
    for i0 in range(n):
        end = dates[i0] + timedelta(days=horizon_days)
        if end > last:
            break
        i, hub, net, nb, ff = i0, 0.0, 0.0, 0, None
        while i < n and dates[i] <= end:
            paid = max(0.0, a.fee - hub)
            hub = max(0.0, hub - a.fee)
            net -= paid
            nb += 1
            _, st, e, passed, fund = rows[i]
            for p, t in zip(passed, a.hub_steps):
                if dates[p] <= end:
                    hub += a.fee * t
            death = e if st == 'fail' else None
            if st == 'pass' and fund:
                k, fst, fe, pays = fund
                if ff is None and dates[k] <= end:
                    ff = (dates[k] - dates[i0]).days
                for m, (j, c) in enumerate(pays, 1):
                    if dates[j] > end:
                        break
                    net += c + (paid * a.cash_refund if m == 1 else 0.0)
                    if m == a.hub_payout[0]:
                        hub += a.fee * a.hub_payout[1]
                death = fe if fst == 'bust' else None
            if death is None:
                break
            i = death + 1
        nets.append(net)
        buys.append(nb)
        first_fund.append(ff)
    return nets, buys, first_fund


def report():
    d = pickle.load(open(OUT, 'rb'))
    R, dates, slip_h = d['results'], d['dates'], d['slip']
    print(f'样本 {dates[0]} ~ {dates[-1]}（{len(dates)} 个交易日），实盘 7 折 = 每股额外滑点 ${slip_h}\n')
    for slip, tag in ((0.0, '回测'), (slip_h, '实盘7折')):
        print(f'================ {tag} ================')
        for a in ACCTS:
            print(f'\n--- {a.name}（报名费 ${a.fee:.0f}）---')
            best = []
            for fl in a.fund_levs:
                out = R[(a.key, fl, slip)]
                for el in a.eval_levs:
                    rows = out[el]
                    s12 = single(a, rows, dates, 365)
                    nets, buys, ff = chain(a, rows, dates, 365)
                    ok = [x for x in ff if x is not None]
                    best.append((np.mean(nets), el, fl, s12, nets, buys, ff, ok))
            best.sort(key=lambda x: -x[0])
            for mean_net, el, fl, s, nets, buys, ff, ok in best[:4]:
                n = s['n']
                med = lambda xs: f'{np.median(xs):.0f}' if xs else '-'
                print(f'  考核 {el:g}x / funded {fl:g}x：单号 12 个月内 过P1 {s["p1"] / n:.0%}  拿到funded {s["funded"] / n:.0%}'
                      f'（中位 {med(s["t_fund"])} 天）  有出金 {s["paid"] / n:.0%}（首次中位 {med(s["t_pay"])} 天）'
                      f'  funded 后爆仓 {s["bust"] / max(s["funded"], 1):.0%}  单号到手均值 ${np.mean(s["cash"]):,.0f}')
                print(f'      死了就重买 12 个月：净现金 均值 ${mean_net:,.0f} / 中位 ${np.median(nets):,.0f} / '
                      f'P10 ${np.percentile(nets, 10):,.0f}，平均买 {np.mean(buys):.1f} 个，'
                      f'12 个月内拿到 funded {len(ok) / len(ff):.0%}（中位 {med(ok)} 天）')
        print()


def funded_report(days=250):
    """只看 funded 阶段：Summer 200K 官方规则 vs 去掉一致性 / 出金上限 $6,000（3% 新规），对照 High Stakes。"""
    from dataclasses import replace
    slip_h = pickle.load(open(OUT, 'rb'))['slip']
    pps.load()
    pps.build_paths((0.0, slip_h))
    dates = [d for d, _, _ in pps.DAYS]
    ks = [k for k in range(len(dates)) if (dates[-1] - dates[k]).days >= days][::3]
    cases = [('s8', 2.0, None), ('s8', 2.0, 'nocons'), ('s8', 2.0, 'cap6k'), ('s8', 2.5, None),
             ('hs', 2.0, None), ('hs', 3.0, None)]
    for slip, tag in ((0.0, '回测'), (slip_h, '实盘7折')):
        print(f'==== {tag}：funded 开户后 {days} 天（{len(ks)} 个开户日）====')
        for key, fl, var in cases:
            a = ACCT[key]
            name = a.name
            if var == 'nocons':
                a, name = replace(a, pay_consistency=0.0), name + '（假设无一致性）'
            elif var == 'cap6k':
                a, name = replace(a, pay_cap=6000.0), name + '（假设上限 $6,000）'
            eng = pps.Engine(fl, a.size, slip, daily_stop=1.0)
            first, cash, bust = [], [], 0
            for k in ks:
                st, e, pays = run_funded(eng, a, k)
                first.append((dates[pays[0][0]] - dates[k]).days if pays else 1 << 30)
                cash.append(sum(c for j, c in pays if (dates[j] - dates[k]).days <= days))
                bust += st == 'bust' and (dates[e] - dates[k]).days <= days
            first = np.array(first)
            print(f'  {name} {fl:g}x：到手 均值 ${np.mean(cash):,.0f} / 中位 ${np.median(cash):,.0f}；'
                  f'首次出金中位 {np.median(first):.0f} 天，60 天内有出金 {np.mean(first <= 60):.0%}；爆仓 {bust / len(ks):.0%}')


if __name__ == '__main__':
    if sys.argv[1] == 'run':
        run(float(sys.argv[2]) if len(sys.argv) > 2 else None)
    elif sys.argv[1] == 'funded':
        funded_report()
    else:
        report()
