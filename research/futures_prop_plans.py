#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期货 prop 产品规则（2026-11-08 按各家 help center 核实，价格取常年可用折扣后的实付价）。

只收录允许自动化交易的产品；Apex（含 Legacy）用户协议禁止自动化，未收录。
规则无法完全对应时取接近且偏保守的写法，见各条 note。
"""

import os as _os
import sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, ROOT)
_os.chdir(ROOT)  # 数据与输出路径均相对仓库根目录

from futures_prop_study import Phase, Plan

E, I, S = 'eod', 'intraday', 'static'
API_TOPSTEP = 'TopstepX API（$14.50/月），条款要求订单从本人设备发出、禁 VPS'
NT8 = '允许 bot，无 API；NinjaTrader（Windows）或 TradingView webhook'
TRADEIFY = '允许 bot，无 API；要求同一策略不得在其他公司使用'
LUCID_R = '允许自动化（帮助中心无审批；旧版 Pro/Direct 合同写需书面批准）；Rithmic 线可 Linux 直连 Protocol API'


def topstep(size, dd, target, fee, cap_std, cap_cons, path='std', no_act=False, fee_no_act=0.0):
    common = dict(size=0.0, monthly=True, target=target, ev=Phase(dd, E, 0.0), ev_consistency=0.55, ev_min_days=2,
                  fd=Phase(dd, E, 0.0), pay_frac=0.5, pay_min=125, split=((1e18, 0.9),),
                  floor_after_payout='lock', lock_after=0.0, auto=API_TOPSTEP)
    k = f'ts{size // 1000}{"c" if path == "cons" else ""}{"n" if no_act else ""}'
    name = f'Topstep {size // 1000}K（{"一致性" if path == "cons" else "5 盈利日"}出金{"，免激活费" if no_act else ""}）'
    if path == 'std':
        pay = dict(pay_min_days=0, pay_win_days=5, pay_win_thr=150, pay_caps=(cap_std,))
    else:
        pay = dict(pay_min_days=3, pay_consistency=0.40, pay_caps=(cap_cons,))
    return Plan(k, name, fee=fee_no_act if no_act else fee, activation=0.0 if no_act else 149.0, **common, **pay,
                note='XFA 从 $0 起步，MLL 锁在 $0，首次出金后 MLL 固定为 $0')


def tradeify_flex(size, dd, thr, cap, fee):
    return Plan(f'tdf{size // 1000}', f'Tradeify Select Flex {size // 1000}K', size=size, fee=fee,
                target={25_000: 1500, 50_000: 3000, 100_000: 6000, 150_000: 9000}[size],
                ev=Phase(dd, E, None), ev_consistency=0.40, ev_min_days=3,
                fd=Phase(dd, E, 100.0), pay_min_days=0, pay_win_days=5, pay_win_thr=thr, pay_frac=0.5,
                pay_caps=(cap,), pay_min=250, floor_after_payout='lock', auto=TRADEIFY)


def tradeify_daily(size, dd_ev, dd_fd, dll, keep, cap, fee):
    return Plan(f'tdd{size // 1000}', f'Tradeify Select Daily {size // 1000}K', size=size, fee=fee,
                target={25_000: 1500, 50_000: 3000, 100_000: 6000, 150_000: 9000}[size],
                ev=Phase(dd_ev, E, None), ev_consistency=0.40, ev_min_days=3,
                fd=Phase(dd_fd, E, 100.0, dll=dll), pay_min_days=1, pay_keep=keep, pay_cycle_mult=2.0,
                pay_caps=(cap,), pay_min=250, floor_after_payout='lock', auto=TRADEIFY)


def tradeify_growth(size, dd, dll, thr, buf, caps, pmin, fee):
    return Plan(f'tdg{size // 1000}', f'Tradeify Growth {size // 1000}K', size=size, fee=fee,
                target={25_000: 1500, 50_000: 3000, 100_000: 6000, 150_000: 9000}[size],
                ev=Phase(dd, E, None, dll=dll), ev_min_days=1,
                fd=Phase(dd, E, 100.0, dll=dll), pay_min_days=0, pay_win_days=5, pay_win_thr=thr,
                pay_consistency=0.35, pay_buffer=buf, pay_caps=caps, pay_min=pmin,
                floor_after_payout='lock', auto=TRADEIFY)


def tradeify_lightning(size, dd, dll, prof, caps, fee):
    return Plan(f'tdl{size // 1000}', f'Tradeify Lightning {size // 1000}K（直接 funded）', size=size, fee=fee,
                fd=Phase(dd, E, 100.0, dll=dll), pay_min_days=0, pay_consistency=(0.20, 0.25, 0.30),
                pay_cycle_profit=prof, pay_caps=caps, pay_min=1000, floor_after_payout='lock', auto=TRADEIFY)


def fn_flex(size, target, dd, thr, cap, fee):
    return Plan(f'fnf{size // 1000}', f'FundedNext Flex {size // 1000}K', size=size, fee=fee, target=target,
                ev=Phase(dd, E, 100.0), ev_consistency=0.40, ev_min_days=3,
                fd=Phase(dd, E, 100.0), pay_min_days=0, pay_win_days=5, pay_win_thr=thr, pay_cycle_profit=500,
                pay_frac=0.5, pay_caps=(cap,), pay_min=250, split=((1e18, 0.95),), max_payouts=5, auto=NT8,
                note='考核一致性按「最佳单日 ≤ 40% × 总利润」近似')


def fn_rapid(size, target, dd, caps, fee, daily=False, dll=0.0, keep=0.0):
    return Plan(f'fnr{"d" if daily else "p"}{size // 1000}',
                f'FundedNext Rapid {"Daily" if daily else "Pro"} {size // 1000}K', size=size, fee=fee, target=target,
                ev=Phase(dd, E, 100.0, dll=dll), ev_min_days=1, fd=Phase(dd, E, 100.0, dll=dll),
                pay_min_days=1 if daily else 3, pay_cycle_profit=500, pay_consistency=0.0 if daily else 0.40,
                pay_keep=keep, pay_caps=caps, pay_min=250, max_payouts=5, auto=NT8)


def mffu_rapid(size, dd, keep, fee):
    return Plan(f'mfr{size // 1000}', f'MFFU Rapid {size // 1000}K', size=size, fee=fee,
                target={25_000: 1500, 50_000: 3000, 100_000: 6000, 150_000: 9000}[size],
                ev=Phase(dd, E, None), ev_consistency=0.50, ev_min_days=2,
                fd=Phase(dd, I, 100.0), pay_min_days=1, pay_keep=keep, pay_min=500, auto=NT8 + '；Funded 禁止持仓过 FOMC 前后 2 分钟',
                note='funded 为日内 trailing（含浮盈）')


def mffu_rapid_eod(size, dd, keep, prof, fee):
    return Plan(f'mfe{size // 1000}', f'MFFU Rapid EOD {size // 1000}K', size=size, fee=fee,
                target={25_000: 1500, 50_000: 3000}[size], ev=Phase(dd, E, None), ev_consistency=0.30, ev_min_days=4,
                fd=Phase(dd, E, 100.0), pay_min_days=1, pay_keep=keep, pay_cycle_profit=prof, pay_min=500,
                auto=NT8 + '；Funded 禁止持仓过 FOMC 前后 2 分钟')


def mffu_builder(size, dd, dll, keep, pmin, cap, fee):
    return Plan(f'mfb{size // 1000}', f'MFFU Builder {size // 1000}K', size=size, fee=fee,
                target={25_000: 1500, 50_000: 3000, 100_000: 6000, 150_000: 9000}[size],
                ev=Phase(dd, E, None, dll=dll), ev_min_days=1, fd=Phase(dd, E, None, dll=dll),
                pay_min_days=2, pay_consistency=0.50, pay_buffer=keep + pmin, pay_keep=keep, pay_cycle_profit=pmin,
                pay_caps=(cap,), pay_min=pmin, split=((1e18, 0.8),), max_payouts=5, floor_after_payout='lock', auto=NT8)


def mffu_pro(size, dd, keep, fee):
    return Plan(f'mfp{size // 1000}', f'MFFU Pro {size // 1000}K', size=size, fee=fee,
                target={50_000: 3000, 100_000: 6000, 150_000: 9000}[size],
                ev=Phase(dd, E, None), ev_consistency=0.50, ev_min_days=2, fd=Phase(dd, E, None),
                pay_min_days=1, pay_cycle=14, pay_keep=keep, pay_min=1000, split=((1e18, 0.8),),
                floor_after_payout='lock', auto=NT8 + '；Funded 禁止持仓过 FOMC 前后 2 分钟')


def lucid_flex(size, target, dd, thr, cap, fee, dll=0.0):
    key, name = f'luf{size // 1000}', f'Lucid Flex {size // 1000}K'
    if dll:
        key, name = key + 'd', name + f'（软日亏 ${dll:.0f}）'
    return Plan(key, name, size=size, fee=fee, target=target,
                ev=Phase(dd, E, 100.0, dll=dll), ev_consistency=0.40, ev_min_days=3,
                fd=Phase(dd, E, 100.0, dll=dll), pay_min_days=0, pay_win_days=5, pay_win_thr=thr, pay_frac=0.5,
                pay_caps=(cap,), pay_min=500, max_payouts=5, floor_after_payout='lock', auto=LUCID_R)


def lucid_pro(size, target, dd, prof, keep, caps, fee):
    return Plan(f'lup{size // 1000}', f'Lucid Pro {size // 1000}K（无日亏版）', size=size, fee=fee, target=target,
                ev=Phase(dd, E, 100.0), fd=Phase(dd, E, 100.0), pay_min_days=1, pay_cycle_profit=prof,
                pay_consistency=0.40, pay_keep=keep, pay_caps=caps, pay_min=500, max_payouts=5, auto=LUCID_R)


def lucid_direct(size, dd, dll, first, prof, caps, fee):
    return Plan(f'lud{size // 1000}', f'Lucid Direct {size // 1000}K（直接 funded）', size=size, fee=fee,
                fd=Phase(dd, E, 100.0, dll=dll), pay_min_days=1, pay_first_profit=first, pay_cycle_profit=prof,
                pay_consistency=0.20, pay_caps=caps, pay_min=500, max_payouts=5, auto=LUCID_R,
                note='一致性按周期内最佳日近似（官方为历史最佳日，实际更严）')


PLANS = [
    topstep(50_000, 2000, 3000, 49, 2000, 3000), topstep(50_000, 2000, 3000, 49, 2000, 3000, 'cons'),
    topstep(50_000, 2000, 3000, 49, 2000, 3000, no_act=True, fee_no_act=95),
    topstep(100_000, 3000, 6000, 99, 3000, 4000), topstep(100_000, 3000, 6000, 99, 3000, 4000, 'cons'),
    topstep(150_000, 4500, 9000, 199, 5000, 6000), topstep(150_000, 4500, 9000, 199, 5000, 6000, 'cons'),
    tradeify_flex(25_000, 1000, 100, 1250, 109 * 0.7), tradeify_flex(50_000, 2000, 150, 2500, 165 * 0.7),
    tradeify_flex(100_000, 3000, 200, 3500, 265 * 0.7), tradeify_flex(150_000, 4500, 250, 4500, 369 * 0.7),
    tradeify_daily(50_000, 2000, 2000, 1000, 2100, 1250, 165 * 0.7),
    tradeify_daily(100_000, 3000, 2500, 1250, 2600, 1750, 265 * 0.7),
    tradeify_growth(50_000, 2000, 1250, 150, 3000, (1500, 2000, 2500, 3000), 500, 145 * 0.6),
    tradeify_growth(100_000, 3500, 2500, 200, 4500, (2000, 2500, 3000, 4000), 1000, 255 * 0.6),
    tradeify_lightning(50_000, 2000, 1250, (3000, 2000), (2000, 2000, 2000, 2500), 492 * 0.7),
    tradeify_lightning(100_000, 4000, 2500, (6000, 3500), (2500, 2500, 2500, 3000), 660 * 0.7),
    fn_flex(50_000, 2500, 1500, 200, 1500, 84.99), fn_flex(100_000, 5000, 2500, 200, 2500, 159.99),
    fn_flex(150_000, 8000, 4000, 250, 4000, 289.99),
    fn_rapid(50_000, 3000, 2000, (1000, 1000, 1000, 1250), 174.99),
    fn_rapid(100_000, 5000, 2500, (2000, 2000, 2000, 2500), 289.99),
    fn_rapid(50_000, 3000, 2000, (1000, 1000, 1000, 1250), 179.99, daily=True, dll=1000, keep=2100),
    mffu_rapid(50_000, 2000, 2100, 125), mffu_rapid(100_000, 3000, 3100, 356 * 0.6),
    mffu_rapid(150_000, 4000, 4600, 463 * 0.6),
    mffu_rapid_eod(50_000, 2000, 2100, 500, 225 * 0.6),
    mffu_builder(50_000, 2000, 1000, 2100, 500, 2000, 153 * 0.6),
    mffu_builder(100_000, 3000, 1750, 3100, 1000, 3000, 282 * 0.6),
    mffu_pro(50_000, 2000, 2100, 265 * 0.6), mffu_pro(100_000, 3000, 3100, 401 * 0.6),
    lucid_flex(50_000, 3000, 2000, 150, 2000, 115.5), lucid_flex(50_000, 3000, 2000, 150, 2000, 80.5, dll=1200),
    lucid_flex(100_000, 6000, 3000, 200, 2500, 195.3),
    lucid_flex(150_000, 9000, 4500, 250, 3000, 265.3),
    lucid_pro(50_000, 3000, 2000, 500, 2100, (2000, 2500), 150.0),
    lucid_pro(100_000, 6000, 3000, 750, 3100, (2500, 3000), 237.0),
    lucid_direct(50_000, 2000, 1200, 3000, 2500, (2000, 2000, 2000, 2500), 360.5),
    lucid_direct(100_000, 3500, 2100, 6000, 3500, (2500, 2500, 2500, 3000), 490.0),
]
