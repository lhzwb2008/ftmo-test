"""
开盘冷清半仓，与 Quantra/backtest.py quiet_open_multipliers 一致：
当日 09:30-09:39 成交量之和 / 前 QUIET_OPEN_LOOKBACK_DAYS 个交易日同窗口均值 < 阈值 → 当日仓位 × QUIET_OPEN_POSITION_MULT。
只改仓位不改信号。数据不足（历史不够 N 天、当日 09:39 K 线未到）时返回 1.0。

用法：主循环每次准备开仓前调用 update(minute_df, now)；当日首次算出后缓存，之后 current_multiplier(day) 直接返回。
minute_df 需含 Date(date)、Time('HH:MM')、Volume 列。
"""
import pandas as pd

from strategy_params import (
    QUIET_OPEN_HALF_POSITION,
    QUIET_OPEN_LOOKBACK_DAYS,
    QUIET_OPEN_POSITION_MULT,
    QUIET_OPEN_REL_THRESHOLD,
)

OPEN_WINDOW_START = "09:30"
OPEN_WINDOW_END = "09:39"

_state = {"date": None, "mult": 1.0, "rel": None}


def describe():
    if not QUIET_OPEN_HALF_POSITION:
        return "开盘冷清半仓: 关闭"
    return (
        f"开盘冷清半仓: {OPEN_WINDOW_START}-{OPEN_WINDOW_END} 成交量 / 前{QUIET_OPEN_LOOKBACK_DAYS}日均值 "
        f"< {QUIET_OPEN_REL_THRESHOLD} → 仓位×{QUIET_OPEN_POSITION_MULT}"
    )


def compute_open_rel_volume(minute_df, day):
    """返回 day 的开盘相对成交量；当日窗口不完整或历史不足时返回 None。"""
    if minute_df is None or minute_df.empty:
        return None
    bars = minute_df[(minute_df["Time"] >= OPEN_WINDOW_START) & (minute_df["Time"] <= OPEN_WINDOW_END)]
    if not ((bars["Date"] == day) & (bars["Time"] == OPEN_WINDOW_END)).any():
        return None
    vol = bars.groupby("Date")["Volume"].sum().sort_index()
    prior = vol[vol.index < day].tail(QUIET_OPEN_LOOKBACK_DAYS)
    if len(prior) < QUIET_OPEN_LOOKBACK_DAYS:
        return None
    baseline = float(prior.mean())
    if baseline <= 0 or pd.isna(baseline):
        return None
    return float(vol[day]) / baseline


def update(minute_df, now):
    """当日尚未确定系数时尝试计算；返回当前系数。"""
    day = now.date()
    if _state["date"] == day:
        return _state["mult"]
    if not QUIET_OPEN_HALF_POSITION:
        _state.update(date=day, mult=1.0, rel=None)
        return 1.0
    rel = compute_open_rel_volume(minute_df, day)
    ts = now.strftime('%Y-%m-%d %H:%M:%S')
    if rel is None:
        print(f"[{ts}] 开盘冷清判断: 数据不足（需当日 {OPEN_WINDOW_END} K线及前{QUIET_OPEN_LOOKBACK_DAYS}个交易日），暂按满仓")
        return 1.0
    mult = QUIET_OPEN_POSITION_MULT if rel < QUIET_OPEN_REL_THRESHOLD else 1.0
    _state.update(date=day, mult=mult, rel=rel)
    print(f"[{ts}] 开盘冷清判断: 相对成交量={rel:.3f}（阈值 {QUIET_OPEN_REL_THRESHOLD}）→ 当日仓位系数 {mult}")
    return mult


def current_multiplier(day):
    """返回 day 已确定的仓位系数，未确定时为 1.0。"""
    if _state["date"] == day:
        return _state["mult"]
    return 1.0
