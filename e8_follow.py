#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E8 Pro Perpetual 纳指永续：自己算 QQQ 噪声带信号，不依赖任何其它策略进程。

每分钟把行情缓存（longport_data_service.py 写的 market_data_cache.db）里今天已收完的 1 分钟 K
交给 backtest.py 回放一遍，回放结束时的持仓就是现在应持的方向；参数全部来自 strategy_params.py。
只跟方向，仓位按 E8 初始资金的 NOTIONAL_MULT 倍名义金额，开盘冷清日减半。
E8 不接受市价单，用穿过盘口的限价单成交；每笔带美元止损，进程挂了也不会把日亏线打穿。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, time as dtime

import pandas as pd
import pytz

import backtest
import quiet_open
from strategy_params import (
    LOOKBACK_DAYS, K1, K2, ENABLE_K_SIDE_ADJUSTMENT, USE_VWAP,
    ENABLE_TRAILING_TAKE_PROFIT, TRAILING_TP_ACTIVATION_PCT, TRAILING_TP_CALLBACK_PCT,
    ENABLE_ENTRY_TREND_FILTER, ENTRY_TREND_ER5_MIN, ENABLE_RANGE1_FILTER, ENTRY_TREND_RANGE1_MAX,
    ENABLE_SIGMA_FILTER, ENTRY_TREND_SIGMA_MIN, STRATEGY_VERSION,
)
from trend_er5_gate import history_days_back
import k_side_adjust

ET = pytz.timezone("America/New_York")
ENDPOINT = "https://trade.e8markets.com/api/mcp"
SYMBOL = "HL_HIP3_XYZ_XYZ100"
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
MARKET_DB = os.environ.get("MARKET_DATA_DB_PATH", os.path.join(REPO_DIR, "market_data_cache.db"))
SIGNAL_SYMBOL = "QQQ.US"
SIGNAL_CSV = os.environ.get("E8_SIGNAL_CSV", "/tmp/e8_signal_minutes.csv")
MARKET_DATA_MAX_AGE_SEC = 120
STRATEGY_START = (9, 40)
STRATEGY_END = (15, 40)
CHECK_INTERVAL_MINUTES = 15
MAX_POSITIONS_PER_DAY = 10

# $5,000、日亏 $125、总亏 $400、单日计入上限 $100。
# 近一年（2025-10-10 ~ 2026-10-16）4x 通过率最高、也够快：约 3 个月内 62% 的起点能过，
# 中位 26 个交易日。再加大杠杆只增加爆仓，因为单日计入被 $100 封顶，亏的一边却放大。
NOTIONAL_MULT = 4.0
LOT_STEP = 0.01
MAX_LOTS = 1.5
MIN_PRICE = 5_000.0
MAX_PRICE = 80_000.0
# 官方日亏 2.5%（$125）之前先平；总亏 8%（底线 $4,600）留 $100。
DAILY_LOSS_HALT = 100.0
DAILY_ROOM_HALT = 0.5          # room.dailyPct，剩余百分点；0.5 = 还剩 $25
STATIC_ROOM_HALT = 2.0         # 剩余 2 个百分点 = 离 $4,600 还有 $100
PROFIT_CAP_HALT = 90.0         # 官方单日计入上限 $100，到 $90 就停
STOP_LOSS_USD = 110.0          # 略宽于脚本的 $100 日亏停手，只兜进程失联；再紧会切掉策略自己的出场
CROSS_PCT = 0.0015             # 限价穿过最新价 0.15%，相当于立刻成交的市价单
SESSION_OPEN = dtime(9, 40)
SESSION_CLOSE = dtime(15, 39)
# E8 API 每天 50,000 次；每轮 2 次，全天 3 秒一轮会在晚上用完额度、被 429 锁到次日
ACTIVE_POLL_SEC = 3
IDLE_POLL_SEC = 60


def log(msg):
    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def call(tool, args=None):
    key = os.environ["E8_API_KEY"]
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args or {}},
    }).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"{tool} HTTP {exc.code}: {detail}") from exc
    outer = json.loads(raw)
    if "error" in outer:
        raise RuntimeError(f"{tool}: {outer['error'].get('message', outer['error'])}")
    return json.loads(outer["result"]["content"][0]["text"])


def strategy_config(start, end):
    gates = []
    if ENABLE_ENTRY_TREND_FILTER:
        gates.append({"metric": "er5", "min": ENTRY_TREND_ER5_MIN})
    if ENABLE_RANGE1_FILTER:
        gates.append({"metric": "range1", "max": ENTRY_TREND_RANGE1_MAX})
    if ENABLE_SIGMA_FILTER:
        gates.append({"metric": "sigma", "min": ENTRY_TREND_SIGMA_MIN})
    config = {
        "data_path": SIGNAL_CSV, "ticker": "QQQ", "initial_capital": 5000,
        "lookback_days": LOOKBACK_DAYS, "start_date": start, "end_date": end,
        "check_interval_minutes": CHECK_INTERVAL_MINUTES,
        "trading_start_time": STRATEGY_START, "trading_end_time": STRATEGY_END,
        "max_positions_per_day": MAX_POSITIONS_PER_DAY,
        "K1": K1, "K2": K2, "enable_k_side_adjustment": ENABLE_K_SIDE_ADJUSTMENT, "use_vwap": USE_VWAP,
        "enable_trailing_take_profit": ENABLE_TRAILING_TAKE_PROFIT,
        "trailing_tp_activation_pct": TRAILING_TP_ACTIVATION_PCT,
        "trailing_tp_callback_pct": TRAILING_TP_CALLBACK_PCT,
        "stop_entries_after_trailing_tp": False,
        "entry_trend_filter": gates or None,
        # 仓位由 quiet_open 和 NOTIONAL_MULT 决定，这里只要方向
        "quiet_open_half_position": False,
        # 半日由行情服务的交易日历判断；backtest 按本机时钟认"今天"，服务器在东八区，午后会把今天当成半日
        "skip_half_trading_days": False,
        "enable_transaction_fees": False, "slippage_per_share": 0,
        "enable_intraday_stop_loss": False, "enable_per_trade_stop_loss": False,
        "print_daily_trades": False, "print_trade_details": False, "show_equity_report": False,
    }
    if ENABLE_K_SIDE_ADJUSTMENT:
        config["k_side_adjustment"] = {"long": {
            "metric": "minutes_from_open", "min": k_side_adjust.K_LONG_AFTER_MINUTES,
            "k_if_true": k_side_adjust.K1_AFTER_THRESHOLD, "k_if_false": k_side_adjust.K1_BEFORE_THRESHOLD,
        }}
    return config


def market_state(now):
    """返回 (今天能否交易, 说明, 行情截止分钟)。截止分钟之前的 K 线已经收完。"""
    try:
        conn = sqlite3.connect(MARKET_DB)
        state = dict(conn.execute("SELECT key, value FROM service_state").fetchall())
        conn.close()
    except sqlite3.Error as exc:
        return False, f"读不到行情缓存 {MARKET_DB}: {exc}", None
    if state.get("calendar_date") != now.date().isoformat():
        return False, f"交易日历不是今天（{state.get('calendar_date')}）", None
    if state.get("is_trading_day") != "1":
        return False, "今天不是交易日", None
    if state.get("is_half_trading_day") == "1":
        return False, "今天是半交易日，不交易", None
    if state.get("prev_trading_day_is_half") == "1":
        return False, "上一交易日是半交易日，噪声带数据不全，不交易", None
    try:
        last_ok = ET.localize(datetime.strptime(state.get("last_success_at", ""), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return True, f"行情心跳格式异常: {state.get('last_success_at')}", None
    age = (now - last_ok).total_seconds()
    if age > MARKET_DATA_MAX_AGE_SEC:
        return True, f"行情 {age:.0f} 秒没更新", None
    return True, "", last_ok.replace(second=0, microsecond=0, tzinfo=None)


def load_minutes(day, cutoff):
    start = day - timedelta(days=history_days_back(LOOKBACK_DAYS))
    conn = sqlite3.connect(MARKET_DB)
    rows = conn.execute("""
        SELECT datetime_et, open, high, low, close, volume FROM candles
        WHERE symbol = ? AND date >= ? AND date <= ? AND datetime_et < ?
        ORDER BY datetime_et ASC
    """, (SIGNAL_SYMBOL, start.isoformat(), day.isoformat(), cutoff.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()
    conn.close()
    df = pd.DataFrame(rows, columns=["DateTime", "Open", "High", "Low", "Close", "Volume"])
    df["DateTime"] = pd.to_datetime(df["DateTime"])
    df = df.drop_duplicates(subset=["DateTime"])
    tod = df["DateTime"].dt.strftime("%H:%M")
    df = df[(tod >= "09:30") & (tod <= "16:00") & (df["DateTime"].dt.weekday < 5)]
    return df.reset_index(drop=True)


def strategy_target(day, minutes):
    """backtest.py 回放到最后一根已收完的 K，返回 (方向, 说明)。"""
    today_bars = minutes[minutes["DateTime"].dt.date == day]
    if today_bars.empty:
        return "flat", "今天还没有 K 线"
    minutes.to_csv(SIGNAL_CSV, index=False)
    config = strategy_config(day - timedelta(days=10), day)
    with contextlib.redirect_stdout(io.StringIO()):
        _, _, trades, _ = backtest.run_backtest(config)
    if trades is None or trades.empty:
        return "flat", "今天没有开仓信号"
    trades = trades[pd.to_datetime(trades["entry_time"]).dt.date == day]
    if trades.empty:
        return "flat", "今天没有开仓信号"
    last = trades.iloc[-1]
    entry_time = pd.Timestamp(last["entry_time"])
    exit_time = pd.Timestamp(last["exit_time"])
    side = "long" if last["side"] == "Long" else "short"
    # 回放到数据末尾仍持仓时，backtest 会在最后一根 K 记一笔 Market Close
    if last["exit_reason"] == "Market Close" and exit_time == today_bars["DateTime"].iloc[-1]:
        return side, f"{entry_time:%H:%M} 入场 {side} @ {last['entry_price']:.2f}，今天第 {len(trades)} 笔"
    return "flat", (f"{side} {entry_time:%H:%M} @ {last['entry_price']:.2f} → "
                    f"{exit_time:%H:%M} @ {last['exit_price']:.2f} 出场（{last['exit_reason']}）")


def in_session(now):
    if now.weekday() >= 5:
        return False
    return SESSION_OPEN <= now.time() < SESSION_CLOSE


def position_side(positions):
    for pos in positions:
        if pos.get("symbol") != SYMBOL:
            continue
        side = str(pos.get("side", "")).lower()
        if side in ("long", "buy"):
            return "long", pos
        if side in ("short", "sell"):
            return "short", pos
    return "flat", None


def lots_for(price, initial, size_mult=1.0):
    if price < MIN_PRICE or price > MAX_PRICE:
        raise RuntimeError(f"价格异常 {price}，拒绝下单")
    notional = initial * NOTIONAL_MULT * size_mult
    lots = int(notional / price / LOT_STEP) * LOT_STEP
    lots = min(lots, MAX_LOTS)
    if lots < LOT_STEP:
        raise RuntimeError(f"计算出的手数 {lots} 低于最小手数")
    return round(lots, 2), round(notional, 2)


def aggressive_limit(side, price):
    if side == "buy":
        return round(price * (1 + CROSS_PCT), 2)
    return round(price * (1 - CROSS_PCT), 2)


def stop_price(side, price, lots):
    distance = STOP_LOSS_USD / lots
    if side == "buy":
        return round(price - distance, 2)
    return round(price + distance, 2)


def open_position(account, side, lots, price):
    order_side = "buy" if side == "long" else "sell"
    limit = aggressive_limit(order_side, price)
    stop = stop_price(order_side, price, lots)
    client_id = f"e8f-{int(time.time())}-{side}-{lots}"
    result = call("e8_order_place", {
        "accountId": account,
        "symbol": SYMBOL,
        "side": order_side,
        "type": "limit",
        "amount": lots,
        "limitPrice": limit,
        "stopLoss": stop,
        "slippage": 50,
        "clientOrderId": client_id,
    })
    order = result.get("order") or {}
    log(f"提交开仓 {side} {lots} 张 限价 {limit} 止损 {stop} 状态 {order.get('status')} id {order.get('id')}")
    return order.get("id")


def cancel_order(order_id):
    if not order_id:
        return
    try:
        call("e8_order_cancel", {"orderId": order_id})
        log(f"撤销未成交挂单 {order_id}")
    except RuntimeError as exc:
        log(f"撤单失败（可能已成交）{order_id}: {exc}")


def close_position(account):
    result = call("e8_position_close", {"accountId": account, "symbol": SYMBOL, "slippage": 50})
    log(f"平仓 {result.get('status')} 成交价 {result.get('executedPrice')} 盈亏 {result.get('realizedPnl')}")


def risk_block(limits):
    """返回需要强制空仓的原因；不需要时返回空字符串。"""
    if not limits:
        return ""
    breached = limits.get("breached") or {}
    if any(breached.get(k) for k in ("daily", "total", "margin")):
        return "账户已触线"
    pnl = (limits.get("pnl") or {}).get("today")
    room = limits.get("room") or {}
    if pnl is not None and pnl <= -DAILY_LOSS_HALT:
        return f"当日已亏 ${pnl:.0f}，停在日亏线前面"
    if pnl is not None and pnl >= PROFIT_CAP_HALT:
        return f"当日已盈 ${pnl:.0f}，停在单日计入上限前面"
    daily_room = room.get("dailyPct")
    if daily_room is not None and daily_room <= DAILY_ROOM_HALT:
        return f"日亏剩余 {daily_room:.2f} 个百分点"
    total_room = room.get("totalPct")
    if total_room is not None and total_room <= STATIC_ROOM_HALT:
        return f"总亏剩余 {total_room:.2f} 个百分点"
    return ""


def main():
    account = os.environ["E8_ACCOUNT_ID"]
    limits = call("e8_account_limits", {"accountId": account})
    initial = float(limits.get("initialBalance") or 5000)
    log(f"E8 跟单启动 账户 {limits.get('accountId')} 初始 ${initial:.0f} "
        f"名义 {NOTIONAL_MULT:g}x=${initial * NOTIONAL_MULT:.0f} 品种 {SYMBOL}")
    log(f"日亏停 ${DAILY_LOSS_HALT:.0f} / 官方 ${initial * 0.025:.0f}；"
        f"总亏底线 ${initial * 0.92:.0f}，剩余 {STATIC_ROOM_HALT:g} 个百分点即停；"
        f"单日盈利停 ${PROFIT_CAP_HALT:.0f}")
    log(f"信号: 自算 {SIGNAL_SYMBOL}（backtest.py 回放，策略 {STRATEGY_VERSION}），行情缓存 {MARKET_DB}")
    log(f"  {k_side_adjust.format_k_strategy_params(K1, K2, LOOKBACK_DAYS, ENABLE_K_SIDE_ADJUSTMENT)}，"
        f"{CHECK_INTERVAL_MINUTES} 分钟检查 {STRATEGY_START[0]:02d}:{STRATEGY_START[1]:02d}-"
        f"{STRATEGY_END[0]:02d}:{STRATEGY_END[1]:02d}；{quiet_open.describe()}")

    today = None
    desired = "flat"
    signal_note = ""
    last_cutoff = None
    last_market_note = None
    pending_order = None
    pending_since = 0.0
    last_block = ""

    while True:
        now = datetime.now(ET)
        if now.date() != today:
            today = now.date()
            desired = "flat"
            last_cutoff = None
            log("新的一天，目标仓位清成空仓，等待今天的信号")

        if in_session(now):
            tradable, market_note, cutoff = market_state(now)
            if market_note != last_market_note:
                if market_note:
                    log(f"行情/日历: {market_note}")
                last_market_note = market_note
            if not tradable:
                new_desired, new_note = "flat", market_note
            elif cutoff is None or cutoff == last_cutoff:
                new_desired, new_note = desired, signal_note
            else:
                try:
                    minutes = load_minutes(today, cutoff)
                    quiet_open.update(minutes.assign(Date=minutes["DateTime"].dt.date,
                                                     Time=minutes["DateTime"].dt.strftime("%H:%M")), now)
                    new_desired, new_note = strategy_target(today, minutes)
                    last_cutoff = cutoff
                except Exception as exc:  # noqa: BLE001 回放失败时保持原目标，下一分钟再算
                    log(f"信号回放失败，保持目标 {desired}: {exc!r}")
                    new_desired, new_note = desired, signal_note
            if new_desired != desired:
                log(f"策略信号 {desired} → {new_desired}：{new_note}")
            desired, signal_note = new_desired, new_note

        try:
            limits = call("e8_account_limits", {"accountId": account})
            positions = call("e8_positions_list", {"accountId": account}).get("positions") or []
        except (RuntimeError, urllib.error.URLError, TimeoutError, OSError) as exc:
            log(f"查询失败，下轮重试: {exc}")
            time.sleep(5)
            continue

        reason = risk_block(limits)
        target = desired if in_session(now) and not reason else "flat"
        if reason != last_block:
            if reason:
                log(f"风控空仓: {reason}")
            last_block = reason
        actual, _pos = position_side(positions)

        if pending_order and time.time() - pending_since > 20 and actual == "flat":
            cancel_order(pending_order)
            pending_order = None

        try:
            if actual != "flat" and actual != target:
                close_position(account)
                pending_order = None
            elif actual == "flat" and target != "flat":
                if pending_order:
                    pass
                else:
                    price = float(call("e8_trade_asset_get", {"symbol": SYMBOL})["lastPrice"])
                    size_mult = quiet_open.current_multiplier(today)
                    lots, notional = lots_for(price, initial, size_mult)
                    log(f"开仓目标 {target} 价格 {price} 手数 {lots} 名义约 ${lots * price:.0f}"
                        f"（计划 ${notional:.0f}，冷清系数 {size_mult:g}）")
                    pending_order = open_position(account, target, lots, price)
                    pending_since = time.time()
            elif actual == target and actual != "flat":
                pending_order = None
        except (RuntimeError, urllib.error.URLError, TimeoutError, OSError) as exc:
            log(f"下单失败，下轮重试: {exc}")

        busy = in_session(now) or actual != "flat" or pending_order
        time.sleep(ACTIVE_POLL_SEC if busy else IDLE_POLL_SEC)


if __name__ == "__main__":
    main()
