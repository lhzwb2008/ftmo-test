#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTMO 10×100K（challenge 2x / funded 1.5x）+ IBKR MNQ 逐日联合回测。

对齐本仓库:
  - 信号/成交: backtest.simulate_day（与 simulate_ftmo / simulate_ibkr 同一套）
  - FTMO 规则: SQLiteSignalEA_ftmo + simulate_ftmo
  - MNQ 张数: 与 simulate_ibkr 一致，日内初始保证金、净值 100%（约 13x）

资金结构:
  10 个 100K 同时买、同时跑，杠杆均为考关 2x / 实盘 1.5x
  10 条路径相同（同信号、同杠杆、同日起），按 1 条模拟后 ×10
  Funded 利润（80%）每 14 日历日转入同一个 IBKR 账户做 MNQ
  爆仓立刻重买同规格 100K，维持 10 个账户

默认窗口（改 WINDOWS 即可）:
  1) 2020-07-01 ~ 2022-08-07  quantra/qqq_market_hours_with_indicators.csv
  2) 2022-07-01 ~ 2024-08-07  同上
  3) 2024-07-01 ~ 2026-08-07  quantra/qqq_longport_2year.csv

用法:
  conda activate quantra
  python ftmo_ibkr_combo_backtest.py
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from math import floor

import matplotlib

matplotlib.use('Agg')

import numpy as np
import pandas as pd

from backtest import (
    apply_k_bounds,
    compute_daily_trend_features,
    compute_entry_trend_pass_series,
    simulate_day,
)

# ---------------------------------------------------------------------------
# FTMO
# ---------------------------------------------------------------------------
N_FTMO_ACCOUNTS = 10
FTMO_ACCOUNT_SIZE = 100_000.0
CHALLENGE_FEE = 540.0  # 2-Step 100K 标价 €540；按 1:1 记美元
LEV_CHALLENGE = 2.0
LEV_FUNDED = 1.5
PROFIT_SPLIT = 0.80
PAYOUT_MIN_CALENDAR_DAYS = 14
P1_TARGET = 0.10
P2_TARGET = 0.05
MIN_TRADING_DAYS = 4
MAX_TOTAL_LOSS = 0.10
MAX_DAILY_LOSS = 0.05
EA_DAILY_LOSS_BUFFER = 0.05

# ---------------------------------------------------------------------------
# MNQ / IBKR（与 simulate_ibkr.py 常量一致）
# ---------------------------------------------------------------------------
NQ_QQQ_RATIO = 41.45
MNQ_POINT_VALUE = 2.0
MARGIN_USAGE_PCT = 1.0
MNQ_INTRADAY_IM_PCT = 0.0771
MNQ_COMMISSION_RT = 1.24
MNQ_SLIPPAGE_RT = 1.00

_REF_MNQ_PX = 29_800.0
IBKR_INTRADAY_IM_PCT = MNQ_INTRADAY_IM_PCT
IBKR_INTRADAY_MM_PCT = 2904.80 / (_REF_MNQ_PX * MNQ_POINT_VALUE)

QUANTRA_DIR = '/Users/Wezhang/workspace/quantra'
HIST_DATA = os.path.join(QUANTRA_DIR, 'qqq_market_hours_with_indicators.csv')
LONGPORT_2Y = os.path.join(QUANTRA_DIR, 'qqq_longport_2year.csv')
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reports', 'ftmo_ibkr_combo')

# 与 2024-07-01 ~ 2026-08-07 对齐的两段「两年」窗口 + 原窗口
WINDOWS = [
    {
        'key': '2020_2022',
        'label': '2020-07 ~ 2022-08',
        'data_path': HIST_DATA,
        'start_date': date(2020, 7, 1),
        'end_date': date(2022, 8, 7),
    },
    {
        'key': '2022_2024',
        'label': '2022-07 ~ 2024-08',
        'data_path': HIST_DATA,
        'start_date': date(2022, 7, 1),
        'end_date': date(2024, 8, 7),
    },
    {
        'key': '2024_2026',
        'label': '2024-07 ~ 2026-08',
        'data_path': LONGPORT_2Y,
        'start_date': date(2024, 7, 1),
        'end_date': date(2026, 8, 7),
    },
]


def strategy_config(window):
    return {
        'data_path': window['data_path'],
        'ticker': 'QQQ',
        'lookback_days': 1,
        'start_date': window['start_date'],
        'end_date': window['end_date'],
        'check_interval_minutes': 15,
        'enable_transaction_fees': True,
        'transaction_fee_per_share': 0.008166,
        'min_round_trip_fee': 2.16,
        'slippage_per_share': 0.01,
        'trading_start_time': (9, 40),
        'trading_end_time': (15, 40),
        'max_positions_per_day': 10,
        'print_daily_trades': False,
        'print_trade_details': False,
        'K1': 1,
        'K2': 1.04,
        'enable_k_side_adjustment': True,
        'k_side_adjustment': {
            'long': {
                'metric': 'minutes_from_open',
                'min': 120,
                'k_if_true': 0.9,
                'k_if_false': 1.0,
            },
        },
        'enable_trailing_take_profit': True,
        'trailing_tp_activation_pct': 0.006,
        'trailing_tp_callback_pct': 0.65,
        'entry_trend_filter': [
            {'metric': 'er5', 'min': 0.1},
            {'metric': 'range1', 'max': 0.029},
            {'metric': 'sigma', 'min': 0.0003},
        ],
        'enable_per_trade_stop_loss': False,
        'intraday_stop_loss_mode': 'from_day_start',
        'use_vwap': False,
    }


def prepare_strategy_data(config):
    data_path = config['data_path']
    lookback_days = config.get('lookback_days', 1)
    start_date = config.get('start_date')
    end_date = config.get('end_date')
    trading_start_time = config.get('trading_start_time', (9, 40))
    trading_end_time = config.get('trading_end_time', (15, 40))
    check_interval_minutes = config.get('check_interval_minutes', 15)

    price_df = pd.read_csv(data_path, parse_dates=['DateTime'])
    price_df.sort_values('DateTime', inplace=True)
    price_df['Date'] = price_df['DateTime'].dt.date
    price_df['Time'] = price_df['DateTime'].dt.strftime('%H:%M')

    trend_feat_df = compute_daily_trend_features(price_df)
    if start_date is not None:
        price_df = price_df[price_df['Date'] >= start_date]
    if end_date is not None:
        price_df = price_df[price_df['Date'] <= end_date]
    price_df = pd.merge(price_df, trend_feat_df, on='Date', how='left')

    if 'DayOpen' not in price_df.columns or 'DayClose' not in price_df.columns:
        opening_prices = (
            price_df.groupby('Date').first().reset_index()[['Date', 'Open']]
            .rename(columns={'Open': 'DayOpen'})
        )
        closing_prices = (
            price_df.groupby('Date').last().reset_index()[['Date', 'Close']]
            .rename(columns={'Close': 'DayClose'})
        )
        price_df = pd.merge(price_df, opening_prices, on='Date', how='left')
        price_df = pd.merge(price_df, closing_prices, on='Date', how='left')
    else:
        # 历史文件已有 DayOpen/DayClose，按窗口内重算，避免用到窗口外的日开盘
        opening_prices = (
            price_df.groupby('Date').first().reset_index()[['Date', 'Open']]
            .rename(columns={'Open': 'DayOpen'})
        )
        closing_prices = (
            price_df.groupby('Date').last().reset_index()[['Date', 'Close']]
            .rename(columns={'Close': 'DayClose'})
        )
        price_df = price_df.drop(columns=['DayOpen', 'DayClose'], errors='ignore')
        price_df = pd.merge(price_df, opening_prices, on='Date', how='left')
        price_df = pd.merge(price_df, closing_prices, on='Date', how='left')

    price_df['prev_close'] = price_df.groupby('Date')['DayClose'].transform('first').shift(1)
    price_df['day_open'] = price_df.groupby('Date')['DayOpen'].transform('first')

    date_refs = []
    for d in price_df['Date'].unique():
        row = price_df[price_df['Date'] == d].iloc[0]
        day_open = row['day_open']
        prev_close = row['prev_close']
        if not pd.isna(prev_close):
            upper_ref = max(day_open, prev_close)
            lower_ref = min(day_open, prev_close)
        else:
            upper_ref = day_open
            lower_ref = day_open
        date_refs.append({'Date': d, 'upper_ref': upper_ref, 'lower_ref': lower_ref})
    price_df = price_df.drop(columns=['upper_ref', 'lower_ref'], errors='ignore')
    price_df = pd.merge(price_df, pd.DataFrame(date_refs), on='Date', how='left')
    price_df['ret'] = price_df['Close'] / price_df['day_open'] - 1

    pivot = price_df.pivot(index='Date', columns='Time', values='ret').abs()
    sigma = pivot.rolling(window=lookback_days, min_periods=lookback_days).mean().shift(1)
    sigma = sigma.stack().reset_index(name='sigma')
    price_df = pd.merge(price_df, sigma, on=['Date', 'Time'], how='left')

    incomplete = set()
    for d in price_df['Date'].unique():
        dd = price_df[price_df['Date'] == d]
        if len(dd) and dd['sigma'].isna().mean() > 0.1:
            incomplete.add(d)
    price_df = price_df[~price_df['Date'].isin(incomplete)]
    price_df['sigma'] = price_df.groupby('Date')['sigma'].ffill()
    price_df['sigma'] = price_df.groupby('Date')['sigma'].bfill()
    price_df['sigma'] = price_df['sigma'].fillna(0)

    price_df['intraday_ret'] = price_df['Close'] / price_df['day_open'] - 1
    day_start_dt = price_df.groupby('Date')['DateTime'].transform('min')
    price_df['minutes_from_open'] = (price_df['DateTime'] - day_start_dt).dt.total_seconds() / 60
    price_df['cum_high'] = price_df.groupby('Date')['High'].cummax()
    price_df['cum_low'] = price_df.groupby('Date')['Low'].cummin()
    span = price_df['cum_high'] - price_df['cum_low']
    price_df['intraday_range_pos'] = (
        (price_df['Close'] - price_df['cum_low']) / span.replace(0, np.nan)
    ).clip(0, 1)
    day_sigma_med = price_df.groupby('Date')['sigma'].transform('median')
    price_df['sigma_vs_day_median'] = price_df['sigma'] / day_sigma_med.replace(0, np.nan)

    price_df = apply_k_bounds(price_df, config)
    price_df['entry_trend_pass'] = compute_entry_trend_pass_series(price_df, config)

    allowed_times = []
    h, m = trading_start_time
    end_h, end_m = trading_end_time
    while h < end_h or (h == end_h and m <= end_m):
        allowed_times.append(f'{h:02d}:{m:02d}')
        m += check_interval_minutes
        if m >= 60:
            h += m // 60
            m = m % 60
    end_str = f'{end_h:02d}:{end_m:02d}'
    if end_str not in allowed_times:
        allowed_times.append(end_str)
        allowed_times.sort()

    return price_df, allowed_times, sorted(price_df['Date'].unique())


def unpack_simulate_day(result):
    trades = result[0]
    mdd = float(result[1])
    loss_from_start, low, high = float(result[2]), float(result[3]), float(result[4])
    return trades, mdd, loss_from_start, low, high


def qqq_position_size(capital, leverage, day_open):
    if capital <= 0 or leverage <= 0 or day_open <= 0:
        return 0
    return int(floor(capital * leverage / day_open))


def mnq_notional_1(qqq_px):
    return float(qqq_px) * NQ_QQQ_RATIO * MNQ_POINT_VALUE


def mnq_contracts(equity, qqq_px, usage_pct):
    if equity <= 0 or qqq_px <= 0:
        return 0, 0.0, 0.0, 0.0
    notional_1 = mnq_notional_1(qqq_px)
    margin_1 = notional_1 * IBKR_INTRADAY_IM_PCT
    mm_1 = notional_1 * IBKR_INTRADAY_MM_PCT
    qty = int(floor(equity * usage_pct / margin_1))
    notional = qty * notional_1
    lev = (notional / equity) if equity > 0 else 0.0
    return qty, notional, lev, mm_1


@dataclass
class FtmoAccount:
    name: str
    size: float
    lev_challenge: float
    lev_funded: float
    phase: str = 'p1'
    capital: float = 0.0
    funded_start: date | None = None
    last_payout_date: date | None = None
    refund_pending: bool = False
    target_hit: bool = False
    phase_trade_days: int = 0
    n_challenges_bought: int = 0
    n_p1_pass: int = 0
    n_p2_pass: int = 0
    n_fail_p1: int = 0
    n_fail_p2: int = 0
    n_fail_funded: int = 0
    n_payouts: int = 0
    payout_to_ibkr: float = 0.0
    refund_to_ibkr: float = 0.0
    fees_paid: float = 0.0
    events: list = field(default_factory=list)

    def __post_init__(self):
        self.buy_new_challenge(None)

    def buy_new_challenge(self, trade_date):
        self.phase = 'p1'
        self.capital = self.size
        self.funded_start = None
        self.last_payout_date = None
        self.refund_pending = False
        self.target_hit = False
        self.phase_trade_days = 0
        self.n_challenges_bought += 1
        self.fees_paid += CHALLENGE_FEE
        if trade_date is not None:
            self.events.append((trade_date, 'buy_challenge', f'fee ${CHALLENGE_FEE:.0f}'))

    @property
    def leverage(self):
        return self.lev_funded if self.phase == 'funded' else self.lev_challenge

    def accrued_trader_share(self):
        if self.phase != 'funded':
            return 0.0
        return max(0.0, self.capital - self.size) * PROFIT_SPLIT


def simulate_ftmo_day(acct: FtmoAccount, day_data, prev_close, allowed_times, base_cfg):
    official_daily = acct.size * MAX_DAILY_LOSS
    ea_stop = official_daily * (1.0 - EA_DAILY_LOSS_BUFFER)
    floor_eq = acct.size * (1.0 - MAX_TOTAL_LOSS)
    day_open = float(day_data['day_open'].iloc[0])
    pos = qqq_position_size(acct.capital, acct.leverage, day_open)

    cfg = base_cfg.copy()
    cfg['initial_capital'] = acct.size
    cfg['enable_intraday_stop_loss'] = True
    cfg['intraday_stop_loss_mode'] = 'from_day_start'
    cfg['max_daily_loss_amount'] = ea_stop
    cfg['enable_transaction_fees'] = True
    cfg['transaction_fee_per_share'] = 0.008166
    cfg['slippage_per_share'] = 0.01

    if pos <= 0:
        return 0.0, False, None, 0

    result = simulate_day(day_data, prev_close, allowed_times, pos, cfg, acct.capital)
    trades, mdd, loss_from_start, low, high = unpack_simulate_day(result)
    day_pnl = sum(t['pnl'] for t in trades)
    eod = acct.capital + day_pnl
    lowest = min(low, eod)
    loss_usd = max(0.0, acct.capital - lowest)

    failed = False
    reason = None
    if loss_usd >= official_daily - 1e-6:
        failed, reason = True, f'日亏 ${loss_usd:,.0f} >= ${official_daily:,.0f}'
    elif lowest <= floor_eq + 1e-9:
        failed, reason = True, f'总亏损底 ${lowest:,.0f} <= ${floor_eq:,.0f}'
    return day_pnl, failed, reason, len(trades)


def maybe_pass_phase(acct: FtmoAccount, trade_date):
    if acct.phase == 'funded' or acct.phase_trade_days < MIN_TRADING_DAYS:
        return False
    if acct.phase == 'p1' and acct.capital >= acct.size * (1 + P1_TARGET):
        acct.n_p1_pass += 1
        acct.phase = 'p2'
        acct.capital = acct.size
        acct.target_hit = False
        acct.phase_trade_days = 0
        acct.events.append((trade_date, 'p1_pass', 'reset to p2 @ size'))
        return True
    if acct.phase == 'p2' and acct.capital >= acct.size * (1 + P2_TARGET):
        acct.n_p2_pass += 1
        acct.phase = 'funded'
        acct.capital = acct.size
        acct.funded_start = trade_date
        acct.last_payout_date = trade_date
        acct.refund_pending = True
        acct.target_hit = False
        acct.phase_trade_days = 0
        acct.events.append((trade_date, 'funded', 'start funded'))
        return True
    return False


def maybe_payout(acct: FtmoAccount, trade_date):
    if acct.phase != 'funded' or acct.last_payout_date is None:
        return 0.0, 0.0
    if (trade_date - acct.last_payout_date).days < PAYOUT_MIN_CALENDAR_DAYS:
        return 0.0, 0.0
    profit = acct.capital - acct.size
    if profit <= 0:
        return 0.0, 0.0
    trader = profit * PROFIT_SPLIT
    refund = 0.0
    if acct.refund_pending:
        refund = CHALLENGE_FEE
        acct.refund_pending = False
        acct.refund_to_ibkr += refund
    acct.payout_to_ibkr += trader
    acct.n_payouts += 1
    acct.capital = acct.size
    acct.last_payout_date = trade_date
    acct.events.append((trade_date, 'payout', f'trader ${trader:,.0f} refund ${refund:,.0f}'))
    return trader, refund


def apply_ftmo_eod(acct: FtmoAccount, day_pnl, failed, reason, n_trades, trade_date):
    if failed:
        if acct.phase == 'p1':
            acct.n_fail_p1 += 1
        elif acct.phase == 'p2':
            acct.n_fail_p2 += 1
        else:
            acct.n_fail_funded += 1
        acct.events.append((trade_date, 'fail', f'{acct.phase} {reason}'))
        return True

    acct.capital += day_pnl
    if n_trades > 0:
        acct.phase_trade_days += 1
    maybe_pass_phase(acct, trade_date)
    return False


def simulate_ibkr_day(equity, day_data, prev_close, allowed_times, base_cfg, usage_pct, daily_stop_pct):
    if equity <= 0:
        return {'pnl': 0.0, 'qty': 0, 'lev': 0.0, 'note': 'no_capital', 'cost': 0.0}

    qqq_px = float(day_data['day_open'].iloc[0])
    qty, notional, lev, mm_1 = mnq_contracts(equity, qqq_px, usage_pct)
    if qty < 1:
        return {'pnl': 0.0, 'qty': 0, 'lev': lev, 'note': 'below_1_mnq', 'cost': 0.0}

    shares = int(round(qty * NQ_QQQ_RATIO * MNQ_POINT_VALUE))
    if shares <= 0:
        return {'pnl': 0.0, 'qty': qty, 'lev': lev, 'note': 'shares_0', 'cost': 0.0}

    cfg = base_cfg.copy()
    cfg['initial_capital'] = equity
    cfg['enable_transaction_fees'] = False
    cfg['slippage_per_share'] = 0.0
    if daily_stop_pct and daily_stop_pct > 0:
        cfg['enable_intraday_stop_loss'] = True
        cfg['intraday_stop_loss_mode'] = 'from_day_start'
        cfg['max_daily_loss_amount'] = equity * daily_stop_pct
    else:
        cfg['enable_intraday_stop_loss'] = False
        cfg['max_daily_loss_amount'] = 0

    result = simulate_day(day_data, prev_close, allowed_times, shares, cfg, equity)
    trades, mdd, loss_from_start, low, high = unpack_simulate_day(result)
    cost = len(trades) * qty * (MNQ_COMMISSION_RT + MNQ_SLIPPAGE_RT)
    raw_pnl = sum(t['pnl'] for t in trades)
    eod_equity = equity + raw_pnl - cost
    low_eq = min(low, eod_equity)

    mm_total = qty * mm_1
    note = 'ok'
    if low_eq <= 0:
        day_pnl = -equity
        note = 'wipe'
    elif low_eq < mm_total:
        day_pnl = max(-equity, mm_total - equity)
        note = 'margin_call'
    else:
        day_pnl = raw_pnl - cost
        if daily_stop_pct and loss_from_start >= daily_stop_pct - 1e-9:
            note = 'daily_stop'

    return {'pnl': day_pnl, 'qty': qty, 'lev': lev, 'note': note, 'cost': cost}


def run_ftmo_path(price_df, allowed_times, dates, cfg):
    by_date = {d: g.copy() for d, g in price_df.groupby('Date')}
    acct = FtmoAccount('F100K_2x/1.5x', FTMO_ACCOUNT_SIZE, LEV_CHALLENGE, LEV_FUNDED)
    n_mult = N_FTMO_ACCOUNTS
    rows = []
    n = len(dates)
    for i, trade_date in enumerate(dates):
        day_data = by_date[trade_date].sort_values('DateTime').reset_index(drop=True)
        if len(day_data) < 10:
            continue
        prev_close = day_data['prev_close'].iloc[0]
        if pd.isna(prev_close):
            continue
        prev_close = float(prev_close)

        phase_sod = acct.phase
        lev_sod = acct.leverage
        day_pnl, failed, reason, n_trades = simulate_ftmo_day(
            acct, day_data.copy(), prev_close, allowed_times, cfg
        )
        blew = apply_ftmo_eod(acct, day_pnl, failed, reason, n_trades, trade_date)
        payout = refund = 0.0
        if blew:
            acct.buy_new_challenge(trade_date)
        else:
            payout, refund = maybe_payout(acct, trade_date)

        one_pnl = 0.0 if blew else day_pnl
        funded_pnl = one_pnl if phase_sod == 'funded' else 0.0
        phase_eod = acct.phase
        rows.append({
            'Date': trade_date,
            'phase': phase_sod,
            'phase_eod': phase_eod,
            'lev': lev_sod,
            'one_pnl': one_pnl,
            'all_pnl': one_pnl * n_mult,
            'funded_pnl': funded_pnl * n_mult,
            'one_equity': acct.capital,
            'failed': blew,
            'n_failed': n_mult if blew else 0,
            'passed_p1': phase_eod in ('p2', 'funded'),
            'is_funded': phase_eod == 'funded',
            'payout_to_ibkr': payout * n_mult,
            'refund_to_ibkr': refund * n_mult,
            'accrued': acct.accrued_trader_share() * n_mult,
            'fees_cum': acct.fees_paid * n_mult,
        })
        if (i + 1) % 80 == 0 or (i + 1) == n:
            print(
                f'  FTMO {i+1}/{n} {trade_date} | {acct.phase} 单户 ${acct.capital:,.0f} | '
                f'出金累计 ${acct.payout_to_ibkr * n_mult:,.0f} ({n_mult}户)',
                flush=True,
            )
    return pd.DataFrame(rows), acct


def run_ibkr_on_payouts(ftmo_daily, price_df, allowed_times, cfg, usage_pct=1.0):
    by_date = {d: g.copy() for d, g in price_df.groupby('Date')}
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    realized = 0.0
    fees = 0.0
    n_trade = n_skip = n_call = n_wipe = n_stop = 0
    first_trade = None
    lev_list = []
    qty_list = []
    rows = []

    dates = list(ftmo_daily['Date'])
    n = len(dates)
    for i, trade_date in enumerate(dates):
        day_data = by_date[trade_date].sort_values('DateTime').reset_index(drop=True)
        prev_close = float(day_data['prev_close'].iloc[0])
        res = simulate_ibkr_day(
            equity, day_data.copy(), prev_close, allowed_times, cfg,
            usage_pct, None,
        )
        pnl = float(res['pnl'])
        if res['qty'] >= 1:
            n_trade += 1
            if first_trade is None:
                first_trade = trade_date
            lev_list.append(res['lev'])
            qty_list.append(res['qty'])
        else:
            n_skip += 1
        if res['note'] == 'margin_call':
            n_call += 1
        elif res['note'] == 'wipe':
            n_wipe += 1
        elif res['note'] == 'daily_stop':
            n_stop += 1
        realized += pnl
        fees += res['cost']
        equity = max(0.0, equity + pnl)

        ftmo_row = ftmo_daily.iloc[i]
        equity += float(ftmo_row['payout_to_ibkr'] + ftmo_row['refund_to_ibkr'])
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

        accrued = float(ftmo_row['accrued'])
        fees_cum = float(ftmo_row['fees_cum'])
        rows.append({
            'Date': trade_date,
            'ibkr_pnl': pnl,
            'ibkr_qty': res['qty'],
            'ibkr_lev': res['lev'],
            'ibkr_note': res['note'],
            'ibkr_equity': equity,
            'payout_to_ibkr': float(ftmo_row['payout_to_ibkr']),
            'refund_to_ibkr': float(ftmo_row['refund_to_ibkr']),
            'accrued': accrued,
            'fees_cum': fees_cum,
            'net_wealth': equity + accrued - fees_cum,
            'phase': ftmo_row['phase'],
            'phase_eod': ftmo_row['phase_eod'],
            'passed_p1': bool(ftmo_row['passed_p1']),
            'is_funded': bool(ftmo_row['is_funded']),
            'one_equity': float(ftmo_row['one_equity']),
            'funded_pnl': float(ftmo_row['funded_pnl']),
            'failed': bool(ftmo_row['failed']),
        })
        if (i + 1) % 80 == 0 or (i + 1) == n:
            print(
                f'  IBKR {i+1}/{n} {trade_date} | ${equity:,.0f} | '
                f'{res["note"]} qty={res["qty"]} lev={res["lev"]:.1f}x',
                flush=True,
            )

    daily = pd.DataFrame(rows)
    lev_arr = np.array(lev_list) if lev_list else np.array([0.0])
    qty_arr = np.array(qty_list) if qty_list else np.array([0])
    stats = {
        'ibkr_equity': round(equity, 2),
        'ibkr_realized_pnl': round(realized, 2),
        'ibkr_fees_slip': round(fees, 2),
        'ibkr_trade_days': n_trade,
        'ibkr_skip_days': n_skip,
        'ibkr_margin_call': n_call,
        'ibkr_wipe': n_wipe,
        'ibkr_daily_stop': n_stop,
        'ibkr_max_dd_pct': round(max_dd * 100, 2),
        'ibkr_peak': round(peak, 2),
        'ibkr_first_trade': str(first_trade) if first_trade else None,
        'ibkr_lev_median': round(float(np.median(lev_arr)), 2),
        'ibkr_lev_mean': round(float(np.mean(lev_arr)), 2),
        'ibkr_qty_median': round(float(np.median(qty_arr)), 1),
        'net_profit': round(float(daily['net_wealth'].iloc[-1]), 2) if len(daily) else 0.0,
    }
    return daily, stats


def account_summary(acct: FtmoAccount):
    return {
        'challenges_bought': acct.n_challenges_bought,
        'p1_pass': acct.n_p1_pass,
        'p2_pass': acct.n_p2_pass,
        'fail_p1': acct.n_fail_p1,
        'fail_p2': acct.n_fail_p2,
        'fail_funded': acct.n_fail_funded,
        'payouts': acct.n_payouts,
        'payout_to_ibkr': round(acct.payout_to_ibkr, 2),
        'refund_to_ibkr': round(acct.refund_to_ibkr, 2),
        'fees_paid': round(acct.fees_paid, 2),
        'final_phase': acct.phase,
        'final_equity': round(acct.capital, 2),
        'events': [
            {'date': str(d), 'type': t, 'detail': det}
            for d, t, det in acct.events
        ],
    }


def monthly_from_daily(daily):
    d = daily.copy()
    d['month'] = pd.to_datetime(d['Date']).dt.to_period('M').astype(str)
    g = d.groupby('month', as_index=False).agg(
        net_wealth=('net_wealth', 'last'),
        ibkr=('ibkr_equity', 'last'),
        ibkr_pnl=('ibkr_pnl', 'sum'),
        funded_pnl=('funded_pnl', 'sum'),
        payout=('payout_to_ibkr', 'sum'),
        refund=('refund_to_ibkr', 'sum'),
        accrued=('accrued', 'last'),
        one_equity=('one_equity', 'last'),
        phase_start=('phase', 'first'),
        phase=('phase_eod', 'last'),
        passed_p1=('passed_p1', 'last'),
        is_funded=('is_funded', 'last'),
        n_fail=('failed', 'sum'),
    )
    g['ibkr_pnl_cum'] = g['ibkr_pnl'].cumsum()
    g['funded_pnl_cum'] = g['funded_pnl'].cumsum()
    g['payout_cum'] = g['payout'].cumsum()
    g['ftmo_equity_all'] = g['one_equity'] * N_FTMO_ACCOUNTS
    return g


def event_date(acct: FtmoAccount, etype):
    for d, t, _det in acct.events:
        if t == etype:
            return str(d)
    return None


def print_monthly(monthly):
    print(
        f"{'月份':<10}{'过P1':<6}{'Funded':<8}{'阶段':<8}"
        f"{'Funded当月':>12}{'Funded累计':>12}{'应计未出':>12}"
        f"{'出金累计':>12}{'IBKR账户':>14}{'月末净值':>14}"
    )
    for r in monthly.itertuples(index=False):
        print(
            f"{r.month:<10}"
            f"{'是' if r.passed_p1 else '否':<6}"
            f"{'是' if r.is_funded else '否':<8}"
            f"{r.phase:<8}"
            f"${r.funded_pnl:>10,.0f}"
            f"${r.funded_pnl_cum:>10,.0f}"
            f"${r.accrued:>10,.0f}"
            f"${r.payout_cum:>10,.0f}"
            f"${r.ibkr:>12,.0f}"
            f"${r.net_wealth:>12,.0f}"
        )


def run_window(window):
    cfg = strategy_config(window)
    start_fees = N_FTMO_ACCOUNTS * CHALLENGE_FEE
    print('\n' + '=' * 72)
    print(f"窗口 {window['label']}  |  {os.path.basename(window['data_path'])}")
    print('=' * 72)
    print(f"数据: {cfg['data_path']}  {cfg['start_date']} ~ {cfg['end_date']}")
    print('预处理...')
    price_df, allowed_times, dates = prepare_strategy_data(cfg)
    if not dates:
        print('  无有效交易日，跳过')
        return None
    print(f'有效交易日: {len(dates)} ({dates[0]} ~ {dates[-1]})')

    print(f'\n--- FTMO {N_FTMO_ACCOUNTS}×100K  2x/1.5x ---')
    ftmo_daily, acct = run_ftmo_path(price_df, allowed_times, dates, cfg)
    if ftmo_daily.empty:
        print('  FTMO 日表为空，跳过')
        return None

    out_dir = os.path.join(OUTPUT_DIR, window['key'])
    os.makedirs(out_dir, exist_ok=True)
    ftmo_daily.to_csv(os.path.join(out_dir, 'ftmo_daily.csv'), index=False)

    fees = acct.fees_paid * N_FTMO_ACCOUNTS
    payouts = acct.payout_to_ibkr * N_FTMO_ACCOUNTS
    refunds = acct.refund_to_ibkr * N_FTMO_ACCOUNTS
    accrued = acct.accrued_trader_share() * N_FTMO_ACCOUNTS

    print('\nFTMO（金额已 ×10）:')
    print(
        f"  报名 {acct.n_challenges_bought}次/户 | "
        f"P1 {acct.n_p1_pass}/{acct.n_fail_p1} | P2 {acct.n_p2_pass}/{acct.n_fail_p2} | "
        f"funded爆 {acct.n_fail_funded} | 出金 {acct.n_payouts}次/户 "
        f"合计 ${payouts:,.0f} | 期末 {acct.phase} 单户 ${acct.capital:,.0f}"
    )
    print(
        f"  P1过关 {event_date(acct, 'p1_pass') or '未过'} | "
        f"Funded {event_date(acct, 'funded') or '未过'} | "
        f"报名费 ${fees:,.0f} | 出金 ${payouts:,.0f} | 退费 ${refunds:,.0f}"
    )

    print('\n--- IBKR 日内 100% 净值 ~13x ---')
    daily, stats = run_ibkr_on_payouts(ftmo_daily, price_df, allowed_times, cfg, MARGIN_USAGE_PCT)
    monthly = monthly_from_daily(daily)
    daily.to_csv(os.path.join(out_dir, 'daily.csv'), index=False)
    monthly.to_csv(os.path.join(out_dir, 'monthly.csv'), index=False)

    print(
        f"  期末 IBKR ${stats['ibkr_equity']:,.2f} | 净利 ${stats['net_profit']:,.2f} | "
        f"杠杆中位 {stats['ibkr_lev_median']:.1f}x | "
        f"强平 {stats['ibkr_margin_call']} / 打穿 {stats['ibkr_wipe']} | "
        f"MDD {stats['ibkr_max_dd_pct']:.1f}%"
    )
    print('\n按月账户情况（10 户合计）')
    print_monthly(monthly)

    cal_days = (dates[-1] - dates[0]).days
    summary = {
        'key': window['key'],
        'label': window['label'],
        'data_path': window['data_path'],
        'start': str(ftmo_daily['Date'].iloc[0]),
        'end': str(ftmo_daily['Date'].iloc[-1]),
        'trading_days': int(len(ftmo_daily)),
        'calendar_days': int(cal_days),
        'p1_date': event_date(acct, 'p1_pass'),
        'funded_date': event_date(acct, 'funded'),
        'challenge_fees_paid': round(fees, 2),
        'payouts_to_ibkr': round(payouts, 2),
        'refunds_to_ibkr': round(refunds, 2),
        'accrued_unpaid': round(accrued, 2),
        'account': account_summary(acct),
        'ibkr': stats,
        'monthly': [
            {
                'month': r.month,
                'phase': r.phase,
                'passed_p1': bool(r.passed_p1),
                'is_funded': bool(r.is_funded),
                'funded_pnl': round(float(r.funded_pnl), 2),
                'funded_pnl_cum': round(float(r.funded_pnl_cum), 2),
                'accrued': round(float(r.accrued), 2),
                'payout_cum': round(float(r.payout_cum), 2),
                'ibkr': round(float(r.ibkr), 2),
                'net_wealth': round(float(r.net_wealth), 2),
                'n_fail': int(r.n_fail),
            }
            for r in monthly.itertuples(index=False)
        ],
    }
    with open(os.path.join(out_dir, 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary


def run_combo():
    print('FTMO 10×100K 2x/1.5x + IBKR MNQ  多窗口回测')
    print(f'IBKR 日内 IM {IBKR_INTRADAY_IM_PCT*100:.2f}%  usage 100% → 约 {1.0/IBKR_INTRADAY_IM_PCT:.1f}x')
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    results = []
    for window in WINDOWS:
        summary = run_window(window)
        if summary:
            results.append(summary)

    print('\n' + '=' * 72)
    print('三段对比（净利润 = IBKR 期末 + 应计未出 − 报名费）')
    print('=' * 72)
    print(
        f"{'窗口':<22}{'交易日':>6}{'P1':>12}{'Funded':>12}"
        f"{'出金':>12}{'IBKR':>14}{'净利':>14}{'MDD':>8}{'爆仓':>6}"
    )
    for s in results:
        fails = s['account']['fail_p1'] + s['account']['fail_p2'] + s['account']['fail_funded']
        print(
            f"{s['label']:<22}{s['trading_days']:>6}"
            f"{(s['p1_date'] or '-'):>12}{(s['funded_date'] or '-'):>12}"
            f"${s['payouts_to_ibkr']:>10,.0f}"
            f"${s['ibkr']['ibkr_equity']:>12,.0f}"
            f"${s['ibkr']['net_profit']:>12,.0f}"
            f"{s['ibkr']['ibkr_max_dd_pct']:>7.1f}%"
            f"{fails:>6}"
        )
    combo_path = os.path.join(OUTPUT_DIR, 'windows_summary.json')
    with open(combo_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f'\n明细: {OUTPUT_DIR}')
    return results


if __name__ == '__main__':
    run_combo()
