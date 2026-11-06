#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Funded 账户「拿回考核费」测算：按各家出金条件比较杠杆，以及「一直交易」vs「条件满足即停手等出金日」。

复用 prop_payout_study.py 的 1x 日内路径（策略 = strategy_params.py 当前版本），2020 年起每 STEP 个交易日开一次户，
最多观察 HORIZON 个交易日。EA 日亏按官方 × 0.95 触发（DailyLossBufferPercent=5），盘中权益跌破官方线才算爆仓。

  FTMO 200K 2-Step：日亏 5% / 总亏 10% 静态；无盈利日要求；首次出金（第一笔交易后满 14 天、利润 ≥ $20）即退费
  TTP 100K 2-Phase（2026-04-14 购买）：日亏 5% / 总亏 10% 静态；每次出金前 3 个盈利日（当日 > 初始 0.5%）、
      收到账户满 14 天、奖励 ≥ $100；首次出金即退费
  Goat 100K 2-Step Standard：日亏 5% / 总亏 10% 静态；每次出金前 3 或 4 个盈利日（≥ 0.5%）、14 天周期；
      Goat Guard 浮亏 2% 第一次分成降 50%、第二次爆仓；日盈 $3,000 封顶；前两次出金上限 6%，必须全提；第 4 次出金退费
  Aqua 100K 2-Step Standard：日亏 5% / 总亏 8% 静态；EA 单笔硬止损 0.9%（Wave Stop 1%）；首次出金前 3 个盈利日（≥ 0.5%）、
      14 天周期，前两次出金上限 $10,000；第 4 次出金退费

用法：python prop_refund_study.py [extra_slip]   （默认 0.035，对应 prop_payout_study 的「实盘7折」）
"""

from __future__ import annotations

import multiprocessing as mp
import pickle
import sys
import time
from dataclasses import dataclass

import numpy as np

import prop_payout_study as pps

STEP = 5
HORIZON = 252
LEVERAGES = (0.5, 0.75, 1.0, 1.5, 2.0)
POLICIES = ('keep', 'stop')
OUT = 'reports/prop_refund_study.pkl'


@dataclass(frozen=True)
class Rule:
    key: str
    name: str
    size: float
    daily_limit: float
    max_loss: float
    good_day: float = 0.0
    good_days: int = 0
    good_days_every_cycle: bool = True
    cycle_days: int = 14
    min_profit: float = 100.0
    payouts_for_refund: int = 1
    split: float = 0.8
    hard_sl: float = 0.0
    guard: float = 0.0
    day_cap: float = 0.0
    early_cap: float = 0.0
    full_withdraw: bool = True


RULES = (
    Rule('ftmo', 'FTMO 200K', 200_000.0, 0.05, 0.10, min_profit=20.0, split=0.8),
    Rule('ttp', 'TTP 100K', 100_000.0, 0.05, 0.10, good_day=0.005, good_days=3, min_profit=125.0, split=0.8 * 0.99),
    Rule('goat3', 'Goat 100K（3 个盈利日）', 100_000.0, 0.05, 0.10, good_day=0.005, good_days=3, payouts_for_refund=4,
         split=0.8 * 0.97, guard=0.02, day_cap=3_000.0, early_cap=6_000.0),
    Rule('goat4', 'Goat 100K（4 个盈利日）', 100_000.0, 0.05, 0.10, good_day=0.005, good_days=4, payouts_for_refund=4,
         split=0.8 * 0.97, guard=0.02, day_cap=3_000.0, early_cap=6_000.0),
    Rule('aqua', 'Aqua 100K', 100_000.0, 0.05, 0.08, good_day=0.005, good_days=3, good_days_every_cycle=False,
         payouts_for_refund=4, split=0.9 * 0.97, hard_sl=0.009, early_cap=10_000.0, full_withdraw=False),
)


def day_with_float(i, cap, lev, stop_usd, guard_usd, slip):
    """在 1x 路径上按名义本金缩放；额外返回本日浮亏触及 Goat Guard 的笔数。"""
    n = cap * lev * pps.QUIET.get(pps.DAYS[i][0], 1.0)
    if n <= 0:
        return 0.0, cap, cap, 0
    s = stop_usd / n
    g = guard_usd / n if guard_usd else 0.0
    sp = (0.01 + slip) / float(pps.DAYS[i][1]['day_open'].iloc[0])
    r = lo = hi = 0.0
    hits = 0
    for neg_lo, run_hi, f in pps.PATHS[slip][i]:
        ds = pps._first(neg_lo, s + r)
        if ds >= len(neg_lo):
            if g and neg_lo[-1] >= g:
                hits += 1
            lo, hi = min(lo, r - neg_lo[-1]), max(hi, r + run_hi[-1])
            r += f
        else:
            if g and s + r >= g:
                hits += 1
            r = lo = -s - sp
            break
    return r * n, cap + min(lo, r) * n, cap + max(hi, r) * n, hits


def run_account(i, lev, rule, slip, policy, eng):
    size = rule.size
    stop_usd = size * rule.daily_limit * 0.95
    cap, cash, n_pay, guard_hits = size, 0.0, 0, 0
    cycle_start, good = pps.DAYS[i][0], 0
    need = rule.good_days
    end = min(len(pps.DAYS), i + HORIZON)
    traded_days = 0
    for j in range(i, end):
        d = pps.DAYS[j][0]
        profit = cap - size
        ready = good >= need and profit >= rule.min_profit
        if ready and (d - cycle_start).days >= rule.cycle_days:
            amt = profit
            if not rule.full_withdraw:
                amt = min(profit, rule.early_cap) if n_pay < 2 and rule.early_cap else profit
            elif rule.early_cap and n_pay < 2:
                amt = min(profit, rule.early_cap)
            split = 0.5 * rule.split / 0.8 if guard_hits else rule.split
            cash += amt * split
            n_pay += 1
            cap = size if rule.full_withdraw else cap - amt
            if n_pay >= rule.payouts_for_refund:
                return 'refund', j - i, (d - pps.DAYS[i][0]).days, cash, traded_days, guard_hits
            cycle_start, good = d, 0
            if not rule.good_days_every_cycle:
                need = 0
            continue
        if policy == 'stop' and ready:
            continue
        if rule.hard_sl:
            pnl, low, _, _ = eng.day(j, cap)
            hits = 0
        else:
            pnl, low, _, hits = day_with_float(j, cap, lev, stop_usd, size * rule.guard, slip)
        traded_days += 1
        if cap - low >= size * rule.daily_limit or low <= size * (1 - rule.max_loss):
            return 'bust', j - i, (d - pps.DAYS[i][0]).days, cash, traded_days, guard_hits
        guard_hits += hits
        if rule.guard and guard_hits >= 2:
            return 'bust', j - i, (d - pps.DAYS[i][0]).days, cash, traded_days, guard_hits
        if rule.day_cap and pnl > rule.day_cap:
            pnl = rule.day_cap
        cap += pnl
        if rule.good_days and pnl > size * rule.good_day:
            good += 1
    return 'timeout', end - 1 - i, (pps.DAYS[end - 1][0] - pps.DAYS[i][0]).days, cash, traded_days, guard_hits


def job(spec):
    key, lev, slip, policy = spec
    rule = next(r for r in RULES if r.key == key)
    eng = None
    if rule.hard_sl:
        eng = pps.Engine(lev, rule.size, slip, daily_stop=rule.size * rule.daily_limit * 0.95, hard_sl=rule.hard_sl)
    t0 = time.time()
    last = len(pps.DAYS) - HORIZON
    out = [run_account(i, lev, rule, slip, policy, eng) for i in range(0, last, STEP)]
    return spec, out, time.time() - t0


def summarize(rows):
    st = np.array([r[0] for r in rows])
    ok = [r for r in rows if r[0] == 'refund']
    cal = np.array([r[2] for r in ok]) if ok else np.array([np.nan])
    return dict(n=len(rows), refund=np.mean(st == 'refund'), bust=np.mean(st == 'bust'),
                timeout=np.mean(st == 'timeout'), med=np.nanmedian(cal), p90=np.nanpercentile(cal, 90),
                guard1=np.mean([r[5] >= 1 for r in rows]))


def report(results, slips):
    tag = {0.0: '回测', slips[-1]: '实盘打折'}
    pol = {'keep': '一直交易', 'stop': '满足即停'}
    for rule in RULES:
        print(f'\n== {rule.name}（退费需第 {rule.payouts_for_refund} 次出金）')
        for slip in slips:
            for policy in POLICIES:
                for lev in LEVERAGES:
                    s = summarize(results[(rule.key, lev, slip, policy)])
                    extra = f'，Goat Guard 触发过 {s["guard1"]:.0%}' if rule.guard else ''
                    print(f'  {tag[slip]} {pol[policy]} {lev:g}x: 一年内退费 {s["refund"]:.0%}，爆仓 {s["bust"]:.0%}，'
                          f'未达成 {s["timeout"]:.0%} | 退费用时中位 {s["med"]:.0f} 天 / P90 {s["p90"]:.0f} 天{extra}')


def main():
    slip = float(sys.argv[1]) if len(sys.argv) > 1 else 0.035
    slips = (0.0, slip)
    t0 = time.time()
    pps.load()
    pps.build_paths(slips)
    print(f'路径预计算 {time.time() - t0:.0f}s，样本 {pps.DAYS[0][0]} ~ {pps.DAYS[-1][0]}', flush=True)
    specs = [(r.key, lev, s, p) for r in RULES for lev in LEVERAGES for s in slips for p in POLICIES]
    results = {}
    with mp.get_context('fork').Pool(6) as pool:
        for spec, out, dt in pool.imap_unordered(job, specs):
            results[spec] = out
    print(f'全部 {len(specs)} 组完成 {time.time() - t0:.0f}s', flush=True)
    pickle.dump({'results': results, 'slips': slips, 'dates': [d for d, _, _ in pps.DAYS]}, open(OUT, 'wb'))
    report(results, slips)


if __name__ == '__main__':
    main()
