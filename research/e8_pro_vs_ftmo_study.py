#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E8 Markets E8 Pro 100K（6% 档，$398）vs FTMO 2-Step 100K：通过率、耗时、funded 出金、「死了就重买」12 个月净现金。

复用 the5ers_summer_study.py 的 1x 日内路径（qqq_longport.csv，2024-01 起）和「1x 年化 7 折」额外滑点。
每个交易日都当作一次买号日：考核 → KYC（FUNDED_DELAY 个交易日）→ funded 出金，直到爆仓或数据结束。
EA 日亏停手线 = min(官方日亏额度, 当日余额 − 总亏地板) × 95%（隔夜空仓，日初余额 = 官方日初基准）。

  E8 Pro 100K 6%（E8 帮助中心 E8 Pro / Daily profit cap / Payout request from E8 Pro）：
    一步考核 目标 6%（平仓利润）；日亏 2.5% 初始资金，从当日 00:00 余额往下算，硬违规；总亏 6% 静态，
    第一次出金后地板上移到初始资金且不再动；考核和 funded 每天只有 2% 初始资金算数，超出部分次日被扣掉
    （EA 盘中到 +2% 就平仓收工，见 E8_EA_DAY_CAP；对照组不收工、超出部分作废）；
    funded 每天可申请，需自上次出金以来再赚 ≥ 1%，每次提「总利润 × 50%」× 分润 80%，另一半留作缓冲；
    无一致性、无出金上限、不退报名费；挂掉 7 天内重买 9 折（标价 $468 → $421）
  FTMO 2-Step 100K：P1 10% / P2 5%（目标 + 1% 盘中止盈，与 simulate 一致），每步最少 4 个交易日；
    日亏 5%，总亏 10% 静态；funded 第一笔交易后满 14 天可提，之后每 14 天，提走全部利润 × 80%；
    第一次出金全额退报名费（€439 优惠价 ≈ $510）

用法：python3 research/e8_pro_vs_ftmo_study.py run      # 需先跑过 the5ers_summer_study.py run（读它标定的滑点）
      python3 research/e8_pro_vs_ftmo_study.py report
"""

from __future__ import annotations

import os as _os
import sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, ROOT)
_os.chdir(ROOT)

import multiprocessing as mp
import pickle
import sys
import time
from dataclasses import dataclass
from datetime import timedelta

import numpy as np

import the5ers_summer_study as ts

pps = ts.pps
OUT = 'reports/e8_pro_vs_ftmo_study.pkl'
FUNDED_DELAY = 3
STOP_BUFFER = 0.95
E8_EA_DAY_CAP = True
HORIZON = 365


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    size: float
    fee: float
    rebuy_fee: float
    targets: tuple
    daily: float
    max_loss: float
    min_days: int
    tp_buffer: float
    profit_cap: float      # 每天只算这么多利润（初始资金比例），0 = 不限
    ea_day_cap: bool       # EA 盘中到日盈帽就平仓收工
    split: float
    refund: float          # 第一次出金退报名费比例
    eval_levs: tuple
    fund_levs: tuple
    pay_thr: tuple = (0.0,)  # E8：总利润达到多少才申请出金（≥ 1%）


PLANS = (
    Plan('e8', 'E8 Pro 100K 6%（EA 到 +2% 收工）', 100e3, 398.0, 421.0, (0.06,), 0.025, 0.06, 0, 0.001, 0.02, True,
         0.8, 0.0, (1.0, 1.5, 2.0, 2.5, 3.0), (0.75, 1.0, 1.5, 2.0, 2.5), (0.01, 0.03, 0.06, 0.08, 0.10, 0.15, 0.20)),
    Plan('e8n', 'E8 Pro 100K 6%（不收工，超 2% 作废）', 100e3, 398.0, 421.0, (0.06,), 0.025, 0.06, 0, 0.001, 0.02, False,
         0.8, 0.0, (1.0, 1.5, 2.0, 2.5, 3.0), (0.75, 1.0, 1.5, 2.0, 2.5), (0.01, 0.02, 0.03, 0.04, 0.06)),
    Plan('ftmo', 'FTMO 2-Step 100K', 100e3, 510.0, 510.0, (0.10, 0.05), 0.05, 0.10, 4, 0.01, 0.0, False,
         0.8, 1.0, (1.5, 2.0, 2.5, 3.0, 4.0, 5.0), (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0)),
)
PLAN = {p.key: p for p in PLANS}


def make_engine(p, lev, slip):
    cap = p.size * p.profit_cap if p.ea_day_cap else 0.0
    return pps.Engine(lev, p.size, slip, daily_stop=1.0, day_cap=cap)


def day(eng, p, j, cap, floor_):
    limit = p.size * p.daily
    eng.daily_stop = max(min(limit, cap - floor_), 1.0) * STOP_BUFFER
    pnl, low, high, _ = eng.day(j, cap)
    bust = (cap - low >= limit) or (low <= floor_)
    if p.profit_cap:
        c = p.size * p.profit_cap
        pnl, high = min(pnl, c), min(high, cap + c)
    return pnl, low, high, bust


def eval_step(eng, p, i, target):
    n = len(pps.DAYS)
    floor_ = p.size * (1 - p.max_loss)
    goal = p.size * (1 + target)
    tp = goal + p.size * p.tp_buffer
    cap = p.size
    for j in range(i, n):
        pnl, low, high, bust = day(eng, p, j, cap, floor_)
        if bust:
            return 'fail', j
        if high >= tp:
            pnl = tp - cap
        cap += pnl
        if cap >= goal:
            k = max(j, i + p.min_days - 1)
            return ('pass', k) if k < n else ('open', n - 1)
    return 'open', n - 1


def run_eval(eng, p, i):
    j, e = i, i
    for t in p.targets:
        st, e = eval_step(eng, p, j, t)
        if st != 'pass':
            return st, e
        j = e + 1
        if j >= len(pps.DAYS):
            return 'open', e
    return 'pass', e


def e8_funded(eng, p, k, thr):
    size = p.size
    cap, base, floor_ = size, size, size * (1 - p.max_loss)
    pays = []
    for j in range(k, len(pps.DAYS)):
        pnl, low, high, bust = day(eng, p, j, cap, floor_)
        if bust:
            return 'bust', j, pays
        cap += pnl
        profit = cap - size
        if profit >= max(thr, 0.01) * size and cap - base >= 0.01 * size:
            amt = 0.5 * profit
            pays.append((j, amt * p.split))
            cap -= amt
            base, floor_ = cap, size
    return 'alive', len(pps.DAYS) - 1, pays


def ftmo_funded(eng, p, k):
    size = p.size
    cap, floor_ = size, size * (1 - p.max_loss)
    last = pps.DAYS[k][0]
    pays = []
    for j in range(k, len(pps.DAYS)):
        pnl, low, high, bust = day(eng, p, j, cap, floor_)
        if bust:
            return 'bust', j, pays
        cap += pnl
        d = pps.DAYS[j][0]
        if (d - last).days >= 14 and cap - size >= 20:
            pays.append((j, (cap - size) * p.split))
            cap, last = size, d
    return 'alive', len(pps.DAYS) - 1, pays


def eval_job(spec):
    key, lev, slip = spec
    p = PLAN[key]
    eng = make_engine(p, lev, slip)
    return spec, [run_eval(eng, p, i) for i in range(len(pps.DAYS))]


def funded_job(spec):
    key, lev, slip, thr, ks = spec
    p = PLAN[key]
    eng = make_engine(p, lev, slip)
    f = (lambda k: e8_funded(eng, p, k, thr)) if key.startswith('e8') else (lambda k: ftmo_funded(eng, p, k))
    return (key, lev, slip, thr), {k: f(k) for k in ks}


def run():
    slip_h = pickle.load(open(ts.OUT, 'rb'))['slip']
    pps.load()
    print(f'交易日 {len(pps.DAYS)}：{pps.DAYS[0][0]} ~ {pps.DAYS[-1][0]}；实盘 7 折额外滑点 ${slip_h}/股', flush=True)
    t0 = time.time()
    slips = (0.0, slip_h)
    pps.build_paths(slips)
    print(f'路径预计算 {time.time() - t0:.0f}s', flush=True)
    n = len(pps.DAYS)
    evals, funded = {}, {}
    with mp.get_context('fork').Pool(7) as pool:
        specs = [(p.key, el, s) for s in slips for p in PLANS for el in p.eval_levs]
        for spec, out in pool.imap_unordered(eval_job, specs):
            evals[spec] = out
        print(f'考核完成 {time.time() - t0:.0f}s', flush=True)
        specs = []
        for s in slips:
            for p in PLANS:
                ks = sorted({e + 1 + FUNDED_DELAY for el in p.eval_levs for st, e in evals[(p.key, el, s)]
                             if st == 'pass' and e + 1 + FUNDED_DELAY < n})
                specs += [(p.key, fl, s, thr, ks) for fl in p.fund_levs for thr in p.pay_thr]
        for spec, out in pool.imap_unordered(funded_job, specs):
            funded[spec] = out
        print(f'funded 完成 {time.time() - t0:.0f}s', flush=True)
    pickle.dump({'evals': evals, 'funded': funded, 'dates': [d for d, _, _ in pps.DAYS], 'slip': slip_h},
                open(OUT, 'wb'))


# ---------------------------------------------------------------- 报告
def single(p, ev, fd, dates):
    """单个账户（不重买）买号后 HORIZON 自然日内。"""
    n, last = len(dates), dates[-1]
    s = dict(n=0, passed=0, t_pass=[], paid=0, t_pay=[], cash=[], bust=0)
    for i, (st, e) in enumerate(ev):
        end = dates[i] + timedelta(days=HORIZON)
        if end > last:
            break
        s['n'] += 1
        cash = 0.0
        k = e + 1 + FUNDED_DELAY
        if st == 'pass' and k < n and dates[k] <= end:
            s['passed'] += 1
            s['t_pass'].append((dates[e] - dates[i]).days)
            fst, fe, pays = fd[k]
            got = [c for j, c in pays if dates[j] <= end]
            if got:
                s['paid'] += 1
                s['t_pay'].append((dates[pays[0][0]] - dates[i]).days)
                cash = sum(got) + p.fee * p.refund
            s['bust'] += fst == 'bust' and dates[fe] <= end
        s['cash'].append(cash - p.fee)
    return s


def chain(p, ev, fd, dates):
    """一个名额「死了就重买」HORIZON 内的净现金（到手 − 报名费）。"""
    n, last = len(dates), dates[-1]
    nets, buys = [], []
    for i0 in range(n):
        end = dates[i0] + timedelta(days=HORIZON)
        if end > last:
            break
        i, net, nb = i0, 0.0, 0
        while i < n and dates[i] <= end:
            fee = p.fee if nb == 0 else p.rebuy_fee
            net -= fee
            nb += 1
            st, e = ev[i]
            death = e if st == 'fail' else None
            k = e + 1 + FUNDED_DELAY
            if st == 'pass' and k < n:
                fst, fe, pays = fd[k]
                for m, (j, c) in enumerate(pays, 1):
                    if dates[j] > end:
                        break
                    net += c + (fee * p.refund if m == 1 else 0.0)
                death = fe if fst == 'bust' else None
            if death is None:
                break
            i = death + 1
        nets.append(net)
        buys.append(nb)
    return np.array(nets), np.array(buys)


def report():
    d = pickle.load(open(OUT, 'rb'))
    E, F, dates, slip_h = d['evals'], d['funded'], d['dates'], d['slip']
    print(f'样本 {dates[0]} ~ {dates[-1]}（{len(dates)} 个交易日），观察期 {HORIZON} 天，实盘 7 折 = 每股额外滑点 ${slip_h}\n')
    med = lambda xs: f'{np.median(xs):.0f}' if len(xs) else '-'
    for slip, tag in ((0.0, '回测'), (slip_h, '实盘7折')):
        print(f'================ {tag} ================')
        for p in PLANS:
            print(f'\n--- {p.name}（报名费 ${p.fee:.0f}）---')
            rows = []
            for el in p.eval_levs:
                ev = E[(p.key, el, slip)]
                for fl in p.fund_levs:
                    for thr in p.pay_thr:
                        fd = F[(p.key, fl, slip, thr)]
                        nets, buys = chain(p, ev, fd, dates)
                        rows.append((nets.mean(), el, fl, thr, single(p, ev, fd, dates), nets, buys))
            rows.sort(key=lambda x: -x[0])
            for mean_net, el, fl, thr, s, nets, buys in rows[:4]:
                n = s['n']
                th = f' / 利润 ≥{thr:.0%} 才提' if p.key.startswith('e8') else ''
                print(f'  考核 {el:g}x / funded {fl:g}x{th}：单号 12 个月 通过 {s["passed"] / n:.0%}（中位 {med(s["t_pass"])} 天）'
                      f'  有出金 {s["paid"] / n:.0%}（首次中位 {med(s["t_pay"])} 天）'
                      f'  funded 后爆仓 {s["bust"] / max(s["passed"], 1):.0%}  单号净额均值 ${np.mean(s["cash"]):,.0f}')
                print(f'      死了就重买 12 个月：净现金 均值 ${mean_net:,.0f} / 中位 ${np.median(nets):,.0f} / '
                      f'P10 ${np.percentile(nets, 10):,.0f} / P90 ${np.percentile(nets, 90):,.0f}，'
                      f'亏钱 {np.mean(nets < 0):.0%}，平均买 {buys.mean():.1f} 个')
        print()


PICKS = [('ftmo', 2.0, 1.5, 0.0)] + [('e8', 1.0, fl, thr) for fl in (0.75, 1.0, 1.5)
                                      for thr in (0.01, 0.03, 0.06, 0.10)] + [('e8', 1.5, 1.0, 0.06), ('e8', 1.5, 1.0, 0.10)]


def picks():
    """指定配置（FTMO 现跑 2x / 1.5x，E8 低杠杆）逐个输出。"""
    d = pickle.load(open(OUT, 'rb'))
    E, F, dates, slip_h = d['evals'], d['funded'], d['dates'], d['slip']
    med = lambda xs: f'{np.median(xs):.0f}' if len(xs) else '-'
    for slip, tag in ((0.0, '回测'), (slip_h, '实盘7折')):
        print(f'================ {tag} ================')
        for key, el, fl, thr in PICKS:
            p = PLAN[key]
            ev, fd = E[(key, el, slip)], F[(key, fl, slip, thr)]
            s = single(p, ev, fd, dates)
            nets, buys = chain(p, ev, fd, dates)
            n = s['n']
            th = f' / ≥{thr:.0%} 才提' if key.startswith('e8') else ''
            print(f'  {p.name[:16]} 考核 {el:g}x / funded {fl:g}x{th}：通过 {s["passed"] / n:.0%}（中位 {med(s["t_pass"])} 天）'
                  f' 有出金 {s["paid"] / n:.0%}（首次中位 {med(s["t_pay"])} 天） funded 爆仓 {s["bust"] / max(s["passed"], 1):.0%}'
                  f' 单号净额 ${np.mean(s["cash"]):,.0f} | 重买：均值 ${nets.mean():,.0f} 中位 ${np.median(nets):,.0f}'
                  f' P10 ${np.percentile(nets, 10):,.0f} 亏钱 {np.mean(nets < 0):.0%} 买 {buys.mean():.1f} 个')


def eval_table():
    """各杠杆下单纯考核的通过率 / 中位天数（不含 funded）。"""
    d = pickle.load(open(OUT, 'rb'))
    E, dates, slip_h = d['evals'], d['dates'], d['slip']
    for slip, tag in ((0.0, '回测'), (slip_h, '实盘7折')):
        print(f'==== {tag}：考核结果（已结束样本）====')
        for p in PLANS:
            for el in p.eval_levs:
                ev = E[(p.key, el, slip)]
                done = [(i, st, e) for i, (st, e) in enumerate(ev) if st != 'open']
                win = [(dates[e] - dates[i]).days for i, st, e in done if st == 'pass']
                print(f'  {p.name} {el:g}x：通过 {len(win) / len(done):.0%}（{len(done)} 个样本），通过中位 '
                      f'{np.median(win) if win else 0:.0f} 天')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'report'
    {'run': run, 'report': report, 'eval': eval_table, 'picks': picks}[cmd]()
