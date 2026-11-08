#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期货 prop 账户「出金 / 购买价」与「首次出金用时」测算（策略 = strategy_params.py 当前版本）。

路径来自 futures_prop_paths.py（逐笔逐分钟，单位 = 1x 名义比例）。每张 MNQ 名义固定按当前价格
（QQQ × NQ_QQQ_RATIO × $2）缩放，衡量的是「现在开户」时固定美元回撤相对仓位的大小，而不是 2020 年的小名义。

滚动起点：2020 年起每 STEP 个交易日买一次考核，从购买日起观察 HORIZON 个交易日：
  考核期：盘中权益触及目标（并满足一致性窗口）即平仓通过，最少交易日不足时按补小单处理；
  funded：每个收盘后检查出金资格，满足即按规则最大额申请；爆仓或到期结束。
费用：一次性考核费，或按自然月续费直到通过 / 失败；通过后付激活费。
指标：一年内到手出金均值 / 购买成本均值；首次出金距购买日的自然日（只统计拿到出金的样本）。

用法：python research/futures_prop_study.py [plan_key ...]
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
from dataclasses import dataclass, field, replace

import numpy as np

PATHS_FILE = 'reports/futures_prop_paths.pkl'
OUT = 'reports/futures_prop_study.pkl'
STEP = 5
HORIZON = 252
QQQ_NOW = 691.95
NQ_QQQ_RATIO = 41.45
NC = QQQ_NOW * NQ_QQQ_RATIO * 2.0   # 单张 MNQ 名义
COMM_RT = 1.24                      # 每张来回手续费
EXIT_SLIP = 1.0                     # 风控 / 目标平仓每张额外滑点（2 tick）
SLIPS = (0.0, 0.035)

PATHS, DAYS = {}, []


@dataclass(frozen=True)
class Phase:
    dd: float                      # 最大回撤金额
    dd_type: str = 'eod'           # eod / intraday / static
    lock: float | None = 100.0     # trailing 底线最多升到 起始 + lock；None = 不锁
    dll: float = 0.0               # 日亏限额（相对当日起始余额）
    dll_hard: bool = False         # True = 触及即爆仓；False = 当日强平停手
    contracts: int = 1


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    size: float
    fee: float
    monthly: bool = False
    activation: float = 0.0
    # 考核（target=0 表示直接 funded）
    target: float = 0.0
    ev: Phase | None = None
    ev_consistency: float = 0.0    # 最佳单日 ≤ pct × 总利润
    ev_min_days: int = 0
    ev_day_cap: float = 0.0        # 我方自设单日止盈（为满足一致性）
    # funded
    fd: Phase = field(default_factory=lambda: Phase(2000))
    pay_min_days: int = 5          # 每个出金周期最少交易日
    pay_win_days: int = 0          # 每个周期需要的盈利日数
    pay_win_thr: float = 0.0       # 盈利日门槛
    pay_cycle: int = 0             # 两次出金最少间隔自然日
    pay_consistency: float = 0.0   # 周期内最佳单日 ≤ pct × 周期利润
    pay_buffer: float = 0.0        # 余额须 ≥ 起始 + buffer 才能申请
    pay_keep: float = 0.0          # 出金后余额须 ≥ 起始 + keep
    pay_frac: float = 1.0          # 每次最多提取 (余额-起始) 的比例
    pay_caps: tuple = ()           # 每次出金上限（按次序，最后一个重复；空 = 无上限）
    pay_min: float = 250.0
    pay_cycle_profit: float = 0.0  # 每个周期（首次自 funded 起）净利润至少多少才能申请
    pay_first_profit: float = 0.0  # 首次出金前总利润至少多少（LucidDirect 等）
    pay_cycle_mult: float = 0.0    # 单次出金 ≤ 周期利润 × 倍数（Tradeify Select Daily = 2）
    split: tuple = ((1e18, 0.9),)  # 累计出金分档：(累计上限, 分成)
    max_payouts: int = 0           # 达到后视为转 Live 结束（0 = 不限）
    fd_day_cap: float = 0.0
    floor_after_payout: str = 'keep'  # keep = 底线不动；lock = 出金后底线固定为 起始 + lock_after
    lock_after: float = 100.0
    auto: str = ''                 # 自动化 / API 说明
    note: str = ''


# ---------------------------------------------------------------- 日内引擎
def run_day(i, slip, ph: Phase, b0, floor_eod, peak0, start, day_cap=0.0, tgt_lo=None, tgt_hi=None):
    """返回 (状态, 当日盈亏, 当日最高权益峰值)；状态 ok / bust / pass。"""
    n = ph.contracts
    if DAYS[i][2] < 1.0 and n > 1:
        n = max(1, int(n * DAYS[i][2]))
    scale, comm, xs = n * NC, n * COMM_RT, n * EXIT_SLIP
    r = 0.0
    peak = peak0
    for adv, fav, f in PATHS[slip][i]:
        base = b0 + r
        lo = base + adv * scale
        hi = base + fav * scale
        lo[-1] -= comm
        hi[-1] -= comm
        m = len(lo)
        big = 1 << 30
        if ph.dd_type == 'intraday':
            pk = np.empty(m)
            pk[0] = peak
            if m > 1:
                pk[1:] = np.maximum(peak, np.maximum.accumulate(hi[:-1]))
            fl = pk - ph.dd
            if ph.lock is not None:
                fl = np.minimum(fl, start + ph.lock)
            hit = np.nonzero(lo <= fl)[0]
        else:
            hit = np.nonzero(lo <= floor_eod)[0]
        kb = hit[0] if len(hit) else big
        kd = big
        if ph.dll:
            h = np.nonzero(b0 - lo >= ph.dll)[0]
            kd = h[0] if len(h) else big
        kt = big
        if tgt_lo is not None and tgt_lo <= tgt_hi:
            h = np.nonzero(hi >= tgt_lo)[0]
            kt = h[0] if len(h) else big
        kc = big
        if day_cap:
            h = np.nonzero(hi - b0 >= day_cap)[0]
            kc = h[0] if len(h) else big
        k = min(kb, kd, kt, kc)
        if k >= m:
            peak = max(peak, float(hi.max()))
            r += f * scale - comm
            continue
        if kb == k:
            return 'bust', r, peak
        if kd == k:
            if ph.dll_hard:
                return 'bust', r, peak
            return 'ok', -ph.dll - xs - comm, peak
        if kt == k:
            return 'pass', tgt_lo - b0 - xs - comm, max(peak, tgt_lo)
        return 'ok', day_cap - xs - comm, max(peak, b0 + day_cap)
    return 'ok', r, peak


def _floor(ph, start, hwm):
    if ph.dd_type == 'static':
        return start - ph.dd
    fl = hwm - ph.dd
    return min(fl, start + ph.lock) if ph.lock is not None else fl


# ---------------------------------------------------------------- 账户
def months_billed(d0, d1):
    return (d1 - d0).days // 30 + 1


def run_eval(plan, i, slip, end):
    ph, start = plan.ev, plan.size
    b, hwm, best, n_days = start, start, 0.0, 0
    floor = _floor(ph, start, hwm)
    pct = plan.ev_consistency
    for j in range(i, end):
        lo_t = start + max(plan.target, best / pct if pct else 0.0)
        hi_t = (b - pct * start) / (1 - pct) if pct else 1e18
        st, pnl, peak = run_day(j, slip, ph, b, floor, max(hwm, b), start, plan.ev_day_cap, lo_t, hi_t)
        if st == 'bust':
            return 'fail', j
        b += pnl
        n_days += 1
        best = max(best, pnl)
        if ph.dd_type == 'intraday':
            hwm = max(hwm, peak)
        else:
            hwm = max(hwm, b)
        floor = max(floor, _floor(ph, start, hwm))
        if st == 'pass' or (b - start >= plan.target and (not pct or best <= pct * (b - start))):
            k = max(j, i + plan.ev_min_days - 1)
            return ('pass', k) if k < end else ('open', end - 1)
    return 'open', end - 1


def _at(v, n):
    if isinstance(v, tuple):
        return v[min(n, len(v) - 1)] if v else 0.0
    return v


def split_amount(plan, paid_before, amt):
    got, cum = 0.0, paid_before
    left = amt
    for cap, s in plan.split:
        if cum >= cap:
            continue
        x = min(left, cap - cum)
        got += x * s
        cum += x
        left -= x
        if left <= 0:
            break
    return got


def run_funded(plan, i, slip, end):
    ph, start = plan.fd, plan.size
    b, hwm = start, start
    floor = _floor(ph, start, hwm)
    cash, gross, n_pay = 0.0, 0.0, 0
    c_days = c_win = 0
    c_best, c_base, c_start = 0.0, start, DAYS[i][0]
    pays = []
    for j in range(i, end):
        st, pnl, peak = run_day(j, slip, ph, b, floor, max(hwm, b), start, plan.fd_day_cap)
        if st == 'bust':
            return 'bust', cash, n_pay, pays, j
        b += pnl
        if pnl != 0.0:
            c_days += 1
        if pnl >= plan.pay_win_thr and pnl > 0 and plan.pay_win_days:
            c_win += 1
        c_best = max(c_best, pnl)
        hwm = max(hwm, peak if ph.dd_type == 'intraday' else b)
        floor = max(floor, _floor(ph, start, hwm))
        # 出金资格
        if c_days < plan.pay_min_days or c_win < plan.pay_win_days:
            continue
        if plan.pay_cycle and (DAYS[j][0] - c_start).days < plan.pay_cycle:
            continue
        if b - start < plan.pay_buffer:
            continue
        cyc = b - c_base
        if cyc <= 0 or cyc < _at(plan.pay_cycle_profit, n_pay):
            continue
        if n_pay == 0 and b - start < plan.pay_first_profit:
            continue
        cons = _at(plan.pay_consistency, n_pay)
        if cons and c_best > cons * cyc:
            continue
        amt = min((b - start) * plan.pay_frac, b - start - plan.pay_keep)
        if plan.pay_cycle_mult:
            amt = min(amt, cyc * plan.pay_cycle_mult)
        if plan.pay_caps:
            amt = min(amt, plan.pay_caps[min(n_pay, len(plan.pay_caps) - 1)])
        if amt < plan.pay_min:
            continue
        b -= amt
        cash += split_amount(plan, gross, amt)
        gross += amt
        n_pay += 1
        pays.append((j, cash))
        if plan.floor_after_payout == 'lock':
            floor = start + plan.lock_after
            ph = replace(ph, lock=plan.lock_after)
        c_days = c_win = 0
        c_best, c_base, c_start = 0.0, b, DAYS[j][0]
        if plan.max_payouts and n_pay >= plan.max_payouts:
            return 'live', cash, n_pay, pays, j
    return 'alive', cash, n_pay, pays, end - 1


def run_account(plan, i, slip, end=None):
    end = min(len(DAYS), i + HORIZON) if end is None else end
    d0 = DAYS[i][0]
    if plan.ev is not None:
        st, j = run_eval(plan, i, slip, end)
        cost = plan.fee * (months_billed(d0, DAYS[j][0]) if plan.monthly else 1)
        if st != 'pass':
            return dict(st=st, cost=cost, cash=0.0, n_pay=0, first=None, pass_days=None, end=j)
        pass_days = (DAYS[j][0] - d0).days
        fstart = j + 1
    else:
        cost, pass_days, fstart = plan.fee, 0, i
    cost += plan.activation
    if fstart >= end:
        return dict(st='open', cost=cost, cash=0.0, n_pay=0, first=None, pass_days=pass_days, end=end - 1)
    st, cash, n_pay, pays, j = run_funded(plan, fstart, slip, end)
    first = (DAYS[pays[0][0]][0] - d0).days if pays else None
    return dict(st=st, cost=cost, cash=cash, n_pay=n_pay, first=first, pass_days=pass_days, end=j)


def run_slot(plan, i, slip, horizon=HORIZON):
    """一个账户名额：失败 / 爆仓 / 转 Live 后次日立即重买，统计 horizon 内的总成本与总到手。"""
    end = min(len(DAYS), i + horizon)
    cost = cash = 0.0
    n_buy = 0
    j = i
    while j < end:
        r = run_account(plan, j, slip, end)
        cost += r['cost']
        cash += r['cash']
        n_buy += 1
        j = r['end'] + 1
    return cost, cash, n_buy


def job(spec):
    plan, slip = spec
    last = len(DAYS) - HORIZON
    return spec, [run_account(plan, i, slip) for i in range(0, last, STEP)]


def summarize(rows):
    cost = np.mean([r['cost'] for r in rows])
    cash = np.mean([r['cash'] for r in rows])
    passed = np.mean([r['pass_days'] is not None for r in rows])
    paid = [r['first'] for r in rows if r['first'] is not None]
    bust_f = np.mean([r['st'] == 'bust' for r in rows])
    return dict(n=len(rows), cost=cost, cash=cash, ratio=cash / cost, net=cash - cost, passed=passed,
                p_pay=len(paid) / len(rows), first_med=np.median(paid) if paid else np.nan,
                first_mean=np.mean(paid) if paid else np.nan, bust_f=bust_f,
                pays=np.mean([r['n_pay'] for r in rows]))


def load_paths():
    global PATHS, DAYS
    obj = pickle.load(open(PATHS_FILE, 'rb'))
    PATHS, DAYS = obj['paths'], obj['days']


def run(plans):
    load_paths()
    specs = [(p, s) for p in plans for s in SLIPS]
    res = {}
    t0 = time.time()
    with mp.get_context('fork').Pool(6) as pool:
        for spec, rows in pool.imap_unordered(job, specs, chunksize=2):
            res[(spec[0].key, spec[1])] = rows
    print(f'{len(specs)} 组完成 {time.time() - t0:.0f}s', flush=True)
    return res


if __name__ == '__main__':
    load_paths()
    print(len(DAYS), NC)
