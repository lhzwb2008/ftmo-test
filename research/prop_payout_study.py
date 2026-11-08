#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prop 账户通过率 / funded 出金测算（新版策略 R+开盘冷清半仓，可按"实盘打折"重算）。

滚动起点：2020 年起每 STEP 个交易日当作一次开考 / 开户，逐日 simulate_day 跑到结束。
实盘打折：给每股额外加滑点，使 1x 年化收益降到回测的 HAIRCUT 倍（保留原有波动路径）。

  FTMO 2-Step：P1 +10% / P2 +5%（目标+1% 盘中触及或收盘达标），最少 4 天；日亏 5%（EA 4.75%）；总亏 10% 静态
  Aqua Standard 2-Step 100K：P1 +8% / P2 +5%；日亏 5%（EA 4.75%）；总亏 8% 静态；
      funded 单笔硬止损 0.9%（Wave Stop 1%）；满 14 天且余额 > 初始即提走超出部分，90% 分润再扣 3% 手续费
  Aqua Pay Later 200K：Challenge +3%、5% trailing、硬止损 0.95%、单笔浮亏 1% 封号；
      funded 规则同 backtest_aqua_paylater_funded.py（日亏 2.85%、5% trailing、日盈帽、15% 一致性、5 个 ≥0.5% 盈利日）

用法：python research/prop_payout_study.py calibrate | run
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
from datetime import date
from math import floor

import numpy as np
import pandas as pd

import ftmo_ibkr_combo_backtest as combo
from backtest import quiet_open_multipliers, simulate_day

MERGED = 'reports/qqq_merged_minutes.csv'
DATA_START = date(2019, 11, 1)
SIM_START = date(2020, 1, 1)
STEP = 10
HORIZON = 252  # funded 观察 12 个月
OUT = 'reports/prop_payout_study.pkl'

DAYS, ALLOWED, BASE_CFG, QUIET = [], [], {}, {}


def load():
    global DAYS, ALLOWED, BASE_CFG, QUIET
    cfg = combo.strategy_config({'data_path': MERGED, 'start_date': DATA_START, 'end_date': None})
    cfg.update(K2=1, enable_k_side_adjustment=False,
               entry_trend_filter=[{'metric': 'er5', 'min': 0.1}, {'metric': 'range1', 'max': 0.04},
                                   {'metric': 'sigma', 'min': 0.0003}])
    price_df, allowed, _ = combo.prepare_strategy_data(cfg)
    raw = pd.read_csv(MERGED, usecols=['DateTime', 'Volume'], parse_dates=['DateTime'])
    raw['Date'] = raw['DateTime'].dt.date
    raw['Time'] = raw['DateTime'].dt.strftime('%H:%M')
    QUIET, _ = quiet_open_multipliers(raw, {'quiet_open_half_position': True})
    days = []
    for d, g in price_df.groupby('Date', sort=True):
        if d < SIM_START:
            continue
        g = g.sort_values('DateTime').reset_index(drop=True)
        if len(g) < 300:
            continue
        pc = g['prev_close'].iloc[0]
        days.append((d, g, None if pd.isna(pc) else float(pc)))
    DAYS, ALLOWED, BASE_CFG = days, allowed, cfg


PATHS = {}  # extra_slip -> [每日 [(浮亏 running-min, 浮盈 running-max, 平仓盈亏)，单位=1x 名义本金的比例]]
PATH_CAPITAL = 200_000.0


def _day_paths(args):
    i, slip = args
    d, g, pc = DAYS[i]
    day_open = float(g['day_open'].iloc[0])
    pos = int(floor(PATH_CAPITAL / day_open))
    cfg = dict(BASE_CFG)
    cfg.update(initial_capital=PATH_CAPITAL, slippage_per_share=0.01 + slip, enable_intraday_stop_loss=False,
               max_daily_loss_amount=0, max_daily_profit_amount=0, enable_per_trade_stop_loss=False)
    trades = combo.unpack_simulate_day(simulate_day(g, pc, ALLOWED, pos, cfg, PATH_CAPITAL))[0]
    notional = pos * day_open
    times = g['DateTime'].values
    lo_px, hi_px = g['Low'].values, g['High'].values
    out = []
    for t in trades:
        a = int(np.searchsorted(times, np.datetime64(pd.Timestamp(t['entry_time']))))
        b = int(np.searchsorted(times, np.datetime64(pd.Timestamp(t['exit_time']))))
        sh, ep, fee = t['position_size'], t['entry_price'], t.get('transaction_fees', 0.0) or 0.0
        if t['side'] == 'Long':
            adverse, favor = (lo_px[a + 1:b] - ep) * sh, (hi_px[a + 1:b] - ep) * sh
        else:
            adverse, favor = (ep - hi_px[a + 1:b]) * sh, (ep - lo_px[a + 1:b]) * sh
        f = t['pnl'] / notional
        lo = np.minimum.accumulate(np.append((adverse - fee) / notional, f))
        hi = np.maximum.accumulate(np.append((favor - fee) / notional, f))
        out.append((-lo, hi, f))
    return out


def build_paths(slips):
    with mp.get_context('fork').Pool(6) as pool:
        for s in slips:
            PATHS[s] = pool.map(_day_paths, [(i, s) for i in range(len(DAYS))], chunksize=20)


def _first(arr, thr):
    return int(np.searchsorted(arr, thr)) if thr > 0 else 1 << 30


class Engine:
    """在 1x 路径上按名义本金缩放；日亏止损 / 日盈帽触发后当天停止交易，单笔硬止损只截断该笔。"""

    def __init__(self, lev, size, extra_slip, daily_stop=0.0, day_cap=0.0, hard_sl=0.0):
        self.lev, self.size, self.paths = lev, size, PATHS[extra_slip]
        self.daily_stop, self.day_cap, self.hard_sl_usd = daily_stop, day_cap, size * hard_sl
        self.slip, self.hard_sl, self.cache = extra_slip, hard_sl, {}

    def _full(self, i, capital):
        """单笔硬止损触发后策略可能再次进场，这类日子用完整分钟模拟（资金按 0.25% 分档缓存）。"""
        q = self.size * 0.0025
        cap_q = round(capital / q) * q
        key = (i, cap_q)
        if key not in self.cache:
            d, g, pc = DAYS[i]
            day_open = float(g['day_open'].iloc[0])
            pos = int(floor(cap_q * self.lev * QUIET.get(d, 1.0) / day_open))
            cfg = dict(BASE_CFG)
            cfg.update(initial_capital=self.size, slippage_per_share=0.01 + self.slip,
                       enable_intraday_stop_loss=self.daily_stop > 0, intraday_stop_loss_mode='from_day_start',
                       max_daily_loss_amount=self.daily_stop, max_daily_profit_amount=self.day_cap,
                       enable_per_trade_stop_loss=True,
                       per_trade_stop_loss_pct=self.hard_sl_usd / max(pos * day_open, 1.0))
            trades, _, _, low, high = combo.unpack_simulate_day(simulate_day(g, pc, ALLOWED, pos, cfg, cap_q))
            pnl = sum(t['pnl'] for t in trades)
            worst = min((t['pnl'] for t in trades), default=0.0)
            self.cache[key] = (pnl, min(low - cap_q, pnl, 0.0), max(high - cap_q, pnl, 0.0), worst)
        pnl, lo, hi, worst = self.cache[key]
        return pnl, capital + lo, capital + hi, worst

    def day(self, i, capital):
        n = capital * self.lev * QUIET.get(DAYS[i][0], 1.0)
        if n <= 0:
            return 0.0, capital, capital, 0.0
        s = self.daily_stop / n if self.daily_stop else 0.0
        c = self.day_cap / n if self.day_cap else 0.0
        h = self.hard_sl_usd / n if self.hard_sl_usd else 0.0
        sp = (0.01 + self.slip) / float(DAYS[i][1]['day_open'].iloc[0])
        r = lo = hi = 0.0
        worst = 0.0
        for neg_lo, run_hi, f in self.paths[i]:
            hs = _first(neg_lo, h) if h else 1 << 30
            ds = _first(neg_lo, s + r) if s else 1 << 30
            cs = int(np.searchsorted(run_hi, c - r)) if c else 1 << 30
            m = len(neg_lo)
            if min(hs, ds, cs) >= m:
                lo, hi = min(lo, r - neg_lo[-1]), max(hi, r + run_hi[-1])
                r += f
                worst = min(worst, f)
            elif ds <= hs and ds <= cs:
                worst = min(worst, -s - r - sp)
                r = lo = -s - sp
                break
            elif hs <= cs:
                return self._full(i, capital)
            else:
                lo = min(lo, r - (neg_lo[cs - 1] if cs else 0.0))
                worst = min(worst, c - r - sp)
                hi, r = c, c - sp
                break
        return r * n, capital + min(lo, r) * n, capital + max(hi, r) * n, worst * n


# ---------------------------------------------------------------- 考核阶段
def phase(eng, i, target, daily_limit, static_floor, trail=None, min_days=0, tp_buffer=0.01,
          float_breach=None):
    size = eng.size
    cap = hwm = size
    for j in range(i, len(DAYS)):
        pnl, low, high, worst = eng.day(j, cap)
        if float_breach and worst <= -size * float_breach:
            return 'fail', j
        if daily_limit and cap - low >= size * daily_limit:
            return 'fail', j
        if static_floor and low <= size * (1 - static_floor):
            return 'fail', j
        if trail and low <= hwm * (1 - trail):
            return 'fail', j
        hit = high >= size * (1 + target + tp_buffer)
        cap += pnl
        hwm = max(hwm, cap)
        if hit or cap >= size * (1 + target):
            k = max(j, i + min_days - 1)
            return ('pass', k) if k < len(DAYS) else ('open', len(DAYS) - 1)
    return 'open', len(DAYS) - 1


# ---------------------------------------------------------------- funded 出金
def aqua_std_funded(eng, i, horizon=HORIZON):
    size = eng.size
    cap = size
    cash, n_pay, last = 0.0, 0, DAYS[i][0]
    end = min(len(DAYS), i + horizon)
    for j in range(i, end):
        pnl, low, high, worst = eng.day(j, cap)
        if cap - low >= size * 0.05 or low <= size * 0.92:
            return 'bust', cash, n_pay, j
        cap += pnl
        if (DAYS[j][0] - last).days >= 14 and cap > size + 100:
            cash += (cap - size) * 0.9 * 0.97
            n_pay += 1
            cap = size
            last = DAYS[j][0]
    return ('alive' if end - i >= horizon else 'open'), cash, n_pay, end - 1


def paylater_funded(eng, i, horizon=HORIZON, balance=200_000.0, hwm=None, log=None, cycle_base=None):
    size = 200_000.0
    cap = balance
    hwm = hwm or balance
    cash, n_pay = 0.0, 0
    cycle_start, pnls = DAYS[i][0], []
    cycle_base = cap if cycle_base is None else cycle_base
    end = min(len(DAYS), i + horizon)
    for j in range(i, end):
        pnl, low, high, worst = eng.day(j, cap)
        floor_ = hwm * 0.95
        if worst <= -size * 0.01 or cap - low >= size * 0.03 or low <= floor_:
            return 'bust', cash, n_pay, j
        cap += pnl
        hwm = max(hwm, cap)
        pnls.append(pnl)
        net = cap - cycle_base
        good = sum(1 for p in pnls if p >= size * 0.005)
        if good < 5 or (DAYS[j][0] - cycle_start).days < 14 or net <= 0:
            continue
        if max(pnls) >= 0.15 * net:
            continue
        leave = max(size, hwm * 0.95 + size * 0.01)
        req = min(cap - size, cap - leave)
        if n_pay < 2:
            req = min(req, 10_000.0)
        if req < 100:
            continue
        cap -= req
        cash += req * 0.9 * 0.97
        n_pay += 1
        if log is not None:
            log.append((j - i + 1, req * 0.9 * 0.97))
        cycle_start, cycle_base, pnls = DAYS[j][0], cap, []
    return ('alive' if end - i >= horizon else 'open'), cash, n_pay, end - 1


# ---------------------------------------------------------------- 任务
def starts(horizon=0):
    last = len(DAYS) - 1 - horizon
    return list(range(0, last + 1, STEP))


def job(spec):
    kind, lev, slip, extra = spec
    t0 = time.time()
    out = []
    if kind == 'ftmo':
        e = Engine(lev, 100_000.0, slip, daily_stop=100_000 * 0.05 * 0.95)
        for i in starts():
            st, j = phase(e, i, 0.10, 0.05, 0.10, min_days=4)
            if st == 'pass' and j + 1 < len(DAYS):
                st, j = phase(e, j + 1, 0.05, 0.05, 0.10, min_days=4)
            elif st == 'pass':
                st = 'open'
            out.append((i, st, j))
    elif kind == 'aqua_std':
        ch = Engine(lev, 100_000.0, slip, daily_stop=100_000 * 0.05 * 0.95)
        fd = Engine(lev, 100_000.0, slip, daily_stop=100_000 * 0.05 * 0.95, hard_sl=0.009)
        for i in starts():
            st, j = phase(ch, i, 0.08, 0.05, 0.08, min_days=3)
            if st == 'pass' and j + 1 < len(DAYS):
                st, j = phase(ch, j + 1, 0.05, 0.05, 0.08, min_days=3)
            elif st == 'pass':
                st = 'open'
            res = None
            if st == 'pass' and j + 1 < len(DAYS):
                res = aqua_std_funded(fd, j + 1)
            out.append((i, st, j, res))
        for i in starts(HORIZON):
            out.append(('funded_only', i, aqua_std_funded(fd, i)))
    elif kind == 'pl_challenge':
        e = Engine(lev, 200_000.0, slip, hard_sl=0.0095)
        for i in starts():
            out.append((i, *phase(e, i, 0.03, 0, 0, trail=0.05, tp_buffer=0.0, float_breach=0.01)))
    elif kind == 'pl_funded':
        cap_amt = extra
        e = Engine(lev, 200_000.0, slip, daily_stop=200_000 * 0.03 * 0.95, day_cap=cap_amt, hard_sl=0.0095)
        for i in starts(HORIZON):
            out.append(('new', i, paylater_funded(e, i)))
            out.append(('now', i, paylater_funded(e, i, balance=197_000.0, hwm=203_000.0, cycle_base=200_000.0)))
    return spec, out, time.time() - t0


def cagr_1x(slip):
    e = Engine(1.0, 100_000.0, slip)
    cap = 100_000.0
    for i in range(len(DAYS)):
        cap += e.day(i, cap)[0]
    yrs = (DAYS[-1][0] - DAYS[0][0]).days / 365.25
    return (cap / 1e5) ** (1 / yrs) - 1


def full_day(i, capital, lev, size, slip, daily_stop, day_cap, hard_sl):
    d, g, pc = DAYS[i]
    day_open = float(g['day_open'].iloc[0])
    pos = int(floor(capital * lev * QUIET.get(d, 1.0) / day_open))
    cfg = dict(BASE_CFG)
    cfg.update(initial_capital=size, slippage_per_share=0.01 + slip, enable_intraday_stop_loss=daily_stop > 0,
               intraday_stop_loss_mode='from_day_start', max_daily_loss_amount=daily_stop,
               max_daily_profit_amount=day_cap, enable_per_trade_stop_loss=hard_sl > 0,
               per_trade_stop_loss_pct=size * hard_sl / (pos * day_open) if hard_sl else 0.03)
    trades = combo.unpack_simulate_day(simulate_day(g, pc, ALLOWED, pos, cfg, capital))[0]
    return sum(t['pnl'] for t in trades)


def validate(slip):
    rng = np.random.default_rng(0)
    idx = rng.choice(len(DAYS), 300, replace=False)
    for name, lev, size, stop, cap, hsl in (('FTMO 4x', 4.0, 1e5, 4750, 0, 0),
                                            ('PayLater 1x 帽1100', 1.0, 2e5, 5700, 1100, 0.0095),
                                            ('PayLater 2x 无帽', 2.0, 2e5, 5700, 0, 0.0095)):
        e = Engine(lev, size, slip, stop, cap, hsl)
        fast = np.array([e.day(i, size)[0] for i in idx])
        full = np.array([full_day(i, size, lev, size, slip, stop, cap, hsl) for i in idx])
        print(f'  校验 {name}: 均值 快 {fast.mean():.1f} / 全 {full.mean():.1f}，相关 {np.corrcoef(fast, full)[0, 1]:.3f}，'
              f'|差|中位 {np.median(np.abs(fast - full)):.1f}', flush=True)


def run(slip_haircut):
    load()
    t0 = time.time()
    build_paths((0.0, slip_haircut))
    print(f'路径预计算 {time.time() - t0:.0f}s；1x 年化 回测 {cagr_1x(0.0):+.2%} / 打折 {cagr_1x(slip_haircut):+.2%}', flush=True)
    validate(slip_haircut)
    specs = []
    for slip in (0.0, slip_haircut):
        specs += [('ftmo', lev, slip, None) for lev in (1.5, 2.0, 3.0, 4.0)]
        specs += [('aqua_std', lev, slip, None) for lev in (1.0, 2.0, 3.0, 4.0)]
        specs += [('pl_challenge', lev, slip, None) for lev in (1.0, 2.0)]
        specs += [('pl_funded', lev, slip, cap) for lev in (1.0, 1.5, 2.0, 3.0)
                  for cap in (1100.0, 1500.0, 2000.0, 0.0)]
    t0 = time.time()
    results = {}
    with mp.get_context('fork').Pool(6) as pool:
        for spec, out, dt in pool.imap_unordered(job, specs):
            results[spec] = out
            print(f'{spec} 完成 {dt:.0f}s（累计 {time.time() - t0:.0f}s，{len(results)}/{len(specs)}）', flush=True)
    pickle.dump({'results': results, 'dates': [d for d, _, _ in DAYS], 'slip': slip_haircut}, open(OUT, 'wb'))


if __name__ == '__main__':
    run(float(sys.argv[1]))
