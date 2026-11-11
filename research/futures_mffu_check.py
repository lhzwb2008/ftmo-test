#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MFFU Rapid 专项：在 futures_prop_study 上补两条官方规则（2026-11-11 核实 help.myfundedfutures.com），重算单名额一年净收益。

1. News Trading Policy：Rapid / Pro Sim Funded 在 Tier 1 新闻前后 2 分钟不得有持仓或挂单（考核不受限）。
   对本策略只影响 FOMC 声明 / 纪要（14:00 ET）；CPI、非农在 8:30，早于开盘。
   做法：FOMC 日 13:56 收盘价强平，当日不再开仓。纪要日按声明日 + 21 天近似（落在非交易日取前一个交易日）。
2. Understanding Rapid Live：Risk 团队按「持续出金」自行决定转 Live，不可拒绝；转后所有 sim 账户休眠、暂停新购买，
   账户内利润最多 $5,000 进 Reserve。按「第 N 次出金后转 Live、名额终止不重买」做敏感性，账户内余利与 Live 账户价值都按 0 计。
另：持有任一 100K / 150K Sim Funded 时 Sim Funded 合计最多 3 张；只持 25K / 50K 最多 5 张；考核最多 10 张。

用法：python research/futures_mffu_check.py
"""

from __future__ import annotations

import os as _os
import sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, ROOT)
_os.chdir(ROOT)  # 数据与输出路径均相对仓库根目录

import multiprocessing as mp
from bisect import bisect_right
from dataclasses import replace
from datetime import date, timedelta
from math import floor

import numpy as np
import pandas as pd

import ftmo_ibkr_combo_backtest as combo
import futures_prop_study as fps
import prop_payout_study as pps
from backtest import simulate_day
from futures_prop_analyze import build
from futures_prop_paths import DATA, PATH_CAPITAL
from futures_prop_plans import PLANS

OUT_TXT = 'reports/futures_mffu_check.txt'
RECENT = '2024-09-01'
FLAT_AT = pd.Timestamp('13:56').time()

FOMC_STATEMENTS = [
    '2020-01-29', '2020-04-29', '2020-06-10', '2020-07-29', '2020-09-16', '2020-11-05', '2020-12-16',
    '2021-01-27', '2021-03-17', '2021-04-28', '2021-06-16', '2021-07-28', '2021-09-22', '2021-11-03', '2021-12-15',
    '2022-01-26', '2022-03-16', '2022-05-04', '2022-06-15', '2022-07-27', '2022-09-21', '2022-11-02', '2022-12-14',
    '2023-02-01', '2023-03-22', '2023-05-03', '2023-06-14', '2023-07-26', '2023-09-20', '2023-11-01', '2023-12-13',
    '2024-01-31', '2024-03-20', '2024-05-01', '2024-06-12', '2024-07-31', '2024-09-18', '2024-11-07', '2024-12-18',
    '2025-01-29', '2025-03-19', '2025-05-07', '2025-06-18', '2025-07-30', '2025-09-17', '2025-10-29', '2025-12-10',
    '2026-01-28', '2026-03-18', '2026-04-29', '2026-06-17', '2026-07-29', '2026-09-16', '2026-10-28',
]

CASES = (
    ('mfr100', '4|2|2700'), ('mfr50', '3|1|1350'),
    ('fnf50', '2|2|900'), ('luf50', '3|3|1080'),
)
LIVE_AFTER = (0, 5, 3)


def fomc_days(trading_days):
    out = set()
    for s in FOMC_STATEMENTS:
        d = date.fromisoformat(s)
        for t in (d, d + timedelta(days=21)):
            k = bisect_right(trading_days, t) - 1
            if k >= 0 and (t - trading_days[k]).days < 4:
                out.add(k)
    return sorted(out)


def _flat_day(args):
    """FOMC 日：13:56 收盘价强平，之后不开新仓；单位与 futures_prop_paths 相同。"""
    i, slip = args
    d, g, pc = pps.DAYS[i]
    day_open = float(g['day_open'].iloc[0])
    pos = int(floor(PATH_CAPITAL / day_open))
    cfg = dict(pps.BASE_CFG)
    cfg.update(initial_capital=PATH_CAPITAL, slippage_per_share=0.01 + slip, enable_transaction_fees=False,
               enable_intraday_stop_loss=False, max_daily_loss_amount=0, max_daily_profit_amount=0,
               enable_per_trade_stop_loss=False)
    trades = combo.unpack_simulate_day(simulate_day(g, pc, pps.ALLOWED, pos, cfg, PATH_CAPITAL))[0]
    notional = pos * day_open
    times = g['DateTime'].values
    lo_px, hi_px, cl_px = g['Low'].values, g['High'].values, g['Close'].values
    k = int(np.nonzero(g['DateTime'].dt.time.values <= FLAT_AT)[0][-1])
    out, n_cut = [], 0
    for t in trades:
        a = int(np.searchsorted(times, np.datetime64(pd.Timestamp(t['entry_time']))))
        b = int(np.searchsorted(times, np.datetime64(pd.Timestamp(t['exit_time']))))
        if a >= k:
            n_cut += 1
            continue
        sh, ep, long_ = t['position_size'], t['entry_price'], t['side'] == 'Long'
        pnl = t['pnl']
        if b > k:
            b = k + 1
            px = cl_px[k] - (0.01 + slip) if long_ else cl_px[k] + (0.01 + slip)
            pnl = (px - ep) * sh if long_ else (ep - px) * sh
            n_cut += 1
        if long_:
            adv, fav = (lo_px[a + 1:b] - ep) * sh, (hi_px[a + 1:b] - ep) * sh
        else:
            adv, fav = (ep - hi_px[a + 1:b]) * sh, (ep - lo_px[a + 1:b]) * sh
        f = pnl / notional
        out.append((np.append(adv / notional, f).astype(np.float32),
                    np.append(fav / notional, f).astype(np.float32), float(f)))
    return i, slip, out, n_cut


P_EV, P_FD = {}, {}
_orig_eval, _orig_funded = fps.run_eval, fps.run_funded


def _eval_news_ok(*a):
    fps.PATHS = P_EV
    return _orig_eval(*a)


def _funded_news_flat(*a):
    fps.PATHS = P_FD
    return _orig_funded(*a)


def run_slot(plan, i, slip, stop_at_live, horizon=fps.HORIZON):
    """stop_at_live=False 时与 futures_prop_study.run_slot 相同（FundedNext / Lucid 满 5 次出金后结束可重买）。"""
    end = min(len(fps.DAYS), i + horizon)
    cost = cash = 0.0
    n_buy, j, live = 0, i, False
    while j < end:
        r = fps.run_account(plan, j, slip, end)
        cost += r['cost']
        cash += r['cash']
        n_buy += 1
        if stop_at_live and r['st'] == 'live':
            live = True
            break
        j = r['end'] + 1
    return cost, cash, n_buy, live


def slot_job(spec):
    plan, slip, idx, news = spec
    fps.run_eval = _eval_news_ok
    fps.run_funded = _funded_news_flat if news else _orig_funded
    fps.PATHS = P_EV
    return [run_slot(plan, i, slip, plan.key.startswith('mf')) for i in idx]


def main():
    global P_EV, P_FD
    fps.load_paths()
    P_EV = fps.PATHS
    hair = fps.SLIPS[1]
    trading_days = [m[0] for m in fps.DAYS]
    fd_idx = fomc_days(trading_days)

    pps.MERGED = DATA
    pps.load()
    assert [d for d, _, _ in pps.DAYS] == trading_days, '行情缓存与路径文件的交易日不一致，先重跑 futures_prop_paths.py'
    with mp.get_context('fork').Pool(6) as pool:
        flat = pool.map(_flat_day, [(i, hair) for i in fd_idx])
    P_FD = {hair: list(P_EV[hair])}
    n_cut = 0
    for i, _, out, c in flat:
        P_FD[hair][i] = out
        n_cut += c
    yrs = (trading_days[-1] - trading_days[0]).days / 365.25
    d_ev = sum(sum(t[2] for t in P_EV[hair][i]) for i in fd_idx)
    d_fd = sum(sum(t[2] for t in P_FD[hair][i]) for i in fd_idx)
    lines = [f'FOMC 声明 / 纪要日 {len(fd_idx)} 天（{len(fd_idx) / yrs:.1f} 天/年），其中 {n_cut} 笔交易被提前平仓或取消；'
             f'这些天 1x 收益合计 {d_ev:+.3f} → {d_fd:+.3f}（打折口径）']

    plans = {p.key: p for p in PLANS}
    idx_all = list(range(0, len(fps.DAYS) - fps.HORIZON, fps.STEP * 2))
    idx_rec = [i for i in range(0, len(fps.DAYS) - fps.HORIZON, fps.STEP) if str(fps.DAYS[i][0]) >= RECENT]
    specs, labels = [], []
    for key, cfg in CASES:
        base = build(plans[key], f'{key}|{cfg}')
        mffu = key.startswith('mf')
        variants = [('原模型', base, False)]
        if mffu:
            for n in LIVE_AFTER:
                tag = 'FOMC 空仓' + (f' + 第 {n} 次出金后转 Live' if n else '')
                variants.append((tag, replace(base, max_payouts=n) if n else base, True))
        for tag, p, news in variants:
            labels.append((p.name, tag))
            specs += [(p, hair, idx_all, news), (p, hair, idx_rec, news)]
    with mp.get_context('fork').Pool(6) as pool:
        out = pool.map(slot_job, specs)

    lines.append('\n== 单名额连续一年（失败即重买；转 Live 即终止），打折口径：年均净收益（全样本 / 近期 = 2024-09 起开户）')
    for n, (name, tag) in enumerate(labels):
        a, r = np.array(out[2 * n]), np.array(out[2 * n + 1])
        lines.append(f'{name:<20} {tag:<24} 全样本 净 ${(a[:, 1] - a[:, 0]).mean():>6.0f} | 近期 净 ${(r[:, 1] - r[:, 0]).mean():>6.0f}'
                     f'（成本 ${r[:, 0].mean():.0f}，到手 ${r[:, 1].mean():.0f}，买 {r[:, 2].mean():.1f} 次，'
                     f'亏钱名额 {np.mean(r[:, 1] < r[:, 0]):.0%}，一年内转 Live {np.mean(r[:, 3]):.0%}）')
    txt = '\n'.join(lines)
    open(OUT_TXT, 'w').write(txt + '\n')
    print(txt)


if __name__ == '__main__':
    main()
