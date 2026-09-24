#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aqua Pay Later $200k funded：最近样本上的出金 / 爆仓 / 杠杆对照。

规则对齐 simulate_aqua_paylater.py 与 SQLiteSignalEA_aqua_paylater.mq5，
以及 2026-08 之后的 Pay After Pass 帮助中心：
- 单笔硬止损 = 初始资金 × 0.95%（-1% 浮亏封号留缓冲）
- 日亏 = 初始资金 × 3%，EA 再提前 5% 缓冲（实际触发 2.85%）
- 最大回撤 5% trailing，高点只升不降；出金不把高点拉下来
- 日盈帽默认按 150k 的 $1100 同比放到 200k（$1467）
- 出金：满 14 个日历日、至少 5 个 ≥0.5% 的盈利日、最好一天 < 周期净利润的 15%
- 分润 90%，再扣 3% 出金手续费；前两次提款请求最多 $10,000
- 提款后余额必须留在 trailing 地板之上，再加 1% 初始资金的缓冲
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime

import backtest


INITIAL = 200_000.0
ACTIVATION_FEE = 597.93
SPLIT = 0.90
PAYOUT_PROCESS_FEE = 0.03
HARD_SL_PCT = 0.0095
DAILY_LOSS_PCT = 0.03 * (1.0 - 0.05)  # EA DailyLossBufferPercent = 5
TRAIL_PCT = 0.05
CONSISTENCY = 0.15
MIN_DAY_PROFIT = INITIAL * 0.005
SAFETY_BUFFER = INITIAL * 0.01
FIRST_PAYOUT_CAP = 10_000.0
MIN_PROFITABLE_DAYS = 5
MIN_CALENDAR_DAYS = 14
# 150k 脚本默认 $1100，200k 同比
DAY_CAP_200K = 1100.0 * (INITIAL / 150_000.0)


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


@dataclass
class Account:
    leverage: float
    day_cap: float
    balance: float = INITIAL
    hwm: float = INITIAL
    cash: float = 0.0
    payouts: list = field(default_factory=list)
    busted: bool = False
    bust_date: date | None = None
    cycle_start: date | None = None
    cycle_base: float = INITIAL
    cycle_pnls: list = field(default_factory=list)
    blocked_consistency_days: int = 0
    end_balance: float = INITIAL
    end_date: date | None = None

    def on_day_close(self, trade_date, trades, day_pnl, capital, intraday_low, intraday_high, capital_start):
        trade_date = _as_date(trade_date)
        self.end_date = trade_date
        if self.cycle_start is None:
            self.cycle_start = trade_date
            self.cycle_base = capital_start
        self.balance = float(capital)
        self.cycle_pnls.append(float(day_pnl))

        floor = self.hwm * (1.0 - TRAIL_PCT)
        if intraday_low <= floor or self.balance <= floor:
            self.busted = True
            self.bust_date = trade_date
            self.end_balance = min(self.balance, floor)
            self.balance = self.end_balance
            return self.balance

        if self.balance > self.hwm:
            self.hwm = self.balance

        self._maybe_payout(trade_date)
        self.end_balance = self.balance
        return self.balance

    def _maybe_payout(self, trade_date: date):
        net = self.balance - self.cycle_base
        best = max(self.cycle_pnls) if self.cycle_pnls else 0.0
        good_days = sum(1 for pnl in self.cycle_pnls if pnl >= MIN_DAY_PROFIT)
        held = (trade_date - self.cycle_start).days
        if good_days < MIN_PROFITABLE_DAYS or held < MIN_CALENDAR_DAYS or net <= 0:
            return
        if best >= CONSISTENCY * net:
            self.blocked_consistency_days += 1
            return
        floor = self.hwm * (1.0 - TRAIL_PCT)
        leave = max(INITIAL, floor + SAFETY_BUFFER)
        room = self.balance - leave
        profit_above_start = self.balance - INITIAL
        request = min(profit_above_start, room)
        if len(self.payouts) < 2:
            request = min(request, FIRST_PAYOUT_CAP)
        if request < 100:
            return
        self.balance -= request
        received = request * SPLIT * (1.0 - PAYOUT_PROCESS_FEE)
        self.cash += received
        self.payouts.append({
            "date": trade_date.isoformat(),
            "gross_withdrawn": round(request, 2),
            "received": round(received, 2),
            "balance_after": round(self.balance, 2),
            "hwm": round(self.hwm, 2),
            "best_day": round(best, 2),
            "cycle_net": round(net, 2),
            "best_share": round(best / net, 4),
        })
        self.cycle_start = trade_date
        self.cycle_base = self.balance
        self.cycle_pnls = []


def base_config(leverage: float, day_cap: float, hook):
    return {
        "data_path": "qqq_longport.csv",
        "ticker": "QQQ",
        "initial_capital": INITIAL,
        "lookback_days": 1,
        "start_date": date(2026, 3, 24),
        "end_date": date(2026, 9, 23),
        "check_interval_minutes": 15,
        "enable_transaction_fees": True,
        "transaction_fee_per_share": 0.008166,
        "min_round_trip_fee": 2.16,
        "slippage_per_share": 0.01,
        "trading_start_time": (9, 40),
        "trading_end_time": (15, 40),
        "max_positions_per_day": 10,
        "print_daily_trades": False,
        "print_trade_details": False,
        "show_equity_report": False,
        "K1": 1,
        "K2": 1.04,
        "enable_k_side_adjustment": True,
        "k_side_adjustment": {
            "long": {
                "metric": "minutes_from_open",
                "min": 120,
                "k_if_true": 0.9,
                "k_if_false": 1.0,
            },
        },
        "leverage": leverage,
        "use_vwap": False,
        "enable_intraday_stop_loss": True,
        "max_daily_loss_amount": INITIAL * DAILY_LOSS_PCT,
        "intraday_stop_loss_mode": "from_day_start",
        "enable_per_trade_stop_loss": True,
        "hard_sl_pct_of_initial": HARD_SL_PCT,
        "max_daily_profit_amount": day_cap,
        "enable_trailing_take_profit": True,
        "trailing_tp_activation_pct": 0.006,
        "trailing_tp_callback_pct": 0.65,
        "entry_trend_filter": [
            {"metric": "er5", "min": 0.1},
            {"metric": "range1", "max": 0.029},
            {"metric": "sigma", "min": 0.0003},
        ],
        "on_day_close": hook,
    }


def run_one(leverage: float, day_cap: float) -> Account:
    acct = Account(leverage=leverage, day_cap=day_cap)

    def hook(trade_date, trades, day_pnl, capital, intraday_low, intraday_high, capital_start):
        capital = acct.on_day_close(
            trade_date, trades, day_pnl, capital, intraday_low, intraday_high, capital_start,
        )
        if acct.busted:
            hook_cfg["_stop_backtest"] = True
        return capital

    hook_cfg = base_config(leverage, day_cap, hook)
    backtest.run_backtest(hook_cfg)
    return acct


def summarize(acct: Account) -> dict:
    net = acct.cash - ACTIVATION_FEE
    return {
        "leverage": acct.leverage,
        "day_cap": round(acct.day_cap, 2),
        "busted": acct.busted,
        "bust_date": acct.bust_date.isoformat() if acct.bust_date else "",
        "end_date": acct.end_date.isoformat() if acct.end_date else "",
        "end_balance": round(acct.end_balance, 2),
        "hwm": round(acct.hwm, 2),
        "payouts": len(acct.payouts),
        "cash": round(acct.cash, 2),
        "net_after_fee": round(net, 2),
        "consistency_block_days": acct.blocked_consistency_days,
        "detail": acct.payouts,
    }


def main():
    if not os.path.exists("qqq_longport.csv"):
        raise SystemExit("缺少 qqq_longport.csv")
    grid = [
        (1.0, DAY_CAP_200K),
        (1.5, DAY_CAP_200K),
        (2.0, DAY_CAP_200K),
        (2.5, DAY_CAP_200K),
        (3.0, DAY_CAP_200K),
        (2.0, 1100.0),
        (2.0, 0.0),
    ]
    rows = []
    for lev, cap in grid:
        print("\n" + "=" * 72)
        print(f"RUN leverage={lev} day_cap={cap:.2f}")
        acct = run_one(lev, cap)
        row = summarize(acct)
        rows.append(row)
        print(
            f"RESULT lev={row['leverage']} cap={row['day_cap']} bust={row['busted']} "
            f"{row['bust_date']} payouts={row['payouts']} cash={row['cash']} "
            f"net={row['net_after_fee']} end_bal={row['end_balance']} "
            f"consistency_blocked_days={row['consistency_block_days']}"
        )
        for payout in row["detail"]:
            print("  payout", payout)
    print("\n==== SUMMARY ====")
    for row in rows:
        print(
            f"{row['leverage']:>4}x cap={row['day_cap']:<8} bust={str(row['busted']):<5} "
            f"payouts={row['payouts']} cash={row['cash']:<10} net={row['net_after_fee']:<10} "
            f"end={row['end_balance']}"
        )


if __name__ == "__main__":
    main()
