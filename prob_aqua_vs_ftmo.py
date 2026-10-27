#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aqua Pay Later 200K「首次可提现」 vs FTMO 2-Step 100K「拿到 funded」概率（滚动起点历史测算）。

每个交易日都当作一次「开考日」，用同一套信号（backtest.simulate_day）逐日跑到成功或爆仓：
  Aqua Pay Later 200K（对齐 simulate_aqua_paylater.py / SQLiteSignalEA_aqua_paylater.mq5）
    Challenge: 目标 +3%（日内权益触及即达标）；无日亏；5% trailing（EOD 余额高点）；
               单笔硬止损 0.95% 初始资金，单笔亏损 ≥1% 视为浮亏封号
    Funded:    日亏 3%（EA 2.85% 触发）；5% trailing；单笔 1% 封号；日盈帽 $1100；
               首次出金：满 14 日历日 + ≥5 天日盈 ≥$1000 + 最好一天 <15% 周期净利 + 余额高于地板缓冲
  FTMO 2-Step 100K（对齐 simulate_ftmo.py / SQLiteSignalEA_ftmo.mq5）
    Phase1 +10% / Phase2 +5%（脚本止盈=目标+1%，日内触及或收盘达标），最少 4 个交易日；
    日亏 5%（EA 4.75% 触发）；总亏 10% 静态地板；无时间限制
    Funded（参考）：首次出金 = 满 14 日历日且收盘余额 > 初始

数据：2024 年前 Quantra/qqq_market_hours_with_indicators.csv，之后 qqq_longport.csv
用法：python prob_aqua_vs_ftmo.py [--step N] [--from YYYY-MM-DD] [--lev-aqua X] [--lev-ftmo X]
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import time
from datetime import date
from math import floor

import pandas as pd

from backtest import simulate_day
import ftmo_ibkr_combo_backtest as combo

LONG_HIST = '/Users/wenbozhang/workspace/Quantra/qqq_market_hours_with_indicators.csv'
LONGPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'qqq_longport.csv')
MERGED = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reports', 'qqq_merged_minutes.csv')
SPLIT_DATE = pd.Timestamp('2024-01-01')

AQUA_SIZE = 200_000.0
AQUA_TARGET = 0.03
AQUA_TRAIL = 0.05
AQUA_HARD_SL = 0.0095
AQUA_FLOAT_BREACH = 0.01
AQUA_DAILY_LOSS = 0.03
AQUA_DAILY_BUFFER = 0.05
AQUA_DAY_CAP = 1100.0
AQUA_MIN_DAY_PROFIT = AQUA_SIZE * 0.005
AQUA_MIN_GOOD_DAYS = 5
AQUA_MIN_CAL_DAYS = 14
AQUA_CONSISTENCY = 0.15
AQUA_SAFETY = AQUA_SIZE * 0.01

FTMO_SIZE = 100_000.0
FTMO_P1 = 0.10
FTMO_P2 = 0.05
FTMO_TP_BUFFER = 0.01
FTMO_DAILY_LOSS = 0.05
FTMO_DAILY_BUFFER = 0.05
FTMO_MAX_LOSS = 0.10
FTMO_MIN_DAYS = 4
FTMO_PAYOUT_DAYS = 14

DAYS = []
ALLOWED = []
BASE_CFG = {}
LEV = {'aqua': 2.0, 'ftmo': 2.0}


def build_merged_csv():
    if os.path.exists(MERGED):
        return
    os.makedirs(os.path.dirname(MERGED), exist_ok=True)
    cols = ['DateTime', 'Open', 'High', 'Low', 'Close', 'Volume']
    a = pd.read_csv(LONG_HIST, usecols=cols, parse_dates=['DateTime'])
    a = a[(a['DateTime'] < SPLIT_DATE) & (a['DateTime'].dt.strftime('%H:%M') <= '15:59')]
    b = pd.read_csv(LONGPORT, usecols=cols, parse_dates=['DateTime'])
    b = b[b['DateTime'] >= SPLIT_DATE]
    pd.concat([a, b]).sort_values('DateTime').to_csv(MERGED, index=False)


def load_days(start: date):
    global DAYS, ALLOWED, BASE_CFG
    cfg = combo.strategy_config({'data_path': MERGED, 'start_date': start, 'end_date': None})
    price_df, allowed, dates = combo.prepare_strategy_data(cfg)
    days = []
    for d, g in price_df.groupby('Date', sort=True):
        g = g.sort_values('DateTime').reset_index(drop=True)
        if len(g) < 300:
            continue
        pc = g['prev_close'].iloc[0]
        days.append((d, g, None if pd.isna(pc) else float(pc)))
    DAYS, ALLOWED, BASE_CFG = days, allowed, cfg


def run_day(i, capital, lev, size, daily_stop=0.0, day_cap=0.0, hard_sl=0.0):
    d, g, pc = DAYS[i]
    day_open = float(g['day_open'].iloc[0])
    pos = int(floor(capital * lev / day_open))
    cfg = dict(BASE_CFG)
    cfg['initial_capital'] = size
    cfg['enable_intraday_stop_loss'] = daily_stop > 0
    cfg['intraday_stop_loss_mode'] = 'from_day_start'
    cfg['max_daily_loss_amount'] = daily_stop
    cfg['max_daily_profit_amount'] = day_cap
    if hard_sl > 0 and pos > 0:
        cfg['enable_per_trade_stop_loss'] = True
        cfg['per_trade_stop_loss_pct'] = size * hard_sl / (pos * day_open)
    else:
        cfg['enable_per_trade_stop_loss'] = False
    if pos <= 0:
        return 0.0, capital, capital, 0.0, 0
    trades, _, _, low, high = combo.unpack_simulate_day(
        simulate_day(g, pc, ALLOWED, pos, cfg, capital))
    pnl = sum(t['pnl'] for t in trades)
    worst_trade = min((t['pnl'] for t in trades), default=0.0)
    return pnl, min(low, capital + pnl), max(high, capital + pnl), worst_trade, len(trades)


def aqua_challenge(i):
    cap = hwm = AQUA_SIZE
    for j in range(i, len(DAYS)):
        pnl, low, high, worst, _ = run_day(j, cap, LEV['aqua'], AQUA_SIZE, hard_sl=AQUA_HARD_SL)
        if worst <= -AQUA_SIZE * AQUA_FLOAT_BREACH:
            return 'fail', j, '单笔浮亏≥1%'
        if low <= hwm * (1 - AQUA_TRAIL):
            return 'fail', j, '5% trailing'
        if high >= AQUA_SIZE * (1 + AQUA_TARGET):
            return 'pass', j, ''
        cap += pnl
        hwm = max(hwm, cap)
    return 'open', len(DAYS) - 1, ''


def aqua_funded_first_payout(i):
    cap = hwm = AQUA_SIZE
    start = DAYS[i][0]
    pnls = []
    stop = AQUA_SIZE * AQUA_DAILY_LOSS * (1 - AQUA_DAILY_BUFFER)
    for j in range(i, len(DAYS)):
        pnl, low, high, worst, _ = run_day(j, cap, LEV['aqua'], AQUA_SIZE, stop, AQUA_DAY_CAP, AQUA_HARD_SL)
        if worst <= -AQUA_SIZE * AQUA_FLOAT_BREACH:
            return 'fail', j, '单笔浮亏≥1%'
        if cap - low >= AQUA_SIZE * AQUA_DAILY_LOSS:
            return 'fail', j, '日亏3%'
        if low <= hwm * (1 - AQUA_TRAIL):
            return 'fail', j, '5% trailing'
        cap += pnl
        hwm = max(hwm, cap)
        pnls.append(pnl)
        net = cap - AQUA_SIZE
        good = sum(1 for p in pnls if p >= AQUA_MIN_DAY_PROFIT)
        held = (DAYS[j][0] - start).days
        if good < AQUA_MIN_GOOD_DAYS or held < AQUA_MIN_CAL_DAYS or net <= 0:
            continue
        if max(pnls) >= AQUA_CONSISTENCY * net:
            continue
        leave = max(AQUA_SIZE, hwm * (1 - AQUA_TRAIL) + AQUA_SAFETY)
        if min(net, cap - leave) >= 100:
            return 'pass', j, ''
    return 'open', len(DAYS) - 1, ''


def ftmo_phase(i, target):
    cap = FTMO_SIZE
    stop = FTMO_SIZE * FTMO_DAILY_LOSS * (1 - FTMO_DAILY_BUFFER)
    for j in range(i, len(DAYS)):
        pnl, low, high, _, _ = run_day(j, cap, LEV['ftmo'], FTMO_SIZE, stop)
        if cap - low >= FTMO_SIZE * FTMO_DAILY_LOSS:
            return 'fail', j, '日亏5%'
        if low <= FTMO_SIZE * (1 - FTMO_MAX_LOSS):
            return 'fail', j, '总亏10%'
        cap += pnl
        hit_tp = high >= FTMO_SIZE * (1 + target + FTMO_TP_BUFFER)
        if hit_tp or cap >= FTMO_SIZE * (1 + target):
            k = max(j, i + FTMO_MIN_DAYS - 1)
            return ('pass', k, '') if k < len(DAYS) else ('open', len(DAYS) - 1, '')
    return 'open', len(DAYS) - 1, ''


def ftmo_funded_first_payout(i):
    cap = FTMO_SIZE
    start = DAYS[i][0]
    stop = FTMO_SIZE * FTMO_DAILY_LOSS * (1 - FTMO_DAILY_BUFFER)
    for j in range(i, len(DAYS)):
        pnl, low, high, _, _ = run_day(j, cap, LEV['ftmo'], FTMO_SIZE, stop)
        if cap - low >= FTMO_SIZE * FTMO_DAILY_LOSS:
            return 'fail', j, '日亏5%'
        if low <= FTMO_SIZE * (1 - FTMO_MAX_LOSS):
            return 'fail', j, '总亏10%'
        cap += pnl
        if (DAYS[j][0] - start).days >= FTMO_PAYOUT_DAYS and cap > FTMO_SIZE:
            return 'pass', j, ''
    return 'open', len(DAYS) - 1, ''


STAGES = {
    'aqua_ch': aqua_challenge,
    'aqua_fd': aqua_funded_first_payout,
    'ftmo_p1': lambda i: ftmo_phase(i, FTMO_P1),
    'ftmo_p2': lambda i: ftmo_phase(i, FTMO_P2),
    'ftmo_fd': ftmo_funded_first_payout,
}


def _worker(job):
    stage, i = job
    return stage, i, STAGES[stage](i)


def run_jobs(pool, jobs):
    out = {}
    for stage, i, res in pool.imap_unordered(_worker, jobs, chunksize=2):
        out[(stage, i)] = res
    return out


def chain(starts, first, second, results):
    """first 通过后次日开 second；返回 [(start, outcome, end_idx, reason, stage_failed)]。"""
    rows = []
    for i in starts:
        st, j, why = results[(first, i)]
        if st != 'pass':
            rows.append((i, st, j, why, first))
            continue
        k = j + 1
        if k >= len(DAYS):
            rows.append((i, 'open', j, '', second))
            continue
        st2, j2, why2 = results[(second, k)]
        rows.append((i, st2, j2, why2, second))
    return rows


def summarize(name, rows):
    done = [r for r in rows if r[1] != 'open']
    n_open = len(rows) - len(done)
    win = [r for r in done if r[1] == 'pass']
    p = len(win) / len(done) if done else 0.0
    cal = sorted((DAYS[r[2]][0] - DAYS[r[0]][0]).days for r in win)
    med = cal[len(cal) // 2] if cal else 0
    fails = {}
    for r in done:
        if r[1] == 'fail':
            key = f'{r[4]}:{r[3]}'
            fails[key] = fails.get(key, 0) + 1
    print(f'\n[{name}] 已结束样本 {len(done)}（未结束 {n_open}）  成功率 {p:.1%}  成功中位耗时 {med} 自然日')
    for k, v in sorted(fails.items(), key=lambda x: -x[1]):
        print(f'    失败 {k}: {v} ({v / len(done):.1%})')
    by_year = {}
    for r in done:
        y = DAYS[r[0]][0].year
        a, b = by_year.get(y, (0, 0))
        by_year[y] = (a + (r[1] == 'pass'), b + 1)
    print('    按开考年份: ' + '  '.join(f'{y}:{a / b:.0%}({b})' for y, (a, b) in sorted(by_year.items())))
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--step', type=int, default=1)
    ap.add_argument('--from', dest='start', default='2018-01-02')
    ap.add_argument('--lev-aqua', type=float, default=2.0)
    ap.add_argument('--lev-ftmo', type=float, default=2.0)
    ap.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = ap.parse_args()
    LEV['aqua'], LEV['ftmo'] = args.lev_aqua, args.lev_ftmo

    t0 = time.time()
    build_merged_csv()
    data_start = pd.Timestamp(args.start).date()
    load_days(data_start)
    print(f'交易日 {len(DAYS)}：{DAYS[0][0]} ~ {DAYS[-1][0]}，加载 {time.time() - t0:.0f}s；'
          f'杠杆 Aqua {LEV["aqua"]}x / FTMO {LEV["ftmo"]}x')

    starts = list(range(0, len(DAYS), args.step))
    ctx = mp.get_context('fork')
    with ctx.Pool(args.workers) as pool:
        res = run_jobs(pool, [(s, i) for i in starts for s in ('aqua_ch', 'ftmo_p1')])
        print(f'第一阶段完成 {time.time() - t0:.0f}s')
        nxt = []
        for i in starts:
            for first, second in (('aqua_ch', 'aqua_fd'), ('ftmo_p1', 'ftmo_p2')):
                st, j, _ = res[(first, i)]
                if st == 'pass' and j + 1 < len(DAYS) and (second, j + 1) not in res:
                    res[(second, j + 1)] = None
                    nxt.append((second, j + 1))
        res.update(run_jobs(pool, nxt))
        print(f'第二阶段完成 {time.time() - t0:.0f}s')
        nxt = []
        for i in starts:
            st, j, _ = res[('ftmo_p1', i)]
            if st != 'pass' or j + 1 >= len(DAYS):
                continue
            st2, j2, _ = res[('ftmo_p2', j + 1)]
            if st2 == 'pass' and j2 + 1 < len(DAYS) and ('ftmo_fd', j2 + 1) not in res:
                res[('ftmo_fd', j2 + 1)] = None
                nxt.append(('ftmo_fd', j2 + 1))
        res.update(run_jobs(pool, nxt))
        print(f'第三阶段完成 {time.time() - t0:.0f}s')

    aqua_ch = [(i, *res[('aqua_ch', i)], 'aqua_ch') for i in starts]
    summarize('Aqua 200K Challenge 通过', aqua_ch)
    aqua = chain(starts, 'aqua_ch', 'aqua_fd', res)
    pa = summarize('Aqua 200K 开考 → 首次可提现', aqua)
    ftmo_p1 = [(i, *res[('ftmo_p1', i)], 'ftmo_p1') for i in starts]
    summarize('FTMO 100K Phase1 通过', ftmo_p1)
    ftmo = chain(starts, 'ftmo_p1', 'ftmo_p2', res)
    pf = summarize('FTMO 100K 开考 → 拿到 funded', ftmo)
    ftmo_pay = []
    for i, st, j, why, stage in ftmo:
        if st != 'pass':
            ftmo_pay.append((i, st, j, why, stage))
        elif j + 1 >= len(DAYS):
            ftmo_pay.append((i, 'open', j, '', 'ftmo_fd'))
        else:
            ftmo_pay.append((i, *res[('ftmo_fd', j + 1)], 'ftmo_fd'))
    pfp = summarize('FTMO 100K 开考 → 首次可提现（参考）', ftmo_pay)
    print(f'\n==== 结论：Aqua 首次可提现 {pa:.1%}  vs  FTMO funded {pf:.1%}（FTMO 首次出金 {pfp:.1%}） ====')


if __name__ == '__main__':
    main()
