# -*- coding: utf-8 -*-
"""
Prop firm $100K 年度期望测算（挑战杠杆 = Funded 杠杆）

用真实两年 1 分钟回测日收益做自助推演，覆盖 FTMO / FundedNext / The5ers /
Blueberry / GOAT / TTP / E8 / FN Futures Flex。

输出 same_leverage_detailed.csv：年净收益、购考次数、挑战通过率、
Funded 存活天数、爆仓率、提款成功率等。

用法: python experiment_prop_ev.py
"""
import io
import contextlib
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from backtest import run_backtest

DATA_PATH = "../quantra/qqq_longport_2year.csv"
BASE_CONFIG = {
    'data_path': DATA_PATH,
    'ticker': 'QQQ',
    'initial_capital': 100000,
    'lookback_days': 1,
    'start_date': date(2024, 7, 1),
    'end_date': date(2026, 6, 30),
    'check_interval_minutes': 15,
    'enable_transaction_fees': True,
    'transaction_fee_per_share': 0.008166,
    'slippage_per_share': 0.01,
    'trading_start_time': (9, 40),
    'trading_end_time': (15, 40),
    'max_positions_per_day': 10,
    'print_daily_trades': False,
    'print_trade_details': False,
    'K1': 1, 'K2': 1,
    'enable_k_side_adjustment': True,
    'k_side_adjustment': {'long': {'metric': 'minutes_from_open', 'min': 120, 'k_if_true': 0.9, 'k_if_false': 1.0}},
    'use_vwap': False,
    'enable_per_trade_stop_loss': False,
    'enable_trailing_take_profit': True,
    'trailing_tp_activation_pct': 0.001,
    'trailing_tp_callback_pct': 0.7,
    'entry_trend_filter': {'metric': 'er5', 'min': 0.1},
}

START = 100000.0
YEAR_DAYS = 252
LEV_GRID = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0]
SEED = 17
N_BOOT = 10000
N_PIPE = 6000


def get_daily_returns(leverage, stop_pct=None):
    """跑真实回测，返回日收益率序列与 metrics。stop_pct=None 表示不加额外日内止损。"""
    cfg = dict(BASE_CONFIG)
    cfg['leverage'] = leverage
    if stop_pct is not None:
        cfg['enable_intraday_stop_loss'] = True
        cfg['intraday_stop_loss_pct'] = stop_pct
    else:
        cfg['enable_intraday_stop_loss'] = False
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        daily_df, _monthly, _trades, metrics = run_backtest(cfg)
    return daily_df['daily_return'].to_numpy(), metrics


@dataclass
class Firm:
    name: str
    fee: float
    reset_fee: float
    targets: List[float]
    daily_dd: Optional[float]
    max_dd: float
    dd_type: str
    min_days: int
    consistency: Optional[float]
    daily_profit_cap: Optional[float]
    ch_lev: float
    funded_lev: float
    share: float
    payout_cycle: int
    first_wait: int
    min_payout_pct: float
    payout_mode: str
    fee_refund_at: int
    notes: str = ""


FIRMS = [
    Firm("FTMO 2-Step", 480.0, 480.0, [0.10, 0.05], 0.05, 0.10, "static", 4, None, None, 2.0, 1.5, 0.80, 14, 14, 0.0, "static_all", 1, "静态10%；首提后退费"),
    Firm("FundedNext Stellar", 549.99, 494.99, [0.08, 0.05], 0.05, 0.10, "static", 5, None, None, 2.0, 1.5, 0.80, 14, 21, 0.0, "static_all", 1, "首提21天后每14天；首提后退费"),
    Firm("The5ers High Stakes", 545.0, 545.0, [0.10, 0.05], 0.05, 0.10, "static", 3, None, None, 2.0, 1.5, 0.80, 14, 14, 0.0015, "static_all", 1, "10%/5%；首提后退费"),
    Firm("Blueberry Prime", 455.0, 390.0, [0.08, 0.06], 0.04, 0.10, "static", 5, None, None, 2.0, 1.5, 0.80, 14, 14, 0.0, "static_all", 0, "日亏4%；不退费"),
    Firm("GOAT 2-Step Standard", 398.0, 398.0, [0.10, 0.05], 0.05, 0.10, "static", 3, None, None, 2.0, 1.5, 0.80, 14, 14, 0.001, "goat", 4, "前2次提现封顶6%；第4次退费"),
    Firm("TTP CFD 2-Phase", 569.0, 569.0, [0.10, 0.05], 0.05, 0.10, "eod_trail", 3, None, None, 2.0, 1.5, 0.80, 14, 14, 0.001, "trail_half", 3, "EOD追踪10%；半提；第3次退费"),
    Firm("E8 Pro", 366.0, 439.0, [0.08], 0.025, 0.08, "static_then_lock", 1, None, 0.02, 2.0, 1.5, 0.80, 1, 0, 0.01, "e8_half", 0, "首提攒8%后半提；2%日利润cap"),
    Firm("FN Futures Flex", 129.99, 144.99, [0.05], None, 0.025, "eod_trail", 1, 0.40, None, 2.0, 1.5, 0.80, 5, 5, 0.0, "trail_all", 0, "挑战40%consistency；每5日提光"),
]


def trail_floor(hw: float, max_dd_pct: float, start: float = START) -> float:
    raw = hw - max_dd_pct * start
    return start if raw >= start else raw


def run_challenge(returns, firm: Firm, max_days: int = 500) -> Tuple[str, int]:
    bal = 1.0
    day = 0
    n = len(returns)
    for phase_i, _target in enumerate(firm.targets):
        hw = bal
        best_day = 0.0
        counted = 0.0
        phase_days = 0
        floor_static = 1.0 - firm.max_dd
        need = sum(firm.targets[: phase_i + 1])
        while day < n and day < max_days:
            r = float(returns[day])
            day += 1
            phase_days += 1
            day_start = bal
            worst = day_start + day_start * min(0.0, r) * 1.2
            end = day_start * (1.0 + r)
            if firm.daily_dd is not None and day_start - worst > firm.daily_dd:
                return 'fail', day
            floor = floor_static if firm.dd_type in ("static", "static_then_lock") else trail_floor(hw, firm.max_dd, start=1.0)
            if min(worst, end) <= floor + 1e-12:
                return 'fail', day
            pnl = end - day_start
            bal = end
            if firm.dd_type == "eod_trail":
                hw = max(hw, bal)
            if firm.daily_profit_cap is not None and pnl > 0:
                counted += min(pnl, firm.daily_profit_cap)
            else:
                counted += pnl
            best_day = max(best_day, max(0.0, pnl))
            phase_profit = bal - 1.0
            ok_profit = (counted >= need) if firm.daily_profit_cap is not None else (phase_profit >= need)
            if ok_profit and phase_days >= firm.min_days:
                if firm.consistency is not None and best_day > firm.consistency * max(phase_profit, 1e-12):
                    continue
                break
        else:
            return 'end', day
    return 'pass', day


def evaluate_challenge(returns, firm: Firm, n: int = N_BOOT, seed: int = SEED):
    rng = np.random.default_rng(seed)
    res = [run_challenge(rng.choice(returns, size=500, replace=True), firm) for _ in range(n)]
    passes = [d for r, d in res if r == 'pass']
    fails = [d for r, d in res if r == 'fail']
    p = len(passes) / len(res)
    avg_pass = float(np.mean(passes)) if passes else float('nan')
    avg_fail = float(np.mean(fails)) if fails else 0.0
    if p > 0:
        e_days = (1 - p) / p * avg_fail + avg_pass
        e_cost = firm.fee + (1 - p) / p * firm.reset_fee
    else:
        e_days, e_cost = float('inf'), float('inf')
    return dict(boot_pass=p, avg_pass=avg_pass, avg_fail=avg_fail, e_days=e_days, e_cost=e_cost)


def maybe_refund(cash, firm, n_payouts, fee_paid):
    if firm.fee_refund_at and n_payouts == firm.fee_refund_at:
        return cash + fee_paid
    return cash


def sim_pipeline_detailed(returns, firm, n=N_PIPE, seed=SEED + 3):
    """
    挑战与 Funded 使用同一日收益序列（同杠杆）。
    返回聚合指标 + 分位数。
    """
    rng = np.random.default_rng(seed)

    year_nets = []
    year_buys = []
    year_challenge_fails = []  # 年内挑战失败重考次数（不含首次购）
    year_funded_episodes = []
    year_busts = []
    year_payouts = []
    year_gross_payout = []  # 分润后到手（不含退费）
    year_refunds = []
    year_challenge_days = []
    year_funded_days = []

    # 每个 Funded 片段
    ep_lives = []          # 存活交易日
    ep_busted = []         # 是否爆仓结束（vs 年到/仍存活）
    ep_got_payout = []     # 是否至少提款一次
    ep_payout_amt = []     # 该片段提款到手
    ep_payout_n = []

    for _ in range(n):
        day = 0
        cash = 0.0
        n_buy = 0
        n_ch_fail = 0
        n_bust = 0
        n_pay_total = 0
        gross_pay = 0.0
        refund_total = 0.0
        ch_days = 0
        fu_days = 0
        n_episodes = 0

        while day < YEAR_DAYS:
            cash -= firm.fee
            fee_paid = firm.fee
            n_buy += 1
            n_payouts = 0
            ep_pay_amt = 0.0
            passed = False

            while day < YEAR_DAYS:
                result, d = run_challenge(rng.choice(returns, size=500, replace=True), firm)
                day += d
                ch_days += d
                if result == 'pass':
                    passed = True
                    break
                if day >= YEAR_DAYS:
                    break
                cash -= firm.reset_fee
                fee_paid = firm.reset_fee
                n_buy += 1
                n_ch_fail += 1
            if not passed:
                break

            n_episodes += 1
            bal = START
            hw = START
            if firm.dd_type in ("static", "static_then_lock"):
                floor = START * (1.0 - firm.max_dd)
            else:
                floor = trail_floor(hw, firm.max_dd)

            first_payout_done = False
            days_since_payout = 0
            days_on_funded = 0
            since_countable = 0.0
            goat_payout_count = 0
            busted = False

            while day < YEAR_DAYS:
                r = float(rng.choice(returns))
                day_start = bal
                worst = day_start + day_start * min(0.0, r) * 1.2
                end = day_start * (1.0 + r)

                if firm.daily_dd is not None and day_start - worst > firm.daily_dd * START:
                    busted = True
                    n_bust += 1
                    day += 1
                    days_on_funded += 1
                    fu_days += 1
                    break
                if min(worst, end) <= floor:
                    busted = True
                    n_bust += 1
                    day += 1
                    days_on_funded += 1
                    fu_days += 1
                    break

                pnl = end - day_start
                if firm.payout_mode == "goat" and pnl > 3000:
                    pnl = 3000.0
                    end = day_start + pnl
                bal = end
                days_since_payout += 1
                days_on_funded += 1
                fu_days += 1

                if firm.dd_type == "eod_trail":
                    hw = max(hw, bal)
                    floor = trail_floor(hw, firm.max_dd)
                elif firm.dd_type == "static_then_lock" and first_payout_done:
                    floor = START

                if pnl > 0:
                    if firm.daily_profit_cap is not None:
                        since_countable += min(pnl, firm.daily_profit_cap * START)
                    else:
                        since_countable += pnl
                else:
                    since_countable += pnl

                profit = bal - START
                min_need = firm.min_payout_pct * START

                if firm.payout_mode == "e8_half":
                    need = 0.08 * START if not first_payout_done else max(min_need, 0.01 * START)
                    if since_countable >= need and profit >= need:
                        req = profit * 0.50
                        got = req * firm.share
                        cash += got
                        gross_pay += got
                        ep_pay_amt += got
                        bal -= req
                        since_countable = bal - START
                        n_payouts += 1
                        n_pay_total += 1
                        days_since_payout = 0
                        if not first_payout_done:
                            first_payout_done = True
                            floor = START
                        before = cash
                        cash = maybe_refund(cash, firm, n_payouts, fee_paid)
                        refund_total += cash - before
                        if bal <= floor:
                            busted = True
                            n_bust += 1
                            day += 1
                            break
                    day += 1
                    continue

                if not first_payout_done:
                    can_cycle = days_on_funded >= max(firm.first_wait, firm.payout_cycle)
                else:
                    can_cycle = days_since_payout >= firm.payout_cycle

                if can_cycle and profit > min_need:
                    if firm.payout_mode == "static_all":
                        got = profit * firm.share
                        cash += got
                        gross_pay += got
                        ep_pay_amt += got
                        bal = START
                    elif firm.payout_mode == "trail_all":
                        got = profit * firm.share
                        cash += got
                        gross_pay += got
                        ep_pay_amt += got
                        bal = START
                        hw = START
                        floor = trail_floor(hw, firm.max_dd)
                    elif firm.payout_mode == "trail_half":
                        take = profit * 0.50
                        got = take * firm.share
                        cash += got
                        gross_pay += got
                        ep_pay_amt += got
                        bal -= take
                        if floor >= START - 1e-9:
                            floor = START
                    elif firm.payout_mode == "goat":
                        if goat_payout_count < 2 and profit > 0.06 * START:
                            bal -= (profit - 0.06 * START)
                            profit = 0.06 * START
                        take = profit
                        got = take * firm.share
                        cash += got
                        gross_pay += got
                        ep_pay_amt += got
                        bal -= take
                        if bal < START:
                            bal = START
                        goat_payout_count += 1

                    first_payout_done = True
                    n_payouts += 1
                    n_pay_total += 1
                    days_since_payout = 0
                    since_countable = 0.0
                    before = cash
                    cash = maybe_refund(cash, firm, n_payouts, fee_paid)
                    refund_total += cash - before

                day += 1
            else:
                # 年末仍存活
                if bal > START:
                    if firm.payout_mode in ("e8_half", "trail_half"):
                        got = (bal - START) * 0.50 * firm.share
                    else:
                        got = (bal - START) * firm.share
                    cash += got
                    gross_pay += got
                    ep_pay_amt += got

            ep_lives.append(days_on_funded)
            ep_busted.append(1 if busted else 0)
            ep_got_payout.append(1 if n_payouts > 0 else 0)
            ep_payout_amt.append(ep_pay_amt)
            ep_payout_n.append(n_payouts)

            if not busted:
                break  # 年到或仍持有，本年内不再重开

        year_nets.append(cash)
        year_buys.append(n_buy)
        year_challenge_fails.append(n_ch_fail)
        year_funded_episodes.append(n_episodes)
        year_busts.append(n_bust)
        year_payouts.append(n_pay_total)
        year_gross_payout.append(gross_pay)
        year_refunds.append(refund_total)
        year_challenge_days.append(ch_days)
        year_funded_days.append(fu_days)

    net = np.array(year_nets)
    ep_lives_a = np.array(ep_lives) if ep_lives else np.array([0.0])
    ep_bust_a = np.array(ep_busted) if ep_busted else np.array([0.0])
    ep_pay_a = np.array(ep_got_payout) if ep_got_payout else np.array([0.0])

    return dict(
        pipeline_net=float(net.mean()),
        pipeline_med=float(np.median(net)),
        pipeline_p10=float(np.percentile(net, 10)),
        pipeline_p90=float(np.percentile(net, 90)),
        profit_prob=float(np.mean(net > 0)),
        avg_buys=float(np.mean(year_buys)),
        avg_ch_fails=float(np.mean(year_challenge_fails)),
        avg_funded_episodes=float(np.mean(year_funded_episodes)),
        avg_busts_per_year=float(np.mean(year_busts)),
        avg_payouts_per_year=float(np.mean(year_payouts)),
        avg_gross_payout=float(np.mean(year_gross_payout)),
        avg_refund=float(np.mean(year_refunds)),
        avg_challenge_days=float(np.mean(year_challenge_days)),
        avg_funded_days=float(np.mean(year_funded_days)),
        # Funded 片段口径
        funded_avg_life_days=float(ep_lives_a.mean()),
        funded_bust_rate=float(ep_bust_a.mean()),  # 该片段以爆仓结束的比例
        funded_survive_year_rate=float(1.0 - ep_bust_a.mean()),
        funded_payout_success_rate=float(ep_pay_a.mean()),  # 至少提款一次
        funded_avg_payout_per_ep=float(np.mean(ep_payout_amt) if ep_payout_amt else 0.0),
        funded_avg_payout_n_per_ep=float(np.mean(ep_payout_n) if ep_payout_n else 0.0),
        n_funded_episodes_total=len(ep_lives),
    )


def main():
    print("回测日收益（无额外日内止损）...", flush=True)
    ret_cache = {}
    for lev in LEV_GRID:
        r, m = get_daily_returns(lev, None)
        ann = (1 + pd.Series(r)).prod() ** (252 / len(r)) - 1
        ret_cache[lev] = r
        print(f"  {lev}x: 日均{r.mean()*100:.3f}% 年化{ann:.1%} MDD{m.get('mdd'):.2%}", flush=True)

    rows = []
    for firm0 in FIRMS:
        for L in LEV_GRID:
            firm = deepcopy(firm0)
            firm.ch_lev = L
            firm.funded_lev = L
            print(f"\n======== {firm.name} @ {L}x/{L}x ========", flush=True)
            r = ret_cache[L]
            ch = evaluate_challenge(r, firm, n=N_BOOT)
            det = sim_pipeline_detailed(r, firm, n=N_PIPE)
            print(
                f"  挑战通过率{ch['boot_pass']:.0%} 期望首过{ch['e_days']:.0f}天 期望考试费${ch['e_cost']:.0f}",
                flush=True,
            )
            print(
                f"  年净${det['pipeline_net']:,.0f} | 购考{det['avg_buys']:.2f}次 | "
                f"Funded片段{det['avg_funded_episodes']:.2f} | "
                f"片段均活{det['funded_avg_life_days']:.0f}天 | "
                f"片段爆仓率{det['funded_bust_rate']:.0%} | "
                f"至少提款率{det['funded_payout_success_rate']:.0%}",
                flush=True,
            )
            rows.append(dict(
                firm=firm.name,
                leverage=L,
                # 挑战
                challenge_pass_rate=ch['boot_pass'],
                challenge_avg_pass_days=ch['avg_pass'],
                challenge_avg_fail_days=ch['avg_fail'],
                challenge_e_first_pass_days=ch['e_days'],
                challenge_e_cost=ch['e_cost'],
                # 年管线
                annual_net_ev=det['pipeline_net'],
                annual_net_med=det['pipeline_med'],
                annual_net_p10=det['pipeline_p10'],
                annual_net_p90=det['pipeline_p90'],
                annual_profit_prob=det['profit_prob'],
                avg_account_purchases=det['avg_buys'],
                avg_challenge_retries=det['avg_ch_fails'],
                avg_funded_episodes_per_year=det['avg_funded_episodes'],
                avg_busts_per_year=det['avg_busts_per_year'],
                avg_payouts_per_year=det['avg_payouts_per_year'],
                avg_gross_payout_per_year=det['avg_gross_payout'],
                avg_fee_refund_per_year=det['avg_refund'],
                avg_challenge_days_per_year=det['avg_challenge_days'],
                avg_funded_days_per_year=det['avg_funded_days'],
                # Funded 片段
                funded_avg_life_days=det['funded_avg_life_days'],
                funded_bust_rate=det['funded_bust_rate'],
                funded_survive_to_year_end_rate=det['funded_survive_year_rate'],
                funded_got_payout_rate=det['funded_payout_success_rate'],
                funded_avg_payout_per_episode=det['funded_avg_payout_per_ep'],
                funded_avg_payout_count_per_episode=det['funded_avg_payout_n_per_ep'],
            ))

    df = pd.DataFrame(rows)
    df.to_csv("same_leverage_detailed.csv", index=False)

    # 控制台：按 firm 分组的可读表
    print("\n" + "=" * 100)
    print("同杠杆明细汇总（挑战=Funded）")
    print("=" * 100)
    cols = [
        'firm', 'leverage', 'annual_net_ev', 'avg_account_purchases',
        'challenge_pass_rate', 'challenge_e_first_pass_days',
        'funded_got_payout_rate', 'funded_avg_life_days', 'funded_bust_rate',
        'avg_busts_per_year', 'avg_payouts_per_year', 'annual_profit_prob',
    ]
    print(df[cols].to_string(index=False))
    print("\n已写入 same_leverage_detailed.csv")


if __name__ == '__main__':
    main()
